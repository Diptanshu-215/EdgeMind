"""End-to-end test of the whole demo story against REAL processes.

Starts the launcher (Qdrant Server + gateway + two device processes) in a temp folder,
then drives it over HTTP exactly like the dashboard does.

    python tests/test_e2e.py                # bundled Qdrant Server (downloads once)
    EDGEMIND_QDRANT=embedded python tests/test_e2e.py   # no server: point sync
Set QDRANT_BIN=/path/to/qdrant to reuse a binary instead of downloading.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
A, B, CONSOLE, GW = "http://127.0.0.1:8001", "http://127.0.0.1:8002", "http://127.0.0.1:8000", "http://127.0.0.1:8100"
H = httpx.Client(timeout=60)


def wait(pred, what, timeout=60, every=0.5):
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


def run_story():
    cfg = H.get(f"{CONSOLE}/api/config").json()
    snapshots = cfg["qdrant"] != "embedded"
    print("cloud:", cfg["qdrant"], "| snapshots:", snapshots)
    for dev in (A, B):
        H.post(f"{dev}/auto-sync", json={"value": False})
    wait(lambda: len(titles(A)) >= 11 and len(titles(B)) >= 9, "devices to bootstrap from the cloud")

    # 1. subscriptions + policy
    a, b = titles(A), titles(B)
    assert "Feeder P-12 tripping in rain" in a and "Feeder P-12 tripping in rain" not in b
    assert "Bird fault on 11 kV line" in b and "Bird fault on 11 kV line" not in a
    assert a["Shift handover"]["sync_state"] == "local_only"
    assert a["T-417 LT bushing flashover"]["sync_state"] == "redacted_synced"
    assert "sales@" in a["T-417 LT bushing flashover"]["text"]  # full text stays on the device
    cloud = {d["title"]: d for d in H.get(f"{GW}/memory").json()}
    assert "[EMAIL]" in cloud["T-417 LT bushing flashover"]["text"] and "[PHONE]" in cloud["T-417 LT bushing flashover"]["text"]
    assert "Store room access" not in cloud and "Shift handover" not in cloud
    st = H.get(f"{A}/state").json()
    if snapshots:
        assert st["stats"]["mirrors"]["pune"] >= 4 and st["snapshots"] is True
    print("1 ok: subscriptions, redaction, private notes")

    # 2. offline write + search, then reconnect
    online(A, False)
    doc = write(A, title="Chiller-type DT cooling fan seized", kind="fix", site="global",
                text="Radiator cooling fan on the 315 kVA DT seized. Freed the bearing, greased it, fan runs again.")
    assert doc["sync_state"] == "pending"
    hits = H.post(f"{A}/search", json={"q": "transformer radiator fan seized bearing"}).json()
    assert hits["results"][0]["doc_id"] == doc["doc_id"], hits["results"][0]["title"]
    assert "Chiller-type DT cooling fan seized" not in {d["title"] for d in H.get(f"{GW}/memory").json()}
    r = online(A, True)
    assert r["sync"]["pushed"] >= 1, r
    r = sync(B)
    assert "Chiller-type DT cooling fan seized" in titles(B)
    if snapshots:
        assert r["partial"], r  # B refreshed its global mirror with a PARTIAL snapshot
    print("2 ok: offline write/search, push on reconnect, partial snapshot pull")
    sync(A)  # A settles: its local copy is purged once the mirror holds it
    a = titles(A)
    assert a["Chiller-type DT cooling fan seized"]["layer"] == "mirror", a["Chiller-type DT cooling fan seized"]

    # 3. conflict: both edit the same fleet memory offline
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
    print("3 ok: conflict detected, merged, propagated")

    # 4. contradiction (conflicting facts) detected offline
    online(B, False)
    c = write(B, title="Bushing re-torqued on T-88", kind="fix", site="nagpur",
              text="Tightened the HV bushing terminals on T-88 to 50 Nm as per the vendor sheet.")
    assert c["contradicts"] and c["contradicts"][0]["theirs"] == "40 Nm", c["contradicts"]
    assert H.get(f"{B}/review").json()["contradictions"]
    online(B, True)
    print("4 ok: contradiction 50 Nm vs 40 Nm flagged")

    # 5. fleet demand: B can't find it, A's private note is suggested for sharing
    H.post(f"{B}/ask-fleet", json={"q": "transformer drain valve leaking and no spare valve"})
    sync(B)
    r = sync(A)
    sug = H.get(f"{A}/review").json()["suggestions"]
    assert any(s["title"] == "Drain valve leak trick" for s in sug), (r, sug)
    note = next(s for s in sug if s["title"] == "Drain valve leak trick")
    H.post(f"{A}/suggestions/{note['doc_id']}/share").raise_for_status()
    sync(A), sync(B)
    assert "Drain valve leak trick" in titles(B)
    print("5 ok: fleet demand -> suggestion -> shared -> received")

    # 6. ask (offline answer with citations)
    online(A, False)
    ans = H.post(f"{A}/ask", json={"q": "what must I do before working on a line?"}).json()
    assert ans["answer"] and "[1]" in ans["answer"] and ans["sources"], ans
    online(A, True)
    print("6 ok: offline answer:", ans["engine"], "|", ans["answer"][:90], "...")

    # 7. real cloud outage: kill the server processes, keep working, recover
    H.post(f"{CONSOLE}/api/cloud/stop").raise_for_status()
    inc = write(A, title="Live wire on ground near market", kind="incident", site="pune",
                text="Conductor snapped and lying live near the vegetable market. Area cordoned, feeder isolated.")
    r = H.post(f"{A}/sync").json()
    assert not r["ok"] and "unreachable" in r["error"], r
    assert len(H.get(f"{A}/outbox").json()) == 1
    H.post(f"{CONSOLE}/api/cloud/start").raise_for_status()
    r = sync(A)
    assert r["pushed"] >= 1 and H.get(f"{A}/outbox").json() == []
    print("7 ok: real outage -> queued -> drained after restart")

    # 8. delete propagates
    H.delete(f"{A}/memory/{inc['doc_id']}").raise_for_status()
    sync(A)
    assert inc["doc_id"] not in {d["doc_id"] for d in H.get(f"{GW}/memory").json()}
    assert "Live wire on ground near market" not in titles(A)
    print("8 ok: delete propagates")

    m = H.get(f"{A}/metrics").json()
    print("metrics A:", {k: m[k] for k in ("searches", "search_p50_ms", "search_p95_ms", "bytes_partial_total", "bytes_full_total")})
    print("ALL CHECKS PASSED")


def main():
    data = Path(tempfile.mkdtemp(prefix="edgemind-e2e-"))
    if os.getenv("QDRANT_BIN"):
        (data / "bin").mkdir()
        shutil.copy(os.environ["QDRANT_BIN"], data / "bin" / Path(os.environ["QDRANT_BIN"]).name)
    env = dict(os.environ, EDGEMIND_DATA=str(data), AUTO_SYNC_SECONDS="3600")
    proc = subprocess.Popen([sys.executable, "launch.py", "--no-browser"], cwd=ROOT, env=env)
    try:
        wait(lambda: H.get(f"{CONSOLE}/api/config").json()["phase"] == "ready", "stack to start", timeout=300, every=1)
        run_story()
    finally:
        proc.terminate()
        proc.wait(30)
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
