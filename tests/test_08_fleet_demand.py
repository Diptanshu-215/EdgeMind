"""Edge-to-cloud AI workflow: a question one crew can't answer becomes fleet demand; another crew's private
note that answers it is suggested for sharing; one tap shares it; the first crew receives it live.

PS outcome: "Demonstrate a meaningful edge-to-cloud AI workflow, rather than simply running a local vector database."
"""
import time

from app import embeddings
from helpers import wait_for

NOTE_TITLE = "Cable joint without heat-shrink kit"
NOTE = ("If the heat-shrink cable joint kit is missing, make the 11 kV cable joint with cold-shrink tubes and "
        "self-amalgamating tape. Holds until the proper kit arrives.")
QUESTION = "heat-shrink cable joint kit missing, how to joint the 11 kV cable"


def demand_from(device, device_name="tablet-B", query=QUESTION):
    emb = embeddings.get()
    device.set_meta("fleet_demand", [{"device": device_name, "query": query, "dense": emb.embed_query(query),
                                      "top": 0.2, "ts": time.time()}])


# ---------------------------------------------------------------- component
def test_a_note_written_while_the_fleet_needs_it_is_suggested(device):
    demand_from(device)
    note = device.write(NOTE, NOTE_TITLE, "note")
    assert note["sync_state"] == "suggested", note["policy"]
    assert any("tablet-B" in r for r in note["policy"]["reasons"]), note["policy"]["reasons"]
    errand = device.write("Buy milk on the way back to the depot.", "Errand", "note")
    assert errand["sync_state"] == "local_only", "an unrelated note must not be suggested"
    assert [s["doc_id"] for s in device.suggestions()] == [note["doc_id"]]


def test_an_existing_note_is_re_evaluated_when_demand_arrives(device):
    note = device.write(NOTE, NOTE_TITLE, "note")
    private = device.write(NOTE, NOTE_TITLE + " (mine)", "note", visibility="private")
    assert note["sync_state"] == "local_only"
    demand_from(device)
    changed = device.reevaluate_personal()
    assert changed == [NOTE_TITLE], changed
    assert device.get_local(private["doc_id"])["sync_state"] == "local_only", "a private note must never be suggested"


def test_sharing_a_suggestion_publishes_it_fleet_wide(device):
    demand_from(device)
    note = device.write(NOTE, NOTE_TITLE, "note")
    shared = device.share(note["doc_id"])
    assert shared["sync_state"] == "pending" and shared["site"] == "global" and shared["visibility"] == "shared"
    assert note["doc_id"] in {o["doc_id"] for o in device.outbox()}


def test_failed_searches_are_queued_once_for_the_fleet(device):
    device.record_miss("rooftop solar inverter firmware")
    device.record_miss("rooftop solar inverter firmware")  # asked twice in a row: reported once
    misses = device.unsent_misses()
    assert [m["query"] for m in misses] == ["rooftop solar inverter firmware"]
    device.mark_misses_sent([m["id"] for m in misses])
    assert device.unsent_misses() == []


# ---------------------------------------------------------------- system
def test_fleet_demand_to_suggestion_to_share_to_received(hub, flow):
    A, B = hub.A, hub.B
    with flow.step("tablet-A keeps a field tip as a personal note (stays on the node)"):
        note = A.write(NOTE, NOTE_TITLE, "note")
        private = A.write(NOTE + " My own copy.", NOTE_TITLE + " (private)", "note", visibility="private")
        assert note["sync_state"] == "local_only" and private["sync_state"] == "local_only"
        assert hub.cloud_doc(note["doc_id"]) is None
    with flow.step("tablet-B can't answer the question and asks the fleet"):
        assert B.ask_fleet(QUESTION)["ok"] is True
    with flow.step("the gateway records the fleet demand"):
        wait_for(lambda: any(d["device"] == "tablet-B" and d["query"] == QUESTION for d in hub.cloud_fleet()["demand"]),
                 "the gateway to list tablet-B's question as fleet demand", 20)
    with flow.step("tablet-A learns the demand live and suggests sharing its note"):
        wait_for(lambda: note["doc_id"] in {s["doc_id"] for s in A.review()["suggestions"]},
                 "tablet-A to suggest its note for sharing", 30)
        assert any("tablet-B" in r for r in A.doc(note["doc_id"])["policy"]["reasons"])
        assert A.doc(private["doc_id"])["sync_state"] == "local_only", "the private note must never be suggested"
    with flow.step("tablet-A's crew taps Share"):
        A.share(note["doc_id"])
    with flow.step("tablet-B receives the note live"):
        got = B.wait_doc(note["doc_id"], "receives the shared note", timeout=20)
        assert got["text"] == NOTE
        assert "received from tablet-A" in [h["event"] for h in B.history(note["doc_id"])]
    with flow.step("the private copy never left tablet-A"):
        assert hub.cloud_doc(private["doc_id"]) is None


def test_a_failed_search_is_reported_to_the_fleet_automatically(hub, flow):
    q = "How do I update the firmware of a rooftop solar inverter?"
    with flow.step("tablet-B searches for something no node knows"):
        r = hub.B.search(q)
        assert r["miss"] is True, f"expected a miss; best match was {r['top_match']}"
    with flow.step("the miss reaches the gateway as fleet demand on the next sync"):
        wait_for(lambda: any(d["device"] == "tablet-B" and d["query"] == q for d in hub.cloud_fleet()["demand"]),
                 "the gateway to list the failed search as fleet demand", 30, 1)
    with flow.step("tablet-A sees tablet-B's demand"):
        wait_for(lambda: any(d["device"] == "tablet-B" and d["query"] == q for d in hub.A.state()["demand"]),
                 "tablet-A to receive tablet-B's demand", 30, 1)
