"""Conflicting facts: a memory that states a different value than an existing one (50 Nm vs 40 Nm) is flagged, offline.

PS outcome: "Handle evolving local memory, updates, and conflicting information."
"""
from app import facts
from edge_support import fleet_doc, receive
from helpers import uid, wait_for

SOP_TITLE = "DT HV bushing terminal torque"
SOP_TEXT = ("Tighten distribution transformer HV bushing terminal connectors to 40 Nm with a calibrated torque wrench. "
            "Over-tightening cracks the porcelain; under-tightening causes hot spots.")


def doc(text, kind="fix", doc_id="x", title=""):
    return {"doc_id": doc_id, "title": title, "text": text, "kind": kind}


# ---------------------------------------------------------------- component: the fact checker
def test_quantities_are_parsed_with_normalised_units():
    got = {(q["value"], q["unit"]) for q in facts.quantities("Torque to 40 N-m, 100 kVA DT, earth under 5 ohms, 72°C.")}
    assert {(40.0, "Nm"), (100.0, "kVA"), (5.0, "ohm")} <= got, got


def test_a_different_value_for_the_same_thing_is_a_contradiction():
    found = facts.contradictions(doc("Tightened the HV bushing terminals on T-88 to 50 Nm."),
                                 [doc(SOP_TEXT, "manual", "sop", SOP_TITLE)])
    assert found and found[0]["mine"] == "50 Nm" and found[0]["theirs"] == "40 Nm", found


def test_values_within_tolerance_are_not_a_contradiction():
    assert facts.contradictions(doc("Tightened the HV bushing terminals to 41 Nm."),
                                [doc(SOP_TEXT, "manual", "sop", SOP_TITLE)]) == []


def test_different_units_or_unrelated_context_are_not_contradictions():
    sop = [doc(SOP_TEXT, "manual", "sop", SOP_TITLE)]
    assert facts.contradictions(doc("Tightened the HV bushing terminals, cable is 50 mm long."), sop) == []
    assert facts.contradictions(doc("Set the pump motor coupling bolts to 50 Nm."), sop) == []


def test_sensor_readings_are_never_contradictions():
    assert facts.contradictions(doc("DT top-oil temperature 72°C.", "reading"),
                                [doc("DT top-oil temperature 64°C.", "reading", "r2")]) == []


# ---------------------------------------------------------------- component: on a device, offline
def test_a_write_that_contradicts_fleet_knowledge_is_flagged_on_the_device(device):
    sop = receive(device, "global", fleet_doc(SOP_TITLE, SOP_TEXT))
    mine = device.write("Tightened the HV bushing terminals on T-88 to 50 Nm as per the vendor sheet.",
                        "Bushing re-torqued on T-88", "fix")
    assert mine["contradicts"] and mine["contradicts"][0]["doc_id"] == sop, mine["contradicts"]
    listed = device.contradictions()
    assert [(c["doc_id"], c["other_id"]) for c in listed] == [(mine["doc_id"], sop)]
    device.dismiss_contradiction(mine["doc_id"], sop)
    assert device.contradictions() == []


def test_a_contradicting_fleet_memory_that_arrives_is_flagged(device):
    receive(device, "global", fleet_doc(SOP_TITLE, SOP_TEXT))
    incoming = fleet_doc("Bushing torque on T-5", "Torqued the HV bushing terminal connectors on T-5 to 60 Nm.",
                         kind="fix", author="tablet-Z")
    receive(device, "global", incoming)
    device.scan_new_fleet_docs([incoming["payload"]])
    assert any(c["doc_id"] == incoming["payload"]["doc_id"] for c in device.contradictions())


def test_deleting_a_memory_clears_its_contradictions(device):
    receive(device, "global", fleet_doc(SOP_TITLE, SOP_TEXT))
    mine = device.write("Tightened the HV bushing terminals on T-9 to 55 Nm.", "Bushing on T-9", "fix")
    assert device.contradictions()
    device.delete(mine["doc_id"])
    assert device.contradictions() == []


# ---------------------------------------------------------------- system
def test_a_contradiction_is_flagged_offline_the_moment_it_is_written(hub, flow):
    B = hub.B
    u = uid()
    with B.offline():
        with flow.step("tablet-B (offline) writes a torque value that differs from the SOP"):
            d = B.write(f"Tightened the HV bushing terminals on T-{u} to 50 Nm as per the vendor sheet.",
                        f"Bushing re-torqued on T-{u}", "fix", site="nagpur")
        with flow.step("the write comes back flagged: 50 Nm vs the SOP's 40 Nm"):
            c = next((x for x in d["contradicts"] if x["title"] == SOP_TITLE), None)
            assert c, f"not flagged against the SOP '{SOP_TITLE}'; flagged: {d['contradicts']}"
            assert (c["mine"], c["theirs"]) == ("50 Nm", "40 Nm"), c
        with flow.step("it is listed in tablet-B's review queue"):
            assert any(x["doc_id"] == d["doc_id"] for x in B.review()["contradictions"])
            assert any(e["event"] == "CONTRADICTION" for e in B.log(50))
    with flow.step("the crew dismisses it after checking"):
        for x in [x for x in B.review()["contradictions"] if x["doc_id"] == d["doc_id"]]:
            B.post("contradictions/dismiss", json={"doc_id": d["doc_id"], "other_id": x["other_id"]})
        assert not any(x["doc_id"] == d["doc_id"] for x in B.review()["contradictions"])


def test_a_contradicting_memory_from_another_crew_is_flagged_on_arrival(hub, flow):
    A, B = hub.A, hub.B
    u = uid()
    with flow.step("tablet-A shares a fix with a torque value that differs from the SOP"):
        d = A.write(f"Torqued the distribution transformer HV bushing terminal connectors on T-{u} to 60 Nm.",
                    f"Bushing torque on T-{u}", "fix", site="global")
    with flow.step("tablet-B receives it and flags the contradiction by itself"):
        B.wait_doc(d["doc_id"], "receives A's fix")
        wait_for(lambda: any(x["doc_id"] == d["doc_id"] for x in B.review()["contradictions"]),
                 "tablet-B to flag the incoming memory against the 40 Nm SOP", 20)
