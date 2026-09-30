"""Remote edge node + cloud reset: a separate `launch.py join` process (a second laptop) with its own data folder
joins over HTTP, works offline, and after a hub reset (new cloud epoch) re-publishes its own knowledge.

The reset test wipes the fleet, so it runs last in the suite.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from helpers import Node, free_port, uid, wait_for

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def remote(hub):
    data = Path(tempfile.mkdtemp(prefix="edgemind-remote-"))
    port = free_port()
    dev_id = f"remote-{uid()}"
    env = dict(os.environ, PYTHONUTF8="1", EDGEMIND_BIND="127.0.0.1", EDGEMIND_PUBLIC_URL=f"http://127.0.0.1:{port}",
               AUTO_SYNC_SECONDS="5", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    for k in ("EDGEMIND_DATA", "GATEWAY_URL", "EDGEMIND_DEVICE", "EDGEMIND_SITES", "EDGEMIND_PORT", "EDGEMIND_JOIN_CODE"):
        env.pop(k, None)
    log = open(data.parent / f"{data.name}.log", "wb")
    proc = subprocess.Popen([sys.executable, "launch.py", "join", "--hub", hub.gateway, "--code", hub.join_code,
                             "--name", "Remote crew", "--sites", "nagpur,global", "--port", str(port),
                             "--data", str(data), "--id", dev_id], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    R = Node(f"http://127.0.0.1:{port}", dev_id)
    try:
        wait_for(lambda: proc.poll() is None and (lambda st: st["live"] and st["connected"])(R.state()),
                 f"the remote node to start, enroll, finish its first sync and go live (log: {log.name})", 240, 1)
        yield {"node": R, "id": dev_id, "data": data, "proc": proc}
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        time.sleep(1)
        shutil.rmtree(data, ignore_errors=True)
        Path(log.name).unlink(missing_ok=True)


def test_a_remote_node_joins_over_http_with_its_own_shards(hub, remote):
    R = remote["node"]
    st = R.state()
    assert st["enrolled"] and st["connected"] and st["id"] == remote["id"]
    assert (remote["data"] / "devices" / remote["id"] / "local").is_dir(), "the remote node has no shard of its own"
    wait_for(lambda: len(R.memory()) >= 8, "the remote node to bootstrap", 60)
    titles = R.titles()
    assert "Bird fault on 11 kV line" in titles and "Feeder P-12 tripping in rain" not in titles
    assert "<title>EdgeMind" in R.get("").text, "the remote node must serve the field app to its own crew"


def test_the_hub_lists_and_proxies_the_remote_node(hub, remote):
    node = wait_for(lambda: next((x for x in hub.config()["nodes"] if x["id"] == remote["id"]), None),
                    "the hub to list the remote node", 30)
    assert node["kind"] == "remote" and node["online"], node
    assert hub.node(remote["id"]).state()["id"] == remote["id"], "the hub does not proxy to the remote node"


def test_memories_flow_both_ways_between_remote_and_hub_nodes(hub, remote, flow):
    RP, B = hub.node(remote["id"]), hub.B
    u = uid()
    with flow.step("a write on the remote node (through the hub) reaches tablet-B live"):
        r_doc = RP.write(f"Swapped a flashed-over disc insulator string {u} at the Hingna cut-point.",
                         f"Remote insulator swap {u}", "fix", site="nagpur")
        B.wait_doc(r_doc["doc_id"], "receives the remote node's write", timeout=20)
    with flow.step("a write on tablet-B reaches the remote node"):
        b_doc = B.write(f"Re-fixed the danger board {u} on the Hingna DT.", f"Danger board {u}", "fix", site="global")
        remote["node"].wait_doc(b_doc["doc_id"], "receives tablet-B's write", timeout=20)


def test_the_remote_node_works_offline_and_catches_up(hub, remote, flow):
    R = remote["node"]
    u = uid()
    with R.offline():
        with flow.step("uplink off: the remote node still searches and saves"):
            assert R.search("bird fault on the 11 kV line")["results"][0]["title"] == "Bird fault on 11 kV line"
            doc = R.write(f"Cleared a bird nest {u} from the 11 kV jumper.", f"Bird nest {u}", "fix", site="nagpur")
            assert doc["sync_state"] == "pending"
    with flow.step("uplink back: tablet-B receives the offline write"):
        hub.B.wait_doc(doc["doc_id"], "receives the remote node's offline write", timeout=30)


def test_after_a_hub_reset_the_remote_node_republishes_its_knowledge(hub, remote, flow):
    """Destructive: wipes the fleet (new cloud epoch). Runs last."""
    R = remote["node"]
    u = uid()
    with flow.step("the remote node writes a memory that only it will still hold after the reset"):
        doc = R.write(f"Replaced the Hingna feeder pillar lock {u}.", f"Feeder pillar lock {u}", "fix", site="nagpur")
        hub.wait_cloud(doc["doc_id"], "remote write uploaded")
    with flow.step("the operator resets the hub (the fleet data is wiped, new epoch)"):
        hub.reset()
        wait_for(lambda: hub.config()["phase"] == "ready", "the hub to be ready after the reset", 600, 2)
    with flow.step("the remote node notices the new epoch and re-publishes what it wrote"):
        hub.wait_cloud(doc["doc_id"], "the remote node to re-publish its memory", timeout=180)
        assert any(e["event"] == "cloud epoch changed" for e in R.log(200))
    with flow.step("the fresh tablet-B bootstraps and gets the remote node's memory back"):
        wait_for(lambda: hub.B.doc(doc["doc_id"]), "tablet-B to receive the re-published memory", 240, 1)
