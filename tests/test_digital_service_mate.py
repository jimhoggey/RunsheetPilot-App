"""The digital Service Mate: the window on the ProPresenter computer.

Owner's spec (6 Oct 2026, docs/superpowers/specs/2026-10-06-digital-service-
mate-design.md): one Service Mate, on a clock or on this computer. The window
shows what the ticked stations' clocks are sent — generic cues included —
plus optional sections the operator ticks to see what earns its place.
"""
import datetime as dt

from propresenterrunsheet.service_mate.protocol import EndsAtHolder

NOW = dt.datetime(2026, 10, 11, 18, 13, 0)


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
