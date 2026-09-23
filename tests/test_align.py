"""Tests for the LLM alignment pass — mostly for its distrust of the model.

The alignment call is the only part of placement that can read meaning:
a slide whose graphic says GIVING against a runsheet line called
"Offering" share no word, so no string rule will ever connect them. It
is also the only part that can be confidently, fluently wrong, in front
of a live congregation.

So the tests here are weighted accordingly. A couple cover the prompt
carrying the signals it needs; the rest are about what `parse_sections`
refuses to believe.
"""
from propresenterrunsheet.parsing.align import (
    build_alignment_prompt,
    describe_playlist,
    describe_runsheet,
    parse_sections,
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
    """An item with no OCR text still has neighbours, and they are the
    only thing that says where it belongs."""
    out = describe_playlist(ITEMS, {0: "GIVING"}, is_header)
    assert '0. [media] IMG_4021  reads: "GIVING"' in out
    assert "1. [presentation] Goodness Of God" in out   # a song, no OCR text
    assert "2. [media] Comp 1_1" in out


def test_headers_are_context_and_do_not_shift_the_numbering():
    """Slides are numbered as the payload builder counts them, so a
    header must not take a number — but it is shown, as a hint."""
    out = describe_playlist([{"type": "header", "id": {"name": "Old"}}] + ITEMS,
                            {}, is_header)
    assert "-- header: Old" in out
    assert "0. [media] IMG_4021" in out


def test_facts_are_stated_as_settled_not_re_asked():
    prompt = build_alignment_prompt(RUNSHEET, ITEMS, {0: "GIVING"},
                                    {1: 1}, is_header)
    assert "playlist item 1 belongs to runsheet line 1 (Goodness of God)" \
        in prompt


# ── what it refuses to believe ────────────────────────────────────────────

def _reply(pairs):
    return ('{"items": [' + ",".join(
        f'{{"playlist": {p}, "runsheet": {"null" if n is None else n}}}'
        for p, n in pairs) + "]}")


def test_a_clean_reply_is_accepted():
    assert parse_sections(_reply([(0, 0), (2, 2)]), 3, 3) == {0: 0, 2: 2}


def test_an_out_of_order_answer_is_kept_it_is_how_a_shuffle_is_seen():
    assert parse_sections(_reply([(0, 2), (1, 0), (2, 1)]), 3, 3) == \
        {0: 2, 1: 0, 2: 1}


def test_markdown_fences_do_not_defeat_it():
    fenced = "```json\n" + _reply([(1, 0)]) + "\n```"
    assert parse_sections(fenced, 3, 3) == {1: 0}


def test_null_is_an_accepted_answer_and_simply_files_nothing():
    """Null is what the prompt asks for when the model can't tell, so it
    has to be cheap — the slide stays with the one above it."""
    assert parse_sections(_reply([(0, 0), (1, None)]), 3, 3) == {0: 0}


def test_out_of_range_indexes_are_dropped():
    assert parse_sections(_reply([(99, 0), (1, 1)]), 3, 3) == {1: 1}
    assert parse_sections(_reply([(0, 9), (1, 1)]), 3, 3) == {1: 1}


def test_a_second_answer_for_one_slide_is_ignored():
    assert parse_sections(_reply([(0, 0), (0, 2)]), 3, 3) == {0: 0}


def test_a_reply_about_a_settled_slide_loses():
    """What someone vouched for outranks the model by construction."""
    assert parse_sections(_reply([(1, 2)]), 3, 3, known={1: 1}) == {}


def test_a_collapsed_reply_is_thrown_away_whole():
    """Every slide under one line of a longer runsheet is syntactically
    perfect and worthless — it is what a model produces when it hasn't
    understood but still wants to help."""
    assert parse_sections(_reply([(0, 1), (1, 1), (2, 1)]), 3, 3) == {}


def test_junk_and_prose_return_nothing_rather_than_raising():
    """Falling back to the deterministic rules is the shipped behaviour,
    so every failure here has to land there quietly."""
    for junk in ("I think item 3 is the offering!", "", "{}", "null",
                 '{"items": "nope"}', '{"items": [{"playlist": "a"}]}'):
        assert parse_sections(junk, 3, 3) == {}


def test_booleans_are_not_mistaken_for_indexes():
    """`True == 1` in Python, so a sloppy check would file slide 1
    because the model answered `true`."""
    assert parse_sections('{"items": [{"playlist": true, "runsheet": 1}]}',
                          3, 3) == {}
    assert parse_sections('{"items": [{"playlist": 0, "runsheet": true}]}',
                          3, 3) == {}
