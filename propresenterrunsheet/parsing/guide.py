"""Tidy a church's lighting guide, once, when it is uploaded.

A guide is usually a designed one-pager: read out of the PDF its table
comes apart into jumbled lines, and a model asked to map that onto every
runsheet got it right one parse and wrong the next (Worship's three
changes landing on the wrong songs). Turned once into a plain numbered
list — moment, cue, level — the same parses came out right twice running.
So the upload pays for one model call, the operator can read and correct
the list in Service Mate, and every parse sends the tidy version.
"""
import logging

import requests

from ..config import APP_NAME
from ..logging_setup import log_safe
from .align import OPENROUTER_URL
from .models import fetch_catalogue, reasoning_for, resolve_model
from .openrouter import Stopped, chat

log = logging.getLogger("pp_runsheet")

TIDY_PROMPT = (
    "Below is a church's guide to its house lighting through a service, "
    "read out of a document, so its layout may be jumbled. Rewrite it as a "
    "numbered list of its moments in service order, one per line, exactly:\n"
    "N. <moment> — when: <the cue: what happens in the room, short> — "
    "<house-lights level>\n"
    "Rules: one level per line as a percentage (a named stage like "
    "\"Deep 2 to 3%\" becomes \"2%\"; \"Hold 30%\" stays \"hold 30%\"). In a "
    "worship set the first song is praise and the songs after it are "
    "worship: add which song of the set a moment belongs to, e.g. "
    "\"(1st song of the set)\". Keep it short. No other text.\n\nGUIDE:\n---\n"
)


def tidy_guide(text: str, or_key: str, configured_model: str, post=None) -> str:
    """The guide as a numbered list of moments, or "" when it couldn't be
    tidied (no key or model, the call failed) — the caller keeps the text
    as read then, which still works, just less reliably."""
    text = (text or "").strip()
    if not text or not or_key:
        return ""
    model = resolve_model(configured_model or "", fetch_catalogue(), api_key=or_key)
    if not model:
        return ""
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "user", "content": TIDY_PROMPT + text + "\n---"}],
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
            log.warning("Lighting guide tidy: HTTP %s from %s", r.status_code, log_safe(model))
            return ""
        out = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    except (Stopped, ValueError, requests.RequestException):
        log.warning("Lighting guide tidy failed (%s); keeping the guide as read", log_safe(model))
        return ""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()[:1].isdigit()]
    return "\n".join(lines)
