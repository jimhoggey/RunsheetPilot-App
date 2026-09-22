"""End-to-end test of the OCR → LLM → placement cascade.

The cascade exists for one case the string rules cannot touch: a slide
whose graphic reads GIVING against a runsheet line called "Offering".
They share no word, so `resolve_object` scores nothing, and the header
would land marked ↕ for the operator to drag.

Both halves are injected here — a fake OCR backend instead of the
platform engine, a fake OpenRouter instead of the network — so the whole
path is exercised on any machine. The real OCR engine is Windows-only on
the operator's box (`winocr`), which is precisely why none of this is
allowed to depend on it being present.
"""
import json

import propresenterrunsheet.routes.playlist as playlist_mod
from propresenterrunsheet.propresenter import thumbnails
from propresenterrunsheet.propresenter.playlist_update import build_update_payload


def _media(name, uuid):
    return {"id": {"uuid": uuid, "name": name, "index": 0}, "type": "media",
            "target_uuid": f"T-{uuid}", "is_hidden": False, "is_pco": False}


def _pres(name, uuid):
    return {"id": {"uuid": uuid, "name": name, "index": 0},
            "type": "presentation",
            "presentation_info": {"presentation_uuid": f"PU-{uuid}"},
            "is_hidden": False, "is_pco": False}


# A playlist named the way real ones are: two meaningless stills, one
# song whose .pro file IS named after the song, and a video.
PLAYLIST = [
    _media("IMG_4021", "1"),
    _pres("Goodness Of God", "2"),
    _media("Comp 1_1", "3"),
    _media("bumper.mp4", "4"),
]

RUNSHEET = [
    {"parsed": {"type": "other", "title": "Offering", "duration_min": 4}},
    {"parsed": {"type": "song", "title": "Goodness of God"}},
    {"parsed": {"type": "announcement", "title": "Connect Card"}},
]

# What the machine's OCR engine sees on each still.
SLIDE_TEXT = {0: "GIVING", 2: "NEXT STEPS"}


# ── the OCR half ──────────────────────────────────────────────────────────

def test_songs_and_videos_are_not_ocrd():
    """Lyrics would hand the model a wall of verse, and this operator
    always names videos on the runsheet, so neither is worth reading."""
    assert thumbnails.ocr_targets(PLAYLIST) == [(0, "IMG_4021"), (2, "Comp 1_1")]


def test_ocr_pass_returns_text_keyed_by_playlist_index(monkeypatch):
    """The dict keys must be PLAYLIST indexes, not "the third thing we
    OCR'd" — everything downstream places headers by that number."""
    asked = {}

    class _Blob:
        ok = True

        def __init__(self, index):
            # Stand in for the rendered pixels: the fake OCR backend
            # below reads the index straight back out.
            self.content = str(index).encode()

    def fake_get(url, params=None, timeout=0, **kw):
        # .../v1/playlist/{uuid}/{index}/thumbnail/{cue}
        index = int(url.split("/thumbnail/")[0].rsplit("/", 1)[1])
        asked[index] = params
        return _Blob(index)

    monkeypatch.setattr(thumbnails, "_read",
                        lambda blob, backend: SLIDE_TEXT.get(int(blob), ""))
    out = thumbnails.ocr_playlist_media(
        "http://pp", "PL", PLAYLIST, backend=lambda img: [],
        http_get=fake_get)
    assert out == {0: "GIVING", 2: "NEXT STEPS"}
    assert sorted(asked) == [0, 2], "songs and videos must not be fetched"
    # Thumbnails are requested big enough to actually OCR: PP's own
    # default of 256 reads nothing but a huge title.
    assert all(p["quality"] >= 768 for p in asked.values())


def test_a_missing_ocr_engine_degrades_to_no_text(monkeypatch):
    """The engine is Windows-only in practice. Its absence must cost the
    upgrade, never the feature."""
    from propresenterrunsheet.parsing.ocr import OCRUnavailable

    def no_engine():
        raise OCRUnavailable("nope")

    monkeypatch.setattr(thumbnails, "pick_backend", no_engine)
    assert thumbnails.ocr_playlist_media("http://pp", "PL", PLAYLIST) == {}


# ── the whole cascade ─────────────────────────────────────────────────────

def test_the_rules_alone_leave_the_meaningless_slides_unplaced():
    """The baseline this feature exists to improve on."""
    _, report = build_update_payload(PLAYLIST, RUNSHEET)
    assert report["anchored"] == 1        # only the song, by its .pro name
    assert report["unplaced"] == 2


def test_slide_text_places_what_no_string_rule_could(monkeypatch):
    """GIVING → "Offering" and NEXT STEPS → "Connect Card" share no word
    with their runsheet lines. This is the whole point of the pass."""
    from propresenterrunsheet.parsing import align

    captured = {}

    class _Reply:
        ok = True

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": json.dumps(
                {"placements": [{"runsheet": 0, "playlist": 0},
                                {"runsheet": 2, "playlist": 2}]})}}]}

    def fake_post(url, headers=None, json=None, timeout=0, **kw):
        captured["prompt"] = json["messages"][0]["content"]
        captured["temperature"] = json["temperature"]
        return _Reply()

    known = {1: 1}                        # the song, already placed
    found = align.align_playlist(
        RUNSHEET, PLAYLIST, SLIDE_TEXT, known,
        lambda it: (it.get("type") or "") == "header",
        or_key="k", model="m", post=fake_post)
    assert found == {0: 0, 2: 2}

    # The prompt has to carry the three things that make this tractable.
    assert 'reads: "GIVING"' in captured["prompt"]
    assert "already placed at playlist item 1" in captured["prompt"]
    assert "[presentation] Goodness Of God" in captured["prompt"]
    # Placement must not wobble between two runs of the same runsheet —
    # update mode treats an identical result as a no-op and skips the write.
    assert captured["temperature"] == 0

    items, report = build_update_payload(PLAYLIST, RUNSHEET, ai_anchors=found)
    assert report["anchored"] == 3 and report["unplaced"] == 0
    assert report["by_ai"] == 2
    # Headers keep create mode's exact labelling, duration and all.
    assert [i["id"]["name"] for i in items] == [
        "Offering (4 min)", "IMG_4021",
        "Goodness of God", "Goodness Of God",
        "Connect Card", "Comp 1_1",
        "bumper.mp4"]


def test_a_busy_provider_gets_exactly_one_retry_on_the_backup_model():
    """Free providers are often overloaded, and OpenRouter reports it as
    HTTP 200 with an error body. Retry once on the backup, then give up
    quietly — the rules' placement still stands."""
    from propresenterrunsheet.parsing import align

    busy = {"error": {"code": 503, "message": "Upstream error from Nvidia: "
                      "Service temporarily overloaded"}}
    answer = {"choices": [{"message": {"content": json.dumps(
        {"placements": [{"runsheet": 0, "playlist": 0}]})}}]}

    def run(replies, backup):
        asked = []

        def fake_post(url, json=None, **kw):
            asked.append(json["model"])
            body = replies.pop(0)
            return type("R", (), {"ok": True, "status_code": 200,
                                  "json": staticmethod(lambda: body)})()

        found = align.align_playlist(
            RUNSHEET, PLAYLIST, SLIDE_TEXT, {1: 1}, lambda it: False,
            or_key="k", model="m", post=fake_post, backup=backup)
        return found, asked

    assert run([busy, answer], "b") == ({0: 0}, ["m", "b"])
    assert run([busy, busy], "b") == ({}, ["m", "b"])
    assert run([busy], None) == ({}, ["m"])


def test_an_ai_placement_never_outranks_the_operator():
    """Recall is the operator saying where it goes. The model guessing
    otherwise must lose, every time."""
    existing = [{"id": {"uuid": "", "name": "Offering", "index": 0},
                 "type": "header", "target_uuid": "", "is_hidden": False,
                 "is_pco": False, "header_color": {}},
                _media("Comp 1_1", "3"),
                _media("IMG_4021", "1")]
    _, report = build_update_payload(
        existing, [RUNSHEET[0]], ai_anchors={0: 1})
    assert report["by_recall"] == 1 and report["by_ai"] == 0


# ── the write reuses what was confirmed ───────────────────────────────────

def test_anchors_from_the_client_are_re_validated():
    """They come back over HTTP. Same checks the model's own answer gets."""
    sane = playlist_mod._sane_anchors
    assert sane({"0": 1, "1": 3}, 3, 10) == {0: 1, 1: 3}
    assert sane({"0": 5, "1": 2}, 3, 10) == {0: 5}      # backwards dropped
    assert sane({"9": 1}, 3, 10) == {}                  # out of range
    assert sane({"0": 99}, 3, 10) == {}                 # past the playlist
    assert sane("not a dict", 3, 10) == {}
    assert sane({"x": "y"}, 3, 10) == {}
