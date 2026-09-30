"""On-device AI answers: instant cited answers from the node's own memory, offline; honest "don't know";
optional local LLM (Ollama) streaming a grounded answer.

PS outcome: "A complete edge-native AI product that can remember, retrieve, operate offline ..."
"""
import pytest

from app import answer
from helpers import uid

PTW = {"doc_id": "1", "title": "Permit-to-work before any line work",
       "text": "Never start work on the word of the substation alone. Get a written permit-to-work with a PTW number, "
               "open the feeder breaker and the AB switch, test for dead with a HV tester, and earth the line on both "
               "sides of the work point before touching it."}
EARTH = {"doc_id": "2", "title": "Earthing resistance check",
         "text": "Measure DT neutral and body earth pits with an earth tester after the monsoon. Keep each pit under 5 ohm."}
QUESTION = "what must I do before working on a line?"
UNKNOWN = "How do I update the firmware of a rooftop solar inverter?"


# ---------------------------------------------------------------- component
def test_the_instant_answer_is_extracted_from_the_notes_with_citations():
    a = answer.answer(QUESTION, [PTW, EARTH])
    assert "[1]" in a["answer"] and "permit" in a["answer"].lower(), a["answer"]
    assert [s["n"] for s in a["sources"]] == [1, 2] and a["sources"][0]["title"] == PTW["title"]
    assert a["engine"].startswith("instant") and a["ms"] >= 0


def test_an_unsure_answer_says_so_instead_of_guessing():
    a = answer.answer(UNKNOWN, [EARTH], confident=False)
    assert a["answer"].startswith("Nothing on this node clearly answers that."), a["answer"]
    assert answer.answer(UNKNOWN, [])["answer"] == "Nothing on this node answers that yet."


def test_the_question_language_is_detected():
    assert answer.language("लाइन पर काम शुरू करने से पहले क्या करना चाहिए?") == "hi/mr"
    assert answer.language(QUESTION) == "en"


def test_the_llm_prompt_only_contains_the_retrieved_notes():
    msgs = answer._messages(QUESTION, [PTW, EARTH])
    assert "ONLY the numbered notes" in msgs[0]["content"] and answer.NOT_IN_NOTES in msgs[0]["content"]
    assert f"[1] {PTW['title']}" in msgs[1]["content"] and f"[2] {EARTH['title']}" in msgs[1]["content"]
    hindi = answer._messages("लाइन पर काम से पहले क्या करें?", [PTW])
    assert "answer in simple English" in hindi[1]["content"]


# ---------------------------------------------------------------- system
def test_a_node_answers_offline_with_citations(hub):
    with hub.A.offline():
        ans = hub.A.ask(QUESTION)
    assert ans["online"] is False and ans["miss"] is False, ans
    assert "[1]" in ans["answer"] and "permit" in ans["answer"].lower(), ans["answer"]
    assert ans["sources"] and ans["sources"][0]["title"], ans["sources"]
    assert ans["search_ms"] < 500, f"retrieval for the answer took {ans['search_ms']} ms"


def test_a_node_says_it_does_not_know_instead_of_inventing(hub):
    ans = hub.B.ask(UNKNOWN)
    assert ans["miss"] is True, f"expected a miss, best match {ans['top_match']}"
    assert ans["answer"].startswith("Nothing on this node clearly answers that."), ans["answer"]
    assert ans["llm"] is False, "the LLM must not be offered when the notes don't cover the question"


def test_knowledge_from_another_crew_answers_questions_offline(hub, flow):
    u = uid()
    with flow.step("tablet-B shares a fix with every circle"):
        doc = hub.B.write(f"SF6 gas pressure on the ring main unit {u} read low. Topped up SF6 gas to the green band "
                          "and checked the cable box seals.", f"RMU SF6 pressure low {u}", "fix", site="global")
        hub.A.wait_doc(doc["doc_id"], "receives B's fix")
    with flow.step("tablet-A, offline, answers a question from it with a citation"):
        with hub.A.offline():
            ans = hub.A.ask("what to do when the ring main unit SF6 gas pressure is low?")
        assert doc["doc_id"] in [s["doc_id"] for s in ans["sources"]], [s["title"] for s in ans["sources"]]
        assert "SF6" in ans["answer"], ans["answer"]


def test_the_local_llm_streams_a_grounded_answer_or_admits_it_does_not_know(hub, flow):
    if not hub.A.state()["llm"]:
        pytest.skip("no local LLM (Ollama with the configured model) on this machine")
    with flow.step("grounded answer from the node's memory"):
        text, done = hub.A.ask_stream(QUESTION)
        assert not done.get("error"), done
        assert not done.get("not_in_notes") and "permit" in text.lower(), (text, done)
    with flow.step("an unrelated question is refused, not invented"):
        text, done = hub.B.ask_stream(UNKNOWN)
        assert done.get("not_in_notes"), (text, done)
