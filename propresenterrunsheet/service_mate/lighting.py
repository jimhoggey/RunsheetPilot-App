"""The lighting heads-up: where the lights are now, and the next change.

Each item carries `lighting_steps` — the house-lighting changes during it,
in order, as {level, when}. The parse fills them from the runsheet, and
from the church's lighting guide (Service Mate settings) where the
runsheet gives no level; the runsheet wins where they differ. A runsheet
saved before steps existed has a single `lighting` string instead, read
as one step. The per-station `cues.lights` suggestions are not used here.

It says what is COMING UP, never "change now": the moment is the room's
call ("thanks band"), which nothing here can see. Within the live section
the operator ticks a step off on the card (`done`); earlier sections'
steps count as done. Repeats of the current level are skipped.

Shared by the lights clock (lights_view) and the floating card (heads_up),
so the two can never disagree.
"""


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").casefold().split()) == " ".join((b or "").casefold().split())


def steps_of(item) -> list:
    """An item's lighting steps as [{level, when}]."""
    if not isinstance(item, dict):
        return []
    raw = item.get("lighting_steps")
    if isinstance(raw, list):
        return [{"level": str(s.get("level") or "").strip(), "when": str(s.get("when") or "").strip()}
                for s in raw if isinstance(s, dict) and str(s.get("level") or "").strip()]
    legacy = str(item.get("lighting") or "").strip()
    return [{"level": legacy, "when": ""}] if legacy else []


def has_lighting(items) -> bool:
    return any(steps_of(it) for it in items or [])


def _title(item) -> str:
    return str(item.get("title") or "") if isinstance(item, dict) else ""


def heads_up(items, idx, done: int = 0) -> dict:
    """{now, next, then}. `now` is the last level already set: every step of
    earlier items, plus the first `done` steps of item `idx`. `next` and
    `then` are the following two changes, each {level, when, section,
    index, step, here}: `step` is its position within its item, `here`
    means it happens during item `idx`. None when there is none."""
    items = items or []
    now = ""
    for it in items[:idx]:
        for s in steps_of(it):
            now = s["level"]
    current = steps_of(items[idx]) if 0 <= idx < len(items) else []
    done = max(0, min(int(done or 0), len(current)))
    for s in current[:done]:
        now = s["level"]
    pending = [(idx, k, s) for k, s in enumerate(current) if k >= done]
    pending += [(j, k, s) for j in range(idx + 1, len(items))
                for k, s in enumerate(steps_of(items[j]))]
    picked, last = [], now
    for j, k, s in pending:
        if _same(s["level"], last):
            continue
        picked.append({**s, "section": _title(items[j]), "index": j, "step": k, "here": j == idx})
        last = s["level"]
        if len(picked) == 2:
            break
    return {"now": now, "next": picked[0] if picked else None,
            "then": picked[1] if len(picked) > 1 else None}


def done_for(state: dict, idx: int) -> int:
    """Steps of the live item the operator has ticked off — only while that
    item is still live."""
    d = state.get("lighting_done") or {}
    return int(d.get("count") or 0) if isinstance(d, dict) and d.get("index") == idx else 0


def current_index(state: dict, items: list) -> int:
    try:
        return max(0, min(int(state.get("current_index") or 0), len(items) - 1))
    except (TypeError, ValueError):
        return 0


def lights_view(state: dict) -> dict:
    """The runsheet state as the LIGHTS clock should show it: its cue is
    where the lights are now, and its "next" is the next lighting change
    rather than simply the next item. A runsheet that states no lighting
    is returned unchanged, so the station keeps today's cues.

    The next change leads its title ("12% · halfway through the song"):
    every clock layout shows the next title, but the compact ones never
    show a next cue. It carries no notes or length (`no_duration`), which
    would describe its section, not the change. With no change left, the
    real next item stays next, so the clock doesn't say END OF SERVICE
    halfway through. Before the first level, the station keeps its cues."""
    items = state.get("items") or []
    if not has_lighting(items):
        return state
    idx = current_index(state, items)
    if not isinstance(items[idx], dict):
        return state
    hu = heads_up(items, idx, done_for(state, idx))
    view = [{**items[idx], "cues": {**(items[idx].get("cues") or {}), "lights": [f"Now: {hu['now']}"]}}
            if hu["now"] else items[idx]]
    nxt = hu["next"]
    if nxt:
        moment = nxt["when"] if nxt["here"] or not nxt["when"] else f"{nxt['section']}, {nxt['when']}"
        view.append({"title": f"{nxt['level']} · {moment or nxt['section']}",
                     "type": items[nxt["index"]].get("type"),
                     "cues": {"lights": [f"Coming up: {nxt['level']}"]},
                     "no_duration": True})
    elif idx + 1 < len(items) and isinstance(items[idx + 1], dict):
        view.append(items[idx + 1])
    return {**state, "items": view, "current_index": 0}
