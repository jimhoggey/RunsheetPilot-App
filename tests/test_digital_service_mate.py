"""The digital Service Mate: the window on the ProPresenter computer.

Owner's spec (6 Oct 2026, docs/superpowers/specs/2026-10-06-digital-service-
mate-design.md): one Service Mate, on a clock or on this computer. The window
shows what the ticked stations' clocks are sent — generic cues included —
plus optional sections the operator ticks to see what earns its place.
"""
import datetime as dt

import pytest

from propresenterrunsheet.service_mate.mate import mate_config, mate_view, segment_timing
from propresenterrunsheet.service_mate.protocol import EndsAtHolder, build_state_payload

NOW = dt.datetime(2026, 10, 11, 18, 13, 0)
ENDS = NOW + dt.timedelta(minutes=17)

ITEMS = [
    {"title": "Worship", "type": "song", "start_time": "6:10 PM", "duration_min": 20,
     "notes": "Drop lights to 12% for worship.",
     "cues": {"screen": ["Cue song slides", "Lyrics up"], "lights": ["Stage wash"]},
     "lighting_steps": [{"level": "12%", "when": "halfway"}, {"level": "8%", "when": "first worship song"}]},
    {"title": "Welcome", "type": "announcement", "start_time": "6:30 PM", "duration_min": 5,
     "cues": {"screen": ["Slide — Welcome"], "lights": ["House up"]}},
    {"title": "Notices", "type": "announcement", "start_time": "6:35 PM", "duration_min": 5},
    {"title": "Message", "type": "sermon", "start_time": "6:40 PM", "duration_min": 30},
    {"title": "Close", "type": "other", "start_time": "7:10 PM"},
]


def _state(**kw):
    return {"items": ITEMS, "current_index": 0, "current_started_at": "2026-10-11T18:13:00", **kw}


# ── Settings: which stations sit at this desk, which extras show ─────────────

@pytest.mark.parametrize("cfg, want", [
    ({}, {"on": False, "stations": ["screen"], "show": ["timing"]}),
    # The lighting window, switched on before the digital Service Mate existed.
    ({"lighting_window": True}, {"on": True, "stations": ["lights"], "show": ["timing"]}),
    ({"lighting_window": True, "mate": {"on": False, "stations": ["screen"], "show": []}},
     {"on": False, "stations": ["screen"], "show": []}),
    # Hand-edited or stale: unknown names dropped, canonical order, no repeats.
    ({"mate": {"on": 1, "stations": ["lights", "sound", 3, "screen", "lights"], "show": ["later", "x"]}},
     {"on": True, "stations": ["screen", "lights"], "show": ["later"]}),
    ({"mate": {"on": True, "stations": "lights", "show": None}},
     {"on": True, "stations": ["screen"], "show": ["timing"]}),
    ({"mate": {"stations": []}}, {"on": False, "stations": ["screen"], "show": ["timing"]}),
    ({"mate": "on"}, {"on": False, "stations": ["screen"], "show": ["timing"]}),
])
def test_the_desk_settings_are_read_safely(cfg, want):
    assert mate_config(cfg) == want


# ── Timing: planned start and length, and how far ahead or behind ────────────

@pytest.mark.parametrize("start, started, want", [
    ("6:10 PM", "2026-10-11T18:10:40", 0),       # within a minute: on time
    ("6:10 PM", "2026-10-11T18:13:00", 3),       # behind
    ("6:10 PM", "2026-10-11T18:08:00", -2),      # ahead
    ("6:10", "2026-10-11T18:13:00", 3),          # no am/pm: the nearer reading
    ("6:10 PM", "2026-10-11T19:30:00", None),    # over an hour out: not a live start
    ("6:10 PM", "2026-10-10T18:30:00", None),    # loaded the day before
    ("6:10 PM", None, None),
    ("6:10 PM", 1760000000, None),               # a hand-edited state file
])
def test_how_far_behind_the_segment_started(start, started, want):
    t = segment_timing({"start_time": start, "duration_min": 20}, started, NOW)
    assert (t["start"], t["length_min"], t["drift_min"]) == (start, 20, want)


@pytest.mark.parametrize("item, want", [
    ({"duration_min": 20}, {"start": "", "length_min": 20, "drift_min": None}),
    ({"start_time": "soon", "duration_min": 5}, {"start": "", "length_min": 5, "drift_min": None}),
    ({"start_time": "6:10 PM"}, {"start": "6:10 PM", "length_min": 0, "drift_min": 3}),
    ({"title": "Close"}, None),
])
def test_timing_shows_what_the_runsheet_gives(item, want):
    assert segment_timing(item, "2026-10-11T18:13:00", NOW) == want


# ── The view: each station gets exactly what its clock is sent ───────────────

def test_each_station_gets_its_clocks_own_payload():
    view = mate_view(_state(), ["screen", "lights"], [], ENDS, NOW)
    assert view["state"] == "live"
    for role in ("screen", "lights"):
        assert view["stations"][role] == build_state_payload(role, "detailed", _state(), ENDS, NOW)
    assert view["stations"]["screen"]["cues"] == ["Cue song slides", "Lyrics up"]


def test_only_the_ticked_stations_are_built():
    assert list(mate_view(_state(), ["screen"], [], ENDS, NOW)["stations"]) == ["screen"]


def test_lights_ticked_shows_the_next_two_changes():
    view = mate_view(_state(), ["lights"], [], ENDS, NOW)
    lt = view["lighting"]
    assert (lt["next"]["level"], lt["then"]["level"], lt["done"]) == ("12%", "8%", 0)
    ticked = mate_view(_state(lighting_done={"index": 0, "count": 1}), ["lights"], [], ENDS, NOW)
    assert (ticked["lighting"]["next"]["level"], ticked["lighting"]["done"]) == ("8%", 1)


def test_no_lighting_card_without_lights_or_without_steps():
    assert mate_view(_state(), ["screen"], [], ENDS, NOW)["lighting"] is None
    plain = [{k: v for k, v in it.items() if k != "lighting_steps"} for it in ITEMS]
    view = mate_view({**_state(), "items": plain}, ["lights"], [], ENDS, NOW)
    assert view["lighting"] is None
    assert view["stations"]["lights"]["cues"] == ["Stage wash"]     # the Lights clock's cues instead


def test_the_lights_payload_keeps_the_real_next_item():
    """The window draws its own lighting card, so Up next stays the next item."""
    assert mate_view(_state(), ["lights"], [], ENDS, NOW)["stations"]["lights"]["next_title"] == "Welcome"


def test_extras_come_only_when_ticked():
    assert mate_view(_state(), ["screen"], [], ENDS, NOW)["extras"] == {}
    extras = mate_view(_state(), ["screen", "lights"], ["timing", "notes", "next_cues", "later"],
                       ENDS, NOW)["extras"]
    assert extras["timing"] == {"start": "6:10 PM", "length_min": 20, "drift_min": 3}
    assert extras["notes"] == "Drop lights to 12% for worship."
    assert extras["next_cues"] == {"screen": ["Slide — Welcome"], "lights": ["House up"]}
    assert [x["title"] for x in extras["later"]] == ["Notices", "Message", "Close"]
    assert extras["later"][0]["start_time"] == "6:35 PM"


def test_an_extra_with_nothing_to_say_is_left_out():
    last = _state(current_index=4)
    extras = mate_view(last, ["screen"], ["notes", "next_cues", "later"], ENDS, NOW)["extras"]
    assert extras == {}


@pytest.mark.parametrize("state, want", [
    ({"items": ITEMS, "standby": True}, "standby"),
    ({"items": []}, "empty"),
    ({}, "empty"),
])
def test_plain_states_say_so(state, want):
    assert mate_view(state, ["screen"], ["timing"], None, NOW) == {"state": want}


# ── The countdown: the loop's own deadline, read without moving it ──────────

def _running(started="2026-10-11T18:10:00"):
    return {"items": [{"title": "Worship", "duration_min": 20}],
            "current_index": 0, "current_started_at": started}


def test_peek_reads_the_held_deadline_for_the_current_item_only():
    holder = EndsAtHolder()
    assert holder.peek(_running()) is None                       # loop hasn't run yet
    held = holder.resolve(_running(), NOW)
    assert holder.peek(_running()) == held
    assert holder.peek(_running("2026-10-11T18:30:00")) is None  # another item now


def test_peek_never_moves_the_clocks_deadline():
    holder = EndsAtHolder()
    held = holder.resolve(_running(), NOW)
    holder.peek(_running("2026-10-11T18:30:00"))
    assert holder.resolve(_running(), NOW + dt.timedelta(seconds=1)) == held
