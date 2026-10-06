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


def test_a_junk_position_shows_the_first_segment_rather_than_failing():
    view = mate_view(_state(current_index="abc"), ["screen"], [], None, NOW)
    assert view["stations"]["screen"]["title"] == "Worship"


# ── Routes ───────────────────────────────────────────────────────────────────

def _set_desk(**mate):
    from propresenterrunsheet.service_mate import state as sm_state
    cfg = sm_state._read_clocks_config()
    sm_state._write_clocks_config({**cfg, "mate": {"on": False, **mate}})


@pytest.fixture
def desk(sm_enabled):
    """A runsheet live, and Screens + Lights ticked at this desk."""
    from propresenterrunsheet.service_mate import daemon, state as sm_state
    sm_state._write_runsheet_state(_state())
    _set_desk(stations=["screen", "lights"], show=["timing"])
    daemon._ENDS_AT.reset()
    return sm_enabled


def test_the_window_is_off_with_service_mate(client):
    assert client.get("/api/mate").get_json() == {"state": "off"}


def test_the_api_serves_the_ticked_stations(desk):
    view = desk.get("/api/mate").get_json()
    assert (view["state"], sorted(view["stations"])) == ("live", ["lights", "screen"])
    assert view["lighting"]["next"]["level"] == "12%"
    assert view["extras"]["timing"]["start"] == "6:10 PM"
    _set_desk(stations=["sound", "screen"])
    assert list(desk.get("/api/mate").get_json()["stations"]) == ["screen"]


def test_the_countdown_is_the_clocks_own_deadline(desk):
    from propresenterrunsheet.service_mate import daemon, state as sm_state
    from propresenterrunsheet.service_mate.protocol import _iso
    assert desk.get("/api/mate").get_json()["stations"]["screen"]["ends_at"] is None   # loop not run yet
    held = daemon._ENDS_AT.resolve(sm_state._read_runsheet_state(), NOW)
    assert desk.get("/api/mate").get_json()["stations"]["screen"]["ends_at"] == _iso(held)


@pytest.mark.parametrize("reachable, tracking, ok", [
    (False, True, False), (False, False, True), (True, True, True)])
def test_it_says_when_propresenter_is_not_answering(desk, monkeypatch, reachable, tracking, ok):
    from propresenterrunsheet.service_mate import pp_track, state as sm_state
    monkeypatch.setitem(pp_track.PP_REACHABLE, "ok", reachable)
    sm_state._write_runsheet_state(_state(auto_track={"enabled": tracking}))
    assert desk.get("/api/mate").get_json()["pp_ok"] is ok


def test_the_old_lighting_page_leads_to_the_mate(client):
    r = client.get("/lighting")
    assert r.status_code == 302 and r.headers["Location"].endswith("/mate")


def test_the_mate_page_is_served(client):
    page = client.get("/mate").data
    assert b"Service Mate" in page and b"/api/mate" in page and b"/api/lighting/done" in page


# ── The floating window ──────────────────────────────────────────────────────

class _Handlers(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self

    def __isub__(self, fn):
        self.remove(fn)
        return self


class _FakeWindow:
    def __init__(self):
        self.destroyed = False
        self.width, self.resized = 400, None      # the operator widened it
        self.events = type("E", (), {"closed": _Handlers()})()

    def destroy(self):
        self.destroyed = True

    def resize(self, width, height):
        self.resized = (width, height)


@pytest.fixture
def fake_webview(monkeypatch):
    from propresenterrunsheet import native
    made = []

    class _Webview:
        @staticmethod
        def create_window(title, url, **kw):
            made.append((title, url, kw))
            return _FakeWindow()
    monkeypatch.setattr(native, "webview", _Webview)
    return made


def _desk_on():
    from propresenterrunsheet.service_mate import state as sm_state
    from propresenterrunsheet.service_mate.mate import mate_config
    return mate_config(sm_state._read_clocks_config())["on"]


def test_the_window_opens_on_top_inside_the_app_and_closes(sm_enabled, fake_webview):
    from propresenterrunsheet import native
    r = sm_enabled.post("/api/mate/window", json={"on": True}).get_json()
    assert r["native"] is True and r["mate"]["on"] is True
    (title, url, kw), = fake_webview
    assert (title, url.endswith("/mate"), kw["on_top"]) == ("Service Mate", True, True)
    win = native._mate
    sm_enabled.post("/api/mate/window", json={"on": False})
    assert win.destroyed and native._mate is None and not _desk_on()


@pytest.mark.parametrize("stations, height, min_height", [
    (["screen"], 180, 150), (["lights"], 240, 170), (["screen", "lights"], 320, 240)])
def test_it_is_as_tall_as_the_stations_it_shows(sm_enabled, fake_webview, stations, height, min_height):
    """Measured at 360 wide: the whole view fits, and the minimum still
    shows the essentials — with Lights, the NEXT card."""
    sm_enabled.post("/api/mate/window", json={"on": True, "stations": stations})
    kw = fake_webview[0][2]
    assert (kw["height"], kw["min_size"]) == (height, (280, min_height))


def test_ticking_a_station_while_open_keeps_the_one_window_and_fits_it(sm_enabled, fake_webview):
    from propresenterrunsheet import native
    sm_enabled.post("/api/mate/window", json={"on": True})
    r = sm_enabled.post("/api/mate/window", json={"on": True, "stations": ["lights", "bogus"],
                                                  "show": ["notes"]}).get_json()
    assert len(fake_webview) == 1
    assert r["mate"] == {"on": True, "stations": ["lights"], "show": ["notes"]}
    assert native._mate.resized == (400, 240)            # the operator's width kept


def test_a_show_box_leaves_the_operators_window_size_alone(sm_enabled, fake_webview):
    from propresenterrunsheet import native
    sm_enabled.post("/api/mate/window", json={"on": True})
    sm_enabled.post("/api/mate/window", json={"on": True, "show": ["timing", "later"]})
    assert native._mate.resized is None


def test_saving_retires_the_old_lighting_switch(sm_enabled):
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_clocks_config({**sm_state._read_clocks_config(), "lighting_window": True})
    sm_enabled.post("/api/mate/window", json={"on": False, "stations": ["lights"]})
    cfg = sm_state._read_clocks_config()
    assert "lighting_window" not in cfg and cfg["mate"]["stations"] == ["lights"]


def test_closing_it_with_its_own_button_switches_the_setting_off(sm_enabled, fake_webview):
    from propresenterrunsheet import native
    sm_enabled.post("/api/mate/window", json={"on": True})
    for handler in list(native._mate.events.closed):
        handler()
    assert native._mate is None and not _desk_on()


def test_no_native_window_when_the_page_opened_a_popup(sm_enabled, fake_webview):
    r = sm_enabled.post("/api/mate/window", json={"on": True, "popup": True}).get_json()
    assert r["native"] is False and fake_webview == []


def test_in_a_browser_the_page_is_told_to_open_a_popup(sm_enabled):
    assert sm_enabled.post("/api/mate/window", json={"on": True}).get_json()["native"] is False


def test_switching_service_mate_off_closes_the_window(sm_enabled, fake_webview):
    from propresenterrunsheet import native
    sm_enabled.post("/api/mate/window", json={"on": True})
    win = native._mate
    sm_enabled.post("/api/clocks", json={"enabled": False})
    assert win.destroyed and native._mate is None and not _desk_on()
    assert sm_enabled.get("/api/mate").get_json() == {"state": "off"}


def test_it_is_part_of_service_mate(client):
    """Off with the master switch, like every other Service Mate action."""
    assert client.post("/api/mate/window", json={"on": True}).status_code == 409


def test_closing_the_main_window_takes_the_mate_but_keeps_it_on(monkeypatch, isolated_state):
    """pywebview only returns when every window is closed: the mate must
    close with the main window, or the app never quits."""
    import types
    from propresenterrunsheet import native, server
    closed = _Handlers()
    fake = types.SimpleNamespace(
        create_window=lambda *a, **k: types.SimpleNamespace(events=types.SimpleNamespace(closed=closed)),
        start=lambda: None)
    assert server._run_native_window(5757, webview_module=fake)
    assert native.close_mate_window in closed


def test_a_failed_native_start_leaves_no_phantom_window(isolated_state):
    import types
    from propresenterrunsheet import native, server

    def boom():
        raise RuntimeError("no WebView2")
    fake = types.SimpleNamespace(create_window=lambda *a, **k: object(), start=boom)
    assert server._run_native_window(5757, webview_module=fake) is False
    assert native.webview is None


# ── The loop keeps ProPresenter followed for a desk with no clocks ───────────

@pytest.mark.parametrize("cfg, runs", [
    ({"mate": {"on": True}}, True),
    ({"lighting_window": True}, True),          # switched on before the mate existed
    ({"mate": {"on": False}}, False),
])
def test_the_loop_runs_for_the_window_alone(monkeypatch, isolated_state, cfg, runs):
    from propresenterrunsheet import licensing
    from propresenterrunsheet.service_mate import daemon, state as sm_state
    monkeypatch.setattr(licensing, "service_mate_allowed", lambda: True)
    monkeypatch.setattr(licensing, "start_trial_if_needed", lambda: None)
    sm_state._write_clocks_config({"enabled": True, "clocks": [], **cfg})
    sm_state._write_runsheet_state(_state())
    polls = []
    monkeypatch.setattr(daemon, "_maybe_advance_from_pp", lambda s: (polls.append(1), s)[1])
    daemon._clocks_loop_tick(0)
    assert bool(polls) is runs
