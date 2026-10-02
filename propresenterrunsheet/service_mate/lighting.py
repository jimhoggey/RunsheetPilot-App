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
    return str(item.get("lighting") or "").strip() if isinstance(item, dict) else ""


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
    is returned unchanged, so the station keeps today's cues.

    The next change's SETTING leads its title ("House lights 30% ·
    Message"): every clock layout shows the next title, but the compact
    ones never show a next cue. Its duration is dropped, since "30 MIN"
    would describe that item, not the one actually up next. With no
    change left, the real next item stays next, so the clock doesn't say
    END OF SERVICE halfway through."""
    items = state.get("items") or []
    try:
        idx = max(0, min(int(state.get("current_index") or 0), len(items) - 1))
    except (TypeError, ValueError):
        return state
    if not has_lighting(items) or not isinstance(items[idx], dict):
        return state
    hu = heads_up(items, idx)

    def with_cue(item, cue):
        return {**item, "cues": {**(item.get("cues") or {}), "lights": [cue]}}

    # Before the first stated setting, the station keeps its usual cues.
    view = [with_cue(items[idx], f"Now: {hu['now']}") if hu["now"] else items[idx]]
    if hu["next_index"] is not None:
        # Only what the clock shows. No notes or duration: the clock would
        # read "30 min" out of either as the length of what's up next.
        view.append({"title": f"{hu['next']} · {hu['next_section']}",
                     "type": items[hu["next_index"]].get("type"),
                     "cues": {"lights": [f"Coming up: {hu['next']}"]},
                     "no_duration": True})
    elif idx + 1 < len(items) and isinstance(items[idx + 1], dict):
        view.append(items[idx + 1])
    return {**state, "items": view, "current_index": 0}
