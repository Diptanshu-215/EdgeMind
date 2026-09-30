"""Node-side sync: the only code that talks to the network.

One cycle:
  1. hello   – heartbeat, report failed searches, receive fleet demand, site heads and the
               cloud epoch (a new epoch means the fleet was reset: rehydrate)
  2. push    – drain the outbox in priority order through compare-and-set
  3. pull    – per subscribed site whose head moved, pick the cheapest transport:
                 no mirror yet      -> FULL snapshot (gzip stream) restored into a Qdrant Edge shard
                 a few changes      -> POINT delta upserted into the mirror (sub-second, a few KB)
                 many changes       -> PARTIAL snapshot: only the segments that changed
               and every few minutes a partial snapshot reconciles the mirror with the server
               exactly (point deltas give liveness, snapshots give consistency)
  4. settle  – purge local copies the mirror now holds, scan new fleet memories for
               contradictions, re-run the policy for personal notes against fleet demand

A background listener holds the gateway's event stream: when any node writes to a site this
node follows, a sync starts within ~200 ms. The timer is only a fallback.
"""
import ipaddress
import json
import os
import socket
import threading
import time
import uuid
import zlib

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import httpx

from . import config, embeddings, policy
from .device import EdgeDevice

LOCAL_ONLY_FIELDS = {"sync_state", "visibility", "policy", "edits", "base_version", "contradicts", "layer",
                     "score", "match", "pushed_version"}
POINT_DELTA_MAX = 64          # up to this many changes: point delta; above: partial snapshot
RECONCILE_SECONDS = float(os.getenv("EDGEMIND_RECONCILE_SECONDS", "180"))  # after point deltas, reconcile when idle this long


class Offline(Exception):
    pass


_DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dns")


class SyncClient:
    def __init__(self, dev: EdgeDevice, public_url: str = ""):
        self.dev = dev
        self.public_url = public_url
        self.http = httpx.Client(base_url=config.GATEWAY_URL, trust_env=False,
                                 timeout=httpx.Timeout(config.NET_TIMEOUT, read=config.NET_TIMEOUT))
        self.connected = False
        self.live = False
        self.poll_mode = False  # the live channel is buffered by a proxy: sync on a short timer instead
        self.last_error = None
        self._stream = None
        self._stop = threading.Event()
        self._enroll_lock = threading.Lock()
        self._dns = (0.0, True)
        if config.JOIN_CODE:
            dev.set_meta("join_code", config.JOIN_CODE)

    # ------------------------------------------------------------------ enrollment
    @property
    def token(self):
        return self.dev.meta("token")

    def ensure_token(self):
        """The sync worker and the live listener share one enrollment."""
        with self._enroll_lock:
            if not self.token:
                self.enroll()
            return self.token

    def forget_token(self, used):
        with self._enroll_lock:
            if self.token == used:
                self.dev.set_meta("token", None)

    def enroll(self):
        code = self.dev.meta("join_code") or config.JOIN_CODE
        if not code:
            raise Offline("not enrolled: no fleet join code")
        try:
            r = self.http.post("/enroll", json={"join_code": code, "device_id": self.dev.id, "label": self.dev.label,
                                                "sites": self.dev.sites, "url": self.public_url})
        except httpx.HTTPError as exc:
            raise Offline(f"cloud unreachable ({type(exc).__name__})") from exc
        if r.status_code == 403:
            self.dev.log("enroll refused", "the hub rejected the join code", "error")
            raise Offline("join code rejected by the hub")
        r.raise_for_status()
        data = r.json()
        self.dev.set_meta("token", data["token"])
        if data["sites"] != self.dev.sites:
            self.dev.log("sites", f"hub enrolled this node for {', '.join(data['sites'])}")
        self.dev.log("enrolled", f"joined the fleet at {config.GATEWAY_URL} as {self.dev.id}")
        return data

    # ------------------------------------------------------------------ transport
    def _reachable_name(self):
        """Name lookups ignore HTTP timeouts and can hang for a minute on a broken network (captive
        Wi-Fi, a container cut off its network). Resolve the gateway with a 2 s limit instead."""
        host = urlparse(config.GATEWAY_URL).hostname or ""
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        if host == "localhost":
            return True
        at, ok = self._dns
        if time.time() - at < 10:
            return ok
        try:
            _DNS_POOL.submit(socket.getaddrinfo, host, None).result(timeout=2)
            ok = True
        except Exception:
            ok = False
        self._dns = (time.time(), ok)
        return ok

    def _request(self, method, path, stream=False, timeout=None, **kw):
        if not self.dev.online:
            raise Offline("uplink off")
        if not self._reachable_name():
            self.connected = False
            raise Offline("cloud unreachable (gateway name does not resolve)")
        for attempt in (0, 1):
            token = self.ensure_token()
            try:
                if timeout is not None:  # NB: timeout=None would DISABLE timeouts in httpx, not use the default
                    kw["timeout"] = timeout
                req = self.http.build_request(method, path, headers={"X-Device-Token": token}, **kw)
                r = self.http.send(req, stream=stream)
            except httpx.HTTPError as exc:
                self.connected = False
                raise Offline(f"cloud unreachable ({type(exc).__name__})") from exc
            if r.status_code == 401 and attempt == 0:  # token revoked or hub reset: enroll again
                r.close()
                self.forget_token(token)
                continue
            if r.status_code >= 500:
                r.close()
                self.connected = False
                raise Offline(f"cloud error {r.status_code}")
            if r.status_code >= 400:
                detail = r.read().decode(errors="replace")[:200]
                r.close()
                raise RuntimeError(f"{method} {path}: {r.status_code} {detail}")
            self.connected = True
            return r
        raise Offline("not authorised by the hub")

    def _call(self, method, path, **kw):
        r = self._request(method, path, **kw)
        r.read()
        return r

    def _download(self, method, path, dest, **kw):
        """Stream a gzip snapshot to disk. Returns (wire_bytes, raw_bytes, head_seq)."""
        r = self._request(method, path, stream=True, timeout=httpx.Timeout(config.NET_TIMEOUT, read=120), **kw)
        if r.status_code == 204:  # nothing changed on the server
            r.close()
            return 0, 0, int(r.headers.get("X-Site-Seq", 0))
        layers = [zlib.decompressobj(31) for _ in range(int(r.headers.get("X-Gzip-Layers", 0)))]

        def unwrap(data, final=False):
            for gz in layers:
                data = gz.decompress(data) + (gz.flush() if final else b"")
            return data

        raw = 0
        try:
            with open(dest, "wb") as f:
                for chunk in r.iter_raw(1 << 20):
                    data = unwrap(chunk)
                    raw += len(data)
                    f.write(data)
                tail = unwrap(b"", final=True)
                raw += len(tail)
                f.write(tail)
            if any(not gz.eof for gz in layers) or raw == 0:
                raise Offline("snapshot download incomplete")  # never apply a truncated snapshot
        except (httpx.HTTPError, zlib.error) as exc:
            self.connected = False
            dest.unlink(missing_ok=True)
            raise Offline(f"download interrupted ({type(exc).__name__})") from exc
        except Offline:
            dest.unlink(missing_ok=True)
            raise
        finally:
            r.close()
        return r.num_bytes_downloaded, raw, int(r.headers.get("X-Site-Seq", 0))

    # ------------------------------------------------------------------ payloads
    def _cloud_payload(self, doc):
        redacted = doc.get("policy", {}).get("decision") == "sync_redacted"
        p = {k: v for k, v in doc.items() if k not in LOCAL_ONLY_FIELDS and not k.startswith("_")}
        p.update(redacted=redacted, deleted=bool(doc.get("deleted")))
        if redacted:
            p["text"], p["title"] = policy.redact(doc["text"]), policy.redact(doc["title"])
        if doc.get("deleted"):
            p["text"] = ""
        return p

    # ------------------------------------------------------------------ steps
    def hello(self, res):
        misses = self.dev.unsent_misses()
        body = {"stats": self.dev.stats(), "url": self.public_url, "label": self.dev.label,
                "misses": [{"query": m["query"], "dense": m["dense"], "top": m["top"]} for m in misses]}
        t0 = time.time()
        data = self._call("POST", "/sync/hello", json=body).json()
        self.dev.set_meta("clock_offset", data["server_time"] - (t0 + time.time()) / 2)
        self.dev.mark_misses_sent([m["id"] for m in misses])
        known = self.dev.meta("epoch")
        if known and known != data["epoch"]:
            res["rehydrated"] = self.dev.rehydrate(data["epoch"])
        elif not known:
            self.dev.set_meta("epoch", data["epoch"])
        old = {(d["device"], d["query"]) for d in self.dev.fleet_demand()}
        new = {(d["device"], d["query"]) for d in data["demand"]}
        if new != old:
            self.dev.set_meta("fleet_demand", data["demand"])
        self.dev.set_meta("cloud_snapshots", data["snapshots"])
        mine = embeddings.get().dense.name
        if data.get("embedder") and data["embedder"] != mine and not self.dev.meta("embedder_warned"):
            self.dev.log("EMBEDDER MISMATCH", f"cloud uses {data['embedder']}, this node uses {mine}; "
                                              "restart with the same model", "error")
            self.dev.set_meta("embedder_warned", True)
        res["demand_new"] = len(new - old)
        res["misses_sent"] = len(misses)
        return data

    def push(self, res):
        items, docs = [], {}
        for ob in self.dev.outbox():
            doc = self.dev.get_local(ob["doc_id"], with_vector=True)
            if doc is None:
                self.dev.dequeue(ob["doc_id"])
                continue
            if ob["op"] == "retract":
                doc = dict(doc, deleted=True)
            docs[doc["doc_id"]] = (doc, ob)
            items.append({"doc_id": doc["doc_id"], "site": doc["site"], "base_version": doc.get("base_version", 0),
                          "payload": self._cloud_payload(doc), "dense": doc["_dense"],
                          "image": None if doc.get("deleted") else doc.get("_image"),
                          "text": "" if doc.get("deleted") else f"{doc['title']} {doc['text']}"})
        if not items:
            return
        t0 = time.time()
        results = self._call("POST", "/push", json={"items": items}).json()["results"]
        for r in results:
            doc, ob = docs[r["doc_id"]]
            doc.pop("_dense", None)
            if r["status"] == "accepted":
                self._accepted(doc, ob, r, res)
            elif r["status"] == "conflict":
                self._conflict(doc, ob, r["current"], res)
            else:
                self.dev.log("push rejected", f"'{doc['title']}': {r.get('error')}", "error")
                self.dev.dequeue(doc["doc_id"])
        if self.dev.meta("offline_since"):
            self.dev.set_meta("last_drain_s", round(time.time() - t0, 2))

    def _accepted(self, doc, ob, r, res):
        self.dev.dequeue(doc["doc_id"])
        res["pushed"] += 1
        if ob["op"] == "retract":
            doc.update(base_version=0, version=0, deleted=False, sync_state="local_only")
            self.dev.put_local(doc)
            self.dev.log("retracted", f"'{doc['title']}' withdrawn from the fleet; device-only now")
            return
        if ob["op"] == "delete":
            doc.update(version=r["version"], base_version=r["version"], pushed_version=r["version"], sync_state="synced")
            self.dev.put_local(doc)  # tombstone kept until the mirror shows the delete
            self.dev.log("deleted from fleet", f"'{doc['title']}'")
            return
        redacted = doc.get("policy", {}).get("decision") == "sync_redacted"
        doc.update(version=r["version"], base_version=r["version"], pushed_version=r["version"],
                   sync_state="redacted_synced" if redacted else "synced")
        if r.get("similar_to"):
            doc["similar_to"] = r["similar_to"]
            res["duplicates"] += 1
            self.dev.log("possible duplicate", f"'{doc['title']}' ≈ '{r['similar_to']['title']}' "
                                               f"(similarity {r['similar_to']['score']}); linked, not merged")
        self.dev.put_local(doc)
        tag = " (redacted copy)" if redacted else ""
        note = " — restored an item someone deleted" if r.get("resurrected") else ""
        self.dev.log("pushed", f"'{doc['title']}' → fleet v{r['version']}{tag}{note}")

    def _conflict(self, doc, ob, cur, res):
        self.dev.dequeue(doc["doc_id"])
        if ob["op"] == "delete":  # we deleted, someone edited: their edit wins
            self.dev.remove_local(doc["doc_id"])
            self.dev.log("delete skipped", f"'{doc['title']}' was updated by {cur.get('author_device')}; keeping their edit")
            return
        if doc.get("kind") == "reading":  # sensor-style data: newest reading wins
            if doc["updated_at"] >= cur.get("updated_at", 0):
                doc.update(base_version=cur["version"], sync_state="pending")
                self.dev.put_local(doc)
                self.dev.enqueue(doc["doc_id"], "upsert", ob["priority"])
                self.dev.log("conflict auto-resolved", f"'{doc['title']}': your reading is newer, re-sending")
            else:
                self.dev.remove_local(doc["doc_id"])
                self.dev.log("conflict auto-resolved", f"'{doc['title']}': newer reading from {cur.get('author_device')} wins")
            return
        doc["sync_state"] = "conflict"
        self.dev.put_local(doc)
        self.dev.add_conflict(doc, cur)
        res["conflicts"] += 1
        self.dev.log("CONFLICT", f"'{doc['title']}' was changed by {cur.get('author_device')} while you edited it", "warn")

    def _full(self, site, res):
        snap = self.dev.dir / "tmp" / f"{site}-full-{uuid.uuid4().hex[:6]}.snapshot"
        wire, raw, seq = self._download("GET", f"/sites/{site}/snapshot", snap)
        self.dev.restore_mirror(site, snap, seq, wire)
        self._add_bytes("bytes_full_total", wire)
        res["full"].append({"site": site, "bytes": wire, "raw": raw})
        self.dev.set_meta(f"reconciled_{site}", time.time())
        self.dev.set_meta(f"point_deltas_{site}", 0)
        self.dev.log("full snapshot", f"{site}: bootstrapped mirror shard from Qdrant Server "
                                      f"({wire / 1024:.0f} KB on the wire, {raw / 1048576:.0f} MB shard)")

    def _partial(self, site, res, why):
        snap = self.dev.dir / "tmp" / f"{site}-part-{uuid.uuid4().hex[:6]}.snapshot"
        wire, raw, seq = self._download("POST", f"/sites/{site}/snapshot/partial", snap, json=self.dev.manifest(site))
        if raw == 0:  # 204: the mirror already matches the server exactly
            self.dev.set_meta(f"mirror_seq_{site}", max(seq, self.dev.mirror_seq(site)))
            self.dev.set_meta(f"reconciled_{site}", time.time())
            self.dev.set_meta(f"point_deltas_{site}", 0)
            return
        try:
            self.dev.apply_partial(site, snap, seq)
        except Exception as exc:  # a mirror that can't take the partial is rebuilt, never left stuck
            self.dev.log("mirror rebuild", f"{site}: partial snapshot did not apply ({exc}); restoring a full snapshot", "warn")
            snap.unlink(missing_ok=True)
            self._full(site, res)
            return
        self._add_bytes("bytes_partial_total", wire)
        full = self.dev.meta(f"full_bytes_{site}", 0)
        res["partial"].append({"site": site, "bytes": wire, "raw": raw, "full_bytes": full})
        self.dev.set_meta(f"reconciled_{site}", time.time())
        self.dev.set_meta(f"point_deltas_{site}", 0)
        self.dev.log("partial snapshot", f"{site}: {why} · {wire / 1024:.0f} KB of changed segments"
                                         + (f" (full shard {full / 1024:.0f} KB)" if full else ""))

    def _points(self, site, have, res):
        data = self._call("GET", f"/sites/{site}/changes", params={"since": have}).json()
        self.dev.apply_points(site, data["changes"], data["head"])
        size = len(json.dumps(data["changes"]))
        self._add_bytes("bytes_points_total", size)
        res["points"].append({"site": site, "count": len(data["changes"]), "bytes": size})
        self.dev.set_meta(f"point_deltas_{site}", self.dev.meta(f"point_deltas_{site}", 0) + 1)
        if data["changes"]:
            self.dev.log("point delta", f"{site}: {len(data['changes'])} changed memories, {size / 1024:.1f} KB")
        return [d["payload"] for d in data["changes"]]

    def pull(self, heads, snapshots, res):
        for site in self.dev.sites:
            head = heads.get(site, 0)
            have = self.dev.mirror_seq(site)
            has_mirror = self.dev.mirrors.get(site) is not None
            stale = (snapshots and has_mirror and self.dev.meta(f"point_deltas_{site}", 0)
                     and time.time() - self.dev.meta(f"reconciled_{site}", 0) > RECONCILE_SECONDS)
            if head <= have and has_mirror and not stale:
                continue
            bootstrap = not has_mirror
            if not snapshots:
                new_docs = self._points(site, have, res)
            elif not has_mirror:
                self._full(site, res)
                new_docs = self._docs_since(site, have)
            elif 0 < head - have <= POINT_DELTA_MAX:  # new changes always take the fast path; reconcile only when idle
                new_docs = self._points(site, have, res)
            else:
                why = "reconcile after point deltas" if head <= have else f"{head - have} changes"
                self._partial(site, res, why)
                new_docs = self._docs_since(site, have)
            fresh = [d for d in new_docs if d.get("author_device") != self.dev.id]
            res["pulled"] += len(fresh)
            if not bootstrap:
                self._propagation(fresh)
            for d in fresh:
                self.dev.record_history(d, "deleted by " + d.get("author_device", "?") if d.get("deleted")
                                        else f"received from {d.get('author_device')}")
            for d in fresh[:5]:
                self.dev.log("received", f"'{d.get('title')}' from {d.get('author_device')} (v{d.get('version')})")
            self.dev.scan_new_fleet_docs(fresh)

    def _propagation(self, docs):
        """Write on node X -> searchable on this node: measured against the server clock."""
        now = time.time() + self.dev.meta("clock_offset", 0)
        lat = [now - d["synced_at"] for d in docs if d.get("synced_at") and now - d["synced_at"] < 30]
        if lat:
            ms = round(max(lat) * 1000)
            hist = (self.dev.meta("propagation_ms", []) + [ms])[-50:]
            self.dev.set_meta("propagation_ms", hist)

    def _docs_since(self, site, seq):
        from qdrant_edge import FieldCondition, Filter, RangeFloat, ScrollRequest
        shard = self.dev.mirrors.get(site)
        if shard is None:
            return []
        with self.dev.lock:
            recs, _ = shard.scroll(ScrollRequest(limit=200, with_payload=True,
                                                 filter=Filter(must=[FieldCondition(key="seq", range=RangeFloat(gt=float(seq)))])))
        return [dict(r.payload) for r in recs]

    def _add_bytes(self, key, n):
        self.dev.set_meta(key, self.dev.meta(key, 0) + n)

    def settle(self, res):
        """Purge local copies that the mirror now holds (Qdrant's dual-write pattern)."""
        purged = 0
        for d in self.dev.local_docs():
            if d.get("sync_state") != "synced" or not d.get("pushed_version"):
                continue
            m = self.dev.get_mirror(d["doc_id"], site=d.get("site"))
            if m and m.get("version", 0) >= d["pushed_version"]:
                self.dev.remove_local(d["doc_id"])
                purged += 1
        res["purged"] = purged
        res["suggested"] = self.dev.reevaluate_personal()

    # ------------------------------------------------------------------ public
    def sync(self, reason="auto"):
        res = {"reason": reason, "at": time.time(), "ok": False, "pushed": 0, "pulled": 0, "conflicts": 0,
               "duplicates": 0, "full": [], "partial": [], "points": [], "purged": 0}
        if not self.dev.online:
            res["error"] = "uplink off"
            return res
        was_ok = (self.dev.meta("last_sync") or {}).get("ok")
        t0 = time.perf_counter()
        try:
            data = self.hello(res)
            self.push(res)
            self.pull(data["heads"], data["snapshots"], res)
            self.settle(res)
            res["ok"] = True
            self.dev.set_meta("offline_since", None)
            if self.last_error:
                self.dev.log("sync resumed", "cloud reachable again")
            self.last_error = None
        except Offline as exc:
            res["error"] = str(exc)
            self.drop_stream()  # the live channel is dead too: show it now, not after a 30 s read timeout
            if self.last_error != res["error"]:
                self.dev.log("sync paused", res["error"], "warn")
            self.last_error = res["error"]
            if not self.dev.meta("offline_since"):
                self.dev.set_meta("offline_since", time.time())
        except Exception as exc:  # never let a sync bug kill the node
            res["error"] = f"{type(exc).__name__}: {exc}"
            self.dev.log("sync error", res["error"], "error")
        res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        busy = res["pushed"] or res["pulled"] or res["conflicts"] or res["full"] or res["partial"] or res["purged"]
        if res["ok"] and (busy or reason not in ("auto", "live")):
            nbytes = sum(x["bytes"] for x in res["full"] + res["partial"] + res["points"])
            self.dev.log("sync", f"{reason}: ↑{res['pushed']} ↓{res['pulled']} · conflicts {res['conflicts']} · "
                                 f"{nbytes / 1024:.1f} KB · {res['ms']} ms")
        self.dev.set_meta("last_sync", res)
        if busy or was_ok != res["ok"]:
            self.dev.bump()
        return res

    def resolve(self, doc_id, strategy):
        conflict = next((c for c in self.dev.conflicts() if c["doc_id"] == doc_id), None)
        if not conflict:
            return False
        local = self.dev.get_local(doc_id) or conflict["local"]
        cloud = conflict["cloud"]
        if strategy == "theirs":
            self.dev.remove_local(doc_id)  # the mirror copy (theirs) becomes visible again
        else:
            if strategy == "merge":
                local["text"] = (f"{local['text']}\n\n— merged with the version from {cloud.get('author_device')} —\n"
                                 f"{cloud.get('text', '')}")
            local.update(base_version=cloud["version"], version=cloud["version"], sync_state="pending",
                         updated_at=time.time(), author_device=self.dev.id)
            self.dev.put_local(local)
            self.dev.record_history(local, f"conflict resolved on this node: {strategy}")
            self.dev.enqueue(doc_id, "upsert", local.get("policy", {}).get("priority", 1))
        self.dev.drop_conflict(doc_id)
        self.dev.log("conflict resolved", f"'{local['title']}' → {strategy}")
        return True

    # ------------------------------------------------------------------ live channel
    def listen(self, trigger, enabled):
        """Hold the gateway event stream; call trigger() when this node has something to fetch.
        Runs in its own thread for the life of the process."""
        backoff = 1.0
        last_demand = None
        while not self._stop.is_set():
            if not (self.dev.online and enabled()):
                self.live = False
                self._stop.wait(0.5)
                continue
            got_event = None
            try:
                if not self._reachable_name():
                    raise Offline("gateway name does not resolve")
                token = self.ensure_token()
                with self.http.stream("GET", "/sync/events", headers={"X-Device-Token": token},
                                      timeout=httpx.Timeout(config.NET_TIMEOUT, read=30)) as r:
                    if r.status_code == 401:
                        self.forget_token(token)
                        raise Offline("token rejected")
                    r.raise_for_status()
                    self._stream = r
                    # The gateway sends a first event immediately. If it doesn't arrive, a proxy (e.g. a
                    # Cloudflare tunnel over HTTP/2) is holding the stream: drop it and poll instead.
                    got_event = threading.Event()
                    watchdog = threading.Timer(8, lambda: None if got_event.is_set() else r.close())
                    watchdog.start()
                    backoff = 1.0
                    for line in r.iter_lines():
                        if not got_event.is_set() and line.startswith("data:"):
                            got_event.set()
                            watchdog.cancel()
                            if self.poll_mode:
                                self.dev.log("live channel", "server push works again: leaving poll mode")
                            self.poll_mode, self.live = False, True
                            self.dev.bump()
                        if self._stop.is_set() or not (self.dev.online and enabled()):
                            break
                        if not line.startswith("data:"):
                            continue
                        ev = json.loads(line[5:])
                        behind = any(h > self.dev.mirror_seq(s) for s, h in ev["heads"].items())
                        demand = ev["demand_v"] != last_demand and last_demand is not None
                        last_demand = ev["demand_v"]
                        if behind or demand or ev["epoch"] != self.dev.meta("epoch"):
                            trigger("live")
            except Exception:
                pass
            finally:
                self._stream = None
                if self.live:
                    self.live = False
                    self.dev.bump()
            if got_event is not None and not got_event.is_set() and self.dev.online and self.connected:
                if not self.poll_mode:
                    self.poll_mode = True
                    self.dev.log("live channel", "server push is buffered by a proxy: syncing every 3 s instead")
                    self.dev.bump()
                self._stop.wait(60)  # try server push again later
                continue
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 8.0)

    def drop_stream(self):
        """Uplink switched off: close the live channel now so the fleet sees this node go dark."""
        s = self._stream
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        self.drop_stream()
