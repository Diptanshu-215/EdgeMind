"""The hub: launcher + supervisor + live dashboard + field-app host.

Starts every part as its own OS process, exactly as it would run in the field:
  qdrant      Qdrant Server (bundled binary, or yours via QDRANT_URL)   loopback only
  gateway     cloud sync gateway                                          LAN :8100
  nodes       one edge-node process per crew (tablet-A, tablet-B, ...)    loopback, via the hub
and serves on the LAN (:8000):
  /           the operator dashboard (live over SSE)
  /m          the crew field app for phones: pick a crew, then ask / add / review
  /api/...    proxies to every node (local or on another laptop) and to the cloud

Any other laptop can join as a real edge node with `python launch.py join ...`; it shows up
on the dashboard by itself. "Stop cloud" really kills the server processes.

Run:  python launch.py
"""
import asyncio
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, lifeline, qdrant_bin, seed

STATIC = config.ROOT / "static"
LOGS = config.DATA_DIR / "logs"
NODES_FILE = config.DATA_DIR / "nodes.json"
SECRETS = config.hub_secrets()
ADMIN = {"X-Admin-Token": SECRETS["admin_token"]}
TRUST_LOCALHOST = os.getenv("EDGEMIND_TRUST_LOCALHOST", "1") == "1"
# external: Qdrant, the gateway and the nodes run elsewhere (docker compose, a VM, other laptops);
# the hub only serves the dashboard / field app, proxies, and fans out live events.
EXTERNAL = os.getenv("EDGEMIND_HUB_MODE", "") == "external"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")


def say(msg):
    print(f"[edgemind] {msg}", flush=True)


def port_busy(port):
    try:
        with socket.create_connection((config.HOST, port), timeout=0.3):
            return True
    except OSError:
        return False


def free_port(preferred, taken=()):
    p = preferred
    while p in taken or port_busy(p):
        p += 1
    return p


class Proc:
    def __init__(self, name, cmd, env=None, cwd=None, health=None):
        self.name, self.cmd, self.env, self.cwd, self.health = name, cmd, env or {}, cwd, health
        self.p = None
        self.log_file = None

    def start(self):
        if self.alive():
            return
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log_file = open(LOGS / f"{self.name}.log", "ab")
        env = dict(os.environ, **self.env, EDGEMIND_PARENT_PID=str(os.getpid()))
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        self.p = subprocess.Popen(self.cmd, env=env, cwd=self.cwd, stdout=self.log_file, stderr=subprocess.STDOUT, **kw)
        lifeline.adopt(self.p)

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(8)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.p = None
        if self.log_file:
            self.log_file.close()
            self.log_file = None

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def wait_healthy(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            if self.p is not None and self.p.poll() is not None:
                raise RuntimeError(f"{self.name} exited; see {LOGS / (self.name + '.log')}")
            try:
                if httpx.get(self.health, timeout=1.5, trust_env=False).status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.4)
        raise RuntimeError(f"{self.name} did not become healthy; see {LOGS / (self.name + '.log')}")


class Supervisor:
    def __init__(self):
        self.phase = "starting"
        self.error = None
        self.qdrant = None
        self.qdrant_url = config.QDRANT_URL
        self.console_port = config.CONSOLE_PORT
        self.lock = threading.RLock()
        self.py = [sys.executable, "-m", "uvicorn"]
        self.common = {"EDGEMIND_DATA": str(config.DATA_DIR), "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
                       "PYTHONIOENCODING": "utf-8", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                       "EDGEMIND_MODELS": str(config.MODELS_DIR)}
        self.gateway_port = config.GATEWAY_PORT
        self.gateway = None
        self.nodes = {}   # id -> {id, label, sites, port}
        self.procs = {}   # id -> Proc

    # ------------------------------------------------------------------ registry
    def load_nodes(self):
        nodes = config.DEFAULT_NODES
        if NODES_FILE.exists():
            try:
                nodes = json.loads(NODES_FILE.read_text())
            except ValueError:
                pass
        self.nodes = {}
        taken = {self.console_port, self.gateway_port}
        for n in nodes:
            n = dict(n, port=free_port(n["port"], taken))
            taken.add(n["port"])
            self.nodes[n["id"]] = n
        self.save_nodes()

    def save_nodes(self):
        NODES_FILE.parent.mkdir(parents=True, exist_ok=True)
        NODES_FILE.write_text(json.dumps(list(self.nodes.values()), indent=2))

    def node_url(self, dev_id):
        return f"http://{config.HOST}:{self.nodes[dev_id]['port']}"

    def _node_proc(self, n):
        env = dict(self.common, EDGEMIND_DEVICE=n["id"], EDGEMIND_LABEL=n["label"], EDGEMIND_SITES=",".join(n["sites"]),
                   EDGEMIND_PORT=str(n["port"]), EDGEMIND_PUBLIC_URL=f"http://{config.HOST}:{n['port']}",
                   EDGEMIND_JOIN_CODE=SECRETS["join_code"], GATEWAY_URL=f"http://{config.HOST}:{self.gateway_port}",
                   EDGEMIND_EMBEDDER=self.common.get("EDGEMIND_EMBEDDER", config.EMBEDDER))
        return Proc(n["id"], self.py + ["app.device_api:app", "--host", config.HOST, "--port", str(n["port"]),
                                        "--log-level", "warning"], env, cwd=str(config.ROOT),
                    health=f"http://{config.HOST}:{n['port']}/state")

    # ------------------------------------------------------------------ qdrant + gateway
    def _qdrant_running(self, url):
        try:
            return "qdrant" in httpx.get(url, timeout=1.5, trust_env=False).text.lower()
        except httpx.HTTPError:
            return False

    def setup_qdrant(self):
        if os.getenv("EDGEMIND_QDRANT", "bundled") == "embedded":
            self.qdrant_url = ""
            return
        if self.qdrant_url:
            say(f"Using your Qdrant Server at {self.qdrant_url}")
            return
        url = f"http://{config.HOST}:{config.QDRANT_PORT}"
        if self._qdrant_running(url):
            say(f"Found a Qdrant Server already running at {url}; using it")
            self.qdrant_url = url
            return
        binary = qdrant_bin.ensure_binary(say)
        if not binary:
            self.qdrant_url = ""
            return
        store = config.DATA_DIR / "qdrant"
        store.mkdir(parents=True, exist_ok=True)
        env = {
            "QDRANT__STORAGE__STORAGE_PATH": str(store / "storage"),
            "QDRANT__STORAGE__SNAPSHOTS_PATH": str(store / "snapshots"),
            "QDRANT__SERVICE__HOST": config.HOST,  # never exposed on the LAN
            "QDRANT__SERVICE__HTTP_PORT": str(config.QDRANT_PORT),
            "QDRANT__SERVICE__GRPC_PORT": str(config.QDRANT_PORT + 1),
            "QDRANT__TELEMETRY_DISABLED": "true",
            "QDRANT__LOG_LEVEL": "WARN",
        }
        self.qdrant = Proc("qdrant", [str(binary)], env, cwd=str(store), health=f"{url}/readyz")
        self.qdrant_url = url

    def make_gateway(self):
        env = dict(self.common, QDRANT_URL=self.qdrant_url, EDGEMIND_ADMIN_TOKEN=SECRETS["admin_token"],
                   EDGEMIND_JOIN_CODE=SECRETS["join_code"])
        self.gateway = Proc("gateway", self.py + ["app.gateway:app", "--host", config.BIND_HOST, "--port",
                                                  str(self.gateway_port), "--log-level", "warning"],
                            env, cwd=str(config.ROOT), health=f"{self.gateway_base}/health")

    @property
    def gateway_base(self):
        return config.GATEWAY_URL if EXTERNAL else f"http://{config.HOST}:{self.gateway_port}"

    def start_cloud(self):
        if self.qdrant:
            self.qdrant.start()
            self.qdrant.wait_healthy(60)
        self.gateway.start()
        self.gateway.wait_healthy(120)

    def stop_cloud(self):
        self.gateway.stop()
        if self.qdrant:
            self.qdrant.stop()

    # ------------------------------------------------------------------ nodes
    def start_node(self, dev_id):
        p = self.procs.get(dev_id) or self._node_proc(self.nodes[dev_id])
        self.procs[dev_id] = p
        p.start()
        return p

    def start_nodes(self):
        for dev_id in self.nodes:
            self.start_node(dev_id)
        for p in self.procs.values():
            p.wait_healthy(180)

    def seed_node_notes(self, dev_id):
        flag = config.DATA_DIR / "devices" / dev_id / ".seeded"
        notes = seed.DEVICE_NOTES.get(dev_id)
        if not notes or flag.exists():
            return
        for n in notes:
            httpx.post(f"{self.node_url(dev_id)}/memory", json=n, timeout=240, trust_env=False).raise_for_status()
        flag.write_text("ok")

    def add_node(self, label, sites):
        with self.lock:
            used = {n["port"] for n in self.nodes.values()} | {self.console_port, self.gateway_port}
            letter = next(c for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if f"tablet-{c}" not in self.nodes)
            dev_id = f"tablet-{letter}"
            port = free_port(max([n["port"] for n in self.nodes.values()] + [config.FIRST_NODE_PORT - 1]) + 1, used)
            self.nodes[dev_id] = {"id": dev_id, "label": label or f"Line crew {letter}", "sites": sites, "port": port}
            self.save_nodes()
            p = self.start_node(dev_id)
        p.wait_healthy(180)
        return self.nodes[dev_id]

    def remove_node(self, dev_id):
        with self.lock:
            p = self.procs.pop(dev_id, None)
            if p:
                p.stop()
            self.nodes.pop(dev_id, None)
            self.save_nodes()
        try:
            httpx.delete(f"{self.gateway_base}/admin/devices/{dev_id}", headers=ADMIN, timeout=5, trust_env=False)
        except httpx.HTTPError:
            pass
        for _ in range(10):  # Windows keeps memory-mapped shard files locked for a moment
            shutil.rmtree(config.DATA_DIR / "devices" / dev_id, ignore_errors=True)
            if not (config.DATA_DIR / "devices" / dev_id).exists():
                break
            time.sleep(0.5)

    # ------------------------------------------------------------------ lifecycle
    def warm_model(self):
        """Load (and on first run download) the embedding model ONCE, so the processes
        don't race on the download and all end up with the same embedder."""
        self.phase = "loading the AI embedding model (first run downloads ~700 MB: text, multilingual and photo models)"
        code = ("from app import embeddings, config\n"
                "print(embeddings.get().dense.name, flush=True)\n"
                "try:\n"
                "    v = embeddings.vision(); v.embed_text('warm'); v._load('_img', 'ImageEmbedding', config.CLIP_VISION)\n"
                "except Exception as exc:\n"
                "    print('[photos unavailable]', exc)\n"
                "print(embeddings.get().dense.name)")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(config.ROOT), capture_output=True, text=True,
                             env=dict(os.environ, **self.common), timeout=900)
        name = (out.stdout.strip().splitlines() or ["?"])[-1]
        say(f"Embedding model: {name}")
        if "hash" in name and os.getenv("EDGEMIND_EMBEDDER", "auto") != "hash":
            self.common["EDGEMIND_EMBEDDER"] = "hash"  # model unavailable: every process uses the same fallback

    def boot_external(self):
        self.phase = "waiting for the sync gateway"
        end = time.time() + 600
        while True:
            try:
                health = httpx.get(f"{self.gateway_base}/health", timeout=5, trust_env=False).json()
                break
            except Exception:
                if time.time() > end:
                    raise
                time.sleep(2)
        if health.get("needs_seed"):
            self.phase = "loading demo data"
            httpx.post(f"{self.gateway_base}/admin/reset", headers=ADMIN, timeout=300, trust_env=False).raise_for_status()
        self.qdrant_url = health.get("mode", "external")
        self.phase = "ready"

    def boot(self):
        if EXTERNAL:
            try:
                self.boot_external()
                banner(self)
            except Exception as exc:
                self.phase, self.error = "error", str(exc)
                say(f"Startup failed: {exc}")
            return
        try:
            with self.lock:
                self.load_nodes()
                self.warm_model()
                self.phase = "starting Qdrant Server"
                self.setup_qdrant()
                self.phase = "starting sync gateway"
                self.make_gateway()
                fresh = not (config.DATA_DIR / "gateway" / "gateway.sqlite").exists()
                self.start_cloud()
                health = httpx.get(f"{self.gateway_base}/health", timeout=10, trust_env=False).json()
                if fresh or health.get("needs_seed"):
                    httpx.post(f"{self.gateway_base}/admin/reset", headers=ADMIN, timeout=180, trust_env=False).raise_for_status()
                self.phase = "starting edge nodes"
                self.start_nodes()
                for dev_id in self.nodes:
                    self.seed_node_notes(dev_id)
                self.phase = "ready"
            banner(self)
        except Exception as exc:
            self.phase, self.error = "error", str(exc)
            say(f"Startup failed: {exc}")

    def reset(self):
        if EXTERNAL:  # nodes keep running; they see the new cloud epoch and rehydrate by themselves
            self.phase = "resetting"
            try:
                httpx.post(f"{self.gateway_base}/admin/reset", headers=ADMIN, timeout=300, trust_env=False).raise_for_status()
            finally:
                self.phase = "ready"
            return
        with self.lock:
            self.phase = "resetting"
            for p in self.procs.values():
                p.stop()
            self.procs = {}
            self.gateway.stop()
            for d in ("devices", "gateway", "cloud-embedded"):
                shutil.rmtree(config.DATA_DIR / d, ignore_errors=True)
            NODES_FILE.unlink(missing_ok=True)
            self.load_nodes()
            if self.qdrant and not self.qdrant.alive():
                self.qdrant.start()
                self.qdrant.wait_healthy(60)
            self.gateway.start()
            self.gateway.wait_healthy(120)
            httpx.post(f"{self.gateway_base}/admin/reset", headers=ADMIN, timeout=180, trust_env=False).raise_for_status()
            self.phase = "starting edge nodes"
            self.start_nodes()
            for dev_id in self.nodes:
                self.seed_node_notes(dev_id)
            self.phase = "ready"

    def shutdown(self):
        for p in [*self.procs.values(), self.gateway] + ([self.qdrant] if self.qdrant else []):
            if p:
                p.stop()

    def proc_status(self):
        if EXTERNAL:
            return {"gateway": HUB.fleet is not None}
        items = [("qdrant", self.qdrant)] if self.qdrant else []
        items += [("gateway", self.gateway)] + list(self.procs.items())
        return {name: bool(p and p.alive()) for name, p in items}


SUP = Supervisor()


def hub_urls():
    ip = config.lan_ip()
    port = os.getenv("EDGEMIND_PUBLIC_PORT") or SUP.console_port  # the published port when behind Docker
    return {"ip": ip, "console": f"http://{ip}:{port}", "phone": f"http://{ip}:{port}/m",
            "gateway": os.getenv("EDGEMIND_PUBLIC_GATEWAY") or f"http://{ip}:{SUP.gateway_port}"}


def join_command():
    u = hub_urls()
    return f'python launch.py join --hub {u["gateway"]} --code {SECRETS["join_code"]} --name "Line crew X" --sites pune,global'


def banner(sup):
    u = hub_urls()
    lines = [
        "",
        "  EdgeMind hub is live",
        f"  Dashboard (this laptop): http://{config.HOST}:{sup.console_port}",
        f"  Dashboard (any device):  {u['console']}",
        f"  Crew phones:             {u['phone']}",
        f"  Operator PIN:            {SECRETS['operator_pin']}   (needed for admin actions from other devices)",
        f"  Fleet join code:         {SECRETS['join_code']}",
        f"  Join another laptop as an edge node:",
        f"    {join_command()}",
        "  Windows asks once to allow Python on the network: choose Private networks -> Allow.",
        "",
    ]
    print("\n".join(lines), flush=True)
    try:
        import segno
        segno.make(u["phone"], error="m").terminal(compact=True)
    except Exception:
        pass


# ====================================================================== live aggregator
CLIENT: httpx.AsyncClient = None


class Hub:
    """Holds one event stream to the gateway and one per edge node, and fans every event
    out to all dashboards and phones connected to /api/events."""

    def __init__(self):
        self.clients = set()
        self.tasks = {}
        self.last = {}
        self.fleet = None
        self.fleet_at = 0.0

    def publish(self, ev):
        if "data" in ev:
            self.last[ev["src"]] = ev
        else:
            self.last.pop(ev["src"], None)
        for q in list(self.clients):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    async def refresh_fleet(self, force=False):
        if not force and time.time() - self.fleet_at < 0.5:
            return self.fleet
        self.fleet_at = time.time()
        try:
            r = await CLIENT.get(f"{SUP.gateway_base}/fleet", headers=ADMIN, timeout=4)
            self.fleet = r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            self.fleet = None
        return self.fleet

    def remote_nodes(self):
        if not self.fleet:
            return {}
        return {d["id"]: d for d in self.fleet["devices"]
                if d["id"] not in SUP.nodes and d.get("url") and not d.get("revoked")}

    async def follow(self, src, url, headers=None):
        while True:
            try:
                async with CLIENT.stream("GET", url, headers=headers or {},
                                         timeout=httpx.Timeout(None, connect=2)) as r:
                    if r.status_code != 200:
                        raise httpx.HTTPError(f"status {r.status_code}")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = json.loads(line[5:])
                        if src == "cloud":
                            await self.refresh_fleet(force=True)
                            data["fleet"] = self.fleet
                        self.publish({"src": src, "data": data})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            self.publish({"src": src, "down": True})
            await asyncio.sleep(1.0)

    async def supervise(self):
        while True:
            try:
                await self.refresh_fleet()
                wanted = {"cloud": (f"{SUP.gateway_base}/events", ADMIN)}
                if SUP.phase in ("ready", "starting edge nodes"):
                    for dev_id, n in list(SUP.nodes.items()):
                        wanted[dev_id] = (f"http://{config.HOST}:{n['port']}/events", None)
                for dev_id, d in self.remote_nodes().items():
                    if d["online"]:
                        wanted[dev_id] = (f"{d['url'].rstrip('/')}/events", None)
                for src in list(self.tasks):
                    if src not in wanted:
                        self.tasks.pop(src).cancel()
                        self.publish({"src": src, "down": True, "removed": True})
                for src, (url, headers) in wanted.items():
                    if src not in self.tasks or self.tasks[src].done():
                        self.tasks[src] = asyncio.create_task(self.follow(src, url, headers))
            except Exception as exc:  # noqa: BLE001
                print("[hub]", exc, flush=True)
            await asyncio.sleep(1.0)


HUB = Hub()


@asynccontextmanager
async def lifespan(_app):
    global CLIENT
    CLIENT = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(60, connect=3))
    threading.Thread(target=SUP.boot, daemon=True).start()
    task = asyncio.create_task(HUB.supervise())
    yield
    task.cancel()
    for t in HUB.tasks.values():
        t.cancel()
    await CLIENT.aclose()
    SUP.shutdown()


SSE_HEADERS = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform",
               "X-Accel-Buffering": "no"}  # exact type: proxies (Cloudflare) only stream this

app = FastAPI(title="EdgeMind hub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Phones must never run a stale app.js after an update: revalidate (ETag makes it cheap)."""
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ====================================================================== auth (operator actions)
def is_operator(request: Request):
    if TRUST_LOCALHOST and request.client.host in ("127.0.0.1", "::1"):
        return True
    pin = request.headers.get("x-operator-pin") or request.cookies.get("em_pin") or ""
    return bool(pin) and hmac.compare_digest(pin, SECRETS["operator_pin"])


def require_operator(request: Request):
    if not is_operator(request):
        raise HTTPException(403, "operator PIN required")


class PinIn(BaseModel):
    pin: str = Field(max_length=20)


@app.post("/api/login")
def login(body: PinIn, response: Response):
    if not hmac.compare_digest(body.pin.strip(), SECRETS["operator_pin"]):
        raise HTTPException(403, "wrong PIN")
    response.set_cookie("em_pin", body.pin.strip(), httponly=True, samesite="lax", max_age=12 * 3600)
    return {"ok": True}


# ====================================================================== pages
def _field_page(base, dev_id=""):
    html = (STATIC / "field.html").read_text(encoding="utf-8")
    inject = f'<script>window.EM_BASE={json.dumps(base)};window.EM_HUB=true;window.EM_DEV={json.dumps(dev_id)};</script>'
    return HTMLResponse(html.replace("<!--EM_BOOT-->", inject), headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/m")
def phone_home():
    return _field_page("")


@app.get("/field/{dev_id}")
def field(dev_id: str):
    if not ID_RE.match(dev_id):
        raise HTTPException(404)
    return _field_page(f"/api/dev/{dev_id}", dev_id)


@app.get("/api/qr.svg")
def qr(text: str):
    import segno
    if len(text) > 400:
        raise HTTPException(400, "too long")
    import io
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#111", light="#fff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "max-age=3600"})


# ====================================================================== state
def nodes_view():
    fleet = HUB.fleet or {}
    by_id = {d["id"]: d for d in fleet.get("devices", [])}
    out = []
    for dev_id, n in SUP.nodes.items():
        f = by_id.get(dev_id, {})
        out.append({"id": dev_id, "label": n["label"], "sites": n["sites"], "kind": "local", "port": n["port"],
                    "running": bool(SUP.procs.get(dev_id) and SUP.procs[dev_id].alive()),
                    "online": f.get("online", False), "live": f.get("live", False), "addr": "this laptop"})
    for dev_id, d in HUB.remote_nodes().items():
        out.append({"id": dev_id, "label": d["label"], "sites": d["sites"], "kind": "remote", "url": d["url"],
                    "running": d["online"], "online": d["online"], "live": d["live"], "addr": d.get("addr")})
    return out


@app.get("/api/config")
async def cfg(request: Request):
    await HUB.refresh_fleet()
    op = is_operator(request)
    return {
        "phase": SUP.phase, "error": SUP.error, "qdrant": SUP.qdrant_url or "embedded",
        "bundled_qdrant": SUP.qdrant is not None, "procs": SUP.proc_status(), "sites": config.SITES,
        "external": EXTERNAL,
        "nodes": nodes_view(), "hub": hub_urls(), "operator": op,
        "join_code": SECRETS["join_code"] if op else None, "join_command": join_command() if op else None,
    }


# ====================================================================== live events
@app.get("/api/events")
async def events(request: Request):
    q = asyncio.Queue(maxsize=500)
    HUB.clients.add(q)

    async def gen():
        try:
            for ev in list(HUB.last.values()):
                yield f"data: {json.dumps(ev)}\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), 10)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            HUB.clients.discard(q)

    return StreamingResponse(gen(), headers=SSE_HEADERS)


# ====================================================================== proxies
def node_base(dev_id):
    if dev_id in SUP.nodes:
        return SUP.node_url(dev_id)
    d = HUB.remote_nodes().get(dev_id)
    if d:
        return d["url"].rstrip("/")
    raise HTTPException(404, f"unknown node {dev_id}")


@app.api_route("/api/dev/{dev_id}/{path:path}", methods=["GET", "POST", "DELETE"])
async def dev_proxy(dev_id: str, path: str, request: Request):
    url = f"{node_base(dev_id)}/{path}"
    if path in ("events", "ask/stream"):  # live streams pass straight through
        body = await request.body()

        async def gen():
            try:
                async with CLIENT.stream(request.method, url, content=body or None, timeout=httpx.Timeout(None, connect=3),
                                         headers={"content-type": "application/json"}) as r:
                    async for chunk in r.aiter_raw():
                        yield chunk
                        if await request.is_disconnected():
                            break
            except httpx.HTTPError:
                yield b"event: down\ndata: {}\n\n"

        return StreamingResponse(gen(), headers=SSE_HEADERS)
    try:
        r = await CLIENT.request(request.method, url, params=request.query_params, content=await request.body(),
                                 headers={"content-type": request.headers.get("content-type", "application/json")},
                                 timeout=httpx.Timeout(120, connect=3))
    except httpx.HTTPError:
        raise HTTPException(502, f"{dev_id} is not reachable")
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))


@app.api_route("/api/cloud/{path:path}", methods=["GET", "POST"])
async def cloud_proxy(path: str, request: Request):
    if path not in ("memory", "log", "fleet", "search", "health"):
        raise HTTPException(404)
    try:
        r = await CLIENT.request(request.method, f"{SUP.gateway_base}/{path}", params=request.query_params,
                                 content=await request.body(), headers=dict(ADMIN, **{"content-type": "application/json"}),
                                 timeout=httpx.Timeout(20, connect=2))
    except httpx.HTTPError:
        raise HTTPException(503, "cloud is down")
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))


# ====================================================================== operator actions
def _busy():
    if SUP.phase != "ready":
        raise HTTPException(409, f"busy: {SUP.phase}")


@app.post("/api/cloud-stop")
async def cloud_stop(request: Request):
    require_operator(request)
    if EXTERNAL:
        raise HTTPException(409, "the cloud runs in Docker: docker compose stop gateway qdrant")
    _busy()
    await asyncio.to_thread(SUP.stop_cloud)
    return {"procs": SUP.proc_status()}


@app.post("/api/cloud-start")
async def cloud_start(request: Request):
    require_operator(request)
    if EXTERNAL:
        raise HTTPException(409, "the cloud runs in Docker: docker compose start qdrant gateway")
    _busy()
    await asyncio.to_thread(SUP.start_cloud)
    return {"procs": SUP.proc_status()}


@app.post("/api/reset")
async def reset(request: Request):
    require_operator(request)
    if SUP.phase not in ("ready", "error"):
        raise HTTPException(409, f"busy: {SUP.phase}")
    await asyncio.to_thread(SUP.reset)
    return {"ok": True}


class NodeIn(BaseModel):
    label: str = Field(default="", max_length=60)
    sites: list[str] = Field(min_length=1)


@app.post("/api/nodes")
async def add_node(body: NodeIn, request: Request):
    require_operator(request)
    if EXTERNAL:
        raise HTTPException(409, "nodes run in Docker or on other laptops: see the Join dialog")
    _busy()
    sites = [s for s in body.sites if s in config.SITES]
    if not sites:
        raise HTTPException(400, f"sites must be some of {list(config.SITES)}")
    if len(SUP.nodes) >= 8:
        raise HTTPException(400, "at most 8 nodes on one hub laptop; join more from other laptops")
    return await asyncio.to_thread(SUP.add_node, body.label.strip(), sites)


@app.delete("/api/nodes/{dev_id}")
async def remove_node(dev_id: str, request: Request):
    require_operator(request)
    if dev_id in SUP.nodes:
        await asyncio.to_thread(SUP.remove_node, dev_id)
    else:
        r = await CLIENT.delete(f"{SUP.gateway_base}/admin/devices/{dev_id}", headers=ADMIN)
        if r.status_code != 200:
            raise HTTPException(r.status_code, "no such node")
    await HUB.refresh_fleet(force=True)
    return {"ok": True}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    SUP.console_port = config.CONSOLE_PORT if EXTERNAL or not port_busy(config.CONSOLE_PORT) else free_port(8080)
    if SUP.console_port != config.CONSOLE_PORT:
        say(f"Port {config.CONSOLE_PORT} is in use by another program; the dashboard uses {SUP.console_port}")
    SUP.gateway_port = config.GATEWAY_PORT if EXTERNAL else free_port(config.GATEWAY_PORT, {SUP.console_port})
    open_browser = "--no-browser" not in sys.argv
    if open_browser:
        threading.Timer(2.5, lambda: webbrowser.open(f"http://{config.HOST}:{SUP.console_port}")).start()
    say(f"Dashboard: http://{config.HOST}:{SUP.console_port}  (Ctrl+C to stop everything)")
    try:
        uvicorn.run(app, host=config.BIND_HOST, port=SUP.console_port, log_level="warning",
                    timeout_graceful_shutdown=2)
    finally:
        SUP.shutdown()
