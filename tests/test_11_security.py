"""Security: enrollment with the join code, per-device tokens, site access control, admin-only cloud reads,
token revocation, operator PIN for destructive hub actions, input validation.
"""
import socket

import httpx
import pytest

from app import config
from helpers import uid

DENSE = [0.1] * 384


@pytest.fixture(scope="module")
def probe(hub):
    """A throwaway device enrolled for the nagpur circle only, straight against the gateway."""
    dev_id = f"probe-{uid()}"
    r = hub.gw("POST", "/enroll", json={"join_code": hub.join_code, "device_id": dev_id, "label": "security probe",
                                        "sites": ["nagpur", "global"]})
    assert r.status_code == 200, r.text
    return {"id": dev_id, "h": {"X-Device-Token": r.json()["token"]}}


def test_cloud_reads_need_the_admin_token(hub):
    for method, path in (("GET", "/memory"), ("GET", "/fleet"), ("GET", "/log"), ("POST", "/search")):
        r = hub.gw(method, path, json={"q": "fuse"} if method == "POST" else None)
        assert r.status_code == 401, f"{method} {path} without a token -> {r.status_code}"
    assert hub.gw("GET", "/memory", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert hub.gw("GET", "/memory", headers=hub.admin).status_code == 200


def test_node_endpoints_need_a_device_token(hub):
    assert hub.gw("POST", "/sync/hello", json={}).status_code == 401
    assert hub.gw("GET", "/sites/pune/changes").status_code == 401
    assert hub.gw("GET", "/sites/pune/changes", headers={"X-Device-Token": "forged"}).status_code == 401


def test_a_wrong_join_code_is_refused(hub):
    r = hub.gw("POST", "/enroll", json={"join_code": "EM-0000", "device_id": f"intruder-{uid()}", "sites": ["pune"]})
    assert r.status_code == 403, r.text
    r = hub.gw("POST", "/enroll", json={"join_code": hub.join_code, "device_id": "bad id!", "sites": ["pune"]})
    assert r.status_code == 422, "malformed device ids must be rejected"


def test_a_device_only_reads_the_sites_it_enrolled_for(hub, probe):
    assert hub.gw("GET", "/sites/nagpur/changes", headers=probe["h"]).status_code == 200
    assert hub.gw("GET", "/sites/pune/changes", headers=probe["h"]).status_code == 403
    assert hub.gw("GET", "/sites/mars/changes", headers=probe["h"]).status_code == 404
    if hub.snapshots:
        assert hub.gw("GET", "/sites/pune/snapshot", headers=probe["h"]).status_code == 403


def test_a_device_cannot_write_to_a_site_it_did_not_enroll_for(hub, probe):
    r = hub.gw("POST", "/push", headers=probe["h"], json={"items": [{
        "doc_id": "00000000-0000-0000-0000-000000000001", "site": "pune", "base_version": 0,
        "payload": {"title": "x", "text": "x"}, "dense": DENSE}]})
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "rejected", r.text


def test_push_payloads_are_validated(hub, probe):
    r = hub.gw("POST", "/push", headers=probe["h"], json={"items": [{
        "doc_id": "00000000-0000-0000-0000-000000000002", "site": "nagpur", "base_version": 0,
        "payload": {"title": "x", "text": "x"}, "dense": [0.1] * 5}]})
    assert r.status_code == 422, "a vector of the wrong size must be rejected"


def test_a_revoked_device_token_stops_working(hub, probe):
    assert hub.gw("DELETE", f"/admin/devices/{probe['id']}", headers=hub.admin).status_code == 200
    assert hub.gw("GET", "/sites/nagpur/changes", headers=probe["h"]).status_code == 401
    assert any(d["id"] == probe["id"] and d["revoked"] for d in hub.cloud_fleet()["devices"])


def test_destructive_hub_actions_need_the_operator_pin(hub):
    for path in ("api/cloud-stop", "api/cloud-start", "api/reset"):
        hub.post(path, expect=(403,))
        hub.post(path, headers={"X-Operator-Pin": "000000"}, expect=(403,))
    hub.post("api/nodes", json={"label": "x", "sites": ["pune"]}, expect=(403,))
    assert hub.config()["procs"]["gateway"] is True, "a refused cloud-stop must not stop the cloud"


def test_operator_login_and_what_non_operators_can_see(hub):
    with httpx.Client(base_url=hub.base, trust_env=False, timeout=30) as visitor:  # own cookie jar
        public = visitor.get("/api/config").json()
        assert public["operator"] is False and public["join_code"] is None and public["join_command"] is None, \
            "a visitor without the PIN must not see the fleet join code"
        assert visitor.post("/api/login", json={"pin": "000000"}).status_code == 403
        r = visitor.post("/api/login", json={"pin": hub.pin})
        assert r.status_code == 200 and "em_pin" in visitor.cookies
        op = visitor.get("/api/config").json()
        assert op["operator"] is True and op["join_code"] == hub.join_code, "the login cookie was not honoured"


def test_qdrant_server_is_not_reachable_from_the_network(hub):
    ip = config.lan_ip()
    if ip.startswith("127."):
        pytest.skip("this machine has no LAN address to probe from")
    port = int(hub.config()["qdrant"].rsplit(":", 1)[1]) if hub.snapshots else None
    if port is None:
        pytest.skip("embedded mode: no Qdrant Server")
    with socket.socket() as s:
        s.settimeout(1)
        assert s.connect_ex((ip, port)) != 0, f"Qdrant Server answers on {ip}:{port}; it must listen on loopback only"
