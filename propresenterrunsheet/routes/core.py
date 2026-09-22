"""Core routes — index page, health check, quit.

These don't fit in any of the topic-specific blueprints — they're the
machinery that makes the app feel like an app. `/` returns the rendered
index template (HTML/CSS/JS lives in templates/ + static/). `/api/quit`
exits the process so the UI's Quit button works."""

import logging
import os
import sys
import threading
import time
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, render_template, request

from ..config import VERSION


bp = Blueprint("core", __name__)
log = logging.getLogger("pp_runsheet")

# The hostnames this app's own page is ever served under: the native
# window loads http://127.0.0.1:<port>, the browser fallback
# http://localhost:<port> (see server.py).
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(netloc: str) -> str:
    """'localhost:5757' → 'localhost', '[::1]:80' → '::1', junk → ''."""
    try:
        return (urlsplit(f"//{netloc}").hostname or "").lower()
    except ValueError:
        return ""


@bp.before_app_request
def _only_this_apps_own_page():
    """Refuse any request that did not come from this app's own page.

    The server binds to 127.0.0.1, so nothing on the LAN can reach it —
    but a web page open in a browser on the SAME machine can, through DNS
    rebinding: a site points its own hostname at 127.0.0.1, and the
    browser then treats this API as same-origin with it. Every route here
    trusts its caller — folders to scan and export into, a quit endpoint,
    writes to the operator's ProPresenter — so the caller has to be this
    app. (This is also why the CodeQL path alerts on the operator-chosen
    library and export folders are by design: the chooser is the
    operator, and this check is what guarantees it.)

    Host is the header rebinding cannot fake: the browser still sends the
    attacker's hostname. Origin covers a cross-site form post. A missing
    Origin is allowed (same-origin GETs and non-browser tools send none);
    "null" is not — a sandboxed frame sends that, never this app.

    Sec-Fetch-Site closes the gap Origin leaves: a page elsewhere can
    fire a plain GET at 127.0.0.1 from an <img> or <script> tag, which
    carries no Origin at all, and several routes act on GET query strings
    (/api/library/auto scans the ?dir= it is given). Every current browser
    engine, including the WebKit and Chromium webviews this app runs in,
    sends it on every request: "same-origin" from this app's own page,
    "none" for the window's first load, "cross-site" from anywhere else.
    Tools like curl and the test client send none, so an absent header
    stays allowed."""
    if _hostname(request.host) not in _LOCAL_HOSTS:
        log.warning("Refused a request whose Host is not this machine")
        return jsonify({"error": "Not allowed."}), 403
    origin = request.headers.get("Origin")
    if origin is not None and _hostname(urlsplit(origin).netloc) not in _LOCAL_HOSTS:
        log.warning("Refused a cross-origin request")
        return jsonify({"error": "Not allowed."}), 403
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        log.warning("Refused a request from another site")
        return jsonify({"error": "Not allowed."}), 403
    return None


@bp.route("/")
def index():
    # The HTML / CSS / JS for the UI lives in templates/index.html and
    # static/app.{css,js} so editors give us syntax highlighting and the
    # browser can cache the static assets. PyInstaller bundles these via
    # --add-data flags in build_mac.sh / build_win.bat.
    return render_template("index.html")


@bp.route("/api/health")
def api_health():
    return jsonify({"ok": True, "version": VERSION, "platform": sys.platform})


@bp.route("/api/quit", methods=["POST"])
def api_quit():
    log.info("Quit requested via UI")
    def _bye():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({"ok": True})
