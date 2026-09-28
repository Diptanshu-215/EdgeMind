"""Cloud storage behind the sync gateway: one Qdrant collection per site.

Server mode (QDRANT_URL set): a real Qdrant Server, which also serves full and
partial shard snapshots that devices restore straight into Qdrant Edge.
Embedded mode: qdrant-client local mode; no snapshots, devices use point sync.
"""
import threading
import warnings

import httpx
from qdrant_client import QdrantClient
from qdrant_client import models as m

from . import config, embeddings

NOT_DELETED = m.Filter(must_not=[m.FieldCondition(key="deleted", match=m.MatchValue(value=True))])


def sparse(sv):
    return m.SparseVector(indices=list(sv.indices), values=list(sv.values))


class CloudStore:
    def __init__(self):
        self.lock = threading.RLock()
        if config.QDRANT_URL:
            self.client = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY, timeout=10)
            self.server = True
            self.mode = f"Qdrant Server {config.QDRANT_URL}"
            headers = {"api-key": config.QDRANT_API_KEY} if config.QDRANT_API_KEY else {}
            self.http = httpx.Client(base_url=config.QDRANT_URL, headers=headers, timeout=60,
                                     trust_env=not config.QDRANT_URL.startswith(("http://127.", "http://localhost")))
        else:
            path = config.DATA_DIR / "cloud-embedded"
            path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(path))
            self.server = False
            self.mode = "Embedded Qdrant (no snapshots: point sync)"
            self.http = None
        for site in config.SITES:
            self.ensure(site)

    # ---------------------------------------------------------------- setup
    def ensure(self, site):
        col = config.collection(site)
        with self.lock:
            if self.client.collection_exists(col):
                return
            # Tuned for edge sync (see tools/bench.py): bulk data is indexed into sealed
            # segments, new writes land in a small appendable one, so a partial snapshot
            # after a few changes is ~1% of the full shard instead of ~50%.
            extra = {"shard_number": 1,
                     "optimizers_config": m.OptimizersConfigDiff(indexing_threshold=500, default_segment_number=8)
                     } if self.server else {}
            self.client.create_collection(
                col,
                vectors_config={"dense": m.VectorParams(size=config.DENSE_DIM, distance=m.Distance.COSINE)},
                sparse_vectors_config={"bm25": m.SparseVectorParams(modifier=m.Modifier.IDF)},
                **extra,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for field, schema in (("seq", m.PayloadSchemaType.INTEGER), ("deleted", m.PayloadSchemaType.BOOL)):
                    try:
                        self.client.create_payload_index(col, field, field_schema=schema)
                    except Exception:
                        pass

    def reset(self):
        with self.lock:
            for site in config.SITES:
                col = config.collection(site)
                if self.client.collection_exists(col):
                    self.client.delete_collection(col)
                self.ensure(site)

    def ping(self):
        try:
            with self.lock:
                self.client.get_collections()
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- reads
    def get(self, site, doc_id):
        with self.lock:
            pts = self.client.retrieve(config.collection(site), [doc_id], with_payload=True, with_vectors=["dense"])
        if not pts:
            return None
        return {"payload": pts[0].payload, "dense": pts[0].vector["dense"]}

    def changes_since(self, site, since_seq, limit=500):
        flt = m.Filter(must=[m.FieldCondition(key="seq", range=m.Range(gt=since_seq))])
        out, offset = [], None
        with self.lock:
            while True:
                pts, offset = self.client.scroll(config.collection(site), scroll_filter=flt, with_payload=True,
                                                 with_vectors=["dense"], limit=256, offset=offset)
                out.extend({"payload": p.payload, "dense": p.vector["dense"]} for p in pts)
                if offset is None or len(out) >= limit:
                    break
        out.sort(key=lambda d: d["payload"]["seq"])
        return out[:limit]

    def list(self, limit=300):
        docs = []
        with self.lock:
            for site in config.SITES:
                pts, _ = self.client.scroll(config.collection(site), scroll_filter=NOT_DELETED, with_payload=True, limit=limit)
                docs.extend(p.payload for p in pts)
        docs.sort(key=lambda d: d.get("seq", 0), reverse=True)
        return docs

    def count(self):
        with self.lock:
            return sum(self.client.count(config.collection(s), count_filter=NOT_DELETED, exact=True).count for s in config.SITES)

    def search(self, query, sites=None, limit=6):
        emb = embeddings.get()
        dense, sq = emb.embed_query(query), sparse(emb.sparse_query(query))
        hits = []
        with self.lock:
            for site in sites or config.SITES:
                res = self.client.query_points(
                    config.collection(site),
                    prefetch=[m.Prefetch(query=dense, using="dense", limit=20, filter=NOT_DELETED),
                              m.Prefetch(query=sq, using="bm25", limit=20, filter=NOT_DELETED)],
                    query=m.FusionQuery(fusion=m.Fusion.RRF), limit=limit, with_payload=True).points
                hits.extend({"score": p.score, **p.payload} for p in res)
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:limit]

    def find_similar(self, dense, exclude_id, threshold):
        flt = m.Filter(must_not=[m.HasIdCondition(has_id=[exclude_id]),
                                 m.FieldCondition(key="deleted", match=m.MatchValue(value=True))])
        best = None
        with self.lock:
            for site in config.SITES:
                res = self.client.query_points(config.collection(site), query=dense, using="dense", limit=1,
                                               query_filter=flt, with_payload=True).points
                if res and res[0].score >= threshold and (best is None or res[0].score > best["score"]):
                    best = {"doc_id": res[0].payload["doc_id"], "title": res[0].payload.get("title"),
                            "score": round(res[0].score, 3), "site": site}
        return best

    # ---------------------------------------------------------------- writes
    def upsert(self, site, doc_id, dense, text, payload):
        vec = {"dense": dense}
        if text:
            vec["bm25"] = sparse(embeddings.get().sparse_doc(text))
        with self.lock:
            self.client.upsert(config.collection(site), [m.PointStruct(id=doc_id, vector=vec, payload=payload)], wait=True)

    # ---------------------------------------------------------------- snapshots (server mode)
    def full_snapshot(self, site) -> bytes:
        r = self.http.get(f"/collections/{config.collection(site)}/shards/0/snapshot")
        r.raise_for_status()
        return r.content

    def partial_snapshot(self, site, manifest) -> bytes:
        r = self.http.post(f"/collections/{config.collection(site)}/shards/0/snapshot/partial/create", json=manifest)
        r.raise_for_status()
        return r.content
