"""Central configuration. Everything can be overridden with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("EDGEMIND_DATA", ROOT / "data"))

# ---------------------------------------------------------------- ports
HOST = "127.0.0.1"
CONSOLE_PORT = int(os.getenv("CONSOLE_PORT", "8000"))   # dashboard + process supervisor
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8100"))   # cloud sync gateway
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))     # bundled Qdrant Server (HTTP)
GATEWAY_URL = os.getenv("GATEWAY_URL", f"http://{HOST}:{GATEWAY_PORT}")

# ---------------------------------------------------------------- cloud
# QDRANT_URL set  -> use that server (Docker, Qdrant Cloud, or the bundled binary).
# QDRANT_URL empty -> gateway uses embedded Qdrant (no snapshots, point sync only).
QDRANT_URL = os.getenv("QDRANT_URL", "").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None
QDRANT_VERSION = "v1.19.1"  # bundled server version, tested with qdrant-edge-py 0.8
COLLECTION_PREFIX = "em_site_"

# ---------------------------------------------------------------- embeddings
EMBEDDER = os.getenv("EDGEMIND_EMBEDDER", "auto")  # auto | fastembed | hash
FASTEMBED_MODEL = os.getenv("FASTEMBED_MODEL", "BAAI/bge-small-en-v1.5")
DENSE_DIM = 384
DUPLICATE_THRESHOLD = os.getenv("DUPLICATE_THRESHOLD", "")  # empty = per-embedder default

# ---------------------------------------------------------------- local LLM (optional)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")

# ---------------------------------------------------------------- fleet
SITES = {
    "pune": "Pune circle",
    "nagpur": "Nagpur circle",
    "global": "All circles (SOPs, safety)",
}
DEVICES = {
    "tablet-A": {"label": "Line crew A · Pune circle", "sites": ["pune", "global"], "port": 8001,
                 "token": "dev-token-A"},
    "tablet-B": {"label": "Line crew B · Nagpur circle", "sites": ["nagpur", "global"], "port": 8002,
                 "token": "dev-token-B"},
}
DEVICE_ID = os.getenv("EDGEMIND_DEVICE", "")  # set by the launcher for a device process

AUTO_SYNC_SECONDS = float(os.getenv("AUTO_SYNC_SECONDS", "4"))
NET_TIMEOUT = float(os.getenv("NET_TIMEOUT", "4"))
DEMAND_WINDOW_SECONDS = 60 * 60  # fleet search misses older than this are ignored


def device_url(dev_id: str) -> str:
    return f"http://{HOST}:{DEVICES[dev_id]['port']}"


def collection(site: str) -> str:
    return f"{COLLECTION_PREFIX}{site}"
