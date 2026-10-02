"""The lighting heads-up: where the lights are now, and the next change.

Only lighting the RUNSHEET states counts — each item's `lighting` field,
which the parse fills from the runsheet's own words ("House lights 50%")
and leaves empty otherwise. The per-station `cues.lights` suggestions are
the model's ideas and are not used here.

It says what is COMING UP, never "change now": the moment depends on the
room ("thanks band"), which nothing here can see. So the next change is the
first later item whose setting differs from the current one; items that
state nothing, or repeat the current setting, are skipped.

Shared by the lights clock (lights_view) and the floating window
(heads_up), so the two can never disagree.
"""


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").casefold().split()) == " ".join((b or "").casefold().split())


def _stated(item) -> str:
    return str((item or {}).get("lighting") or "").strip()


def has_lighting(items) -> bool:
    return any(_stated(it) for it in items or [])


def heads_up(items, idx) -> dict:
    """{now, now_section, next, next_section, next_index}; "" / None where
    the runsheet doesn't say. `now` is the last setting stated at or before
    item `idx`."""
    items = items or []
    out = {"now": "", "now_section": "", "next": "", "next_section": "", "next_index": None}
    for it in items[:idx + 1]:
        if _stated(it):
            out["now"], out["now_section"] = _stated(it), str(it.get("title") or "")
    for i in range(idx + 1, len(items)):
        s = _stated(items[i])
        if s and not _same(s, out["now"]):
            out.update(next=s, next_section=str(items[i].get("title") or ""), next_index=i)
            break
    return out


def lights_view(state: dict) -> dict:
    """The runsheet state as the LIGHTS clock should show it: its cue is
    where the lights are now, and its "next" is the next lighting change
    rather than simply the next item. A runsheet that states no lighting
    is returned unchanged, so the station keeps today's cues."""
    items = state.get("items") or []
    if not has_lighting(items):
        return state
    idx = max(0, min(int(state.get("current_index") or 0), len(items) - 1))
    hu = heads_up(items, idx)

    def with_cue(item, cue):
        return {**item, "cues": {**(item.get("cues") or {}), "lights": [cue] if cue else []}}

    view = [with_cue(items[idx], f"Now: {hu['now']}" if hu["now"] else "")]
    if hu["next_index"] is not None:
        view.append(with_cue(items[hu["next_index"]], f"Coming up: {hu['next']}"))
    return {**state, "items": view, "current_index": 0}
