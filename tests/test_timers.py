"""Tests for which runsheet items get a ProPresenter countdown timer."""
import pytest
import requests

from propresenterrunsheet.propresenter.timers import _create_pp_timers, is_key_part


def _item(title, type_="other", mins=10):
    return {"parsed": {"title": title, "type": type_, "duration_min": mins}}


RUNSHEET = [_item("Welcome", "mc_on_stage", 5), _item("Worship", "prayer_and_ministry", 30),
            _item("Games Rotation: Oli, Mia, Amos"), _item("Announcements", "announcement", 5),
            _item("Preach", "sermon", 30), _item("Build My Life", "song", 5)]


@pytest.mark.parametrize("title, type_, key", [
    ("Worship", "prayer_and_ministry", True),
    ("Games Rotation: Oli, Mia, Amos", "other", True),
    ("Preach", "sermon", True),
    ("Message — Ps Cathie", "sermon", True),     # the type says sermon
    ("Preaching", "other", True),
    ("Welcome", "mc_on_stage", False),
    ("Response Moment and Prayer", "prayer_and_ministry", False),
    ("Gamestop giveaway", "other", False),       # a word, not a part of one
    ("Land Worship - Priya", "mc_on_stage", False),
    ("Worship night next Friday", "announcement", False),
    ("Guest preacher intro", "other", False),
])
def test_key_parts_are_the_sermon_worship_and_games(title, type_, key):
    assert is_key_part({"title": title, "type": type_}) is key


@pytest.fixture
def made(monkeypatch):
    """The timer names ProPresenter was asked to create."""
    names = []
    ok = type("R", (), {"ok": True, "status_code": 200, "json": lambda self: []})()
    monkeypatch.setattr(requests, "get", lambda *a, **k: ok)
    monkeypatch.setattr(requests, "post",
                        lambda url, json=None, **k: names.append(json["name"]) or ok)
    return names


def test_every_timed_item_but_songs_gets_a_timer_by_default(made):
    _create_pp_timers("http://pp", "Sunday", RUNSHEET)
    assert len(made) == 5


def test_key_parts_only_times_the_sermon_worship_and_games(made):
    result = _create_pp_timers("http://pp", "Sunday", RUNSHEET, key_only=True)
    assert [n.split(". ", 1)[1] for n in made] == [
        "Worship (30 min)", "Games Rotation: Oli, Mia, Amos (10 min)", "Preach (30 min)"]
    # Service Mate's auto-track keys timers by runsheet position.
    assert sorted(result["timer_names"]) == [1, 2, 4]
