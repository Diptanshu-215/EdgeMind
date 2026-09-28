"""Launcher + dashboard ("console").

Starts every part of the demo as its own OS process, exactly as it would run in the field:
  qdrant      Qdrant Server (bundled binary, or yours via QDRANT_URL)
  gateway     cloud sync gateway           :8100
  tablet-A    device process + its shards  :8001
  tablet-B    device process + its shards  :8002
and serves the dashboard on :8000. "Stop cloud" really kills the server processes.

Run:  python launch.py
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, qdrant_bin, seed

STATIC = config.ROOT / "static"
LOGS = config.DATA_DIR / "logs"
ADMIN = {"X-Admin-Token": "edgemind-admin"}


def say(msg):
    print(f"[edgemind] {msg}", flush=True)


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
        env = dict(os.environ, **self.env)
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        self.p = subprocess.Popen(self.cmd, env=env, cwd=self.cwd, stdout=self.log_file, stderr=subprocess.STDOUT, **kw)

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
        self.lock = threading.Lock()
        py = [sys.executable, "-m", "uvicorn"]
        common = {"EDGEMIND_DATA": str(config.DATA_DIR), "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
                  "PYTHONIOENCODING": "utf-8", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        self.common = common
        self.gateway = Proc("gateway", py + ["app.gateway:app", "--host", config.HOST, "--port", str(config.GATEWAY_PORT),
                                             "--log-level", "warning"], common, cwd=str(config.ROOT),
                            health=f"{config.GATEWAY_URL}/health")
        self.devices = {
            dev_id: Proc(dev_id, py + ["app.device_api:app", "--host", config.HOST, "--port", str(cfg["port"]),
                                       "--log-level", "warning"], dict(common, EDGEMIND_DEVICE=dev_id),
                         cwd=str(config.ROOT), health=f"{config.device_url(dev_id)}/state")
            for dev_id, cfg in config.DEVICES.items()
        }

    # ------------------------------------------------------------------ qdrant
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
            "QDRANT__SERVICE__HTTP_PORT": str(config.QDRANT_PORT),
            "QDRANT__SERVICE__GRPC_PORT": str(config.QDRANT_PORT + 1),
            "QDRANT__TELEMETRY_DISABLED": "true",
            "QDRANT__LOG_LEVEL": "WARN",
        }
        self.qdrant = Proc("qdrant", [str(binary)], env, cwd=str(store), health=f"{url}/readyz")
        self.qdrant_url = url

    def start_cloud(self):
        if self.qdrant:
            self.qdrant.start()
            self.qdrant.wait_healthy(60)
        self.gateway.env["QDRANT_URL"] = self.qdrant_url
        self.gateway.start()
        self.gateway.wait_healthy(120)

    def stop_cloud(self):
        self.gateway.stop()
        if self.qdrant:
            self.qdrant.stop()

    # ------------------------------------------------------------------ devices
    def start_devices(self):
        for p in self.devices.values():
            p.start()
        for p in self.devices.values():
            p.wait_healthy(120)

    def seed_device_notes(self):
        for dev_id, notes in seed.DEVICE_NOTES.items():
            flag = config.DATA_DIR / "devices" / dev_id / ".seeded"
            if flag.exists():
                continue
            for n in notes:
                httpx.post(f"{config.device_url(dev_id)}/memory", json=n, timeout=60, trust_env=False).raise_for_status()
            flag.write_text("ok")

    def warm_model(self):
        """Load (and on first run download) the embedding model ONCE, so the three processes
        don't race on the download and all end up with the same embedder."""
        self.phase = "loading the AI embedding model (first run downloads ~70 MB)"
        code = "from app import embeddings; print(embeddings.get().dense.name)"
        out = subprocess.run([sys.executable, "-c", code], cwd=str(config.ROOT), capture_output=True, text=True,
                             env=dict(os.environ, **self.common), timeout=900)
        name = (out.stdout.strip().splitlines() or ["?"])[-1]
        say(f"Embedding model: {name}")
        if "hash" in name and os.getenv("EDGEMIND_EMBEDDER", "auto") != "hash":
            # model could not be loaded: make every process use the same fallback
            self.common["EDGEMIND_EMBEDDER"] = "hash"
            for p in [self.gateway, *self.devices.values()]:
                p.env["EDGEMIND_EMBEDDER"] = "hash"

    def boot(self):
        try:
            with self.lock:
                self.warm_model()
                self.phase = "starting Qdrant Server"
                self.setup_qdrant()
                self.phase = "starting sync gateway"
                fresh = not (config.DATA_DIR / "gateway" / "gateway.sqlite").exists()
                self.start_cloud()
                if fresh:
                    httpx.post(f"{config.GATEWAY_URL}/admin/reset", headers=ADMIN, timeout=180, trust_env=False).raise_for_status()
                self.phase = "starting devices"
                self.start_devices()
                self.seed_device_notes()
                self.phase = "ready"
            say(f"Ready: open http://{config.HOST}:{config.CONSOLE_PORT}")
        except Exception as exc:
            self.phase, self.error = "error", str(exc)
            say(f"Startup failed: {exc}")

    def reset(self):
        with self.lock:
            self.phase = "resetting"
            for p in self.devices.values():
                p.stop()
            self.gateway.stop()
            for d in ("devices", "gateway", "cloud-embedded"):
                shutil.rmtree(config.DATA_DIR / d, ignore_errors=True)
            if self.qdrant and not self.qdrant.alive():
                self.qdrant.start()
                self.qdrant.wait_healthy(60)
            self.gateway.env["QDRANT_URL"] = self.qdrant_url
            self.gateway.start()
            self.gateway.wait_healthy(120)
            httpx.post(f"{config.GATEWAY_URL}/admin/reset", headers=ADMIN, timeout=180, trust_env=False).raise_for_status()
            self.start_devices()
            self.seed_device_notes()
            self.phase = "ready"

    def shutdown(self):
        for p in [*self.devices.values(), self.gateway] + ([self.qdrant] if self.qdrant else []):
            p.stop()

    def procs(self):
        items = [("qdrant", self.qdrant)] if self.qdrant else []
        items += [("gateway", self.gateway)] + list(self.devices.items())
        return {name: bool(p and p.alive()) for name, p in items}


SUP = Supervisor()


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=SUP.boot, daemon=True).start()
    yield
    SUP.shutdown()


app = FastAPI(title="EdgeMind console", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def cfg():
    return {
        "phase": SUP.phase, "error": SUP.error, "gateway": config.GATEWAY_URL,
        "qdrant": SUP.qdrant_url or "embedded", "bundled_qdrant": SUP.qdrant is not None,
        "devices": [{"id": k, "label": v["label"], "url": config.device_url(k), "sites": v["sites"]}
                    for k, v in config.DEVICES.items()],
        "sites": config.SITES, "procs": SUP.procs(),
    }


@app.post("/api/cloud/stop")
def cloud_stop():
    if SUP.phase != "ready":
        raise HTTPException(409, f"busy: {SUP.phase}")
    SUP.stop_cloud()
    return {"procs": SUP.procs()}


@app.post("/api/cloud/start")
def cloud_start():
    if SUP.phase != "ready":
        raise HTTPException(409, f"busy: {SUP.phase}")
    SUP.start_cloud()
    return {"procs": SUP.procs()}


@app.post("/api/reset")
def reset():
    if SUP.phase not in ("ready", "error"):
        raise HTTPException(409, f"busy: {SUP.phase}")
    SUP.reset()
    return {"ok": True}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    open_browser = "--no-browser" not in sys.argv
    if open_browser:
        threading.Timer(2.5, lambda: webbrowser.open(f"http://{config.HOST}:{config.CONSOLE_PORT}")).start()
    say(f"Dashboard: http://{config.HOST}:{config.CONSOLE_PORT}  (Ctrl+C to stop everything)")
    try:
        uvicorn.run(app, host=config.HOST, port=config.CONSOLE_PORT, log_level="warning")
    finally:
        SUP.shutdown()
