"""Service Mate keeps up with ProPresenter: PP is asked every tick (0.5 s).

Reported 2 Oct 2026: with "Follow ProPresenter" on, a click in PP took
about 5 s to reach the clocks. PP was polled every 4th tick (2 s), and a
section must come back twice in a row before Service Mate moves (pp_track's
stickiness), so the clocks were 2-4 s behind before they even repainted.
"""
import pytest
import requests

from propresenterrunsheet.service_mate import daemon, pp_track, state as sm_state


@pytest.fixture
def loop(monkeypatch, isolated_state):
    """One custom-firmware clock, a two-item runsheet, PP answering."""
    from propresenterrunsheet import licensing
    monkeypatch.setattr(licensing, "service_mate_allowed", lambda: True)
    monkeypatch.setattr(licensing, "start_trial_if_needed", lambda: None)
    sm_state._write_clocks_config({"enabled": True, "clocks": [
        {"id": "screen", "ip": "10.0.0.1", "role": "screen", "verbosity": "compact"}]})
    sm_state._write_runsheet_state({"items": [{"title": "Welcome"}, {"title": "Message"}],
                                    "current_index": 0})
    monkeypatch.setattr(daemon, "_probe_custom", lambda ip: True)
    monkeypatch.setattr(daemon, "_push_state", lambda ip, p: True)
    monkeypatch.setitem(pp_track.PP_REACHABLE, "ok", True)
    daemon._ENDS_AT.reset()
    return monkeypatch


def _count_polls(loop, advance=lambda s: s):
    polls = []
    loop.setattr(daemon, "_maybe_advance_from_pp", lambda s: (polls.append(1), advance(s))[1])
    return polls


@pytest.mark.parametrize("reachable, ticks, polls", [(True, 4, 4), (False, 8, 2)])
def test_pp_is_asked_every_tick_unless_it_is_not_answering(loop, reachable, ticks, polls):
    seen = _count_polls(loop)
    loop.setitem(pp_track.PP_REACHABLE, "ok", reachable)
    for tick in range(ticks):
        daemon._clocks_loop_tick(tick)
    assert len(seen) == polls


def test_the_state_file_is_written_only_when_pp_moved_something(loop):
    writes = []
    loop.setattr(daemon, "_write_runsheet_state", writes.append)
    _count_polls(loop)
    for tick in range(3):
        daemon._clocks_loop_tick(tick)
    assert writes == []
    _count_polls(loop, advance=lambda s: {**s, "current_index": 1})
    daemon._clocks_loop_tick(3)
    assert [w["current_index"] for w in writes] == [1]


def test_reachability_follows_the_last_request(loop):
    def refused(url, **kw):
        raise requests.exceptions.ConnectionError("PP closed")
    loop.setattr(requests, "get", refused)
    pp_track._pp_active_section_probe({"items": []}, "http://127.0.0.1:1")
    assert pp_track.PP_REACHABLE["ok"] is False

    class _NotFound:
        ok, status_code = False, 404
    loop.setattr(requests, "get", lambda url, **kw: _NotFound())
    pp_track._pp_active_section_probe({"items": []}, "http://127.0.0.1:1")
    assert pp_track.PP_REACHABLE["ok"] is True


def test_a_host_that_will_not_resolve_counts_as_not_answering(loop):
    """"worship-mac.local" with that Mac switched off: the lookup fails
    before any request is made, and must still trigger the backoff."""
    from propresenterrunsheet import settings as pp_settings
    from propresenterrunsheet.propresenter import net
    loop.setattr(pp_settings, "load_settings", lambda: {"pp_host": "worship-mac.local"})
    loop.setattr(net, "pp_base", lambda *_a: (_ for _ in ()).throw(net.UnreachableHost("x")))
    pp_track._maybe_advance_from_pp({"items": [{"title": "Welcome"}]})
    assert pp_track.PP_REACHABLE["ok"] is False


def test_a_failed_name_lookup_is_remembered_briefly(monkeypatch):
    """A failing lookup can take ~5 s; asking again on every poll froze the
    countdowns. It is cached (still as "not allowed") for a few seconds."""
    from propresenterrunsheet.propresenter import net
    lookups = []

    def fails(host, *_a, **_k):
        lookups.append(host)
        raise OSError("no such host")
    monkeypatch.setattr(net.socket, "getaddrinfo", fails)
    net.reset_cache()
    try:
        assert net.resolve_pp_host("worship-mac.local") is None
        assert net.resolve_pp_host("worship-mac.local") is None
        assert lookups == ["worship-mac.local"]
    finally:
        net.reset_cache()


def test_a_section_change_in_pp_reaches_service_mate_within_two_ticks(loop):
    """End to end through the real tracker: the operator clicks "Message"
    in PP; two ticks later (one second) Service Mate is on it."""
    class _R:
        def __init__(self, data):
            self.ok, self.status_code, self._data = True, 200, data

        def json(self):
            return self._data

    playlist = [{"id": {"uuid": "h0", "name": "Welcome", "index": 0}, "type": "header"},
                {"id": {"uuid": "m0", "name": "Welcome loop", "index": 1}, "type": "media"},
                {"id": {"uuid": "h1", "name": "Message", "index": 2}, "type": "header"}]
    answers = {
        "/v1/playlist/active": {"presentation": {"playlist": {"uuid": "P"},
                                                 "playlist_item": {"id": {"uuid": "h1", "index": 2}}}},
        "/v1/playlist/P": {"items": playlist},
        "/v1/timers/current": [],
    }
    loop.setattr(requests, "get", lambda url, **kw: _R(next(
        v for k, v in answers.items() if url.endswith(k))))
    loop.setattr(pp_track, "_PP_PLAYLIST_CACHE", {"uuid": None, "items": [], "fetched_at": 0.0})
    loop.setattr(pp_track, "_PENDING_SECTION_TARGET", {"index": None, "count": 0})

    daemon._clocks_loop_tick(0)          # seen once: not trusted yet
    assert sm_state._read_runsheet_state()["current_index"] == 0
    daemon._clocks_loop_tick(1)          # seen twice: Service Mate moves
    assert sm_state._read_runsheet_state()["current_index"] == 1
