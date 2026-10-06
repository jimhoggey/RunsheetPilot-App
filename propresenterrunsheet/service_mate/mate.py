"""The digital Service Mate: what the window on the ProPresenter computer shows.

Pure functions over data handed in; routes/clocks.py reads the files. The
window shows what each ticked station's clock is sent — the very same
`build_state_payload`, from the same state — plus the lighting card and the
optional sections the operator ticks to find out what earns its place
(docs/superpowers/specs/2026-10-06-digital-service-mate-design.md).
"""
import datetime as _dt

from ..parsing.duration import _extract_duration_min
from ..parsing.timed_rows import _minutes, _norm_time
from .lighting import current_index, done_for, has_lighting, heads_up
from .protocol import build_state_payload
from .state import _cues_for, _next_visible_item

# Sound runs at the sound desk, not the ProPresenter computer: not offered
# here yet, though build_state_payload already serves it.
STATIONS = ("screen", "lights")
SHOW = ("timing", "notes", "next_cues", "later")
_MAX_DRIFT_MIN = 60     # further out than this, the segment wasn't started live
_LATER = 3              # segments listed after the next one


def _defaults() -> dict:
    return {"on": False, "stations": ["screen"], "show": ["timing"]}


def _pick(value: object, allowed: tuple[str, ...], default: list[str]) -> list[str]:
    """`value`'s known names in canonical order; `default` unless it's a list."""
    if not isinstance(value, list):
        return list(default)
    return [name for name in allowed if name in value]


def mate_config(cfg: dict) -> dict:
    """{on, stations, show} from clocks.json, cleaned: it's a hand-editable
    file. A lighting window switched on before this existed reads as Lights
    ticked, so nobody has to set it up again."""
    default = _defaults()
    mate = cfg.get("mate")
    if not isinstance(mate, dict):
        if cfg.get("lighting_window"):
            return {**default, "on": True, "stations": ["lights"]}
        return default
    return {"on": bool(mate.get("on")),
            "stations": _pick(mate.get("stations"), STATIONS, default["stations"])
                        or default["stations"],
            "show": _pick(mate.get("show"), SHOW, default["show"])}


def _drift_min(start: str, planned: int, started_at: str | None,
               now: _dt.datetime) -> int | None:
    """Whole minutes the segment started after its planned time (negative:
    ahead), or None when that can't be known or isn't a live start."""
    try:
        began = _dt.datetime.fromisoformat(str(started_at or "")).replace(tzinfo=None)
    except ValueError:
        return None
    readings = [planned]
    if planned < 12 * 60 and _norm_time(start)[-2:] not in ("am", "pm"):
        readings.append(planned + 12 * 60)      # "6:10" may mean either
    midnight = _dt.datetime.combine(now.date(), _dt.time())
    late_s = min(((began - midnight).total_seconds() - m * 60 for m in readings), key=abs)
    drift = int(late_s / 60)                    # toward zero: within a minute is on time
    return drift if abs(drift) <= _MAX_DRIFT_MIN else None


def segment_timing(item: dict, started_at: str | None, now: _dt.datetime) -> dict | None:
    """{start, length_min, drift_min} for the Timing section, or None when the
    runsheet gives neither a start time nor a length."""
    start = str(item.get("start_time") or "").strip()
    planned = _minutes(start) if start else None
    if planned is None:
        start = ""
    length = _extract_duration_min(item)
    if not start and not length:
        return None
    return {"start": start, "length_min": length,
            "drift_min": _drift_min(start, planned, started_at, now) if start else None}


def mate_view(state: dict, stations: list[str], show: list[str],
              ends_at: _dt.datetime | None, now: _dt.datetime) -> dict:
    """Everything the window draws. Each station's entry is its clock's own
    payload, built from the plain state (the window draws its own lighting
    card, so Up next stays the next item). Extras appear only when ticked
    and only when they have something to say."""
    if state.get("standby"):
        return {"state": "standby"}
    items = state.get("items") or []
    if not items:
        return {"state": "empty"}
    idx = current_index(state, items)
    cur = items[idx] if isinstance(items[idx], dict) else {}
    nxt = _next_visible_item(items, idx)
    nxt = nxt if isinstance(nxt, dict) else None

    lighting = None
    if "lights" in stations and has_lighting(items):
        done = done_for(state, idx)
        hu = heads_up(items, idx, done)
        lighting = {"next": hu["next"], "then": hu["then"], "done": done}

    extras: dict = {}
    if "timing" in show and (timing := segment_timing(cur, state.get("current_started_at"), now)):
        extras["timing"] = timing
    if "notes" in show and (notes := str(cur.get("notes") or "").strip()):
        extras["notes"] = notes
    if "next_cues" in show and nxt:
        extras["next_cues"] = {role: _cues_for(role, nxt) for role in stations}
    if "later" in show and (later := [{"title": str(it.get("title") or ""),
                                       "start_time": str(it.get("start_time") or "")}
                                      for it in items[idx + 2:idx + 2 + _LATER]
                                      if isinstance(it, dict)]):
        extras["later"] = later

    return {"state": "live",
            "stations": {role: build_state_payload(role, "detailed", state, ends_at, now)
                         for role in stations},
            "lighting": lighting, "extras": extras}
