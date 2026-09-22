"""Ask a model where each runsheet header belongs in an existing playlist.

The last resort in update mode's placement cascade, and the only one
that can read MEANING. The deterministic rules compare strings: every
word of "WELCOME SLIDE" must appear in the runsheet title. Against
`IMG_4021` they have nothing, and against a slide whose graphic says
GIVING while the runsheet line says "Offering" they have nothing either
— the two share no word at all.

What this module sends is TEXT, never images. `propresenter/
thumbnails.py` has already pulled each still's thumbnail from
ProPresenter and read it with the platform's own OCR engine, locally
and for free, so the model receives "item 7 reads GIVING" rather than
a picture of item 7. Same information, a fraction of the tokens, and
it works on any model rather than only a vision one.

Three things make this a tractable question rather than an open
40-way alignment:

  • BOTH LISTS ARE IN SERVICE ORDER. The model is filling gaps in a
    sequence, not matching two unordered bags.
  • THE DETERMINISTIC ANCHORS ARE GIVEN AS FACTS, not re-asked. Songs
    in particular are usually already placed, because a `.pro` file is
    normally named after its song — so they pin the sequence and the
    model only reasons about what sits between them.
  • EVERY playlist item is listed, including ones with no OCR text.
    Omitting them would leave holes in the ordering and destroy the one
    signal that costs nothing.

Nothing here is trusted. `parse_alignment` re-checks every number the
model returns, and `propresenter/playlist_update.py` scores whatever
survives BELOW recall and below the operator's aliases — a thing the
operator told us always beats a thing the model inferred. The preview
card then shows the result before a single byte is written."""

import json
import logging
import re

from ..config import APP_NAME


log = logging.getLogger("pp_runsheet")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# A model that maps most of the runsheet onto one slide has not solved
# the problem, it has collapsed. Such a reply is thrown away whole
# rather than partly believed — see parse_alignment.
_COLLAPSE_RATIO = 0.6

_PROMPT = """\
You are placing section headers into a ProPresenter playlist.

A church service runsheet lists what happens, in order. A ProPresenter
playlist holds the slides and media for that service, also in order.
Your job: for each runsheet line, say which playlist item it starts at,
so a coloured header can be inserted directly above that item.

RUNSHEET, in service order:
{runsheet}

PLAYLIST, in order. "reads:" is text read off the slide itself by OCR;
absent means the slide has no readable text (a photo, a motion
background, or a video, which this operator always names on the
runsheet instead).
{playlist}

{anchors}
RULES
- Answer with the playlist index each runsheet line starts at.
- Indexes must INCREASE down the runsheet. Two lines cannot share an
  index, and a later line cannot point above an earlier one.
- Use null when you genuinely cannot tell. Null is a good answer. A
  wrong guess puts the wrong label above the wrong slide in front of a
  live congregation; an honest null gets the header placed in runsheet
  order instead, which is safe.
- The already-placed lines above are FACTS. Do not move them, and keep
  everything else consistent with them.
- Weigh, in this order: text read off the slide, the slide's name, the
  position in the sequence, then the runsheet's times and durations
  (a 30-minute line is the sermon; a 3-minute one is not).

Reply with JSON only:
{{"placements": [{{"runsheet": 0, "playlist": 3}}, {{"runsheet": 1, "playlist": null}}]}}
"""


def describe_runsheet(matched: list) -> str:
    """The runsheet as numbered lines, with the priors that cost nothing.

    Time and duration are included because they discriminate exactly
    where OCR is blank: a 30-minute item is the sermon whatever the
    slide behind it looks like."""
    lines = []
    for n, mi in enumerate(matched or []):
        p = (mi.get("parsed") or {}) if isinstance(mi, dict) else {}
        bits = [f"{n}. {(p.get('title') or '').strip() or '(untitled)'}"]
        if p.get("type"):
            bits.append(f"[{p['type']}]")
        if (p.get("start_time") or "").strip():
            bits.append(f"at {p['start_time'].strip()}")
        try:
            mins = int(p.get("duration_min") or 0)
        except (TypeError, ValueError):
            mins = 0
        if mins > 0:
            bits.append(f"{mins} min")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def describe_playlist(items: list, slide_text: dict, is_header_fn) -> str:
    """Every non-header playlist item, numbered by its real index.

    Items with no OCR text are still listed. They are the sequence the
    model reasons about, and leaving them out would turn a dense ordered
    list into a sparse one with unexplained gaps."""
    text = slide_text or {}
    lines = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict) or is_header_fn(it):
            continue
        name = ((it.get("id") or {}).get("name") or "").strip() or "(unnamed)"
        kind = (it.get("type") or "item").lower()
        line = f"{i}. [{kind}] {name}"
        got = (text.get(i) or "").strip()
        if got:
            line += f'  reads: "{got}"'
        lines.append(line)
    return "\n".join(lines)


def describe_anchors(known: dict, matched: list) -> str:
    """The deterministic matches, stated as settled."""
    if not known:
        return ""
    lines = []
    for n in sorted(known):
        p = ((matched[n].get("parsed") or {})
             if n < len(matched) and isinstance(matched[n], dict) else {})
        title = (p.get("title") or "").strip() or "(untitled)"
        lines.append(f"- runsheet {n} ({title}) is already placed at "
                     f"playlist item {known[n]}")
    return ("ALREADY PLACED — these are settled, work around them:\n"
            + "\n".join(lines) + "\n\n")


def build_alignment_prompt(matched: list, items: list, slide_text: dict,
                           known: dict, is_header_fn) -> str:
    return _PROMPT.format(
        runsheet=describe_runsheet(matched),
        playlist=describe_playlist(items, slide_text, is_header_fn),
        anchors=describe_anchors(known, matched))


def parse_alignment(content: str, n_runsheet: int, max_index: int,
                    known: dict = None) -> dict:
    """Validate a model's answer into `{runsheet_index: playlist_index}`.

    Every number is re-checked here, because the cost of a bad one is a
    wrong label above a slide during a live service. Rejected outright:
    indexes out of range, a line that contradicts a deterministic
    anchor, an order that goes backwards, and — whole-reply — an answer
    that collapses most of the runsheet onto a single slide, which is
    what a model does when it has not understood the question but still
    wants to be helpful.

    Returns {} for an unusable reply. That is not a failure: it means
    placement falls back to the deterministic rules, which is exactly
    where it started."""
    known = known or {}
    try:
        body = content.strip()
        body = re.sub(r"^```[a-z]*\n?", "", body)
        body = re.sub(r"\n?```$", "", body)
        m = re.search(r"\{.*\}", body, re.DOTALL)
        data = json.loads(m.group() if m else body)
    except Exception:
        log.info("Alignment reply was not JSON — ignoring it")
        return {}

    raw = data.get("placements") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return {}

    proposed = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        n, pos = row.get("runsheet"), row.get("playlist")
        if not isinstance(n, int) or isinstance(n, bool):
            continue
        if pos is None or isinstance(pos, bool) or not isinstance(pos, int):
            continue
        if not (0 <= n < n_runsheet) or not (0 <= pos <= max_index):
            continue
        # A deterministic anchor outranks the model by construction, so a
        # contradiction is dropped rather than argued with.
        if n in known:
            continue
        proposed[n] = pos

    if not proposed:
        return {}

    # Collapse guard: "everything belongs at item 3" is syntactically
    # perfect and semantically worthless.
    counts = {}
    for pos in proposed.values():
        counts[pos] = counts.get(pos, 0) + 1
    if len(proposed) > 2 and max(counts.values()) / len(proposed) >= _COLLAPSE_RATIO:
        log.info("Alignment reply collapsed onto one slide — ignoring it")
        return {}

    # Enforce a strictly increasing sequence against the anchors too, so
    # the model's answers and the settled ones form one coherent order.
    merged = dict(known)
    merged.update(proposed)
    kept, last_pos = {}, -1
    for n in sorted(merged):
        pos = merged[n]
        if pos <= last_pos:
            if n in known:
                # Never drop a settled anchor; drop whatever crossed it.
                kept = {k: v for k, v in kept.items()
                        if k in known or v < pos}
                kept[n] = pos
                last_pos = pos
            continue
        kept[n] = pos
        last_pos = pos
    return {n: pos for n, pos in kept.items() if n not in known}


def align_playlist(matched: list, items: list, slide_text: dict, known: dict,
                   is_header_fn, or_key: str, model: str, post=None) -> dict:
    """One OpenRouter call, fully validated. {} whenever anything is off.

    Never raises: placement without this pass is the shipped behaviour,
    so every failure here degrades to it rather than stopping an
    operator who is trying to get a service ready."""
    if not or_key or not model or not matched:
        return {}
    prompt = build_alignment_prompt(matched, items, slide_text, known,
                                   is_header_fn)
    try:
        sender = post
        if sender is None:
            import requests as req
            sender = req.post
        r = sender(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {or_key}",
                     "HTTP-Referer": "runsheet-pilot",
                     "X-Title": APP_NAME,
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  # Placement must not wobble between two runs of the
                  # same runsheet: update mode treats an identical
                  # result as a no-op and skips the write entirely.
                  "temperature": 0,
                  "response_format": {"type": "json_object"}},
            timeout=60)
        if not getattr(r, "ok", False):
            log.info("Alignment call returned HTTP %s — placing without it",
                     getattr(r, "status_code", "?"))
            return {}
        content = (r.json()["choices"][0]["message"]["content"]) or ""
    except Exception:
        log.info("Alignment call failed — placing without it", exc_info=True)
        return {}
    out = parse_alignment(content, len(matched), max(len(items) - 1, 0), known)
    log.info("Alignment placed %d of %d runsheet items the rules missed",
             len(out), len(matched) - len(known or {}))
    return out
