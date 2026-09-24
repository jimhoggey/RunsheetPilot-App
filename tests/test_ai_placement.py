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
                {"items": [{"playlist": 0, "runsheet": 0},
                           {"playlist": 2, "runsheet": 2},
                           {"playlist": 3, "runsheet": None}]})}}]}

    def fake_post(url, headers=None, json=None, timeout=0, **kw):
        captured["prompt"] = json["messages"][0]["content"]
        captured["temperature"] = json["temperature"]
        captured["provider"] = json.get("provider")
        return _Reply()

    known = {1: 1}                        # the song, already filed
    found = align.align_playlist(
        RUNSHEET, PLAYLIST, SLIDE_TEXT, known,
        lambda it: (it.get("type") or "") == "header",
        or_key="k", model="m", post=fake_post)
    assert found == {0: 0, 2: 2}

    # The prompt has to carry the three things that make this tractable.
    assert 'reads: "GIVING"' in captured["prompt"]
    assert "playlist item 1 belongs to runsheet line 1" in captured["prompt"]
    assert "[presentation] Goodness Of God" in captured["prompt"]
    # Placement must not wobble between two runs of the same runsheet —
    # update mode treats an identical result as a no-op and skips the write.
    assert captured["temperature"] == 0
    # Slide text carries names: providers that store or train are refused.
    assert captured["provider"] == {"data_collection": "deny"}

    items, report = build_update_payload(PLAYLIST, RUNSHEET,
                                         sections={**found, **known})
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
        {"items": [{"playlist": 0, "runsheet": 0}]})}}]}

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


def test_a_model_that_never_answers_costs_the_budget_not_minutes(monkeypatch):
    """OpenRouter's keep-alive bytes meant a read timeout never fired, and a
    preview sat waiting for minutes. The whole pass now has a hard limit."""
    import time
    from propresenterrunsheet.parsing import align

    monkeypatch.setattr(align, "_BUDGET_S", 0.3)

    def stuck(*_a, **_k):
        time.sleep(5)

    t = time.monotonic()
    found = align.align_playlist(RUNSHEET, PLAYLIST, SLIDE_TEXT, {},
                                 lambda it: False, or_key="k", model="m",
                                 post=stuck, backup="b")
    assert found == {} and time.monotonic() - t < 1.5


def test_an_ai_placement_never_outranks_the_operator():
    """Recall is the operator saying where it goes. The model guessing
    otherwise must lose, every time."""
    existing = [{"id": {"uuid": "", "name": "Offering", "index": 0},
                 "type": "header", "target_uuid": "", "is_hidden": False,
                 "is_pco": False, "header_color": {}},
                _media("Comp 1_1", "3"),
                _media("IMG_4021", "1")]
    _, report = build_update_payload(
        existing, [RUNSHEET[0]], sections={1: 0})
    assert report["by_recall"] == 1 and report["by_ai"] == 0


# ── stale file names ──────────────────────────────────────────────────────
# Media names in a working playlist are often out of date, so they must
# not steer the model. Songs are different: a .pro file IS its title.

def test_a_slide_with_a_meaningless_name_can_still_be_placed_by_the_model():
    """"Final.png" has no usable word, so the name rule skipped it as a
    candidate — and the model's correct answer for it was dropped."""
    playlist = [_media("Final.png", "1"), _media("Worship loop", "2")]
    _, report = build_update_payload(playlist, RUNSHEET[:1], sections={0: 0})
    assert report["by_ai"] == 1
    assert report["placements"][0]["above"] == "Final.png"


def test_the_model_overrules_a_stale_name():
    """"Offering" named the slide that now shows the notices."""
    playlist = [_media("Welcome", "1"), _media("Offering", "2"),
                _media("IMG_7", "3")]
    runsheet = [{"parsed": {"type": "other", "title": "Offering"}}]
    _, by_name = build_update_payload(playlist, runsheet)
    assert by_name["placements"][0]["above"] == "Offering"
    _, report = build_update_payload(playlist, runsheet, sections={2: 0})
    assert report["placements"][0]["above"] == "IMG_7"


def _ai_pass(monkeypatch, raw, matched):
    """Run the route's AI pass with OCR and the model faked; return what
    the model was handed and what came back."""
    seen = {}
    monkeypatch.setattr("propresenterrunsheet.settings.load_settings",
                        lambda: {"or_key": "k", "or_model": "m"})
    monkeypatch.setattr(playlist_mod, "fetch_catalogue", lambda: None)
    # OCR answers by ProPresenter's own index, which counts headers.
    monkeypatch.setattr(playlist_mod, "ocr_playlist_media",
                        lambda base, uuid, items: {
                            i: f"text {i}" for i, it in enumerate(items)
                            if it["type"] != "header"})

    def fake_align(matched, items, slide_text, known, *_a, **_k):
        seen.update(items=[i["id"]["name"] for i in items],
                    slide_text=slide_text, known=known)
        return {0: 0}

    monkeypatch.setattr(playlist_mod, "align_playlist", fake_align)
    _, report = build_update_payload(raw, matched)
    found, model = playlist_mod._ai_sections("http://pp", "PL", raw, matched,
                                             report)
    assert model in (None, "m")         # the model asked, reported for the UI
    return found, seen


def _header(name):
    return {"id": {"uuid": "", "name": name, "index": 0}, "type": "header",
            "target_uuid": "", "is_hidden": False, "is_pco": False,
            "header_color": {}}


def test_the_model_counts_positions_the_way_the_payload_builder_does(monkeypatch):
    """A re-run starts from a playlist that already has headers. Numbering
    by ProPresenter's index there put every AI placement a few slides off."""
    raw = [_header("↕ Notices"), _media("IMG_1", "1"),
           _header("↕ Notices"), _media("IMG_2", "2")]
    found, seen = _ai_pass(monkeypatch, raw, RUNSHEET[:1])
    assert seen["items"] == ["IMG_1", "IMG_2"]      # ↕ means "didn't know"
    assert seen["slide_text"] == {0: "text 1", 1: "text 3"}
    assert found == {0: 0}


def test_where_a_header_sits_is_shown_to_the_model_not_given_as_fact(monkeypatch):
    """Slides get moved around the headers. Taking a header's spot as
    settled told the model a shuffled slide was still what it used to be,
    so a shuffled playlist could never be noticed."""
    raw = [_header("Offering"), _media("IMG_1", "1")]
    _, seen = _ai_pass(monkeypatch, raw, [RUNSHEET[0]])
    assert seen["items"] == ["Offering", "IMG_1"] and seen["known"] == {}


def test_only_songs_and_aliases_are_facts_to_the_model(monkeypatch):
    playlist = [_pres("Goodness Of God", "2"), _media("Offering", "3")]
    runsheet = [{"parsed": {"type": "song", "title": "Goodness of God"}},
                {"parsed": {"type": "other", "title": "Offering"}}]
    _, seen = _ai_pass(monkeypatch, playlist, runsheet)
    assert seen["known"] == {0: 0}      # the song, not the "Offering" still


def test_the_model_is_asked_even_when_names_placed_everything(monkeypatch):
    playlist = [_media("Offering", "3")]
    runsheet = [{"parsed": {"type": "other", "title": "Offering"}}]
    found, seen = _ai_pass(monkeypatch, playlist, runsheet)
    assert seen["known"] == {} and found == {0: 0}


def test_a_playlist_with_nothing_to_place_against_is_never_sent(monkeypatch):
    """Empty, or holding only headers: no answer could change anything."""
    for raw in ([], [_header("Offering")]):
        found, seen = _ai_pass(monkeypatch, raw, RUNSHEET[:1])
        assert found == {} and seen == {}


def test_agreeing_with_a_name_is_not_reported_as_reading_the_slide():
    """"Read off the slide" is for what only reading found — and a video
    is never read at all."""
    playlist = [_media("Offering Video.mp4", "1")]
    runsheet = [{"parsed": {"type": "other", "title": "Offering Video"}}]
    _, report = build_update_payload(playlist, runsheet, sections={0: 0})
    assert report["by_name"] == 1 and report["by_ai"] == 0


# ── the write reuses what was confirmed ───────────────────────────────────

def test_a_slide_reading_from_the_client_is_re_validated():
    """It comes back over HTTP: `{slide: runsheet line}`, whole numbers in
    range. Out of runsheet order is allowed — that is a shuffle."""
    sane = playlist_mod._sane_sections
    assert sane({"0": 1, "3": 2}, 3, 10) == {0: 1, 3: 2}
    assert sane({"0": 2, "1": 0}, 3, 10) == {0: 2, 1: 0}
    assert sane({"0": 9}, 3, 10) == {}                  # no such line
    assert sane({"99": 0}, 3, 10) == {}                 # past the playlist
    assert sane({"-1": 0, "0": -1}, 3, 10) == {}
    assert sane("not a dict", 3, 10) == {}
    assert sane({"x": "y", "1": None}, 3, 10) == {}


def test_a_slide_reading_sent_as_pairs_keeps_its_play_order():
    """JSON objects don't keep the order of number keys, so the reading
    travels as pairs. Order kept, each slide once, junk dropped."""
    sane = playlist_mod._sane_sections
    got = sane([[5, 1], [9, 1], [0, 1], [5, 2]], 3, 10)
    assert list(got.items()) == [(5, 1), (9, 1), (0, 1)]
    assert sane([[1], [1, 2, 3], "12", None, [0, 9], ["2", "0"]], 3, 10) == {2: 0}
