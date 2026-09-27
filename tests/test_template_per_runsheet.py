"""A template is chosen per runsheet, and chosen well.

The 27 Sep 2026 report: a Young Adults runsheet was built from the youth
template on a ProPresenter that has BOTH a youth and a young adults
template. The pick autosaved and was restored at the next launch, so a
template chosen for a youth night was still "pinned" days later — and a
pin is never second-guessed. Now the page sends its pick with each parse
("" = Auto) and resets it for every new runsheet; a saved pin only counts
for a caller that sends none.

The same service also had "Prayer and Ministry" twice, and only the first
got the template's screen: the model tags a section once.
"""
import io
import json

import pytest

from propresenterrunsheet.propresenter.templates import (
    auto_detect_template_uuid, share_repeated_links,
)

TEMPLATES = [
    {"uuid": "u-youth", "name": "Youth Service - Library"},
    {"uuid": "u-ya",    "name": "Young Adults Service - Library"},
    {"uuid": "u-sun",   "name": "Sunday Morning Library"},
    {"uuid": "u-jy",    "name": "Junior Youth Library"},
]


# ── Matching ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hint, want", [
    # The reported masthead: its date must not vote for the Sunday template.
    ("ya_runsheet.pdf Sunday, 27 September, 2026 4:00 PM Young Adults Service", "u-ya"),
    ("Young Adult", "u-ya"),                 # singular vs the template's plural
    ("YA Night", "u-ya"),                    # the short form
    ("Friday, 21 August, 2026 Youth", "u-youth"),
    ("Youth Service", "u-youth"),            # all of "Youth", half of "Junior Youth"
    ("Junior Youth", "u-jy"),
    ("Sunday Morning Service 27th Sept", "u-sun"),
])
def test_auto_picks_the_template_that_names_the_service(hint, want):
    assert auto_detect_template_uuid(TEMPLATES, hint=hint) == want


def test_a_weekday_that_is_not_part_of_a_date_still_counts():
    assert auto_detect_template_uuid(TEMPLATES, hint="Sunday Morning") == "u-sun"


@pytest.mark.parametrize("hint, want", [
    ("Sunday 10am Service", "u-10"),     # a time is not a date: "sunday" stays
    ("Sunday 6pm Service", "u-6"),       # templates told apart by time alone
    ("Friday Juniors", "u-fj"),          # "jun" in Juniors is not June
])
def test_times_and_look_alike_words_are_not_dates(hint, want):
    templates = [{"uuid": "u-10", "name": "Sunday 10am Library"},
                 {"uuid": "u-6",  "name": "Sunday 6pm Library"},
                 {"uuid": "u-fj", "name": "Friday Juniors Library"},
                 {"uuid": "u-sj", "name": "Sunday Juniors Library"}]
    assert auto_detect_template_uuid(templates, hint=hint) == want


def test_the_tie_break_does_not_depend_on_playlist_order():
    assert auto_detect_template_uuid(TEMPLATES[::-1], hint="Youth") == "u-youth"


# ── Repeated sections ────────────────────────────────────────────────────────

SECTION = {"header": {"name": "Prayer and Ministry", "uuid": "h", "color": {}},
           "items": [{"name": "Ministry Screen", "uuid": "m", "type": "media"}]}


def test_a_repeated_part_of_the_service_gets_the_same_media():
    items = [{"title": "Prayer and Ministry", "type": "prayer", "library_match": SECTION},
             {"title": "Message", "type": "message", "library_match": None},
             {"title": "Prayer & Ministry", "type": "prayer", "library_match": None}]
    assert share_repeated_links(items) == 1
    assert items[2]["library_match"] == SECTION
    assert items[2]["library_match"] is not SECTION     # a copy, not a shared dict
    assert items[1]["library_match"] is None


def test_songs_are_left_to_the_song_matcher():
    items = [{"title": "Worship Time", "type": "prayer", "library_match": SECTION},
             {"title": "Worship Time", "type": "song", "library_match": None}]
    assert share_repeated_links(items) == 0


@pytest.mark.parametrize("first, second", [
    ("Video", "Video"),                  # one word can name two different parts
    ("Video 1", "Video 2"),              # one word apart is a different item
    ("Prayer and Ministry", "Ministry Time"),
])
def test_only_the_same_multi_word_title_shares_a_link(first, second):
    items = [{"title": first, "type": "video", "library_match": SECTION},
             {"title": second, "type": "video", "library_match": None}]
    assert share_repeated_links(items) == 0


# ── Parse route ──────────────────────────────────────────────────────────────

@pytest.fixture
def pp(client, monkeypatch):
    """ProPresenter with a youth and a young adults template; the young
    adults one has a "Prayer and Ministry" section."""
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "extract_pdf_text",
                        lambda _p: "Young Adults Service\n4:00 PM Prayer and Ministry")
    monkeypatch.setattr(parse_mod, "fetch_catalogue", lambda *_a, **_k: None)
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists",
                        lambda *_a, **_k: list(TEMPLATES[:2]))
    monkeypatch.setattr(parse_mod, "fetch_pp_playlist_items", lambda *_a, **_k: [
        {"id": {"name": "Prayer and Ministry", "uuid": "hdr", "index": 0},
         "type": "header", "header_color": {}},
        {"id": {"name": "Ministry Screen", "uuid": "it", "index": 1},
         "type": "media", "target_uuid": "med"}])
    # A pin saved last week, for the youth template.
    monkeypatch.setattr(parse_mod, "load_settings",
                        lambda: {"template_playlist_uuid": "u-youth"})
    return client


def _parse(client, form_extra=None, titles=("Prayer and Ministry", "Message",
                                            "Prayer & Ministry")):
    import requests
    reply = json.dumps({
        "service_name": "YA 27 Sep", "service_type": "Young Adults",
        "items": [{"title": t, "type": "mc_on_stage",
                   "library_match": "Prayer and Ministry" if i == 0 else ""}
                  for i, t in enumerate(titles)]})

    class _R:
        status_code = 200
        def json(self):
            return {"model": "test/model:free",
                    "choices": [{"message": {"content": reply}}]}
        def raise_for_status(self):
            return None

    orig = requests.post
    requests.post = lambda *a, **k: _R()
    try:
        return client.post("/api/upload_and_parse", data={
            "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "runsheet.pdf"),
            "or_key": "sk-or-test", "or_model": "test/model:free",
            **(form_extra or {}),
        }, content_type="multipart/form-data").get_json()
    finally:
        requests.post = orig


def test_the_pages_auto_beats_a_pin_left_in_settings(pp):
    body = _parse(pp, {"template_playlist_uuid": ""})
    assert body["template"]["uuid"] == "u-ya"
    assert body["template"]["pinned"] is False


def test_a_pick_sent_with_the_parse_is_used_and_reported_as_pinned(pp):
    body = _parse(pp, {"template_playlist_uuid": "u-youth"})
    assert body["template"]["uuid"] == "u-youth"
    assert body["template"]["pinned"] is True


def test_every_repeat_of_a_section_is_populated(pp):
    body = _parse(pp, {"template_playlist_uuid": ""})
    links = [it.get("library_match") for it in body["items"]]
    assert links[0] and links[2], links
    assert links[2]["header"]["name"] == "Prayer and Ministry"
    assert links[1] is None


def test_the_log_names_a_picked_template(pp, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="pp_runsheet"):
        _parse(pp, {"template_playlist_uuid": "u-youth"})
    assert "Template: u-youth (picked by the operator)" in caplog.text


def test_the_service_name_is_the_label_when_the_model_gives_no_type(pp, monkeypatch):
    """The banner, Re-match and create resolve from `service_label`; it
    must be the hint parse actually matched on, or they can disagree."""
    import requests
    reply = json.dumps({"service_name": "Young Adults Night", "items": [
        {"title": "Welcome", "type": "mc_on_stage"}]})

    class _R:
        status_code = 200
        def json(self):
            return {"model": "m", "choices": [{"message": {"content": reply}}]}
        def raise_for_status(self):
            return None

    monkeypatch.setattr(requests, "post", lambda *a, **k: _R())
    body = pp.post("/api/upload_and_parse", data={
        "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "runsheet.pdf"),
        "or_key": "sk-or-test", "or_model": "m", "template_playlist_uuid": ""},
        content_type="multipart/form-data").get_json()
    assert body["template"]["service_label"] == "Young Adults Night"
    assert body["template"]["uuid"] == "u-ya"


def test_rematch_on_auto_ignores_a_pin_left_in_settings(pp, monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod
    used = []
    monkeypatch.setattr(parse_mod, "link_items_to_template",
                        lambda _p, _b, tmpl, **_k: used.append(tmpl) or 0)
    pp.post("/api/match", json={
        "parsed": [{"title": "Prayer and Ministry", "type": "prayer"}],
        "library": [], "rematch_template": True, "matching": True,
        "template_playlist_uuid": "", "service_label": "Young Adults"})
    assert used == ["u-ya"]           # Auto's answer, not the saved youth pin


@pytest.mark.parametrize("playlists, kept", [
    (TEMPLATES[:2], False),   # PP answered; no template is for Kids Church
    ([], True),               # PP couldn't be read: keep what's linked
])
def test_rematch_to_an_auto_that_declines_drops_the_old_links(pp, monkeypatch,
                                                              playlists, kept):
    """Switching a pinned parse back to Auto, for a service with no
    template, must not leave the pinned template's media to be built."""
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists", lambda *_a, **_k: list(playlists))
    items = pp.post("/api/match", json={
        "parsed": [{"title": "Prayer and Ministry", "type": "prayer",
                    "library_match": SECTION}],
        "library": [], "rematch_template": True, "matching": True,
        "template_playlist_uuid": "", "service_label": "Kids Church"}).get_json()["items"]
    assert bool(items[0]["parsed"]["library_match"]) is kept


def test_the_prompt_says_a_section_can_match_twice(app_module):
    from propresenterrunsheet.parsing.ai import LIBRARY_CONTEXT_ADDENDUM
    assert "more than one item" in LIBRARY_CONTEXT_ADDENDUM


# ── /api/template/auto — the Step 1 banner's answer ──────────────────────────

@pytest.fixture
def auto(client, monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists",
                        lambda *_a, **_k: list(TEMPLATES[:2]))
    return lambda **body: client.post("/api/template/auto", json=body).get_json()


def test_the_banner_reads_the_heading_before_the_parse(auto):
    got = auto(filename="runsheet.pdf",
               text="Sunday, 27 September, 2026\n4:00 PM\nYoung Adults Service\n"
                    "4:00 PM  5  Welcome")
    assert got == {"uuid": "u-ya", "name": "Young Adults Service - Library",
                   "declined": False}


def test_the_banner_uses_the_models_reading_after_the_parse(auto):
    assert auto(filename="x.pdf", text="", service_label="Youth")["uuid"] == "u-youth"
    got = auto(filename="x.pdf", text="", service_label="Kids Church")
    assert got["uuid"] == "" and got["declined"] is True
