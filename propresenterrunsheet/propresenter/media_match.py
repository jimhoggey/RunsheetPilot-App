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
import re

log = logging.getLogger("pp_runsheet")

MAX_PER_ITEM = 4        # a section of four is a lot to place under one segment
MAX_CANDIDATES = 150    # what fits in a prompt without crowding out the runsheet
MIN_FOLDER_SCORE = 0.5  # how much of a folder's name the runsheet must echo

# media_assist._tokens exists to make a FILENAME comparable, so it throws
# away "service", "sunday", "church", "youth" and every bare number —
# noise when every file on the machine carries them. A folder is named
# for precisely those words ("Sunday Service Media", "Summit 2026"), and
# borrowing that tokeniser here reduced "Sunday Service Media" to an
# empty set, which can never match anything. Hence a tokeniser of its
# own: drop only what a folder name adds no meaning with, and keep the
# year, which is the whole difference between Summit 2026 and 2025.
_FOLDER_STOP = {"the", "and", "for", "with", "media", "folder", "folders",
                "screens", "slides", "assets", "misc", "stuff"}


def _words(text: str) -> set:
    return {w for w in re.split(r"[^\w]+", (text or "").casefold())
            if len(w) > 2 and w not in _FOLDER_STOP}


def _folder_score(folder: str, text: str) -> float:
    """How much of the shorter name the two share, 0–1.

    The shorter side, for the reason media_assist.suggest_item found:
    a runsheet line carries words a folder never will ("C3B Summit 2026
    — Night 1" against a folder called "Summit 2026"). Scoring against
    the longer side rejects exactly the match we want.
    """
    want, have = _words(text), _words(folder)
    return len(want & have) / min(len(want), len(have)) if want and have else 0.0


def _item_text(item) -> str:
    """What a runsheet line says, for matching a folder against it."""
    if not isinstance(item, dict):
        return ""
    return f"{item.get('title') or ''} {item.get('notes') or ''}"


def relevant_folders(bin_items: list, event: str, items=None) -> list:
    """The Media-bin folders this runsheet names, strongest first.

    Not just the folder for the service. A Sunday runsheet draws on the
    Sunday folder AND the pre-service loop folder AND, when a line says
    "promo Summit", the Summit folder — which the Sunday template has no
    reason to carry, because the template is what happens every week and
    that announcement is not (owner, 28 Oct 2026).

    So every line of the runsheet gets a say, not only the service name:
    a folder is in play when the service, or any single item, echoes at
    least half of its name.

    Order matters as much as membership, because the 150-row cap has to
    take its rows from somewhere. The service's own folder leads — it is
    the base for the week, where a folder named by one line is an extra
    on top — then the strongest score, then the longest folder name,
    since a folder with more words that still matched said more. Without
    that last pair a one-word folder like "Promo" scores a perfect 1.0
    against any line mentioning a promo, and would take the cap's rows
    off the Sunday folder purely by sitting earlier in the bin.
    """
    ranked: dict = {}
    lines = [_item_text(it) for it in (items or [])]
    for b in bin_items or []:
        folder = (b.get("folder") or "").strip() if isinstance(b, dict) else ""
        if not folder or folder in ranked:
            continue
        from_service = _folder_score(folder, event)
        from_line = max((_folder_score(folder, t) for t in lines), default=0.0)
        if max(from_service, from_line) >= MIN_FOLDER_SCORE:
            ranked[folder] = (from_service >= MIN_FOLDER_SCORE,
                              max(from_service, from_line),
                              len(_words(folder)))
    return sorted(ranked, key=lambda f: ranked[f], reverse=True)


def candidates(bin_items: list, event: str, items=None,
               folder_only: bool = False) -> list:
    """The bin assets worth offering for this runsheet, best folder first.

    Every asset in every folder the runsheet names (`relevant_folders`),
    strongest folder first so that when the 150-row cap bites it is the
    weakest association that loses rows, not the Summit folder losing to
    a folder that happened to share one word.

    With no folder named at all the whole bin is offered, ranked by how
    much each asset's own name echoes the service — a last resort for an
    operator who hasn't filed anything by event.

    `folder_only` drops that last resort and returns nothing instead. It
    is what a templated service uses (owner, 28 Oct 2026): there the bin
    fills gaps around a template that already covers the week, so it may
    reach media deliberately filed for this service and no further.
    Trawling a decade of bin media to decorate a Sunday is how wrong
    slides reach the screen, and fewer accurate screens beats more.
    """
    assets = [b for b in bin_items or []
              if isinstance(b, dict) and b.get("uuid") and (b.get("name") or "").strip()]
    wanted = relevant_folders(assets, event, items)
    if wanted:
        by_folder = {f: [] for f in wanted}
        for b in assets:
            bucket = by_folder.get((b.get("folder") or "").strip())
            if bucket is not None:
                bucket.append(b)
        out = [b for f in wanted for b in by_folder[f]]
        return out[:MAX_CANDIDATES]
    if folder_only:
        return []
    ranked = sorted(assets, key=lambda b: -_folder_score(b.get("name") or "", event))
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
    kept only when it is a real offered asset, on a real item — and not
    on an item a template already filled.

    An asset may be placed under SEVERAL segments (owner, 28 Oct 2026).
    A hold screen, a lower third, a giving QR each belong in more than
    one place in a service, and a once-only rule forbids exactly that.
    The cost is accepted knowingly: a film the model half-recognises in
    three segments now lands in all three.

    What stays is the guard within one segment — the same slide twice
    under one header is never what anyone meant — and the cap of four.
    """
    by_n = {i: a for i, a in enumerate(offered or [])}
    out = {}
    for pick in (picks if isinstance(picks, list) else [])[:MAX_CANDIDATES]:
        if not isinstance(pick, dict):
            continue
        n, i = pick.get("media"), pick.get("item")
        if not (isinstance(n, int) and isinstance(i, int) and not isinstance(n, bool)):
            continue
        if n not in by_n or not (0 <= i < len(items)):
            continue
        if (items[i] or {}).get("library_match"):
            continue                       # a template already spoke for this one
        placed = out.setdefault(i, [])
        if len(placed) >= MAX_PER_ITEM:
            continue
        if any(e["uuid"] == by_n[n]["uuid"] for e in placed):
            continue
        placed.append(_entry(by_n[n], len(placed)))
    return {i: {"header": {"name": (items[i].get("title") or "").strip(),
                           "uuid": "", "color": {}},
                "items": entries}
            for i, entries in out.items() if entries}
