"""Map the church's lighting guide onto a parsed runsheet — its own call.

The main parse prompt reads what the RUNSHEET says about lighting, and it
does that well. This second call does the other half: where the runsheet
says nothing, which of the guide's moments happens in which item.

Asking one prompt to do both traded them against each other. Sharpen the
runsheet's precedence and the guide stopped filling gaps; soften it and the
guide overwrote levels the runsheet had stated. Here precedence is
STRUCTURAL: an item the runsheet gave a level is never offered to this
call, so the guide cannot overwrite it, whatever the model answers.

The model only chooses which moment lands on which item. The level and the
cue wording come from the guide itself, read here — so a level can never be
invented, and `when` is always the church's own wording. That is the
consistency the guide exists to give.
"""
import json
import logging
import re

import requests

from ..config import APP_NAME
from ..logging_setup import log_safe
from .align import OPENROUTER_URL
from .models import fetch_catalogue, reasoning_for, resolve_model
from .openrouter import Stopped, chat

log = logging.getLogger("pp_runsheet")

# A tidied guide line: "3. First praise song — when: halfway through — 12%"
# (parsing/guide.py writes this shape; an untidied line rarely matches, and
# a moment we can't read is one we don't offer.)
_NUMBERED = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*(.+)$")
_LEVEL = re.compile(r"\d")
_MAX_ITEMS = 60

PROMPT = (
    "A church runs its house lights to the same moments every Sunday. Below "
    "are those moments, numbered, and this Sunday's runsheet items, "
    "numbered.\n"
    "Say which item each moment happens in, matching on what the moment IS. "
    "Work through all of them: most moments do happen, and missing one "
    "leaves that part of the service dark. One item can take two or three "
    "moments, and plenty of items take none. The moments are in service "
    "order and so are the items, so your item numbers must only ever go "
    "FORWARD, and each moment is used at most once. Skip a moment only when "
    "this service really doesn't have it.\n"
    "Where the runsheet lists songs as their own items, the worship-set "
    "moments belong on THOSE items, in order — not on a \"Worship\" block "
    "before them, and never on a later item such as the preach. In a "
    "worship set the first song is praise and the rest are worship, so the "
    "\"first worship song\" is the SECOND song of the set.\n"
    "Answer with JSON only: {\"steps\": [{\"moment\": 1, \"item\": 0}, ...]}"
)


def moments_of(guide: str) -> list:
    """The guide as [{n, when, level}] — its own numbers, cues and levels.

    Read here rather than asked of the model, so a level is never invented
    and the cue wording stays the church's own.
    """
    out = []
    for line in (guide or "").splitlines():
        m = _NUMBERED.match(line)
        if not m:
            continue
        parts = [p.strip() for p in m.group(2).split("—") if p.strip()]
        level = parts[-1] if len(parts) > 1 and _LEVEL.search(parts[-1]) else ""
        when = next((p[len("when:"):].strip() for p in parts
                     if p.lower().startswith("when:")), "")
        # The moment's NAME ("Land worship", "Welcome", "Preach HARD SWITCH")
        # is what matches a runsheet item; its `when` is what the operator
        # reads. Both are needed, for different jobs.
        name = parts[0] if parts and not parts[0].lower().startswith("when:") else ""
        if level:
            out.append({"n": int(m.group(1)), "name": name[:70],
                        "when": when[:60], "level": level[:30]})
    return out


def _item_lines(items: list) -> str:
    rows = []
    for i, it in enumerate(items[:_MAX_ITEMS]):
        bits = [str(it.get("start_time") or ""), str(it.get("title") or "")]
        if it.get("lighting_steps"):
            bits.append("(the runsheet sets this item's lighting itself)")
        rows.append(f"{i}. " + "  ".join(b for b in bits if b))
    return "\n".join(rows)


def _chosen(reply: str, moments: dict, items: list) -> dict:
    """{item index: [step]} from the model's answer, keeping only choices
    that are real and in service order."""
    try:
        data = json.loads(reply[reply.index("{"):reply.rindex("}") + 1])
    except (ValueError, TypeError):
        return {}
    out, last, used = {}, -1, set()
    for pick in (data.get("steps") or [])[:_MAX_ITEMS]:
        if not isinstance(pick, dict):
            continue
        n, i = pick.get("moment"), pick.get("item")
        if not (isinstance(n, int) and isinstance(i, int)):
            continue
        # Real, unused, in order, and on an item the runsheet left alone —
        # that last one is what makes the runsheet win no matter the answer.
        if n not in moments or n in used or not (last <= i < len(items)):
            continue
        if items[i].get("lighting_steps"):
            continue
        used.add(n)
        last = i
        out.setdefault(i, []).append({"level": moments[n]["level"], "when": moments[n]["when"]})
    return out


def plan_lighting(items: list, guide: str, or_key: str, configured_model: str,
                  post=None) -> dict:
    """{item index: [{level, when}]} for the items the runsheet left blank.

    Empty when there is no guide, no key, no model, or the call fails — the
    runsheet's own lighting still stands on its own.
    """
    moments = moments_of(guide)
    blank = [i for i, it in enumerate(items[:_MAX_ITEMS]) if not it.get("lighting_steps")]
    if not (moments and blank and or_key and items):
        return {}
    model = resolve_model(configured_model or "", fetch_catalogue(), api_key=or_key)
    if not model:
        return {}
    guide_lines = "\n".join(
        f"{m['n']}. {m['name']}" + (f" — {m['when']}" if m["when"] else "")
        for m in moments)
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "user", "content":
                          f"{PROMPT}\n\nMOMENTS:\n{guide_lines}\n\nRUNSHEET:\n{_item_lines(items)}"}],
            # Only providers that neither store nor train on requests.
            "provider": {"data_collection": "deny"}}
    effort = reasoning_for(model, fetch_catalogue())
    if effort:
        body["reasoning"] = effort
    try:
        r = chat(post or requests.post, OPENROUTER_URL, timeout_s=60,
                 headers={"Authorization": f"Bearer {or_key}",
                          "HTTP-Referer": "runsheet-pilot", "X-Title": APP_NAME},
                 body=body)
        if r.status_code != 200:
            log.warning("Lighting plan: HTTP %s from %s", r.status_code, log_safe(model))
            return {}
        reply = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    except (Stopped, ValueError, requests.RequestException):
        log.warning("Lighting plan failed (%s); the runsheet's own lighting stands", log_safe(model))
        return {}
    return _chosen(reply, {m["n"]: m for m in moments}, items)
