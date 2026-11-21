"""Every media folder has one job (owner, 20 Nov 2026).

Announcements: every file goes in, under the line it belongs to.
Preach: every file under the preach, title screen first. Loops: the
pre-service loop opens the playlist and the end loop closes it. Anything
else: only what fits. Spec: docs/superpowers/specs/2026-11-21-folder-jobs-design.md
"""
import pytest

import propresenterrunsheet.routes.parse as parse_mod
from propresenterrunsheet.propresenter.media_match import (
    folder_job, place, scope, stale, with_bin, without_bin,
)
from propresenterrunsheet.settings import load_settings, save_settings

BIN = [
    {"uuid": "A1", "name": "Junior Youth Sunday", "folder": "Announcements"},
    {"uuid": "A2", "name": "Young Adults 29th Nov", "folder": "Announcements"},
    {"uuid": "A3", "name": "Baptisms", "folder": "Announcements"},
    {"uuid": "P2", "name": "Preach Slide 10", "folder": "Preach"},
    {"uuid": "P3", "name": "Preach Slide 2", "folder": "Preach"},
    {"uuid": "P1", "name": "Preach Title", "folder": "Preach"},
    {"uuid": "L1", "name": "Sunday Pre Loop.mp4", "folder": "Pre-service loop"},
    {"uuid": "L2", "name": "Sunday End Loop.mp4", "folder": "End service loop"},
    {"uuid": "S1", "name": "Hold screen", "folder": "Sunday media"},
]
SUNDAY = ["Announcements", "Preach", "Pre-service loop", "End service loop", "Sunday media"]
RUNSHEET = [
    {"title": "Countdown", "type": "other"},
    {"title": "Junior Youth Out", "type": "other", "notes": "Junior Youth screen"},
    {"title": "Announcements", "type": "announcement"},
    {"title": "Preach", "type": "sermon"},
    {"title": "Closing Song", "type": "song"},
]


@pytest.mark.parametrize("name, job", [
    ("Announcements", "every"), ("Sunday Notices", "every"), ("News", "every"),
    ("Preach", "preach"), ("Sermon slides", "preach"), ("Preaching", "preach"),
    ("Pre-service loop", "start"), ("PreLoop", "start"), ("Summit pre-service", "start"),
    ("Walk-in", "start"),
    ("End service loop", "end"), ("Post-loop", "end"), ("Summit after service", "end"),
    ("Outro", "end"),
    ("Sunday media", "pick"), ("Weekend loop", "pick"), ("Present", "pick"), ("", "pick"),
])
def test_a_folder_name_suggests_its_job(name, job):
    assert folder_job(name) == job


def _sunday(jobs=None):
    return [{"match": "Sunday", "folders": SUNDAY, **({"jobs": jobs} if jobs else {})}]


def test_a_saved_row_without_jobs_gets_the_name_guess():
    """Rows saved before v2.20 have folders and no jobs."""
    sc = scope(BIN, "Sunday", RUNSHEET, _sunday())
    assert sc["jobs"] == {"Announcements": "every", "Preach": "preach",
                          "Pre-service loop": "start", "End service loop": "end",
                          "Sunday media": "pick"}


def test_the_operators_job_beats_the_name():
    sc = scope(BIN, "Sunday", RUNSHEET, _sunday({"Announcements": "pick", "Preach": "bogus"}))
    assert sc["jobs"]["Announcements"] == "pick"
    assert sc["jobs"]["Preach"] == "preach", "an unknown job falls back to the name"


def test_the_models_job_is_used_when_nothing_is_saved():
    sc = scope(BIN, "Sunday", RUNSHEET, [], suggested={"Sunday media": "every", "Preach": ""})
    assert sc["jobs"] == {"Sunday media": "every", "Preach": "preach"}


def test_only_announcements_and_the_rest_reach_the_model_announcements_first():
    sc = scope(BIN, "Sunday", RUNSHEET, _sunday())
    assert [a["uuid"] for a in sc["assets"]] == ["A1", "A2", "A3", "S1"]
    assert [a["job"] for a in sc["assets"]] == ["every"] * 3 + ["pick"]


def test_preach_puts_the_title_first_then_name_order():
    sc = scope(BIN, "Sunday", RUNSHEET, _sunday())
    assert [a["uuid"] for a in sc["fixed"]["preach"]] == ["P1", "P3", "P2"]
    assert [a["uuid"] for a in sc["fixed"]["start"]] == ["L1"]
    assert [a["uuid"] for a in sc["fixed"]["end"]] == ["L2"]


def test_every_announcement_is_offered_even_past_the_budget():
    big = [{"uuid": f"X{i}", "name": f"Hold {i}", "folder": "Sunday media"} for i in range(300)]
    sc = scope(big + BIN, "Sunday", RUNSHEET, _sunday())
    assert {"A1", "A2", "A3"} <= {a["uuid"] for a in sc["assets"]}


def _placed(picks, items=RUNSHEET):
    sc = scope(BIN, "Sunday", items, _sunday())
    return place(items, picks, sc["assets"], sc["fixed"])


def test_announcements_go_where_the_model_put_them_and_the_rest_under_announcements():
    out, left = _placed([{"media": 0, "item": 1}])          # Junior Youth → its line
    assert [e["uuid"] for e in out[1]] == ["A1"]
    assert [e["uuid"] for e in out[2]] == ["A2", "A3"], "none dropped"
    assert left == {"announcements": 0, "preach": 0}


def test_an_announcement_goes_in_once_however_often_the_model_names_it():
    out, _ = _placed([{"media": 0, "item": 1}, {"media": 0, "item": 2}])
    assert sum(e["uuid"] == "A1" for line in out.values() for e in line) == 1


def test_announcements_are_never_capped():
    many = [{"uuid": f"N{i}", "name": f"Notice {i}", "folder": "Announcements"}
            for i in range(9)]
    sc = scope(many, "Sunday", RUNSHEET, _sunday())
    out, _ = place(RUNSHEET, [], sc["assets"], sc["fixed"])
    assert len(out[2]) == 9


def test_preach_and_loops_need_no_model():
    out, _ = _placed(None)
    assert [e["uuid"] for e in out[3]] == ["P1", "P3", "P2"]
    assert out[0][0]["uuid"] == "L1" and out[0][0]["job"] == "start"
    assert out[4][-1]["uuid"] == "L2", "the end loop closes the last line, a song or not"


def test_with_no_announcements_or_preach_line_the_leftovers_are_counted():
    items = [{"title": "Welcome", "type": "other"}, {"title": "Worship", "type": "song"}]
    out, left = _placed([], items)
    assert left == {"announcements": 3, "preach": 3}
    assert [e["uuid"] for e in out[0]] == ["L1"] and [e["uuid"] for e in out[1]] == ["L2"]


def test_a_screen_the_template_already_has_is_not_added_again():
    """The office may keep Welcome and Giving in the Announcements folder
    to see them; the template still owns them, so they show once."""
    folder = BIN + [{"uuid": "W9", "name": "Welcome Slide", "folder": "Announcements"},
                    {"uuid": "G1", "name": "Giving QR", "folder": "Announcements"}]
    items = [dict(it) for it in RUNSHEET]
    items[0] = {**items[0], "library_match": {"header": {"name": "Welcome"}, "items": [
        {"name": "welcome slide", "uuid": "T1"}, {"name": "Giving", "uuid": "G1"}]}}
    sc = scope(folder, "Sunday", items, _sunday())
    out, left = place(items, [], sc["assets"], sc["fixed"])
    placed = {e["uuid"] for line in out.values() for e in line}
    assert not placed & {"W9", "G1"}, "same name or same file: the template's"
    assert {"A1", "A2", "A3"} <= placed and left["announcements"] == 0


def test_a_line_orders_loop_template_placed_preach_loop():
    template = {"header": {"name": "Countdown"}, "items": [{"name": "Countdown 5"}]}
    item = {"title": "Countdown", "library_match": template}
    entries = [{"uuid": u, "name": u, "from_bin": True, "job": j}
               for u, j in (("E", "end"), ("Q", "pick"), ("L", "start"), ("P", "preach"))]
    got = with_bin(item, entries)
    assert [e["name"] for e in got["items"]] == ["L", "Countdown 5", "Q", "P", "E"]
    assert without_bin(got) == template, "placing again takes back only the bin's"


def test_entries_sent_back_by_the_page_need_not_be_dicts():
    item = {"title": "Welcome", "library_match": {"header": {"name": "W"},
                                                  "items": ["junk", None, {"name": "T"}]}}
    got = with_bin(item, [{"uuid": "Q", "name": "Q", "from_bin": True, "job": "start"}])
    assert got["items"][0]["name"] == "Q"


def test_a_v219_bin_section_is_replaced_whole():
    old = {"header": {"name": "Opener"}, "items": [{"name": "old"}], "from_bin": True}
    assert without_bin(old) is None
    assert with_bin({"title": "Opener", "library_match": old}, []) is None


@pytest.mark.parametrize("answer, want", [
    ({"stale": [0, 0, 9, "1", True, None]}, ["Young Adults 29th Nov"]),
    ({"stale": "all"}, []), (None, []), ("junk", []),
])
def test_out_of_date_warnings_are_held_to_what_the_model_was_shown(answer, want):
    offered = [{"name": "Young Adults 29th Nov"}, {"name": "Baptisms"}]
    assert stale(answer, offered) == want


# ── Through the routes ─────────────────────────────────────────────────────

@pytest.fixture
def placing(client, monkeypatch):
    """POST /api/media/place against BIN, with the media call answering
    whatever the test sets on `answer`."""
    monkeypatch.setattr(parse_mod, "pp_base", lambda *a, **k: "http://pp")
    monkeypatch.setattr(parse_mod, "fetch_media_bin", lambda base: BIN)
    monkeypatch.setattr(parse_mod, "fetch_pp_playlists", lambda base: [])
    monkeypatch.setattr(parse_mod, "plan_folders",
                        lambda *a, **k: pytest.fail("saved folders are final"))
    asked = {"answer": {"media": [{"media": 0, "item": 1}], "stale": [1]}}

    def _media(items, offered, *a, **k):
        asked["offered"] = [o["uuid"] for o in offered]
        return asked["answer"]
    monkeypatch.setattr(parse_mod, "plan_media", _media)
    save_settings({"or_key": "k", "media_folders_by_service": _sunday()})

    def post(items=RUNSHEET):
        return client.post("/api/media/place", json={
            "parsed": items, "service": "Sunday", "title": "Sunday 23 Nov"})
    asked["post"] = post
    return asked


def test_a_sunday_places_every_folder_by_its_job(placing):
    r = placing["post"]().get_json()
    items = [it["library_match"]["items"] for it in r["items"]]
    assert [e["uuid"] for e in items[0]] == ["L1"]
    assert [e["uuid"] for e in items[1]] == ["A1"]
    assert [e["uuid"] for e in items[2]] == ["A2", "A3"]
    assert [e["uuid"] for e in items[3]] == ["P1", "P3", "P2"]
    assert [e["uuid"] for e in items[4]] == ["L2"]
    got = r["media_from_bin"]
    assert got["stale"] == ["Young Adults 29th Nov"], "warned, and still placed"
    assert got["jobs"]["Announcements"] == "every" and got["ai_failed"] is False
    assert placing["offered"] == ["A1", "A2", "A3", "S1"], "preach and loops need no AI"


def test_when_the_ai_doesnt_answer_on_a_first_place_the_announcements_still_go_in(placing):
    placing["answer"] = None
    r = placing["post"]().get_json()
    assert [e["uuid"] for e in r["items"][2]["library_match"]["items"]] == ["A1", "A2", "A3"]
    assert r["media_from_bin"]["ai_failed"] is True


def test_rematch_keeps_the_bins_files(client, monkeypatch):
    """Re-match recomputes template links; it used to wipe the bin's too."""
    monkeypatch.setattr(parse_mod, "pp_base", lambda *a, **k: "http://pp")
    monkeypatch.setattr(parse_mod, "link_items_to_template",
                        lambda parsed, *a, **k: [it.update(library_match=None)
                                                 for it in parsed] and 0)
    placed = {"header": {"name": "Announcements"}, "from_bin": True,
              "items": [{"name": "Baptisms", "uuid": "A3", "from_bin": True, "job": "every"}]}
    r = client.post("/api/match", json={
        "parsed": [{"title": "Announcements", "type": "announcement",
                    "library_match": placed}],
        "rematch_template": True, "template_playlist_uuid": "TPL"}).get_json()
    assert r["items"][0]["parsed"]["library_match"]["items"][0]["uuid"] == "A3"


def test_a_song_with_media_on_its_line_is_still_matched_to_its_song(client):
    lib = [{"name": "Closing Song", "uuid": "SONG"}]
    placed = {"header": {"name": "Closing Song"}, "from_bin": True,
              "items": [{"name": "End loop", "uuid": "L2", "from_bin": True, "job": "end"}]}
    r = client.post("/api/match", json={
        "parsed": [{"title": "Closing Song", "type": "song", "library_match": placed}],
        "library": lib}).get_json()
    assert r["items"][0]["match"]["uuid"] == "SONG"


def test_saving_folders_keeps_only_real_jobs(client):
    client.post("/api/media_folders", json={
        "service": "Sunday", "folders": ["Announcements", "Preach", "Loops"],
        "jobs": {"Announcements": "every", "Preach": "delete everything",
                 "Loops": "start", "Not ticked": "end"}})
    row = load_settings()["media_folders_by_service"][0]
    assert row["jobs"] == {"Announcements": "every", "Loops": "start"}
