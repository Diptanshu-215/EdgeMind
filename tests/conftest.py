"""Shared fixtures for the EdgeMind test suite.

Two kinds of tests:
  component  in-process: the policy, the fact checker, one EdgeDevice with real Qdrant Edge shards.
             Fast (seconds), no network, no servers.
  system     marked `system`: a REAL, isolated stack (Qdrant Server + sync gateway + two edge-node
             processes + hub) is started once per test session in a temp folder on free ports, and
             tests drive it over HTTP exactly like the dashboard and crew phones do. Your own
             data/ folder and a hub you may have running are never touched.

When a test fails, the report also shows the flow steps that passed/failed, each node's sync
status and recent activity, and the tail of the process logs (see pytest_runtest_makereport).

Environment knobs:
  EDGEMIND_TEST_KEEP=1          keep the stack's temp folder (logs, shards) even when all tests pass
  EDGEMIND_QDRANT=embedded      run the stack without Qdrant Server (point sync; snapshot tests skip)
  EDGEMIND_TEST_BOOT_TIMEOUT    seconds to wait for the stack to start (default 900)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Component tests import app.* in this process: point it at a throwaway data folder BEFORE any import.
UNIT_DATA = Path(tempfile.mkdtemp(prefix="edgemind-unit-"))
os.environ["EDGEMIND_DATA"] = str(UNIT_DATA)
os.environ.setdefault("EDGEMIND_MODELS", str(ROOT / "data" / "models"))

import helpers  # noqa: E402

collect_ignore = ["test_e2e.py"]  # the original single-script story test: `python tests/test_e2e.py`

TEST_PIN = "135790"
_STACK = {"dir": None, "hub": None}


# ====================================================================== markers
def pytest_collection_modifyitems(items):
    for item in items:
        if "hub" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.system)


# ====================================================================== flow steps
class Flow:
    """`with flow.step("B receives A's write"):` - on failure the report lists which steps passed."""

    def __init__(self):
        self.steps = []

    @contextmanager
    def step(self, name):
        entry = {"name": name, "status": "running", "t": time.time()}
        self.steps.append(entry)
        try:
            yield
        except BaseException:
            entry["status"] = "FAILED"
            raise
        else:
            entry["status"] = "ok"
        finally:
            entry["s"] = time.time() - entry["t"]

    def render(self):
        mark = {"ok": "[ ok ]", "FAILED": "[FAIL]", "running": "[ .. ]"}
        return "\n".join(f"  {mark[s['status']]} {i}. {s['name']}  ({s.get('s', 0):.1f}s)"
                         for i, s in enumerate(self.steps, 1))


@pytest.fixture
def flow():
    return Flow()


@pytest.fixture(autouse=True)
def _reset_touched():
    helpers.TOUCHED.clear()
    yield


# ====================================================================== component fixtures
@pytest.fixture
def make_device():
    """Factory for in-process edge devices with real Qdrant Edge shards in a temp folder."""
    from app.device import EdgeDevice
    made = []

    def make(dev_id=None, sites=("pune", "global"), label="Unit test node"):
        dev = EdgeDevice(dev_id or f"unit-{helpers.uid()}", label, list(sites))
        made.append(dev)
        return dev

    yield make
    for dev in made:
        try:
            dev.close()
        except Exception:  # noqa: BLE001 (already closed by the test)
            pass


@pytest.fixture
def device(make_device):
    return make_device()


# ====================================================================== the real stack
def _say(request, msg):
    tr = request.config.pluginmanager.get_plugin("terminalreporter")
    if tr:
        tr.write_line(f"[edgemind-test] {msg}")


@pytest.fixture(scope="session")
def hub(request):
    """Start an isolated EdgeMind hub (Qdrant Server + gateway + tablet-A + tablet-B) and yield a Hub client."""
    data = Path(tempfile.mkdtemp(prefix="edgemind-system-"))
    _STACK["dir"] = data
    if (ROOT / "data" / "bin").exists():  # reuse the downloaded Qdrant Server binary
        shutil.copytree(ROOT / "data" / "bin", data / "bin")
    console = helpers.free_port()
    gateway = helpers.free_port(avoid={console})
    qdrant = helpers.free_port(pair=True, avoid={console, gateway})
    env = dict(os.environ, EDGEMIND_DATA=str(data), CONSOLE_PORT=str(console), GATEWAY_PORT=str(gateway),
               QDRANT_PORT=str(qdrant), EDGEMIND_BIND="127.0.0.1", EDGEMIND_PIN=TEST_PIN,
               EDGEMIND_TRUST_LOCALHOST="0",  # so the operator-PIN checks are really exercised
               AUTO_SYNC_SECONDS="5", EDGEMIND_RECONCILE_SECONDS="5", PYTHONUTF8="1",
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    for k in ("EDGEMIND_HUB_MODE", "QDRANT_URL", "GATEWAY_URL", "EDGEMIND_DEVICE", "EDGEMIND_SITES",
              "EDGEMIND_PORT", "EDGEMIND_PUBLIC_URL", "EDGEMIND_JOIN_CODE", "EDGEMIND_ADMIN_TOKEN"):
        env.pop(k, None)
    base = f"http://127.0.0.1:{console}"
    log = open(data / "hub.log", "wb")
    _say(request, f"starting an isolated stack in {data} (hub :{console}, gateway :{gateway}, qdrant :{qdrant}); "
                  f"first start takes 1-3 minutes...")
    proc = subprocess.Popen([sys.executable, "launch.py", "--no-browser"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    t0 = time.time()

    def tail(name, n=40):
        path = data / ("hub.log" if name == "hub" else f"logs/{name}.log")
        return "\n".join(path.read_bytes().decode("utf-8", "replace").splitlines()[-n:]) if path.exists() else "(none)"

    try:
        timeout = float(os.getenv("EDGEMIND_TEST_BOOT_TIMEOUT", "900"))
        phase = None
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"the hub process exited with code {proc.returncode} during startup.\n"
                                   f"--- hub.log ---\n{tail('hub')}")
            try:
                cfg = httpx.get(f"{base}/api/config", timeout=3, trust_env=False,
                                headers={"X-Operator-Pin": TEST_PIN}).json()
                if cfg["phase"] != phase:
                    phase = cfg["phase"]
                    _say(request, f"  hub phase: {phase}  ({time.time() - t0:.0f}s)")
                if cfg["phase"] == "ready":
                    break
                if cfg["phase"] == "error":
                    raise RuntimeError(f"the hub failed to start: {cfg['error']}\n--- hub.log ---\n{tail('hub')}\n"
                                       f"--- gateway.log ---\n{tail('gateway')}\n--- tablet-A.log ---\n{tail('tablet-A')}")
            except httpx.HTTPError:
                pass
            if time.time() - t0 > timeout:
                raise RuntimeError(f"the hub did not become ready in {timeout:.0f}s (last phase: {phase})\n"
                                   f"--- hub.log ---\n{tail('hub')}")
            time.sleep(1)

        secrets = json.loads((data / "secrets.json").read_text())
        gw = f"http://127.0.0.1:{cfg['hub']['gateway'].rsplit(':', 1)[1]}"
        h = helpers.Hub(base, TEST_PIN, data, gw, secrets["admin_token"], cfg["qdrant"] != "embedded")
        h.join_code = secrets["join_code"]
        _say(request, "  waiting for tablet-A and tablet-B to enroll, bootstrap their mirrors and go live...")
        helpers.wait_for(lambda: len(h.A.memory()) >= 11 and len(h.B.memory()) >= 9,
                         "tablet-A (>= 11 memories) and tablet-B (>= 9) to bootstrap from the cloud", timeout=240, every=1)
        helpers.wait_for(lambda: h.A.state()["live"] and h.B.state()["live"], "both nodes' live channels", timeout=120)
        _say(request, f"  stack ready in {time.time() - t0:.0f}s (cloud: {cfg['qdrant']})")
        _STACK["hub"] = h
        yield h
    finally:
        _STACK["hub"] = None
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        time.sleep(2)  # the hub's children die with it (Job Object + lifeline)
        keep = os.getenv("EDGEMIND_TEST_KEEP") == "1" or request.session.testsfailed
        if keep:
            _say(request, f"stack data kept for inspection: {data}  (logs in {data / 'logs'}, hub output in {data / 'hub.log'})")
        else:
            for _ in range(10):
                shutil.rmtree(data, ignore_errors=True)
                if not data.exists():
                    break
                time.sleep(0.5)


def pytest_sessionfinish(session):
    shutil.rmtree(UNIT_DATA, ignore_errors=True)


# ====================================================================== failure details
_QUICK = httpx.Client(timeout=5, trust_env=False)


def _node_report(node):
    try:
        st = _QUICK.get(f"{node.base}/state").json()
        last = st.get("last_sync") or {}
        lines = [f"online={st['online']} connected={st['connected']} live={st['live']} enrolled={st['enrolled']} "
                 f"outbox={st['stats']['outbox']} conflicts={st['stats']['conflicts']} mirrors={st['stats']['mirrors']}",
                 f"last sync: reason={last.get('reason')} ok={last.get('ok')} error={last.get('error')} "
                 f"pushed={last.get('pushed')} pulled={last.get('pulled')}",
                 "recent activity (newest first):"]
        for e in _QUICK.get(f"{node.base}/log?limit=15").json():
            lines.append(f"  {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['level']:5} "
                         f"{e['event']}: {helpers.short(e['detail'], 160)}")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return f"(could not read the node's state: {type(exc).__name__}: {exc})"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if not rep.failed or rep.when not in ("setup", "call"):
        return
    flow = getattr(item, "funcargs", {}).get("flow")
    if flow and flow.steps:
        rep.sections.append(("EdgeMind flow (where it stopped)", flow.render()))
    h = _STACK["hub"]
    if h is None or "hub" not in getattr(item, "fixturenames", ()):
        return
    for node in sorted(helpers.TOUCHED, key=lambda n: n.name):
        rep.sections.append((f"node {node.name}: sync status + activity", _node_report(node)))
    names = ["gateway"] + [n.name for n in sorted(helpers.TOUCHED, key=lambda n: n.name)
                           if (h.data_dir / "logs" / f"{n.name}.log").exists()]
    for name in names:
        rep.sections.append((f"process log tail: {name}.log", h.log_tail(name, 15)))
    rep.sections.append(("stack folder (kept after a failure)", str(h.data_dir)))
