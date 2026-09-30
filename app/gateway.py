"""Cloud sync gateway: the only thing edge nodes talk to when they have an uplink.

Why a gateway instead of nodes writing to Qdrant directly:
  * enrollment                    -> a node joins with the fleet join code and gets its own
                                     token (stored hashed); tokens can be revoked
  * site access control           -> a node reads and writes only the sites it enrolled for
  * server-side sequence numbers  -> incremental sync never depends on device clocks
  * compare-and-set on `version`  -> two nodes can't both overwrite the same memory
  * snapshot proxy                -> nodes restore/refresh Qdrant Edge shards from
                                     Qdrant Server full/partial snapshots through here
  * fleet demand                  -> collects failed searches so nodes can decide
                                     what private knowledge is worth sharing
  * live push (SSE)               -> nodes hold an event stream; a new write anywhere makes
                                     every subscribed node sync within a second. The open
                                     stream is also the node's presence.

Qdrant Server itself listens on loopback only; nothing outside the hub can reach it.

Run: uvicorn app.gateway:app --host 0.0.0.0 --port 8100
"""
import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from . import config, embeddings, seed
from .cloudstore import CloudStore

SSE_HEADERS = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform",
               "X-Accel-Buffering": "no"}  # exact type: proxies (Cloudflare) only stream this

app = FastAPI(title="EdgeMind sync gateway")

SECRETS = config.hub_secrets()
STATE_DIR = config.DATA_DIR / "gateway"
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(STATE_DIR / "gateway.sqlite", check_same_thread=False)
LOCK = threading.RLock()


def _init_db():
    cols = {r[1] for r in DB.execute("PRAGMA table_info(devices)").fetchall()}
    if cols and "token_hash" not in cols:  # data from an older version without enrollment
        DB.execute("DROP TABLE devices")
    DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS demand(id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, query TEXT,
            dense TEXT, top REAL, ts REAL);
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, label TEXT, sites TEXT, token_hash TEXT,
            url TEXT, addr TEXT, enrolled_at REAL, last_seen REAL, stats TEXT, revoked INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, device TEXT, event TEXT,
            detail TEXT, bytes INTEGER DEFAULT 0);
        """
    )
    DB.commit()


_init_db()
STORE = CloudStore()


# ------------------------------------------------------------------ helpers
def meta(key, default=0):
    with LOCK:
        row = DB.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(key, value):
    with LOCK:
        DB.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))
        DB.commit()


class Bus:
    """In-memory change counter + fleet heads, read by the SSE streams without touching SQLite."""

    def __init__(self):
        self.v = 0
        self.demand_v = 0
        self.heads = {}
        self.epoch = ""
        self.streams = {}  # device id -> open event streams (presence)

    def bump(self, demand=False):
        with LOCK:
            self.v += 1
            if demand:
                self.demand_v += 1


BUS = Bus()


def load_heads():
    if meta("schema", 0) != config.SCHEMA or meta("embedder", "") != embeddings.get().name:
        # collections from an older layout / another embedding model: rebuild them (new epoch below)
        STORE.reset()
        DB.executescript("DELETE FROM meta; DELETE FROM demand;")
        set_meta("schema", config.SCHEMA)
        set_meta("embedder", embeddings.get().name)
        set_meta("needs_seed", True)
    if not meta("epoch", ""):
        set_meta("epoch", uuid.uuid4().hex[:12])
    BUS.epoch = meta("epoch", "")
    BUS.heads = {s: meta(f"head_{s}", 0) for s in config.SITES}


load_heads()


def next_seq(site):
    with LOCK:
        seq = meta("seq", 0) + 1
        set_meta("seq", seq)
        set_meta(f"head_{site}", seq)
        BUS.heads[site] = seq
    return seq


def glog(device, event, detail="", nbytes=0):
    with LOCK:
        DB.execute("INSERT INTO log(ts,device,event,detail,bytes) VALUES(?,?,?,?,?)",
                   (time.time(), device, event, detail, nbytes))
        DB.commit()
    BUS.bump()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def device_auth(x_device_token: str = Header(default="")):
    if not x_device_token:
        raise HTTPException(401, "missing device token")
    with LOCK:
        row = DB.execute("SELECT id, sites, revoked, label FROM devices WHERE token_hash=?",
                         (token_hash(x_device_token),)).fetchone()
    if not row or row[2]:
        raise HTTPException(401, "unknown or revoked device token")
    return {"id": row[0], "sites": json.loads(row[1]), "label": row[3]}


def admin_auth(x_admin_token: str = Header(default="")):
    if not x_admin_token or not hmac.compare_digest(x_admin_token, SECRETS["admin_token"]):
        raise HTTPException(401, "admin token required")


def _site_allowed(dev, site):
    if site not in config.SITES:
        raise HTTPException(404, "unknown site")
    if site not in dev["sites"]:
        raise HTTPException(403, f"{dev['id']} is not enrolled for site '{site}'")


def _content(p):
    return (p.get("title", "").strip(), p.get("text", "").strip(), bool(p.get("deleted")))


def _presence(dev_id, last_seen, now):
    streaming = BUS.streams.get(dev_id, 0) > 0
    return streaming or (last_seen and now - last_seen < config.PRESENCE_SECONDS), streaming


# ------------------------------------------------------------------ models
class EnrollIn(BaseModel):
    join_code: str = Field(max_length=40)
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{2,40}$")
    label: str = Field(default="", max_length=80)
    sites: list[str] = Field(min_length=1, max_length=10)
    url: str = Field(default="", max_length=200)


class PushItem(BaseModel):
    doc_id: str = Field(max_length=64)
    site: str
    base_version: int
    payload: dict
    dense: list[float] = Field(min_length=config.DENSE_DIM, max_length=config.DENSE_DIM)
    text: str = Field(default="", max_length=config.MAX_TEXT + config.MAX_TITLE + 10)
    image: list[float] | None = Field(default=None, min_length=config.IMAGE_DIM, max_length=config.IMAGE_DIM)


class PushIn(BaseModel):
    items: list[PushItem] = Field(max_length=200)


class Miss(BaseModel):
    query: str = Field(max_length=config.MAX_QUERY)
    dense: list[float] = Field(min_length=config.DENSE_DIM, max_length=config.DENSE_DIM)
    top: float = 0.0


class HelloIn(BaseModel):
    stats: dict = {}
    misses: list[Miss] = Field(default=[], max_length=50)
    url: str = Field(default="", max_length=200)
    label: str = Field(default="", max_length=80)


class SearchIn(BaseModel):
    q: str = Field(max_length=config.MAX_QUERY)
    limit: int = Field(default=6, ge=1, le=50)


# ------------------------------------------------------------------ public
@app.get("/health")
def health():
    return {"ok": STORE.ping(), "mode": STORE.mode, "snapshots": STORE.server, "epoch": BUS.epoch,
            "needs_seed": bool(meta("needs_seed", False)),
            "embedder": embeddings.get().name, "sites": config.SITES}


@app.post("/enroll")
def enroll(body: EnrollIn, request: Request):
    """A new edge node joins the fleet with the join code and receives its own token."""
    if not hmac.compare_digest(body.join_code.strip().upper(), SECRETS["join_code"].upper()):
        glog(body.device_id, "enroll refused", f"wrong join code from {request.client.host}")
        raise HTTPException(403, "wrong join code")
    sites = [s for s in dict.fromkeys(body.sites) if s in config.SITES]
    if not sites:
        raise HTTPException(400, f"sites must be some of {list(config.SITES)}")
    token = secrets.token_urlsafe(32)
    now = time.time()
    with LOCK:
        DB.execute(
            "INSERT INTO devices(id,label,sites,token_hash,url,addr,enrolled_at,last_seen,stats,revoked) "
            "VALUES(?,?,?,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET label=excluded.label, sites=excluded.sites, "
            "token_hash=excluded.token_hash, url=excluded.url, addr=excluded.addr, revoked=0",
            (body.device_id, body.label or body.device_id, json.dumps(sites), token_hash(token), body.url,
             request.client.host, now, now, "{}"))
        DB.commit()
    glog(body.device_id, "enrolled", f"{body.label or body.device_id} joined the fleet from {request.client.host} "
                                     f"· sites {', '.join(sites)}")
    return {"device_id": body.device_id, "token": token, "sites": sites, "epoch": BUS.epoch, "server_time": now}


# ------------------------------------------------------------------ node endpoints
@app.post("/sync/hello")
def hello(body: HelloIn, request: Request, dev: dict = Depends(device_auth)):
    """One round trip per sync cycle: heartbeat + report failed searches + learn fleet demand + site heads."""
    now = time.time()
    did = dev["id"]
    with LOCK:
        DB.execute("UPDATE devices SET last_seen=?, stats=?, url=COALESCE(NULLIF(?,''),url), addr=?, "
                   "label=COALESCE(NULLIF(?,''),label) WHERE id=?",
                   (now, json.dumps(body.stats), body.url, request.client.host, body.label, did))
        for miss in body.misses:
            DB.execute("INSERT INTO demand(device,query,dense,top,ts) VALUES(?,?,?,?,?)",
                       (did, miss.query, json.dumps(miss.dense), miss.top, now))
        DB.commit()
        rows = DB.execute("SELECT device,query,dense,top,ts FROM demand WHERE device!=? AND ts>? ORDER BY ts DESC LIMIT 50",
                          (did, now - config.DEMAND_WINDOW_SECONDS)).fetchall()
    for miss in body.misses:
        glog(did, "fleet demand", f"no good answer on device for '{miss.query}'")
    if body.misses:
        BUS.bump(demand=True)
    demand = [{"device": r[0], "query": r[1], "dense": json.loads(r[2]), "top": r[3], "ts": r[4]} for r in rows]
    return {"device": did, "sites": dev["sites"], "demand": demand,
            "heads": {s: BUS.heads.get(s, 0) for s in dev["sites"]}, "snapshots": STORE.server,
            "server_time": now, "epoch": BUS.epoch, "embedder": embeddings.get().name}


@app.get("/sync/events")
async def node_events(request: Request, dev: dict = Depends(device_auth)):
    """Live channel for a node: pushes site heads and demand changes the moment they happen."""
    did, sites = dev["id"], dev["sites"]

    def snapshot():
        return {"heads": {s: BUS.heads.get(s, 0) for s in sites}, "demand_v": BUS.demand_v, "epoch": BUS.epoch,
                "server_time": time.time()}

    async def gen():
        BUS.streams[did] = BUS.streams.get(did, 0) + 1
        BUS.bump()
        last, last_sent = None, 0.0
        try:
            while not await request.is_disconnected():
                snap = snapshot()
                key = (json.dumps(snap["heads"], sort_keys=True), snap["demand_v"], snap["epoch"])
                if key != last:
                    last, last_sent = key, time.time()
                    yield f"data: {json.dumps(snap)}\n\n"
                elif time.time() - last_sent > 10:
                    last_sent = time.time()
                    yield ": ping\n\n"
                await asyncio.sleep(0.2)
        finally:
            BUS.streams[did] = max(0, BUS.streams.get(did, 1) - 1)
            BUS.bump()

    return StreamingResponse(gen(), headers=SSE_HEADERS)


@app.post("/push")
def push(body: PushIn, dev: dict = Depends(device_auth)):
    """Compare-and-set upsert. An item is accepted only if the cloud copy is still at
    `base_version` (or is a tombstone, in which case an edit wins over a delete)."""
    emb = embeddings.get()
    did = dev["id"]
    results = []
    with LOCK:
        for it in body.items:
            if it.site not in config.SITES or it.site not in dev["sites"]:
                results.append({"doc_id": it.doc_id, "status": "rejected",
                                "error": f"{did} is not enrolled for site '{it.site}'"})
                glog(did, "push rejected", f"'{it.payload.get('title')}' → {it.site}: not enrolled for that site")
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
                    glog(did, "conflict", f"'{it.payload.get('title')}' changed by {c.get('author_device')} "
                                          f"since v{it.base_version}")
                    continue
            similar = None
            if not c and not tomb:
                similar = STORE.find_similar(it.dense, it.doc_id, emb.dup_threshold, sites=dev["sites"])
            version = (c.get("version", 0) if c else 0) + 1
            seq = next_seq(it.site)
            payload = config.with_flags(dict(it.payload, doc_id=it.doc_id, site=it.site, version=version, seq=seq,
                                             synced_at=time.time(), author_device=did))
            if similar:
                payload["similar_to"] = similar
            STORE.upsert(it.site, it.doc_id, it.dense, "" if tomb else it.text, payload, None if tomb else it.image)
            what = "deleted" if tomb else ("redacted copy of" if payload.get("redacted") else "pushed")
            glog(did, "push", f"{what} '{payload.get('title')}' → {it.site} v{version} (seq {seq})")
            results.append({"doc_id": it.doc_id, "status": "accepted", "version": version, "seq": seq,
                            "similar_to": similar, "resurrected": bool(c and c.get("deleted") and not tomb)})
    return {"results": results}


@app.get("/sites/{site}/changes")
def changes(site: str, since: int = 0, limit: int = 500, dev: dict = Depends(device_auth)):
    """Point delta: every memory of a site with seq > since. The head is read under the push
    lock first, so a node that stores `head` can never skip a write."""
    _site_allowed(dev, site)
    with LOCK:
        head = BUS.heads.get(site, 0)
        docs = STORE.changes_since(site, since, limit=min(limit, 500))
    if len(docs) == min(limit, 500):
        head = docs[-1]["payload"]["seq"]  # truncated: the node continues from here next time
    nbytes = len(json.dumps(docs))
    if docs:
        glog(dev["id"], "point delta", f"{site}: {len(docs)} changed memories, {nbytes / 1024:.1f} KB", nbytes)
    return {"site": site, "head": head, "changes": docs}


def _snapshot_stream(site, dev_id, kind, manifest=None):
    with LOCK:  # the head must be read before the snapshot is cut (writes after it get a higher seq)
        head = BUS.heads.get(site, 0)
    resp, layers = STORE.open_snapshot(site, manifest)
    first, rest = STORE.first_chunk(resp)
    if not first:  # the node already holds every segment: nothing to send
        resp.close()
        return Response(status_code=204, headers={"X-Site-Seq": str(head)})
    stats = {}

    def body():
        yield from STORE.wrap(resp, stats, first, rest)
        glog(dev_id, f"{kind} snapshot", f"{site}: {stats['wire'] / 1024:.0f} KB on the wire", stats["wire"])

    return StreamingResponse(body(), media_type="application/octet-stream",
                             headers={"X-Site-Seq": str(head), "X-Gzip-Layers": str(layers + 1)})


@app.get("/sites/{site}/snapshot")
def full_snapshot(site: str, dev: dict = Depends(device_auth)):
    _site_allowed(dev, site)
    if not STORE.server:
        raise HTTPException(409, "snapshots need Qdrant Server")
    return _snapshot_stream(site, dev["id"], "full")


@app.post("/sites/{site}/snapshot/partial")
async def partial_snapshot(site: str, request: Request, dev: dict = Depends(device_auth)):
    _site_allowed(dev, site)
    if not STORE.server:
        raise HTTPException(409, "snapshots need Qdrant Server")
    return _snapshot_stream(site, dev["id"], "partial", await request.json())


# ------------------------------------------------------------------ operator endpoints (admin token)
@app.get("/memory", dependencies=[Depends(admin_auth)])
def memory():
    return STORE.list()


@app.post("/search", dependencies=[Depends(admin_auth)])
def search(body: SearchIn):
    return STORE.search(body.q, limit=body.limit)


@app.get("/log", dependencies=[Depends(admin_auth)])
def log(limit: int = 60):
    with LOCK:
        rows = DB.execute("SELECT ts,device,event,detail,bytes FROM log ORDER BY id DESC LIMIT ?",
                          (min(limit, 500),)).fetchall()
    return [{"ts": r[0], "device": r[1], "event": r[2], "detail": r[3], "bytes": r[4]} for r in rows]


@app.get("/fleet", dependencies=[Depends(admin_auth)])
def fleet():
    now = time.time()
    with LOCK:
        rows = DB.execute("SELECT id,label,sites,url,addr,enrolled_at,last_seen,stats,revoked FROM devices "
                          "ORDER BY enrolled_at").fetchall()
        demand = DB.execute("SELECT device,query,ts FROM demand WHERE ts>? ORDER BY ts DESC LIMIT 10",
                            (now - config.DEMAND_WINDOW_SECONDS,)).fetchall()
        bytes_total = DB.execute("SELECT COALESCE(SUM(bytes),0) FROM log").fetchone()[0]
    devices = []
    for r in rows:
        online, streaming = _presence(r[0], r[6], now)
        devices.append({"id": r[0], "label": r[1], "sites": json.loads(r[2]), "url": r[3], "addr": r[4],
                        "enrolled_at": r[5], "last_seen": r[6], "seen_ago": round(now - (r[6] or now), 1),
                        "online": bool(online) and not r[8], "live": streaming, "stats": json.loads(r[7] or "{}"),
                        "revoked": bool(r[8])})
    return {
        "mode": STORE.mode, "snapshots": STORE.server, "count": STORE.count(), "epoch": BUS.epoch,
        "heads": dict(BUS.heads), "devices": devices,
        "demand": [{"device": d[0], "query": d[1], "ts": d[2]} for d in demand],
        "bytes_total": bytes_total,
    }


@app.get("/events", dependencies=[Depends(admin_auth)])
async def events(request: Request):
    """Operator live channel: one event per fleet change (push, enrollment, presence, demand)."""
    async def gen():
        last, last_sent = None, 0.0
        while not await request.is_disconnected():
            if BUS.v != last:
                last, last_sent = BUS.v, time.time()
                live = sorted(d for d, n in BUS.streams.items() if n > 0)
                yield f"data: {json.dumps({'v': BUS.v, 'heads': BUS.heads, 'live': live})}\n\n"
            elif time.time() - last_sent > 10:
                last_sent = time.time()
                yield ": ping\n\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(gen(), headers=SSE_HEADERS)


@app.delete("/admin/devices/{device_id}", dependencies=[Depends(admin_auth)])
def revoke(device_id: str):
    with LOCK:
        n = DB.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,)).rowcount
        DB.commit()
    if not n:
        raise HTTPException(404, "no such device")
    glog("hq-console", "revoked", f"{device_id} can no longer sync")
    return {"ok": True}


@app.post("/admin/reset", dependencies=[Depends(admin_auth)])
def admin_reset(with_seed: bool = True):
    """Wipe fleet data and start a new epoch. Enrolled nodes keep their tokens; on their next
    sync they see the new epoch, drop their mirrors and re-publish their own knowledge."""
    with LOCK:
        STORE.reset()
        DB.executescript("DELETE FROM meta; DELETE FROM demand; DELETE FROM log;")
        DB.commit()
        set_meta("schema", config.SCHEMA)
        set_meta("embedder", embeddings.get().name)
        set_meta("epoch", uuid.uuid4().hex[:12])
        load_heads()
        n = 0
        if with_seed:
            for item in seed.fleet_docs():
                seq = next_seq(item["site"])
                config.with_flags(item["payload"]).update(version=1, seq=seq, synced_at=time.time())
                STORE.upsert(item["site"], item["doc_id"], item["dense"], item["text"], item["payload"])
                n += 1
        glog("hq-console", "seeded", f"{n} fleet memories across {len(config.SITES)} sites")
    embeddings.get().release()  # the gateway only needs the text model to seed
    return {"ok": True, "seeded": n, "epoch": BUS.epoch}
