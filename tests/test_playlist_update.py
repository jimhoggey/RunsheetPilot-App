"""Unit tests for the update-existing-playlist merge engine.

Update mode's promise is narrow and absolute: every slide the operator
put in that playlist is still there, in the same order, and the only
thing that changed is the section headers. These tests pin that promise
and the placement rules that make the headers useful — see
propresenter/playlist_update.py for why placement is built around the
MISS rather than the hit."""

from propresenterrunsheet.propresenter.playlist_update import (
    UNPLACED_MARK,
    anchor_tokens,
    build_update_payload,
    echo_existing_item,
    recall_key,
    split_existing,
    title_token_set,
    verify_content_preserved,
    visible_signature,
)


def _media(name, uuid="A", target=None, **extra):
    return {"id": {"uuid": uuid, "name": name, "index": 0}, "type": "media",
            "target_uuid": target if target is not None else f"T-{uuid}",
            "is_hidden": False, "is_pco": False, **extra}


def _pres(name, uuid="P", pres_uuid=None, **extra):
    return {"id": {"uuid": uuid, "name": name, "index": 0},
            "type": "presentation",
            "presentation_info": {"presentation_uuid": pres_uuid or f"PU-{uuid}"},
            "is_hidden": False, "is_pco": False, **extra}


def _header(name):
    return {"id": {"uuid": "", "name": name, "index": 0}, "type": "header",
            "target_uuid": "", "is_hidden": False, "is_pco": False,
            "header_color": {"red": 1, "green": 0, "blue": 0, "alpha": 1}}


def _item(title, type_="other", **extra):
    return {"parsed": {"type": type_, "title": title, **extra}}


def _names(items):
    return [(it.get("id") or {}).get("name") for it in items]


def _content(items):
    return [it for it in items if it["type"] != "header"]


# ── the promise ───────────────────────────────────────────────────────────

def test_every_existing_item_survives_in_its_original_order():
    """The one thing update mode may never get wrong. Media the runsheet
    says nothing about must come through untouched — that is the whole
    reason an operator reaches for this instead of Create."""
    existing = [_media("PRESERVICE LOOP", "1"), _media("CTA_GIVING", "2"),
                _pres("Goodness Of God", "3"), _media("IMG_4021", "4")]
    items, _ = build_update_payload(existing, [_item("Pre-service")])
    assert _names(_content(items)) == [
        "PRESERVICE LOOP", "CTA_GIVING", "Goodness Of God", "IMG_4021"]


def test_existing_headers_are_replaced_not_accumulated():
    """The runsheet is the single source of truth for this playlist's
    organisation, so running twice is a no-op rather than a second set
    of headers."""
    existing = [_header("Old section"), _media("PRESERVICE LOOP", "1")]
    items, report = build_update_payload(existing, [_item("Pre-service")])
    assert "Old section" not in _names(items)
    assert report["headers_removed"] == 1
    # Feed the result back in: same playlist out, no duplication.
    again, _ = build_update_payload(items, [_item("Pre-service")])
    assert _names(again) == _names(items)


def test_no_matching_content_still_delivers_every_header():
    """~1 service in 5 anchors nothing. It must still be worth running:
    12 coloured headers with times, stacked at the top under a banner,
    beat typing them into ProPresenter by hand."""
    existing = [_media("IMG_4021", "1"), _media("Comp 1_1", "2")]
    runsheet = [_item("Welcome"), _item("Offering"), _item("Preach")]
    items, report = build_update_payload(existing, runsheet)
    assert report["anchored"] == 0 and report["unplaced"] == 3
    assert "DRAG THESE INTO PLACE" in items[0]["id"]["name"]
    assert _names(items)[1:4] == [UNPLACED_MARK + "Welcome",
                                  UNPLACED_MARK + "Offering",
                                  UNPLACED_MARK + "Preach"]
    assert _names(_content(items)) == ["IMG_4021", "Comp 1_1"]


def test_empty_playlist_is_organised_not_treated_as_a_failure():
    """[] is a real, empty playlist — distinct from a failed read, which
    never reaches this function (see fetch_pp_playlist_raw)."""
    items, report = build_update_payload([], [_item("Welcome")])
    assert _names(items) == ["Welcome"]
    assert report["anchored"] == 0
    # No banner: there is nothing to drag headers into place among.
    assert "DRAG" not in items[0]["id"]["name"]


# ── placement ─────────────────────────────────────────────────────────────

def test_header_lands_directly_above_the_item_it_names():
    existing = [_media("Countdown", "1"), _media("WELCOME SLIDE", "2")]
    items, report = build_update_payload(
        existing, [_item("Welcome and Connection Cards")])
    assert _names(items) == ["Countdown",
                             "Welcome and Connection Cards",
                             "WELCOME SLIDE"]
    assert report["anchored"] == 1 and report["by_name"] == 1


def test_hyphenated_runsheet_wording_matches_a_concatenated_media_name():
    """"Pre-service" → "PRESERVICE LOOP". The single loosening on the
    title side, and the one that earns its keep on real church data."""
    assert "preservice" in title_token_set("Pre-service")
    assert anchor_tokens("PRESERVICE LOOP") == {"preservice"}
    items, _ = build_update_payload(
        [_media("PRESERVICE LOOP", "1")], [_item("Pre-service")])
    assert _names(items) == ["Pre-service", "PRESERVICE LOOP"]


def test_unplaced_items_before_the_first_anchor_go_to_the_top():
    """Not buried immediately above a late anchor: they happen at the
    start of the service and the operator reads top-down."""
    existing = [_media("IMG_01", "1"), _media("IMG_02", "2"),
                _media("WELCOME SLIDE", "3")]
    items, _ = build_update_payload(
        existing, [_item("Pre-service music"), _item("Welcome")])
    assert _names(items) == [UNPLACED_MARK + "Pre-service music",
                             "IMG_01", "IMG_02", "Welcome", "WELCOME SLIDE"]


def test_unplaced_items_between_anchors_stack_above_the_later_one():
    """Deliberately NOT spread across the gap — spreading attaches
    specific slides to an item we failed to identify, which looks like
    knowledge and isn't."""
    existing = [_media("WELCOME SLIDE", "1"), _media("IMG_01", "2"),
                _media("IMG_02", "3"), _media("OFFERING", "4")]
    items, _ = build_update_payload(
        existing, [_item("Welcome"), _item("Notices"), _item("Offering")])
    assert _names(items) == ["Welcome", "WELCOME SLIDE", "IMG_01", "IMG_02",
                             UNPLACED_MARK + "Notices", "Offering", "OFFERING"]


def test_unplaced_items_after_the_last_anchor_go_to_the_end():
    existing = [_media("WELCOME SLIDE", "1"), _media("IMG_01", "2")]
    items, _ = build_update_payload(
        existing, [_item("Welcome"), _item("Preach")])
    assert _names(items)[-1] == UNPLACED_MARK + "Preach"


def test_anchors_can_never_cross():
    """Both lists are in service order, so an anchor set that goes
    backwards is incoherent. The weaker of two crossing matches is
    dropped and its item is marked unplaced rather than silently
    reordering the operator's service."""
    existing = [_media("OFFERING", "1"), _media("WELCOME SLIDE", "2")]
    items, report = build_update_payload(
        existing, [_item("Welcome"), _item("Offering")])
    assert _names(_content(items)) == ["OFFERING", "WELCOME SLIDE"]
    assert report["anchored"] == 1 and report["unplaced"] == 1


def test_one_token_name_cannot_outrank_a_two_token_name():
    """Noise-stripping reduces "WELCOME SLIDE" to {welcome}, which would
    happily swallow any line containing that word. A stripped-to-one
    name only ever scores a weak anchor."""
    existing = [_media("WELCOME SLIDE", "1"), _media("Welcome Kids", "2")]
    items, _ = build_update_payload(existing, [_item("Welcome Kids Moment")])
    assert _names(items) == ["WELCOME SLIDE", "Welcome Kids Moment",
                             "Welcome Kids"]


# ── recall: the operator's drag is the strongest signal there is ──────────

def test_recall_key_ignores_the_time_and_duration_the_label_carries():
    assert (recall_key("↕ Offering & Announcements — 10:05 AM (6 min)")
            == recall_key("Offering & Announcements"))
    assert recall_key("⚠ ACTION NEEDED — Mystery Song") == "mystery song"
    assert recall_key("📖 John 3 — 9:40 AM") == "john 3"


def test_split_existing_reads_last_weeks_placement_out_of_the_playlist():
    existing = [_header("Notices — 10:05 AM (6 min)"), _media("IMG_4021", "9")]
    kept, recalled = split_existing(existing)
    assert _names(kept) == ["IMG_4021"]
    assert recalled["notices"] == ("T-9", "img_4021")


def test_a_remembered_drag_beats_a_name_match_elsewhere():
    """The operator moved "Notices" above IMG_4021 last week. That is a
    direct answer to the question the name rule can only guess at, so it
    wins — and keeps winning every week after."""
    existing = [_header("Notices — 10:05 AM"), _media("IMG_4021", "9"),
                _media("NOTICES SLIDE", "8")]
    items, report = build_update_payload(existing, [_item("Notices")])
    assert _names(items) == ["Notices", "IMG_4021", "NOTICES SLIDE"]
    assert report["by_recall"] == 1 and report["by_name"] == 0


def test_two_headers_with_the_same_wording_cannot_claim_one_slide():
    existing = [_header("Worship"), _media("IMG_01", "1"),
                _header("Worship"), _media("IMG_02", "2")]
    _, recalled = split_existing(existing)
    assert recalled["worship"] == ("T-1", "img_01")


# ── echoing existing items back to ProPresenter ───────────────────────────

def test_echo_preserves_loop_duration_and_presentation_info():
    """PP keeps loop behaviour only if `duration` and
    `presentation_info` come back with the item."""
    raw = _pres("END SERVICE LOOP", "5", duration=300,
                destination="presentation")
    out = echo_existing_item(raw)
    assert out["duration"] == 300
    assert out["presentation_info"] == {"presentation_uuid": "PU-5"}
    assert out["destination"] == "presentation"
    # id.uuid mirrors the ASSET uuid, not the playlist-item uuid.
    assert out["id"]["uuid"] == "PU-5"
    # target_uuid must be present on every item or PP's PUT 400s.
    assert out["target_uuid"] == ""


def test_echo_passes_through_fields_this_app_has_never_seen():
    """Verbatim echo is the only safe default when rewriting someone
    else's playlist: a field we drop is a field the operator loses."""
    raw = _media("Clip", "7", some_future_pp_field={"a": 1})
    assert echo_existing_item(raw)["some_future_pp_field"] == {"a": 1}


def test_echo_does_not_mutate_the_snapshot():
    raw = _media("Clip", "7")
    echo_existing_item(raw)["id"]["uuid"] = "CLOBBERED"
    assert raw["id"]["uuid"] == "7"


# ── verification ──────────────────────────────────────────────────────────

def test_verify_ignores_header_changes_but_catches_a_missing_slide():
    before = [_header("old"), _media("A", "1"), _media("B", "2")]
    after_ok = [_header("new"), _media("A", "1"), _media("B", "2")]
    after_bad = [_header("new"), _media("A", "1")]
    assert verify_content_preserved(before, after_ok)["ok"]
    check = verify_content_preserved(before, after_bad)
    assert not check["ok"] and check["missing"] == ["b"]


def test_verify_catches_a_reorder():
    before = [_media("A", "1"), _media("B", "2")]
    after = [_media("B", "2"), _media("A", "1")]
    check = verify_content_preserved(before, after)
    assert not check["ok"] and check["reordered"] is True


def test_verify_tolerates_propresenter_minting_new_item_uuids():
    """PP assigns fresh playlist-item uuids on write. Comparing those
    would report every healthy update as data loss and roll it back."""
    before = [_media("A", "1")]
    after = [_media("A", "REMINTED", target="T-1")]
    assert verify_content_preserved(before, after)["ok"]


def test_verify_tolerates_propresenter_minting_new_media_ids_too():
    """Checked against a live ProPresenter 21.4: a write re-mints the
    media target_uuid as well, on every item, every time. Only type, name
    and order are stable, so they are all the check may compare."""
    before = [_media("A", "1", target="T-1"), _media("B", "2", target="T-2")]
    after = [_media("A", "X1", target="NEW-1"), _media("B", "X2", target="NEW-2")]
    assert verify_content_preserved(before, after)["ok"]
    # ...while a dropped or reordered slide is still caught.
    assert not verify_content_preserved(before, after[:1])["ok"]
    assert not verify_content_preserved(before, after[::-1])["ok"]


# ── the live ProPresenter 21.4 test playlist, reproduced ──────────────────
#
# Ten stills named the way operators name them, and a ten-line runsheet.
# Against the real thing the first version anchored NONE of them (the
# "01" prefix was a required word), and a second run wrote again because
# a stack of unplaced headers was "recalled" onto the first slide.

_LIVE_NAMES = ["01_welcome", "02_worship", "03_song_1", "04_song_2",
               "05_announcements", "06_giving", "07_tithing", "08_message",
               "09_prayer", "10_thanks_for_coming"]
_LIVE_RUNSHEET = [_item(t) for t in [
    "Pre-service", "Welcome", "Worship", "Song 1", "Song 2",
    "Announcements", "Offering", "Message - Ps Cathie", "Prayer Ministry",
    "Close"]]


def _live_playlist():
    return [_media(n, str(i)) for i, n in enumerate(_LIVE_NAMES)]


def _as_pp_reads_it_back(items):
    """Every id re-minted; headers lose target_uuid (seen live)."""
    out = []
    for i, it in enumerate(items):
        it = echo_existing_item(it)
        it["id"]["uuid"] = f"NEW-{i}"
        if it["type"] == "media":
            it["target_uuid"] = f"NEWT-{i}"
        else:
            it.pop("target_uuid", None)
        out.append(it)
    return out


def test_a_numeric_ordering_prefix_is_not_part_of_the_name():
    assert anchor_tokens("01_welcome") == {"welcome"}
    assert anchor_tokens("03_song_1") == {"song", "1"}
    # Only a LEADING number is an ordering prefix.
    assert anchor_tokens("Psalm 23") == {"psalm", "23"}
    assert anchor_tokens("42") == {"42"}


def test_numbered_media_anchors_by_name():
    _, report = build_update_payload(_live_playlist(), _LIVE_RUNSHEET)
    placed = {p["title"]: p["above"] for p in report["placements"]
              if p["placed"]}
    assert placed == {
        "Welcome": "01_welcome", "Worship": "02_worship",
        "Song 1": "03_song_1", "Song 2": "04_song_2",
        "Announcements": "05_announcements",
        "Message - Ps Cathie": "08_message",
        "Prayer Ministry": "09_prayer"}


def test_running_twice_plans_the_same_playlist():
    """Visible result of run two == run one, so the second click is a
    no-op — including after PP has re-minted every id."""
    first, _ = build_update_payload(_live_playlist(), _LIVE_RUNSHEET)
    second, _ = build_update_payload(_as_pp_reads_it_back(first),
                                     _LIVE_RUNSHEET)
    assert visible_signature(second) == visible_signature(first)


def test_a_stack_of_unplaced_headers_is_not_recalled():
    """When nothing anchors, every header stacks above the first slide.
    None of them was placed there, so none may be remembered there."""
    stacked, _ = build_update_payload(
        [_media("IMG_1", "1"), _media("IMG_2", "2")],
        [_item("Welcome"), _item("Offering"), _item("Close")])
    _, recalled = split_existing(_as_pp_reads_it_back(stacked))
    assert recalled == {}


def test_a_dragged_out_unplaced_header_is_recalled():
    """A ↕ header the operator dragged out of the stack stands alone
    above its slide — that IS a placement, and it should stick."""
    playlist = [_header(UNPLACED_MARK + "Offering (4 min)"),
                _media("06_giving", "6")]
    _, recalled = split_existing(playlist)
    assert recalled["offering"][1] == "06_giving"
