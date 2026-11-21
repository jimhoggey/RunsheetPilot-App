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
      └─ 5. CHECK  (code)    real files, real items, every folder's
                             job done ............................ place

Every folder has one JOB (owner, 20 Nov 2026), saved with the folders or
given by the model in step 2, else guessed from its name (folder_job):

    every   announcements: all of them go in; the model picks each one's
            line, and code puts any it didn't place under the
            announcements line
    preach  all under the preach, title screen first; no model
    start   opens the playlist, at the top of the first line; no model
    end     closes it, at the end of the last line; no model
    pick    the model places what fits and leaves the rest out

Only `every` and `pick` files reach the model in step 4.

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
import re

from .templates import SERVICE_FILLERS, service_tokens

log = logging.getLogger("pp_runsheet")

MAX_PER_ITEM = 4        # "only what fits": a lot to place under one segment
MAX_CANDIDATES = 150    # what fits in a prompt without crowding out the runsheet

JOBS = ("every", "preach", "start", "end", "pick")
# Where a line's media sits by job: the opening loop, the template's own
# screens, files placed by the model, the preach, the closing loop.
_ORDER = {"start": 0, "": 1, "every": 2, "pick": 2, "preach": 3, "end": 4}
_HELD = {"service", "loop", "loops"}


def folder_job(name: str) -> str:
    """The job a folder's NAME suggests: the picker's starting guess, and
    the answer when neither the operator nor the model gave one.
    "Pre-service loop" opens, "End service loop" closes, "Preach" goes
    under the preach, "Announcements" all go in; anything else is "pick".
    Whole words only, so "Weekend loop" is not an end loop."""
    w = set(re.findall(r"[a-z]+", str(name or "").lower()))
    if (w & {"preservice", "preloop", "preloops", "walkin"}
            or ("pre" in w and w & _HELD) or {"walk", "in"} <= w):
        return "start"
    if (w & {"postservice", "postloop", "endloop", "afterservice", "outro"}
            or (w & {"end", "post", "after", "exit", "closing"} and w & _HELD)):
        return "end"
    if any(x.startswith(("preach", "sermon")) for x in w):
        return "preach"
    if any(x.startswith("announce") for x in w) or w & {"notices", "news"}:
        return "every"
    return "pick"


def _natural(name: str) -> list:
    """Sort key that puts "Slide 2" before "Slide 10"."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", name or "")]

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


def _budgeted(assets: list, items: list, cap: int = MAX_CANDIDATES) -> list:
    """Spend the row budget ACROSS the runsheet, not on one global rank.

    Ranking every asset against the service name and taking the top 150
    gave a twelve-item runsheet 150 assets that all looked like the
    service and none chosen for any particular line. Each item now gets
    its own share — the assets whose names that line accounts for most —
    and whatever budget is left over is filled in folder order.

    Every name and line is tokenised once up front: inside the sort key,
    sixty lines against a 3,000-file bin was 360,000 regex passes a parse.
    """
    if len(assets) <= cap:
        return assets
    lines = [tokens(t) for t in (_item_text(it) for it in items or []) if t.strip()]
    names = [tokens(a.get("name") or "") for a in assets]
    per = max(1, cap // len(lines)) if lines else 0
    out, seen = [], set()
    for line in lines:
        echo = [len(n & line) / len(n) if n else 0.0 for n in names]
        for k in sorted(range(len(assets)), key=lambda k: -echo[k])[:per]:
            a = assets[k]
            if a["uuid"] not in seen:
                seen.add(a["uuid"])
                out.append(a)
    for a in assets:
        if len(out) >= cap:
            break
        if a["uuid"] not in seen:
            seen.add(a["uuid"])
            out.append(a)
    return out[:cap]


def scope(bin_items: list, service: str, items=None, rows=None,
          template_names=(), suggested=None, title: str = "") -> dict:
    """What to offer the asset-choosing model, what code places itself,
    and where it all came from.

    Returns {"assets", "fixed", "folders", "jobs", "source", "service"}.
    `assets` are the `every` and `pick` files for the model, each tagged
    with its "job" — every announcement first and always, then a budget
    of the rest. `fixed` holds the preach, start and end files, which
    never need the model. `jobs` is {folder: job}. `source` says who
    chose the folders, best authority first:
      "chosen"    — the operator's saved row for this service; final.
      "suggested" — the folder-choosing model (`suggested`, {folder:
                    job} or a list), held to eligible_folders so it
                    can't reach another service's.
      "matched"   — no model answer, so folders whose name names this
                    service.
      "none"      — nothing in the bin is for this service.
    The caller shows `folders` whatever the source: a silent scope is
    what made the first version feel arbitrary.
    """
    row = row_for_service(rows, service, title)
    folders = [str(f).strip() for f in (row.get("folders") or [])
               if isinstance(f, str) and f.strip()] if row else []
    given = row.get("jobs") if row and isinstance(row.get("jobs"), dict) else {}
    source = "chosen" if folders else ""
    if not folders and suggested:
        allowed = {f["name"] for f in eligible_folders(
            bin_items, service, items, rows, template_names)}
        given = suggested if isinstance(suggested, dict) else {}
        folders = [f for f in dict.fromkeys(suggested) if f in allowed]
        source = "suggested" if folders else ""
    if not folders:
        folders = recommended_folders(bin_items, service, rows, template_names)
        given, source = {}, ("matched" if folders else "none")
    jobs = {f: given.get(f) if given.get(f) in JOBS else folder_job(f)
            for f in folders}
    wanted = {f: [] for f in folders}
    for b in _assets(bin_items):
        bucket = wanted.get((b.get("folder") or "").strip())
        if bucket is not None:
            bucket.append({**b, "job": jobs[b["folder"].strip()]})
    by_job = {j: [b for f in folders if jobs[f] == j for b in wanted[f]]
              for j in JOBS}
    must = by_job["every"][:MAX_CANDIDATES]
    title_first = lambda a: (not re.search(r"title|announc", a["name"], re.I),
                             _natural(a["name"]))
    return {"assets": must + _budgeted(by_job["pick"], items,
                                       MAX_CANDIDATES - len(must)),
            "fixed": {"preach": sorted(by_job["preach"], key=title_first),
                      "start": sorted(by_job["start"], key=lambda a: _natural(a["name"])),
                      "end": sorted(by_job["end"], key=lambda a: _natural(a["name"]))},
            "folders": folders, "jobs": jobs, "source": source,
            "service": str(row.get("match") or "") if row else ""}


def _entry(asset: dict, job: str) -> dict:
    """One playlist entry in PP's shape. `target_uuid` is the identity PP
    honours for media; `uuid` mirrors it (propresenter/playlist.py).
    Marked `from_bin` with its folder's job, so placing again replaces
    only the bin's own files, never a template's on the same line."""
    return {"name": asset["name"], "uuid": asset["uuid"],
            "target_uuid": asset["uuid"], "type": "media", "index": 0,
            "from_bin": True, "job": job}


def _index(v, size: int):
    """`v` when it is a real position in a list of `size`, else None."""
    ok = isinstance(v, int) and not isinstance(v, bool) and 0 <= v < size
    return v if ok else None


def _first(items: list, kind: str):
    return next((i for i, it in enumerate(items)
                 if isinstance(it, dict) and it.get("type") == kind), None)


def place(items: list, picks, offered: list, fixed=None) -> tuple:
    """Where every bin file goes: ({item index: [entries]}, left over),
    left over being {"announcements": n, "preach": n} with no line to go to.

    The model only pairs numbers; names and uuids are read from the bin
    here, so it cannot invent an asset PP would 404 on, and a pair counts
    only when both numbers are real.

    `pick` files go where the model put them, under up to four segments
    each (owner, 28 Oct 2026: a hold screen or a giving QR belongs in more
    than one place), at most four to a line. `every` files go in exactly
    once whatever the model says: one it didn't place goes under the
    first announcement line. `preach` files go under the first sermon,
    `start` files open the first line and `end` files close the last.

    A screen the template already puts anywhere in the playlist is never
    added again from a folder — same file or same name (owner, 21 Nov
    2026: the office may keep Welcome and Giving in the Announcements
    folder so they can see them; the template still owns them).
    """
    out: dict = {}
    left = {"announcements": 0, "preach": 0}
    done = set()
    owned = set()
    for it in items:
        lm = it.get("library_match") if isinstance(it, dict) else None
        for e in (lm or {}).get("items") or [] if isinstance(lm, dict) else []:
            if isinstance(e, dict):
                owned |= {str(e.get("uuid") or e.get("target_uuid") or ""),
                          str(e.get("name") or "").strip().lower()}
    owned.discard("")

    def put(i, asset):
        line = out.setdefault(i, [])
        if ({asset["uuid"], asset["name"].strip().lower()} & owned
                or any(e["uuid"] == asset["uuid"] for e in line)):
            return False
        line.append(_entry(asset, asset.get("job") or "pick"))
        return True

    by_n = dict(enumerate(offered or []))
    for pick in (picks if isinstance(picks, list) else [])[:2 * MAX_CANDIDATES]:
        if not isinstance(pick, dict):
            continue
        n, i = _index(pick.get("media"), len(by_n)), _index(pick.get("item"), len(items))
        if n is None or i is None:
            continue
        a = by_n[n]
        if a.get("job") == "every":
            if a["uuid"] not in done and put(i, a):
                done.add(a["uuid"])
        elif sum(e["job"] == "pick" for e in out.get(i, [])) < MAX_PER_ITEM:
            put(i, a)
    home = _first(items, "announcement")
    for a in by_n.values():
        if a.get("job") != "every" or a["uuid"] in done:
            continue
        done.add(a["uuid"])
        if home is None:
            left["announcements"] += 1
        else:
            put(home, a)
    fixed = fixed or {}
    preach = _first(items, "sermon")
    for a in fixed.get("preach") or []:
        if preach is None:
            left["preach"] += 1
        else:
            put(preach, a)
    if items:
        for job, i in (("start", 0), ("end", len(items) - 1)):
            for a in fixed.get(job) or []:
                put(i, a)
    return {i: line for i, line in out.items() if line}, left


def stale(answer, offered: list) -> list:
    """Names of the files the model thinks are past their date, held to
    what it was shown. A warning only: they are still placed (owner,
    20 Nov 2026: "all of them, warn me about stale ones")."""
    picks = answer.get("stale") if isinstance(answer, dict) else None
    names = [offered[n]["name"] for n in (picks if isinstance(picks, list) else [])[:20]
             if _index(n, len(offered or [])) is not None]
    return list(dict.fromkeys(names))


def bin_entries(lm) -> list:
    """The files a line got from the bin. A v2.19 section marked the whole
    section `from_bin` rather than each entry; both count."""
    if not isinstance(lm, dict) or not isinstance(lm.get("items"), list):
        return []
    return [e for e in lm["items"] if isinstance(e, dict)
            and (e.get("from_bin") or lm.get("from_bin"))]


def without_bin(lm):
    """A line's library_match with the bin's files taken out: the
    template's link as it was, or None when the bin was all it had."""
    if not bin_entries(lm):
        return lm
    if lm.get("from_bin"):
        return None
    return {**lm, "items": [e for e in lm["items"]
                            if not (isinstance(e, dict) and e.get("from_bin"))]}


def with_bin(item: dict, entries: list):
    """`item`'s library_match with its old bin files swapped for
    `entries`, every file in its place on the line: the opening loop, the
    template's screens, the placed files, the preach, the closing loop."""
    lm = without_bin(item.get("library_match"))
    if not entries:
        return lm
    # Items arrive back from the page, so an entry need not be a dict.
    order = lambda e: _ORDER.get(e.get("job", "") if isinstance(e, dict)
                                 and e.get("from_bin") else "", 1)
    if lm is None:
        return {"header": {"name": (item.get("title") or "").strip(),
                           "uuid": "", "color": {}},
                "items": sorted(entries, key=order), "from_bin": True}
    if not isinstance(lm.get("items"), list):
        return lm                           # an old single-slide link: leave it
    return {**lm, "items": sorted(lm["items"] + entries, key=order)}
