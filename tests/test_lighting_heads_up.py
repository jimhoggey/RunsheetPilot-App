"""The lighting heads-up: where the lights are now, and the next change.

Owner's spec (2 Oct 2026): runsheets now carry lighting settings ("set
lights to 50%"). Show what's COMING UP — never "change now", because the
moment is the room's call ("thanks band"). Only what the runsheet states;
a level with no lights named means the house lights; a repeated setting
isn't a change. Shown on the Lights clock, and in an optional floating
always-on-top window.
"""
import io
import json

import pytest

from propresenterrunsheet.service_mate.lighting import has_lighting, heads_up, lights_view

ITEMS = [
    {"title": "Pre-service", "lighting": "House lights 50%"},
    {"title": "Worship", "lighting": "House lights 12%"},
    {"title": "Prayer", "lighting": ""},
    {"title": "Response", "lighting": "house  LIGHTS 12%"},   # the same setting again
    {"title": "Message", "lighting": "House lights 30%"},
    {"title": "Close"},
]


@pytest.mark.parametrize("idx, now, nxt, at", [
    (0, "House lights 50%", "House lights 12%", "Worship"),
    (1, "House lights 12%", "House lights 30%", "Message"),   # skips the repeat
    (2, "House lights 12%", "House lights 30%", "Message"),   # nothing stated: still 12%
    (4, "House lights 30%", "", ""),                          # no more changes
])
def test_now_and_the_next_change(idx, now, nxt, at):
    h = heads_up(ITEMS, idx)
    assert (h["now"], h["next"], h["next_section"]) == (now, nxt, at)


def test_a_runsheet_with_no_lighting_says_nothing():
    items = [{"title": "Welcome", "cues": {"lights": ["Stage wash"]}}]   # a model suggestion
    assert not has_lighting(items)
    assert heads_up(items, 0)["now"] == "" and heads_up(items, 0)["next"] == ""


def test_the_lights_clock_shows_now_and_coming_up():
    view = lights_view({"items": ITEMS, "current_index": 1, "current_started_at": "x"})
    assert [it["title"] for it in view["items"]] == ["Worship", "Message"]
    assert view["current_index"] == 0 and view["current_started_at"] == "x"
    assert view["items"][0]["cues"]["lights"] == ["Now: House lights 12%"]
    assert view["items"][1]["cues"]["lights"] == ["Coming up: House lights 30%"]
    assert ITEMS[1].get("cues") is None          # the real state is untouched


def test_the_lights_clock_is_unchanged_without_stated_lighting():
    state = {"items": [{"title": "Welcome", "cues": {"lights": ["Spot"]}}], "current_index": 0}
    assert lights_view(state) is state


def test_the_payload_for_the_lights_station(monkeypatch):
    import datetime as dt
    from propresenterrunsheet.service_mate.protocol import build_state_payload
    view = lights_view({"items": ITEMS, "current_index": 0})
    p = build_state_payload("lights", "compact", view, None, dt.datetime(2026, 10, 4, 18))
    assert p["cues"] == ["Now: House lights 50%"]
    assert (p["next_title"], p["next_cue"]) == ("Worship", "Coming up: House lights 12%")


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
    assert sent["10.0.0.3"]["cues"] == ["Now: House lights 12%"]
    assert sent["10.0.0.3"]["next_title"] == "Message"
    assert sent["10.0.0.2"]["next_title"] == "Prayer"       # sound: the plain next item


@pytest.mark.parametrize("verbosity", ["compact", "detailed"])
def test_an_item_saved_with_no_type_still_renders(verbosity):
    """Seen in the log 48 times: `"type": null` crashed the stock clock render."""
    from propresenterrunsheet.service_mate.render import _render_cue
    jpg = _render_cue("lights", {"items": [{"title": "Welcome", "type": None}],
                                 "current_index": 0}, verbosity=verbosity)
    assert jpg[:2] == b"\xff\xd8"


def test_the_preview_shows_the_lights_heads_up(sm_enabled):
    """The Lights station's preview must match its real clock."""
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 1})
    r = sm_enabled.get("/api/clocks/preview?role=lights")
    assert r.status_code == 200 and r.data[:2] == b"\xff\xd8"


# ── The parse ────────────────────────────────────────────────────────────────

def test_the_prompt_asks_for_the_stated_lighting_only():
    from propresenterrunsheet.parsing.ai import SERVICE_MATE_CUE_ADDENDUM as add
    assert "`lighting`" in add and "never suggest one" in add and "house lights" in add.lower()


def test_the_parse_keeps_only_a_short_line_of_lighting(parse_client, monkeypatch):
    import requests
    reply = json.dumps({"service_name": "S", "items": [
        {"title": "Welcome", "type": "mc_on_stage", "lighting": "  House\nlights   50% "},
        {"title": "Worship", "type": "song", "lighting": ["House lights 12%"]},
        {"title": "Notices", "type": "announcement", "lighting": "x" * 200}]})

    class _R:
        status_code = 200
        def json(self):
            return {"model": "m", "choices": [{"message": {"content": reply}}]}
        def raise_for_status(self):
            return None
    monkeypatch.setattr(requests, "post", lambda *a, **k: _R())
    items = parse_client.post("/api/upload_and_parse", data={
        "pdf": (io.BytesIO(b"%PDF-1.4 fake"), "r.pdf"), "or_key": "k", "or_model": "m"},
        content_type="multipart/form-data").get_json()["items"]
    got = {it["title"]: it["lighting"] for it in items}
    assert got["Welcome"] == "House lights 50%"
    assert got["Worship"] == ""                      # not a string: dropped
    assert len(got["Notices"]) == 60


# ── The routes and the floating window ───────────────────────────────────────

def test_the_api_reads_the_live_state(sm_enabled):
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": 1})
    h = sm_enabled.get("/api/lighting").get_json()
    assert (h["now"], h["next"], h["next_section"], h["has_lighting"], h["section"]) == (
        "House lights 12%", "House lights 30%", "Message", True, "Worship")


def test_a_bad_current_index_does_not_break_the_card(sm_enabled):
    from propresenterrunsheet.service_mate import state as sm_state
    sm_state._write_runsheet_state({"items": ITEMS, "current_index": "abc"})
    assert sm_enabled.get("/api/lighting").get_json()["section"] == "Pre-service"


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


def test_it_is_part_of_service_mate(client):
    """Off with the master switch, like every other Service Mate action."""
    assert client.post("/api/lighting/window", json={"on": True}).status_code == 409


def test_the_card_page_is_served(client):
    page = client.get("/lighting").data
    assert b"Lighting" in page and b"/api/lighting" in page
