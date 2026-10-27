"""Auto-track by identity, not by re-guessing a header name every poll.

Owner's spec (27 Oct 2026): "I don't want to be clicking the next and
preview buttons. It should automatically queue, automatically ask
ProPresenter where are you, and then automatically know what to show on
the Service Mate display."

It already asked. What it could not reliably do was understand the
answer: every poll fuzzy-matched the active section's header NAME against
the runsheet titles at 0.6, so a tidied header, a template's own wording
or update mode's ↕ dropped it under the threshold and tracking went
quiet. These tests cover recording ProPresenter's own uuids at the moment
the playlist is written, and following them afterwards.
"""
import time

import pytest

from propresenterrunsheet.propresenter.anchors import anchor_labels, read_anchors
from propresenterrunsheet.service_mate import pp_track

ITEMS = [{"type": "welcome", "title": "Welcome"},
         {"type": "song", "title": "Build My Life"},
         {"type": "sermon", "title": "King Jesus"}]


def _matched(items=None, song_match=None):
    out = []
    for it in items or ITEMS:
        mi = {"parsed": dict(it), "match": None}
        if song_match and it["type"] == "song":
            mi["match"] = song_match
        out.append(mi)
    return out


class _Reply:
    def __init__(self, items, ok=True):
        self._items, self.ok = items, ok

    def json(self):
        return {"items": self._items}

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("ProPresenter said no")


def _pp_item(name, uuid, kind="header"):
    return {"id": {"uuid": uuid, "name": name, "index": 0}, "type": kind}


# ── What gets anchored ──────────────────────────────────────────────────────

def test_every_runsheet_item_gets_the_name_pp_will_carry():
    labels = anchor_labels(_matched())
    # Whatever decorations header_label adds, the label is the one the PUT
    # writes — derived from build_playlist_payload itself, so it cannot drift.
    assert sorted(set(labels.values())) == [0, 1, 2]


def test_update_modes_marked_header_anchors_to_the_same_item():
    """A header update mode could not place is the same label behind a ↕ —
    and is exactly the one the old name matching could not read back."""
    labels = anchor_labels(_matched())
    plain = [n for n, i in labels.items() if i == 0 and not n.startswith("↕")]
    assert ["↕ " + n in labels for n in plain] == [True]
    assert labels["↕ " + plain[0]] == 0


def test_a_name_two_items_share_is_dropped_rather_than_guessed():
    """An anchor pointing at the wrong segment is worse than no anchor:
    without one you get the fallback, with a wrong one you get the wrong
    cue on every clock in the building."""
    twice = [{"type": "welcome", "title": "Welcome"},
             {"type": "welcome", "title": "Welcome"}]
    assert anchor_labels(_matched(twice)) == {}


@pytest.mark.parametrize("matched", [[], None])
def test_nothing_to_anchor_is_not_an_error(matched):
    assert anchor_labels(matched) == {}


# ── Reading ProPresenter's own uuids back ───────────────────────────────────

def test_the_playlist_read_back_pairs_uuids_with_runsheet_items():
    labels = anchor_labels(_matched())
    name = next(n for n, i in labels.items() if i == 2 and not n.startswith("↕"))
    got = read_anchors("http://pp", "PL1", labels,
                       http_get=lambda url, **kw: _Reply([
                           _pp_item(name, "U-SERMON"),
                           _pp_item("Some slide the operator added", "U-MEDIA",
                                    "presentation")]))
    assert got == {"playlist": "PL1", "by_uuid": {"U-SERMON": 2}}


def test_two_playlist_items_with_one_label_both_mean_that_segment():
    """Update mode writes into a playlist that may already hold a header by
    this name — and ProPresenter gives each its own uuid. Anchoring both is
    right: whichever the operator clicks, it is the same segment."""
    got = read_anchors("http://pp", "PL1", {"Welcome": 0},
                       http_get=lambda url, **kw: _Reply([
                           _pp_item("Welcome", "FIRST"),
                           _pp_item("Welcome", "SECOND")]))
    assert got["by_uuid"] == {"FIRST": 0, "SECOND": 0}


@pytest.mark.parametrize("reply", [
    _Reply([], ok=False),                      # PP refused
    _Reply([]),                                # empty playlist
    _Reply([_pp_item("Nothing we wrote", "U")]),   # no name we recognise
    _Reply([_pp_item("Welcome", "")]),         # no uuid to anchor on
])
def test_a_playlist_that_cannot_be_read_back_anchors_nothing(reply):
    """Tracking then falls back to matching header names, which is also
    what still carries a playlist Runsheet Pilot did not build."""
    assert read_anchors("http://pp", "PL1", {"Welcome": 0},
                        http_get=lambda url, **kw: reply) == {}


def test_a_dead_connection_anchors_nothing():
    def _boom(url, **kw):
        raise OSError("no route to host")
    assert read_anchors("http://pp", "PL1", {"Welcome": 0}, http_get=_boom) == {}


@pytest.mark.parametrize("base, pid, labels", [
    ("", "PL1", {"Welcome": 0}), ("http://pp", "", {"Welcome": 0}),
    ("http://pp", "PL1", {}),
])
def test_nothing_to_ask_about_makes_no_request(base, pid, labels):
    def _boom(url, **kw):
        raise AssertionError("asked PP with nothing to anchor")
    assert read_anchors(base, pid, labels, http_get=_boom) == {}


# ── Following them ──────────────────────────────────────────────────────────

PLIST = [_pp_item("Welcome — 10:00 AM", "U-WELCOME"),
         _pp_item("Opener film", "U-FILM", "media"),
         _pp_item("Build My Life", "U-SONG", "presentation"),
         _pp_item("King Jesus — 10:30 AM", "U-SERMON")]

ANCHORED = {"items": ITEMS,
            "pp_anchors": {"playlist": "PL1",
                           "by_uuid": {"U-WELCOME": 0, "U-SONG": 1,
                                       "U-SERMON": 2}}}


@pytest.mark.parametrize("pos, expect", [
    (0, 0),    # on the header itself
    (1, 0),    # a media slide under it — still that segment
    (2, 1),    # the song anchors to its own item, not the header above it
    (3, 2),
])
def test_the_active_position_resolves_by_uuid(pos, expect):
    assert pp_track._anchored_index(ANCHORED, PLIST, pos, "PL1") == expect


def test_a_renamed_header_still_tracks():
    """The whole point of recording the uuid: the operator can tidy the
    name in ProPresenter and nothing breaks."""
    renamed = [dict(it, id={**it["id"], "name": "WELCOME (final) v2"})
               for it in PLIST]
    assert pp_track._anchored_index(ANCHORED, renamed, 0, "PL1") == 0


@pytest.mark.parametrize("state, playlist", [
    (ANCHORED, "SOME-OTHER-PLAYLIST"),            # not the one we anchored
    ({"items": ITEMS}, "PL1"),                    # never anchored
    ({"items": ITEMS, "pp_anchors": {}}, "PL1"),
    ({"items": ITEMS, "pp_anchors": {"playlist": "PL1", "by_uuid": {}}}, "PL1"),
    # An index that no longer exists — a shorter runsheet loaded since.
    ({"items": ITEMS[:1],
      "pp_anchors": {"playlist": "PL1", "by_uuid": {"U-SERMON": 2}}}, "PL1"),
])
def test_without_a_usable_anchor_it_says_so_rather_than_guessing(state, playlist):
    assert pp_track._anchored_index(state, PLIST, 3, playlist) is None


def test_nothing_anchored_above_the_active_item_is_not_a_match():
    """Media dragged in above the first header: there is no segment it
    belongs to, so tracking leaves the cue where the operator put it."""
    orphan = [_pp_item("Pre-service loop", "U-LOOP", "media")] + PLIST
    bare = {"items": ITEMS,
            "pp_anchors": {"playlist": "PL1", "by_uuid": {"U-SERMON": 2}}}
    assert pp_track._anchored_index(bare, orphan, 0, "PL1") is None


def test_the_probe_prefers_the_anchor_over_the_name_match(monkeypatch):
    """Both could answer here; identity wins, and says which it used."""
    monkeypatch.setattr(pp_track, "_pp_get_playlist_items",
                        lambda base, uuid: PLIST)

    class _R:
        ok = True

        @staticmethod
        def json():
            return {"presentation": {
                "playlist": {"uuid": "PL1"},
                "playlist_item": {"id": {"uuid": "U-SONG", "index": 2}}}}
    monkeypatch.setattr("requests.get", lambda url, **kw: _R())
    got = pp_track._pp_active_section_probe(ANCHORED, "http://pp")
    assert (got["section_idx"], got["source"]) == (1, "anchor")


def test_following_is_reported_while_it_lasts(monkeypatch):
    monkeypatch.setattr(pp_track, "PP_FOLLOWING",
                        {"source": "anchor", "at": time.time(), "live": True})
    assert pp_track.follow_status() == {"reachable": True, "following": True,
                                        "source": "anchor", "live": True}


def test_propresenter_answering_with_nothing_running_is_not_a_miss(monkeypatch):
    """The ordinary state before a service starts. It must read differently
    from "running something I can't place", which is a real problem."""
    monkeypatch.setattr(pp_track, "PP_FOLLOWING",
                        {"source": "", "at": time.time(), "live": False})
    got = pp_track.follow_status()
    assert (got["reachable"], got["following"], got["live"]) == (True, False, False)
