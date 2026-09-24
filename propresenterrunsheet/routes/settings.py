"""User-settings routes — /api/settings (GET/POST) and /api/prompt (GET/POST).

GET /api/settings tacks on the discovered ProPresenter root and library
folders so the UI can pre-fill its dropdowns; the POST is a partial
update that merges into whatever's already on disk."""

import difflib
import re
import sys

from flask import Blueprint, jsonify, request

from ..config import DATA_DIR, VERSION, WHATS_NEW, recent_release_notes
from ..parsing.ai import DEFAULT_PROMPT
from ..parsing.models import (
    catalogue_entry, estimate_cost, fetch_catalogue, fetch_key_info,
    free_model_ids, is_router, measured_costs, pick_default_model,
    pick_paid_model, reasoning_for, recommended_models, usable_models,
)
from ..propresenter.paths import find_library_dirs, find_pp_root
from ..settings import load_settings, save_settings
from .. import stats


bp = Blueprint("settings", __name__)


@bp.route("/api/models", methods=["GET"])
def get_models():
    """Free models that can return the JSON the parser needs, best first.

    Fetched live from OpenRouter rather than hardcoded — a baked-in list goes
    stale the moment a model is retired, which is exactly how the old default
    (`google/gemini-2.0-flash-exp:free`) ended up 404ing for everyone.

    `auto` is what an unset model setting resolves to, so the dropdown can
    label the automatic option without re-implementing the ranking. Both go
    empty rather than erroring when OpenRouter can't be reached, so Settings
    still opens offline.
    """
    # force=True: the catalogue is cached for hours, which is right for
    # the parse path but wrong here — this is the moment the operator is
    # LOOKING at the list, and a model withdrawn since launch must not
    # still be offered. One request, fails soft to the cache.
    catalogue = fetch_catalogue(force=True)
    # Is the key funded? Paid models are offered only when it is —
    # showing them to someone who can't pay produces a 402 on their first
    # parse, which is a far worse first impression than a shorter list.
    # Unknown (offline, bad key) is treated as not funded.
    settings = load_settings()
    info = fetch_key_info((settings.get("or_key") or "").strip())
    funded = bool(info.get("funded"))
    # For the line under the key in Settings: free or paid, what's left,
    # and what a runsheet has really cost on each model this install used.
    key = {**info, "measured": measured_costs(settings.get("parse_costs"))}

    if not catalogue:
        return jsonify({"models": [], "auto": None, "available": False,
                        "recommended": [], "funded": funded, "key": key})
    # A key with credit runs on a paid model (resolve_model), so the free
    # list is not offered and Automatic names the paid pick. Without credit
    # — including a weekly limit used up — Automatic is free-only, as it
    # must be on a fresh install.
    paid = pick_paid_model(catalogue) if funded else None
    models = [] if paid else [
        {"id": m["id"], "name": m.get("name") or m["id"],
         "context_length": m.get("context_length") or 0}
        for m in usable_models(catalogue)]
    saved = (settings.get("or_model") or "").strip()
    return jsonify({"models": models,
                    "auto": paid or pick_default_model(catalogue),
                    # A free model saved from before the key had credit:
                    # parses already run on `auto`, so the page moves the
                    # setting to Automatic to show that.
                    "free_saved": bool(paid and saved in free_model_ids(catalogue)),
                    "recommended": recommended_models(catalogue) if funded
                                   else [],
                    "funded": funded,
                    "key": key,
                    "available": True})


# vendor/model, optionally :variant — the shape of every OpenRouter id.
_MODEL_ID = re.compile(r"[\w.\-]+/[\w.\-]+(?::[\w.\-]+)?")


@bp.route("/api/models/check", methods=["POST"])
def check_model():
    """A model id pasted into Settings: is it real, and what would a
    runsheet cost on it? Looked up in the catalogue, priced the way the
    recommended models are (estimate_cost). No prompt is sent."""
    model_id = str((request.get_json(silent=True) or {}).get("model") or "").strip()
    if len(model_id) > 120 or not _MODEL_ID.fullmatch(model_id):
        return jsonify({"ok": False, "message": "That doesn't look like an "
                        "OpenRouter model id. They look like openai/gpt-4.1-mini."})
    catalogue = fetch_catalogue()
    if catalogue and not catalogue_entry(catalogue, model_id):
        catalogue = fetch_catalogue(force=True)     # listed since the cache?
    if not catalogue:
        return jsonify({"ok": False, "message": "Couldn't reach OpenRouter to check it."})
    entry = catalogue_entry(catalogue, model_id)
    if entry is None:
        ids = [m.get("id") for m in catalogue.get("data") or []
               if isinstance(m, dict) and isinstance(m.get("id"), str)]
        near = difflib.get_close_matches(model_id, ids, n=1)
        return jsonify({"ok": False, "message": f"OpenRouter has no model called "
                        f"{model_id}." + (f" Did you mean {near[0]}?" if near else "")})

    cost = None if is_router(model_id) else estimate_cost(entry)
    if is_router(model_id):
        price = "its price depends on the model it picks"
    elif cost is None:
        price = "its price isn't listed"
    elif cost == 0:
        price = "free"
    else:   # two significant figures, never 5e-05: "about $0.00049"
        price = f"about ${float(f'{cost:.2g}'):.10f}".rstrip("0") + " a runsheet"
    thinks = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
    if reasoning_for(model_id, catalogue):
        note = " Its reasoning is kept to the minimum."
    elif thinks.get("mandatory") or thinks.get("default_enabled"):
        note = (" It reasons before every answer, so runsheets take longer "
                "and cost more than that.")
    else:
        note = ""
    return jsonify({"ok": True, "cost_per_parse": cost,
                    "message": f"OpenRouter has it — {price}.{note}"})


@bp.route("/api/whats_new", methods=["GET"])
def get_whats_new():
    """Whether to show the once-per-version what's-new popup, and its notes.

    Shows when the running VERSION differs from the last one this install
    recorded — which catches the in-app updater's relaunch AND a manually
    installed DMG, because the trigger is the version change itself, not
    the update action.

    A fresh install (no recorded version) never shows: nothing is "new"
    to someone seeing the app for the first time — their first sight
    should be the welcome greeter, not a changelog. Recording the current
    version here is what arms the popup for their NEXT update.

    Deliberately does NOT mark the version seen — only the dismiss POST
    does. If the app dies between this call and the popup rendering, the
    notes survive to the next launch instead of being silently eaten.
    """
    last = (load_settings().get("last_seen_version") or "").strip()
    if not last:
        save_settings({"last_seen_version": VERSION})
        return jsonify({"show": False, "version": VERSION, "notes": []})
    if last != VERSION:
        stats.track("whats_new_shown", from_version=last)
    return jsonify({
        "show":    last != VERSION,
        "version": VERSION,
        "notes":   list(WHATS_NEW)[:3],
    })


@bp.route("/api/release_notes", methods=["GET"])
def get_release_notes():
    """What changed across the last few releases — for the version badge.

    Distinct from /api/whats_new in two ways that matter: it spans several
    versions rather than only the running one, and it is READ-ONLY. It must
    never mark a version seen, or an operator glancing at the changelog
    would silently rob themselves of the popup after their next update.

    Served from RELEASE_NOTES in config.py rather than the GitHub API on
    purpose — a booth with no internet still gets its changelog, and the
    published release bodies have turned out to be an unreliable source
    (several are GitHub's auto-generated "What's Changed" list).
    """
    return jsonify({"current": VERSION, "releases": recent_release_notes(3)})


@bp.route("/api/whats_new/seen", methods=["POST"])
def post_whats_new_seen():
    """The popup was dismissed — never show these notes again.

    save_settings merges into the file, so the operator's key/host are
    untouched.
    """
    save_settings({"last_seen_version": VERSION})
    return jsonify({"ok": True})


@bp.route("/api/settings", methods=["GET"])
def get_settings():
    # The UI has successfully reached the backend, so this launch is not
    # a "won't open" case — clear the boot marker. This is the first call
    # the front end makes, and doing it here (not on every poll) keeps it
    # to one filesystem touch per run.
    stats.boot_ok()
    s = load_settings()
    pp_root = find_pp_root()
    s["pp_root"] = pp_root
    s["library_dirs"] = find_library_dirs(pp_root)
    s["platform"] = sys.platform
    s["version"] = VERSION
    s["data_dir"] = str(DATA_DIR)
    return jsonify(s)


@bp.route("/api/settings", methods=["POST"])
def post_settings():
    body = request.get_json(silent=True) or {}
    # Read BEFORE writing — this is the only moment the old values still
    # exist, and diffing here rather than in the browser means every writer
    # is covered, including the ones that bypass the UI's autosave (the
    # model dropdown, and the port-discovery writeback that saves without
    # anyone touching the keyboard).
    before = load_settings()
    save_settings(body)
    # Apply the analytics toggle immediately — waiting for a restart to
    # honour "turn this off" is not a real opt-out. Deliberately before
    # track(): someone who just switched analytics OFF must not have that
    # very action phoned home.
    if "stats_enabled" in body:
        stats.set_enabled(bool(body.get("stats_enabled")))
    # keys= is kept so existing charts don't go blank; it is now the least
    # informative thing in the event.
    stats.track("settings_saved", keys=len(body),
                **stats.settings_change_props(before, body))
    return jsonify({"ok": True})


@bp.route("/api/prompt", methods=["GET"])
def get_prompt():
    saved = (load_settings().get("ai_prompt") or "").strip()
    return jsonify({
        "prompt":     saved or DEFAULT_PROMPT,
        "is_default": not saved,
        "default":    DEFAULT_PROMPT,
    })


@bp.route("/api/prompt", methods=["POST"])
def post_prompt():
    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "")
    # Empty string is meaningful → "revert to default".
    save_settings({"ai_prompt": prompt if isinstance(prompt, str) else ""})
    return jsonify({"ok": True})
