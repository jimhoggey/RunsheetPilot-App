"""Settings → Service folders: each ProPresenter template's own media
folders, their jobs, and the church's note on what each holds.

Owner, 21 Nov 2026: list every template; under each, the folders to add
from (pre-service loop, end loop, announcements, preach…); a note per
folder ("one-off events and ones we hold for a while: Connect Dinner,
Barefoot Bowls"). Template links, which this replaced, are gone.
"""
import pytest

import propresenterrunsheet.parsing.media_plan as mp
import propresenterrunsheet.routes.parse as parse_mod
import propresenterrunsheet.routes.settings as settings_routes
from propresenterrunsheet.propresenter.media_match import saved_row, scope
from propresenterrunsheet.settings import load_settings, save_settings

TPL = "1F2E3D4C-0000-4000-8000-00000000ABCD"
BIN = [
    {"uuid": "A1", "name": "Barefoot Bowls", "folder": "Announcements"},
    {"uuid": "L1", "name": "Pre Loop.mp4", "folder": "Pre-service loop"},
    {"uuid": "S1", "name": "Sunday hold", "folder": "Sunday media"},
]
ROWS = [
    {"match": "Sunday Morning Library", "template": TPL,
     "folders": ["Announcements", "Pre-service loop"],
     "notes": {"Announcements": "  one-off events   and ones we run for a while "}},
    {"match": "Sunday", "folders": ["Sunday media"]},
]


def test_the_matched_templates_folders_win_over_a_name_match():
    assert saved_row(ROWS, "Sunday", "Sunday 23 Nov", TPL)["template"] == TPL
    assert saved_row(ROWS, "Sunday", "Sunday 23 Nov", "")["match"] == "Sunday"
    assert saved_row(ROWS, "Sunday", "", "SOME-OTHER-TEMPLATE")["match"] == "Sunday"


def test_a_templates_notes_reach_the_scope_tidied():
    sc = scope(BIN, "Sunday", [{"title": "Notices"}], ROWS, template=TPL)
    assert sc["source"] == "chosen" and sc["service"] == "Sunday Morning Library"
    assert sc["notes"] == {"Announcements": "one-off events and ones we run for a while"}
    assert [a["uuid"] for a in sc["assets"]] == ["A1"]
    assert [a["uuid"] for a in sc["fixed"]["start"]] == ["L1"]


def test_the_notes_are_put_to_the_ai(openrouter):
    sent = openrouter.say('{"media": []}')
    mp.plan_media([{"title": "Notices"}], [{"uuid": "A1", "name": "Bowls", "job": "every"}],
                  "sk-or-x", "", notes={"Announcements": "one-off events"})
    asked = sent["body"]["messages"][0]["content"]
    assert "WHAT THE CHURCH SAYS ITS FOLDERS HOLD:\n- Announcements: one-off events" in asked


def test_no_notes_means_no_notes_heading(openrouter):
    sent = openrouter.say('{"media": []}')
    mp.plan_media([{"title": "Notices"}], [{"uuid": "A1", "name": "Bowls"}], "sk-or-x", "")
    assert "FOLDERS HOLD" not in sent["body"]["messages"][0]["content"]


# ── saving ──────────────────────────────────────────────────────────────────

def _post(client, **body):
    return client.post("/api/media_folders", json=body)


def _rows():
    return load_settings()["media_folders_by_service"]


def test_one_row_per_template_beside_the_named_services(client):
    _post(client, service="Summit", folders=["Summit 2026"])
    _post(client, service="Sunday Morning Library", template=TPL,
          folders=["Announcements"], jobs={"Announcements": "every"},
          notes={"Announcements": "one-off events", "Not ticked": "x"})
    r = _post(client, service="Sunday Morning Library", template=TPL,
              folders=["Announcements", "Preach"], jobs={"Preach": "preach"},
              notes={"Announcements": "one-off events"})
    assert r.status_code == 200
    rows = _rows()
    assert [r.get("template", "") for r in rows] == ["", TPL]
    assert rows[1]["notes"] == {"Announcements": "one-off events"}
    assert rows[1]["jobs"] == {"Preach": "preach"}


def test_a_save_from_the_step_1_picker_keeps_the_settings_notes(client):
    _post(client, service="Sunday Morning Library", template=TPL,
          folders=["Announcements", "Preach"], notes={"Announcements": "events", "Preach": "slides"})
    _post(client, service="Sunday Morning Library", template=TPL,
          folders=["Announcements"], jobs={"Announcements": "every"})
    assert _rows()[0]["notes"] == {"Announcements": "events"}


def test_no_folders_removes_only_that_templates_row(client):
    _post(client, service="Sunday", folders=["Sunday media"])
    _post(client, service="Sunday Morning Library", template=TPL, folders=["A"])
    _post(client, service="Sunday Morning Library", template=TPL, folders=[])
    assert [r["match"] for r in _rows()] == ["Sunday"]


def test_a_template_needs_no_service_words(client):
    """A template called just "Library" names no service; its uuid does."""
    assert _post(client, service="Library", template=TPL, folders=["A"]).status_code == 200


@pytest.mark.parametrize("template", ["../timer/x", "a b", ["x"], 7])
def test_a_template_must_look_like_a_propresenter_id(client, template):
    assert _post(client, service="Sunday", template=template, folders=["A"]).status_code == 400
    assert not load_settings().get("media_folders_by_service")


# ── what Settings shows ─────────────────────────────────────────────────────

@pytest.fixture
def pp(monkeypatch):
    monkeypatch.setattr(settings_routes, "pp_base", lambda *a, **k: "http://pp")
    monkeypatch.setattr(settings_routes, "fetch_pp_playlists", lambda base: [
        {"uuid": TPL, "name": "Sunday Morning Library"},
        {"uuid": "P2", "name": "Sunday 23 Nov"},            # a playlist, not a template
        {"uuid": "T3", "name": "Youth Service - Template"}])
    monkeypatch.setattr(settings_routes, "fetch_media_bin", lambda base: BIN)


def test_settings_lists_the_templates_their_rows_and_every_folder(client, pp):
    save_settings({"media_folders_by_service": ROWS})
    got = client.get("/api/service_folders").get_json()
    assert got["pp_ok"] is True
    assert [t["name"] for t in got["templates"]] == ["Sunday Morning Library",
                                                    "Youth Service - Template"]
    assert {"name": "Pre-service loop", "count": 1, "job": "start"} in got["bin_folders"]
    assert [r["match"] for r in got["rows"]] == ["Sunday Morning Library", "Sunday"]


def test_with_propresenter_away_settings_still_shows_what_is_saved(client, monkeypatch):
    def _away(*a, **k):
        raise ConnectionError("ProPresenter is closed")
    monkeypatch.setattr(settings_routes, "fetch_pp_playlists", _away)
    save_settings({"media_folders_by_service": ROWS})
    got = client.get("/api/service_folders").get_json()
    assert got["pp_ok"] is False and got["templates"] == []
    assert len(got["rows"]) == 2


# ── placing ─────────────────────────────────────────────────────────────────

def test_placing_uses_the_matched_templates_folders(client, monkeypatch):
    monkeypatch.setattr(parse_mod, "pp_base", lambda *a, **k: "http://pp")
    monkeypatch.setattr(parse_mod, "fetch_media_bin", lambda base: BIN)
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists", lambda base: [])
    monkeypatch.setattr(parse_mod, "plan_folders",
                        lambda *a, **k: pytest.fail("the template's folders are final"))
    asked = {}

    def _media(items, offered, *a, notes=None, **k):
        asked["notes"] = notes
        return {"media": []}
    monkeypatch.setattr(parse_mod, "plan_media", _media)
    save_settings({"or_key": "k", "media_folders_by_service": ROWS})
    r = client.post("/api/media/place", json={
        "parsed": [{"title": "Notices", "type": "announcement"}],
        "service": "Sunday", "title": "Sunday 23 Nov", "template": TPL}).get_json()
    got = r["media_from_bin"]
    assert (got["source"], got["saved_as"]) == ("chosen", "Sunday Morning Library")
    assert asked["notes"] == {"Announcements": "one-off events and ones we run for a while"}
    assert [e["uuid"] for e in r["items"][0]["library_match"]["items"]] == ["L1", "A1"]
