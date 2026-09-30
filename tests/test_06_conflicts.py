"""Evolving memory: concurrent edits (conflicts) and how they resolve, edit-vs-delete, sensor readings, duplicates.

PS outcome: "Handle evolving local memory, updates, and conflicting information."
"""
from helpers import uid, wait_for

A_ADD = " Crew A: use the yellow earthing kit."
B_ADD = " Crew B: always two-person verification."


def shared_memory(hub, kind="manual", text=None, title=None):
    """tablet-A publishes a memory to every circle; wait until both nodes hold cloud version 1."""
    u = uid()
    doc = hub.A.write(text or f"Before climbing pole {u}, inspect the pole base and check the climbing gear.",
                      title or f"Pole climbing check {u}", kind, site="global")
    hub.B.wait_doc(doc["doc_id"], "receives the shared memory", lambda d: d.get("version", 0) >= 1)
    hub.A.wait_doc(doc["doc_id"], "has its write confirmed by the cloud",
                   lambda d: d.get("version", 0) >= 1 and d["sync_state"] == "synced")
    return hub.A.doc(doc["doc_id"])


def edit_both_offline(hub, doc, a_text, b_text, b_action=None):
    """Both crews edit the same memory offline. A reconnects first (its edit wins the race), then B."""
    A, B = hub.A, hub.B
    A.set_online(False)
    B.set_online(False)
    a_back = False
    try:
        A.edit(doc, a_text)
        B.edit(doc, b_text) if b_action is None else b_action()
        A.set_online(True)
        a_back = True
        hub.wait_cloud(doc["doc_id"], "tablet-A's edit uploaded first", lambda c: c["text"] == a_text)
    finally:
        if not a_back:
            A.set_online(True)
        B.set_online(True)


def make_conflict(hub, flow):
    with flow.step("tablet-A shares a memory; both nodes hold v1"):
        doc = shared_memory(hub)
    a_text, b_text = doc["text"] + A_ADD, doc["text"] + B_ADD
    with flow.step("both crews edit it offline; A reconnects first"):
        edit_both_offline(hub, doc, a_text, b_text)
    with flow.step("tablet-B reconnects: its edit is based on a stale version -> conflict"):
        conflict = wait_for(lambda: next((c for c in hub.B.review()["conflicts"] if c["doc_id"] == doc["doc_id"]), None),
                            "tablet-B to report the conflict in its review queue", 20)
        assert "yellow" in conflict["cloud"]["text"], "the conflict should show the cloud's (A's) version"
        assert "two-person" in conflict["local"]["text"], "the conflict should show B's own version"
        assert hub.B.doc(doc["doc_id"])["sync_state"] == "conflict"
    return doc


def test_resolve_a_conflict_by_merging_both_edits(hub, flow):
    doc = make_conflict(hub, flow)
    with flow.step("tablet-B chooses 'merge'"):
        hub.B.resolve(doc["doc_id"], "merge")
    with flow.step("both edits reach tablet-A as one merged version"):
        merged = hub.A.wait_doc(doc["doc_id"], "receives the merged text",
                                lambda d: "yellow" in d["text"] and "two-person" in d["text"])
        assert merged["version"] >= 3, merged["version"]
        assert hub.B.review()["conflicts"] == []
    with flow.step("both nodes' memory history shows how it evolved"):
        a_events = [h["event"] for h in hub.A.history(doc["doc_id"])]
        b_events = [h["event"] for h in hub.B.history(doc["doc_id"])]
        assert "received from tablet-B" in a_events, a_events
        assert "conflict resolved on this node: merge" in b_events, b_events


def test_resolve_a_conflict_by_keeping_mine(hub, flow):
    doc = make_conflict(hub, flow)
    with flow.step("tablet-B chooses 'keep mine'"):
        hub.B.resolve(doc["doc_id"], "mine")
    with flow.step("B's version becomes the fleet version"):
        hub.A.wait_doc(doc["doc_id"], "receives B's version", lambda d: "two-person" in d["text"] and "yellow" not in d["text"])
        hub.wait_cloud(doc["doc_id"], "cloud holds B's version", lambda c: "two-person" in c["text"])


def test_resolve_a_conflict_by_taking_theirs(hub, flow):
    doc = make_conflict(hub, flow)
    with flow.step("tablet-B chooses 'take theirs'"):
        hub.B.resolve(doc["doc_id"], "theirs")
    with flow.step("B drops its edit and shows A's version; nothing is re-sent"):
        hub.B.wait_doc(doc["doc_id"], "shows A's version", lambda d: "yellow" in d["text"] and "two-person" not in d["text"])
        assert doc["doc_id"] not in {o["doc_id"] for o in hub.B.outbox()}
        assert "two-person" not in hub.cloud_doc(doc["doc_id"])["text"]


def test_resolve_rejects_unknown_conflicts_and_strategies(hub):
    hub.B.resolve("00000000-0000-0000-0000-00000000beef", "merge", expect=(404,))
    hub.B.resolve("00000000-0000-0000-0000-00000000beef", "coin-flip", expect=(400,))


def test_an_edit_wins_over_a_concurrent_delete(hub, flow):
    with flow.step("tablet-A shares a memory; both nodes hold v1"):
        doc = shared_memory(hub)
    a_text = doc["text"] + A_ADD
    with flow.step("offline: A edits it while B deletes it; A reconnects first"):
        edit_both_offline(hub, doc, a_text, None, b_action=lambda: hub.B.delete(doc["doc_id"]))
    with flow.step("B's delete is skipped: the memory stays, with A's edit"):
        hub.B.wait_doc(doc["doc_id"], "keeps the memory with A's edit", lambda d: "yellow" in d["text"])
        assert any(e["event"] == "delete skipped" for e in hub.B.log(100))
        assert hub.cloud_doc(doc["doc_id"]) is not None


def test_sensor_readings_resolve_automatically_newest_wins(hub, flow):
    u = uid()
    with flow.step("tablet-A shares a transformer reading"):
        doc = shared_memory(hub, "reading", f"DT T-{u} top-oil temperature 60 C at 10:00.", f"DT T-{u} oil temperature")
    older, newer = f"DT T-{u} top-oil temperature 62 C at 11:00.", f"DT T-{u} top-oil temperature 65 C at 12:00."
    with flow.step("offline: A records an older reading, B a newer one; A reconnects first"):
        edit_both_offline(hub, doc, older, newer)
    with flow.step("B's newer reading wins everywhere, with no conflict for a human to resolve"):
        hub.wait_cloud(doc["doc_id"], "cloud holds the newest reading", lambda c: "65 C" in c["text"], timeout=30)
        hub.A.wait_doc(doc["doc_id"], "gets the newest reading", lambda d: "65 C" in d["text"], timeout=30)
        assert not [c for c in hub.B.review()["conflicts"] if c["doc_id"] == doc["doc_id"]]
        assert any(e["event"] == "conflict auto-resolved" for e in hub.B.log(100))


def test_near_identical_memories_are_linked_as_duplicates_not_merged(hub, flow):
    u = uid()
    text, title = f"Fitted a drip loop on the street light feed {u} to keep water out.", f"Street light drip loop {u}"
    with flow.step("tablet-A shares a fix"):
        first = hub.A.write(text, title, "fix", site="global")
        hub.wait_cloud(first["doc_id"], "first copy uploaded")
    with flow.step("tablet-B writes the same fix independently"):
        second = hub.B.write(text, title, "fix", site="global")
    with flow.step("the gateway links B's copy to A's instead of overwriting it"):
        c = hub.wait_cloud(second["doc_id"], "second copy uploaded")
        assert (c.get("similar_to") or {}).get("doc_id") == first["doc_id"], c.get("similar_to")
        assert hub.cloud_doc(first["doc_id"]) is not None, "the first copy must not be replaced"
        wait_for(lambda: any(e["event"] == "possible duplicate" for e in hub.B.log(100)),
                 "tablet-B to log the possible duplicate", 15)
