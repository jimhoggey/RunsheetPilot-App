"""One small JSON question to OpenRouter — the call every *_plan module makes.

media_plan, lighting_plan and folder_plan each ask a model one focused
question and want one JSON object back. Each used to carry the same twenty
lines to do it; these are those lines, once. What stays in each module is
what differs: its prompt, how long it waits, and what it will accept from
the answer.

The guarantees live here so no caller can drop one: only providers that
neither store nor train on requests (`data_collection: deny`), temperature
0, and None — never an exception — when anything goes wrong, so a failed
question leaves the runsheet as it was.
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


def ask_json(prompt: str, or_key: str, configured_model: str, *, label: str,
             timeout_s: float = 60, post=None):
    """The model's answer to `prompt` as a dict, or None.

    None when there is no key or no usable model, the request fails or
    isn't a 200, or the reply holds no JSON object — each logged under
    `label` ("Media plan"), never with the key. Prose around the JSON is
    tolerated: models wrap it in ``` fences or a sentence of preamble.
    `post` is requests.post, or a stand-in.
    """
    if not or_key:
        return None
    model = resolve_model(configured_model or "", fetch_catalogue(), api_key=or_key)
    if not model:
        return None
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
            # Only providers that neither store nor train on requests.
            "provider": {"data_collection": "deny"}}
    effort = reasoning_for(model, fetch_catalogue())
    if effort:
        body["reasoning"] = effort
    try:
        r = chat(post or requests.post, OPENROUTER_URL, timeout_s=timeout_s,
                 headers={"Authorization": f"Bearer {or_key}",
                          "HTTP-Referer": "runsheet-pilot", "X-Title": APP_NAME},
                 body=body)
        if r.status_code != 200:
            log.warning("%s: HTTP %s from %s", label, r.status_code, log_safe(model))
            return None
        reply = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        data = json.loads(reply[reply.index("{"):reply.rindex("}") + 1])
    except (Stopped, ValueError, requests.RequestException):
        log.warning("%s failed (%s)", label, log_safe(model))
        return None
    return data if isinstance(data, dict) else None
