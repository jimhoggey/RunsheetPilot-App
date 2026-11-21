"""ProPresenter library route.

/api/library/auto tries PP's REST API first, then falls back to reading
.pro files in the library folder saved in Settings. Called silently by
the UI on launch and before each parse, so the operator never has to
touch the library settings."""

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..logging_setup import log_safe
from ..propresenter.library import scan_library
from ..settings import load_settings


bp = Blueprint("library", __name__)
log = logging.getLogger("pp_runsheet")


def _fetch_library_via_api(host: str, port: str):
    """Used by /api/library/auto. Returns the
    library items list on success, None when PP is unreachable, or
    raises for unexpected errors. Picks the FIRST library returned by
    /v1/libraries — operators with multiple libraries can still point
    Settings at a specific library folder and choose disk mode."""
    import requests as req
    from ..propresenter.net import pp_base
    # Built OUTSIDE the try: host/port can come from the request, so an
    # unvalidated URL here would be an SSRF sink. pp_base validates and pins to the
    # vetted address, and UnreachableHost propagates to the app-level
    # handler as a plain refusal.
    base = pp_base(host, port)
    try:
        r = req.get(f"{base}/v1/libraries", timeout=6)
        r.raise_for_status()
        libs = r.json()
        if isinstance(libs, dict):
            lib_id = next(iter(libs))
            v = libs[lib_id]
            lib_id = v.get("uuid") or v.get("name") or lib_id
        else:
            lib_id = libs[0].get("uuid") or libs[0].get("name")
        r2 = req.get(f"{base}/v1/library/{lib_id}", timeout=12)
        r2.raise_for_status()
        return r2.json().get("items", [])
    except req.exceptions.ConnectionError:
        return None
    except Exception:
        log.exception("Library API fetch failed")
        return None


@bp.route("/api/library/auto", methods=["GET"])
def api_library_auto():
    """Best-effort library load — used by the UI on launch + before parse
    so the operator doesn't have to manually scan / fetch.

    Order of attempts:
      1. PP REST API (live, accurate, requires PP running).
      2. Local disk scan of `library_dir` from settings.
      3. Empty list with source="none" so the UI can show "no library".

    The disk scan only ever reads settings.library_dir: a request can't
    point it at another folder (the UI saves a just-typed folder first).
    Query params override settings:
      ?host=  / ?port=     PP REST host/port (defaults to settings)
      ?mode=auto|api|disk  which source(s) to try (defaults to
                           settings.lib_source, falling back to "auto").
                           - auto: try API, fall back to disk on failure
                           - api:  API only (skip disk fallback)
                           - disk: disk only (skip API)

    Response shape:
      {"items": [...], "source": "api"|"disk"|"none", "count": N}
    The `source` field lets the UI surface where the library came from
    (e.g. "423 songs · loaded from PP" vs "423 songs · loaded from disk")."""
    settings = load_settings()
    host = (request.args.get("host") or settings.get("pp_host")
            or "localhost").strip()
    port = (request.args.get("port") or settings.get("pp_port")
            or "50001").strip()
    disk_dir = (settings.get("library_dir") or "").strip()
    mode = (request.args.get("mode") or settings.get("lib_source")
            or "auto").strip().lower()
    if mode not in ("auto", "api", "disk"):
        mode = "auto"

    # 1. Try PP REST (unless mode locks to disk only).
    if mode in ("auto", "api"):
        items = _fetch_library_via_api(host, port)
        if items:
            log.info(f"Library auto: {len(items)} items from PP API "
                     f"({log_safe(host)}:{log_safe(port)}, mode={mode})")
            return jsonify({"items": items, "source": "api",
                            "count": len(items)})
        if mode == "api":
            # API-only mode and PP didn't answer — don't fall back to disk.
            log.info(f"Library auto: PP unreachable in api-only mode "
                     f"({log_safe(host)}:{log_safe(port)})")
            return jsonify({"items": [], "source": "none", "count": 0})

    # 2. Try disk scan (unless mode locks to api only — already returned above).
    if mode in ("auto", "disk") and disk_dir and Path(disk_dir).exists():
        try:
            items = scan_library(disk_dir)
            log.info(f"Library auto: {len(items)} items from disk "
                     f"({log_safe(disk_dir)}, mode={mode})")
            return jsonify({"items": items, "source": "disk",
                            "count": len(items)})
        except Exception:
            log.exception(f"Library auto: disk scan of {log_safe(disk_dir)} "
                          "failed")

    # 3. Nothing worked — UI will show "no library, open Settings".
    log.info(f"Library auto: no source available (mode={mode})")
    return jsonify({"items": [], "source": "none", "count": 0})
