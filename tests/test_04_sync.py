"""Edge <-> cloud sync: enrollment, snapshot bootstrap, push, point deltas, partial-snapshot reconcile, live propagation.

PS goal: "Synchronize data between edge devices and Qdrant Server when connectivity returns."
"""
import threading
import time

import httpx
import pytest

from app.sync_client import POINT_DELTA_MAX
from edge_support import fleet_doc, receive
from helpers import uid, wait_for


# ---------------------------------------------------------------- component
def test_point_sync_builds_a_mirror_shard_and_tracks_the_server_sequence(device):
    a = fleet_doc("Lightning arrester check", "Check lightning arresters on every DT before the monsoon.")
    b = fleet_doc("Drop-out fuse sizing", "Size drop-out fuses to the DT rating, never bigger.")
    receive(device, "global", a, b)
    assert device.mirrors["global"] is not None
    assert device.mirror_seq("global") == max(a["payload"]["seq"], b["payload"]["seq"])
    view = {d["doc_id"]: d for d in device.merged_docs()}
    assert view[a["payload"]["doc_id"]]["layer"] == "mirror" and view[a["payload"]["doc_id"]]["sync_state"] == "synced"


def test_a_server_side_delete_removes_the_memory_from_the_mirror(device):
    d = fleet_doc("Old SOP", "An outdated procedure.")
    receive(device, "global", d)
    receive(device, "global", fleet_doc("Old SOP", "", version=2, doc_id=d["payload"]["doc_id"], deleted=True))
    assert d["payload"]["doc_id"] not in {x["doc_id"] for x in device.merged_docs()}


def test_rehydrate_after_a_cloud_reset_republishes_this_nodes_own_memories(device):
    mine = fleet_doc("My fleet fix", "A fix this node wrote earlier.", kind="fix", author=device.id)
    other = fleet_doc("Someone else's fix", "Another crew's fix.", kind="fix", author="tablet-Z")
    receive(device, "global", mine, other)
    private = device.write("Personal reminder.", "Reminder", "note")
    n = device.rehydrate("new-epoch")
    assert n == 1, "only memories written on this node are re-published"
    assert device.mirrors["global"] is None, "mirrors from the old cloud epoch must be dropped"
    queued = {o["doc_id"] for o in device.outbox()}
    assert queued == {mine["payload"]["doc_id"]}
    assert device.get_local(private["doc_id"])["sync_state"] == "local_only", "private notes stay private"
    assert device.meta("epoch") == "new-epoch"


# ---------------------------------------------------------------- system
def test_nodes_enroll_and_bootstrap_their_mirrors_from_the_server(hub):
    st = hub.A.state()
    assert st["enrolled"] and st["connected"] and st["live"], {k: st[k] for k in ("enrolled", "connected", "live")}
    assert st["stats"]["mirrors"]["pune"] >= 4 and st["stats"]["mirrors"]["global"] >= 4, st["stats"]["mirrors"]
    if hub.snapshots:
        assert st["snapshots"] is True
        assert any(e["event"] == "full snapshot" for e in hub.A.log(300)), \
            "tablet-A never bootstrapped a mirror from a full Qdrant Server snapshot"
        assert hub.A.metrics()["full_snapshot_bytes"]["pune"] > 0


def test_each_node_only_holds_the_sites_it_subscribes_to(hub):
    a, b = hub.A.titles(), hub.B.titles()
    assert "Feeder P-12 tripping in rain" in a and "Feeder P-12 tripping in rain" not in b, "pune data leaked to B"
    assert "Bird fault on 11 kV line" in b and "Bird fault on 11 kV line" not in a, "nagpur data leaked to A"
    assert "Permit-to-work before any line work" in a and "Permit-to-work before any line work" in b


def test_a_write_on_one_node_reaches_the_other_live_without_a_manual_sync(hub, flow):
    A, B = hub.A, hub.B
    u = uid()
    with flow.step("tablet-A writes a fix for all circles"):
        t0 = time.time()
        doc = A.write(f"Re-crimped the jumper lug {u} on the test pole.", f"Jumper lug {u}", "fix", site="global")
    with flow.step("the gateway stores it in Qdrant Server"):
        c = hub.wait_cloud(doc["doc_id"], "fix uploaded", timeout=15)
        assert c["author_device"] == "tablet-A" and c["version"] == 1 and c["seq"] > 0
    with flow.step("tablet-B receives it with no manual sync (live push)"):
        B.wait_doc(doc["doc_id"], "receives the fix live", timeout=15)
        took = time.time() - t0
        assert took < 10, f"live propagation took {took:.1f}s"
    with flow.step("tablet-B measured the propagation latency"):
        wait_for(lambda: B.metrics()["propagation_last_ms"] is not None, "tablet-B propagation metric", 10)


def test_a_confirmed_write_moves_from_the_local_shard_to_the_mirror(hub):
    A = hub.A
    u = uid()
    doc = A.write(f"Replaced the stay wire insulator {u}.", f"Stay insulator {u}", "fix", site="global")
    A.wait_doc(doc["doc_id"], "local copy purged once the mirror holds it (dual-write pattern)",
               lambda d: d["layer"] == "mirror", timeout=20)
    assert doc["doc_id"] not in {o["doc_id"] for o in A.outbox()}


def test_an_edit_propagates_as_a_new_version(hub, flow):
    A, B = hub.A, hub.B
    u = uid()
    with flow.step("tablet-A writes, tablet-B receives v1"):
        doc = A.write(f"Cleaned the LT fuse unit {u}.", f"LT fuse unit {u}", "fix", site="global")
        v1 = B.wait_doc(doc["doc_id"], "receives v1", lambda d: d.get("version") == 1)
    with flow.step("tablet-B edits it"):
        B.edit(v1, v1["text"] + " Checked again by crew B.")
    with flow.step("tablet-A receives v2 with B's text"):
        A.wait_doc(doc["doc_id"], "receives v2", lambda d: "crew B" in d["text"] and d.get("version") == 2)
        events = [h["event"] for h in A.history(doc["doc_id"])]
        assert "received from tablet-B" in events, events


def test_a_delete_propagates_and_never_resurfaces_in_search(hub, flow):
    A, B = hub.A, hub.B
    u = uid()
    text = f"Conductor {u} snapped and lying live near the vegetable market. Area cordoned, feeder isolated."
    with flow.step("tablet-A writes an incident; tablet-B receives it"):
        doc = A.write(text, f"Live wire at market {u}", "incident", site="global")
        B.wait_doc(doc["doc_id"], "receives the incident")
    with flow.step("tablet-A deletes it"):
        assert A.delete(doc["doc_id"])["ok"]
    with flow.step("the cloud and tablet-B drop it"):
        wait_for(lambda: hub.cloud_doc(doc["doc_id"]) is None, "cloud to delete it", 20)
        B.wait_gone(doc["doc_id"], "memory deleted on tablet-B too")
    with flow.step("no node returns it in search any more"):
        for node in (A, B):
            assert doc["doc_id"] not in node.search_ids(text), f"{node.name} still returns the deleted memory"
            assert doc["doc_id"] not in node.search_ids(u, mode="keyword")


def test_mirrors_reconcile_by_partial_snapshot_after_point_deltas(hub, flow):
    if not hub.snapshots:
        pytest.skip("partial snapshots need Qdrant Server (stack is running in embedded mode)")
    A, B = hub.A, hub.B
    u = uid()
    t0 = time.time()
    with flow.step("tablet-A writes; tablet-B gets it as a fast point delta"):
        doc = A.write(f"Replaced the bird guard {u} on the cut-point pole.", f"Bird guard {u}", "fix", site="global")
        B.wait_doc(doc["doc_id"], "receives the point delta")
    with flow.step("when idle, tablet-B reconciles its mirror with a partial snapshot"):
        # Either segments are downloaded (logged 'partial snapshot') or the gateway answers 204 'already
        # identical' (not logged); both reset the node's point-delta counter and stamp reconciled_<site>.
        def reconciled():
            B.sync()
            return (hub.node_meta("tablet-B", "reconciled_global") or 0) > t0 \
                and not hub.node_meta("tablet-B", "point_deltas_global")
        wait_for(reconciled, "tablet-B to reconcile its global mirror (partial snapshot or 204 identical)",
                 timeout=60, every=2)
    with flow.step("no sync errors and the memory is still there"):
        errors = B.log_since(t0, {"sync error", "mirror rebuild"})
        assert not errors, errors
        assert B.doc(doc["doc_id"]), "the memory disappeared after reconciling"
        assert doc["doc_id"] in B.search_ids(u, mode="keyword")


def test_a_node_stays_responsive_while_it_applies_a_partial_snapshot(hub, flow):
    """Crews must be able to search while their node reconciles in the background."""
    if not hub.snapshots:
        pytest.skip("partial snapshots need Qdrant Server (stack is running in embedded mode)")
    A, B = hub.A, hub.B
    samples, stop = [], threading.Event()

    def crew_searching():  # a phone searching on tablet-B through the hub, 10x per second
        with httpx.Client(trust_env=False, timeout=60) as c:
            while not stop.is_set():
                t = time.time()
                r = c.post(f"{B.base}/search", json={"q": "transformer oil leak"})
                samples.append((t, time.time() - t, r.status_code))
                time.sleep(0.1)

    t0 = time.time()
    th = None
    with flow.step("tablet-B is offline while tablet-A writes more than POINT_DELTA_MAX changes"):
        with B.offline():
            for i in range(POINT_DELTA_MAX + 6):
                A.write(f"Replaced the fuse carrier on pole {i} ({uid()}).", f"Fuse carrier {uid()}", "fix",
                        site="global")
            wait_for(lambda: A.outbox() == [], "tablet-A to upload all its writes", 60)
            th = threading.Thread(target=crew_searching, daemon=True)
            th.start()  # a crew keeps searching on tablet-B from here on
            time.sleep(1)
    try:
        with flow.step("tablet-B reconnects and applies a partial snapshot (the crew is still searching)"):
            applied = wait_for(lambda: B.log_since(t0, {"partial snapshot"}),
                               "tablet-B to apply a partial snapshot with the changed segments", 60)
            time.sleep(2)
    finally:
        stop.set()
        if th:
            th.join(70)
    with flow.step("no crew search was blocked or failed while it was applied"):
        at = applied[-1]["ts"]
        around = [s for s in samples if s[0] <= at + 2]
        assert around, "no searches were measured"
        worst = max(around, key=lambda s: s[1])
        failed = [s for s in around if s[2] != 200]
        assert not failed, f"{len(failed)} searches failed during the partial snapshot, e.g. HTTP {failed[0][2]}"
        assert worst[1] < 2.0, (
            f"a search on tablet-B took {worst[1]:.1f}s while it applied a partial snapshot ({applied[-1]['detail']}); "
            f"normal is < 0.2s. EdgeDevice.apply_partial holds the device lock during update_from_snapshot, "
            f"and search() waits on that lock.")


def test_sync_status_is_reported_on_the_node_and_the_gateway(hub):
    r = hub.A.sync()
    assert r["ok"], r
    for key in ("pushed", "pulled", "conflicts", "ms", "full", "partial", "points"):
        assert key in r, f"sync report is missing {key!r}"
    st = hub.A.state()
    assert st["connected"] and st["last_sync"]["ok"] and st["sync_error"] is None
    fleet = {d["id"]: d for d in hub.cloud_fleet()["devices"]}
    assert fleet["tablet-A"]["online"] and fleet["tablet-A"]["live"], fleet["tablet-A"]
    assert fleet["tablet-A"]["sites"] == ["pune", "global"]
