"""Flask blueprint for /api/clocks/* endpoints.

Configure clock IPs / brightness / verbosity, probe + test individual
devices, render an inline preview, and reset everything to standby."""

import datetime as _dt

from flask import Blueprint, Response, jsonify, redirect, render_template, request

from ..native import close_mate_window, open_mate_window
from ..parsing.ai import LIGHTING_GUIDE_MAX_CHARS
from ..service_mate.lighting import current_index, done_for, heads_up, lights_view
from ..service_mate.mate import mate_config, mate_view, window_size
from ..service_mate.pp_track import PP_REACHABLE
from ..settings import load_settings, save_settings

from ..service_mate.constants import (
    ROLE_ACCENT, SM_TESTCARD_FILENAME, SM_VERBOSITIES, SM_VERBOSITY_DEFAULT,
)
from ..service_mate.daemon import _CLOCKS_LOOP_LAST_PUSHED, _ENDS_AT
from ..service_mate.geekmagic import _push_to_clock, _set_clock_brightness
from ..service_mate.render import _render_cue, _render_standby, _render_test_card
from ..service_mate.state import (
    _read_clocks_config, _read_runsheet_state, _write_clocks_config,
    _write_runsheet_state,
)


bp = Blueprint("clocks", __name__)


# Standard response when the operator has the Service Mate master switch
# OFF. GET/POST /api/clocks (read + update config) stay open so the UI
# can read state + flip the switch back on; everything that actually
# touches hardware or renders a cue returns 409 so the off-state is
# obvious to anyone hitting the API directly (curl, scripts, …).
_DISABLED_RESPONSE = ({"ok": False,
                       "error": "Service Mate is disabled in settings. "
                                "Flip the master switch on the Clocks card "
                                "to enable."},
                      409)


# Returned when the paid Service Mate trial has ended and no licence key is
# active. 402 Payment Required is the semantically-correct status. The UI
# reads license_state to switch the card into its "locked" overlay.
_UNLICENSED_RESPONSE = ({"ok": False,
                         "error": "Your Service Mate free trial has ended. "
                                  "Enter a licence key in Settings to keep "
                                  "pushing cues to your clocks.",
                         "license_state": "expired"},
                        402)


def _check_sm_enabled(cfg: dict):
    """Return (json_body, status_code) when Service Mate may NOT run, else
    None to indicate the caller should proceed. Tiny helper so each
    hardware-touching route can early-return with one line.

    Two gates, in order:
      1. Licence/trial — blocks (402) when the 14-day trial has expired and
         no valid licence key is present. See propresenterrunsheet/licensing.
      2. Master switch — blocks (409) when the operator has it toggled off.
    """
    from ..licensing import service_mate_allowed
    if not service_mate_allowed():
        return _UNLICENSED_RESPONSE
    if not cfg.get("enabled"):
        return _DISABLED_RESPONSE
    return None


@bp.route("/api/clocks", methods=["GET"])
def api_clocks_get():
    cfg = _read_clocks_config()
    # The desk settings as they apply, old lighting switch included.
    return jsonify({**cfg, "mate": mate_config(cfg)})


@bp.route("/api/clocks", methods=["POST"])
def api_clocks_post():
    body = request.get_json(silent=True) or {}
    cfg = _read_clocks_config()
    if "clocks" in body and isinstance(body["clocks"], list):
        cleaned = []
        for c in body["clocks"]:
            if not isinstance(c, dict):
                continue
            verbosity = (c.get("verbosity") or SM_VERBOSITY_DEFAULT).strip().lower()
            if verbosity not in SM_VERBOSITIES:
                verbosity = SM_VERBOSITY_DEFAULT
            cleaned.append({
                "id":        (c.get("id") or c.get("role") or "").strip().lower(),
                "ip":        (c.get("ip") or "").strip(),
                "role":      (c.get("role") or c.get("id") or "screen").strip().lower(),
                "name":      (c.get("name") or "").strip(),
                "verbosity": verbosity,
            })
        cfg["clocks"] = cleaned
    if "brightness" in body:
        try:
            cfg["brightness"] = max(1, min(100, int(body["brightness"])))
        except Exception:
            # Not a number (a blank field, say): keep the saved brightness
            # rather than failing the rest of this save.
            pass
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
        # Turning the master switch on counts as "first active use" — start
        # the 14-day trial clock now (no-op if already licensed/started).
        if cfg["enabled"]:
            from ..licensing import start_trial_if_needed
            start_trial_if_needed()
        else:
            # The on-screen Service Mate goes too, rather than staying on
            # top with a frozen view.
            cfg["mate"] = {**mate_config(cfg), "on": False}
            cfg.pop("lighting_window", None)
            close_mate_window()
    _write_clocks_config(cfg)
    return jsonify({"ok": True, "config": cfg})


@bp.route("/api/clocks/<clock_id>/probe", methods=["POST"])
def api_clock_probe(clock_id: str):
    cfg = _read_clocks_config()
    disabled = _check_sm_enabled(cfg)
    if disabled:
        return jsonify(disabled[0]), disabled[1]
    clock = next((c for c in cfg.get("clocks", [])
                  if c.get("id") == clock_id), None)
    if not clock:
        return jsonify({"error": "unknown clock"}), 404
    ip = (clock.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "no IP set"}), 200
    # Report WHICH firmware is there, not just that something answered. This
    # is the whole device-type story in the UI: a read-out that cannot be set
    # wrong, rather than a toggle that can.
    from ..service_mate.geekmagic import _identify_clock
    return jsonify(_identify_clock(ip))


@bp.route("/api/clocks/<clock_id>/test", methods=["POST"])
def api_clock_test(clock_id: str):
    cfg = _read_clocks_config()
    disabled = _check_sm_enabled(cfg)
    if disabled:
        return jsonify(disabled[0]), disabled[1]
    clock = next((c for c in cfg.get("clocks", [])
                  if c.get("id") == clock_id), None)
    if not clock:
        return jsonify({"error": "unknown clock"}), 404
    ip = (clock.get("ip") or "").strip()
    if not ip:
        return jsonify({"ok": False, "error": "no IP set"}), 200
    role = clock.get("role") or "screen"
    from ..service_mate.geekmagic import _probe_custom, _push_test_state
    if _probe_custom(ip):
        # Custom firmware has no image-upload route, so the JPEG test card
        # would simply fail. Send a state payload it can actually render.
        ok = _push_test_state(ip, role)
        _CLOCKS_LOOP_LAST_PUSHED.pop(clock_id, None)
        return jsonify({"ok": ok})
    jpg = _render_test_card(role, ip)
    if cfg.get("brightness"):
        _set_clock_brightness(ip, int(cfg["brightness"]))
    ok = _push_to_clock(ip, jpg, filename=SM_TESTCARD_FILENAME)
    # Test card and live cue are different files on the device — clearing the
    # last-pushed hash for this clock means the next loop tick re-pushes the
    # cue image, returning the device to the live view within ~1 s. Otherwise
    # the test card would stay until the cue content next changed.
    _CLOCKS_LOOP_LAST_PUSHED.pop(clock_id, None)
    return jsonify({"ok": ok})


@bp.route("/api/clocks/standby", methods=["POST"])
def api_clocks_standby():
    """Reset all clocks to the pre-service waiting page. Persists `standby:true`
    in the runsheet state so the daemon keeps pushing the standby image; the
    flag is cleared automatically on the next runsheet load (parse / create
    playlist / explicit POST /api/runsheet/state with items)."""
    disabled = _check_sm_enabled(_read_clocks_config())
    if disabled:
        return jsonify(disabled[0]), disabled[1]
    state = {
        "standby": True,
        "items": [],
        "current_index": 0,
        "current_started_at": _dt.datetime.now().isoformat(),
    }
    _write_runsheet_state(state)
    # Force every clock to re-push on the next loop tick (~500 ms) instead of
    # waiting for the next content change. Without this, clocks that are
    # already showing the standby image (e.g. after a server restart) wouldn't
    # tick refresh until the 40 s anti-bitrot push.
    _CLOCKS_LOOP_LAST_PUSHED.clear()
    return jsonify({"ok": True})


@bp.route("/api/clocks/preview", methods=["GET"])
def api_clocks_preview():
    """Return the rendered JPEG for a given role + verbosity — used by the UI
    for an inline preview without the device, and for development."""
    disabled = _check_sm_enabled(_read_clocks_config())
    if disabled:
        return jsonify(disabled[0]), disabled[1]
    role = (request.args.get("role") or "screen").lower()
    if role not in ROLE_ACCENT:
        role = "screen"
    verbosity = (request.args.get("verbosity") or SM_VERBOSITY_DEFAULT).lower()
    if verbosity not in SM_VERBOSITIES:
        verbosity = SM_VERBOSITY_DEFAULT
    state = _read_runsheet_state() or {}
    # If the operator hit Standby (or saved an explicit standby flag), preview
    # mirrors what the device is actually showing right now.
    if state.get("standby"):
        return Response(_render_standby(role), mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    # No runsheet at all → fall through to demo data so first-time users can
    # see what a live cue will look like before loading a PDF.
    if not state.get("items"):
        state = {
            "items": [
                {"type": "song", "title": "Build My Life", "duration_min": 5,
                 "notes": "9:30 AM",
                 "cues": {"screen": "Cue song slides",
                          "sound":  "Band mics live · MC mute",
                          "lights": "Stage wash — band"}},
                {"type": "sermon", "title": "King Jesus — Ps David", "duration_min": 30,
                 "notes": "10:14 AM",
                 "cues": {"screen": "Sermon slides",
                          "sound":  "Mic on for Ps David",
                          "lights": "Spot — preacher"}}
            ],
            "current_index": 0,
            "current_started_at": _dt.datetime.now().isoformat(),
        }
    # The Lights station previews what its clock really shows: the heads-up.
    jpg = _render_cue(role, lights_view(state) if role == "lights" else state,
                      verbosity=verbosity)
    return Response(jpg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ─── Lighting heads-up (service_mate/lighting.py) ─────────────────────────

@bp.route("/lighting")
def lighting_page():
    """The lighting card grew into the on-screen Service Mate."""
    return redirect("/mate")


# ─── The on-screen Service Mate (service_mate/mate.py) ────────────────────

@bp.route("/mate")
def mate_page():
    """The always-on-top window: what this desk's clocks would show."""
    return render_template("mate.html")


@bp.route("/api/mate", methods=["GET"])
def api_mate():
    # Service Mate off or its trial over (the window might be open in a
    # browser popup): say so, rather than keep showing a frozen view.
    cfg = _read_clocks_config()
    if _check_sm_enabled(cfg):
        return jsonify({"state": "off"})
    desk = mate_config(cfg)
    state = _read_runsheet_state() or {}
    # The deadline the loop holds for the clocks — read, never resolved
    # here, so the window can't nudge the clocks' countdown.
    view = mate_view(state, desk["stations"], desk["show"], _ENDS_AT.peek(state),
                     _dt.datetime.now())
    auto = state.get("auto_track")
    tracking = isinstance(auto, dict) and bool(auto.get("enabled"))
    return jsonify({**view, "pp_ok": PP_REACHABLE["ok"] or not tracking})


@bp.route("/api/lighting/done", methods=["POST"])
def api_lighting_done():
    """Tick the next change in the live section off. The app can't hear where
    the band is; the operator can.

    A tick names the step the card showed ({index, step}) and is refused
    unless that is still the next change, so a double-click or a card a
    second out of date can't silently skip a cue. Undo names the tick count
    the card showed ({undo: true, done}) and goes back to the change before,
    past any repeats the tick skipped, in one click."""
    cfg = _read_clocks_config()
    blocked = _check_sm_enabled(cfg)
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    state = _read_runsheet_state() or {}
    items = state.get("items") or []
    if not items:
        return jsonify({"ok": False}), 409
    idx = current_index(state, items)
    count = done_for(state, idx)
    nxt = heads_up(items, idx, count)["next"]
    if body.get("undo"):
        if count == 0 or body.get("done") != count:
            return jsonify({"ok": False}), 409
        count = next((k for k in range(count - 1, -1, -1)
                      if heads_up(items, idx, k)["next"] != nxt), 0)
    else:
        if not (nxt and nxt["here"]) or [body.get("index"), body.get("step")] != [nxt["index"], nxt["step"]]:
            return jsonify({"ok": False}), 409     # not what the card showed any more
        count = nxt["step"] + 1
    state["lighting_done"] = {"index": idx, "count": count}
    _write_runsheet_state(state)
    return jsonify({"ok": True})


@bp.route("/api/lighting/guide", methods=["GET", "POST"])
def api_lighting_guide():
    """The church's lighting guide, read into every parse (parsing/ai.py).

    POST a PDF or picture as `file`: it is read, then tidied into a numbered
    list of moments by one model call (parsing/guide.py) — kept as read when
    that can't run, and `tidied` says why: "ok", "no_key" or "failed". JSON
    {"text": ...} saves the operator's own text as is (their corrections);
    {"clear": true} removes it."""
    tidied = None
    if request.method == "POST":
        blocked = _check_sm_enabled(_read_clocks_config())
        if blocked:
            return blocked
        upload = request.files.get("file")
        body = {} if upload else (request.get_json(silent=True) or {})
        if upload is not None:
            from ..parsing.guide import tidy_guide
            from .parse import _extracted_or_error
            text, _source, error = _extracted_or_error(upload)
            if error:
                return jsonify({"ok": False, "error": error}), 400
            settings = load_settings()
            key = str(settings.get("or_key") or "")
            tidy = tidy_guide(text[:LIGHTING_GUIDE_MAX_CHARS], key, str(settings.get("or_model") or ""))
            tidied = "ok" if tidy else "failed" if key else "no_key"
            text = tidy or text
        else:
            text = "" if body.get("clear") else str(body.get("text") or "")
        save_settings({"lighting_guide": text.strip()[:LIGHTING_GUIDE_MAX_CHARS]})
    guide = str(load_settings().get("lighting_guide") or "")
    return jsonify({"ok": True, "has_guide": bool(guide), "text": guide,
                    "moments": sum(1 for ln in guide.splitlines() if ln.strip()[:1].isdigit()),
                    "tidied": tidied})


@bp.route("/api/mate/window", methods=["POST"])
def api_mate_window():
    """Show or hide the on-screen Service Mate, and save which stations and
    sections it shows ({on, stations?, show?}). `native: false` tells the
    page there is no native window here. `popup: true` means the page has
    opened a browser popup itself, so no native one is opened as well."""
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    on = bool(body.get("on"))
    cfg = _read_clocks_config()
    if on:
        blocked = _check_sm_enabled(cfg)
        if blocked:
            return blocked
    picked = {k: body[k] for k in ("stations", "show") if k in body}
    desk = mate_config({"mate": {**mate_config(cfg), **picked, "on": on}})
    cfg["mate"] = desk
    cfg.pop("lighting_window", None)
    _write_clocks_config(cfg)
    if not on:
        native = close_mate_window()
    elif body.get("popup"):
        native = False
    else:
        native = open_mate_window(request.host_url + "mate", *window_size(desk["stations"]))
    return jsonify({"ok": True, "on": on, "native": native, "mate": desk})
