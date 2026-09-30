"""Central configuration. Everything can be overridden with environment variables."""
import json
import os
import secrets
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("EDGEMIND_DATA", ROOT / "data"))

# ---------------------------------------------------------------- network
# Processes on the hub talk to each other over loopback. Only the console (phones,
# judges' laptops) and the gateway (remote edge nodes) listen on the LAN.
HOST = "127.0.0.1"
BIND_HOST = os.getenv("EDGEMIND_BIND", "0.0.0.0")
CONSOLE_PORT = int(os.getenv("CONSOLE_PORT", "8000"))   # dashboard, field app, process supervisor
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8100"))   # cloud sync gateway
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))     # bundled Qdrant Server (HTTP, loopback only)
GATEWAY_URL = os.getenv("GATEWAY_URL", f"http://{HOST}:{GATEWAY_PORT}").rstrip("/")

# ---------------------------------------------------------------- cloud
# QDRANT_URL set  -> use that server (Docker, Qdrant Cloud, or the bundled binary).
# QDRANT_URL empty -> gateway uses embedded Qdrant (no snapshots, point sync only).
QDRANT_URL = os.getenv("QDRANT_URL", "").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None
QDRANT_VERSION = "v1.19.1"  # bundled server version, tested with qdrant-edge-py 0.8
COLLECTION_PREFIX = "em_site_"

# ---------------------------------------------------------------- embeddings
EMBEDDER = os.getenv("EDGEMIND_EMBEDDER", "auto")  # auto | fastembed | hash
# Multilingual: a lineman can ask in Hindi or Marathi and find English SOPs (same 384 dims as bge-small)
FASTEMBED_MODEL = os.getenv("FASTEMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
CLIP_VISION = "Qdrant/clip-ViT-B-32-vision"   # photos -> 512-dim "image" vector, loaded only when needed
CLIP_TEXT = "Qdrant/clip-ViT-B-32-text"       # text -> the same space, for "find photos of ..."
IMAGE_DIM = 512
SCHEMA = 3  # bump when collections/shards change shape; stale data is rebuilt automatically
MODELS_DIR = Path(os.getenv("EDGEMIND_MODELS", DATA_DIR / "models"))
DENSE_DIM = 384
DUPLICATE_THRESHOLD = os.getenv("DUPLICATE_THRESHOLD", "")  # empty = per-embedder default

# ---------------------------------------------------------------- local LLM (optional)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")

# ---------------------------------------------------------------- fleet
SITES = {
    "pune": "Pune circle",
    "nagpur": "Nagpur circle",
    "global": "All circles (SOPs, safety)",
}
# Edge nodes the hub starts on its own machine. More can be added from the dashboard,
# and any other laptop can join over the network with `launch.py join`.
DEFAULT_NODES = [
    {"id": "tablet-A", "label": "Line crew A · Pune circle", "sites": ["pune", "global"], "port": 8001},
    {"id": "tablet-B", "label": "Line crew B · Nagpur circle", "sites": ["nagpur", "global"], "port": 8002},
]
FIRST_NODE_PORT = int(os.getenv("FIRST_NODE_PORT", "8001"))

# set by the launcher for a device process
DEVICE_ID = os.getenv("EDGEMIND_DEVICE", "")
DEVICE_LABEL = os.getenv("EDGEMIND_LABEL", "")
DEVICE_SITES = [s for s in os.getenv("EDGEMIND_SITES", "").split(",") if s]
DEVICE_PORT = int(os.getenv("EDGEMIND_PORT", "0") or 0)
DEVICE_PUBLIC_URL = os.getenv("EDGEMIND_PUBLIC_URL", "")
JOIN_CODE = os.getenv("EDGEMIND_JOIN_CODE", "")

AUTO_SYNC_SECONDS = float(os.getenv("AUTO_SYNC_SECONDS", "15"))  # fallback; pushes trigger sync instantly
NET_TIMEOUT = float(os.getenv("NET_TIMEOUT", "4"))
DEMAND_WINDOW_SECONDS = 60 * 60  # fleet search misses older than this are ignored
PRESENCE_SECONDS = 20            # a node not heard from for this long is shown offline

MAX_TEXT = 4000
MAX_TITLE = 160
MAX_QUERY = 400


def lan_ip() -> str:
    """This machine's address on the local network (no packet is sent)."""
    override = os.getenv("EDGEMIND_PUBLIC_HOST")
    if override:
        return override
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return HOST
    finally:
        s.close()


def with_flags(payload: dict) -> dict:
    """Keyword mirrors of boolean flags. qdrant-edge-py 0.8 never matches MatchValue(True) on a bool
    payload, so every filter on the edge uses these keyword fields instead."""
    payload["status"] = "deleted" if payload.get("deleted") else "active"
    if payload.get("has_photo"):
        payload["photo"] = "yes"
    else:
        payload.pop("photo", None)
    return payload


def collection(site: str) -> str:
    return f"{COLLECTION_PREFIX}{site}"


def hub_secrets() -> dict:
    """Fleet join code, operator PIN and gateway admin token.
    Generated once per hub and stored in data/secrets.json; env vars win."""
    path = DATA_DIR / "secrets.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError:
            data = {}
    changed = False
    defaults = {
        "join_code": lambda: f"EM-{secrets.randbelow(9000) + 1000}",
        "operator_pin": lambda: f"{secrets.randbelow(900000) + 100000}",
        "admin_token": lambda: secrets.token_urlsafe(24),
    }
    for key, make in defaults.items():
        if not data.get(key):
            data[key] = make()
            changed = True
    if changed:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    data["join_code"] = os.getenv("EDGEMIND_JOIN_CODE") or data["join_code"]
    data["operator_pin"] = os.getenv("EDGEMIND_PIN") or data["operator_pin"]
    data["admin_token"] = os.getenv("EDGEMIND_ADMIN_TOKEN") or data["admin_token"]
    return data
