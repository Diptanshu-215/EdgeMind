"""Offline question answering over the device's own memory.

1. Retrieve the top memories with hybrid search (done by the caller).
2. If a local LLM is running (Ollama, default llama3.2:1b), ask it to answer ONLY from
   those memories and cite them as [1], [2].
3. Otherwise fall back to an extractive answer: the sentences that best match the
   question, each cited. Both paths need no internet.
"""
import re
import time

import httpx

from . import config

_TOK = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "is", "are", "to", "of", "on", "in", "for", "and", "or", "what", "how", "do", "i", "my",
         "with", "it", "if", "at", "be", "should", "can", "does", "which", "when", "there", "this", "that"}
_llm_state = {"checked": 0.0, "ok": False}


def llm_available() -> bool:
    now = time.time()
    if now - _llm_state["checked"] > 30:
        try:
            r = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=1.0, trust_env=False)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            _llm_state["ok"] = r.status_code == 200 and any(n.startswith(config.OLLAMA_MODEL.split(":")[0]) for n in names)
        except Exception:
            _llm_state["ok"] = False
        _llm_state["checked"] = now
    return _llm_state["ok"]


def _llm(question, sources):
    ctx = "\n".join(f"[{i + 1}] {s['title']}: {s['text']}" for i, s in enumerate(sources))
    prompt = (
        "You are a field assistant for electricity line crews. Answer the question using ONLY the numbered notes. "
        "Be brief (at most 4 sentences), practical, and cite notes like [1]. Put any safety step first. "
        "If the notes do not answer it, say so.\n\n"
        f"Notes:\n{ctx}\n\nQuestion: {question}\nAnswer:"
    )
    r = httpx.post(f"{config.OLLAMA_URL}/api/generate", timeout=60, trust_env=False,
                   json={"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False,
                         "options": {"temperature": 0.1, "num_predict": 200}})
    r.raise_for_status()
    return r.json()["response"].strip()


def _extractive(question, sources, max_sentences=3):
    q = {w for w in _TOK.findall(question.lower()) if w not in _STOP}
    scored = []
    for i, s in enumerate(sources):
        for sent in re.split(r"(?<=[.!?;])\s+", s["text"]):
            words = set(_TOK.findall(sent.lower()))
            overlap = len(q & words)
            # earlier-ranked memories and safety steps get a boost
            score = overlap + (len(sources) - i) * 0.3 + (1.0 if re.search(r"\b(never|always|isolate|earth|permit)\b", sent, re.I) else 0)
            if overlap:
                scored.append((score, i, sent.strip()))
    if not scored:
        if not sources:
            return "Nothing on this device answers that yet."
        return f"Closest note: {sources[0]['text']} [1]"
    scored.sort(key=lambda t: -t[0])
    picked, seen = [], set()
    for _, i, sent in scored:
        if sent in seen:
            continue
        seen.add(sent)
        picked.append((i, sent))
        if len(picked) == max_sentences:
            break
    picked.sort(key=lambda t: t[0])
    return " ".join(f"{sent} [{i + 1}]" for i, sent in picked)


def answer(question, sources, confident=True):
    t0 = time.perf_counter()
    engine = "extractive (no model)"
    text = None
    if not confident:
        best = f" Closest note: “{sources[0]['title']}” [1]." if sources else ""
        text = "Nothing on this tablet clearly answers that." + best
    elif sources and llm_available():
        try:
            text = _llm(question, sources)
            engine = f"local LLM · {config.OLLAMA_MODEL}"
        except Exception:
            text = None
    if text is None:
        text = _extractive(question, sources)
    return {"answer": text, "engine": engine, "ms": round((time.perf_counter() - t0) * 1000, 1),
            "sources": [{"n": i + 1, "doc_id": s["doc_id"], "title": s["title"], "found_on": s.get("found_on", "device")}
                        for i, s in enumerate(sources)]}
