#!/usr/bin/env python3
"""Runsheet Pilot — entry point.

The actual app lives in the `propresenterrunsheet/` package. This file
stays as the entry point so the launchers (launch_mac.sh / run.bat) and
the PyInstaller specs in build_mac.sh / build_win.bat don't need
updating; it also keeps the Flask app object next to the
templates/ + static/ folders so Flask finds them automatically (both
in dev and inside a frozen .app/.exe).

Where the code lives now (after the four-phase refactor):
  templates/                          — index.html
  static/                             — app.css, app.js
  propresenterrunsheet/
    config.py                         — VERSION, APP_NAME, DATA_DIR, paths
    logging_setup.py                  — setup_logging(), `log`
    settings.py                       — load_settings, save_settings
    server.py                         — main(), _serve, port discovery
    parsing/                          — pdf, ai prompt, time/duration regex
    propresenter/                     — PP filesystem + REST API + payload
    service_mate/                     — GeekMagic clock subsystem
    routes/                           — Flask blueprints, one per topic

Common feature touch-points:
  - new API endpoint    → propresenterrunsheet/routes/<topic>.py
                          + JS caller in static/app.js
  - new UI panel        → templates/index.html + static/app.js
  - new settings field  → propresenterrunsheet/settings.py
                          + UI in templates/index.html
  - new runsheet type   → DEFAULT_PROMPT in parsing/ai.py,
                          TYPE_COLORS in propresenter/playlist.py,
                          tagClass()/CSS in static/app.{js,css},
                          *_CUES rule tables in service_mate/constants.py
  - clock layout tweak  → service_mate/render.py
"""

import os
import sys

# When invoked as a script (`python3 propresenter_app.py`), Python registers
# this file as `__main__` instead of `propresenter_app`. Some submodule
# imports — and tests via the conftest fixtures — reference
# `propresenter_app.<X>`, so without this alias Python would load this file
# a second time and trip a circular-import error during package init.
if __name__ == "__main__" and "propresenter_app" not in sys.modules:
    sys.modules["propresenter_app"] = sys.modules["__main__"]

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

# Importing any submodule runs `propresenterrunsheet/__init__.py` first,
# and that is the side effect everything below depends on: it configures
# logging and loads the propresenter / service_mate / parsing sub-packages
# before we touch them. So this first import doubles as the package
# bootstrap — keep it above anything else from the package.
from propresenterrunsheet.logging_setup import log
from propresenterrunsheet.routes import register_blueprints


# ── Flask app ─────────────────────────────────────────────────────────────────
# In a normal (source) run, Flask auto-discovers templates/ + static/ next to
# this file. In the FROZEN bundle we point Flask explicitly at the folders
# PyInstaller extracts into sys._MEIPASS. Flask's auto-detection (via
# __main__.__file__) is unreliable in a --onefile --windowed exe — especially
# when the process is launched by the self-updater rather than by double-click
# — which surfaced as "TemplateNotFound: index.html" right after an update.
# Explicit _MEIPASS paths are the documented, bulletproof fix.
if getattr(sys, "frozen", False):
    _bundle = sys._MEIPASS  # PyInstaller sets this on the frozen bundle
    app = Flask(__name__,
                template_folder=os.path.join(_bundle, "templates"),
                static_folder=os.path.join(_bundle, "static"))
else:
    app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB cap on PDF upload
# Auto-reload templates when their files change on disk. Flask defaults
# this to app.debug (False here), so without this any markup edit would
# only show up after restarting the whole app. The mtime check per
# render adds microseconds — meaningless for a local app.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Report any unhandled exception raised inside a request. Flask turns
# these into a 500 for the browser; without this hook they would never
# be seen by anyone. The handler only ever sends the exception TYPE,
# scrubbed message and frame basenames — see propresenterrunsheet/stats.py.
try:
    from flask import got_request_exception as _got_request_exception
    from propresenterrunsheet import stats as _stats

    def _report_request_exception(sender, exception, **_extra):
        try:
            from flask import request as _rq
            _stats.report_error(exception, where_kind="request",
                                route=(_rq.endpoint or "unknown"))
        except Exception:
            # Reporting must never raise inside Flask's error path — that
            # would bury the real exception under this one.
            log.debug("Couldn't report a request exception", exc_info=True)

    _got_request_exception.connect(_report_request_exception, app)
except Exception:
    # Crash reporting is an extra, not a requirement: if the signal isn't
    # available the app still starts, and _unhandled below still logs
    # every crash to app.log.
    log.debug("Crash reporting hook not installed", exc_info=True)

# A host outside loopback/LAN is a refusal, not a crash: pp_base raises
# and this turns it into the same shape every other route error uses, so
# no caller has to repeat the check. HTTP 200 because the JS reads the
# message out of the body.
try:
    from flask import jsonify as _jsonify
    from propresenterrunsheet.propresenter.net import UnreachableHost

    @app.errorhandler(UnreachableHost)
    def _unreachable_host(exc):
        return _jsonify({"ok": False, "error": str(exc)}), 200
except Exception:
    # Registering the handler is an improvement, not a requirement — if
    # Flask's API shifts, the exception still surfaces as a 500 rather
    # than the app failing to start.
    pass


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": "PDF too large (limit 25 MB)."}), 413


@app.errorhandler(Exception)
def _unhandled(e):
    # Flask routes werkzeug HTTPExceptions (404, 405, …) through this
    # catch-all too, because it's registered on the base Exception class.
    # Let those keep their own status code instead of being logged as a
    # crash and masked as a 500 — otherwise the browser's automatic
    # /favicon.ico probe fills the log with scary tracebacks on every
    # launch and every missing URL "fails" as a 500.
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled exception in request")
    return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# Wire every route on the package onto the app
register_blueprints(app)


# ── Re-export shim for tests + CI ─────────────────────────────────────────────
# `tests/conftest.py` and `tests/test_*.py` poke a couple of dozen helpers as
# `app_module.<name>`, and CI's smoke step reads `propresenter_app.VERSION`.
# Re-exporting them here lets those stay unchanged even though the source of
# truth has moved into the package.
#
# The list is exactly what something reads through this module; everything
# else was trimmed. `__all__` below is what marks these as deliberate
# re-exports rather than unused imports — a name added here must be added
# there too.

from propresenterrunsheet.config import VERSION  # noqa: E402
from propresenterrunsheet.parsing import (  # noqa: E402
    DEFAULT_PROMPT, _extract_duration_min, _extract_time_str,
    parse_ai_response,
)
from propresenterrunsheet.propresenter import (  # noqa: E402
    _norm, auto_detect_template_uuid, build_playlist_payload, fuzzy_match,
    playlist_to_sections, resolve_library_name, resolve_section,
)
from propresenterrunsheet.service_mate import (  # noqa: E402
    _CLOCKS_LOOP_LAST_PUSHED, _CLOCK_THEME_SET,
    _clean_header_name, _compute_remaining_seconds, _cue_for,
    _ensure_item_cues, _format_mmss, _maybe_advance_from_pp,
    _next_visible_item, _parse_pp_time, _pp_active_section_index,
    _render_cue, _render_standby, _render_test_card,
    _sm_font, _text_width,
)
from propresenterrunsheet.server import main  # noqa: E402

__all__ = [
    "app",
    # config
    "VERSION",
    # parsing
    "DEFAULT_PROMPT", "_extract_duration_min", "_extract_time_str",
    "parse_ai_response",
    # propresenter
    "_norm", "auto_detect_template_uuid", "build_playlist_payload",
    "fuzzy_match", "playlist_to_sections", "resolve_library_name",
    "resolve_section",
    # service_mate
    "_CLOCKS_LOOP_LAST_PUSHED", "_CLOCK_THEME_SET",
    "_clean_header_name", "_compute_remaining_seconds", "_cue_for",
    "_ensure_item_cues", "_format_mmss", "_maybe_advance_from_pp",
    "_next_visible_item", "_parse_pp_time", "_pp_active_section_index",
    "_render_cue", "_render_standby", "_render_test_card",
    "_sm_font", "_text_width",
]


if __name__ == "__main__":
    # Pass `app` explicitly so `main()` doesn't have to re-import this
    # module via `from propresenter_app import app`. In a PyInstaller
    # bundle that re-import has occasionally surfaced as "routes 404
    # even though they registered" — Python ends up holding two copies
    # of the propresenter_app module (one as __main__ from the bootloader,
    # one freshly loaded via the import). Threading the live `app` object
    # straight through closes that whole class of bug.
    main(app)
