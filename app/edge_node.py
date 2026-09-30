"""Run THIS machine as an edge node of a hub on the network (a crew's van box / tablet).

    python launch.py join --hub http://192.168.1.20:8100 --code EM-1234 --name "Line crew C" --sites pune,global

The node keeps its own Qdrant Edge shards on this machine, serves the field app to crew
phones on this machine's network, and syncs with the hub whenever it can reach it. Pull the
Wi-Fi: it keeps answering. Plug it back: it catches up in seconds.
"""
import argparse
import json
import os
import re
import secrets
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv):
    ap = argparse.ArgumentParser(prog="launch.py join", description="Join a hub as an edge node")
    ap.add_argument("--hub", required=True, help="the hub's gateway URL, e.g. http://192.168.1.20:8100")
    ap.add_argument("--code", required=True, help="fleet join code shown on the hub")
    ap.add_argument("--name", default=f"Crew on {socket.gethostname()}")
    ap.add_argument("--sites", default="global", help="comma separated: pune, nagpur, global")
    ap.add_argument("--port", type=int, default=8001, help="port for this node's field app")
    ap.add_argument("--data", default=str(ROOT / "data"), help="where this node keeps its shards")
    ap.add_argument("--id", default="", help="fixed node id (default: derived from --name)")
    ap.add_argument("--demo-notes", action="store_true", help="add the demo's private notes on first start")
    args = ap.parse_args(argv)

    hub = args.hub.rstrip("/")
    if not hub.startswith("http"):
        hub = "http://" + hub
    if hub.startswith("http://") and not re.search(r":\d+$", hub):
        hub += ":8100"  # a bare LAN address; an https:// URL (tunnel, cloud) keeps its own port
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    ident_file = data / "node-identity.json"
    ident = json.loads(ident_file.read_text()) if ident_file.exists() else {}
    if args.id:
        ident = {"id": args.id}
        ident_file.write_text(json.dumps(ident))
    if not ident.get("id"):
        slug = re.sub(r"[^a-z0-9]+", "-", args.name.lower()).strip("-")[:24] or "node"
        ident = {"id": f"{slug}-{secrets.token_hex(2)}"}
        ident_file.write_text(json.dumps(ident))

    os.environ.update({
        "EDGEMIND_DATA": str(data), "GATEWAY_URL": hub, "EDGEMIND_DEVICE": ident["id"], "EDGEMIND_LABEL": args.name,
        "EDGEMIND_SITES": args.sites, "EDGEMIND_JOIN_CODE": args.code, "PYTHONUTF8": "1",
    })
    if args.demo_notes:
        os.environ["EDGEMIND_DEMO_NOTES"] = "1"

    from . import config  # noqa: E402  (reads the environment set above)

    port = args.port
    while _busy(port):
        port += 1
    ip = config.lan_ip()
    os.environ["EDGEMIND_PORT"] = str(port)
    public = os.getenv("EDGEMIND_PUBLIC_URL") or f"http://{ip}:{port}"  # Docker sets http://<service>:<port>
    os.environ["EDGEMIND_PUBLIC_URL"] = public
    config.DEVICE_PORT, config.DEVICE_PUBLIC_URL = port, public

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import httpx
    try:
        h = httpx.get(f"{hub}/health", timeout=4, trust_env=False).json()
        print(f"[edgemind] hub reachable: {h.get('mode')}")
    except Exception as exc:  # offline-first: start anyway
        print(f"[edgemind] hub not reachable yet ({type(exc).__name__}). The node starts anyway and syncs "
              f"when it can reach {hub}. Same Wi-Fi? Firewall on the hub allows Python?")

    from . import embeddings
    print("[edgemind] loading the embedding model (first run downloads ~700 MB)...", flush=True)
    print(f"[edgemind] embedder: {embeddings.get().dense.name}")
    url = f"http://{ip}:{port}"
    print(f"\n  Edge node '{args.name}' ({ident['id']}) · sites {args.sites}\n"
          f"  Field app for crew phones: {url}\n  Syncing with hub: {hub}\n"
          f"  Windows asks once to allow Python on the network: choose Private networks -> Allow.\n", flush=True)
    try:
        import segno
        segno.make(url, error="m").terminal(compact=True)
    except Exception:
        pass

    import uvicorn
    from .device_api import app
    uvicorn.run(app, host=config.BIND_HOST, port=port, log_level="warning", timeout_graceful_shutdown=2)


def _busy(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False
