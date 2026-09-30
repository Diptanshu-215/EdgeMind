"""Offline-first: nodes keep working with no uplink or a dead cloud, queue writes by priority, and drain on reconnect.

PS outcome: "Support intermittent connectivity and continue operating offline."
"""
import pytest

from helpers import uid, wait_for


@pytest.fixture
def cloud_down(hub):
    """Really stop Qdrant Server and the gateway; always bring them back and wait for the nodes to reconnect."""
    hub.cloud_stop()
    try:
        yield
    finally:
        hub.ensure_cloud_running()
        wait_for(lambda: all(n.state()["connected"] and n.state()["live"] for n in (hub.A, hub.B)),
                 "both nodes to reconnect to the restarted cloud", timeout=90, every=1)


def test_with_the_uplink_off_a_node_keeps_working_and_catches_up_on_reconnect(hub, flow):
    A, B = hub.A, hub.B
    u = uid()
    with A.offline():
        with flow.step("tablet-A loses its uplink"):
            st = A.state()
            assert st["online"] is False and st["offline_since"], st
        with flow.step("a write is saved on the node and queued"):
            doc = A.write(f"Radiator cooling fan {u} on the 315 kVA DT seized; freed and greased the bearing.",
                          f"DT cooling fan seized {u}", "fix", site="global")
            assert doc["sync_state"] == "pending"
            assert doc["doc_id"] in {o["doc_id"] for o in A.outbox()}
        with flow.step("sync refuses while offline"):
            r = A.sync()
            assert r["ok"] is False and r["error"] == "uplink off", r
        with flow.step("the new memory and the fleet mirror are both searchable offline"):
            assert A.search_ids(f"transformer radiator fan seized bearing {u}")[0] == doc["doc_id"]
            assert A.search("permit to work before line work")["results"][0]["title"] == \
                "Permit-to-work before any line work"
        with flow.step("the node still answers questions offline"):
            ans = A.ask("what must I do before working on a line?")
            assert ans["online"] is False and ans["sources"], ans
        with flow.step("nothing reached the cloud"):
            assert hub.cloud_doc(doc["doc_id"]) is None
    with flow.step("uplink back: the queue drains and the fleet gets the write"):
        hub.wait_cloud(doc["doc_id"], "queued write uploaded after reconnect", timeout=20)
        wait_for(lambda: doc["doc_id"] not in {o["doc_id"] for o in A.outbox()}, "tablet-A outbox to drain", 20)
        B.wait_doc(doc["doc_id"], "receives the write made offline")
        assert A.state()["offline_since"] is None


def test_the_offline_queue_drains_in_priority_order_safety_first(hub, flow):
    A = hub.A
    u = uid()
    with A.offline():
        with flow.step("four writes of different priority while offline"):
            manual = A.write(f"Updated checklist {u} for routine LT box inspection.", f"LT box checklist {u}",
                             "manual", site="global")
            fix = A.write(f"Re-tightened the neutral link {u} at the DT.", f"Neutral link {u}", "fix", site="global")
            safety = A.write(f"Sparks {u} seen at the LT distribution box; isolated it.", f"Sparks at LT box {u}", "fix",
                             site="global")
            incident = A.write(f"Pole {u} leaning after a truck hit it; cordoned off.", f"Pole hit by truck {u}",
                               "incident", site="global")
        with flow.step("the outbox is ordered: safety keyword and incidents first, manuals last"):
            order = [o["doc_id"] for o in A.outbox() if o["doc_id"] in
                     {d["doc_id"] for d in (manual, fix, safety, incident)}]
            want = [safety["doc_id"], incident["doc_id"], fix["doc_id"], manual["doc_id"]]
            names = {d["doc_id"]: d["title"] for d in (manual, fix, safety, incident)}
            assert order == want, f"outbox order {[names[i] for i in order]}, expected {[names[i] for i in want]}"
    with flow.step("after reconnect the gateway received them in that order (server sequence numbers)"):
        seq = {d["doc_id"]: hub.wait_cloud(d["doc_id"], f"'{d['title']}' uploaded")["seq"]
               for d in (manual, fix, safety, incident)}
        got = sorted(seq, key=seq.get)
        assert got == want, f"upload order {[names[i] for i in got]}, expected {[names[i] for i in want]}"


def test_a_real_cloud_outage_queues_writes_and_they_drain_by_themselves(hub, flow, cloud_down):
    A, B = hub.A, hub.B
    u = uid()
    with flow.step("Qdrant Server and the gateway are really stopped"):
        procs = hub.config()["procs"]
        assert procs["gateway"] is False and procs.get("qdrant", False) is False, procs
        hub.get("api/cloud/memory", expect=(503,))
    with flow.step("tablet-A writes an incident; sync fails with 'unreachable' and it stays queued"):
        doc = A.write(f"Conductor snapped {u} near the bus stand; area cordoned, feeder isolated.",
                      f"Snapped conductor at bus stand {u}", "incident", site="global")
        r = A.sync()
        assert r["ok"] is False and "unreachable" in r["error"], r
        assert doc["doc_id"] in {o["doc_id"] for o in A.outbox()}
        wait_for(lambda: A.state()["connected"] is False, "tablet-A to report it is disconnected", 15)
    with flow.step("both nodes keep searching their own shards during the outage"):
        assert A.search_ids(f"conductor snapped near the bus stand {u}")[0] == doc["doc_id"]
        r = B.search("permit to work before line work")
        assert r["results"][0]["title"] == "Permit-to-work before any line work" and r["latency_ms"] < 500
    with flow.step("the cloud restarts"):
        hub.cloud_start()
    with flow.step("tablet-A drains its queue by itself (no manual sync) and tablet-B receives the incident"):
        wait_for(lambda: doc["doc_id"] not in {o["doc_id"] for o in A.outbox()}, "tablet-A outbox to drain", 60, 1)
        hub.wait_cloud(doc["doc_id"], "incident uploaded after the outage", timeout=30)
        B.wait_doc(doc["doc_id"], "receives the incident after the outage", timeout=30)


def test_two_nodes_offline_at_the_same_time_exchange_their_writes_on_reconnect(hub, flow):
    A, B = hub.A, hub.B
    u = uid()
    with flow.step("both nodes go offline and each writes something"):
        A.set_online(False)
        B.set_online(False)
        try:
            a = A.write(f"Trimmed trees {u} along the 11 kV Karve Road section.", f"Tree trimming {u}", "fix",
                        site="global")
            b = B.write(f"Replaced the rusted DT fencing gate {u} at Hingna.", f"DT fencing {u}", "fix", site="global")
        finally:
            A.set_online(True)
            B.set_online(True)
    with flow.step("after reconnect each node has the other's write"):
        A.wait_doc(b["doc_id"], "receives B's offline write")
        B.wait_doc(a["doc_id"], "receives A's offline write")
