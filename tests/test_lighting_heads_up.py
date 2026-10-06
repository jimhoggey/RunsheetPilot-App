"""The lighting heads-up: where the lights are now, and the next change.

Owner's spec (2 Oct 2026): runsheets carry lighting settings; show what's
COMING UP, never "change now" — the moment is the room's call ("thanks
band"). One step at a time: NOW (greyed), the NEXT change with its moment,
THEN (greyed). The church's lighting guide supplies the moment wording,
and a level where the runsheet gives none; the runsheet wins. The operator
ticks a step off on the card, since the app can't hear where the band is.
Shown on the Lights clock and in an optional floating always-on-top card.
"""
import io
import json

import pytest

from propresenterrunsheet.service_mate.lighting import (
    has_lighting, heads_up, lights_view, steps_of,
)

ITEMS = [
    {"title": "Walk-in", "lighting_steps": [{"level": "30%", "when": "as doors open"}]},
    {"title": "Worship", "lighting_steps": [
        {"level": "12%", "when": "halfway through the praise song"},
        {"level": "8%", "when": "first worship song starts"},
        {"level": "2%", "when": "halfway through the 1st worship song"}]},
    {"title": "Welcome", "lighting_steps": [{"level": "12%", "when": "when the host walks on"}]},
    {"title": "Notices", "lighting_steps": [{"level": "12%", "when": ""}]},     # a repeat
    {"title": "Message", "lighting_steps": [{"level": "20%", "when": "“Thanks band”"}]},
    {"title": "Close"},
]


def _levels(h):
    return (h["now"], h["next"] and h["next"]["level"], h["then"] and h["then"]["level"])


@pytest.mark.parametrize("idx, done, want", [
    (0, 0, ("", "30%", "12%")),
    (1, 0, ("30%", "12%", "8%")),            # Worship starts: its first step is next
    (1, 1, ("12%", "8%", "2%")),             # ticked off: the next one moves up
    (1, 3, ("2%", "12%", "20%")),            # all done: Welcome's 12%, Notices' 12% skipped
    (3, 0, ("12%", "20%", None)),
    (5, 0, ("20%", None, None)),
])
def test_one_step_at_a_time(idx, done, want):
    assert _levels(heads_up(ITEMS, idx, done)) == want


def test_each_change_says_when_and_where():
    h = heads_up(ITEMS, 1, 0)
    assert h["next"]["when"] == "halfway through the praise song" and h["next"]["here"]
    later = heads_up(ITEMS, 1, 3)["next"]
    assert (later["section"], later["when"], later["here"]) == ("Welcome", "when the host walks on", False)


def test_a_runsheet_saved_before_steps_still_works():
    assert steps_of({"title": "X", "lighting": "House lights 50%"}) == [{"level": "House lights 50%", "when": ""}]
    assert steps_of({"title": "X", "lighting_steps": "junk"}) == []
    assert steps_of("junk") == []


def test_a_runsheet_with_no_lighting_says_nothing():
    items = [{"title": "Welcome", "cues": {"lights": ["Stage wash"]}}]   # a model suggestion
    assert not has_lighting(items)
    assert _levels(heads_up(items, 0)) == ("", None, None)


# ── The Lights clock ─────────────────────────────────────────────────────────

def test_the_lights_clock_shows_now_and_the_next_change():
    view = lights_view({"items": ITEMS, "current_index": 1, "current_started_at": "x"})
    assert view["current_index"] == 0 and view["current_started_at"] == "x"
    assert view["items"][0]["cues"]["lights"] == ["Now: 30%"]
    # The level leads the next title: compact layouts show no next cue.
    assert view["items"][1]["title"] == "12% · halfway through the praise song"
    assert ITEMS[1].get("cues") is None          # the real state is untouched


def test_a_ticked_step_moves_the_clock_on_too():
    view = lights_view({"items": ITEMS, "current_index": 1,
                        "lighting_done": {"index": 1, "count": 1}})
    assert view["items"][0]["cues"]["lights"] == ["Now: 12%"]
    assert view["items"][1]["title"] == "8% · first worship song starts"


def test_a_change_in_a_later_section_names_it():
    view = lights_view({"items": ITEMS, "current_index": 1, "lighting_done": {"index": 1, "count": 3}})
    assert view["items"][1]["title"] == "12% · Welcome, when the host walks on"


def test_the_next_change_never_reports_a_length():
    """"30 min" in the next change's section must not reach the clock as
    the length of what's up next."""
    import datetime as dt
    from propresenterrunsheet.service_mate.protocol import build_state_payload
    items = [{"title": "Worship", "lighting_steps": [{"level": "12%", "when": ""}]},
             {"title": "Preach (30 min)", "notes": "Ps David — 30 min",
              "lighting_steps": [{"level": "20%", "when": ""}]}]
    state = {"items": items, "current_index": 0, "lighting_done": {"index": 0, "count": 1}}
    p = build_state_payload("lights", "compact", lights_view(state), None, dt.datetime(2026, 10, 4, 18))
    assert p["next_title"] == "20% · Preach (30 min)" and "next_duration_s" not in p


def test_with_no_change_left_the_real_next_item_stays_next():
    """Not END OF SERVICE halfway through: the clock's NEXT keeps meaning next."""
    view = lights_view({"items": ITEMS, "current_index": 4,
                        "lighting_done": {"index": 4, "count": 1}})     # its 20% is done
    assert [it["title"] for it in view["items"]] == ["Message", "Close"]


def test_before_the_first_level_the_station_keeps_its_usual_cues():
    items = [{"title": "Doors", "cues": {"lights": ["House up"]}}, *ITEMS]
    assert lights_view({"items": items, "current_index": 0})["items"][0]["cues"]["lights"] == ["House up"]


def test_a_malformed_state_is_shown_plainly():
    state = {"items": ["junk", *ITEMS], "current_index": 0}
    assert lights_view(state) is state
    assert lights_view({"items": ITEMS, "current_index": "abc"})["items"][0]["title"] == "Walk-in"


def test_the_payload_for_the_lights_station():
    import datetime as dt
    from propresenterrunsheet.service_mate.protocol import build_state_payload
    p = build_state_payload("lights", "compact", lights_view({"items": ITEMS, "current_index": 1}),
                            None, dt.datetime(2026, 10, 4, 18))
    assert p["cues"] == ["Now: 30%"]
    assert (p["next_title"], p["next_cue"]) == ("12% · halfway through the praise song",
                                                "Coming up: 12%")


def test_only_the_lights_clock_gets_the_heads_up(monkeypatch, isolated_state):
    from propresenterrunsheet import licensing
    from propresenterrunsheet.service_mate import daemon, state as sm_state
    monkeypatch.setattr(licensing, "service_mate_allowed", lambda: True)
    monkeypatch.setattr(licensing, "start_trial_if_needed", lambda: None)
    sm_state._write_clocks_config({"enabled": True, "clocks": [
        {"id": "lights", "ip": "10.0.0.3", "role": "lights", "verbosity": "compact"},
        {"id": "sound", "ip": "10.0.0.2", "role": "sound", "verbosity": "compact"}]})
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 1})
    sent = {}
    monkeypatch.setattr(daemon, "_probe_custom", lambda ip: True)
    monkeypatch.setattr(daemon, "_push_state", lambda ip, p: sent.setdefault(ip, p) or True)
    monkeypatch.setattr(daemon, "_maybe_advance_from_pp", lambda s: s)
    daemon._CLOCKS_LOOP_LAST_PUSHED.clear()
    daemon._ENDS_AT.reset()
    daemon._clocks_loop_tick(1)
    assert sent["10.0.0.3"]["cues"] == ["Now: 30%"]
    assert sent["10.0.0.3"]["next_title"] == "12% · halfway through the praise song"
    assert sent["10.0.0.2"]["next_title"] == "Welcome"       # sound: the plain next item


@pytest.mark.parametrize("verbosity", ["compact", "detailed"])
def test_an_item_saved_with_no_type_still_renders(verbosity):
    """Seen in the log 48 times: `"type": null` crashed the stock clock render."""
    from propresenterrunsheet.service_mate.render import _render_cue
    jpg = _render_cue("lights", {"items": [{"title": "Welcome", "type": None}],
                                 "current_index": 0}, verbosity=verbosity)
    assert jpg[:2] == b"\xff\xd8"


def test_the_preview_shows_the_lights_heads_up(sm_enabled):
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 1})
    r = sm_enabled.get("/api/clocks/preview?role=lights")
    assert r.status_code == 200 and r.data[:2] == b"\xff\xd8"


# ── The parse and the prompt ─────────────────────────────────────────────────

def test_the_prompt_asks_for_steps_and_carries_the_guide_only_when_given():
    from propresenterrunsheet.parsing.ai import (
        LIGHTING_GUIDE_MAX_CHARS, SERVICE_MATE_CUE_ADDENDUM, assemble_prompt,
    )
    assert "`lighting_steps`" in SERVICE_MATE_CUE_ADDENDUM and "never" in SERVICE_MATE_CUE_ADDENDUM
    plain = assemble_prompt("{RUNSHEET}", "6:00 Worship")
    assert "LIGHTING GUIDE" not in plain
    guided = assemble_prompt("{RUNSHEET}", "6:00 Worship", lighting_guide="Deep 2% · halfway" + "x" * 20000)
    assert "LIGHTING GUIDE" in guided and "first song is praise" in guided
    assert len(guided) < len(plain) + LIGHTING_GUIDE_MAX_CHARS + 2000      # capped


def test_the_parse_keeps_only_well_formed_steps(parse_client, monkeypatch):
    import requests
    reply = json.dumps({"service_name": "S", "items": [
        {"title": "Welcome", "type": "mc_on_stage", "lighting": "old field",
         "lighting_steps": [{"level": "  12%\n", "when": "as the host\nwalks on"}, "junk", {"when": "no level"}]},
        {"title": "Worship", "type": "song", "lighting_steps": [{"level": "x" * 99, "when": "y" * 99}] * 9},
        {"title": "Notices", "type": "announcement", "lighting_steps": {"level": "50%"}}]})

    class _R:
        status_code = 200
        def json(self):
            return {"model": "m", "choices": [{"message": {"content": reply}}]}
        def raise_for_status(self):
            return None
    monkeypatch.setattr(requests, "post", lambda *a, **k: _R())
    items = {it["title"]: it for it in parse_client.post("/api/upload_and_parse", data={
        "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "r.pdf"), "or_key": "k", "or_model": "m"},
        content_type="multipart/form-data").get_json()["items"]}
    assert items["Welcome"]["lighting_steps"] == [{"level": "12%", "when": "as the host walks on"}]
    assert "lighting" not in items["Welcome"]
    worship = items["Worship"]["lighting_steps"]
    assert len(worship) == 6 and len(worship[0]["level"]) == 30 and len(worship[0]["when"]) == 60
    assert items["Notices"]["lighting_steps"] == []


def test_the_saved_guide_reaches_the_parse_prompt(parse_client, monkeypatch):
    import requests
    from propresenterrunsheet import settings as pp_settings
    pp_settings.save_settings({"lighting_guide": "Deep worship — halfway through first worship song — 2%"})
    sent = {}

    class _R:
        status_code = 200
        def json(self):
            return {"model": "m", "choices": [{"message": {"content": '{"items": []}'}}]}
        def raise_for_status(self):
            return None

    def post(url, **kw):
        sent["prompt"] = json.dumps(kw.get("json"))
        return _R()
    monkeypatch.setattr(requests, "post", post)
    parse_client.post("/api/upload_and_parse", data={
        "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "r.pdf"), "or_key": "k", "or_model": "m"},
        content_type="multipart/form-data")
    assert "halfway through first worship song" in sent["prompt"]


# ── Routes ───────────────────────────────────────────────────────────────────

def test_the_api_reads_the_live_state(sm_enabled):
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 1})
    h = sm_enabled.get("/api/lighting").get_json()
    assert (h["now"], h["next"]["level"], h["then"]["level"], h["done"], h["section"]) == (
        "30%", "12%", "8%", 0, "Worship")


def _card(c):
    return c.get("/api/lighting").get_json()


def _tick(c, step=None, **body):
    """Tick off `step` (default: the one the card shows), as the card does."""
    if not body:
        n = step or _card(c)["next"]
        body = {"index": n["index"], "step": n["step"]}
    return c.post("/api/lighting/done", json=body)


def test_ticking_moves_the_next_step_up_and_undo_puts_it_back(sm_enabled):
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 1})
    assert _tick(sm_enabled).get_json()["ok"]
    h = _card(sm_enabled)
    assert (h["now"], h["next"]["level"], h["done"]) == ("12%", "8%", 1)
    assert _tick(sm_enabled, undo=True, done=1).get_json()["ok"]
    assert _card(sm_enabled)["next"]["level"] == "12%"


def test_a_double_click_or_an_old_card_ticks_nothing_extra(sm_enabled):
    """Each click names the step it saw. Once that step is done, the same
    click is refused, so a double-click can't skip the step after it."""
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 1})
    seen = _card(sm_enabled)["next"]
    assert _tick(sm_enabled, seen).status_code == 200
    assert _tick(sm_enabled, seen).status_code == 409                    # the second click
    assert _tick(sm_enabled, undo=True, done=0).status_code == 409       # undo from before the tick
    assert _card(sm_enabled)["done"] == 1


def test_one_undo_brings_back_the_last_change_across_a_repeat(sm_enabled):
    """12% → 12% → 8%: the repeated 12% is skipped going forward, and undo
    skips it going back, so one click always shows the change again."""
    from propresenterrunsheet.service_mate import state as sm_state
    items = [{"title": "Worship", "lighting_steps": [
        {"level": "12%", "when": "a"}, {"level": "12%", "when": "b"}, {"level": "8%", "when": "c"}]},
        {"title": "Message", "lighting_steps": [{"level": "20%", "when": "d"}]}]
    sm_state._write_runsheet_state({"items": items, "current_index": 0})
    _tick(sm_enabled)                                                    # 12%
    assert _card(sm_enabled)["next"]["level"] == "8%"
    _tick(sm_enabled)                                                    # 8%
    _tick(sm_enabled, undo=True, done=_card(sm_enabled)["done"])
    assert _card(sm_enabled)["next"]["level"] == "8%"


def test_ticks_belong_to_the_section_they_were_made_in(sm_enabled):
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 1})
    _tick(sm_enabled)
    state = sm_state._read_runsheet_state()
    sm_state._write_runsheet_state({**state, "current_index": 2})     # ProPresenter moved on
    assert _card(sm_enabled)["done"] == 0


def test_nothing_to_tick_once_the_section_is_done(sm_enabled):
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 3})   # Notices: a repeat only
    assert _tick(sm_enabled).status_code == 409        # its next change is Message's, not here


def test_a_rerun_mid_service_keeps_the_ticks(isolated_state):
    """Update mode re-run with the same items keeps the live position, and
    with it the lighting steps already ticked off."""
    from propresenterrunsheet.routes.playlist import _write_sm_state
    from propresenterrunsheet.service_mate import state as sm_state
    done = {"index": 1, "count": 2}
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 1, "lighting_done": done})
    _write_sm_state("Sunday", [{"parsed": it} for it in ITEMS], None, keep_position=True)
    assert sm_state._read_runsheet_state()["lighting_done"] == done
    _write_sm_state("Sunday", [{"parsed": it} for it in ITEMS[:2]], None, keep_position=True)
    assert "lighting_done" not in sm_state._read_runsheet_state()     # a different runsheet


def test_a_bad_current_index_does_not_break_the_card(sm_enabled):
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": "abc"})
    assert sm_enabled.get("/api/lighting").get_json()["section"] == "Walk-in"


def test_the_operators_own_text_is_saved_as_is(sm_enabled):
    """Pasted or corrected cues are the operator's: never re-tidied."""
    text = "1. Walk-in — when: countdown on — 30%\n2. Welcome — when: host walks on — 12%"
    g = sm_enabled.post("/api/lighting/guide", json={"text": text}).get_json()
    assert (g["has_guide"], g["text"], g["moments"], g["tidied"]) == (True, text, 2, None)
    assert sm_enabled.get("/api/lighting/guide").get_json()["text"] == text
    assert not sm_enabled.post("/api/lighting/guide", json={"clear": True}).get_json()["has_guide"]


@pytest.mark.parametrize("key, tidy, tidied", [
    ("sk-or-x", "1. Walk-in — when: countdown on — 30%", "ok"),
    ("", "", "no_key"),
    ("sk-or-x", "", "failed"),
])
def test_an_uploaded_guide_is_tidied_once(sm_enabled, monkeypatch, key, tidy, tidied):
    """One model call turns the jumbled PDF text into a numbered list; when
    that can't run, the text is kept as read and the page is told why."""
    import propresenterrunsheet.parsing.guide as guide_mod
    import propresenterrunsheet.routes.parse as parse_mod
    from propresenterrunsheet.settings import save_settings
    save_settings({"or_key": key})
    monkeypatch.setattr(parse_mod, "_extracted_or_error", lambda f: ("Walk-in 30% Countdown on", "pdf", None))
    monkeypatch.setattr(guide_mod, "tidy_guide", lambda text, key, model: tidy)
    g = sm_enabled.post("/api/lighting/guide", data={"file": (io.BytesIO(b"%PDF"), "guide.pdf")},
                        content_type="multipart/form-data").get_json()
    assert g["tidied"] == tidied
    assert g["text"] == (tidy or "Walk-in 30% Countdown on")


def test_tidying_keeps_only_the_numbered_lines(monkeypatch):
    import propresenterrunsheet.parsing.guide as guide_mod

    class _R:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content":
                    "Here you go:\n1. Walk-in — when: countdown on — 30%\n\n2. Welcome — when: host walks on — 12%\nHope that helps"}}]}
    monkeypatch.setattr(guide_mod, "fetch_catalogue", lambda: None)
    monkeypatch.setattr(guide_mod, "resolve_model", lambda *a, **k: "openai/gpt-4.1-mini")
    monkeypatch.setattr(guide_mod, "chat", lambda *a, **k: _R())
    assert guide_mod.tidy_guide("jumbled", "sk-or-x", "") == (
        "1. Walk-in — when: countdown on — 30%\n2. Welcome — when: host walks on — 12%")
    assert guide_mod.tidy_guide("jumbled", "", "") == ""       # no key: no call


def test_the_guide_is_part_of_service_mate(client):
    assert client.post("/api/lighting/guide", json={"text": "x"}).status_code == 409


def test_an_unreadable_guide_upload_says_so(sm_enabled):
    r = sm_enabled.post("/api/lighting/guide", data={"file": (io.BytesIO(b"not a pdf"), "guide.txt")},
                        content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"]


# ── The floating window ──────────────────────────────────────────────────────

class _FakeWindow:
    def __init__(self):
        self.destroyed = False
        self.events = type("E", (), {"closed": _Handlers()})()

    def destroy(self):
        self.destroyed = True


class _Handlers(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self

    def __isub__(self, fn):
        self.remove(fn)
        return self


@pytest.fixture
def fake_webview(monkeypatch):
    from propresenterrunsheet import native
    made = []

    class _Webview:
        @staticmethod
        def create_window(title, url, **kw):
            made.append((url, kw))
            return _FakeWindow()
    monkeypatch.setattr(native, "webview", _Webview)
    return made


def test_the_window_opens_on_top_inside_the_app_and_closes(sm_enabled, fake_webview):
    from propresenterrunsheet import native
    r = sm_enabled.post("/api/lighting/window", json={"on": True}).get_json()
    assert r["native"] is True
    (url, kw), = fake_webview
    assert url.endswith("/lighting") and kw["on_top"] is True
    win = native._lighting
    sm_enabled.post("/api/lighting/window", json={"on": False})
    assert win.destroyed and native._lighting is None
    assert sm_enabled.get("/api/lighting").get_json()["on"] is False


def test_closing_it_with_its_own_button_switches_the_setting_off(sm_enabled, fake_webview):
    from propresenterrunsheet import native
    sm_enabled.post("/api/lighting/window", json={"on": True})
    for handler in list(native._lighting.events.closed):
        handler()
    assert native._lighting is None
    assert sm_enabled.get("/api/lighting").get_json()["on"] is False


def test_no_native_window_when_the_page_opened_a_popup(sm_enabled, fake_webview):
    r = sm_enabled.post("/api/lighting/window", json={"on": True, "popup": True}).get_json()
    assert r["native"] is False and fake_webview == []


def test_in_a_browser_the_page_is_told_to_open_a_popup(sm_enabled):
    r = sm_enabled.post("/api/lighting/window", json={"on": True}).get_json()
    assert r == {"ok": True, "on": True, "native": False}


def test_switching_service_mate_off_closes_the_card(sm_enabled, fake_webview):
    from propresenterrunsheet import native
    sm_enabled.post("/api/lighting/window", json={"on": True})
    win = native._lighting
    sm_enabled.post("/api/clocks", json={"enabled": False})
    assert win.destroyed and native._lighting is None
    h = sm_enabled.get("/api/lighting").get_json()
    assert h["off"] is True and h["on"] is False


def test_closing_the_main_window_takes_the_card_but_keeps_it_on(monkeypatch, isolated_state):
    """pywebview only returns when every window is closed: the card must
    close with the main window, or the app never quits."""
    import types
    from propresenterrunsheet import native, server
    closed = _Handlers()
    fake = types.SimpleNamespace(
        create_window=lambda *a, **k: types.SimpleNamespace(events=types.SimpleNamespace(closed=closed)),
        start=lambda: None)
    assert server._run_native_window(5757, webview_module=fake)
    assert native.close_lighting_window in closed


def test_a_failed_native_start_leaves_no_phantom_window(isolated_state):
    import types
    from propresenterrunsheet import native, server

    def boom():
        raise RuntimeError("no WebView2")
    fake = types.SimpleNamespace(create_window=lambda *a, **k: object(), start=boom)
    assert server._run_native_window(5757, webview_module=fake) is False
    assert native.webview is None


def test_it_is_part_of_service_mate(client):
    """Off with the master switch, like every other Service Mate action."""
    assert client.post("/api/lighting/window", json={"on": True}).status_code == 409


def test_the_card_page_is_served(client):
    page = client.get("/lighting").data
    assert b"Lighting" in page and b"/api/lighting/done" in page
