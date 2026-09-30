"""Sync policy: dynamically decide what stays on the device, what syncs, what syncs redacted, and in what order.

PS goal: "Dynamically decide what information should remain local and what should be synchronized."
"""
import pytest

from app import policy
from app.sync_client import SyncClient
from helpers import uid, wait_for

DEMAND = {"score": 0.8, "device": "tablet-B", "query": "cable joint kit missing"}


# ---------------------------------------------------------------- component: the policy itself
@pytest.mark.parametrize("kind,expected", [("note", "local"), ("fix", "sync"), ("incident", "sync"),
                                           ("manual", "sync"), ("reading", "sync")])
def test_default_decision_by_kind(kind, expected):
    assert policy.decide("Checked the feeder and cleared the fault.", kind)["decision"] == expected


def test_private_memory_stays_local_even_if_it_is_a_fix():
    d = policy.decide("Replaced the drop-out fuse.", "fix", visibility="private")
    assert d["decision"] == "local" and "marked private by you" in d["reasons"]


def test_a_note_the_crew_chooses_to_share_syncs():
    assert policy.decide("Useful trick for stuck AB switches.", "note", visibility="shared")["decision"] == "sync"


@pytest.mark.parametrize("text", ["Store room lock: pin 4471", "Portal password: hunter2", "OTP 552211 for the SCADA login"])
def test_credentials_never_leave_the_device_even_when_shared(text):
    d = policy.decide(text, "fix", visibility="shared")
    assert d["decision"] == "local", d
    assert any("credential" in r for r in d["reasons"]), d["reasons"]


def test_pin_insulator_is_not_mistaken_for_a_credential():
    """'Pin insulator' is standard line hardware (the seed data itself mentions a cracked pin insulator).
    A crew fix about one must reach the fleet like any other fix."""
    d = policy.decide("Replaced the cracked pin insulator at pole 118 with a polymer one.", "fix")
    assert d["decision"] == "sync", (
        f"a fix about a PIN INSULATOR was kept on the device as a 'credential': {d['reasons']}. "
        "policy.SECRET_PATTERN matches the word 'pin' followed by any word.")


def test_personal_data_goes_to_the_fleet_only_as_a_redacted_copy():
    text = "Spare bushings from sales@shaktielectricals.in, call 9876543210. Consumer Aadhaar 1234 5678 9012."
    d = policy.decide(text, "fix")
    assert d["decision"] == "sync_redacted", d
    red = policy.redact(text)
    assert "[EMAIL]" in red and "[PHONE]" in red and "[ID_NUMBER]" in red, red
    for raw in ("sales@shaktielectricals.in", "9876543210", "1234 5678 9012"):
        assert raw not in red, f"{raw!r} survived redaction: {red}"


def test_redaction_hides_secret_values():
    red = policy.redact("Gate lock pin 4471, portal password: hunter2")
    assert "4471" not in red and "hunter2" not in red, red


def test_safety_items_jump_the_sync_queue():
    assert policy.decide("Sparks and smoke from the LT distribution box.", "manual")["priority"] == 0
    assert policy.decide("Routine inspection checklist for LT boxes.", "manual")["priority"] == 3
    order = [policy.decide("Routine work.", k)["priority"] for k in ("incident", "fix", "reading", "manual")]
    assert order == sorted(order) and order[0] == 0, order


def test_fleet_demand_turns_a_personal_note_into_a_share_suggestion():
    d = policy.decide("Use cold-shrink tubes if the heat-shrink kit is missing.", "note", demand=DEMAND,
                      demand_threshold=0.55)
    assert d["decision"] == "suggest" and any("tablet-B" in r for r in d["reasons"]), d
    weak = dict(DEMAND, score=0.3)
    assert policy.decide("Use cold-shrink tubes.", "note", demand=weak, demand_threshold=0.55)["decision"] == "local"


def test_fleet_demand_raises_the_priority_of_matching_knowledge():
    assert policy.decide("Cable joint procedure.", "manual", demand=DEMAND, demand_threshold=0.55)["priority"] == 2


def test_every_decision_is_explained():
    d = policy.decide("Balanced the load on DT T-417.", "fix")
    assert d["reasons"] and set(d["scores"]) == {"team_value", "fleet_demand", "local_use", "share_score"}


# ---------------------------------------------------------------- component: the device applies it
def test_device_applies_the_policy_on_every_write(device):
    note = device.write("Pick up the ladder from stores.", "Ladder", "note")
    fix = device.write("Re-terminated the service cable at meter 88.", "Meter 88 cable", "fix")
    cred = device.write("Yard gate code: pin 7712", "Yard gate", "fix", visibility="shared")
    pii = device.write("Contractor on site: ravi.k@example.com, 9123456780.", "Contractor contact", "fix")
    assert (note["sync_state"], fix["sync_state"], cred["sync_state"]) == ("local_only", "pending", "local_only")
    assert pii["policy"]["decision"] == "sync_redacted" and "ravi.k@example.com" in pii["text"]
    queued = {o["doc_id"] for o in device.outbox()}
    assert queued == {fix["doc_id"], pii["doc_id"]}, "only team knowledge may be queued for the fleet"


def test_what_leaves_the_device_is_redacted_and_stripped(device):
    doc = device.write("Contractor on site: ravi.k@example.com, 9123456780.", "Contractor ravi.k@example.com", "fix")
    payload = SyncClient(device)._cloud_payload(device.get_local(doc["doc_id"]))
    assert payload["redacted"] is True
    assert "ravi.k@example.com" not in payload["text"] + payload["title"] and "9123456780" not in payload["text"]
    for local_field in ("sync_state", "visibility", "policy", "base_version"):
        assert local_field not in payload, f"device-only field {local_field!r} would be uploaded"


# ---------------------------------------------------------------- system
def test_seeded_private_notes_never_reach_the_cloud(hub):
    a, b = hub.A.titles(), hub.B.titles()
    assert a["Shift handover"]["sync_state"] == "local_only"
    assert b["Store room access"]["sync_state"] == "local_only"
    cloud = hub.cloud_memory()
    titles = {d["title"] for d in cloud}
    assert "Shift handover" not in titles and "Store room access" not in titles
    assert not any("4471" in d.get("text", "") for d in cloud), "the store-room PIN reached the cloud"


def test_personal_data_reaches_the_fleet_redacted_while_the_node_keeps_the_original(hub, flow):
    A, B = hub.A, hub.B
    u = uid()
    with flow.step("tablet-A writes a fix that contains an email and a phone number"):
        doc = A.write(f"Replaced the LT cable at the market {u}. Contractor on site: rakesh.k@example.com, 9123456780.",
                      f"LT cable at market {u}", "fix", site="global")
        assert doc["policy"]["decision"] == "sync_redacted", doc["policy"]
    with flow.step("the cloud only receives the redacted copy"):
        c = hub.wait_cloud(doc["doc_id"], "redacted copy uploaded")
        assert "[EMAIL]" in c["text"] and "[PHONE]" in c["text"], c["text"]
        assert "rakesh.k@example.com" not in c["text"] and "9123456780" not in c["text"], c["text"]
    with flow.step("tablet-A keeps the original text on the device"):
        mine = A.wait_doc(doc["doc_id"], "marked redacted_synced", lambda d: d["sync_state"] == "redacted_synced")
        assert "rakesh.k@example.com" in mine["text"]
    with flow.step("tablet-B (other crew) receives only the redacted copy"):
        theirs = B.wait_doc(doc["doc_id"], "receives the redacted copy")
        assert "rakesh.k@example.com" not in theirs["text"] and "[EMAIL]" in theirs["text"]


def test_a_credential_is_never_uploaded_even_when_marked_shared(hub):
    A = hub.A
    u = uid()
    doc = A.write(f"Transformer yard {u} gate: pin 7712.", f"Yard gate {u}", "fix", visibility="shared", site="global")
    assert doc["sync_state"] == "local_only", doc["policy"]
    r = A.sync()
    assert r["ok"], r
    assert doc["doc_id"] not in {o["doc_id"] for o in A.outbox()}
    assert hub.cloud_doc(doc["doc_id"]) is None, "a credential reached the cloud"


def test_policy_preview_explains_what_would_be_uploaded(hub):
    d = hub.A.post("policy/preview", json={"text": "Vendor for spares: sales@example.in", "kind": "fix"}).json()
    assert d["decision"] == "sync_redacted" and "[EMAIL]" in d["uploaded_text"], d
    assert d["reasons"] and "share_score" in d["scores"]


def test_making_a_shared_memory_private_withdraws_it_from_the_fleet(hub, flow):
    A, B = hub.A, hub.B
    u = uid()
    with flow.step("tablet-A shares a fix with the fleet; tablet-B receives it"):
        doc = A.write(f"Re-sagged the LT span {u} between poles 3 and 4.", f"LT span re-sag {u}", "fix", site="global")
        hub.wait_cloud(doc["doc_id"], "fix uploaded")
        B.wait_doc(doc["doc_id"], "receives the fix")
    with flow.step("tablet-A marks it private: a retract is queued"):
        A.edit(A.doc(doc["doc_id"]), visibility="private")
    with flow.step("the cloud and tablet-B drop it"):
        wait_for(lambda: hub.cloud_doc(doc["doc_id"]) is None, "cloud to withdraw the memory", 20)
        B.wait_gone(doc["doc_id"], "memory withdrawn from tablet-B", 20)
    with flow.step("tablet-A still has it, as a device-only memory"):
        A.wait_doc(doc["doc_id"], "kept as local_only", lambda d: d["sync_state"] == "local_only")
