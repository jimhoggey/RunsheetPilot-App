"""Extra native windows, when the app runs in its own window (pywebview).

server.py sets `webview` once the main window is open. Without it (the
browser fallback, `--headless`, tests) every function here says so by
returning False, and the page opens a browser popup instead.

pywebview creates windows from any thread once its loop is running, so a
Flask route can open one directly.
"""
import logging

log = logging.getLogger("pp_runsheet")

webview = None            # the pywebview module, set by server.py
_lighting = None          # the open lighting heads-up window, if any


def _forget_lighting():
    global _lighting
    _lighting = None
    # Closed with its own button: switch the setting off, so the toggle in
    # the main window follows (its Service Mate poll reads it back).
    try:
        from .service_mate.state import _read_clocks_config, _write_clocks_config
        cfg = _read_clocks_config()
        cfg["lighting_window"] = False
        _write_clocks_config(cfg)
    except Exception:
        log.exception("Couldn't record the lighting window as closed")


def open_lighting_window(url: str) -> bool:
    """Open (or keep) the always-on-top lighting heads-up. False when there
    is no native window toolkit to open it with."""
    global _lighting
    if webview is None:
        return False
    if _lighting is None:
        _lighting = webview.create_window(
            "Lighting — coming up", url, width=380, height=180,
            min_size=(260, 140), on_top=True,
            background_color="#111118")   # the card's own, so it doesn't flash white
        _lighting.events.closed += _forget_lighting
    return True


def close_lighting_window() -> bool:
    global _lighting
    if webview is None:
        return False
    win, _lighting = _lighting, None
    if win is not None:
        win.events.closed -= _forget_lighting   # we're the ones closing it
        win.destroy()
    return True
