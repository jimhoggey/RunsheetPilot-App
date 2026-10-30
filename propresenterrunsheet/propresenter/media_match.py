"""Put the operator's own Media-bin assets into a runsheet, scoped to the
service the runsheet is for.

A template is a playlist someone built once and reuses; the bin is
everything else the operator has. The bin's job is to fill what the
template doesn't cover — a Sunday runsheet that says "promo Summit"
wants a slide the Sunday template has no reason to carry — and to be the
whole answer for an event with no template at all.

THE FLOW. Code narrows; the model decides (owner, 30 Oct 2026: "Runsheet
Pilot should narrow down the decisions and let the LLM make the final
decision"). Word matching is crude, so it only ever removes what is
clearly wrong or cuts a list to size — every real choice is the model's.

    Media bin — thousands of files, dozens of folders
      │
      ├─ 1. NARROW (code)    drop folders named for ANOTHER service that
      │                      the runsheet never mentions .. eligible_folders
      ├─ 2. DECIDE (model)   which folders hold THIS service's media
      │                      ................ parsing/folder_plan.py
      │                      — skipped when the operator has saved folders
      │                        for this service; theirs are final
      ├─ 3. NARROW (code)    those folders' files, at most 150, a share
      │                      per runsheet line .................. scope
      ├─ 4. DECIDE (model)   which file goes under which item
      │                      ................ parsing/media_plan.py
      └─ 5. CHECK  (code)    real files, real items, never over a
                             template item, four a segment ... sections

If step 2 has no answer (the call failed), folders whose name names this
service stand in for it — still only narrowing, since step 4 decides.

Step 1 is the fix for the first version. Scored on shared words alone, a
folder called "Youth" matched any runsheet that mentioned youth and
emptied itself into the candidate list — on a Summit night the owner
wanted no youth screens at. A folder for another service never reaches
the model now unless a line asks for it ("promo Summit"); and then the
model judges whether the line wants that media or merely names it.

"This service" means its KIND, the parse model's `service_type` — not
the dated title. "Summit 2026 — Sunday Night" is a Summit service that
happens to fall on a Sunday; matched on its title, a "Sunday" row and the
Sunday folders would have claimed it.

There is deliberately NO "search the whole bin" fallback. Offering a
model three thousand filenames costs roughly twenty times as much and
answers worse — it is a longer list to find the wrong slide in. Nothing
matching is a thing to SAY (the banner says it), not to paper over.

This module is the pure half — what counts as a candidate, and what the
model is allowed to have said. The parsing/*_plan.py modules make calls.

Precedence note: media the bin doesn't know about cannot be linked over
PP's API at all (see media_bin.py). Everything here comes OUT of the
bin, so everything here is linkable by construction.
"""
import logging

from .templates import SERVICE_FILLERS, service_tokens

log = logging.getLogger("pp_runsheet")

MAX_PER_ITEM = 4        # a section of four is a lot to place under one segment
MAX_CANDIDATES = 150    # what fits in a prompt without crowding out the runsheet

# Words a FOLDER name adds no service meaning with: what it holds
# ("media", "loops") and where in a service it is used ("pre-service",
# "after service") — "Pre-service loops" names no service, where it would
# otherwise read as one called "pre". Folded into the template filler set
# rather than replacing it, so the two can't drift: "which service is
# this" has one answer across the app, and the first version's separate
# tokeniser is what let "Youth" match anything.
FOLDER_FILLERS = SERVICE_FILLERS | {
    "media", "folder", "screens", "slides", "assets", "misc", "stuff",
    "loop", "video", "image", "graphic", "current", "new", "old",
    "pre", "post", "after", "end",
}


def tokens(name: str) -> set:
    """The service a name is for, as tokens — for a folder, a row or a
    service alike. May be empty: "Current media" or "Loops" names no
    service, so such a folder is only reached by being on a saved row."""
    return service_tokens(name, FOLDER_FILLERS)


def _item_text(item) -> str:
    """What a runsheet line says — title plus the tech note, which is
    where "promo Summit" lives far more often than in the title."""
    if not isinstance(item, dict):
        return ""
    return f"{item.get('title') or ''} {item.get('notes') or ''}"


def _assets(bin_items: list) -> list:
    return [b for b in bin_items or []
            if isinstance(b, dict) and b.get("uuid")
            and (b.get("name") or "").strip()]


def bin_folders(bin_items: list) -> list:
    """[{"name", "count"}] for every folder in the bin, biggest first —
    what the folder picker lists."""
    counts: dict = {}
    for b in _assets(bin_items):
        folder = (b.get("folder") or "").strip()
        if folder:
            counts[folder] = counts.get(folder, 0) + 1
    return [{"name": f, "count": n} for f, n in
            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def row_for_service(rows: list, service: str, title: str = "") -> dict:
    """The operator's saved row for this service, or {}.

    A row applies when every word of its name is in the service or its
    title, AND it shares at least one word with the service itself. So
    "Summit 2026" applies to a Summit service titled "Summit 2026 —
    Saturday" (the year comes from the title), while a "Sunday" row can
    never claim "Summit 2026 — Sunday Night": Sunday is when it is, not
    what it is. The most specific row wins.
    """
    kind = tokens(service)
    named = kind | tokens(title)
    best, hit = 0, {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        want = tokens(str(row.get("match") or ""))
        if want and want <= named and (want & kind) and len(want) > best:
            best, hit = len(want), row
    return hit


def other_service_tokens(service: str, rows: list, template_names=()) -> set:
    """Words that name a service OTHER than this one.

    Drawn from what the machine already knows its services are: the rows
    the operator has saved, and the names of their template playlists. A
    folder carrying one of these is somebody else's.
    """
    names = [str((r or {}).get("match") or "") for r in rows or []
             if isinstance(r, dict)]
    out = set()
    for name in names + [str(n or "") for n in template_names or ()]:
        out |= tokens(name)
    return out - tokens(service)


def recommended_folders(bin_items: list, service: str, rows=None,
                        template_names=()) -> list:
    """The bin folders whose name names THIS service, strongest first —
    the no-model fallback, deliberately conservative: it never reaches
    across to another service, because telling "promo Summit" from
    "youth band" needs the model.

    Ranked by how much of the service they name, then by how specific
    the folder name is, so "Summit 2026 Saturday" leads a bare "Summit"
    — the longer name that still matched said more.
    """
    mine = tokens(service)
    if not mine:
        return []
    others = other_service_tokens(service, rows, template_names)
    ranked = {}
    for entry in bin_folders(bin_items):
        got = tokens(entry["name"])
        if got and (got & mine) and not (got & others):
            ranked[entry["name"]] = (len(got & mine), len(got))
    return sorted(ranked, key=lambda f: ranked[f], reverse=True)


def eligible_folders(bin_items: list, service: str, items=None, rows=None,
                     template_names=()) -> list:
    """[{"name", "count"}] the folder-choosing model may pick from.

    Every bin folder, minus those named for ANOTHER service that no line
    of this runsheet mentions. So a Summit runsheet never sees "Sunday
    Service Media" — while a Sunday runsheet whose line says "promo
    Summit" does see the Summit folders, because it asked for them. A
    mention only makes a folder eligible; whether the line WANTS that
    service's media ("promo Summit") or merely names it ("youth band")
    is the model's judgement.

    This service's own folders lead, so a long bin cut short by the
    prompt cap loses the irrelevant end of the list, not the answer.
    """
    mentioned = set()
    for it in items or []:
        mentioned |= tokens(_item_text(it))
    others = other_service_tokens(service, rows, template_names) - mentioned
    mine = set(recommended_folders(bin_items, service, rows, template_names))
    allowed = [f for f in bin_folders(bin_items) if not (tokens(f["name"]) & others)]
    return ([f for f in allowed if f["name"] in mine]
            + [f for f in allowed if f["name"] not in mine])


def _budgeted(assets: list, items: list) -> list:
    """Spend the row budget ACROSS the runsheet, not on one global rank.

    Ranking every asset against the service name and taking the top 150
    gave a twelve-item runsheet 150 assets that all looked like the
    service and none chosen for any particular line. Each item now gets
    its own share — the assets whose names that line accounts for most —
    and whatever budget is left over is filled in folder order.

    Every name and line is tokenised once up front: inside the sort key,
    sixty lines against a 3,000-file bin was 360,000 regex passes a parse.
    """
    if len(assets) <= MAX_CANDIDATES:
        return assets
    lines = [tokens(t) for t in (_item_text(it) for it in items or []) if t.strip()]
    names = [tokens(a.get("name") or "") for a in assets]
    per = max(1, MAX_CANDIDATES // len(lines)) if lines else 0
    out, seen = [], set()
    for line in lines:
        echo = [len(n & line) / len(n) if n else 0.0 for n in names]
        for k in sorted(range(len(assets)), key=lambda k: -echo[k])[:per]:
            a = assets[k]
            if a["uuid"] not in seen:
                seen.add(a["uuid"])
                out.append(a)
    for a in assets:
        if len(out) >= MAX_CANDIDATES:
            break
        if a["uuid"] not in seen:
            seen.add(a["uuid"])
            out.append(a)
    return out[:MAX_CANDIDATES]


def scope(bin_items: list, service: str, items=None, rows=None,
          template_names=(), suggested=None, title: str = "") -> dict:
    """What to offer the asset-choosing model, and where it came from.

    Returns {"assets", "folders", "source", "service"}. `source` says who
    chose the folders, best authority first:
      "chosen"    — the operator's saved row for this service; final.
      "suggested" — the folder-choosing model (`suggested`), held to
                    eligible_folders so it can't reach another service's.
      "matched"   — no model answer, so folders whose name names this
                    service.
      "none"      — nothing in the bin is for this service.
    The caller shows `folders` whatever the source: a silent scope is
    what made the first version feel arbitrary.
    """
    row = row_for_service(rows, service, title)
    folders = [str(f).strip() for f in (row.get("folders") or [])
               if isinstance(f, str) and f.strip()] if row else []
    source = "chosen" if folders else ""
    if not folders and suggested:
        allowed = {f["name"] for f in eligible_folders(
            bin_items, service, items, rows, template_names)}
        folders = [f for f in dict.fromkeys(suggested) if f in allowed]
        source = "suggested" if folders else ""
    if not folders:
        folders = recommended_folders(bin_items, service, rows, template_names)
        source = "matched" if folders else "none"
    wanted = {f: [] for f in folders}
    for b in _assets(bin_items):
        bucket = wanted.get((b.get("folder") or "").strip())
        if bucket is not None:
            bucket.append(b)
    offered = [b for f in folders for b in wanted[f]]
    return {"assets": _budgeted(offered, items), "folders": folders,
            "source": source,
            "service": str(row.get("match") or "") if row else ""}


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

    Each section is marked `from_bin`, so running these steps again after
    the operator changes a service's folders replaces the bin's own
    earlier picks and never a template's (routes/parse.place_bin_media).
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
                "items": entries, "from_bin": True}
            for i, entries in out.items() if entries}
