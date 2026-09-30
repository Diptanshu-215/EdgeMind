"""User interface: the control-room dashboard and the crew field app, and the APIs + live streams they are built on
(device memory, search results, sync status, system activity).

PS goal: "Provide a user-facing interface to inspect device memory, search results, synchronization status, and
system activity."
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import EventReader, uid

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


# ---------------------------------------------------------------- component
@pytest.mark.parametrize("name", sorted(p.name for p in STATIC.glob("*.js")))
def test_ui_javascript_has_no_syntax_errors(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js not installed: cannot syntax-check the UI scripts")
    r = subprocess.run([node, "--check", str(STATIC / name)], capture_output=True, text=True)
    assert r.returncode == 0, f"static/{name} does not parse:\n{r.stderr}"


def test_every_local_asset_referenced_by_the_pages_exists():
    for page in ("index.html", "field.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
        assert refs, f"{page} references no local assets"
        for ref in refs:
            assert (ROOT / ref.lstrip("/")).is_file(), f"{page} references {ref}, which does not exist"


# ---------------------------------------------------------------- system
def test_the_dashboard_and_its_assets_are_served(hub):
    html = hub.get("").text
    assert "<title>EdgeMind" in html and "/static/app.js" in html
    for ref in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        r = hub.get(ref.lstrip("/"))
        assert r.content, f"{ref} is empty"


def test_the_field_app_is_served_for_phones_and_for_each_crew(hub):
    assert 'window.EM_BASE=""' in hub.get("m").text, "the phone landing page did not get its boot script"
    crew = hub.get("field/tablet-A").text
    assert 'window.EM_BASE="/api/dev/tablet-A"' in crew and "/static/field.js" in crew
    hub.get("field/bad$id", expect=(404,))
    assert "<title>EdgeMind" in hub.A.get("").text, "a node must serve the field app to its own crew"


def test_the_hub_reports_the_whole_fleet(hub):
    cfg = hub.config()
    assert cfg["phase"] == "ready" and cfg["error"] is None
    assert all(cfg["procs"].values()), f"a process is down: {cfg['procs']}"
    nodes = {n["id"]: n for n in cfg["nodes"]}
    for dev in ("tablet-A", "tablet-B"):
        assert nodes[dev]["running"] and nodes[dev]["online"], nodes.get(dev)
    assert set(cfg["sites"]) == {"pune", "nagpur", "global"}


def test_node_state_shows_memory_and_sync_status(hub):
    st = hub.A.state()
    for key in ("id", "sites", "online", "connected", "live", "enrolled", "last_sync", "stats", "search", "embedder",
                "mirror_seq", "snapshots", "demand"):
        assert key in st, f"/state is missing {key!r}"
    for key in ("memories", "own", "by_state", "mirrors", "outbox", "conflicts", "contradictions", "kinds"):
        assert key in st["stats"], f"/state stats is missing {key!r}"
    assert st["stats"]["memories"] >= 11 and st["stats"]["kinds"], st["stats"]
    assert {"conflicts", "contradictions", "suggestions"} <= set(hub.A.review())
    assert isinstance(hub.A.outbox(), list)


def test_activity_is_logged_on_the_node_and_the_gateway(hub):
    u = uid()
    doc = hub.A.write(f"Replaced the service fuse {u} at the milk dairy.", f"Service fuse {u}", "fix", site="global")
    assert any(e["event"] == "memory saved" and u in e["detail"] for e in hub.A.log(20)), "node activity not logged"
    hub.wait_cloud(doc["doc_id"], "fix uploaded")
    gw_log = hub.get("api/cloud/log?limit=50").json()
    assert any(e["event"] == "push" and u in e["detail"] for e in gw_log), "gateway activity not logged"


def test_the_cloud_view_through_the_hub(hub):
    titles = {d["title"] for d in hub.cloud_memory()}
    assert "Permit-to-work before any line work" in titles
    fleet = hub.cloud_fleet()
    assert {"tablet-A", "tablet-B"} <= {d["id"] for d in fleet["devices"]} and fleet["count"] >= 12
    hits = hub.post("api/cloud/search", json={"q": "permit to work"}).json()
    assert hits and hits[0]["title"] == "Permit-to-work before any line work", [h["title"] for h in hits]
    hub.get("api/cloud/admin", expect=(404,))


def test_the_hub_live_stream_pushes_node_and_cloud_events(hub, flow):
    reader = EventReader(f"{hub.base}/api/events")
    try:
        with flow.step("on connect the stream replays the latest state of every source"):
            reader.wait(lambda e: e.get("src") == "cloud" and "data" in e, "a cloud event", 15)
            reader.wait(lambda e: e.get("src") == "tablet-A" and "data" in e, "a tablet-A event", 15)
        with flow.step("a write on tablet-A is pushed to the dashboard within seconds"):
            u = uid()
            hub.A.write(f"Cleared a kite string {u} from the LT line.", f"Kite string {u}", "fix", site="global")
            reader.wait(lambda e: e.get("src") == "tablet-A" and any(u in r.get("detail", "")
                                                                     for r in e.get("data", {}).get("log", [])),
                        "the write to appear in tablet-A's live activity", 15)
    finally:
        reader.close()


def test_a_node_live_stream_carries_its_state(hub):
    reader = EventReader(f"{hub.A.base}/events")
    try:
        ev = reader.wait(lambda e: "state" in e, "the first state event from tablet-A", 15)
        assert ev["state"]["id"] == "tablet-A" and ev["first"] is True
    finally:
        reader.close()


def test_the_join_qr_code_is_generated(hub):
    r = hub.get("api/qr.svg?text=http://192.168.1.20:8000/m")
    assert r.headers["content-type"].startswith("image/svg+xml") and b"<svg" in r.content
    hub.get("api/qr.svg?text=" + "x" * 401, expect=(400,))
