"""Sync policy: what stays on the device, what goes to the fleet, and in what order.

Two layers:
  1. Hard rules (never overridden): credentials never leave; user-private stays; PII
     only leaves as a redacted copy.
  2. Adaptive score: how valuable the memory is to the rest of the fleet right now.
       team_value   – prior by kind (fixes and incidents help others; notes are personal)
       fleet_demand – similarity to searches that FAILED on other devices recently
       local_use    – how often this device itself retrieves it
     A personal note that matches what another crew is looking for becomes a
     "suggest share" (one tap), and anything matching fleet demand jumps the queue.

Every decision returns its reasons and the score breakdown, so the UI can explain it.
"""
import math
import re

KINDS = ["incident", "fix", "reading", "manual", "note"]
SHAREABLE_KINDS = {"incident", "fix", "reading", "manual"}
PRIORITY = {"incident": 0, "fix": 1, "reading": 2, "manual": 3, "note": 4}
TEAM_VALUE = {"incident": 1.0, "fix": 0.9, "manual": 0.8, "reading": 0.7, "note": 0.2}
SAFETY_WORDS = re.compile(r"\b(fire|smoke|leak(?:ing|age)?|shock|electrocut\w*|injur\w*|explo\w*|sparks?|burn\w*|"
                          r"snapped|live wire|flash\w*)\b", re.I)

PII_PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("ID_NUMBER", re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b")),  # Aadhaar-like 12 digits
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("PHONE", re.compile(r"(?:\+?91[ -]?)?\b[6-9]\d{9}\b")),
]
SECRET_PATTERN = re.compile(r"\b(password|passcode|pwd|pin|otp)\b\s*[:=\-]?\s*\S+", re.I)
SUGGEST_THRESHOLD = 0.55


def find_pii(text):
    return [(label, mt.start(), mt.end()) for label, pat in PII_PATTERNS for mt in pat.finditer(text)]


def redact(text):
    text = SECRET_PATTERN.sub(lambda mt: f"{mt.group(1)} [REDACTED]", text)
    for label, pat in PII_PATTERNS:
        text = pat.sub(f"[{label}]", text)
    return text


def decide(text, kind, visibility="auto", demand=None, local_use=0, demand_threshold=SUGGEST_THRESHOLD):
    """demand: best match against fleet demand, e.g. {"score": 0.71, "device": "tablet-B", "query": "..."}"""
    reasons = []
    priority = PRIORITY.get(kind, 4)
    fleet = float(demand["score"]) if demand else 0.0
    use = 1 - math.exp(-local_use / 3)
    team = TEAM_VALUE.get(kind, 0.2)
    share_score = round(max(team, fleet) * 0.85 + use * 0.15, 2)
    scores = {"team_value": team, "fleet_demand": round(fleet, 2), "local_use": round(use, 2),
              "share_score": share_score}

    if SAFETY_WORDS.search(text):
        priority = 0
        reasons.append("safety keyword: jumps to the front of the sync queue")
    if demand and fleet >= demand_threshold:
        priority = max(0, priority - 1)
        reasons.append(f"{demand['device']} searched “{demand['query']}” and found nothing good (match {fleet:.2f})")

    def out(decision, why):
        return {"decision": decision, "reasons": reasons + why, "priority": priority, "scores": scores}

    if visibility == "private":
        return out("local", ["marked private by you"])
    if SECRET_PATTERN.search(text):
        return out("local", ["contains a credential (password/PIN/OTP): never leaves the device"])

    pii = find_pii(text)
    if visibility != "shared" and kind not in SHAREABLE_KINDS:
        if demand and fleet >= demand_threshold:
            return out("suggest", ["personal note, but another crew needs it: tap Share to send"
                                   + (" (PII will be redacted)" if pii else "")])
        return out("local", [f"'{kind}' is personal by default (share score {share_score})"])

    why = "shared by you" if visibility == "shared" else f"'{kind}' is team knowledge (share score {share_score})"
    if pii:
        labels = ", ".join(sorted({p[0].lower() for p in pii}))
        return out("sync_redacted", [why, f"contains {labels}: the fleet gets a redacted copy, the original stays here"])
    return out("sync", [why])
