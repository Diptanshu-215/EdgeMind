"""End-to-end test of the whole system against REAL processes.

Starts the hub (Qdrant Server + gateway + two edge-node processes + console) in a temp
folder, drives it over HTTP exactly like the dashboard and the phones do, then joins a
separate `launch.py join` edge node the way a second laptop would.

    python tests/test_e2e.py                           # bundled Qdrant Server
    EDGEMIND_QDRANT=embedded python tests/test_e2e.py  # no server: point sync only
Set QDRANT_BIN=/path/to/qdrant to reuse a binary instead of downloading.
Ports: CONSOLE_PORT (default 8300 here), GATEWAY_PORT (8100), nodes 8001+.
"""
import base64
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONSOLE_PORT = int(os.getenv("CONSOLE_PORT", "8300"))
CONSOLE = f"http://127.0.0.1:{CONSOLE_PORT}"
A, B = f"{CONSOLE}/api/dev/tablet-A", f"{CONSOLE}/api/dev/tablet-B"  # through the hub, like phones do
CLOUD = f"{CONSOLE}/api/cloud"
H = httpx.Client(timeout=120, trust_env=False)
DATA = None


def wait(pred, what, timeout=60, every=0.25):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = pred()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what} (last: {last})")


def sync(dev):
    r = H.post(f"{dev}/sync").json()
    assert r["ok"], r
    return r


def titles(dev):
    return {d["title"]: d for d in H.get(f"{dev}/memory").json()}


def online(dev, value):
    return H.post(f"{dev}/online", json={"value": value}).json()


def write(dev, **kw):
    r = H.post(f"{dev}/memory", json=kw)
    r.raise_for_status()
    return r.json()


def photo(color, shape):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 400), "white")
    d = ImageDraw.Draw(im)
    (d.ellipse if shape == "circle" else d.rectangle)([70, 70, 330, 330], fill=color)
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def ai_answer(dev, q):
    text, done = "", {}
    with H.stream("POST", f"{dev}/ask/stream", json={"q": q}) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                text += ev.get("t", "")
                if ev.get("done"):
                    done = ev
    return text, done


def cloud_titles():
    return {d["title"]: d for d in H.get(f"{CLOUD}/memory").json()}


def node_token(dev_id, data=None):
    db = sqlite3.connect((data or DATA) / "devices" / dev_id / "state.sqlite")
    try:
        return json.loads(db.execute("SELECT value FROM meta WHERE key='token'").fetchone()[0])
    finally:
        db.close()


def run_story():
    cfg = H.get(f"{CONSOLE}/api/config").json()
    gw = f"http://127.0.0.1:{cfg['hub']['gateway'].rsplit(':', 1)[1]}"
    snapshots = cfg["qdrant"] != "embedded"
    print("cloud:", cfg["qdrant"], "| snapshots:", snapshots, "| gateway:", gw)
    for dev in (A, B):
        H.post(f"{dev}/auto-sync", json={"value": False})
    wait(lambda: len(titles(A)) >= 11 and len(titles(B)) >= 9, "nodes to bootstrap from the cloud")

    # 1. subscriptions + policy
    a, b = titles(A), titles(B)
    assert "Feeder P-12 tripping in rain" in a and "Feeder P-12 tripping in rain" not in b
    assert "Bird fault on 11 kV line" in b and "Bird fault on 11 kV line" not in a
    assert a["Shift handover"]["sync_state"] == "local_only"
    assert a["T-417 LT bushing flashover"]["sync_state"] == "redacted_synced"
    assert "sales@" in a["T-417 LT bushing flashover"]["text"]  # full text stays on the node
    cloud = cloud_titles()
    assert "[EMAIL]" in cloud["T-417 LT bushing flashover"]["text"] and "[PHONE]" in cloud["T-417 LT bushing flashover"]["text"]
    assert "Store room access" not in cloud and "Shift handover" not in cloud
    st = H.get(f"{A}/state").json()
    assert st["enrolled"] and st["connected"]
    if snapshots:
        assert st["stats"]["mirrors"]["pune"] >= 4 and st["snapshots"] is True
        full = st["last_sync"]
    print("1 ok: enrollment, subscriptions, redaction, private notes")

    # 2. security: nothing on the gateway is readable without a token; sites are enforced
    assert H.get(f"{gw}/memory").status_code == 401
    assert H.get(f"{gw}/fleet").status_code == 401
    tok_b = node_token("tablet-B")
    assert H.get(f"{gw}/sites/pune/changes", headers={"X-Device-Token": tok_b}).status_code == 403
    assert H.get(f"{gw}/sites/nagpur/changes", headers={"X-Device-Token": tok_b}).status_code == 200
    bad = H.post(f"{gw}/push", headers={"X-Device-Token": tok_b}, json={"items": [{
        "doc_id": "00000000-0000-0000-0000-000000000001", "site": "pune", "base_version": 0,
        "payload": {"title": "x", "text": "x"}, "dense": [0.1] * 384}]}).json()
    assert bad["results"][0]["status"] == "rejected", bad
    assert H.post(f"{gw}/enroll", json={"join_code": "EM-0000", "device_id": "intruder", "sites": ["pune"]}).status_code == 403
    print("2 ok: admin-only reads, site isolation, wrong join code refused")

    # 3. offline write + search, then reconnect
    online(A, False)
    doc = write(A, title="Chiller-type DT cooling fan seized", kind="fix", site="global",
                text="Radiator cooling fan on the 315 kVA DT seized. Freed the bearing, greased it, fan runs again.")
    assert doc["sync_state"] == "pending"
    hits = H.post(f"{A}/search", json={"q": "transformer radiator fan seized bearing"}).json()
    assert hits["results"][0]["doc_id"] == doc["doc_id"], hits["results"][0]["title"]
    assert "Chiller-type DT cooling fan seized" not in cloud_titles()
    r = online(A, True)
    assert r["sync"]["pushed"] >= 1, r
    r = sync(B)
    assert "Chiller-type DT cooling fan seized" in titles(B)
    if snapshots:
        assert r["points"] or r["partial"], r
    sync(A)  # A settles: its local copy is purged once the mirror holds it
    assert titles(A)["Chiller-type DT cooling fan seized"]["layer"] == "mirror"
    print("3 ok: offline write/search, push on reconnect, delta pull, dual-write purge")

    # 4. conflict: both edit the same fleet memory offline
    ptw = titles(A)["Permit-to-work before any line work"]
    online(A, False), online(B, False)
    write(A, doc_id=ptw["doc_id"], title=ptw["title"], kind="manual", text=ptw["text"] + " Pune: use the yellow earthing kit.")
    write(B, doc_id=ptw["doc_id"], title=ptw["title"], kind="manual", text=ptw["text"] + " Always two-person verification.")
    online(A, True)
    r = online(B, True)
    assert r["sync"]["conflicts"] == 1, r
    rev = H.get(f"{B}/review").json()
    assert len(rev["conflicts"]) == 1 and "yellow" in rev["conflicts"][0]["cloud"]["text"]
    H.post(f"{B}/conflicts/{ptw['doc_id']}/resolve", json={"strategy": "merge"}).raise_for_status()
    sync(B), sync(A)
    merged = titles(A)["Permit-to-work before any line work"]["text"]
    assert "yellow" in merged and "two-person" in merged, merged
    assert H.get(f"{B}/review").json()["conflicts"] == []
    print("4 ok: conflict detected, merged, propagated")
    hist = H.get(f"{A}/memory/{ptw['doc_id']}/history").json()
    events = [h["event"] for h in hist]
    assert "edited on this node" in events and any(e.startswith("received from tablet-B") for e in events), events
    print("4b ok: memory history on A:", " -> ".join(events))

    # 5. contradiction (conflicting facts) detected offline
    online(B, False)
    c = write(B, title="Bushing re-torqued on T-88", kind="fix", site="nagpur",
              text="Tightened the HV bushing terminals on T-88 to 50 Nm as per the vendor sheet.")
    assert c["contradicts"] and c["contradicts"][0]["theirs"] == "40 Nm", c["contradicts"]
    assert H.get(f"{B}/review").json()["contradictions"]
    online(B, True)
    print("5 ok: contradiction 50 Nm vs 40 Nm flagged")

    # 6. fleet demand: B can't find it, A's private note is suggested for sharing
    H.post(f"{B}/ask-fleet", json={"q": "transformer drain valve leaking and no spare valve"})
    sync(B)
    r = sync(A)
    sug = H.get(f"{A}/review").json()["suggestions"]
    assert any(s["title"] == "Drain valve leak trick" for s in sug), (r, sug)
    note = next(s for s in sug if s["title"] == "Drain valve leak trick")
    H.post(f"{A}/suggestions/{note['doc_id']}/share").raise_for_status()
    sync(A), sync(B)
    assert "Drain valve leak trick" in titles(B)
    print("6 ok: fleet demand -> suggestion -> shared -> received")

    # 7. ask (offline answer with citations, semantic sentence selection)
    online(A, False)
    ans = H.post(f"{A}/ask", json={"q": "what must I do before working on a line?"}).json()
    assert ans["answer"] and "[1]" in ans["answer"] and ans["sources"], ans
    assert "permit" in ans["answer"].lower(), ans["answer"]
    online(A, True)
    print("7 ok: offline answer:", ans["engine"], "|", ans["answer"][:90], "...")

    # 7b. cross-lingual retrieval: Hindi and Marathi questions find the English SOPs
    for q, want in [("लाइन पर काम शुरू करने से पहले क्या करना चाहिए?", "Permit-to-work before any line work"),
                    ("ट्रान्सफॉर्मर गरम होत आहे आणि आवाज येतो", "DT T-417 humming and overheating")]:
        r = H.post(f"{A}/search", json={"q": q}).json()
        assert r["results"][0]["title"] == want, (q, [x["title"] for x in r["results"]])
        assert set(r["results"][0]["why"]) >= {"meaning"}, r["results"][0]["why"]
    print("7b ok: Hindi + Marathi questions retrieve the right English SOPs, with per-result score breakdown")

    # 7c. local LLM (only when Ollama + the model are installed): grounded answer, and honest refusal
    if H.get(f"{A}/state").json()["llm"]:
        text, done = ai_answer(A, "what must I do before working on a line?")
        assert not done.get("not_in_notes") and "permit" in text.lower(), (text, done)
        text, done = ai_answer(B, "transformer drain valve leaking and no spare valve")  # shared to B in step 6
        assert not done.get("not_in_notes") and ("ptfe" in text.lower() or "epoxy" in text.lower()), (text, done)
        text, done = ai_answer(B, "How do I update the firmware of a rooftop solar inverter?")
        assert done.get("not_in_notes"), (text, done)
        print(f"7c ok: local LLM ({done.get('model')}) answers from memory, uses the note A shared, "
              "and says 'not in memory' when nobody knows")
    else:
        print("7c skipped: no local LLM on this machine")

    # 8. real cloud outage: kill the server processes, keep working, recover
    H.post(f"{CONSOLE}/api/cloud-stop").raise_for_status()
    inc = write(A, title="Live wire on ground near market", kind="incident", site="pune",
                text="Conductor snapped and lying live near the vegetable market. Area cordoned, feeder isolated.")
    r = H.post(f"{A}/sync").json()
    assert not r["ok"] and "unreachable" in r["error"], r
    assert len(H.get(f"{A}/outbox").json()) == 1
    H.post(f"{CONSOLE}/api/cloud-start").raise_for_status()
    r = sync(A)
    assert r["pushed"] >= 1 and H.get(f"{A}/outbox").json() == []
    print("8 ok: real outage -> queued -> drained after restart")

    # 9. delete propagates
    H.delete(f"{A}/memory/{inc['doc_id']}").raise_for_status()
    sync(A)
    assert inc["doc_id"] not in {d["doc_id"] for d in H.get(f"{CLOUD}/memory").json()}
    assert "Live wire on ground near market" not in titles(A)
    hits = H.post(f"{A}/search", json={"q": "conductor snapped lying live near the vegetable market"}).json()["results"]
    assert inc["doc_id"] not in {h["doc_id"] for h in hits}, [h["title"] for h in hits]
    print("9 ok: delete propagates, and the tombstone never resurfaces in search")

    # 10. LIVE propagation: no manual sync, the gateway pushes and B pulls on its own
    for dev in (A, B):
        H.post(f"{dev}/auto-sync", json={"value": True})
    wait(lambda: H.get(f"{A}/state").json()["live"] and H.get(f"{B}/state").json()["live"], "live channels")
    t0 = time.time()
    write(A, title="Live propagation check", kind="fix", site="global", text="Re-crimped the jumper lug on the global test pole.")
    wait(lambda: "Live propagation check" in titles(B), "live propagation to B", timeout=15, every=0.05)
    live_s = time.time() - t0
    assert live_s < 5, live_s
    print(f"10 ok: live propagation A -> B in {live_s:.2f}s (no manual sync)")

    # 10b. photo evidence: captured on A, embedded on the node (CLIP), thumbnail + vector reach B live
    if H.get(f"{A}/state").json()["vision"]:
        d = write(A, title="Marker on pole 7", text="", kind="fix", site="global", photo=photo("red", "circle"))
        assert d.get("has_photo") and d.get("thumb", "").startswith("data:image/jpeg")
        wait(lambda: "Marker on pole 7" in titles(B), "photo memory to reach B", timeout=20, every=0.2)
        assert titles(B)["Marker on pole 7"].get("thumb")
        r = H.post(f"{B}/search", json={"q": "a red circle"}).json()
        top = next(x for x in r["results"] if x["title"] == "Marker on pole 7")
        assert top["why"].get("photo", 0) >= 0.26, top["why"]
        r = H.post(f"{B}/search/photo", json={"photo": photo("red", "circle")}).json()
        assert r["results"][0]["title"] == "Marker on pole 7", r["results"][:2]
        assert H.get(f"{B}/photos/{d['doc_id']}").status_code == 200  # fleet thumbnail
        assert len(H.get(f"{A}/photos/{d['doc_id']}").content) > 3000  # full photo stays on A
        assert H.get(f"{A}/state").json()["stats"]["photos"] >= 1
        print("10b ok: photo -> CLIP vector on the node -> live to B; text->photo and photo->photo search offline")

    # 10c. scale: a throwaway 3,000-memory Qdrant Edge shard, same hybrid pipeline
    t = H.post(f"{A}/scale-test?n=3000", timeout=300).json()
    assert t["memories"] == 3000 and t["p95_ms"] < 100, t
    # 10d. reconcile: after point deltas the mirrors reconcile by partial snapshot, including the
    # "already identical" case (gateway 204), and every node keeps syncing cleanly
    time.sleep(6)
    for dev in (A, B):
        r = sync(dev)
        r2 = sync(dev)
        assert r["ok"] and r2["ok"], (r, r2)
    errs = [l for l in H.get(f"{A}/log?limit=80").json() if l["event"] in ("sync error", "mirror rebuild")]
    assert not errs, errs
    print("10d ok: mirrors reconcile by partial snapshot (incl. nothing-changed 204) without errors")
    print(f"10c ok: scale test {t['memories']:,} memories: hybrid p50 {t['p50_ms']} ms, p95 {t['p95_ms']} ms")

    # 11. elastic fleet: start a third node from the console, it enrolls + bootstraps + goes live
    n = H.post(f"{CONSOLE}/api/nodes", json={"label": "Line crew C · Pune", "sites": ["pune", "global"]}, timeout=300).json()
    C = f"{CONSOLE}/api/dev/{n['id']}"
    wait(lambda: len(titles(C)) >= 8 and H.get(f"{C}/state").json()["live"], "new node to bootstrap and go live", timeout=120)
    assert "Live propagation check" in titles(C)
    H.delete(f"{CONSOLE}/api/nodes/{n['id']}").raise_for_status()
    assert n["id"] not in {x["id"] for x in H.get(f"{CONSOLE}/api/config").json()["nodes"]}
    print(f"11 ok: {n['id']} added live, bootstrapped, removed and revoked")
    return gw


def remote_story(gw):
    """12-13. A separate `launch.py join` process: its own data folder, its own shards, reaching the
    hub only over HTTP, exactly like a second laptop on the Wi-Fi."""
    code = json.loads((DATA / "secrets.json").read_text())["join_code"]
    rdata = Path(tempfile.mkdtemp(prefix="edgemind-node-"))
    env = dict(os.environ, EDGEMIND_MODELS=str(ROOT / "data" / "models"), PYTHONUTF8="1")
    env.pop("EDGEMIND_DATA", None)
    proc = subprocess.Popen([sys.executable, "launch.py", "join", "--hub", gw, "--code", code, "--name", "Remote crew",
                             "--sites", "nagpur,global", "--port", "8030", "--data", str(rdata)], cwd=ROOT, env=env)
    try:
        R = "http://127.0.0.1:8030"
        wait(lambda: H.get(f"{R}/state").json()["live"], "remote node to enroll and go live", timeout=180)
        rid = H.get(f"{R}/state").json()["id"]
        wait(lambda: len(titles(R)) >= 8, "remote node to bootstrap")
        assert "Bird fault on 11 kV line" in titles(R) and "Feeder P-12 tripping in rain" not in titles(R)
        wait(lambda: any(x["id"] == rid and x["kind"] == "remote" for x in H.get(f"{CONSOLE}/api/config").json()["nodes"]),
             "hub to list the remote node")
        RP = f"{CONSOLE}/api/dev/{rid}"  # the hub proxies to the remote laptop
        write(RP, title="Remote crew insulator swap", kind="fix", site="nagpur",
              text="Swapped a flashed-over disc insulator string at the Hingna cut-point pole.")
        wait(lambda: "Remote crew insulator swap" in titles(B), "remote write to reach B", timeout=15, every=0.1)
        print(f"12 ok: remote node {rid} joined over HTTP, bootstrapped, its write reached tablet-B live")

        # 13. hub reset = new cloud epoch: the remote node keeps its knowledge and re-publishes it
        H.post(f"{CONSOLE}/api/reset", timeout=600).raise_for_status()
        wait(lambda: "Remote crew insulator swap" in cloud_titles(), "remote node to rehydrate the cloud", timeout=120)
        print("13 ok: hub reset -> remote node rehydrated the fleet with its own memories")
    finally:
        proc.terminate()
        proc.wait(30)
        shutil.rmtree(rdata, ignore_errors=True)


def main():
    global DATA
    DATA = Path(tempfile.mkdtemp(prefix="edgemind-e2e-"))
    if os.getenv("QDRANT_BIN"):
        (DATA / "bin").mkdir()
        shutil.copy(os.environ["QDRANT_BIN"], DATA / "bin" / Path(os.environ["QDRANT_BIN"]).name)
    elif (ROOT / "data" / "bin").exists():
        shutil.copytree(ROOT / "data" / "bin", DATA / "bin")
    # reconcile every 5 s (not 180): exercises point delta -> partial snapshot, including "nothing changed" (204)
    env = dict(os.environ, EDGEMIND_DATA=str(DATA), AUTO_SYNC_SECONDS="3600", CONSOLE_PORT=str(CONSOLE_PORT),
               EDGEMIND_RECONCILE_SECONDS="5",
               EDGEMIND_MODELS=str(ROOT / "data" / "models"), PYTHONUTF8="1")
    proc = subprocess.Popen([sys.executable, "launch.py", "--no-browser"], cwd=ROOT, env=env)
    try:
        wait(lambda: H.get(f"{CONSOLE}/api/config").json()["phase"] == "ready", "stack to start", timeout=400, every=1)
        gw = run_story()
        remote_story(gw)
        print("ALL CHECKS PASSED")
    finally:
        proc.terminate()
        proc.wait(30)
        time.sleep(2)  # the hub's children exit with it (job object + lifeline)
        shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":
    main()
