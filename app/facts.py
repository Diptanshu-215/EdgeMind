"""Conflicting-information detection (works offline, no model needed).

Two memories contradict when they are about the same thing (high semantic similarity,
shared words around the number) but state a different value in the same unit,
e.g. "torque bushing terminals to 40 Nm" vs "tightened bushing terminals to 50 Nm".
Sensor readings are excluded: different readings are expected, not contradictions.
"""
import re

UNIT_ALIASES = {
    "nm": "Nm", "n-m": "Nm", "n·m": "Nm", "n.m": "Nm", "kva": "kVA", "kv": "kV", "kw": "kW", "mw": "MW",
    "v": "V", "a": "A", "amp": "A", "amps": "A", "ohm": "ohm", "ohms": "ohm", "ω": "ohm", "°c": "°C", "degc": "°C",
    "psi": "psi", "bar": "bar", "mm": "mm", "kg": "kg", "hz": "Hz", "rpm": "rpm", "%": "%",
}
QTY = re.compile(r"(\d+(?:\.\d+)?)\s*(n\s?[·\-.]?\s?m|kva|kv|kw|mw|ohms?|ω|°\s?c|deg\s?c|psi|bar|mm|kg|hz|rpm|%|amps?|a|v)(?![a-z])",
                 re.I)
STOP = {"with", "from", "that", "this", "have", "each", "when", "then", "than", "into", "only", "after", "before",
        "under", "over", "least", "about", "every", "until", "next", "also", "were", "your"}
SKIP_KINDS = {"reading"}


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 5 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _words(s):
    return {_stem(w) for w in re.findall(r"[a-z]{4,}", s.lower()) if w not in STOP}


def quantities(text):
    out = []
    for sentence in re.split(r"(?<=[.;!?])\s+", text):
        for mt in QTY.finditer(sentence):
            unit = UNIT_ALIASES.get(re.sub(r"\s", "", mt.group(2).lower()), mt.group(2))
            out.append({"value": float(mt.group(1)), "unit": unit, "raw": mt.group(0).strip(), "ctx": _words(sentence)})
    return out


def contradictions(doc, candidates, min_shared_words=2):
    """doc / candidates: dicts with doc_id, title, text, kind (candidates also 'similarity')."""
    if doc.get("kind") in SKIP_KINDS:
        return []
    mine = quantities(f"{doc.get('title', '')}. {doc.get('text', '')}")
    if not mine:
        return []
    found = []
    for other in candidates:
        if other["doc_id"] == doc["doc_id"] or other.get("kind") in SKIP_KINDS:
            continue
        theirs = quantities(f"{other.get('title', '')}. {other.get('text', '')}")
        for a in mine:
            for b in theirs:
                if a["unit"] != b["unit"] or len(a["ctx"] & b["ctx"]) < min_shared_words:
                    continue
                if abs(a["value"] - b["value"]) > 0.05 * max(a["value"], b["value"]):
                    found.append({"doc_id": other["doc_id"], "title": other.get("title"), "unit": a["unit"],
                                  "mine": a["raw"], "theirs": b["raw"],
                                  "about": ", ".join(sorted(a["ctx"] & b["ctx"])[:4]),
                                  "similarity": round(other.get("similarity", 0), 2)})
                    break
            else:
                continue
            break
    return found
