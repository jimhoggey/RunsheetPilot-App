"""Ask which Media-bin asset belongs to which runsheet item.

Its own call, for the same reason the lighting guide has one: the main
parse prompt is already long, and this is a different question — not
"what does the runsheet say" but "which of these files is the one for
this segment". The candidates are narrowed first by
propresenter/media_match.py, so the model is choosing among the event's
own media rather than searching a bin holding years of it.

The model only pairs numbers. Names and uuids are read from the bin
(media_match.sections), so it cannot invent an asset ProPresenter would
404 on, and what it answers is held to what the code allows.
"""
import json
import logging

import requests

from ..config import APP_NAME
from ..logging_setup import log_safe
from .align import OPENROUTER_URL
from .models import fetch_catalogue, reasoning_for, resolve_model
from .openrouter import Stopped, chat

log = logging.getLogger("pp_runsheet")

_MAX_ROWS = 150

PROMPT = (
    "Below are a church service's runsheet items, numbered, and the media "
    "files available for it in ProPresenter, numbered. The files are named "
    "by whoever made them, so match on meaning: \"WTSSF_Summit2026_Screen_"
    "QR_1920x1080\" is the QR screen, \"...Opener...\" is the opener film.\n"
    "Say which item each file belongs to. Most files belong somewhere, but "
    "a file you can't place confidently is better left out than guessed — a "
    "wrong file on screen is worse than none. Leave an item out if nothing "
    "fits it; give an item two or three files only when they really all "
    "belong to that one segment.\n"
    "Answer with JSON only: {\"media\": [{\"media\": 0, \"item\": 2}, ...]}"
)


def _rows(label_of, rows: list) -> str:
    return "\n".join(f"{i}. {label_of(r)}" for i, r in enumerate(rows[:_MAX_ROWS]))


def plan_media(items: list, offered: list, or_key: str, configured_model: str,
               post=None) -> list:
    """[{"media": n, "item": i}, …] — the model's pairings, unvalidated.

    Empty when there is nothing to match, no key, no model, or the call
    fails: the playlist is then built exactly as it is today, with each
    runsheet item's own coloured header and no media.
    """
    if not (items and offered and or_key):
        return []
    model = resolve_model(configured_model or "", fetch_catalogue(), api_key=or_key)
    if not model:
        return []
    asked = (f"{PROMPT}\n\nRUNSHEET:\n"
             f"{_rows(lambda it: (it.get('title') or '').strip(), items)}\n\n"
             f"MEDIA:\n{_rows(lambda a: a.get('name') or '', offered)}")
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "user", "content": asked}],
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
            log.warning("Media plan: HTTP %s from %s", r.status_code, log_safe(model))
            return []
        reply = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        data = json.loads(reply[reply.index("{"):reply.rindex("}") + 1])
    except (Stopped, ValueError, requests.RequestException):
        log.warning("Media plan failed (%s); the playlist keeps its headers only",
                    log_safe(model))
        return []
    picks = data.get("media")
    return picks if isinstance(picks, list) else []
