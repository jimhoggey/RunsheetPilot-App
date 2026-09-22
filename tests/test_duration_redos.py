"""The duration parser must stay linear on hostile input.

Runsheet notes and titles arrive in the create-playlist request body. The
old single-regex search retried `\\d+` from every digit of a long number,
so a note of a few thousand digits took quadratic time (CodeQL
py/polynomial-redos: 16,000 digits took about 1.6 s, 200,000 would take
minutes). These tests pin the fix, and pin that the answers did not change.
"""
import re
import time

import pytest

from propresenterrunsheet.parsing.duration import (
    _DURATION_RE,
    _extract_duration_min,
    _find_duration_number,
)


# The pattern _extract_duration_min used to search with, kept here as the
# oracle: the new code must give the same answer on every normal input.
_OLD_DURATION_RE = re.compile(r"(\d+)\s*(?:min(?:ute)?s?|m\b)", re.IGNORECASE)

_NORMAL_INPUTS = [
    "20 min", "20min", "20 minutes", "20m", "(20 min)", "20 mins",
    "20minute", "20 Minutes", "20 MINS", "Worship (15 minutes)",
    "9:30am 20 MIN slot", "3 songs 20 min", "12 3 min", "x20m",
    "20 m.", "0 min",
    # Whitespace the regex's \s covers: tab, newline, nbsp, em space.
    "20\tmin", "20\nmin", "20 min", "20 min",
    # \d is any decimal digit, not just ASCII (Arabic-Indic 15 here).
    "١٥ min",
    # ...and ones that must NOT match.
    "for 35:00", "20 mx", "20 mm", "5m30s", "20", "", "min 20",
    "Preach - Ps Cathie Green", "G2:4",
]


@pytest.mark.parametrize("text", _NORMAL_INPUTS)
def test_same_answer_as_the_old_pattern(text):
    old = _OLD_DURATION_RE.search(text)
    expected = old.group(1) if old else ""
    assert _find_duration_number(text) == expected
    # The re-exported pattern gained a lookbehind; it must still agree.
    new = _DURATION_RE.search(text)
    assert (new.group(1) if new else "") == expected


def _seconds(fn, *args):
    t = time.perf_counter()
    fn(*args)
    return time.perf_counter() - t


def test_long_run_of_digits_is_fast():
    # The exact shape CodeQL reported: many repetitions of '9', no unit.
    digits = "9" * 200_000
    assert _seconds(_find_duration_number, digits) < 1.0
    assert _find_duration_number(digits) == ""


def test_long_run_of_digits_is_fast_through_the_reexported_pattern():
    digits = "9" * 200_000
    assert _seconds(_DURATION_RE.search, digits) < 1.0


def test_many_short_numbers_and_long_whitespace_are_fast():
    assert _seconds(_find_duration_number, "1 " * 100_000) < 1.0
    assert _seconds(_find_duration_number, "1" + " " * 200_000 + "x") < 1.0


def test_extract_duration_min_on_a_hostile_note_is_fast_and_finds_nothing():
    item = {"duration_min": 0, "notes": "9" * 1_000_000, "title": ""}
    assert _seconds(_extract_duration_min, item) < 1.0
    assert _extract_duration_min(item) == 0


def test_a_huge_digit_run_before_the_unit_does_not_crash():
    """int() refuses strings over 4,300 digits, and the old code raised
    out of timer creation on one. Leading zeros are just zeros: this note
    says 20 minutes and is read as 20."""
    item = {"notes": "0" * 5_000 + "20 min", "title": "Worship (15 min)"}
    assert _extract_duration_min(item) == 20


def test_an_out_of_range_huge_number_falls_through_to_the_title():
    """Too big to be a duration — skipped like any out-of-range match,
    without ever reaching int()."""
    item = {"notes": "9" * 5_000 + " min", "title": "Worship (15 min)"}
    assert _extract_duration_min(item) == 15


def test_the_scan_limit_never_invents_a_duration():
    """The limit is on where a number STARTS, not a cut of the text. A cut
    left "…20 more people" ending in "…20 m", which read as 20 minutes."""
    assert _extract_duration_min({"notes": "x" * 3996 + "20 more people"}) == 0


def test_a_duration_just_inside_the_limit_is_still_found():
    assert _extract_duration_min({"notes": "x" * 3998 + "120 min"}) == 120
