"""Demo dataset: electricity-distribution (DISCOM) line crews.

Crews work on poles, distribution transformers (DTs) and 11 kV feeders, often in
places with poor mobile coverage. Safety procedure (permit-to-work) matters most.
"""
import time
import uuid

from . import embeddings

# (kind, site, title, text)
FLEET = [
    ("manual", "global", "Permit-to-work before any line work",
     "Never start work on the word of the substation alone. Get a written permit-to-work with a PTW number, "
     "open the feeder breaker and the AB switch, test for dead with a HV tester, and earth the line on both "
     "sides of the work point before touching it."),
    ("manual", "global", "Distribution transformer oil leak",
     "For oil seepage on a distribution transformer: check the top cover and bushing gaskets, oil level in the "
     "conservator, and silica gel colour. Top up only with tested oil (BDV of at least 30 kV). Replace gaskets at "
     "the next planned shutdown."),
    ("manual", "global", "DT HV bushing terminal torque",
     "Tighten distribution transformer HV bushing terminal connectors to 40 Nm with a calibrated torque wrench. "
     "Over-tightening cracks the porcelain; under-tightening causes hot spots."),
    ("manual", "global", "Earthing resistance check",
     "Measure DT neutral and body earth pits with an earth tester after the monsoon. Keep each pit under 5 ohm; "
     "add salt and charcoal or a new electrode if higher."),
    ("fix", "pune", "Feeder P-12 tripping in rain",
     "Feeder P-12 tripped on earth fault every rain. Patrol found a cracked pin insulator at pole 118 with flash "
     "marks. Replaced it with a polymer insulator; no trips since."),
    ("fix", "pune", "DT T-417 humming and overheating",
     "DT T-417 (100 kVA) humming and hot. Load on the R phase was 160 A against 60 A on Y. Shifted six service "
     "connections from R to Y; temperature dropped within an hour."),
    ("incident", "pune", "Snapped LT conductor near school gate",
     "LT conductor snapped and fell near the school gate on Karve Road after a tree branch hit it. Area cordoned, "
     "supply isolated, conductor re-strung. Tree trimming requested."),
    ("reading", "pune", "DT T-417 oil temperature",
     "DT T-417 top-oil temperature 72°C at 15:00, load 88%."),
    ("fix", "nagpur", "Pole-mounted DT burnt after lightning",
     "DT on Wardha Road burnt after a lightning storm. Lightning arrester was missing on the B phase and the "
     "earth lead was broken. Replaced the DT, fitted three new arresters and re-did the earth connection."),
    ("fix", "nagpur", "Street light circuit earth fault",
     "Street light circuit tripping the RCCB. Water inside a junction box at pole 42. Dried, resealed with "
     "silicone, and fitted a drip loop."),
    ("incident", "nagpur", "Bird fault on 11 kV line",
     "Repeated tripping on the 11 kV Hingna feeder traced to birds bridging the jumpers at the cut-point pole. "
     "Fitted bird guards on all three phases."),
    ("reading", "nagpur", "33/11 kV power transformer oil temperature",
     "Power transformer 2 at Hingna substation: oil 64°C, winding 71°C at 14:00, load 74%."),
]

# Local memories created on each tablet when the demo starts (goes through the sync policy).
DEVICE_NOTES = {
    "tablet-A": [
        {"kind": "note", "title": "Shift handover",
         "text": "Tell Ravi to re-check the drop-out fuse at T-417 before 9 am tomorrow."},
        {"kind": "fix", "title": "T-417 LT bushing flashover",
         "text": "LT bushing on T-417 flashed over; carbon tracking on the porcelain. Replaced bushing and cleaned. "
                 "Vendor for spare bushings: sales@shaktielectricals.in, 9876543210."},
        {"kind": "note", "title": "Drain valve leak trick",
         "text": "If the transformer drain valve is leaking and no spare valve is available: tighten the gland nut, "
                 "wrap PTFE tape on the threads and seal with epoxy putty. Holds until the next shutdown."},
    ],
    "tablet-B": [
        {"kind": "note", "title": "Store room access", "text": "Store room lock: pin 4471"},
    ],
}


def fleet_docs():
    emb = embeddings.get()
    now = time.time()
    out = []
    for kind, site, title, text in FLEET:
        doc_id = str(uuid.uuid4())
        out.append({
            "site": site, "doc_id": doc_id, "text": f"{title} {text}",
            "dense": emb.embed_dense(f"{title}. {text}"),
            "payload": {"doc_id": doc_id, "title": title, "text": text, "kind": kind, "site": site,
                        "origin_device": "hq-console", "author_device": "hq-console", "created_at": now,
                        "updated_at": now, "deleted": False, "redacted": False},
        })
    return out
