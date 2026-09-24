"""Ask a model which runsheet line each slide in an existing playlist
belongs to.

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

The question is per SLIDE, not per header, because a playlist can be
out of runsheet order: filing each slide under its line is what shows
that, and what lets songs travel with their worship set when the
operator agrees to put the playlist in runsheet order.

  • WHAT SOMEONE VOUCHES FOR IS GIVEN AS FACT, not re-asked: an alias
    they taught, and songs, whose `.pro` file is named after the song.
    Existing headers are shown as context, not facts — slides may have
    been moved under them. Media file names are weighed below what the
    slide actually reads; in a working playlist they are often stale.
  • EVERY playlist item is listed, including ones with no OCR text:
    their neighbours are the only signal they have.

Nothing here is trusted. `parse_sections` re-checks every number the
model returns, and `propresenter/playlist_update.py` scores whatever
survives BELOW recall and below the operator's aliases — a thing the
operator told us always beats a thing the model inferred."""

import json
import logging
import re
import time

from ..config import APP_NAME
from ..logging_setup import log_safe
from .models import provider_failure
from .openrouter import Stopped, chat


log = logging.getLogger("pp_runsheet")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Wall-clock limit on the whole pass, backup model included; past it the
# call is dropped, not just abandoned (see openrouter.chat). Good answers
# from the free default model took 12-28 s; a paid one takes a few.
_BUDGET_S = 30

_PROMPT = """\
You are organising a ProPresenter playlist to match a church service runsheet.

The runsheet lists what happens in the service, in order. The playlist
holds the slides and media for that service. Your job: say which
runsheet line each playlist item belongs to, so it can sit under that
line's header.

RUNSHEET, in service order:
{runsheet}

PLAYLIST, as it is now. "reads:" is text read off the slide itself by
OCR; absent means the slide has no readable text (a photo, a motion
background, or a video). Lines starting "--" are headers already in the
playlist: someone put them there, but slides may have been moved since.
{playlist}

{facts}RULES
- Answer for every numbered playlist item: the runsheet line it belongs
  to, or null when you genuinely cannot tell. Null is a good answer: a
  wrong guess puts a slide in the wrong part of a live service.
- A line can have several items (songs in worship, sermon slides) or none.
- The playlist may be out of order. Judge each item by what it is, not
  by where it sits; use its neighbours only when nothing else tells you.
- A slide that reads something belongs to the line its text is about.
  Slide names are often out of date: use a name only when the slide
  has no readable text, and never over what the slide reads.
- Otherwise weigh the header above it, its neighbours, and the
  runsheet's times and durations (a 30-minute line is the sermon; a
  3-minute one is not).
- The facts above are settled. Do not contradict them.

Reply with JSON only:
{{"items": [{{"playlist": 0, "runsheet": 1}}, {{"playlist": 1, "runsheet": null}}]}}
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
    """The playlist as it stands, slides numbered as the payload builder
    counts them (headers don't count), headers shown as context.

    Items with no OCR text are still listed: their neighbours are the
    only thing that says where they belong."""
    text = slide_text or {}
    lines, pos = [], 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = ((it.get("id") or {}).get("name") or "").strip() or "(unnamed)"
        if is_header_fn(it):
            lines.append(f"-- header: {name}")
            continue
        got = (text.get(pos) or "").strip()
        lines.append(f"{pos}. [{(it.get('type') or 'item').lower()}] {name}"
                     + (f'  reads: "{got}"' if got else ""))
        pos += 1
    return "\n".join(lines)


def describe_facts(known: dict, matched: list) -> str:
    """What someone vouched for — `{playlist position: runsheet line}`."""
    lines = []
    for pos, n in sorted((known or {}).items()):
        p = ((matched[n].get("parsed") or {})
             if n < len(matched) and isinstance(matched[n], dict) else {})
        title = (p.get("title") or "").strip() or "(untitled)"
        lines.append(f"- playlist item {pos} belongs to runsheet line {n} ({title})")
    return ("FACTS, already settled:\n" + "\n".join(lines) + "\n\n") if lines else ""


def build_alignment_prompt(matched: list, items: list, slide_text: dict,
                           known: dict, is_header_fn) -> str:
    return _PROMPT.format(
        runsheet=describe_runsheet(matched),
        playlist=describe_playlist(items, slide_text, is_header_fn),
        facts=describe_facts(known, matched))


def _index(v, size: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < size


def parse_sections(content: str, n_runsheet: int, n_items: int,
                   known: dict = None) -> dict:
    """Validate a model's answer into `{playlist position: runsheet line}`.

    Every number is re-checked, because a bad one puts a slide in the
    wrong part of a live service. Dropped: anything out of range, a
    second answer for the same slide, and an answer about a slide that is
    already a fact. Thrown away whole: a reply that files every slide
    under one line of a longer runsheet — what a model does when it has
    not understood the question but still wants to be helpful.

    The answer may be out of runsheet order; that is how a shuffled
    playlist is noticed. Returns {} for an unusable reply, which leaves
    placement to the deterministic rules."""
    known = known or {}
    try:
        body = re.sub(r"^```[a-z]*\n?|\n?```$", "", content.strip())
        m = re.search(r"\{.*\}", body, re.DOTALL)
        data = json.loads(m.group() if m else body)
    except Exception:
        log.info("Alignment reply was not JSON — ignoring it")
        return {}
    rows = data.get("items") if isinstance(data, dict) else data
    out = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        pos, n = row.get("playlist"), row.get("runsheet")
        if _index(pos, n_items) and _index(n, n_runsheet) \
                and pos not in known and pos not in out:
            out[pos] = n
    # Every slide under one line is a collapse — unless the facts sit on
    # other lines and the whole still runs in runsheet order: the songs
    # known, and the rest one sermon deck.
    merged = [{**known, **out}[p] for p in sorted({**known, **out})]
    if len(out) > 2 and n_runsheet > 2 and len(set(out.values())) == 1 and (
            set(known.values()) <= set(out.values())
            or merged != sorted(merged)):
        log.info("Alignment reply put every slide under one line — ignoring it")
        return {}
    return out


def align_playlist(matched: list, items: list, slide_text: dict, known: dict,
                   is_header_fn, or_key: str, model: str, post=None,
                   backup: str = None) -> dict:
    """One OpenRouter call, fully validated: `{playlist position: runsheet
    line}` for the slides the model could place. {} whenever anything is
    off. `items` is the playlist as it stands, headers included.

    `backup` is asked once if `model`'s provider fails, as the parse does.

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

        deadline = time.monotonic() + _BUDGET_S

        def ask(model_id):
            return chat(
                sender, OPENROUTER_URL, timeout_s=deadline - time.monotonic(),
                headers={"Authorization": f"Bearer {or_key}",
                         "HTTP-Referer": "runsheet-pilot",
                         "X-Title": APP_NAME,
                         "Content-Type": "application/json"},
                body={"model": model_id,
                      "messages": [{"role": "user", "content": prompt}],
                      # Placement must not wobble between two runs of the
                      # same runsheet: update mode treats an identical
                      # result as a no-op and skips the write entirely.
                      "temperature": 0,
                      "response_format": {"type": "json_object"},
                      # Slide text and runsheet lines carry names: only
                      # providers that neither store nor train on requests.
                      "provider": {"data_collection": "deny"}})

        r = ask(model)
        failure = provider_failure(r)
        if failure and backup and backup != model:
            log.info("Alignment: %s failed behind %s (%s) — retrying with %s",
                     log_safe(failure["provider"]), log_safe(model),
                     failure["code"], log_safe(backup))
            r = ask(backup)
            failure = provider_failure(r)
        if failure:
            log.info("Alignment: %s failed (%s) — placing without it",
                     log_safe(failure["provider"]), failure["code"])
            return {}
        if not getattr(r, "ok", False):
            log.info("Alignment call returned HTTP %s — placing without it",
                     getattr(r, "status_code", "?"))
            return {}
        content = (r.json()["choices"][0]["message"]["content"]) or ""
    except Stopped:
        log.info("Alignment: no answer within %ss — placing without it",
                 _BUDGET_S)
        return {}
    except Exception:
        log.info("Alignment call failed — placing without it", exc_info=True)
        return {}
    n_items = sum(1 for it in items or []
                  if isinstance(it, dict) and not is_header_fn(it))
    out = parse_sections(content, len(matched), n_items, known)
    log.info("Alignment filed %d of %d slides under a runsheet line",
             len(out), n_items - len(known or {}))
    return out
