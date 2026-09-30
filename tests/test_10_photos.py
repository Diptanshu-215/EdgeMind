"""Photo evidence: a photo is embedded on the node (CLIP), searchable by words and by photo offline; the fleet gets a
thumbnail + vector live while the full photo stays on the node.
"""
import base64
import io

import pytest

from helpers import uid, wait_for


def photo(color, shape):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 400), "white")
    d = ImageDraw.Draw(im)
    (d.ellipse if shape == "circle" else d.rectangle)([70, 70, 330, 330], fill=color)
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- component
def test_an_unreadable_photo_is_rejected():
    from app.device import decode_photo
    with pytest.raises(Exception):
        decode_photo("data:image/jpeg;base64," + base64.b64encode(b"not an image").decode())


def test_a_thumbnail_is_small_and_a_jpeg():
    from app.device import decode_photo, make_thumb
    thumb = make_thumb(decode_photo(photo("red", "circle")))
    assert thumb.startswith("data:image/jpeg;base64,") and len(thumb) < 40_000


# ---------------------------------------------------------------- system
@pytest.fixture(scope="module")
def vision(hub):
    if not hub.A.state()["vision"]:
        pytest.skip("photo model (CLIP) not available on this node")


def test_a_photo_memory_is_searchable_by_words_and_by_photo_across_the_fleet(hub, flow, vision):
    A, B = hub.A, hub.B
    title = f"Marker on pole {uid()}"
    with flow.step("tablet-A captures a photo memory (CLIP runs on the node)"):
        d = A.write("", title, "fix", site="global", photo=photo("red", "circle"))
        assert d.get("has_photo") and d.get("thumb", "").startswith("data:image/jpeg"), d.keys()
    with flow.step("tablet-B receives the thumbnail and the photo vector live"):
        got = B.wait_doc(d["doc_id"], "receives the photo memory", timeout=30)
        assert got.get("thumb"), "no thumbnail reached tablet-B"
    with flow.step("words find the photo on tablet-B ('a red circle')"):
        r = B.search("a red circle")
        hit = next((x for x in r["results"] if x["doc_id"] == d["doc_id"]), None)
        assert hit, [x["title"] for x in r["results"]]
        assert hit["why"].get("photo", 0) >= 0.26, hit["why"]
    with flow.step("a similar photo finds it on tablet-B ('seen this before?')"):
        r = B.post("search/photo", json={"photo": photo("red", "circle")}).json()
        assert r["results"] and r["results"][0]["doc_id"] == d["doc_id"], [x["title"] for x in r["results"][:3]]
    with flow.step("the full photo stays on tablet-A; tablet-B only has the thumbnail"):
        full = A.get(f"photos/{d['doc_id']}").content
        thumb = B.get(f"photos/{d['doc_id']}").content
        assert len(full) > len(thumb) > 0, (len(full), len(thumb))
        assert A.state()["stats"]["photos"] >= 1


def test_an_invalid_photo_upload_is_rejected(hub, vision):
    bad = "data:image/jpeg;base64," + base64.b64encode(b"definitely not a jpeg").decode()
    hub.A.post("memory", json={"text": "", "title": f"Bad photo {uid()}", "kind": "fix", "photo": bad}, expect=(400,))
    hub.A.post("search/photo", json={"photo": bad}, expect=(400,))
    wait_for(lambda: hub.A.state()["enrolled"], "node still healthy after a bad upload", 10)
