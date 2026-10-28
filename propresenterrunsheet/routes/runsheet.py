"""Flask blueprint for /api/runsheet/* endpoints.

These move the cue index, persist state, and let the Service Mate panel
read what the daemon is showing. None of them call OpenRouter or
ProPresenter directly — that's the daemon's job."""

import datetime as _dt

from flask import Blueprint, jsonify, request

from ..service_mate.state import (
    _ensure_item_cues, _read_runsheet_state, _write_runsheet_state,
)
from ..service_mate import state as _sm_state


bp = Blueprint("runsheet", __name__)


# State the app owns and the browser never sends: a POST from the Service
# Mate card rebuilds the state from its own four fields, which silently
# dropped these. Toggling the auto-track checkbox mid-service therefore
# erased the lighting steps already ticked off and the uuid anchors
# auto-track follows by. Carried over whenever the runsheet is the same one.
_CARRIED_KEYS = ("lighting_done", "pp_anchors")


@bp.route("/api/runsheet/state", methods=["GET"])
def api_runsheet_state_get():
    from ..service_mate.pp_track import follow_status
    state = _read_runsheet_state()
    # Whether tracking is following ProPresenter right now — liveness the
    # card shows next to Now/Next, not state, so it is never written back.
    # The state goes in because a manual cue click pauses tracking, and a
    # card that says "Following" straight after Next is worse than silent.
    return jsonify({**(state or {}), "pp_follow": follow_status(state or {})})


@bp.route("/api/runsheet/state", methods=["POST"])
def api_runsheet_state_post():
    """Replace the runsheet state on disk. Body shape:
       { service_name, items, current_index?, auto_track?: {enabled} }
       items is a list of dicts with type/title/notes/duration_min and
       optional `cues: {screen,sound,lights}` and `pp_timer_name`."""
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    # Fill any missing per-role cues from the rule table.
    for it in items:
        if isinstance(it, dict):
            _ensure_item_cues(it)
    state = {
        "service_name":      (body.get("service_name") or "").strip(),
        "items":             items,
        "current_index":     int(body.get("current_index") or 0),
        "current_started_at": _dt.datetime.now().isoformat(),
        "auto_track":        body.get("auto_track")
                             or {"enabled": True},
    }
    # Same runsheet, so what the app knows about it still holds. A different
    # one means the ticks and anchors belong to a service that is over.
    prev = _read_runsheet_state() or {}
    same = ([(it or {}).get("title") for it in (prev.get("items") or [])]
            == [(it or {}).get("title") for it in items])
    if same:
        for key in _CARRIED_KEYS:
            if prev.get(key):
                state[key] = prev[key]
    _write_runsheet_state(state)
    return jsonify({"ok": True, "state": state})


@bp.route("/api/runsheet/cue", methods=["POST"])
def api_runsheet_cue():
    """Move the current-item index. Body: {"delta": +1|-1} or {"index": N}.
    Sets a 10-second manual override window during which auto-track is paused
    so the operator's click isn't immediately overridden."""
    body = request.get_json(silent=True) or {}
    state = _read_runsheet_state()
    if not state or not state.get("items"):
        return jsonify({"error": "no runsheet loaded"}), 400
    items = state["items"]
    cur = int(state.get("current_index") or 0)
    if "index" in body and body.get("index") is not None:
        new = int(body["index"])
    elif "delta" in body:
        new = cur + int(body["delta"])
    else:
        return jsonify({"error": "delta or index required"}), 400
    new = max(0, min(new, len(items) - 1))
    state["current_index"] = new
    state["current_started_at"] = _dt.datetime.now().isoformat()
    state["manual_override_until"] = (
        _dt.datetime.now() + _dt.timedelta(seconds=10)).isoformat()
    state.pop("pp_remaining_seconds", None)
    _write_runsheet_state(state)
    return jsonify({"ok": True, "current_index": new})


@bp.route("/api/runsheet/state", methods=["DELETE"])
def api_runsheet_state_delete():
    import logging
    # Read the file path through the module so monkeypatched tmp paths in
    # tests are honoured (conftest patches _sm_state.RUNSHEET_STATE_FILE).
    log = logging.getLogger("pp_runsheet")
    try:
        if _sm_state.RUNSHEET_STATE_FILE.exists():
            _sm_state.RUNSHEET_STATE_FILE.unlink()
    except Exception:
        log.exception("Failed to delete runsheet_state.json")
    return jsonify({"ok": True})
