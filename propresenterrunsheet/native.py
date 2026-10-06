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
_mate = None              # the open digital Service Mate window, if any
_mate_height = None       # the height it was last fitted to


def _forget_mate():
    global _mate
    _mate = None
    # Closed with its own button: switch the setting off, so the toggle in
    # the main window follows (its Service Mate poll reads it back). Logged,
    # not raised: this runs inside pywebview's event dispatch.
    try:
        from .service_mate.mate import mate_config
        from .service_mate.state import _read_clocks_config, _write_clocks_config
        cfg = _read_clocks_config()
        cfg["mate"] = {**mate_config(cfg), "on": False}
        cfg.pop("lighting_window", None)
        _write_clocks_config(cfg)
    except Exception:
        log.exception("Couldn't record the Service Mate window as closed")


def open_mate_window(url: str, width: int, height: int, min_height: int) -> bool:
    """Open (or keep) the always-on-top digital Service Mate, fitted to the
    stations it shows. False when there is no native window toolkit."""
    global _mate, _mate_height
    if webview is None:
        return False
    if _mate is None:
        _mate = webview.create_window(
            "Service Mate", url, width=width, height=height,
            min_size=(width, min_height), on_top=True,
            background_color="#111118")   # the card's own, so it doesn't flash white
        _mate.events.closed += _forget_mate
    elif height != _mate_height:
        # A station ticked or unticked while open: refit, keeping the width
        # the operator chose. A Show box doesn't change the height, so it
        # never undoes their resizing. Cosmetic: a failure is only logged.
        _resize(_mate.width, height)
    _mate_height = height
    return True


def _resize(width: int, height: int) -> None:
    global _mate_height
    try:
        _mate.resize(width, height)
        _mate_height = height
    except Exception:
        log.exception("Couldn't resize the Service Mate window")


def size_mate_window(width: int, height: int) -> bool:
    """A− / A+ size the whole view, so the window grows or shrinks with it,
    width included. False when there is no native window to size (a browser
    popup, or the toolkit is missing) — the size still saves."""
    if webview is None or _mate is None:
        return False
    _resize(width, height)
    return True


def close_mate_window() -> bool:
    global _mate
    if webview is None:
        return False
    win, _mate = _mate, None
    if win is not None:
        win.events.closed -= _forget_mate   # we're the ones closing it
        win.destroy()
    return True
