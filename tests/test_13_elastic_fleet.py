"""Elastic fleet: an operator adds an edge node from the hub; it enrolls, bootstraps from a snapshot, goes live and
exchanges memories; removing it stops the process, revokes its token and deletes its shards.
"""
import json
import sqlite3

import pytest

from helpers import uid, wait_for


@pytest.fixture(scope="module")
def crew_c(hub):
    n = hub.add_node("Line crew C · Pune (test)", ["pune", "global"])
    yield n, hub.node(n["id"])
    if n["id"] in {x["id"] for x in hub.config()["nodes"]}:
        hub.remove_node(n["id"])


def test_a_new_node_enrolls_bootstraps_and_goes_live(hub, crew_c):
    n, C = crew_c
    assert n["sites"] == ["pune", "global"]
    wait_for(lambda: len(C.memory()) >= 8 and C.state()["live"], f"{n['id']} to bootstrap and go live", 180, 1)
    titles = C.titles()
    assert "Feeder P-12 tripping in rain" in titles and "Bird fault on 11 kV line" not in titles, \
        "the new node must hold exactly its circles (pune + global)"
    node = next(x for x in hub.config()["nodes"] if x["id"] == n["id"])
    assert node["running"] and node["kind"] == "local", node


def test_a_new_node_exchanges_memories_live_with_the_fleet(hub, crew_c, flow):
    _, C = crew_c
    u = uid()
    with flow.step("crew C writes a Pune fix; crew A (same circle) receives it"):
        c_doc = C.write(f"Replaced the rusted cross-arm {u} on the Karve Road pole.", f"Cross-arm {u}", "fix", site="pune")
        hub.A.wait_doc(c_doc["doc_id"], "receives crew C's fix")
    with flow.step("crew A writes a Pune fix; crew C receives it"):
        a_doc = hub.A.write(f"Fitted a new earth lead {u} at the Kothrud DT.", f"Earth lead {u}", "fix", site="pune")
        C.wait_doc(a_doc["doc_id"], "receives crew A's fix")
    with flow.step("crew B (Nagpur) never gets crew C's Pune fix"):
        assert hub.B.doc(c_doc["doc_id"]) is None


def test_removing_a_node_stops_it_and_revokes_its_token(hub, crew_c, flow):
    n, _ = crew_c
    folder = hub.data_dir / "devices" / n["id"]
    with flow.step("read the node's token before removal"):
        db = sqlite3.connect(folder / "state.sqlite")
        try:
            token = json.loads(db.execute("SELECT value FROM meta WHERE key='token'").fetchone()[0])
        finally:
            db.close()
    with flow.step("the operator removes the node"):
        hub.remove_node(n["id"])
        assert n["id"] not in {x["id"] for x in hub.config()["nodes"]}
    with flow.step("its token no longer works and the gateway lists it as revoked"):
        assert hub.gw("GET", "/sites/pune/changes", headers={"X-Device-Token": token}).status_code == 401
        assert any(d["id"] == n["id"] and d["revoked"] for d in hub.cloud_fleet()["devices"])
    with flow.step("its shards are deleted from disk"):
        wait_for(lambda: not folder.exists(), f"{folder} to be deleted", 15)
