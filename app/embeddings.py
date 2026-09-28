"""Dense + sparse (BM25) embeddings that work fully offline.

Dense: FastEmbed (ONNX, CPU) when available, otherwise a deterministic hashing
embedder so the app never breaks during a demo without the model cached.
Sparse: Qdrant Edge's built-in BM25, identical on device and cloud.
"""
import hashlib
import os
import math
import re
import threading

from qdrant_edge import Bm25

from . import config

_TOKEN = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    """Feature-hashing of word unigrams + character trigrams, L2-normalised.
    Not a real language model, but gives stable, meaningful-enough similarity."""

    name = "hash-384 (offline fallback)"
    dup_threshold = 0.85
    miss_threshold = 0.35
    demand_threshold = 0.35

    def __init__(self, dim: int = config.DENSE_DIM):
        self.dim = dim

    def embed_query(self, text):
        return self.embed([text])[0]

    def _features(self, text: str):
        words = _TOKEN.findall(text.lower())
        for w in words:
            yield "w:" + w, 1.0
            padded = f"#{w}#"
            for i in range(len(padded) - 2):
                yield "c:" + padded[i : i + 3], 0.35
        for a, b in zip(words, words[1:]):
            yield f"b:{a}_{b}", 0.6

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for feat, w in self._features(t):
                h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
                v[h % self.dim] += w if (h >> 32) & 1 else -w
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class FastEmbedder:
    def __init__(self, model: str):
        from fastembed import TextEmbedding  # noqa: WPS433 (optional dependency)

        cache = config.DATA_DIR / "models"
        cache.mkdir(parents=True, exist_ok=True)
        self._m = TextEmbedding(model, cache_dir=str(cache))
        self.name = f"fastembed:{model}"
        self.dup_threshold = 0.9
        self.miss_threshold = 0.70
        self.demand_threshold = 0.62

    def embed(self, texts):
        return [list(map(float, v)) for v in self._m.embed(list(texts))]

    def embed_query(self, text):
        # BGE retrieval models expect queries with an instruction prefix; fastembed adds it
        return list(map(float, next(iter(self._m.query_embed(text)))))


class Embeddings:
    def __init__(self):
        self._lock = threading.Lock()
        self.dense = self._load_dense()
        self.bm25 = Bm25()

    @staticmethod
    def _load_dense():
        if config.EMBEDDER in ("auto", "fastembed"):
            try:
                return FastEmbedder(config.FASTEMBED_MODEL)
            except Exception as exc:  # model not downloadable / not installed
                if config.EMBEDDER == "fastembed":
                    raise
                print(f"[embeddings] FastEmbed unavailable ({type(exc).__name__}); using hashing fallback")
        return HashEmbedder()

    @property
    def dup_threshold(self) -> float:
        return float(config.DUPLICATE_THRESHOLD) if config.DUPLICATE_THRESHOLD else self.dense.dup_threshold

    @property
    def miss_threshold(self) -> float:
        """Best cosine match below this = the device doesn't really know the answer."""
        return float(os.getenv("EDGEMIND_MISS_THRESHOLD") or self.dense.miss_threshold)

    @property
    def demand_threshold(self) -> float:
        """A personal note this similar to another device's failed search is worth offering."""
        return float(os.getenv("EDGEMIND_DEMAND_THRESHOLD") or self.dense.demand_threshold)

    def embed_dense(self, text: str):
        with self._lock:
            return self.dense.embed([text])[0]

    def embed_query(self, text: str):
        """Dense vector for a search query (asymmetric retrieval)."""
        with self._lock:
            return self.dense.embed_query(text)

    def sparse_doc(self, text: str):
        return self.bm25.embed_document(text)

    def sparse_query(self, text: str):
        return self.bm25.embed_query(text)


_instance = None


def get() -> Embeddings:
    global _instance
    if _instance is None:
        _instance = Embeddings()
    return _instance
