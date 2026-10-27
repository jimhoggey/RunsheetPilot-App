"""Put the operator's own Media-bin assets into a runsheet, no template.

A template is a playlist someone built once and reuses. A one-off event
has no template — but it does have media, because the operator's smart
folders pull it into ProPresenter's Media bin as it is produced, and
those folders are named for the event ("Summit 2026"). That naming is
the whole trick: the runsheet says which event it is, so the folder says
which media is even a candidate.

This module is the pure half — what counts as a candidate, and what the
model is allowed to have said. `parsing/media_plan.py` makes the call.

Precedence note: media the bin doesn't know about cannot be linked over
PP's API at all (see media_bin.py). Everything here comes OUT of the
bin, so everything here is linkable by construction.
"""
import logging

from ..media_assist import _tokens

log = logging.getLogger("pp_runsheet")

MAX_PER_ITEM = 4        # a section of four is a lot to place under one segment
MAX_CANDIDATES = 150    # what fits in a prompt without crowding out the runsheet


def _folder_score(folder: str, event: str) -> float:
    """How much of the shorter name the two share, 0–1.

    The shorter side, for the reason media_assist.suggest_item found:
    a runsheet header carries words a folder never will ("C3B Summit
    2026 — Night 1" against a folder called "Summit 2026"). Scoring
    against the longer side rejects exactly the match we want.
    """
    ev, fo = _tokens(event), _tokens(folder)
    return len(ev & fo) / min(len(ev), len(fo)) if ev and fo else 0.0


def candidates(bin_items: list, event: str) -> list:
    """The bin assets worth offering for this event, best first.

    A folder named for the event wins outright: with "Summit 2026" on the
    runsheet and a "Summit 2026" folder in Media, nothing outside that
    folder is a candidate, so a Sunday "Welcome" slide can't be placed in
    a conference playlist. With no such folder the whole bin is offered,
    capped — assets whose own name echoes the event first, since a bin
    holds years of media and the prompt holds 150.
    """
    items = [b for b in bin_items or []
             if isinstance(b, dict) and b.get("uuid") and (b.get("name") or "").strip()]
    scored = [(b, _folder_score(b.get("folder") or "", event)) for b in items]
    in_event = [b for b, s in scored if s >= 0.5]
    if in_event:
        return in_event[:MAX_CANDIDATES]
    ranked = sorted(items, key=lambda b: -_folder_score(b.get("name") or "", event))
    return ranked[:MAX_CANDIDATES]


def _entry(asset: dict, index: int) -> dict:
    """One playlist entry in PP's shape. `target_uuid` is the identity PP
    honours for media; `uuid` mirrors it (propresenter/playlist.py)."""
    return {"name": asset["name"], "uuid": asset["uuid"],
            "target_uuid": asset["uuid"], "type": "media", "index": index}


def sections(items: list, picks, offered: list) -> dict:
    """{item index: library_match} from the model's item↔media pairs.

    The model only chooses pairings; names and uuids are read from the
    bin here, so it cannot invent an asset PP would 404 on. A pick is
    kept only when it is a real offered asset, on a real item, used once
    — and not on an item a template already filled.
    """
    by_n = {i: a for i, a in enumerate(offered or [])}
    out, used = {}, set()
    for pick in (picks if isinstance(picks, list) else [])[:MAX_CANDIDATES]:
        if not isinstance(pick, dict):
            continue
        n, i = pick.get("media"), pick.get("item")
        if not (isinstance(n, int) and isinstance(i, int) and not isinstance(n, bool)):
            continue
        if n not in by_n or n in used or not (0 <= i < len(items)):
            continue
        if (items[i] or {}).get("library_match"):
            continue                       # a template already spoke for this one
        placed = out.setdefault(i, [])
        if len(placed) >= MAX_PER_ITEM:
            continue
        used.add(n)
        placed.append(_entry(by_n[n], len(placed)))
    return {i: {"header": {"name": (items[i].get("title") or "").strip(),
                           "uuid": "", "color": {}},
                "items": entries}
            for i, entries in out.items() if entries}
