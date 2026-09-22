"""Snapshot, verify and roll back an in-place playlist rewrite.

The I/O half of update mode. `playlist_update.py` decides what the
playlist should become; this module makes sure that if ProPresenter
refuses it — or accepts it and quietly loses something — the operator
still has the playlist they walked in with.

The whole module exists because of one asymmetry. Create mode's failure
is "you didn't get a playlist"; update mode's failure is "your playlist
is gone", on a Sunday morning, on work that took an evening to assemble
and that nothing else in the building can reconstruct. So the doctrine
here is inverted from the create route's: there, PP refusing the items
means drop the unlinkable ones and carry on; here, refusing to write is
the SUCCESS case, and every guard aborts with nothing sent rather than
trying to repair its way forward.

The snapshot is written as two files on purpose. The `.json` is for
support and for the restore route. The `.txt` beside it is the ordered
list of item names in plain text, and it is the only artefact a
volunteer can actually act on at 9:40am — ProPresenter cannot import
our JSON, but a person can rebuild a playlist from a list of names."""

import datetime as _dt
import json
import logging
import re
from pathlib import Path

from ..config import DATA_DIR
from .playlist_update import content_fingerprint, verify_content_preserved


log = logging.getLogger("pp_runsheet")

SNAPSHOT_DIR = DATA_DIR / "playlist_backups"

# How many playlist snapshots to keep. Twenty is roughly five months of
# weekly services — long enough that "it was fine two months ago" is
# still answerable, small enough to stay a rounding error on disk.
SNAPSHOT_KEEP = 20

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class UpdateAborted(Exception):
    """A guard refused to write. Carries the operator-facing message.

    `message` is plain English and is shown verbatim — no HTTP status,
    no uuid, no exception text. `reason` is a closed vocabulary for
    logging and telemetry only."""

    def __init__(self, reason: str, message: str, detail=None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail or {}


def _utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def active_playlist_uuid(base: str, http_get=None) -> str:
    """The uuid of the playlist ProPresenter is presenting from, or "".

    Rewriting the contents of the live playlist can move PP's active
    item under the operator's hands mid-service. Shape matches the probe
    in service_mate/pp_track.py, which already parses this endpoint.
    Fails soft: an unreadable answer means "can't tell", and the caller
    treats that as not-active rather than blocking a legitimate
    update."""
    try:
        get = http_get
        if get is None:
            import requests as req
            get = req.get
        r = get(f"{base}/v1/playlist/active", timeout=3)
        if not r.ok:
            return ""
        data = r.json() or {}
        pres = data.get("presentation")
        if not isinstance(pres, dict):
            return ""
        return ((pres.get("playlist") or {}).get("uuid") or "")
    except Exception:
        log.debug("active playlist probe failed", exc_info=True)
        return ""


def snapshot_text(playlist_name: str, items: list) -> str:
    """The human-readable half of a snapshot: the playlist in order, one
    item per line. What a volunteer reads while rebuilding by hand."""
    lines = [f"ProPresenter playlist backup — {playlist_name}",
             f"Taken {_dt.datetime.now().strftime('%d %b %Y, %I:%M %p')}",
             f"{len(items)} items, in order:", ""]
    for i, it in enumerate(items or [], start=1):
        name = ((it.get("id") or {}).get("name") or "").strip() or "(unnamed)"
        kind = (it.get("type") or "item").lower()
        lines.append(f"{i:3}. [{kind}] {name}")
    return "\n".join(lines) + "\n"


def write_snapshot(playlist_uuid: str, playlist_name: str, items: list,
                   dir_path=None) -> Path:
    """Record the playlist exactly as it is, before anything is sent.

    Written with the tmp-file + replace pattern used for settings.json,
    so a crash mid-write cannot leave a truncated backup — which would
    be worse than none, because the operator would trust it.

    `state` starts as "put_in_flight" and is only advanced once the write
    has been read back and verified. A snapshot still reading
    "put_in_flight" is the marker for "we wrote and never confirmed",
    and pruning never removes one."""
    d = Path(dir_path or SNAPSHOT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{_SAFE_NAME.sub('-', playlist_uuid)[:40]}-{_utc_stamp()}"
    path = d / f"{stem}.json"
    payload = {
        "playlist_uuid": playlist_uuid,
        "playlist_name": playlist_name,
        "captured_at":   _dt.datetime.now().isoformat(),
        "state":         "put_in_flight",
        "items":         items,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(path)
    try:
        (d / f"{stem}.txt").write_text(
            snapshot_text(playlist_name, items), encoding="utf-8")
    except Exception:
        log.debug("snapshot .txt write failed (non-fatal)", exc_info=True)
    return path


def mark_snapshot(path, state: str) -> None:
    """Advance a snapshot's state once we know how the write ended."""
    try:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["state"] = state
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        log.debug("snapshot state update failed (non-fatal)", exc_info=True)


def load_snapshot(path) -> dict:
    """Read a snapshot back — for the Undo button and the restore route."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prune_snapshots(keep: int = SNAPSHOT_KEEP, dir_path=None) -> int:
    """Drop the oldest snapshots, never the newest and never an
    unconfirmed one. Returns how many were removed."""
    d = Path(dir_path or SNAPSHOT_DIR)
    if not d.exists():
        return 0
    files = sorted((f for f in d.glob("*.json") if f.is_file()),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    removed = 0
    for f in files[keep:]:
        try:
            if json.loads(f.read_text(encoding="utf-8")
                          ).get("state") == "put_in_flight":
                continue
        except Exception:
            pass
        try:
            f.unlink()
            f.with_suffix(".txt").unlink(missing_ok=True)
            removed += 1
        except Exception:
            log.debug(f"snapshot prune failed for {f.name}", exc_info=True)
    return removed


def rollback(base: str, playlist_uuid: str, snapshot_items: list,
             http_put=None, http_get=None) -> dict:
    """Put the operator's playlist back exactly as it was, then check.

    Exactly ONE attempt, deliberately. A wedged ProPresenter plus a
    retry loop turns one bad playlist into a destroyed one, and the
    honest thing after a failed restore is to stop and hand the operator
    the backup rather than keep hammering.

    Returns `{"attempted": True, "verified": bool, "detail": {...}}`.
    `verified` False is the loud case — the route turns it into the one
    red notice in this app that the operator must not miss."""
    from .templates import fetch_pp_playlist_raw
    out = {"attempted": True, "verified": False, "detail": {}}
    try:
        put = http_put
        if put is None:
            import requests as req
            put = req.put
        put(f"{base}/v1/playlist/{playlist_uuid}",
            json=snapshot_items, timeout=10)
    except Exception:
        log.exception("rollback PUT failed")
        return out
    try:
        back = (http_get or fetch_pp_playlist_raw)(base, playlist_uuid)
    except Exception:
        log.exception("rollback read-back failed")
        return out
    if back is None:
        return out
    check = verify_content_preserved(snapshot_items, back)
    out["verified"] = bool(check.get("ok"))
    out["detail"] = check
    return out


def fingerprint(items: list) -> list:
    """A comparable summary of a playlist's content, for the
    changed-underneath-us guard between preview and write."""
    return [list(t) for t in content_fingerprint(items)]
