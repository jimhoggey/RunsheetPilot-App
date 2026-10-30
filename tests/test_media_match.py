"""Media straight from the bin, scoped to the service the runsheet is for.

Owner's spec (30 Oct 2026): Summit is a youth event with no template, and
its media sits in folders like "Summit 2026 Friday". A Summit runsheet
must draw on those and must NOT reach "Sunday Service Media" or the youth
screens. A Sunday runsheet reads its own folders, and still reaches
Summit's when a line says "promo Summit". Code narrows, the model decides
— the five steps are drawn at the top of propresenter/media_match.py.
"""
import io
import json

import pytest

from propresenterrunsheet.propresenter.media_match import (
    MAX_CANDIDATES, MAX_PER_ITEM, eligible_folders, recommended_folders,
    row_for_service, scope, sections, tokens,
)
from propresenterrunsheet.settings import load_settings, save_settings

# The owner's bin as described: Summit's own folders, the weekly
# services' folders, a working folder, and an old event.
BIN = [
    {"uuid": "F1", "name": "WTSSF_Summit2026_Opener", "folder": "Summit 2026 Friday"},
    {"uuid": "F2", "name": "Summit Friday QR", "folder": "Summit 2026 Friday"},
    {"uuid": "A1", "name": "Summit Saturday Opener", "folder": "Summit 2026 Saturday"},
    {"uuid": "P1", "name": "Summit pre-service loop", "folder": "Summit pre-service"},
    {"uuid": "S1", "name": "C3 Over Canberra hold", "folder": "Sunday Service Media"},
    {"uuid": "S2", "name": "Giving QR", "folder": "Sunday Service Media"},
    {"uuid": "C1", "name": "Hold screen", "folder": "Current media"},
    {"uuid": "Y1", "name": "Youth bumper", "folder": "Youth media"},
    {"uuid": "E1", "name": "Easter sunrise opener", "folder": "Easter 2024"},
]
TEMPLATES = ["Sunday Morning Library", "Youth Service - Library"]
SUMMIT = [{"title": "Pre-service loop"}, {"title": "Opener film"}]
SUNDAY = [{"title": "Welcome"},
          {"title": "Announcements", "notes": "promo Summit 2026"},
          {"title": "Offering", "notes": "giving slide up"}]
SUMMIT_FOLDERS = {"Summit 2026 Friday", "Summit 2026 Saturday", "Summit pre-service"}


def _names(folders):
    return [f["name"] for f in folders]


# ── One answer to "which service is this" ───────────────────────────────────

@pytest.mark.parametrize("name, want", [
    ("Sunday Service Media", {"sunday"}),
    ("Summit 2026 Saturday", {"summit", "2026", "saturday"}),
    ("Summit after service", {"summit"}),
    ("Current media", set()),             # names no service at all
    ("Pre-service loops", set()),         # where in a service, not which
])
def test_a_folder_name_reduces_to_the_service_it_is_for(name, want):
    assert tokens(name) == want


def test_template_detection_reads_names_exactly_as_it_did():
    """Folder fillers are passed in, never added to the shared set, so the
    template picker this tokeniser has always served is unchanged."""
    from propresenterrunsheet.propresenter.templates import _template_signal_tokens
    assert _template_signal_tokens("Youth Service - Library") == {"youth"}
    assert _template_signal_tokens("Sunday Media Library") == {"sunday", "media"}


# ── Step 1: narrow — another service's folders never reach the model ────────

def test_a_summit_runsheet_never_sees_the_sunday_or_youth_folders():
    got = _names(eligible_folders(BIN, "Summit", SUMMIT, [], TEMPLATES))
    assert "Sunday Service Media" not in got
    assert "Youth media" not in got


def test_summits_own_folders_lead_so_a_cap_cuts_the_irrelevant_end():
    got = _names(eligible_folders(BIN, "Summit", SUMMIT, [], TEMPLATES))
    assert set(got[:3]) == SUMMIT_FOLDERS


def test_folders_that_name_no_service_are_left_for_the_model_to_judge():
    got = _names(eligible_folders(BIN, "Summit", SUMMIT, [], TEMPLATES))
    assert {"Current media", "Easter 2024"} <= set(got)


def test_a_line_naming_another_service_makes_its_folders_eligible_only():
    """"youth band" names youth; whether it WANTS youth screens is the
    model's call, so the folder is shown to it — nothing more."""
    items = SUMMIT + [{"title": "Worship", "notes": "youth band"}]
    got = _names(eligible_folders(BIN, "Summit", items, [], TEMPLATES))
    assert "Youth media" in got
    assert "Sunday Service Media" not in got, "nobody mentioned Sunday"


def test_promo_summit_reaches_summits_folders_once_summit_is_known():
    rows = [{"match": "Summit", "folders": ["Summit 2026 Friday"]}]
    asked = _names(eligible_folders(BIN, "Sunday Morning", SUNDAY, rows, TEMPLATES))
    quiet = _names(eligible_folders(BIN, "Sunday Morning", SUNDAY[:1], rows, TEMPLATES))
    assert "Summit 2026 Friday" in asked
    assert "Summit 2026 Friday" not in quiet


# ── The operator's saved folders ────────────────────────────────────────────

ROWS = [{"match": "Sunday", "folders": ["Current media", "Sunday Service Media"]},
        {"match": "Summit 2026", "folders": ["Summit 2026 Friday"]},
        {"match": "Youth", "folders": ["Youth media"]}]


def test_a_saved_row_applies_to_its_service():
    assert row_for_service(ROWS, "Sunday Morning", "Sunday Service — 3 May")["match"] == "Sunday"


def test_a_rows_year_can_come_from_the_dated_title():
    assert row_for_service(ROWS, "Summit", "Summit 2026 — Saturday")["match"] == "Summit 2026"


def test_a_sunday_row_never_claims_a_summit_night_that_falls_on_a_sunday():
    """Sunday is when it is, not what it is."""
    assert row_for_service(ROWS[:1], "Summit", "Summit 2026 — Sunday Night") == {}


def test_the_most_specific_row_wins():
    rows = [{"match": "Summit", "folders": ["a"]}, {"match": "Summit 2026", "folders": ["b"]}]
    assert row_for_service(rows, "Summit", "Summit 2026 Friday")["folders"] == ["b"]


@pytest.mark.parametrize("rows", [
    None, [], ["junk"], [{"match": ""}], [{"match": "Service"}], [{"folders": ["x"]}],
])
def test_a_row_that_names_no_service_never_applies(rows):
    assert row_for_service(rows, "Sunday", "Sunday") == {}


# ── Without a model answer: name matching, this service only ───────────────

def test_without_a_model_only_this_services_own_folders_stand_in():
    assert set(recommended_folders(BIN, "Summit", [], TEMPLATES)) == SUMMIT_FOLDERS


def test_the_more_specific_folder_name_leads():
    bin_items = BIN + [{"uuid": "X", "name": "x", "folder": "Summit"}]
    got = recommended_folders(bin_items, "Summit", [], TEMPLATES)
    assert got.index("Summit") > got.index("Summit 2026 Friday")
    assert got.index("Summit") > got.index("Summit 2026 Saturday")


def test_a_service_with_no_name_recommends_nothing():
    assert recommended_folders(BIN, "", [], TEMPLATES) == []


# ── Step 3: what is offered, and who chose it ──────────────────────────────

def test_a_saved_row_is_final_even_over_the_model():
    rows = [{"match": "Summit", "folders": ["Youth media"]}]     # deliberately
    sc = scope(BIN, "Summit", SUMMIT, rows, TEMPLATES, suggested=["Summit 2026 Friday"])
    assert (sc["source"], sc["folders"], sc["service"]) == ("chosen", ["Youth media"], "Summit")
    assert [a["uuid"] for a in sc["assets"]] == ["Y1"]


def test_the_models_folders_are_used_in_its_order():
    sc = scope(BIN, "Summit", SUMMIT, [], TEMPLATES,
               suggested=["Summit pre-service", "Summit 2026 Friday"])
    assert sc["source"] == "suggested"
    assert [a["uuid"] for a in sc["assets"]] == ["P1", "F1", "F2"]


def test_the_model_cannot_choose_another_services_folder_or_invent_one():
    sc = scope(BIN, "Summit", SUMMIT, [], TEMPLATES,
               suggested=["Sunday Service Media", "Summit 2026 Friday", "Not a folder"])
    assert sc["folders"] == ["Summit 2026 Friday"]


@pytest.mark.parametrize("suggested", [None, [], ["Sunday Service Media"]])
def test_with_no_usable_model_answer_folder_names_stand_in(suggested):
    sc = scope(BIN, "Summit", SUMMIT, [], TEMPLATES, suggested=suggested)
    assert sc["source"] == "matched"
    assert set(sc["folders"]) == SUMMIT_FOLDERS


def test_nothing_for_this_service_offers_nothing_rather_than_the_whole_bin():
    """Owner, 30 Oct 2026: sending every title is wasteful with a low
    chance of success. Nothing matching is said, not papered over."""
    sc = scope(BIN, "Carols by Candlelight", [{"title": "Welcome"}], [], TEMPLATES)
    assert sc == {"assets": [], "folders": [], "source": "none", "service": ""}


@pytest.mark.parametrize("bad", [
    [], None, [{"name": "no uuid", "folder": "Summit"}],
    [{"uuid": "U", "name": "  ", "folder": "Summit"}], ["junk"],
])
def test_a_bin_with_nothing_usable_offers_nothing(bad):
    assert scope(bad, "Summit")["assets"] == []


def test_the_budget_is_shared_across_the_runsheet():
    """Ranked once against the service name, a big folder gave every row
    to whatever looked most like the service. The QR the offering asks
    for sits last in a 400-file folder and is still offered."""
    big = [{"uuid": f"L{i}", "name": f"Loop {i}", "folder": "Summit 2026 Friday"}
           for i in range(399)]
    big.append({"uuid": "QR", "name": "Giving QR code", "folder": "Summit 2026 Friday"})
    sc = scope(big, "Summit", [{"title": "Pre-service loop"},
                               {"title": "Offering", "notes": "giving QR"}],
               suggested=["Summit 2026 Friday"])
    uuids = [a["uuid"] for a in sc["assets"]]
    assert len(uuids) == MAX_CANDIDATES and "QR" in uuids


# ── Step 5: what the model is allowed to have said ─────────────────────────

ITEMS = [{"title": "Countdown"}, {"title": "Opener Film"}, {"title": "Connect QR"}]
OFFERED = [{"uuid": "U1", "name": "WTSSF_Summit2026_Opener", "folder": "Summit 2026"},
           {"uuid": "U2", "name": "WTSSF_Summit2026_QR", "folder": "Summit 2026"},
           {"uuid": "U3", "name": "Summit Countdown", "folder": "Summit 2026"}]


def test_each_pick_becomes_a_header_plus_its_media_marked_as_the_bins():
    out = sections(ITEMS, [{"media": 2, "item": 0}, {"media": 0, "item": 1}], OFFERED)
    assert out[0]["header"]["name"] == "Countdown"
    assert out[0]["items"] == [{"name": "Summit Countdown", "uuid": "U3",
                                "target_uuid": "U3", "type": "media", "index": 0}]
    assert out[0]["from_bin"] is True, "so a re-run replaces it and not a template's"
    assert [e["uuid"] for e in out[1]["items"]] == ["U1"]
    assert 2 not in out


def test_several_assets_can_sit_under_one_item_in_the_order_given():
    out = sections(ITEMS, [{"media": 0, "item": 1}, {"media": 1, "item": 1}], OFFERED)
    assert [(e["uuid"], e["index"]) for e in out[1]["items"]] == [("U1", 0), ("U2", 1)]


def test_an_item_a_template_already_filled_is_left_alone():
    items = [{"title": "Countdown", "library_match": {"header": {}, "items": [{}]}}]
    assert sections(items, [{"media": 2, "item": 0}], OFFERED) == {}


@pytest.mark.parametrize("picks", [
    [{"media": 99, "item": 0}], [{"media": 0, "item": 99}], [{"media": 0, "item": -1}],
    [{"media": True, "item": 0}], [{"media": "0", "item": 0}],
    ["nonsense", None, 7], [], None, "not a list",
])
def test_an_answer_that_doesnt_hold_up_is_dropped(picks):
    assert sections(ITEMS, picks, OFFERED) == {}


def test_one_asset_can_serve_several_segments():
    """Owner, 28 Oct 2026: a hold screen belongs in more than one place."""
    out = sections(ITEMS, [{"media": 0, "item": 0}, {"media": 0, "item": 1}], OFFERED)
    assert [e["uuid"] for e in out[0]["items"]] == ["U1"]
    assert [e["uuid"] for e in out[1]["items"]] == ["U1"]


def test_but_never_twice_under_the_same_header():
    out = sections(ITEMS, [{"media": 0, "item": 0}, {"media": 0, "item": 0}], OFFERED)
    assert [e["uuid"] for e in out[0]["items"]] == ["U1"]


def test_an_item_takes_only_so_much_media():
    big = [{"uuid": f"U{i}", "name": f"Summit {i}"} for i in range(10)]
    out = sections([{"title": "Opener"}], [{"media": i, "item": 0} for i in range(10)], big)
    assert len(out[0]["items"]) == MAX_PER_ITEM


# ── Through the parse route ─────────────────────────────────────────────────

def _route(monkeypatch, items, service_type="", folders=None, picks=None, bin_items=BIN):
    """The parse model returns `items`; the folder and media calls are
    stubbed, and what each was handed is recorded in the returned dict."""
    import requests
    import propresenterrunsheet.routes.parse as parse_mod

    class _R:
        status_code = 200
        def json(self):
            return {"model": "m", "choices": [{"message": {"content": json.dumps(
                {"items": items, "service_type": service_type})}}]}
        def raise_for_status(self):
            return None
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R())
    monkeypatch.setattr(parse_mod, "pp_base", lambda *a, **k: "http://pp")
    monkeypatch.setattr(parse_mod, "fetch_media_bin", lambda base: bin_items)
    seen = {}

    def _folders(service, items, eligible, key, model, title="", post=None):
        seen["service"], seen["title"] = service, title
        seen["eligible"] = _names(eligible)
        return folders or []

    def _media(items, offered, key, model, post=None):
        seen["offered"] = [a["uuid"] for a in offered]
        return picks or []
    monkeypatch.setattr(parse_mod, "plan_folders", _folders)
    monkeypatch.setattr(parse_mod, "plan_media", _media)
    return seen


def _upload(client, filename, **extra):
    return client.post("/api/upload_and_parse", data={
        "pdf": (io.BytesIO(b"%PDF-1.4 fake"), filename),
        "or_key": "k", "or_model": "m", **extra},
        content_type="multipart/form-data").get_json()


def test_with_no_template_the_model_picks_folders_then_files(parse_client, monkeypatch):
    save_settings({"media_folders_by_service": [ROWS[0]]})   # Sunday is a known service
    seen = _route(monkeypatch, [{"title": "Opener Film", "type": "video"},
                                {"title": "Connect QR", "type": "announcement"}],
                  service_type="Summit", folders=["Summit 2026 Friday"],
                  picks=[{"media": 0, "item": 0}, {"media": 1, "item": 1}])
    r = _upload(parse_client, "Summit 2026.pdf")
    assert seen["service"] == "Summit"
    assert "Sunday Service Media" not in seen["eligible"], "narrowed before the model"
    assert seen["offered"] == ["F1", "F2"], "only the folder the model chose"
    assert [it["library_match"]["items"][0]["target_uuid"] for it in r["items"]] == ["F1", "F2"]
    got = r["media_from_bin"]
    assert (got["count"], got["source"], got["folders"]) == (2, "suggested", ["Summit 2026 Friday"])
    assert {"name": "Summit 2026 Friday", "count": 2} in got["bin_folders"]


def test_saved_folders_skip_the_folder_call(parse_client, monkeypatch):
    save_settings({"media_folders_by_service": [
        {"match": "Summit", "folders": ["Summit pre-service"]}]})
    seen = _route(monkeypatch, [{"title": "Pre-service loop", "type": "other"}],
                  service_type="Summit", picks=[{"media": 0, "item": 0}])
    r = _upload(parse_client, "Summit 2026.pdf")
    assert "eligible" not in seen, "the operator's choice is final; no model asked"
    assert seen["offered"] == ["P1"]
    got = r["media_from_bin"]
    assert (got["source"], got["saved_as"], got["service"]) == ("chosen", "Summit", "Summit")


def test_a_failed_folder_call_falls_back_to_folder_names(parse_client, monkeypatch):
    seen = _route(monkeypatch, [{"title": "Opener Film", "type": "video"}],
                  service_type="Summit", folders=[])
    r = _upload(parse_client, "Summit 2026.pdf")
    assert r["media_from_bin"]["source"] == "matched"
    assert set(seen["offered"]) == {"F1", "F2", "A1", "P1"}


def test_a_template_leaves_the_media_bin_on_for_the_gaps(parse_client, monkeypatch):
    """A template is the screens there every week; "promo Summit" wants
    one the Sunday template has no reason to carry."""
    seen = _route(monkeypatch, [{"title": "Welcome", "type": "welcome"},
                                {"title": "Announcements", "type": "announcement",
                                 "notes": "promo Summit 2026"}],
                  service_type="Sunday Morning", folders=["Summit 2026 Friday"],
                  picks=[{"media": 0, "item": 1}])
    r = _upload(parse_client, "Sunday Service.pdf", template_playlist_uuid="TPL-SUNDAY")
    assert "Summit 2026 Friday" in seen["eligible"]
    assert r["items"][1]["library_match"]["items"][0]["target_uuid"] == "F1"


def test_a_media_bin_that_cannot_be_read_just_leaves_the_headers(parse_client, monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod
    _route(monkeypatch, [{"title": "Opener Film", "type": "video"}], service_type="Summit")

    def _boom(base):
        raise RuntimeError("ProPresenter went away")
    monkeypatch.setattr(parse_mod, "fetch_media_bin", _boom)
    r = _upload(parse_client, "Summit 2026.pdf")
    assert r["items"][0]["library_match"] is None
    assert (r["media_from_bin"]["count"], r["media_from_bin"]["source"]) == (0, "none")


# ── Placing again after the operator saves folders ─────────────────────────

def test_placing_again_replaces_the_bins_own_picks_and_never_a_templates(client, monkeypatch):
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "pp_base", lambda *a, **k: "http://pp")
    monkeypatch.setattr(parse_mod, "fetch_media_bin", lambda base: BIN)
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists", lambda base: [])
    monkeypatch.setattr(parse_mod, "plan_folders",
                        lambda *a, **k: pytest.fail("saved folders are final"))
    monkeypatch.setattr(parse_mod, "plan_media",
                        lambda items, offered, *a, **k: [{"media": 0, "item": 1}])
    save_settings({"or_key": "k", "media_folders_by_service": [
        {"match": "Summit", "folders": ["Summit 2026 Saturday"]}]})
    template = {"header": {"name": "Welcome", "uuid": "H"}, "items": [{"name": "T"}]}
    stale = {"header": {"name": "Opener"}, "items": [{"name": "old"}], "from_bin": True}
    r = client.post("/api/media/place", json={
        "parsed": [{"title": "Welcome", "library_match": template},
                   {"title": "Opener", "library_match": stale}],
        "service": "Summit", "title": "Summit 2026 — Saturday"}).get_json()
    assert r["items"][0]["library_match"] == template
    assert r["items"][1]["library_match"]["items"][0]["uuid"] == "A1"
    assert r["media_from_bin"]["source"] == "chosen"


def test_placing_again_while_propresenter_is_away_removes_nothing(client, monkeypatch):
    """The bin's earlier picks used to be cleared BEFORE the bin was read,
    so pressing Save while PP restarted wiped every slide the parse had
    placed and put nothing back."""
    import propresenterrunsheet.routes.parse as parse_mod
    monkeypatch.setattr(parse_mod, "pp_base", lambda *a, **k: "http://pp")
    monkeypatch.setattr(parse_mod, "fetch_media_bin", lambda base: [])
    placed = {"header": {"name": "Opener"}, "items": [{"name": "F1"}], "from_bin": True}
    r = client.post("/api/media/place", json={
        "parsed": [{"title": "Opener", "library_match": placed}], "service": "Summit"})
    assert r.status_code == 503
    assert "unchanged" in r.get_json()["error"]


@pytest.mark.parametrize("body", [{}, {"parsed": "x"}, {"parsed": None}, ["parsed"]])
def test_placing_media_needs_a_runsheet(client, body):
    assert client.post("/api/media/place", json=body).status_code == 400


@pytest.mark.parametrize("body", [["Summit"], "Summit", 7])
def test_saving_folders_needs_an_object(client, body):
    assert client.post("/api/media_folders", json=body).status_code == 400


# ── Saving a service's folders ─────────────────────────────────────────────

def _rows():
    return {r["match"]: r["folders"]
            for r in load_settings()["media_folders_by_service"]}


def test_saving_upserts_one_services_row(client):
    client.post("/api/media_folders", json={"service": "Summit",
                                            "folders": ["Summit 2026 Friday"]})
    client.post("/api/media_folders", json={"service": "Sunday",
                                            "folders": ["Sunday Service Media"]})
    r = client.post("/api/media_folders", json={
        "service": " summit ",
        "folders": ["Summit 2026 Saturday", "Summit 2026 Saturday", 7, "  ", None]})
    assert r.status_code == 200
    assert _rows() == {"Sunday": ["Sunday Service Media"],
                       "summit": ["Summit 2026 Saturday"]}


def test_saving_no_folders_hands_the_service_back_to_the_model(client):
    client.post("/api/media_folders", json={"service": "Summit", "folders": ["a"]})
    client.post("/api/media_folders", json={"service": "Summit", "folders": []})
    assert _rows() == {}


@pytest.mark.parametrize("service", ["", "   ", "Service", None, ["Summit"]])
def test_a_row_must_name_a_service(client, service):
    r = client.post("/api/media_folders", json={"service": service, "folders": ["a"]})
    assert r.status_code == 400
    assert not load_settings().get("media_folders_by_service")


def test_saved_folders_are_bounded(client):
    client.post("/api/media_folders", json={
        "service": "Summit", "folders": [f"F{i}" for i in range(80)] + ["x" * 500]})
    folders = _rows()["Summit"]
    assert len(folders) == 50
    assert all(len(f) <= 200 for f in folders)
