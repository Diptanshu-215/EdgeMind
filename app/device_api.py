"""HTTP API of one device process (one tablet). Started by the launcher with
EDGEMIND_DEVICE=tablet-A. Everything here works with the network off."""
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import answer, config, embeddings, policy
from .device import EdgeDevice
from .sync_client import SyncClient

DEV: EdgeDevice = None
SYNC: SyncClient = None
SYNC_LOCK = threading.Lock()
STOP = threading.Event()


def run_sync(reason):
    with SYNC_LOCK:
        return SYNC.sync(reason)


def loop():
    while not STOP.wait(config.AUTO_SYNC_SECONDS):
        if DEV.online and DEV.meta("auto_sync", True):
            try:
                run_sync("auto")
            except Exception as exc:
                print("[auto-sync]", exc)


@asynccontextmanager
async def lifespan(_app):
    global DEV, SYNC
    dev_id = config.DEVICE_ID or "tablet-A"
    embeddings.get()
    DEV = EdgeDevice(dev_id)
    SYNC = SyncClient(DEV)
    threading.Thread(target=lambda: run_sync("startup"), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    yield
    STOP.set()
    with SYNC_LOCK:
        DEV.close()


app = FastAPI(title="EdgeMind device", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class MemoryIn(BaseModel):
    text: str
    title: str = ""
    kind: str = "note"
    site: str | None = None
    visibility: str = "auto"
    doc_id: str | None = None


class SearchIn(BaseModel):
    q: str
    mode: str = "hybrid"
    kind: str | None = None
    limit: int = 6


class Toggle(BaseModel):
    value: bool


class ResolveIn(BaseModel):
    strategy: str


class PolicyIn(BaseModel):
    text: str
    kind: str = "note"
    visibility: str = "auto"


class PairIn(BaseModel):
    doc_id: str
    other_id: str


@app.get("/state")
def state():
    last = DEV.meta("last_sync") or {}
    return {
        "id": DEV.id, "label": DEV.label, "sites": DEV.sites, "online": DEV.online,
        "auto_sync": DEV.meta("auto_sync", True), "connected": DEV.online and SYNC.connected and bool(last.get("ok")),
        "sync_error": last.get("error"), "last_sync": last, "stats": DEV.stats(),
        "snapshots": DEV.meta("cloud_snapshots"), "mirror_seq": {s: DEV.mirror_seq(s) for s in DEV.sites},
        "embedder": embeddings.get().dense.name, "llm": answer.llm_available(), "kinds": policy.KINDS,
        "offline_since": DEV.meta("offline_since"),
    }


@app.get("/memory")
def memory():
    return DEV.merged_docs()


@app.post("/memory")
def write(body: MemoryIn):
    if not body.text.strip():
        raise HTTPException(400, "text is empty")
    if body.kind not in policy.KINDS:
        raise HTTPException(400, f"kind must be one of {policy.KINDS}")
    with SYNC_LOCK:
        doc = DEV.write(body.text, body.title, body.kind, body.site, body.visibility, body.doc_id)
    if DEV.online:
        threading.Thread(target=lambda: run_sync("write"), daemon=True).start()
    return doc


@app.delete("/memory/{doc_id}")
def delete(doc_id: str):
    with SYNC_LOCK:
        ok = DEV.delete(doc_id)
    if ok and DEV.online:
        threading.Thread(target=lambda: run_sync("delete"), daemon=True).start()
    return {"ok": ok}


@app.post("/search")
def search(body: SearchIn):
    if not body.q.strip():
        raise HTTPException(400, "empty query")
    return DEV.search(body.q, body.limit, body.kind, body.mode)


@app.post("/ask")
def ask(body: SearchIn):
    if not body.q.strip():
        raise HTTPException(400, "empty question")
    found = DEV.search(body.q, 5, None, "hybrid")
    sources = [r for r in found["results"] if r["match"] >= embeddings.get().miss_threshold * 0.8][:5] or found["results"][:3]
    out = answer.answer(body.q, sources, confident=not found["miss"])
    out.update(search_ms=found["latency_ms"], miss=found["miss"], top_match=found["top_match"], results=sources)
    return out


@app.post("/ask-fleet")
def ask_fleet(body: SearchIn):
    DEV.record_miss(body.q)
    if DEV.online:
        threading.Thread(target=lambda: run_sync("ask fleet"), daemon=True).start()
    return {"ok": True}


@app.post("/online")
def set_online(body: Toggle):
    DEV.set_meta("online", body.value)
    if body.value:
        DEV.log("network", "back ONLINE: syncing")
        return {"online": True, "sync": run_sync("reconnect")}
    DEV.set_meta("offline_since", time.time())
    DEV.log("network", "went OFFLINE: working from on-device memory", "warn")
    return {"online": False}


@app.post("/auto-sync")
def auto(body: Toggle):
    DEV.set_meta("auto_sync", body.value)
    return {"auto_sync": body.value}


@app.post("/sync")
def sync_now():
    return run_sync("manual")


@app.get("/outbox")
def outbox():
    items = DEV.outbox()
    for it in items:
        d = DEV.get_local(it["doc_id"])
        it["title"] = d["title"] if d else "?"
        it["kind"] = d["kind"] if d else "?"
    return items


@app.get("/review")
def review():
    return {"conflicts": DEV.conflicts(), "contradictions": DEV.contradictions(), "suggestions": DEV.suggestions()}


@app.post("/conflicts/{doc_id}/resolve")
def resolve(doc_id: str, body: ResolveIn):
    if body.strategy not in ("mine", "theirs", "merge"):
        raise HTTPException(400, "strategy must be mine, theirs or merge")
    with SYNC_LOCK:
        if not SYNC.resolve(doc_id, body.strategy):
            raise HTTPException(404, "no such conflict")
    if DEV.online:
        threading.Thread(target=lambda: run_sync("resolve"), daemon=True).start()
    return {"ok": True}


@app.post("/suggestions/{doc_id}/share")
def share(doc_id: str):
    with SYNC_LOCK:
        doc = DEV.share(doc_id)
    if not doc:
        raise HTTPException(404, "not found")
    if DEV.online:
        threading.Thread(target=lambda: run_sync("share"), daemon=True).start()
    return doc


@app.post("/contradictions/dismiss")
def dismiss(body: PairIn):
    DEV.dismiss_contradiction(body.doc_id, body.other_id)
    return {"ok": True}


@app.post("/policy/preview")
def preview(body: PolicyIn):
    dense = embeddings.get().embed_dense(body.text)
    d = policy.decide(body.text, body.kind, body.visibility, DEV.best_demand(dense), 0,
                      embeddings.get().demand_threshold)
    if d["decision"] == "sync_redacted":
        d["uploaded_text"] = policy.redact(body.text)
    return d


@app.get("/log")
def log(limit: int = 60):
    return DEV.recent_log(limit)


@app.get("/metrics")
def metrics():
    return DEV.metrics()
