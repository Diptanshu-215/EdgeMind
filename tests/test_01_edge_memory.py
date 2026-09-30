"""Edge memory: searchable semantic memory kept directly on the edge device (write, edit, delete, history, restart).

PS goal: "Maintain searchable semantic memory directly on an edge device."
"""
from edge_support import fleet_doc, ids, receive
from helpers import uid


# ---------------------------------------------------------------- component: one device, in-process
def test_write_is_stored_and_searchable_on_the_device(device):
    doc = device.write("Replaced the cracked disc insulator on pole 42 of feeder K-7 with a polymer insulator.",
                       "Disc insulator swap on K-7", "fix")
    assert doc["sync_state"] == "pending", "a fix is team knowledge: it should be queued for the fleet"
    assert [o["doc_id"] for o in device.outbox()] == [doc["doc_id"]]
    stored = {d["doc_id"]: d for d in device.merged_docs()}
    assert doc["doc_id"] in stored, "the memory is not in the device's Qdrant Edge shard"
    assert stored[doc["doc_id"]]["layer"] == "local"
    r = device.search("cracked disc insulator on feeder K-7")
    assert ids(r)[0] == doc["doc_id"], f"top result was {r['results'][0]['title'] if r['results'] else None!r}"


def test_edit_creates_a_new_version_and_reindexes_it(device):
    doc = device.write("Cleaned quokka carbon tracking off the LT bushing porcelain.", "Bushing cleaning", "fix")
    edited = device.write("Replaced the LT bushing because the zircon glaze had cracked.", "Bushing cleaning", "fix",
                          doc_id=doc["doc_id"])
    assert edited["doc_id"] == doc["doc_id"] and edited["edits"] == 1
    assert edited["created_at"] == doc["created_at"], "an edit must keep the original creation time"
    view = next(d for d in device.merged_docs() if d["doc_id"] == doc["doc_id"])
    assert "zircon" in view["text"] and "quokka" not in view["text"]
    assert doc["doc_id"] in ids(device.search("zircon", mode="keyword")), "the new text is not indexed (BM25)"
    assert doc["doc_id"] not in ids(device.search("quokka", mode="keyword")), "the old text is still indexed"
    events = [h["event"] for h in device.history(doc["doc_id"])]
    assert events == ["created on this node", "edited on this node"], events


def test_deleting_a_device_only_memory_removes_it(device):
    note = device.write("The substation store key is kept with the watchman at gate two.", "Store key", "note")
    assert note["sync_state"] == "local_only"
    assert device.delete(note["doc_id"]) is True
    assert note["doc_id"] not in {d["doc_id"] for d in device.merged_docs()}
    assert note["doc_id"] not in ids(device.search("watchman gate store key", mode="keyword"))
    assert device.outbox() == [], "a device-only memory has nothing to tell the fleet when deleted"


def test_deleting_a_fleet_memory_leaves_a_tombstone_that_hides_it(device):
    fid = receive(device, "global", fleet_doc("Capacitor bank fuse",
                                              "Replace blown capacitor bank fuses with the xylophone rated spare set."))
    assert fid in ids(device.search("xylophone", mode="keyword"))
    device.delete(fid)
    tomb = device.get_local(fid)
    assert tomb and tomb["deleted"] is True and tomb["sync_state"] == "pending"
    assert [(o["doc_id"], o["op"]) for o in device.outbox()] == [(fid, "delete")]
    assert device.get_mirror(fid) is not None, "the fleet copy stays until the server confirms the delete"
    assert fid not in {d["doc_id"] for d in device.merged_docs()}
    assert fid not in ids(device.search("xylophone", mode="keyword")), "a deleted memory came back in keyword search"
    assert fid not in ids(device.search("capacitor bank fuses spare set")), "a deleted memory came back in hybrid search"


def test_a_local_edit_overrides_the_older_fleet_copy(device):
    fid = receive(device, "global", fleet_doc("Earth pit check", "Check earth pits after the monsoon.", version=3))
    device.write("Check earth pits after the monsoon and after every walrus storm.", "Earth pit check", "manual",
                 doc_id=fid)
    view = next(d for d in device.merged_docs() if d["doc_id"] == fid)
    assert view["layer"] == "local" and "walrus" in view["text"]
    assert view["base_version"] == 3, "the edit must be based on the fleet version it replaces"
    hits = device.search("walrus storm earth pits", mode="keyword")["results"]
    assert [h["doc_id"] for h in hits].count(fid) == 1, "the local and fleet copies must merge into one result"
    assert hits[0]["layer"] == "local"


def test_memory_survives_a_device_restart(make_device):
    dev = make_device()
    fix = dev.write("Re-crimped the service cable lug at meter box 17.", "Lug re-crimp at meter box 17", "fix")
    note = dev.write("Bring the spare crimping tool tomorrow.", "Crimping tool", "note")
    dev_id = dev.id
    dev.close()
    again = make_device(dev_id)
    stored = {d["doc_id"] for d in again.merged_docs()}
    assert {fix["doc_id"], note["doc_id"]} <= stored, "memories were lost across a restart"
    assert ids(again.search("service cable lug meter box"))[0] == fix["doc_id"]
    assert [o["doc_id"] for o in again.outbox()] == [fix["doc_id"]], "the sync queue was lost across a restart"


def test_facet_counts_and_memory_stats(device):
    device.write("Tightened loose jumper at pole 9.", "Jumper at pole 9", "fix")
    device.write("Always wear insulated gloves on LT work.", "Gloves on LT work", "manual")
    device.write("Call the stores clerk after lunch.", "Stores clerk", "note")
    receive(device, "global", fleet_doc("Conductor down near market", "Conductor down; cordoned the area.",
                                        kind="incident"))
    assert device.facets("kind") == {"fix": 1, "manual": 1, "note": 1, "incident": 1}
    st = device.stats()
    assert st["memories"] == 4 and st["own"] == 3
    assert st["by_state"] == {"pending": 2, "local_only": 1}, st["by_state"]
    assert st["outbox"] == 2 and st["mirrors"]["global"] == 1


# ---------------------------------------------------------------- system: a real node over HTTP
def test_node_api_write_edit_delete_roundtrip(hub, flow):
    A = hub.A
    u = uid()
    with flow.step("write a memory on tablet-A over HTTP"):
        doc = A.write(f"Replaced a corroded jumper clamp {u} on the pole-top AB switch.", f"AB switch clamp {u}", "fix",
                      site="global")
        assert doc["doc_id"] and doc["sync_state"] == "pending"
    with flow.step("it is listed and searchable on the node"):
        assert A.doc(doc["doc_id"]), "not listed in GET /memory"
        assert doc["doc_id"] in A.search_ids(u, mode="keyword")
    with flow.step("edit it: the new text replaces the old"):
        A.edit(doc, f"Replaced a corroded jumper clamp {u} and greased the AB switch contacts.")
        A.wait_doc(doc["doc_id"], "shows the edited text", lambda d: "greased" in d["text"])
        events = [h["event"] for h in A.history(doc["doc_id"])]
        assert events[0] == "created on this node" and "edited on this node" in events, events
    with flow.step("delete it: gone from the list and from search"):
        assert A.delete(doc["doc_id"])["ok"] is True
        A.wait_gone(doc["doc_id"], "memory removed from GET /memory")
        assert doc["doc_id"] not in A.search_ids(u, mode="keyword")


def test_node_api_rejects_invalid_input(hub):
    A = hub.A
    A.post("memory", json={"text": "   ", "kind": "fix"}, expect=(400,))
    A.post("memory", json={"text": "ok", "kind": "banana"}, expect=(400,))
    A.post("memory", json={"text": "ok", "visibility": "public"}, expect=(422,))
    A.post("memory", json={"text": "x" * 4001}, expect=(422,))
    A.post("memory", json={"text": "ok", "title": "t" * 161}, expect=(422,))
    A.post("search", json={"q": "  "}, expect=(400,))
    A.post("search", json={"q": "fuse", "mode": "fuzzy"}, expect=(422,))
    A.post("search", json={"q": "fuse", "limit": 0}, expect=(422,))
    assert A.delete("00000000-0000-0000-0000-00000000dead") == {"ok": False}
    assert A.history("00000000-0000-0000-0000-00000000dead") == []
