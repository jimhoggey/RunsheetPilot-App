"""Tests for the LLM alignment pass — mostly for its distrust of the model.

The alignment call is the only part of placement that can read meaning:
a slide whose graphic says GIVING against a runsheet line called
"Offering" share no word, so no string rule will ever connect them. It
is also the only part that can be confidently, fluently wrong, in front
of a live congregation.

So the tests here are weighted accordingly. A couple cover the prompt
carrying the signals it needs; the rest are about what `parse_alignment`
refuses to believe.
"""
from propresenterrunsheet.parsing.align import (
    build_alignment_prompt,
    describe_playlist,
    describe_runsheet,
    parse_alignment,
)
from propresenterrunsheet.propresenter.playlist_update import is_header


def _item(title, **kw):
    return {"parsed": {"type": kw.pop("type_", "other"), "title": title, **kw}}


ITEMS = [
    {"type": "media", "id": {"name": "IMG_4021"}},
    {"type": "presentation", "id": {"name": "Goodness Of God"}},
    {"type": "media", "id": {"name": "Comp 1_1"}},
]
RUNSHEET = [_item("Offering", start_time="10:20 AM", duration_min=4),
            _item("Goodness of God", type_="song"),
            _item("Preach", type_="sermon", duration_min=30)]


# ── the prompt carries what it needs ──────────────────────────────────────

def test_runsheet_description_carries_time_and_duration():
    """These discriminate exactly where OCR is blank — a 30-minute line
    is the sermon whatever the slide behind it looks like."""
    out = describe_runsheet(RUNSHEET)
    assert "0. Offering [other] at 10:20 AM 4 min" in out
    assert "30 min" in out


def test_every_playlist_item_is_listed_even_with_no_slide_text():
    """Omitting the un-OCR'd items would leave holes in the sequence,
    and the sequence is the strongest signal available."""
    out = describe_playlist(ITEMS, {0: "GIVING"}, is_header)
    assert '0. [media] IMG_4021  reads: "GIVING"' in out
    assert "1. [presentation] Goodness Of God" in out   # a song, no OCR text
    assert "2. [media] Comp 1_1" in out


def test_headers_are_not_offered_as_placement_targets():
    out = describe_playlist([{"type": "header", "id": {"name": "Old"}}] + ITEMS,
                            {}, is_header)
    assert "Old" not in out


def test_settled_anchors_are_stated_as_facts_not_re_asked():
    prompt = build_alignment_prompt(RUNSHEET, ITEMS, {0: "GIVING"},
                                    {1: 1}, is_header)
    assert "ALREADY PLACED" in prompt
    assert "runsheet 1 (Goodness of God) is already placed at playlist item 1" \
        in prompt


# ── what it refuses to believe ────────────────────────────────────────────

def _reply(pairs):
    return ('{"placements": [' + ",".join(
        f'{{"runsheet": {n}, "playlist": {"null" if p is None else p}}}'
        for n, p in pairs) + "]}")


def test_a_clean_reply_is_accepted():
    assert parse_alignment(_reply([(0, 0), (2, 2)]), 3, 2) == {0: 0, 2: 2}


def test_markdown_fences_do_not_defeat_it():
    fenced = "```json\n" + _reply([(0, 1)]) + "\n```"
    assert parse_alignment(fenced, 3, 2) == {0: 1}


def test_null_is_an_accepted_answer_and_simply_places_nothing():
    """Null is what the prompt asks for when the model can't tell, so it
    has to be cheap — the item falls back to runsheet-order placement."""
    assert parse_alignment(_reply([(0, 0), (1, None)]), 3, 2) == {0: 0}


def test_out_of_range_indexes_are_dropped():
    assert parse_alignment(_reply([(0, 99), (1, 1)]), 3, 2) == {1: 1}
    assert parse_alignment(_reply([(9, 0), (1, 1)]), 3, 2) == {1: 1}


def test_backwards_order_is_dropped_not_reordered():
    """Both lists run in service order, so an answer that goes backwards
    is incoherent — and silently honouring it would scramble the
    operator's service."""
    assert parse_alignment(_reply([(0, 2), (1, 1)]), 3, 2) == {0: 2}


def test_a_reply_that_contradicts_a_settled_anchor_loses():
    """A deterministic match outranks the model by construction."""
    assert parse_alignment(_reply([(1, 2)]), 3, 2, known={1: 1}) == {}


def test_the_model_cannot_place_an_item_across_a_settled_anchor():
    out = parse_alignment(_reply([(2, 0)]), 3, 2, known={1: 1})
    assert 2 not in out, "runsheet 2 cannot sit above an anchor at 1"


def test_a_collapsed_reply_is_thrown_away_whole():
    """Everything-at-item-1 is syntactically perfect and worthless — it
    is what a model produces when it hasn't understood but still wants
    to help. Believing half of it would be worse than believing none."""
    assert parse_alignment(_reply([(0, 1), (1, 1), (2, 1)]), 3, 2) == {}


def test_junk_and_prose_return_nothing_rather_than_raising():
    """Falling back to the deterministic rules is the shipped behaviour,
    so every failure here has to land there quietly."""
    for junk in ("I think item 3 is the offering!", "", "{}", "null",
                 '{"placements": "nope"}', '{"placements": [{"runsheet": "a"}]}'):
        assert parse_alignment(junk, 3, 2) == {}


def test_booleans_are_not_mistaken_for_indexes():
    """`True == 1` in Python, so a sloppy check would place a header at
    item 1 because the model answered `true`."""
    assert parse_alignment('{"placements": [{"runsheet": true, "playlist": 1}]}',
                           3, 2) == {}
    assert parse_alignment('{"placements": [{"runsheet": 0, "playlist": true}]}',
                           3, 2) == {}
