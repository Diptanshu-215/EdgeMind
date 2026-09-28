"""Cloud sync gateway: the only thing devices talk to when they are online.

Why a gateway instead of devices writing to Qdrant directly:
  * server-side sequence numbers  -> incremental sync never depends on device clocks
  * compare-and-set on `version`  -> two devices can't both overwrite the same memory
  * device tokens                 -> each tablet authenticates; nothing else can write
  * snapshot proxy                -> devices restore/refresh Qdrant Edge shards from
                                     Qdrant Server full/partial snapshots through here
  * fleet demand                  -> collects failed searches so devices can decide
                                     what private knowledge is worth sharing

Run: uvicorn app.gateway:app --port 8100
"""
import json
import sqlite3
import threading
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import config, embeddings, seed
from .cloudstore import CloudStore

app = FastAPI(title="EdgeMind sync gateway")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TOKENS = {v["token"]: k for k, v in config.DEVICES.items()}
STATE_DIR = config.DATA_DIR / "gateway"
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(STATE_DIR / "gateway.sqlite", check_same_thread=False)
DB.executescript(
    """
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS demand(id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, query TEXT,
        dense TEXT, top REAL, ts REAL);
    CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, last_seen REAL, stats TEXT);
    CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, device TEXT, event TEXT,
        detail TEXT, bytes INTEGER DEFAULT 0);
    """
)
LOCK = threading.RLock()
STORE = CloudStore()


# ------------------------------------------------------------------ helpers
def meta(key, default=0):
    row = DB.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(key, value):
    DB.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))
    DB.commit()


def next_seq(site):
    seq = meta("seq", 0) + 1
    set_meta("seq", seq)
    set_meta(f"head_{site}", seq)
    return seq


def glog(device, event, detail="", nbytes=0):
    DB.execute("INSERT INTO log(ts,device,event,detail,bytes) VALUES(?,?,?,?,?)",
               (time.time(), device, event, detail, nbytes))
    DB.commit()


def device_auth(x_device_token: str = Header(default="")):
    dev = TOKENS.get(x_device_token)
    if not dev:
        raise HTTPException(401, "unknown device token")
    return dev


def _content(p):
    return (p.get("title", "").strip(), p.get("text", "").strip(), bool(p.get("deleted")))


# ------------------------------------------------------------------ models
class PushItem(BaseModel):
    doc_id: str
    site: str
    base_version: int
    payload: dict
    dense: list[float]
    text: str = ""


class PushIn(BaseModel):
    items: list[PushItem]


class Miss(BaseModel):
    query: str
    dense: list[float]
    top: float = 0.0


class HelloIn(BaseModel):
    stats: dict = {}
    misses: list[Miss] = []


class SearchIn(BaseModel):
    q: str
    limit: int = 6


# ------------------------------------------------------------------ device endpoints
@app.get("/health")
def health():
    return {"ok": STORE.ping(), "mode": STORE.mode, "snapshots": STORE.server,
            "embedder": embeddings.get().dense.name}


@app.post("/sync/hello")
def hello(body: HelloIn, dev: str = Depends(device_auth)):
    """One round trip per sync cycle: heartbeat + report failed searches + learn fleet demand + site heads."""
    now = time.time()
    with LOCK:
        DB.execute("INSERT OR REPLACE INTO devices VALUES(?,?,?)", (dev, now, json.dumps(body.stats)))
        for miss in body.misses:
            DB.execute("INSERT INTO demand(device,query,dense,top,ts) VALUES(?,?,?,?,?)",
                       (dev, miss.query, json.dumps(miss.dense), miss.top, now))
            glog(dev, "fleet demand", f"no good answer on device for '{miss.query}'")
        DB.commit()
        rows = DB.execute("SELECT device,query,dense,top,ts FROM demand WHERE device!=? AND ts>? ORDER BY ts DESC LIMIT 50",
                          (dev, now - config.DEMAND_WINDOW_SECONDS)).fetchall()
        heads = {s: meta(f"head_{s}", 0) for s in config.SITES}
    demand = [{"device": r[0], "query": r[1], "dense": json.loads(r[2]), "top": r[3], "ts": r[4]} for r in rows]
    return {"device": dev, "demand": demand, "heads": heads, "snapshots": STORE.server, "server_time": now,
            "embedder": embeddings.get().dense.name}


@app.post("/push")
def push(body: PushIn, dev: str = Depends(device_auth)):
    """Compare-and-set upsert. An item is accepted only if the cloud copy is still at
    `base_version` (or is a tombstone, in which case an edit wins over a delete)."""
    emb = embeddings.get()
    results = []
    with LOCK:
        for it in body.items:
            if it.site not in config.SITES:
                results.append({"doc_id": it.doc_id, "status": "rejected", "error": f"unknown site {it.site}"})
                continue
            cur = STORE.get(it.site, it.doc_id)
            c = cur["payload"] if cur else None
            tomb = bool(it.payload.get("deleted"))
            if c and c.get("version", 0) != it.base_version:
                if _content(c) == _content(it.payload):
                    results.append({"doc_id": it.doc_id, "status": "accepted", "version": c["version"],
                                    "seq": c["seq"], "note": "identical"})
                    continue
                if not c.get("deleted"):
                    results.append({"doc_id": it.doc_id, "status": "conflict", "current": c})
                    glog(dev, "conflict", f"'{it.payload.get('title')}' changed by {c.get('author_device')} since v{it.base_version}")
                    continue
            similar = None
            if not c and not tomb:
                similar = STORE.find_similar(it.dense, it.doc_id, emb.dup_threshold)
            version = (c.get("version", 0) if c else 0) + 1
            seq = next_seq(it.site)
            payload = dict(it.payload, doc_id=it.doc_id, site=it.site, version=version, seq=seq,
                           synced_at=time.time(), author_device=dev)
            if similar:
                payload["similar_to"] = similar
            STORE.upsert(it.site, it.doc_id, it.dense, "" if tomb else it.text, payload)
            what = "deleted" if tomb else ("redacted copy of" if payload.get("redacted") else "pushed")
            glog(dev, "push", f"{what} '{payload.get('title')}' → {it.site} v{version} (seq {seq})")
            results.append({"doc_id": it.doc_id, "status": "accepted", "version": version, "seq": seq,
                            "similar_to": similar, "resurrected": bool(c and c.get("deleted") and not tomb)})
    return {"results": results}


@app.get("/sites/{site}/changes")
def changes(site: str, since: int = 0, dev: str = Depends(device_auth)):
    if site not in config.SITES:
        raise HTTPException(404, "unknown site")
    docs = STORE.changes_since(site, since)
    if docs:
        glog(dev, "point sync", f"{len(docs)} changes from {site}")
    return {"site": site, "head": meta(f"head_{site}", 0), "changes": docs}


def _snapshot_response(site, data, dev, kind):
    glog(dev, f"{kind} snapshot", f"{site}: {len(data) / 1024:.0f} KB", len(data))
    return Response(content=data, media_type="application/octet-stream",
                    headers={"X-Site-Seq": str(meta(f"head_{site}", 0)), "X-Bytes": str(len(data))})


@app.get("/sites/{site}/snapshot")
def full_snapshot(site: str, dev: str = Depends(device_auth)):
    if not STORE.server:
        raise HTTPException(409, "snapshots need Qdrant Server")
    with LOCK:
        head = meta(f"head_{site}", 0)
        data = STORE.full_snapshot(site)
    resp = _snapshot_response(site, data, dev, "full")
    resp.headers["X-Site-Seq"] = str(head)
    return resp


@app.post("/sites/{site}/snapshot/partial")
async def partial_snapshot(site: str, request: Request, dev: str = Depends(device_auth)):
    if not STORE.server:
        raise HTTPException(409, "snapshots need Qdrant Server")
    manifest = await request.json()
    with LOCK:
        head = meta(f"head_{site}", 0)
        data = STORE.partial_snapshot(site, manifest)
    resp = _snapshot_response(site, data, dev, "partial")
    resp.headers["X-Site-Seq"] = str(head)
    return resp


# ------------------------------------------------------------------ dashboard endpoints (read-only)
@app.get("/memory")
def memory():
    return STORE.list()


@app.post("/search")
def search(body: SearchIn):
    return STORE.search(body.q, limit=body.limit)


@app.get("/log")
def log(limit: int = 60):
    rows = DB.execute("SELECT ts,device,event,detail,bytes FROM log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"ts": r[0], "device": r[1], "event": r[2], "detail": r[3], "bytes": r[4]} for r in rows]


@app.get("/fleet")
def fleet():
    rows = DB.execute("SELECT id,last_seen,stats FROM devices").fetchall()
    now = time.time()
    demand = DB.execute("SELECT device,query,ts FROM demand WHERE ts>? ORDER BY ts DESC LIMIT 10",
                        (now - config.DEMAND_WINDOW_SECONDS,)).fetchall()
    return {
        "mode": STORE.mode, "snapshots": STORE.server, "count": STORE.count(),
        "heads": {s: meta(f"head_{s}", 0) for s in config.SITES},
        "devices": [{"id": r[0], "last_seen": r[1], "seen_ago": round(now - r[1], 1), "stats": json.loads(r[2])} for r in rows],
        "demand": [{"device": d[0], "query": d[1], "ts": d[2]} for d in demand],
        "bytes_total": DB.execute("SELECT COALESCE(SUM(bytes),0) FROM log").fetchone()[0],
    }


# ------------------------------------------------------------------ admin (used by the launcher)
def admin(x_admin_token: str = Header(default="")):
    if x_admin_token != "edgemind-admin":
        raise HTTPException(401, "admin only")


@app.post("/admin/reset", dependencies=[Depends(admin)])
def admin_reset(with_seed: bool = True):
    with LOCK:
        STORE.reset()
        DB.executescript("DELETE FROM meta; DELETE FROM demand; DELETE FROM devices; DELETE FROM log;")
        n = 0
        if with_seed:
            for item in seed.fleet_docs():
                seq = next_seq(item["site"])
                item["payload"].update(version=1, seq=seq, synced_at=time.time())
                STORE.upsert(item["site"], item["doc_id"], item["dense"], item["text"], item["payload"])
                n += 1
            glog("hq-console", "seeded", f"{n} fleet memories across {len(config.SITES)} sites")
    return {"ok": True, "seeded": n}
