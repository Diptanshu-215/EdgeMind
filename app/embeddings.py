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


# Calibrated on the demo corpus (English, Hindi, Marathi questions; see README "Multilingual").
# miss: best match below this = the node doesn't know. demand: a private note this close to another
# crew's failed search is worth offering. dup: two memories this close are linked as duplicates.
PROFILES = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": {"dup": 0.93, "miss": 0.42, "demand": 0.55},
    "BAAI/bge-small-en-v1.5": {"dup": 0.90, "miss": 0.70, "demand": 0.62},
}


class FastEmbedder:
    def __init__(self, model: str):
        from fastembed import TextEmbedding  # noqa: WPS433 (optional dependency)

        cache = config.MODELS_DIR
        cache.mkdir(parents=True, exist_ok=True)
        self._m = TextEmbedding(model, cache_dir=str(cache))
        self.name = f"fastembed:{model}"
        p = PROFILES.get(model, PROFILES["BAAI/bge-small-en-v1.5"])
        self.dup_threshold, self.miss_threshold, self.demand_threshold = p["dup"], p["miss"], p["demand"]
        self.multilingual = "multilingual" in model

    def embed(self, texts):
        return [list(map(float, v)) for v in self._m.embed(list(texts))]

    def embed_query(self, text):
        # BGE retrieval models expect queries with an instruction prefix; fastembed adds it
        return list(map(float, next(iter(self._m.query_embed(text)))))


class Embeddings:
    """The dense model loads on first use (the gateway rarely needs it, and can release it)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._dense = None
        self.bm25 = Bm25()

    @property
    def dense(self):
        if self._dense is None:
            with self._load_lock:
                if self._dense is None:
                    self._dense = self._load_dense()
        return self._dense

    @property
    def name(self):
        """The model name, without loading it."""
        if self._dense is not None:
            return self._dense.name
        return HashEmbedder.name if config.EMBEDDER == "hash" else f"fastembed:{config.FASTEMBED_MODEL}"

    def _profile(self, key):
        if self._dense is not None:
            return getattr(self._dense, f"{key}_threshold")
        if config.EMBEDDER == "hash":
            return getattr(HashEmbedder, f"{key}_threshold")
        return PROFILES.get(config.FASTEMBED_MODEL, PROFILES["BAAI/bge-small-en-v1.5"])[key]

    def release(self):
        """Free the model's RAM (it reloads on the next use)."""
        with self._lock:
            self._dense = None
        import gc
        gc.collect()

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
        return float(config.DUPLICATE_THRESHOLD) if config.DUPLICATE_THRESHOLD else self._profile("dup")

    @property
    def miss_threshold(self) -> float:
        """Best cosine match below this = the device doesn't really know the answer."""
        return float(os.getenv("EDGEMIND_MISS_THRESHOLD") or self._profile("miss"))

    @property
    def demand_threshold(self) -> float:
        """A personal note this similar to another device's failed search is worth offering."""
        return float(os.getenv("EDGEMIND_DEMAND_THRESHOLD") or self._profile("demand"))

    def embed_dense(self, text: str):
        with self._lock:
            return self.dense.embed([text])[0]

    def embed_many(self, texts):
        with self._lock:
            return self.dense.embed(list(texts))

    def embed_query(self, text: str):
        """Dense vector for a search query (asymmetric retrieval)."""
        with self._lock:
            return self.dense.embed_query(text)

    def sparse_doc(self, text: str):
        return self.bm25.embed_document(text)

    def sparse_query(self, text: str):
        return self.bm25.embed_query(text)


class Vision:
    """CLIP (ONNX, CPU) for photos: image -> vector, and text -> the same space ("burnt bushing").
    Loaded only when a node first handles a photo, to keep RAM low on small devices."""

    IDLE_SECONDS = 120

    def __init__(self):
        self._img = self._txt = None
        self._lock = threading.Lock()
        self.error = None
        self._used = 0.0
        self._reaper = None

    def _touch(self):
        import time
        self._used = time.time()
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap, daemon=True, name="clip-idle")
            self._reaper.start()

    def _reap(self):
        """Photo models take ~600 MB; drop them when nobody has used photos for a while."""
        import gc
        import time
        while True:
            time.sleep(15)
            if self.loaded and time.time() - self._used > self.IDLE_SECONDS:
                with self._lock:
                    self._img = self._txt = None
                gc.collect()

    def available(self):
        return config.EMBEDDER != "hash" and self.error is None

    def _load(self, attr, cls_name, model):
        if getattr(self, attr) is None:
            try:
                import fastembed
                setattr(self, attr, getattr(fastembed, cls_name)(model, cache_dir=str(config.MODELS_DIR)))
            except Exception as exc:  # no model download possible: photos still saved, just not searchable
                self.error = f"{type(exc).__name__}: {exc}"
                raise
        return getattr(self, attr)

    def embed_image(self, path):
        with self._lock:
            self._touch()
            m = self._load("_img", "ImageEmbedding", config.CLIP_VISION)
            return list(map(float, next(iter(m.embed([str(path)])))))

    def embed_text(self, text):
        with self._lock:
            self._touch()
            m = self._load("_txt", "TextEmbedding", config.CLIP_TEXT)
            return list(map(float, next(iter(m.embed([text])))))

    @property
    def loaded(self):
        return self._img is not None or self._txt is not None


_instance = None
_vision = None


def get() -> Embeddings:
    global _instance
    if _instance is None:
        _instance = Embeddings()
    return _instance


def vision() -> Vision:
    global _vision
    if _vision is None:
        _vision = Vision()
    return _vision
