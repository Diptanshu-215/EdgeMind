"""Device-side sync: the only code that talks to the network.

One cycle:
  1. hello   – heartbeat, report failed searches, receive fleet demand + site heads
  2. push    – drain the outbox in priority order through compare-and-set
  3. pull    – per subscribed site: if the server head moved, refresh the mirror shard
               (full snapshot the first time, partial snapshot afterwards; point sync
               when the cloud has no snapshot support)
  4. settle  – purge local copies the mirror now holds, scan new fleet memories for
               contradictions, re-run the policy for personal notes against fleet demand
"""
import time

import httpx

from . import config, embeddings, policy
from .device import EdgeDevice

LOCAL_ONLY_FIELDS = {"sync_state", "visibility", "policy", "edits", "base_version", "contradicts", "layer",
                     "score", "match", "pushed_version"}


class Offline(Exception):
    pass


class SyncClient:
    def __init__(self, dev: EdgeDevice):
        self.dev = dev
        self.http = httpx.Client(base_url=config.GATEWAY_URL, timeout=config.NET_TIMEOUT, trust_env=False,
                                 headers={"X-Device-Token": config.DEVICES[dev.id]["token"]})
        self.connected = False
        self.last_error = None

    # ------------------------------------------------------------------ transport
    def _call(self, method, path, **kw):
        if not self.dev.online:
            raise Offline("device offline")
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            self.connected = False
            raise Offline(f"cloud unreachable ({type(exc).__name__})") from exc
        if r.status_code >= 500:
            self.connected = False
            raise Offline(f"cloud error {r.status_code}")
        r.raise_for_status()
        self.connected = True
        return r

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
        body = {"stats": self.dev.stats(), "misses": [{"query": m["query"], "dense": m["dense"], "top": m["top"]} for m in misses]}
        data = self._call("POST", "/sync/hello", json=body).json()
        self.dev.mark_misses_sent([m["id"] for m in misses])
        old = {(d["device"], d["query"]) for d in self.dev.fleet_demand()}
        self.dev.set_meta("fleet_demand", data["demand"])
        self.dev.set_meta("cloud_snapshots", data["snapshots"])
        mine = embeddings.get().dense.name
        if data.get("embedder") and data["embedder"] != mine and not self.dev.meta("embedder_warned"):
            self.dev.log("EMBEDDER MISMATCH", f"cloud uses {data['embedder']}, this tablet uses {mine}; "
                                              "restart with the same model", "error")
            self.dev.set_meta("embedder_warned", True)
        res["demand_new"] = len({(d["device"], d["query"]) for d in data["demand"]} - old)
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

    def pull(self, heads, snapshots, res):
        for site in self.dev.sites:
            head = heads.get(site, 0)
            have = self.dev.mirror_seq(site)
            has_mirror = self.dev.mirrors.get(site) is not None
            if head <= have and has_mirror:
                continue
            if snapshots:
                if not has_mirror:
                    r = self._call("GET", f"/sites/{site}/snapshot")
                    self.dev.restore_mirror(site, r.content, int(r.headers.get("X-Site-Seq", head)))
                    self._add_bytes("bytes_full_total", len(r.content))
                    res["full"].append({"site": site, "bytes": len(r.content)})
                    self.dev.log("full snapshot", f"{site}: bootstrapped mirror shard from Qdrant Server "
                                                  f"({len(r.content) / 1024:.0f} KB)")
                else:
                    r = self._call("POST", f"/sites/{site}/snapshot/partial", json=self.dev.manifest(site))
                    self.dev.apply_partial(site, r.content, int(r.headers.get("X-Site-Seq", head)))
                    self._add_bytes("bytes_partial_total", len(r.content))
                    full = self.dev.meta(f"full_bytes_{site}", 0)
                    res["partial"].append({"site": site, "bytes": len(r.content), "full_bytes": full})
                    self.dev.log("partial snapshot", f"{site}: {len(r.content) / 1024:.0f} KB of changed segments"
                                                     + (f" (full shard is {full / 1024:.0f} KB)" if full else ""))
                new_docs = self._docs_since(site, have)
            else:
                data = self._call("GET", f"/sites/{site}/changes", params={"since": have}).json()
                self.dev.apply_points(site, data["changes"], data["head"])
                size = len(str(data))
                self._add_bytes("bytes_points_total", size)
                res["points"].append({"site": site, "count": len(data["changes"]), "bytes": size})
                if data["changes"]:
                    self.dev.log("point sync", f"{site}: {len(data['changes'])} changed memories")
                new_docs = [d["payload"] for d in data["changes"]]
            fresh = [d for d in new_docs if d.get("author_device") != self.dev.id]
            res["pulled"] += len(fresh)
            for d in fresh[:5]:
                self.dev.log("received", f"'{d.get('title')}' from {d.get('author_device')} (v{d.get('version')})")
            self.dev.scan_new_fleet_docs(fresh)

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
            res["error"] = "device offline"
            return res
        t0 = time.perf_counter()
        try:
            data = self.hello(res)
            self.push(res)
            self.pull(data["heads"], data["snapshots"], res)
            self.settle(res)
            res["ok"] = True
            self.dev.set_meta("offline_since", None)
            self.last_error = None
        except Offline as exc:
            res["error"] = str(exc)
            if self.last_error != res["error"]:
                self.dev.log("sync paused", res["error"], "warn")
            self.last_error = res["error"]
            if not self.dev.meta("offline_since"):
                self.dev.set_meta("offline_since", time.time())
        except Exception as exc:  # never let a sync bug kill the device
            res["error"] = f"{type(exc).__name__}: {exc}"
            self.dev.log("sync error", res["error"], "error")
        res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        busy = res["pushed"] or res["pulled"] or res["conflicts"] or res["full"] or res["partial"] or res["purged"]
        if res["ok"] and (busy or reason != "auto"):
            nbytes = sum(x["bytes"] for x in res["full"] + res["partial"] + res["points"])
            self.dev.log("sync", f"{reason}: ↑{res['pushed']} ↓{res['pulled']} · conflicts {res['conflicts']} · "
                                 f"{nbytes / 1024:.0f} KB · {res['ms']} ms")
        self.dev.set_meta("last_sync", res)
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
            self.dev.enqueue(doc_id, "upsert", local.get("policy", {}).get("priority", 1))
        self.dev.drop_conflict(doc_id)
        self.dev.log("conflict resolved", f"'{local['title']}' → {strategy}")
        return True
