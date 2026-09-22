"""Time-of-day + duration parsing for runsheet items.

Used both by the timer-creation step (which stamps PP timers with a
"9:30 AM — 20 min" hint in the name) and by the Service Mate countdown
(which falls back to `duration_min` when no PP timer is currently
running)."""

import re


# Matches "9:24 AM", "9:24am", "12:30 PM", etc. — the AM/PM marker is required
# so we don't accidentally match e.g. a chord "G2:4" or a note like "for 35:00".
# We still extract time-of-day for display purposes (it goes in the timer name
# so the operator can find the right timer at the right moment), but timers
# themselves are duration-based since the runsheet is uploaded days ahead.
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])")

# Durations like "20 min", "20min", "20 minutes", "20m", "(20 min)", found
# as a number and a unit matched separately. The single pattern
# `(\d+)\s*(?:min(?:ute)?s?|m\b)` retried `\d+` from every digit of a long
# number, so a note of a few thousand digits took quadratic time — and
# notes arrive in the request body (CodeQL py/polynomial-redos). A bare
# `\d+` always succeeds on a whole run, so finditer passes over the text
# once; the unit is then checked at one fixed spot after each number.
_NUMBER_RE = re.compile(r"\d+")
_UNIT_RE = re.compile(r"min(?:ute)?s?|m\b", re.IGNORECASE)

# Real notes and titles are a line or two, so a duration STARTING past
# this point is not one we'd ever see. The scan stops at the first number
# that begins beyond it, which bounds the work on a hostile body. It is a
# position limit, not a slice: cutting the text here instead made "…20
# more people" end in "…20 m" and read as 20 minutes.
_DURATION_SCAN_MAX = 4000

# More significant digits than this is past the 24-hour cap anyway, and a
# digit run over 4,300 long would make int() raise.
_DURATION_MAX_DIGITS = 4


def _extract_time_str(text: str) -> str:
    """Return the time-of-day as a display string (e.g. '9:30 AM') or ''."""
    if not text:
        return ""
    m = _TIME_RE.search(text)
    if not m:
        return ""
    h, mn, p = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if not (1 <= h <= 12 and 0 <= mn <= 59):
        return ""
    return f"{h}:{mn:02d} {p}"


def _find_duration_number(text: str) -> str:
    """The number in the first "<number> <unit>" in `text`, or "".

    The same answer the single pattern above gave, in linear time. The whitespace between number and unit is skipped in code:
    str.isspace() is the same character set as the regex's \\s. The unit is
    matched in place rather than on a slice, so `m\\b` still sees the
    character that really follows it."""
    for num in _NUMBER_RE.finditer(text):
        if num.start() >= _DURATION_SCAN_MAX:
            break
        pos = num.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if _UNIT_RE.match(text, pos):
            return num.group()
    return ""


def _extract_duration_min(parsed_item: dict) -> int:
    """Find the duration in whole minutes for a parsed runsheet item.

    Order of precedence:
      1. Explicit `duration_min` field returned by the AI (preferred).
      2. Regex match on the `notes` field ("20 min", "30 minutes", etc.).
      3. Regex match on the `title` field as a last resort.
    Returns 0 if no duration found / 0-duration item — caller should skip."""
    raw = parsed_item.get("duration_min")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    if isinstance(raw, str) and raw.strip().isdigit() and int(raw) > 0:
        return int(raw)
    for field in ("notes", "title"):
        number = _find_duration_number(parsed_item.get(field, "") or "")
        if number:
            digits = number.lstrip("0") or "0"
            # Out of range is treated like the old out-of-range match:
            # skip to the next field. Checked before int() so a huge digit
            # run can't raise.
            if len(digits) <= _DURATION_MAX_DIGITS:
                n = int(digits)
                if 0 < n < 24 * 60:  # sanity cap: under 24 h
                    return n
    return 0
