"""HTTP API of one edge node (a crew's van box / tablet). Everything here works with the
uplink off. Crew phones connect to it over the node's local network and get the field app
at `/`; `/events` pushes every change live.

Identity comes from the environment (set by the hub launcher or `launch.py join`):
EDGEMIND_DEVICE, EDGEMIND_LABEL, EDGEMIND_SITES, EDGEMIND_JOIN_CODE, GATEWAY_URL.
"""
import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import answer, config, embeddings, lifeline, policy
from .device import EdgeDevice
from .sync_client import SyncClient

DEV: EdgeDevice = None
SYNC: SyncClient = None
SYNC_LOCK = threading.Lock()
STOP = threading.Event()
WAKE = threading.Event()
_reason = ["auto"]
STATIC = config.ROOT / "static"


def identity():
    dev_id = config.DEVICE_ID or "tablet-A"
    default = next((n for n in config.DEFAULT_NODES if n["id"] == dev_id), {})
    label = config.DEVICE_LABEL or default.get("label", dev_id)
    sites = config.DEVICE_SITES or default.get("sites", ["global"])
    port = config.DEVICE_PORT or default.get("port", 8001)
    url = config.DEVICE_PUBLIC_URL or f"http://{config.lan_ip()}:{port}"
    return dev_id, label, sites, url


def seed_demo_notes():
    """`launch.py join --demo-notes`: the demo's private notes, written once through the normal policy."""
    import os
    from . import seed
    flag = DEV.dir / ".seeded"
    notes = seed.DEVICE_NOTES.get(DEV.id)
    if os.getenv("EDGEMIND_DEMO_NOTES") != "1" or not notes or flag.exists():
        return
    for n in notes:
        DEV.write(n["text"], n.get("title", ""), n.get("kind", "note"))
    flag.write_text("ok")


def run_sync(reason):
    with SYNC_LOCK:
        return SYNC.sync(reason)


def request_sync(reason):
    """Coalesce: many triggers while a sync runs become one follow-up sync."""
    _reason[0] = reason
    WAKE.set()


def auto_enabled():
    return DEV.meta("auto_sync", True)


def worker():
    while not STOP.is_set():
        woke = WAKE.wait(3 if SYNC.poll_mode else config.AUTO_SYNC_SECONDS)
        WAKE.clear()
        if STOP.is_set():
            return
        if not DEV.online or (not woke and not auto_enabled()):
            continue
        try:
            run_sync(_reason[0] if woke else "auto")
        except Exception as exc:  # noqa: BLE001
            print("[sync-worker]", exc, flush=True)


@asynccontextmanager
async def lifespan(_app):
    global DEV, SYNC
    lifeline.watch_parent()
    dev_id, label, sites, url = identity()
    embeddings.get()
    DEV = EdgeDevice(dev_id, label, sites)
    seed_demo_notes()
    SYNC = SyncClient(DEV, url)
    threading.Thread(target=worker, daemon=True, name="sync-worker").start()
    threading.Thread(target=SYNC.listen, args=(request_sync, auto_enabled), daemon=True, name="live").start()
    request_sync("startup")
    threading.Timer(45, answer.warm).start()  # after boot has settled: first question has no cold start
    yield
    STOP.set()
    WAKE.set()
    SYNC.stop()
    with SYNC_LOCK:
        DEV.close()


SSE_HEADERS = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform",
               "X-Accel-Buffering": "no"}  # exact type: proxies (Cloudflare) only stream this

app = FastAPI(title="EdgeMind edge node", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Phones must never run a stale app.js after an update: revalidate (ETag makes it cheap)."""
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


class MemoryIn(BaseModel):
    text: str = Field(max_length=config.MAX_TEXT)
    title: str = Field(default="", max_length=config.MAX_TITLE)
    kind: str = "note"
    site: str | None = None
    visibility: str = Field(default="auto", pattern="^(auto|shared|private)$")
    doc_id: str | None = Field(default=None, max_length=64)
    photo: str | None = Field(default=None, max_length=6_000_000)  # data URL, resized on the phone


class SearchIn(BaseModel):
    q: str = Field(max_length=config.MAX_QUERY)
    mode: str = Field(default="hybrid", pattern="^(hybrid|dense|keyword|photo)$")
    site: str | None = None
    kind: str | None = None
    limit: int = Field(default=6, ge=1, le=30)


class PhotoIn(BaseModel):
    photo: str = Field(max_length=6_000_000)
    limit: int = Field(default=6, ge=1, le=30)


class Toggle(BaseModel):
    value: bool


class ResolveIn(BaseModel):
    strategy: str


class PolicyIn(BaseModel):
    text: str = Field(max_length=config.MAX_TEXT + config.MAX_TITLE)
    kind: str = "note"
    visibility: str = "auto"


class PairIn(BaseModel):
    doc_id: str
    other_id: str


@app.get("/")
def field_app():
    return FileResponse(STATIC / "field.html", headers={"Cache-Control": "no-cache"})


def state():
    last = DEV.meta("last_sync") or {}
    prop = DEV.meta("propagation_ms", []) or []
    m = DEV.metrics()
    return {
        "search": {"p50": m["search_p50_ms"], "p95": m["search_p95_ms"], "shard_p50": m["shard_p50_ms"],
                   "shard_p95": m["shard_p95_ms"], "count": m["searches"]},
        "id": DEV.id, "label": DEV.label, "sites": DEV.sites, "online": DEV.online,
        "auto_sync": DEV.meta("auto_sync", True), "connected": DEV.online and SYNC.connected and bool(last.get("ok")),
        "live": SYNC.live, "enrolled": bool(SYNC.token), "gateway": config.GATEWAY_URL, "url": SYNC.public_url,
        "sync_error": last.get("error"), "last_sync": last, "stats": DEV.stats(), "version": DEV.version,
        "snapshots": DEV.meta("cloud_snapshots"), "mirror_seq": {s: DEV.mirror_seq(s) for s in DEV.sites},
        "embedder": embeddings.get().dense.name, "llm": answer.llm_available(), "llm_model": config.OLLAMA_MODEL,
        "vision": embeddings.vision().available(), "kinds": policy.KINDS,
        "offline_since": DEV.meta("offline_since"), "propagation_ms": prop[-1] if prop else None,
        "demand": [{"device": d["device"], "query": d["query"]} for d in DEV.fleet_demand()][:5],
    }


_cache = {"v": None, "state": None}


def cached_state():
    v = DEV.version
    if _cache["v"] != v:
        _cache["state"], _cache["v"] = state(), v
    return _cache["state"]


@app.get("/state")
def get_state():
    return cached_state()


@app.get("/events")
async def events(request: Request):
    """Server-sent events: the node's full state plus new activity, pushed on every change."""
    async def gen():
        last_v, last_log, first, last_sent = None, 0, True, 0.0
        while not await request.is_disconnected():
            v = DEV.version
            if v != last_v:
                last_v = v
                st = await asyncio.to_thread(cached_state)
                rows = await asyncio.to_thread(DEV.recent_log, 40, last_log)
                if rows:
                    last_log = rows[0]["id"]
                yield f"data: {json.dumps({'v': v, 'state': st, 'log': rows, 'first': first})}\n\n"
                first, last_sent = False, time.time()
            elif time.time() - last_sent > 10:
                last_sent = time.time()
                yield ": ping\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), headers=SSE_HEADERS)


@app.get("/memory")
def memory():
    return DEV.merged_docs()


@app.post("/memory")
def write(body: MemoryIn):
    if not body.text.strip() and not body.photo:
        raise HTTPException(400, "text is empty")
    if body.kind not in policy.KINDS:
        raise HTTPException(400, f"kind must be one of {policy.KINDS}")
    with SYNC_LOCK:
        try:
            doc = DEV.write(body.text, body.title, body.kind, body.site, body.visibility, body.doc_id, body.photo)
        except (ValueError, OSError) as exc:  # not a readable image
            raise HTTPException(400, f"photo not readable: {exc}")
    if DEV.online:
        request_sync("write")
    return doc


@app.get("/memory/{doc_id}/history")
def history(doc_id: str):
    return DEV.history(doc_id)


@app.delete("/memory/{doc_id}")
def delete(doc_id: str):
    with SYNC_LOCK:
        ok = DEV.delete(doc_id)
    if ok and DEV.online:
        request_sync("delete")
    return {"ok": ok}


@app.post("/search")
def search(body: SearchIn):
    if not body.q.strip():
        raise HTTPException(400, "empty query")
    return DEV.search(body.q, body.limit, body.kind, body.mode, site=body.site)


@app.post("/search/photo")
def search_photo(body: PhotoIn):
    """Point the camera at equipment: have we seen this before (here or in the fleet)?"""
    if not embeddings.vision().available():
        raise HTTPException(503, "photo model not available on this node")
    try:
        return DEV.search_photo(body.photo, body.limit)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, f"photo not readable: {exc}")


@app.get("/photos/{doc_id}")
def photo(doc_id: str):
    """Full photo if it was taken on this node; otherwise the fleet thumbnail."""
    path = DEV.photo_path(doc_id)
    if path:
        return FileResponse(path, media_type="image/jpeg")
    doc = DEV.get(doc_id)
    if not doc or not doc.get("thumb"):
        raise HTTPException(404, "no photo")
    import base64
    return Response(base64.b64decode(doc["thumb"].split(",", 1)[1]), media_type="image/jpeg")


def _sources(q):
    found = DEV.search(q, 5, None, "hybrid", diverse=True)  # MMR: varied, not five copies of one fix
    top = found["top_match"]
    floor = max(embeddings.get().miss_threshold * 0.8, top - 0.12)  # only memories nearly as relevant as the best
    sources = [r for r in found["results"] if r["match"] >= floor or r.get("why", {}).get("photo")][:4] \
        or found["results"][:2]
    sources.sort(key=lambda r: -r["match"])
    return found, sources


@app.post("/ask")
def ask(body: SearchIn):
    """Instant answer (ms). If a local LLM is present the client then opens /ask/stream."""
    if not body.q.strip():
        raise HTTPException(400, "empty question")
    found, sources = _sources(body.q)
    out = answer.answer(body.q, sources, confident=not found["miss"])
    out.update(search_ms=found["latency_ms"], miss=found["miss"], top_match=found["top_match"], results=sources,
               online=DEV.online)
    out["llm"] = out["llm"] and not found["miss"]
    return out


@app.post("/ask/stream")
def ask_stream(body: SearchIn):
    """The AI answer, token by token (Server-Sent Events), written only from the node's memories."""
    if not body.q.strip():
        raise HTTPException(400, "empty question")
    found, sources = _sources(body.q)

    def gen():
        t0, text, started = time.perf_counter(), "", False
        try:
            for tok in answer.stream_llm(body.q, sources):
                text += tok
                if not started:
                    head = text.strip()
                    if len(head) <= len(answer.NOT_IN_NOTES) and answer.NOT_IN_NOTES.startswith(head):
                        continue  # hold back until we know it isn't the refusal marker
                    started, tok = True, text
                yield f"data: {json.dumps({'t': tok})}\n\n"
            not_in = answer.NOT_IN_NOTES in text
            if not_in:
                DEV.log("AI: not in memory", f"the local model found no answer to '{body.q}' in this node's notes")
            yield f"data: {json.dumps({'done': True, 'not_in_notes': not_in, 'ms': round((time.perf_counter() - t0) * 1000), 'model': config.OLLAMA_MODEL})}\n\n"
        except Exception as exc:  # model missing / Ollama stopped: the instant answer stands
            yield f"data: {json.dumps({'done': True, 'error': type(exc).__name__})}\n\n"

    return StreamingResponse(gen(), headers=SSE_HEADERS)


@app.post("/ask-fleet")
def ask_fleet(body: SearchIn):
    DEV.record_miss(body.q)
    if DEV.online:
        request_sync("ask fleet")
    return {"ok": True, "queued": not DEV.online}


@app.post("/online")
def set_online(body: Toggle):
    DEV.set_meta("online", body.value)
    if body.value:
        DEV.log("network", "uplink back: syncing")
        return {"online": True, "sync": run_sync("reconnect")}
    SYNC.drop_stream()
    DEV.set_meta("offline_since", time.time())
    DEV.log("network", "uplink lost: working from on-device memory", "warn")
    return {"online": False}


@app.post("/auto-sync")
def auto(body: Toggle):
    DEV.set_meta("auto_sync", body.value)
    if not body.value:
        SYNC.drop_stream()
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
        request_sync("resolve")
    return {"ok": True}


@app.post("/suggestions/{doc_id}/share")
def share(doc_id: str):
    with SYNC_LOCK:
        doc = DEV.share(doc_id)
    if not doc:
        raise HTTPException(404, "not found")
    if DEV.online:
        request_sync("share")
    return doc


@app.post("/contradictions/dismiss")
def dismiss(body: PairIn):
    DEV.dismiss_contradiction(body.doc_id, body.other_id)
    DEV.bump()
    return {"ok": True}


@app.post("/policy/preview")
def preview(body: PolicyIn):
    dense = embeddings.get().embed_dense(body.text)
    d = policy.decide(body.text, body.kind, body.visibility, DEV.best_demand(dense), 0,
                      embeddings.get().demand_threshold)
    if d["decision"] == "sync_redacted":
        d["uploaded_text"] = policy.redact(body.text)
    return d


@app.post("/scale-test")
def scale_test(n: int = 20000):
    return DEV.scale_test(max(1000, min(n, 50000)))


@app.get("/log")
def log(limit: int = 60):
    return DEV.recent_log(min(limit, 300))


@app.get("/metrics")
def metrics():
    return DEV.metrics()
