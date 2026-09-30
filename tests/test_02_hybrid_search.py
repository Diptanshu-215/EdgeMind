"""Hybrid search: low-latency vector (meaning) + keyword (BM25) search on the node, with no network.

PS outcome: "Perform low-latency vector and hybrid search without network access."
"""
import statistics

import pytest

from edge_support import ids

UNKNOWN = "How do I update the firmware of a rooftop solar inverter?"


@pytest.fixture
def corpus(device):
    w = device.write
    docs = {
        "rain": w("Feeder tripped on earth fault whenever it rained. Found a cracked disc insulator at pole 118 "
                  "and replaced it.", "Feeder tripping in rain", "fix", site="pune"),
        "snap": w("LT conductor snapped near the school gate after a tree branch hit it. Area cordoned and supply "
                  "isolated.", "Snapped conductor near school", "incident", site="pune"),
        "earth": w("Measure earth pit resistance after the monsoon and keep each pit under 5 ohm.",
                   "Earthing resistance check", "manual", site="global"),
        "hum": w("Distribution transformer humming and overheating because one phase carried far more load than "
                 "the others; balanced the load across phases.", "Transformer humming", "fix", site="global"),
    }
    return {k: d["doc_id"] for k, d in docs.items()}


# ---------------------------------------------------------------- component
def test_dense_search_finds_by_meaning(device, corpus):
    r = device.search("transformer making a noise and running hot", mode="dense")
    assert ids(r)[0] == corpus["hum"], [x["title"] for x in r["results"]]


def test_keyword_search_finds_exact_terms(device, corpus):
    r = device.search("pole 118", mode="keyword")
    assert ids(r)[0] == corpus["rain"], [x["title"] for x in r["results"]]


def test_hybrid_search_fuses_both_and_explains_the_ranking(device, corpus):
    r = device.search("cracked insulator feeder tripping in the rain")
    top = r["results"][0]
    assert top["doc_id"] == corpus["rain"], [x["title"] for x in r["results"]]
    assert {"meaning", "keywords"} <= set(top["why"]), f"per-retriever scores missing: {top['why']}"
    assert top["score"] > 0 and 0 < top["match"] <= 1
    assert r["shards"] >= 1 and r["latency_ms"] >= 0


def test_filters_by_kind_and_site(device, corpus):
    r = device.search("problem on the line", kind="incident")
    assert r["results"] and all(x["kind"] == "incident" for x in r["results"])
    r = device.search("problem on the line", site="global")
    assert r["results"] and all(x["site"] == "global" for x in r["results"])


def test_search_latency_is_low(device, corpus):
    lat = [device.search(q)["latency_ms"] for q in ["transformer hot", "earth pit", "conductor snapped",
                                                   "feeder tripping", "insulator cracked"] * 4]
    assert statistics.median(lat) < 250, f"median on-device search latency {statistics.median(lat):.1f} ms"


def test_a_question_the_node_cannot_answer_is_a_miss(device, corpus):
    assert device.search("transformer humming and overheating")["miss"] is False
    r = device.search(UNKNOWN)
    assert r["miss"] is True, f"expected a miss, best match was {r['top_match']}"
    assert UNKNOWN in [m["query"] for m in device.unsent_misses()], "the miss was not recorded for the fleet"


def test_scale_hybrid_search_on_a_large_shard(device):
    t = device.scale_test(n=2000, queries=30)
    assert t["memories"] == 2000
    assert t["p95_ms"] < 100, f"hybrid search p95 on 2,000 memories: {t['p95_ms']} ms"


# ---------------------------------------------------------------- system
def test_offline_search_on_a_node_uses_its_fleet_mirror(hub):
    A = hub.A
    with A.offline():
        assert A.state()["online"] is False
        r = A.search("transformer humming and overheating on one phase")
        top = r["results"][0]
        assert top["title"] == "DT T-417 humming and overheating", [x["title"] for x in r["results"]]
        assert top["layer"] == "mirror", "fleet knowledge should be served from the node's mirror shard"
        assert {"meaning", "keywords"} <= set(top["why"]), top["why"]
        assert r["shards"] >= 3, f"expected local + pune + global shards, got {r['shards']}"
        assert r["latency_ms"] < 500, f"offline search took {r['latency_ms']} ms"


def test_search_modes_over_the_api(hub):
    A = hub.A
    r = A.search("P-12", mode="keyword")
    assert r["results"][0]["title"] == "Feeder P-12 tripping in rain", [x["title"] for x in r["results"]]
    r = A.search("a tree branch hit the wire near a school", mode="dense")
    assert r["results"][0]["title"] == "Snapped LT conductor near school gate", [x["title"] for x in r["results"]]
    r = A.search("anything wrong", kind="incident")
    assert r["results"] and all(x["kind"] == "incident" for x in r["results"])


def test_search_latency_is_reported(hub):
    A = hub.A
    for q in ("oil leak on transformer", "earthing check", "permit to work"):
        A.search(q)
    st = A.state()
    assert st["search"]["count"] >= 3 and st["search"]["p50"] is not None, st["search"]
    assert A.metrics()["search_p95_ms"] < 500


def test_hindi_and_marathi_questions_find_english_procedures(hub):
    if "multilingual" not in hub.A.state()["embedder"]:
        pytest.skip("needs the multilingual embedding model (running with a fallback embedder)")
    for q, want in [("लाइन पर काम शुरू करने से पहले क्या करना चाहिए?", "Permit-to-work before any line work"),
                    ("ट्रान्सफॉर्मर गरम होत आहे आणि आवाज येतो", "DT T-417 humming and overheating")]:
        r = hub.A.search(q)
        assert r["results"][0]["title"] == want, (q, [x["title"] for x in r["results"]])
        assert "meaning" in r["results"][0]["why"]


def test_scale_test_endpoint(hub):
    t = hub.A.post("scale-test?n=3000", timeout=300).json()
    assert t["memories"] == 3000
    assert t["p95_ms"] < 100, f"hybrid search p95 on a 3,000-memory Qdrant Edge shard: {t['p95_ms']} ms"
