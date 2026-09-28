"""Benchmark: on-device search latency and partial-vs-full snapshot size at realistic scale.

    python tools/bench.py --qdrant http://127.0.0.1:6333 --n 5000 --new 20

Needs a running Qdrant Server. Uses the same embedder as the app (EDGEMIND_EMBEDDER).
"""
import argparse
import random
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from qdrant_client import QdrantClient
from qdrant_client import models as m

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qdrant_edge import EdgeShard, Fusion, Prefetch, Query, QueryRequest  # noqa: E402

from app import embeddings  # noqa: E402

EQUIP = ["distribution transformer", "11 kV feeder", "LT line", "drop-out fuse", "lightning arrester", "pin insulator",
         "AB switch", "service cable", "earth pit", "street light circuit", "RMU", "capacitor bank", "bushing", "meter"]
FAULT = ["tripping", "overheating", "oil leak", "humming", "flashover", "burnt", "loose joint", "earth fault",
         "voltage drop", "sparking", "cracked", "corroded", "bird fault", "tree contact"]
ACTION = ["replaced", "re-tightened", "cleaned and resealed", "re-earthed", "balanced the load", "re-strung",
          "fitted a guard", "changed the fuse", "topped up oil", "trimmed trees"]


def fake(i):
    e, f, a = random.choice(EQUIP), random.choice(FAULT), random.choice(ACTION)
    title = f"{e.title()} {f} at pole {random.randint(1, 900)}"
    text = f"{e} {f} reported by crew. Found cause during patrol; {a}. Load {random.randint(20, 99)}% after work."
    return title, text


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qdrant", default="http://127.0.0.1:6333")
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--new", type=int, default=20)
    ap.add_argument("--queries", type=int, default=300)
    ap.add_argument("--index-kb", type=int, default=500)
    ap.add_argument("--segments", type=int, default=8)
    a = ap.parse_args()
    random.seed(7)
    emb = embeddings.get()
    col = "edgemind_bench"
    c = QdrantClient(url=a.qdrant, timeout=120)
    http = httpx.Client(base_url=a.qdrant, timeout=300)
    if c.collection_exists(col):
        c.delete_collection(col)
    c.create_collection(col, vectors_config={"dense": m.VectorParams(size=384, distance=m.Distance.COSINE)},
                        sparse_vectors_config={"bm25": m.SparseVectorParams(modifier=m.Modifier.IDF)}, shard_number=1,
                        optimizers_config=m.OptimizersConfigDiff(indexing_threshold=a.index_kb,
                                                                 default_segment_number=a.segments))

    def points(k0, k1):
        pts = []
        for i in range(k0, k1):
            title, text = fake(i)
            sv = emb.sparse_doc(f"{title} {text}")
            pts.append(m.PointStruct(id=str(uuid.uuid4()),
                                     vector={"dense": emb.embed_dense(f"{title}. {text}"),
                                             "bm25": m.SparseVector(indices=list(sv.indices), values=list(sv.values))},
                                     payload={"title": title, "text": text, "seq": i, "deleted": False}))
        return pts

    t0 = time.time()
    for k in range(0, a.n, 500):
        c.upsert(col, points(k, min(a.n, k + 500)), wait=True)
    print(f"loaded {a.n} memories in {time.time() - t0:.1f}s (embedder: {emb.dense.name})")
    for _ in range(120):  # let the server finish optimizing/indexing
        info = c.get_collection(col)
        if info.status == m.CollectionStatus.GREEN:
            break
        time.sleep(1)

    work = Path(tempfile.mkdtemp(prefix="em-bench-"))
    full = http.get(f"/collections/{col}/shards/0/snapshot").content
    (work / "full.snapshot").write_bytes(full)
    EdgeShard.unpack_snapshot(str(work / "full.snapshot"), str(work / "mirror"))
    shard = EdgeShard.load(str(work / "mirror"))

    lat = []
    qs = [f"{random.choice(EQUIP)} {random.choice(FAULT)}" for _ in range(a.queries)]
    for q in qs:
        qd, qsp = emb.embed_dense(q), emb.sparse_query(q)
        t = time.perf_counter()
        shard.query(QueryRequest(limit=6, prefetches=[Prefetch(limit=30, query=Query.Nearest(qd, using="dense")),
                                                      Prefetch(limit=30, query=Query.Nearest(qsp, using="bm25"))],
                                 query=Fusion.Rrf(k=60), with_payload=True))
        lat.append((time.perf_counter() - t) * 1000)

    c.upsert(col, points(a.n, a.n + a.new), wait=True)
    time.sleep(2)
    part = http.post(f"/collections/{col}/shards/0/snapshot/partial/create", json=shard.snapshot_manifest()).content
    (work / "part.snapshot").write_bytes(part)
    (work / "tmp").mkdir()
    t = time.perf_counter()
    shard.update_from_snapshot(str(work / "part.snapshot"), tmp_dir=str(work / "tmp"))
    apply_ms = (time.perf_counter() - t) * 1000

    print("\n| Measure | Value |\n| --- | --- |")
    print(f"| Memories on device | {a.n:,} |")
    print(f"| Hybrid search p50 / p95 (on device, excl. embedding) | {pct(lat, 50):.2f} ms / {pct(lat, 95):.2f} ms |")
    print(f"| Full snapshot (first sync) | {len(full) / 1024 / 1024:.2f} MB |")
    print(f"| Partial snapshot after {a.new} new memories | {len(part) / 1024:.0f} KB |")
    print(f"| Partial vs full | {100 * len(part) / len(full):.1f}% of the bytes |")
    print(f"| Apply partial snapshot on device | {apply_ms:.0f} ms |")
    shard.close()
    shutil.rmtree(work, ignore_errors=True)
    c.delete_collection(col)


if __name__ == "__main__":
    main()
