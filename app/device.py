"""One edge device (a lineman's tablet).

Storage follows Qdrant's recommended edge layout:
  local/            MUTABLE Qdrant Edge shard: everything written on this device
                    (device-only notes, queued edits, tombstones, redacted originals)
  mirrors/<site>/   one Qdrant Edge shard per subscribed site: a read-only copy of the
                    fleet knowledge, restored from a Qdrant Server snapshot and kept
                    fresh with PARTIAL snapshots (or point sync when no server)
  state.sqlite      outbox, conflicts, contradictions, search log, fleet demand, activity

Search runs on all shards and merges by doc_id; a local copy always wins over the
mirror (your unsynced edit hides the older fleet version). Once the server confirms an
upload and the mirror catches up, the local copy is purged: no double storage.
"""
import json
import math
import re
import shutil
import sqlite3
import statistics
import threading
import time
import uuid

import base64
import io

from qdrant_edge import (
    CountRequest, DecayKind, Distance, EdgeConfig, EdgeShard, EdgeSparseVectorParams, EdgeVectorParams, Expression,
    FacetRequest, FieldCondition, Filter, Formula, Fusion, MatchValue, Mmr, Modifier, PayloadSchemaType, Point,
    Prefetch, Query, QueryRequest, ScrollRequest, UpdateOperation,
)

from . import config, embeddings, facts, policy

# keyword flags (see config.with_flags): bool matching is broken in qdrant-edge-py 0.8
NOT_DELETED = Filter(must_not=[FieldCondition(key="status", match=MatchValue(value="deleted"))])
HAS_PHOTO = FieldCondition(key="photo", match=MatchValue(value="yes"))
OWN_STATES = ("local_only", "suggested", "pending", "conflict", "synced", "redacted_synced")
TRANSIENT = {"layer", "score", "match", "similarity", "found_on", "why"}
INDEXES = (("kind", PayloadSchemaType.Keyword), ("site", PayloadSchemaType.Keyword),
           ("updated_at", PayloadSchemaType.Float), ("status", PayloadSchemaType.Keyword),
           ("photo", PayloadSchemaType.Keyword))
RECENCY_SCALE = 90 * 86400   # a memory 90 days older than another ranks ~10% lower at equal relevance
PHOTO_MIN = 0.26             # CLIP text->photo: matches ~0.30+, look-alikes ~0.25, unrelated <0.20


class _Rows:
    def __init__(self, rows, rowcount):
        self.rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class LockedDB:
    """One SQLite connection shared by the API threads, the sync worker, the live listener and
    the event streams. Python's sqlite3 connection must not be used by two threads at once."""

    def __init__(self, path):
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._l = threading.RLock()

    def execute(self, sql, params=()):
        with self._l:
            cur = self._c.execute(sql, params)
            return _Rows(cur.fetchall(), cur.rowcount)

    def executemany(self, sql, seq):
        with self._l:
            self._c.executemany(sql, seq)

    def executescript(self, sql):
        with self._l:
            self._c.executescript(sql)

    def commit(self):
        with self._l:
            self._c.commit()

    def close(self):
        with self._l:
            self._c.close()


def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0
    return num / den


_WORD = re.compile(r"[a-z0-9]{3,}")
_STOPWORDS = {"the", "and", "for", "what", "how", "with", "must", "should", "before", "after", "does", "any", "can",
              "this", "that", "there", "from", "when", "where", "which", "have", "has", "not", "you", "your", "our"}


def _stems(text):
    out = set()
    for w in _WORD.findall(text.lower()):
        if w in _STOPWORDS:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > 5 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def keyword_overlap(query, doc):
    return len(_stems(query) & _stems(f"{doc.get('title', '')} {doc.get('text', '')}"))


def shard_config():
    return EdgeConfig(
        vectors={"dense": EdgeVectorParams(size=config.DENSE_DIM, distance=Distance.Cosine),
                 "image": EdgeVectorParams(size=config.IMAGE_DIM, distance=Distance.Cosine)},
        sparse_vectors={"bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
    )


VECS = ["dense", "image"]


def _with_vectors(rec, with_vector):
    doc = dict(rec.payload)
    if with_vector:
        doc["_dense"] = rec.vector["dense"]
        if rec.vector.get("image"):
            doc["_image"] = rec.vector["image"]
    return doc


def recency_formula():
    """Qdrant formula rescoring: relevance x (0.8 + 0.2 x exp-decay of age). Fresh fixes win ties."""
    E = Expression
    decay = E.Decay(DecayKind.Exp, E.Variable("updated_at"), E.Constant(time.time()), 0.5, RECENCY_SCALE)
    return Formula(E.Mult([E.Variable("$score"), E.Sum([E.Constant(0.8), E.Mult([E.Constant(0.2), decay])])]),
                   defaults={"updated_at": 0.0})


def make_thumb(img, size=320, quality=72):
    from PIL import Image  # noqa: F401
    t = img.copy()
    t.thumbnail((size, size))
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def decode_photo(data_url):
    """Validate an uploaded photo (data URL or base64) and return a normalised RGB image."""
    from PIL import Image, ImageOps
    raw = base64.b64decode(data_url.split(",", 1)[-1], validate=False)
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((1280, 1280))
    return img


QUIET_META = {"last_sync", "cloud_snapshots", "clock_offset", "bytes_partial_total", "bytes_full_total",
              "bytes_points_total"}


class EdgeDevice:
    def __init__(self, dev_id: str, label: str = "", sites=None):
        self.id, self.label = dev_id, label or dev_id
        self.sites = [s for s in (sites or ["global"]) if s in config.SITES]
        self.lock = threading.RLock()
        self.version = 0  # bumped on every visible change; drives the live event stream
        self.dir = config.DATA_DIR / "devices" / dev_id
        marker = self.dir / "schema.txt"
        stamp = f"{config.SCHEMA}:{embeddings.get().dense.name}"
        if self.dir.exists() and any(self.dir.iterdir()) and (not marker.exists() or marker.read_text() != stamp):
            # shards from an older layout or another embedding model: keep them aside, never delete a crew's notes
            self.dir.rename(self.dir.with_name(f"{dev_id}.old-{int(time.time())}"))
        (self.dir / "mirrors").mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.dir / "tmp", ignore_errors=True)  # leftovers of interrupted downloads
        (self.dir / "tmp").mkdir(exist_ok=True)
        (self.dir / "photos").mkdir(exist_ok=True)
        marker.write_text(stamp)
        self.db = LockedDB(self.dir / "state.sqlite")
        self._init_db()
        self.local = self._open(self.dir / "local")
        self.mirrors = {s: self._load_if_exists(self.mirror_path(s)) for s in self.sites}

    # ================================================================ storage setup
    @staticmethod
    def _open(path):
        if path.exists() and any(path.iterdir()):
            return EdgeShard.load(str(path))
        path.mkdir(parents=True, exist_ok=True)
        shard = EdgeShard.create(str(path), shard_config())
        for field, schema in INDEXES:  # payload indexes: fast filters + native facets
            shard.update(UpdateOperation.create_field_index(field, schema))
        return shard

    @staticmethod
    def _load_if_exists(path):
        return EdgeShard.load(str(path)) if path.exists() and any(path.iterdir()) else None

    def _init_db(self):
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS outbox(doc_id TEXT PRIMARY KEY, op TEXT, priority INT, queued_at REAL,
                    attempts INT DEFAULT 0, last_error TEXT);
                CREATE TABLE IF NOT EXISTS conflicts(doc_id TEXT PRIMARY KEY, local_json TEXT, cloud_json TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS contradictions(doc_id TEXT, other_id TEXT, detail TEXT, ts REAL,
                    PRIMARY KEY(doc_id, other_id));
                CREATE TABLE IF NOT EXISTS access(doc_id TEXT PRIMARY KEY, n INT DEFAULT 0, last REAL);
                CREATE TABLE IF NOT EXISTS searches(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, q TEXT,
                    ms_total REAL, ms_shards REAL, top REAL, hits INT);
                CREATE TABLE IF NOT EXISTS misses(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, q TEXT, dense TEXT,
                    top REAL, sent INT DEFAULT 0);
                CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, event TEXT,
                    detail TEXT);
                CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, ts REAL,
                    version INT, author TEXT, event TEXT, title TEXT, text TEXT);
                CREATE INDEX IF NOT EXISTS history_doc ON history(doc_id, ts);
                """
            )
            self.db.commit()

    def close(self):
        with self.lock:
            for sh in [self.local, *[m for m in self.mirrors.values() if m]]:
                try:
                    sh.flush()
                    sh.close()
                except Exception:
                    pass
            self.db.close()

    # ================================================================ meta / log
    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))
            self.db.commit()
            if key not in QUIET_META:
                self.version += 1

    def bump(self):
        with self.lock:
            self.version += 1

    @property
    def online(self):
        """The node's uplink (the crew van's 4G). Crew phones on the node's local Wi-Fi keep working either way."""
        return self.meta("online", True)

    def log(self, event, detail="", level="info"):
        with self.lock:
            self.db.execute("INSERT INTO log(ts,level,event,detail) VALUES(?,?,?,?)", (time.time(), level, event, detail))
            self.db.commit()
            self.version += 1

    def recent_log(self, limit=60, after_id=0):
        rows = self.db.execute("SELECT id,ts,level,event,detail FROM log WHERE id>? ORDER BY id DESC LIMIT ?",
                               (after_id, limit)).fetchall()
        return [{"id": r[0], "ts": r[1], "level": r[2], "event": r[3], "detail": r[4], "device": self.id} for r in rows]

    def last_log_id(self):
        return self.db.execute("SELECT COALESCE(MAX(id),0) FROM log").fetchone()[0]

    # ================================================================ mirrors (fleet copies)
    def mirror_seq(self, site):
        return self.meta(f"mirror_seq_{site}", 0)

    def mirror_path(self, site):
        return self.dir / "mirrors" / self.meta(f"mirror_dir_{site}", site)

    def _new_mirror_dir(self, site):
        name = f"{site}-{int(time.time() * 1000)}"
        return name, self.dir / "mirrors" / name

    @staticmethod
    def _cleanup(*paths):
        # Windows may keep memory-mapped files open briefly; leftovers are harmless
        for p in paths:
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
            except OSError:
                pass

    def restore_mirror(self, site, snap, seq: int, wire_bytes: int):
        """Bootstrap a site mirror from a FULL server snapshot (unpacked into a fresh folder, then swapped in)."""
        name, target = self._new_mirror_dir(site)
        EdgeShard.unpack_snapshot(str(snap), str(target))
        with self.lock:
            old, old_path = self.mirrors.get(site), self.mirror_path(site)
            self.mirrors[site] = EdgeShard.load(str(target))
            self.set_meta(f"mirror_dir_{site}", name)
            self.set_meta(f"mirror_seq_{site}", seq)
            self.set_meta(f"full_bytes_{site}", wire_bytes)
            if old:
                old.close()
        self._cleanup(snap)
        if old_path != target:
            self._cleanup(old_path)

    def manifest(self, site):
        with self.lock:
            return self.mirrors[site].snapshot_manifest()

    def apply_partial(self, site, snap, seq: int):
        """Refresh a mirror with a PARTIAL snapshot: only segments that changed on the server."""
        tmp = self.dir / "tmp" / f"unpack-{uuid.uuid4().hex[:6]}"
        tmp.mkdir()
        with self.lock:
            self.mirrors[site].update_from_snapshot(str(snap), tmp_dir=str(tmp))
            self.set_meta(f"mirror_seq_{site}", seq)
        self._cleanup(snap, tmp)

    def apply_points(self, site, docs, head):
        """Point-sync fallback (no Qdrant Server): upsert changed points into the mirror."""
        emb = embeddings.get()
        with self.lock:
            if self.mirrors.get(site) is None:
                name, path = self._new_mirror_dir(site)
                self.mirrors[site] = self._open(path)
                self.set_meta(f"mirror_dir_{site}", name)
            pts = []
            for d in docs:
                p = d["payload"]
                text = "" if p.get("deleted") else f"{p.get('title', '')} {p.get('text', '')}"
                vec = {"dense": d["dense"]}
                if text:
                    vec["bm25"] = emb.sparse_doc(text)
                if d.get("image"):
                    vec["image"] = d["image"]
                pts.append(Point(p["doc_id"], vec, p))
            if pts:
                self.mirrors[site].update(UpdateOperation.upsert_points(pts))
            self.set_meta(f"mirror_seq_{site}", max(self.mirror_seq(site), head,
                                                    max((d["payload"]["seq"] for d in docs), default=0)))

    # ================================================================ point access
    def _shards(self):
        return [("local", None, self.local)] + [("mirror", s, m) for s, m in self.mirrors.items() if m]

    def get_local(self, doc_id, with_vector=False):
        with self.lock:
            recs = self.local.retrieve([doc_id], with_payload=True, with_vector=VECS if with_vector else False)
        if not recs:
            return None
        return _with_vectors(recs[0], with_vector)

    def get_mirror(self, doc_id, site=None, with_vector=False):
        with self.lock:
            for s, m in self.mirrors.items():
                if m is None or (site and s != site):
                    continue
                recs = m.retrieve([doc_id], with_payload=True, with_vector=VECS if with_vector else False)
                if recs:
                    return _with_vectors(recs[0], with_vector)
        return None

    def get(self, doc_id, with_vector=False):
        return self.get_local(doc_id, with_vector) or self.get_mirror(doc_id, with_vector=with_vector)

    def put_local(self, doc, dense=None):
        emb = embeddings.get()
        if dense is None:
            dense = emb.embed_dense(f"{doc.get('title', '')}. {doc['text']}")
        payload = config.with_flags({k: v for k, v in doc.items() if not k.startswith("_") and k not in TRANSIENT})
        vec = {"dense": dense}
        if doc.get("text"):
            vec["bm25"] = emb.sparse_doc(f"{doc.get('title', '')} {doc['text']}")
        if doc.get("_image"):
            vec["image"] = doc["_image"]
        with self.lock:
            self.local.update(UpdateOperation.upsert_points([Point(doc["doc_id"], vec, payload)]))
            self.version += 1
        return dense

    def remove_local(self, doc_id):
        with self.lock:
            self.local.update(UpdateOperation.delete_points([doc_id]))
            self.db.execute("DELETE FROM outbox WHERE doc_id=?", (doc_id,))
            self.db.commit()
            self.version += 1

    def enqueue(self, doc_id, op, priority):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO outbox(doc_id,op,priority,queued_at,attempts) VALUES(?,?,?,?,0)",
                            (doc_id, op, priority, time.time()))
            self.db.commit()
            self.version += 1

    def dequeue(self, doc_id):
        with self.lock:
            self.db.execute("DELETE FROM outbox WHERE doc_id=?", (doc_id,))
            self.db.commit()
            self.version += 1

    # ================================================================ cloud epoch
    def rehydrate(self, epoch):
        """The fleet was reset or restored (new cloud epoch): drop the mirrors, keep everything this
        node wrote, and queue it again so the fleet gets it back. Edge nodes re-seed the cloud."""
        emb = embeddings.get()
        with self.lock:
            own = {}
            for s, m in self.mirrors.items():
                if m is None:
                    continue
                offset = None
                while True:
                    recs, offset = m.scroll(ScrollRequest(offset=offset, limit=256, with_payload=True,
                                                          with_vector=VECS))
                    for r in recs:
                        p = _with_vectors(r, True)
                        if p.get("author_device") == self.id and not p.get("deleted"):
                            own[p["doc_id"]] = (p, p.pop("_dense"))
                    if offset is None:
                        break
            old_paths = [self.mirror_path(s) for s in self.sites]
            for m in self.mirrors.values():
                if m:
                    m.close()
            self.mirrors = {s: None for s in self.sites}
            for s in self.sites:
                self.db.execute("DELETE FROM meta WHERE key IN (?,?,?)",
                                (f"mirror_seq_{s}", f"mirror_dir_{s}", f"full_bytes_{s}"))
            self.db.execute("DELETE FROM conflicts")
            self.db.execute("DELETE FROM outbox")
            self.db.commit()
        for p in self.local_docs():
            if p.get("deleted"):
                self.remove_local(p["doc_id"])
            elif p.get("sync_state") not in ("local_only", "suggested"):
                full = self.get_local(p["doc_id"], with_vector=True)
                own[p["doc_id"]] = (full, full.pop("_dense"))
        for doc_id, (p, dense) in own.items():
            p = {k: v for k, v in p.items() if k not in ("pushed_version", "seq", "synced_at", "similar_to")}
            p.update(version=0, base_version=0, sync_state="pending")
            p.setdefault("policy", {"decision": "sync", "priority": 2, "reasons": ["re-published after a cloud reset"]})
            self.put_local(p, dense)
            self.enqueue(doc_id, "upsert", p["policy"].get("priority", 2))
        self._cleanup(*old_paths)
        self.set_meta("epoch", epoch)
        self.log("cloud epoch changed", f"fleet data was reset; rebuilding mirrors and re-publishing "
                                        f"{len(own)} memories written on this node", "warn")
        return len(own)

    def outbox(self):
        rows = self.db.execute("SELECT doc_id,op,priority,queued_at,attempts,last_error FROM outbox "
                               "ORDER BY priority, queued_at").fetchall()
        return [dict(zip(("doc_id", "op", "priority", "queued_at", "attempts", "last_error"), r)) for r in rows]

    def _scroll(self, shard):
        out, offset = [], None
        while True:
            recs, offset = shard.scroll(ScrollRequest(offset=offset, limit=256, with_payload=True))
            out.extend(dict(r.payload) for r in recs)
            if offset is None:
                return out

    def local_docs(self):
        with self.lock:
            return self._scroll(self.local)

    def merged_docs(self):
        """What the technician sees: fleet mirrors overlaid with this device's own copies."""
        merged = {}
        with self.lock:
            for s, m in self.mirrors.items():
                if m is None:
                    continue
                for d in self._scroll(m):
                    d.update(sync_state="synced", layer="mirror")
                    merged[d["doc_id"]] = d
            for d in self._scroll(self.local):
                d["layer"] = "local"
                merged[d["doc_id"]] = d
        docs = [d for d in merged.values() if not d.get("deleted")]
        docs.sort(key=lambda d: d.get("updated_at", 0), reverse=True)
        return docs

    # ================================================================ fleet demand
    def fleet_demand(self):
        return self.meta("fleet_demand", [])

    def best_demand(self, dense):
        best = None
        for d in self.fleet_demand():
            score = cosine(dense, d["dense"])
            if best is None or score > best["score"]:
                best = {"score": round(score, 3), "device": d["device"], "query": d["query"]}
        return best

    def access_count(self, doc_id):
        row = self.db.execute("SELECT n FROM access WHERE doc_id=?", (doc_id,)).fetchone()
        return row[0] if row else 0

    # ================================================================ user operations
    def write(self, text, title="", kind="note", site=None, visibility="auto", doc_id=None, photo=None):
        """Create or edit a memory. Identical online and offline. `photo`: a data URL from the camera."""
        now = time.time()
        local = self.get_local(doc_id, with_vector=True) if doc_id else None
        existing = local or (self.get_mirror(doc_id, with_vector=True) if doc_id else None)
        if existing is None:
            base = 0
        elif local is None or local.get("sync_state") in ("synced", "redacted_synced"):
            base = existing.get("version", 0)
        else:
            base = local.get("base_version", 0)
        # a memory already in the cloud keeps its site; a device-only one may still move
        if existing and base > 0:
            site = existing["site"]
        elif site not in self.sites:
            site = existing["site"] if existing else self.sites[0]

        emb = embeddings.get()
        if not text.strip() and photo:
            text = title.strip() or "Photo evidence"
        title = title.strip() or text.strip()[:60]
        dense = emb.embed_dense(f"{title}. {text.strip()}")
        doc_id = doc_id or str(uuid.uuid4())
        image, thumb = (existing or {}).get("_image"), (existing or {}).get("thumb")
        if photo:
            image, thumb = self.save_photo(doc_id, photo)
        demand = self.best_demand(dense)
        decision = policy.decide(f"{title} {text}", kind, visibility, demand, self.access_count(doc_id) if doc_id else 0,
                                 emb.demand_threshold)
        doc = {
            "doc_id": doc_id, "title": title, "text": text.strip(), "kind": kind, "site": site,
            "visibility": visibility, "policy": decision,
            "origin_device": existing.get("origin_device", self.id) if existing else self.id,
            "author_device": self.id, "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now, "edits": (existing.get("edits", 0) + 1) if existing else 0,
            "version": base, "base_version": base, "deleted": False,
        }
        if thumb:
            doc.update(has_photo=True, thumb=thumb,
                       photo_by=self.id if photo else (existing or {}).get("photo_by", self.id))
        if image:
            doc["_image"] = image
        if decision["decision"] in ("local", "suggest"):
            doc["sync_state"] = "local_only" if decision["decision"] == "local" else "suggested"
            self.dequeue(doc["doc_id"])
            if base > 0:  # it is in the cloud: withdraw it
                self.enqueue(doc["doc_id"], "retract", 0)
                self.log("retract queued", f"'{title}' is now private; it will be withdrawn from the fleet")
        else:
            doc["sync_state"] = "pending"
            self.enqueue(doc["doc_id"], "upsert", decision["priority"])

        doc["contradicts"] = self.check_contradictions(doc, dense)
        self.put_local(doc, dense)
        self.record_history(doc, "edited on this node" if existing else "created on this node")
        self.log("memory edited" if existing else "memory saved",
                 f"'{title}' → {decision['decision']} · " + "; ".join(decision["reasons"])
                 + (" · photo embedded on the node" if photo else ""))
        doc.pop("_image", None)
        return doc

    # ================================================================ memory history
    def record_history(self, doc, event):
        """Every version a node sees of a memory: created, edited here, received from crew X, merged."""
        with self.lock:
            last = self.db.execute("SELECT version, text, title FROM history WHERE doc_id=? ORDER BY id DESC LIMIT 1",
                                   (doc["doc_id"],)).fetchone()
            if last and last[1] == doc.get("text") and last[2] == doc.get("title") and not event.startswith("deleted"):
                return
            self.db.execute("INSERT INTO history(doc_id,ts,version,author,event,title,text) VALUES(?,?,?,?,?,?,?)",
                            (doc["doc_id"], doc.get("updated_at") or time.time(), doc.get("version", 0),
                             doc.get("author_device", self.id), event, doc.get("title", ""), doc.get("text", "")))
            self.db.commit()

    def history(self, doc_id):
        rows = self.db.execute("SELECT ts,version,author,event,title,text FROM history WHERE doc_id=? ORDER BY id",
                               (doc_id,)).fetchall()
        return [dict(zip(("ts", "version", "author", "event", "title", "text"), r)) for r in rows]

    # ================================================================ photos
    def save_photo(self, doc_id, data_url):
        """Full photo stays on this node; the fleet gets a small thumbnail and a CLIP vector."""
        img = decode_photo(data_url)
        path = self.dir / "photos" / f"{doc_id}.jpg"
        img.save(path, "JPEG", quality=85)
        vision = embeddings.vision()
        image = vision.embed_image(path) if vision.available() else None
        return image, make_thumb(img)

    def photo_path(self, doc_id):
        p = self.dir / "photos" / f"{doc_id}.jpg"
        return p if p.exists() else None

    def photo_count(self):
        flt = Filter(must=[HAS_PHOTO], must_not=NOT_DELETED.must_not)
        with self.lock:
            return sum(sh.count(CountRequest(exact=True, filter=flt)) for _, _, sh in self._shards())

    def search_photo(self, data_url, limit=6):
        """Photo -> photo: 'have we seen this before?' Runs on the node's shards, offline."""
        t0 = time.perf_counter()
        img = decode_photo(data_url)
        tmp = self.dir / "tmp" / f"q-{uuid.uuid4().hex[:6]}.jpg"
        img.save(tmp, "JPEG", quality=85)
        try:
            qv = embeddings.vision().embed_image(tmp)
        finally:
            self._cleanup(tmp)
        req = QueryRequest(limit=limit, query=Query.Nearest(qv, using="image"), filter=NOT_DELETED, with_payload=True)
        best = {}
        with self.lock:
            for layer, _, shard in self._shards():
                for r in shard.query(req):
                    d = dict(r.payload, score=round(r.score, 3), match=round(r.score, 3), layer=layer,
                             why={"photo": round(r.score, 3)})
                    if layer == "mirror":
                        d["sync_state"] = "synced"
                    if d["doc_id"] not in best or layer == "local":
                        best[d["doc_id"]] = d
        results = sorted(best.values(), key=lambda d: -d["score"])[:limit]
        return {"latency_ms": round((time.perf_counter() - t0) * 1000, 1), "results": results, "mode": "photo"}

    def delete(self, doc_id):
        local = self.get_local(doc_id)
        doc = local or self.get_mirror(doc_id)
        if not doc:
            return False
        if local and local.get("sync_state") not in ("synced", "redacted_synced"):
            base = local.get("base_version", 0)
        else:
            base = doc.get("version", 0)
        if base > 0 or self.get_mirror(doc_id) is not None:
            tomb = dict(doc, deleted=True, sync_state="pending", updated_at=time.time(), author_device=self.id,
                        base_version=base, version=base)
            self.put_local(tomb)
            self.record_history(tomb, "deleted on this node")
            self.enqueue(doc_id, "delete", 1)
            self.log("delete queued", f"'{doc['title']}' will be removed from the fleet on next sync")
        else:
            self.remove_local(doc_id)
            self.log("memory deleted", f"'{doc['title']}' (was device-only)")
        with self.lock:
            self.db.execute("DELETE FROM contradictions WHERE doc_id=? OR other_id=?", (doc_id, doc_id))
            self.db.commit()
        return True

    def share(self, doc_id):
        doc = self.get_local(doc_id)
        if not doc:
            return None
        # shared because another crew asked for it: publish fleet-wide so they receive it
        site = "global" if "global" in self.sites else doc["site"]
        return self.write(doc["text"], doc["title"], doc["kind"], site, "shared", doc_id)

    # ================================================================ search
    def search(self, query, limit=6, kind=None, mode="hybrid", log_miss=True, site=None, diverse=False):
        """Hybrid retrieval on the node's Qdrant Edge shards, fully offline:
            meaning (dense) + keywords (BM25) [+ photos (CLIP text->image) when the node has photos]
            -> RRF fusion -> recency formula (or MMR diversity for answers).
        Every result carries `why`: its meaning / keyword / photo score, so the ranking is explainable."""
        emb = embeddings.get()
        t0 = time.perf_counter()
        qd = emb.embed_query(query)
        qs = emb.sparse_query(query)
        must = [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in (("kind", kind), ("site", site)) if v]
        flt = Filter(must=must or None, must_not=NOT_DELETED.must_not)
        dense_q, sparse_q = Query.Nearest(qd, using="dense"), Query.Nearest(qs, using="bm25")
        legs = {"meaning": (dense_q, 30, None), "keywords": (sparse_q, 30, None)}
        vision = embeddings.vision()
        if mode in ("hybrid", "photo") and vision.available() and self.photo_count():
            legs["photo"] = (Query.Nearest(vision.embed_text(query), using="image"), 10, PHOTO_MIN)
        if mode == "dense":
            req = QueryRequest(limit=limit * 2, query=dense_q, filter=flt, with_payload=True, with_vector=["dense"])
        elif mode == "keyword":
            req = QueryRequest(limit=limit * 2, query=sparse_q, filter=flt, with_payload=True, with_vector=["dense"])
        elif mode == "photo" and "photo" in legs:
            req = QueryRequest(limit=limit * 2, query=legs["photo"][0], filter=flt, score_threshold=PHOTO_MIN,
                               with_payload=True, with_vector=["dense"])
        else:
            fused = Prefetch(limit=40, query=Fusion.Rrf(k=60),
                             prefetches=[Prefetch(limit=n, query=q, filter=flt, score_threshold=th)
                                         for q, n, th in legs.values()])
            final = Mmr(qd, 0.7, 30, using="dense") if diverse else recency_formula()
            req = QueryRequest(limit=limit * 2, prefetches=[fused], query=final, with_payload=True, with_vector=["dense"])
        t1 = time.perf_counter()
        best = {}
        with self.lock:
            for layer, _site, shard in self._shards():
                for r in shard.query(req):
                    d = dict(r.payload)
                    d.update(score=round(r.score, 4), match=round(cosine(qd, r.vector["dense"]), 3), layer=layer)
                    if layer == "mirror":
                        d["sync_state"] = "synced"
                    prev = best.get(d["doc_id"])
                    if prev is None or (layer == "local" and prev["layer"] == "mirror"):
                        best[d["doc_id"]] = d
            # a local tombstone hides the fleet copy
            for doc_id in list(best):
                loc = self.local.retrieve([doc_id], with_payload=True, with_vector=False)
                if loc and loc[0].payload.get("deleted"):
                    best.pop(doc_id)
            ms_shards = (time.perf_counter() - t1) * 1000
            # Each leg across ALL shards (sub-ms queries). Used twice:
            #  1. explainability: every result shows its meaning / keyword / photo score
            #  2. correct cross-shard ranking: RRF is rank-based, so a shard's own fusion scores are not
            #     comparable between a 3-memory local shard and a 5,000-memory mirror. Candidates come
            #     from Qdrant's per-shard fusion; the final order is RRF over each leg's GLOBAL rank,
            #     times the same recency decay (what a distributed Qdrant does across shards).
            leg_scores = {leg: {} for leg in legs}
            for leg, (q, n, th) in legs.items():
                for _, _, shard in self._shards():
                    for r in shard.query(QueryRequest(limit=n, query=q, filter=flt, with_payload=False,
                                                      score_threshold=th)):
                        rid = str(r.id)
                        if r.score > leg_scores[leg].get(rid, float("-inf")):
                            leg_scores[leg][rid] = r.score
            fused_mode = mode == "hybrid" or (mode == "photo" and "photo" not in legs)
            rrf = {}
            for leg, sc in leg_scores.items():
                for rank, rid in enumerate(sorted(sc, key=sc.get, reverse=True), 1):
                    rrf[rid] = rrf.get(rid, 0.0) + 1.0 / (60 + rank)
            now = time.time()
            for doc_id, d in best.items():
                d["why"] = {leg: round(sc[doc_id], 3) for leg, sc in leg_scores.items() if doc_id in sc}
                if fused_mode:
                    age = max(0.0, now - float(d.get("updated_at") or 0))
                    d["score"] = round(rrf.get(doc_id, 0.0) * (0.8 + 0.2 * 0.5 ** (age / RECENCY_SCALE)), 5)
        results = sorted(best.values(), key=lambda d: d["score"], reverse=True)[:limit]
        ms_total = (time.perf_counter() - t0) * 1000
        top = max((r["match"] for r in results), default=0.0)
        with self.lock:
            now = time.time()
            for r in results[:3]:
                self.db.execute("INSERT INTO access(doc_id,n,last) VALUES(?,1,?) ON CONFLICT(doc_id) DO "
                                "UPDATE SET n=n+1, last=?", (r["doc_id"], now, now))
            self.db.execute("INSERT INTO searches(ts,q,ms_total,ms_shards,top,hits) VALUES(?,?,?,?,?,?)",
                            (now, query, ms_total, ms_shards, top, len(results)))
            self.db.commit()
            self.version += 1
        overlap = keyword_overlap(query, results[0]) if results else 0
        photo_hit = any(r.get("why", {}).get("photo", 0) >= PHOTO_MIN + 0.03 for r in results)
        miss = top < emb.miss_threshold and overlap < 2 and not photo_hit
        if miss and log_miss:
            self.record_miss(query, qd, top)
        return {"latency_ms": round(ms_total, 2), "shard_ms": round(ms_shards, 2), "results": results,
                "top_match": top, "miss": miss, "keyword_overlap": overlap, "shards": 1 + sum(1 for m in self.mirrors.values() if m)}

    def record_miss(self, query, dense=None, top=0.0):
        dense = dense or embeddings.get().embed_query(query)
        with self.lock:
            recent = self.db.execute("SELECT 1 FROM misses WHERE q=? AND ts>?", (query, time.time() - 300)).fetchone()
            if recent:
                return
            self.db.execute("INSERT INTO misses(ts,q,dense,top) VALUES(?,?,?,?)", (time.time(), query, json.dumps(dense), top))
            self.db.commit()
        self.log("asked the fleet", f"nothing good on this device for '{query}' (best match {top:.2f}); will tell the fleet")

    def unsent_misses(self):
        rows = self.db.execute("SELECT id,q,dense,top FROM misses WHERE sent=0").fetchall()
        return [{"id": r[0], "query": r[1], "dense": json.loads(r[2]), "top": r[3]} for r in rows]

    def mark_misses_sent(self, ids):
        with self.lock:
            self.db.executemany("UPDATE misses SET sent=1 WHERE id=?", [(i,) for i in ids])
            self.db.commit()

    # ================================================================ contradictions
    def check_contradictions(self, doc, dense):
        if doc.get("kind") in facts.SKIP_KINDS or not facts.quantities(doc["text"]):
            return []
        cands = []
        with self.lock:
            for layer, site, shard in self._shards():
                for r in shard.query(QueryRequest(limit=5, query=Query.Nearest(dense, using="dense"), filter=NOT_DELETED,
                                                  with_payload=True)):
                    if r.payload["doc_id"] != doc["doc_id"] and r.score >= 0.3:
                        cands.append(dict(r.payload, similarity=r.score))
        found = facts.contradictions(doc, cands)
        with self.lock:
            self.db.execute("DELETE FROM contradictions WHERE doc_id=?", (doc["doc_id"],))
            for f in found:
                self.db.execute("INSERT OR REPLACE INTO contradictions VALUES(?,?,?,?)",
                                (doc["doc_id"], f["doc_id"], json.dumps(f), time.time()))
                self.log("CONTRADICTION", f"'{doc['title']}' says {f['mine']} but '{f['title']}' says {f['theirs']}", "warn")
            self.db.commit()
        return found

    def scan_new_fleet_docs(self, docs):
        """After a sync, check newly arrived fleet memories against what this device knows."""
        for d in docs[:50]:
            if d.get("deleted") or not facts.quantities(d.get("text", "")):
                continue
            full = self.get_mirror(d["doc_id"], with_vector=True)
            if full:
                self.check_contradictions(full, full["_dense"])

    def contradictions(self):
        rows = self.db.execute("SELECT doc_id,other_id,detail,ts FROM contradictions ORDER BY ts DESC").fetchall()
        out = []
        for doc_id, other_id, detail, ts in rows:
            me, other = self.get(doc_id), self.get(other_id)
            if not me or not other or me.get("deleted") or other.get("deleted"):
                continue
            out.append({"doc_id": doc_id, "other_id": other_id, "detail": json.loads(detail), "ts": ts,
                        "doc": {"title": me["title"], "text": me["text"], "author": me.get("author_device")},
                        "other": {"title": other["title"], "text": other["text"], "author": other.get("author_device")}})
        return out

    def dismiss_contradiction(self, doc_id, other_id):
        with self.lock:
            self.db.execute("DELETE FROM contradictions WHERE doc_id=? AND other_id=?", (doc_id, other_id))
            self.db.commit()

    # ================================================================ conflicts
    def add_conflict(self, local, cloud):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO conflicts VALUES(?,?,?,?)",
                            (local["doc_id"], json.dumps(local), json.dumps(cloud), time.time()))
            self.db.commit()

    def conflicts(self):
        rows = self.db.execute("SELECT doc_id,local_json,cloud_json,ts FROM conflicts ORDER BY ts").fetchall()
        return [{"doc_id": r[0], "local": json.loads(r[1]), "cloud": json.loads(r[2]), "ts": r[3]} for r in rows]

    def drop_conflict(self, doc_id):
        with self.lock:
            self.db.execute("DELETE FROM conflicts WHERE doc_id=?", (doc_id,))
            self.db.commit()

    # ================================================================ re-evaluation with fleet demand
    def reevaluate_personal(self):
        """Fleet demand changed: re-run the policy for device-only notes (they may now be worth sharing)."""
        changed = []
        for d in self.local_docs():
            if d.get("deleted") or d.get("sync_state") not in ("local_only", "suggested") or d.get("visibility") == "private":
                continue
            full = self.get_local(d["doc_id"], with_vector=True)
            demand = self.best_demand(full["_dense"])
            dec = policy.decide(f"{d['title']} {d['text']}", d["kind"], d.get("visibility", "auto"), demand,
                                self.access_count(d["doc_id"]), embeddings.get().demand_threshold)
            new_state = {"local": "local_only", "suggest": "suggested"}.get(dec["decision"])
            if new_state and new_state != d["sync_state"]:
                d.update(sync_state=new_state, policy=dec)
                d.pop("layer", None)
                self.put_local(d, full["_dense"])
                changed.append(d["title"])
                if new_state == "suggested":
                    self.log("share suggested", f"'{d['title']}' matches what {demand['device']} is looking for: "
                                                f"“{demand['query']}”")
        return changed

    def suggestions(self):
        return [d for d in self.local_docs() if d.get("sync_state") == "suggested" and not d.get("deleted")]

    # ================================================================ stats / metrics
    def stats(self):
        own = [d for d in self.local_docs() if not d.get("deleted")]
        by_state = {}
        for d in own:
            by_state[d.get("sync_state", "?")] = by_state.get(d.get("sync_state", "?"), 0) + 1
        with self.lock:
            mirror_counts = {s: (m.count(CountRequest(exact=True, filter=NOT_DELETED)) if m else 0)
                             for s, m in self.mirrors.items()}
        private = by_state.get("local_only", 0) + by_state.get("suggested", 0) + by_state.get("redacted_synced", 0)
        return {
            "kinds": self.facets("kind"), "photos": self.photo_count(),
            "memories": len(self.merged_docs()), "own": len(own), "by_state": by_state, "mirrors": mirror_counts,
            "outbox": len(self.outbox()), "conflicts": len(self.conflicts()),
            "contradictions": len(self.contradictions()), "suggestions": by_state.get("suggested", 0),
            "private_pct": round(100 * private / len(own)) if own else 0,
        }

    def scale_test(self, n=20000, queries=100):
        """Live proof of scale: a throwaway Qdrant Edge shard with `n` memories, queried with the
        SAME hybrid pipeline (dense + BM25 -> RRF -> recency formula) the node uses. Vectors are
        real embeddings of field phrases, perturbed (embedding 20k texts would take minutes)."""
        import random
        import numpy as np
        from .seed import FLEET
        rnd = random.Random(7)
        emb = embeddings.get()
        phrases = [f"{t}. {x}" for _, _, t, x in FLEET]
        base = np.array(emb.embed_many(phrases), dtype=np.float32)
        words = " ".join(phrases).lower().split()
        path = self.dir / "tmp" / f"scale-{uuid.uuid4().hex[:6]}"
        path.mkdir(parents=True)
        shard = EdgeShard.create(str(path), shard_config())
        try:
            t0 = time.perf_counter()
            now = time.time()
            for k in range(0, n, 1000):
                m = min(1000, n - k)
                v = base[np.array([rnd.randrange(len(base)) for _ in range(m)])] + \
                    np.random.default_rng(k).normal(0, 0.035, (m, base.shape[1])).astype(np.float32)
                v /= np.linalg.norm(v, axis=1, keepdims=True)
                pts = []
                for j in range(m):
                    text = " ".join(rnd.choice(words) for _ in range(24))
                    pts.append(Point(str(uuid.uuid4()), {"dense": v[j].tolist(), "bm25": emb.sparse_doc(text)},
                                     {"kind": "fix", "status": "active", "updated_at": now - rnd.random() * 3e7}))
                shard.update(UpdateOperation.upsert_points(pts))
            load_s = time.perf_counter() - t0
            t1 = time.perf_counter()
            shard.optimize()  # build the HNSW index, as Qdrant does in the background on a real shard
            index_s = time.perf_counter() - t1
            qs = [rnd.choice(phrases)[:60] for _ in range(queries)]
            qv = [(emb.embed_query(q), emb.sparse_query(q)) for q in qs]
            lat = []
            for d, sp in qv:
                req = QueryRequest(limit=6, prefetches=[Prefetch(limit=40, query=Fusion.Rrf(k=60), prefetches=[
                    Prefetch(limit=30, query=Query.Nearest(d, using="dense"), filter=NOT_DELETED),
                    Prefetch(limit=30, query=Query.Nearest(sp, using="bm25"), filter=NOT_DELETED)])],
                    query=recency_formula(), with_payload=False)
                t = time.perf_counter()
                shard.query(req)
                lat.append((time.perf_counter() - t) * 1000)
            lat.sort()
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            out = {"memories": n, "load_s": round(load_s, 1), "index_s": round(index_s, 1), "queries": queries,
                   "p50_ms": round(lat[len(lat) // 2], 2), "p95_ms": round(lat[int(len(lat) * 0.95) - 1], 2),
                   "disk_mb": round(size / 1048576, 1)}
            self.log("scale test", f"{n:,} memories in a scratch Qdrant Edge shard: hybrid search p50 "
                                   f"{out['p50_ms']} ms · p95 {out['p95_ms']} ms (excl. query embedding)")
            return out
        finally:
            shard.close()
            self._cleanup(path)

    def facets(self, key):
        """Native Qdrant facet counts across the node's shards (drives the filter chips)."""
        out = {}
        with self.lock:
            for _, _, shard in self._shards():
                try:
                    res = shard.facet(FacetRequest(key, limit=20, filter=NOT_DELETED))
                except Exception:  # shard restored before the index existed
                    continue
                for h in res.hits:
                    out[h.value] = out.get(h.value, 0) + h.count
        return out

    def metrics(self):
        rows = self.db.execute("SELECT ms_total, ms_shards FROM searches ORDER BY id DESC LIMIT 200").fetchall()

        def pct(vals, p):
            if not vals:
                return None
            vals = sorted(vals)
            return round(vals[min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))], 2)

        total = [r[0] for r in rows]
        shards = [r[1] for r in rows]
        sync = self.meta("last_sync", {}) or {}
        full = {s: self.meta(f"full_bytes_{s}", 0) for s in self.sites}
        prop = self.meta("propagation_ms", []) or []
        return {
            "propagation_last_ms": prop[-1] if prop else None, "propagation_p50_ms": pct(prop, 50),
            "searches": len(rows),
            "search_p50_ms": pct(total, 50), "search_p95_ms": pct(total, 95),
            "shard_p50_ms": pct(shards, 50), "shard_p95_ms": pct(shards, 95),
            "mean_ms": round(statistics.mean(total), 2) if total else None,
            "last_sync": sync, "full_snapshot_bytes": full,
            "bytes_partial_total": self.meta("bytes_partial_total", 0),
            "bytes_full_total": self.meta("bytes_full_total", 0),
            "bytes_points_total": self.meta("bytes_points_total", 0),
            "offline_since": self.meta("offline_since"),
            "last_drain_s": self.meta("last_drain_s"),
        }
