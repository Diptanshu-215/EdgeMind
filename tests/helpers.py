"""HTTP helpers for the system tests: talk to the hub, an edge node and the gateway the way the
dashboard and the phones do, and fail with a message that says exactly which call went wrong."""
import json
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

import httpx

HTTP = httpx.Client(timeout=httpx.Timeout(120, connect=5), trust_env=False)
TOUCHED = set()  # nodes used by the running test: their activity is attached to a failure report


def short(value, n=700):
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= n else text[:n] + f" ... [{len(text) - n} more chars]"


def check(r, expect=(200,)):
    """Raise a readable AssertionError when an API call returns an unexpected status."""
    if r.status_code not in expect:
        raise AssertionError(f"API call failed: {r.request.method} {r.request.url} -> HTTP {r.status_code} "
                             f"(expected {'/'.join(map(str, expect))})\n    response body: {short(r.text)}")
    return r


def wait_for(pred, what, timeout=30, every=0.25):
    """Poll until pred() returns something truthy. On timeout, say what never happened and the last value seen."""
    end = time.time() + timeout
    last = None
    while True:
        try:
            last = pred()
            if last:
                return last
        except AssertionError as exc:  # an API call failed: keep polling, report it if it never recovers
            last = exc
        except httpx.HTTPError as exc:
            last = exc
        if time.time() > end:
            raise AssertionError(f"Timed out after {timeout}s waiting for: {what}\n    last value seen: {short(last)}")
        time.sleep(every)


def uid():
    return uuid.uuid4().hex[:6]


def free_port(pair=False, avoid=()):
    """A free loopback port (pair=True: port and port+1 both free, as Qdrant needs HTTP + gRPC)."""
    for _ in range(200):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port in avoid or (pair and port + 1 in avoid):
            continue
        if pair:
            try:
                with socket.socket() as s2:
                    s2.bind(("127.0.0.1", port + 1))
            except OSError:
                continue
        return port
    raise RuntimeError("no free port")


class Node:
    """One edge node, reached through the hub's proxy (like a phone) or directly (a remote laptop)."""

    def __init__(self, base, name):
        self.base, self.name = base.rstrip("/"), name

    def __repr__(self):
        return f"Node({self.name})"

    # ------------------------------------------------------------------ raw
    def _call(self, method, path, expect=(200,), **kw):
        TOUCHED.add(self)
        for attempt in range(3):
            r = HTTP.request(method, f"{self.base}/{path}", **kw)
            # The hub answers 502 when a node is momentarily unreachable (e.g. busy applying a partial snapshot).
            # Retry so that one known stall doesn't fail unrelated tests; test_04 checks responsiveness itself.
            if r.status_code == 502 and "not reachable" in r.text and 502 not in expect and attempt < 2:
                time.sleep(1.5)
                continue
            return check(r, expect)

    def get(self, path, expect=(200,), **kw):
        return self._call("GET", path, expect, **kw)

    def post(self, path, expect=(200,), **kw):
        return self._call("POST", path, expect, **kw)

    def delete_(self, path, expect=(200,), **kw):
        return self._call("DELETE", path, expect, **kw)

    # ------------------------------------------------------------------ state
    def state(self):
        return self.get("state").json()

    def memory(self):
        return self.get("memory").json()

    def titles(self):
        return {d["title"]: d for d in self.memory()}

    def doc(self, doc_id):
        """This node's current view of one memory (None if it can't see it)."""
        return next((d for d in self.memory() if d["doc_id"] == doc_id), None)

    def outbox(self):
        return self.get("outbox").json()

    def review(self):
        return self.get("review").json()

    def log(self, limit=80):
        return self.get(f"log?limit={limit}").json()

    def log_since(self, ts, events=None):
        return [e for e in self.log(200) if e["ts"] >= ts and (events is None or e["event"] in events)]

    def metrics(self):
        return self.get("metrics").json()

    def history(self, doc_id):
        return self.get(f"memory/{doc_id}/history").json()

    # ------------------------------------------------------------------ actions
    def write(self, text, title="", kind="note", **kw):
        return self.post("memory", json=dict(text=text, title=title, kind=kind, **kw)).json()

    def edit(self, doc, text=None, **kw):
        body = {"doc_id": doc["doc_id"], "title": doc["title"], "kind": doc["kind"],
                "text": doc["text"] if text is None else text}
        body.update(kw)
        return self.post("memory", json=body).json()

    def delete(self, doc_id):
        return self.delete_(f"memory/{doc_id}").json()

    def search(self, q, **kw):
        return self.post("search", json=dict(q=q, **kw)).json()

    def search_ids(self, q, **kw):
        return [r["doc_id"] for r in self.search(q, **kw)["results"]]

    def ask(self, q):
        return self.post("ask", json={"q": q}).json()

    def ask_stream(self, q):
        """The streamed local-LLM answer: returns (text, final event)."""
        TOUCHED.add(self)
        text, done = "", {}
        with HTTP.stream("POST", f"{self.base}/ask/stream", json={"q": q}) as r:
            check(r)
            for line in r.iter_lines():
                if line.startswith("data:"):
                    ev = json.loads(line[5:])
                    text += ev.get("t", "")
                    if ev.get("done"):
                        done = ev
        return text, done

    def ask_fleet(self, q):
        return self.post("ask-fleet", json={"q": q}).json()

    def sync(self):
        """One manual sync cycle. Returns the node's sync report (check r['ok'] yourself)."""
        return self.post("sync").json()

    def set_online(self, value):
        return self.post("online", json={"value": value}).json()

    @contextmanager
    def offline(self):
        """Cut this node's uplink for the duration of the block; it always comes back."""
        self.set_online(False)
        try:
            yield self
        finally:
            self.set_online(True)

    def share(self, doc_id):
        return self.post(f"suggestions/{doc_id}/share").json()

    def resolve(self, doc_id, strategy, expect=(200,)):
        return self.post(f"conflicts/{doc_id}/resolve", json={"strategy": strategy}, expect=expect)

    # ------------------------------------------------------------------ waits
    def wait_doc(self, doc_id, what, pred=lambda d: True, timeout=20):
        """Wait until this node can see doc_id and pred(doc) holds."""
        def ok():
            d = self.doc(doc_id)
            return d if d is not None and pred(d) else None
        return wait_for(ok, f"{self.name}: {what}", timeout, every=0.2)

    def wait_gone(self, doc_id, what, timeout=20):
        return wait_for(lambda: self.doc(doc_id) is None, f"{self.name}: {what}", timeout, every=0.2)

    def wait_synced(self, doc_id, timeout=20):
        """Wait until this node's write is confirmed by the cloud (outbox drained for it)."""
        return wait_for(lambda: doc_id not in {o["doc_id"] for o in self.outbox()}
                        and (self.doc(doc_id) or {}).get("sync_state") in ("synced", "redacted_synced"),
                        f"{self.name}: '{doc_id}' pushed and confirmed by the cloud", timeout, every=0.2)


class Hub:
    """The hub (console) of the test stack: dashboard, proxies, operator actions."""

    def __init__(self, base, pin, data_dir, gateway, admin_token, snapshots):
        self.base, self.pin, self.data_dir = base, pin, data_dir
        self.gateway, self.admin_token, self.snapshots = gateway, admin_token, snapshots
        self.A = self.node("tablet-A")
        self.B = self.node("tablet-B")

    @property
    def op(self):
        return {"X-Operator-Pin": self.pin}

    def node(self, dev_id):
        return Node(f"{self.base}/api/dev/{dev_id}", dev_id)

    def get(self, path, expect=(200,), **kw):
        return check(HTTP.get(f"{self.base}/{path}", **kw), expect)

    def post(self, path, expect=(200,), **kw):
        return check(HTTP.post(f"{self.base}/{path}", **kw), expect)

    def config(self, operator=True):
        return self.get("api/config", headers=self.op if operator else {}).json()

    # ------------------------------------------------------------------ cloud (through the hub)
    def cloud_memory(self):
        return self.get("api/cloud/memory").json()

    def cloud_doc(self, doc_id):
        return next((d for d in self.cloud_memory() if d["doc_id"] == doc_id), None)

    def cloud_fleet(self):
        return self.get("api/cloud/fleet").json()

    def wait_cloud(self, doc_id, what, pred=lambda d: True, timeout=20):
        def ok():
            d = self.cloud_doc(doc_id)
            return d if d is not None and pred(d) else None
        return wait_for(ok, f"cloud: {what}", timeout, every=0.25)

    def cloud_stop(self):
        return self.post("api/cloud-stop", headers=self.op).json()

    def cloud_start(self):
        return self.post("api/cloud-start", headers=self.op, timeout=300).json()

    def ensure_cloud_running(self):
        procs = self.config()["procs"]
        if not procs.get("gateway") or procs.get("qdrant") is False:
            self.cloud_start()

    def add_node(self, label, sites):
        return self.post("api/nodes", headers=self.op, json={"label": label, "sites": sites}, timeout=400).json()

    def remove_node(self, dev_id):
        return check(HTTP.delete(f"{self.base}/api/nodes/{dev_id}", headers=self.op, timeout=120)).json()

    def reset(self):
        return self.post("api/reset", headers=self.op, timeout=900).json()

    # ------------------------------------------------------------------ gateway (direct, like a remote laptop)
    def gw(self, method, path, **kw):
        return HTTP.request(method, f"{self.gateway}/{path.lstrip('/')}", **kw)

    @property
    def admin(self):
        return {"X-Admin-Token": self.admin_token}

    def node_meta(self, dev_id, key):
        """Read a hub-local node's internal bookkeeping (state.sqlite meta table) straight from disk."""
        db = sqlite3.connect(self.data_dir / "devices" / dev_id / "state.sqlite")
        try:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            db.close()

    def log_tail(self, name, lines=25):
        path = self.data_dir / "logs" / f"{name}.log"
        if name == "hub":
            path = self.data_dir / "hub.log"
        if not path.exists():
            return f"(no {path.name})"
        text = path.read_bytes().decode("utf-8", errors="replace").splitlines()
        return "\n".join(text[-lines:]) or "(empty)"


class EventReader:
    """Read a Server-Sent-Events stream in the background, so a test can act and then check what arrived."""

    def __init__(self, url, headers=None):
        self.events, self.error = [], None
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, args=(url, headers or {}), daemon=True)
        self._t.start()

    def _run(self, url, headers):
        try:
            with httpx.Client(timeout=httpx.Timeout(30, connect=5), trust_env=False) as c:
                with c.stream("GET", url, headers=headers) as r:
                    for line in r.iter_lines():
                        if self._stop.is_set():
                            return
                        if line.startswith("data:"):
                            self.events.append(json.loads(line[5:]))
        except Exception as exc:  # noqa: BLE001
            self.error = exc

    def wait(self, pred, what, timeout=15):
        def found():
            hit = next((e for e in list(self.events) if pred(e)), None)
            if hit is None and self.error is not None:
                raise AssertionError(f"event stream closed: {self.error!r}")
            return hit
        return wait_for(found, what, timeout, every=0.1)

    def close(self):
        self._stop.set()
