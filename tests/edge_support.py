"""In-process helpers for component tests: simulate fleet memories arriving in a device's mirror shard."""
import itertools
import time
import uuid

from app import config, embeddings

_SEQ = itertools.count(1)


def fleet_doc(title, text, kind="manual", site="global", author="tablet-Z", version=1, doc_id=None,
              updated_at=None, deleted=False):
    """A memory exactly as the gateway stores it (server version + sequence number)."""
    now = time.time()
    doc_id = doc_id or str(uuid.uuid4())
    payload = config.with_flags({
        "doc_id": doc_id, "title": title, "text": "" if deleted else text, "kind": kind, "site": site,
        "origin_device": author, "author_device": author, "created_at": now, "updated_at": updated_at or now,
        "deleted": deleted, "redacted": False, "version": version, "seq": next(_SEQ), "synced_at": now,
    })
    return {"payload": payload, "dense": embeddings.get().embed_dense(f"{title}. {text}")}


def receive(dev, site, *docs):
    """The fleet sends these memories to `dev` (point-sync path into its mirror shard for `site`)."""
    dev.apply_points(site, list(docs), max(d["payload"]["seq"] for d in docs))
    return docs[0]["payload"]["doc_id"] if len(docs) == 1 else [d["payload"]["doc_id"] for d in docs]


def ids(results):
    return [r["doc_id"] for r in results["results"]]
