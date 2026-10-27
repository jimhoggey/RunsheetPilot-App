"""Media straight from the bin, for an event with no template.

Owner's spec (27 Oct 2026): Summit is a one-off, so there is no template
playlist to copy. The media exists though — smart folders named for the
event ("Summit 2026") pull it into ProPresenter's Media bin as it is
produced. So: read the bin, work out which asset belongs to which
runsheet item, and place it inline when the playlist is built.
"""
import pytest

from propresenterrunsheet.propresenter.media_match import (
    MAX_PER_ITEM, candidates, sections,
)

BIN = [
    {"uuid": "U1", "name": "WTSSF_Summit2026_Opener_1920x1080", "folder": "Summit 2026"},
    {"uuid": "U2", "name": "WTSSF_Summit2026_Screen_QR_1920x1080", "folder": "Summit 2026"},
    {"uuid": "U3", "name": "Summit Countdown", "folder": "Summit 2026"},
    {"uuid": "U4", "name": "Welcome", "folder": "Sunday Loops"},
    {"uuid": "U5", "name": "Giving QR", "folder": "Sunday Loops"},
]

ITEMS = [{"title": "Countdown"}, {"title": "Opener Film"}, {"title": "Connect QR"}]


# ── Which assets are even offered ───────────────────────────────────────────

def test_a_folder_named_for_the_event_is_the_whole_candidate_list():
    """The point of the smart folders: a Sunday slide can never land in a
    Summit playlist, because it is never offered."""
    assert [a["uuid"] for a in candidates(BIN, "Summit 2026")] == ["U1", "U2", "U3"]


@pytest.mark.parametrize("event", ["Summit 2026", "SUMMIT 2026", "C3B Summit 2026 — Night 1"])
def test_the_event_name_need_not_be_written_the_same_way(event):
    assert {a["folder"] for a in candidates(BIN, event)} == {"Summit 2026"}


def test_with_no_folder_for_the_event_the_whole_bin_is_offered_best_first():
    """An operator who hasn't named a folder for it still gets help; the
    assets whose own names echo the event come first."""
    unsorted_bin = [
        {"uuid": "A", "name": "Welcome", "folder": "Media"},
        {"uuid": "B", "name": "Easter Sunrise Opener", "folder": "Media"},
        {"uuid": "C", "name": "Giving QR", "folder": "Media"},
    ]
    got = candidates(unsorted_bin, "Easter Sunrise")
    assert len(got) == 3                                  # nothing ruled out
    assert got[0]["uuid"] == "B"                          # but the echo leads


def test_an_event_nobody_has_media_for_still_returns_something_usable():
    assert len(candidates(BIN, "Carols by Candlelight")) == len(BIN)


@pytest.mark.parametrize("bad", [
    [], None, [{"name": "no uuid"}], [{"uuid": "U", "name": "  "}], ["junk"],
])
def test_a_bin_with_nothing_usable_offers_nothing(bad):
    assert candidates(bad, "Summit 2026") == []


def test_the_candidate_list_is_capped_for_the_prompt():
    big = [{"uuid": f"U{i}", "name": f"Slide {i}", "folder": "Other"} for i in range(400)]
    assert len(candidates(big, "Summit 2026")) == 150


# ── What the model is allowed to have said ──────────────────────────────────

def _offered():
    return candidates(BIN, "Summit 2026")


def test_each_pick_becomes_a_header_plus_its_media():
    out = sections(ITEMS, [{"media": 2, "item": 0}, {"media": 0, "item": 1}], _offered())
    assert out[0]["header"]["name"] == "Countdown"
    assert out[0]["items"] == [{"name": "Summit Countdown", "uuid": "U3",
                                "target_uuid": "U3", "type": "media", "index": 0}]
    assert [e["uuid"] for e in out[1]["items"]] == ["U1"]
    assert 2 not in out                      # nothing picked for it, no empty section


def test_several_assets_can_sit_under_one_item_in_the_order_given():
    out = sections(ITEMS, [{"media": 0, "item": 1}, {"media": 1, "item": 1}], _offered())
    assert [(e["uuid"], e["index"]) for e in out[1]["items"]] == [("U1", 0), ("U2", 1)]


def test_an_item_a_template_already_filled_is_left_alone():
    """Media-bin matching fills gaps; it never argues with a template."""
    items = [{"title": "Countdown", "library_match": {"header": {}, "items": [{}]}}]
    assert sections(items, [{"media": 2, "item": 0}], _offered()) == {}


@pytest.mark.parametrize("picks", [
    [{"media": 99, "item": 0}],                      # no such asset
    [{"media": 0, "item": 99}],                      # no such item
    [{"media": 0, "item": -1}],
    [{"media": True, "item": 0}],                    # a bool is not an index
    [{"media": "0", "item": 0}],
    ["nonsense", None, 7],
    [],
    None,
    "not a list",
])
def test_an_answer_that_doesnt_hold_up_is_dropped(picks):
    assert sections(ITEMS, picks, _offered()) == {}


def test_one_asset_is_never_placed_twice():
    out = sections(ITEMS, [{"media": 0, "item": 0}, {"media": 0, "item": 1}], _offered())
    assert list(out) == [0]


# ── Through the parse route ─────────────────────────────────────────────────

def _parsed(monkeypatch, items):
    import requests

    class _R:
        status_code = 200
        def json(self):
            return {"model": "m", "choices": [{"message": {
                "content": __import__("json").dumps({"items": items})}}]}
        def raise_for_status(self):
            return None
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R())


def test_with_no_template_the_bin_fills_the_playlist(parse_client, monkeypatch):
    """The whole point: a one-off event, no template, and the media still
    lands in the playlist."""
    import propresenterrunsheet.routes.parse as parse_mod
    _parsed(monkeypatch, [{"title": "Opener Film", "type": "video"},
                          {"title": "Connect QR", "type": "announcement"}])
    monkeypatch.setattr(parse_mod, "pp_base", lambda *a, **k: "http://pp")
    monkeypatch.setattr(parse_mod, "fetch_media_bin", lambda base: BIN)
    seen = {}

    def _plan(items, offered, key, model, post=None):
        seen["offered"] = [a["name"] for a in offered]
        return [{"media": 0, "item": 0}, {"media": 1, "item": 1}]
    monkeypatch.setattr(parse_mod, "plan_media", _plan)

    r = parse_client.post("/api/upload_and_parse", data={
        "pdf": (__import__("io").BytesIO(b"%PDF-1.4 fake"), "Summit 2026.pdf"),
        "or_key": "k", "or_model": "m"},
        content_type="multipart/form-data").get_json()

    assert seen["offered"] == ["WTSSF_Summit2026_Opener_1920x1080",
                               "WTSSF_Summit2026_Screen_QR_1920x1080",
                               "Summit Countdown"], "only the event's own folder"
    got = [it["library_match"] for it in r["items"]]
    assert [m["items"][0]["name"] for m in got] == [
        "WTSSF_Summit2026_Opener_1920x1080", "WTSSF_Summit2026_Screen_QR_1920x1080"]
    assert [m["items"][0]["target_uuid"] for m in got] == ["U1", "U2"]
    assert r["media_from_bin"] == {"count": 2, "folder": "Summit 2026"}


def test_a_media_bin_that_cannot_be_read_just_leaves_the_headers(parse_client, monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod
    _parsed(monkeypatch, [{"title": "Opener Film", "type": "video"}])
    monkeypatch.setattr(parse_mod, "pp_base", lambda *a, **k: "http://pp")

    def _boom(base):
        raise RuntimeError("ProPresenter went away")
    monkeypatch.setattr(parse_mod, "fetch_media_bin", _boom)
    r = parse_client.post("/api/upload_and_parse", data={
        "pdf": (__import__("io").BytesIO(b"%PDF-1.4 fake"), "Summit 2026.pdf"),
        "or_key": "k", "or_model": "m"},
        content_type="multipart/form-data").get_json()
    assert r["items"][0]["library_match"] is None
    assert r["media_from_bin"] == {"count": 0, "folder": ""}


def test_an_item_takes_only_so_much_media():
    big = [{"uuid": f"U{i}", "name": f"Summit {i}", "folder": "Summit 2026"}
           for i in range(10)]
    picks = [{"media": i, "item": 0} for i in range(10)]
    out = sections([{"title": "Opener"}], picks, candidates(big, "Summit 2026"))
    assert len(out[0]["items"]) == MAX_PER_ITEM
