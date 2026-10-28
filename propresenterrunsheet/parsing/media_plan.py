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
    "A file may be used in more than one item when it genuinely belongs in "
    "each — a hold screen, a loop, a lower third or a QR slide can recur "
    "through a service. A film, a sermon bumper or anything made for one "
    "moment belongs to one item only; repeating it is a mistake.\n"
    "Answer with JSON only: {\"media\": [{\"media\": 0, \"item\": 2}, ...]}"
)


def _rows(label_of, rows: list) -> str:
    return "\n".join(f"{i}. {label_of(r)}" for i, r in enumerate(rows[:_MAX_ROWS]))


def _item_row(it: dict) -> str:
    """The title, and the tech note when there is one. The note is where
    a line says what it actually wants on screen — "promo Summit" lives
    there far more often than in the title."""
    title = (it.get("title") or "").strip()
    note = " ".join((it.get("notes") or "").split())[:120]
    return f"{title} — {note}" if note else title


def _media_row(a: dict) -> str:
    """The file, under the folder it sits in. The folder is the operator's
    own filing — "Summit 2026", "Pre-service loops" — and is often the
    only thing that says what a file like WTSSF_B2_v3 is for."""
    name = a.get("name") or ""
    folder = (a.get("folder") or "").strip()
    return f"{folder} / {name}" if folder else name


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
             f"{_rows(_item_row, items)}\n\n"
             f"MEDIA:\n{_rows(_media_row, offered)}")
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
