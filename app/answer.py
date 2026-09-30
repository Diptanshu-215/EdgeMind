"""Offline question answering over the node's own memory. Nothing here needs the internet.

1. Retrieve the top memories with hybrid search (done by the caller, MMR-diversified).
2. Instant answer (milliseconds): the sentences of those memories that answer the question,
   ranked by MEANING with the on-device multilingual embedding model, each cited.
3. AI answer (streamed): a small local LLM (Ollama, default qwen2.5:1.5b) rewrites it from
   ONLY those memories with citations, or says the notes don't cover it, which turns into
   "Ask the fleet". Questions may be in Hindi or Marathi: retrieval is cross-lingual and the
   answer is written in plain English from the (English) SOPs, because 1-2B models write
   unreliable Hindi.
"""

import json
import re
import time

import httpx

from . import config

_llm_state = {"checked": 0.0, "ok": False}
NOT_IN_NOTES = "NOT_IN_NOTES"
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
SYSTEM = (
    "You are a field assistant for electricity line crews. Answer the question using ONLY the numbered notes. "
    "Write 1-3 short sentences in your own words, put safety steps first, and cite notes like [1]. "
    f"If the notes do not answer the question, reply exactly: {NOT_IN_NOTES} and give recomendations as per your knowledge. Answer in English."
)


def llm_available() -> bool:
    now = time.time()
    if now - _llm_state["checked"] > 30:
        try:
            r = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=1.0, trust_env=False)
            names = [m.get("name", "") for m in r.json().get("models", [])]
            _llm_state["ok"] = r.status_code == 200 and config.OLLAMA_MODEL in names
        except Exception:
            _llm_state["ok"] = False
        _llm_state["checked"] = now
    return _llm_state["ok"]


def language(question):
    return "hi/mr" if _DEVANAGARI.search(question) else "en"


def _messages(question, sources):
    ctx = "\n".join(
        f"[{i + 1}] {s['title']}: {s['text']}" for i, s in enumerate(sources)
    )
    note = (
        " (The question is in Hindi or Marathi; answer in simple English.)"
        if language(question) != "en"
        else ""
    )
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Notes:\n{ctx}\n\nQuestion{note}: {question}"},
    ]


def stream_llm(question, sources):
    """Yield answer tokens from the local LLM as they are generated."""
    with httpx.stream(
        "POST",
        f"{config.OLLAMA_URL}/api/chat",
        trust_env=False,
        timeout=httpx.Timeout(5, read=120),
        json={
            "model": config.OLLAMA_MODEL,
            "messages": _messages(question, sources),
            "stream": True,
            "keep_alive": "10m",
            "options": {"temperature": 0.1, "num_predict": 180},
        },
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            d = json.loads(line)
            tok = d.get("message", {}).get("content", "")
            if tok:
                yield tok
            if d.get("done"):
                return


def warm():
    """Load the model into RAM ahead of the first question (cold start is ~8 s on a laptop CPU)."""
    try:
        httpx.post(
            f"{config.OLLAMA_URL}/api/generate",
            trust_env=False,
            timeout=120,
            json={"model": config.OLLAMA_MODEL, "prompt": "", "keep_alive": "10m"},
        )
    except Exception:
        pass


_SAFETY = re.compile(r"\b(never|always|isolate|earth|permit|test for dead)\b", re.I)


def _cos(a, b):
    num = sum(x * y for x, y in zip(a, b))
    den = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5) or 1.0
    return num / den


def _extractive(question, sources, max_sentences=3):
    """Pick the sentences that answer the question, ranked by MEANING with the on-device
    embedding model (not word overlap), then keep only those close to the best one."""
    if not sources:
        return "Nothing on this node answers that yet."
    from . import embeddings

    cands = []
    for i, s in enumerate(sources):
        for sent in re.split(r"(?<=[.!?;])\s+", s["text"]):
            sent = sent.strip()
            if len(sent) > 12 and sent not in {c[1] for c in cands}:
                cands.append((i, sent))
    cands = cands[:24]
    if not cands:
        return f"Closest note: {sources[0]['text']} [1]"
    emb = embeddings.get()
    qv = emb.embed_query(question)
    vecs = emb.embed_many([f"{sources[i]['title']}. {sent}" for i, sent in cands])
    scored = []
    for (i, sent), v in zip(cands, vecs):
        score = (
            _cos(qv, v) + (0.04 if _SAFETY.search(sent) else 0) - 0.015 * i
        )  # safety steps and top memories first
        scored.append((score, i, sent))
    scored.sort(key=lambda t: -t[0])
    best = scored[0][0]
    picked = [(i, sent) for score, i, sent in scored if score >= best - 0.06][
        :max_sentences
    ]
    picked.sort(key=lambda t: t[0])
    return " ".join(f"{sent} [{i + 1}]" for i, sent in picked)


def answer(question, sources, confident=True):
    """The instant answer. The streamed AI answer comes separately (stream_llm)."""
    t0 = time.perf_counter()
    if not confident:
        best = f" Closest note: “{sources[0]['title']}” [1]." if sources else ""
        text = "Nothing on this node clearly answers that." + best
    else:
        text = _extractive(question, sources)
    return {
        "answer": text,
        "engine": "instant · extracted on the node",
        "ms": round((time.perf_counter() - t0) * 1000, 1),
        "language": language(question),
        "llm": bool(sources) and llm_available(),
        "llm_model": config.OLLAMA_MODEL,
        "sources": [
            {
                "n": i + 1,
                "doc_id": s["doc_id"],
                "title": s["title"],
                "thumb": s.get("thumb"),
            }
            for i, s in enumerate(sources)
        ],
    }
