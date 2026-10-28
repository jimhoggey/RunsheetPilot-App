"""Remember which ProPresenter playlist item stands for which runsheet item.

Auto-tracking used to work the only way it could: every poll it walked
ProPresenter's active playlist back to the nearest header, stripped the
decorations off that header's name, and fuzzy-matched the remainder
against the runsheet titles at a 0.6 threshold. That guess has to be
re-made every half-second, and anything that moves the name — the
operator tidying a header, a template's own wording, update mode's ↕ —
drops it under the threshold. Tracking then goes quiet, the clocks sit
still, and the operator reaches for Next.

The names are only ambiguous later. At the moment Runsheet Pilot writes
the playlist it knows exactly which entry it wrote for which runsheet
item, so this module reads the playlist straight back and records the
uuid ProPresenter assigned to each one. From then on tracking is a
dict lookup on an identity neither side can drift from: rename a header,
recolour it, drag it up the playlist — the uuid is the same, and the
Service Mate still knows which segment the operator is in.

The map is a bonus, never a requirement. Every function here returns
empty on any failure and `pp_track` falls back to the name matching it
has always done, which is also what still carries a playlist Runsheet
Pilot did not build.
"""

import logging

from .playlist import build_playlist_payload
from .playlist_update import UNPLACED_MARK

log = logging.getLogger("pp_runsheet")


def anchor_labels(matched: list) -> dict:
    """{the name PP will carry: runsheet item index} for the one entry that
    stands for each runsheet item.

    Derived by running `build_playlist_payload` over each item on its own,
    so it cannot drift from what the real payload emits — a song that
    matched a .pro is anchored on its presentation, everything else on its
    coloured header, and that stays true the next time the payload rules
    change.

    Each name is registered with update mode's "couldn't place this one"
    marker as well as without it: a placed header is byte-identical to
    create mode's, and a marked one is the same label behind a ↕ — which
    is precisely the header the old name matching could not read.

    A name two runsheet items share is dropped rather than guessed at: an
    anchor pointing at the wrong segment is worse than no anchor, which
    only costs the fallback.
    """
    out, seen = {}, set()
    for i, mi in enumerate(matched or []):
        entries = build_playlist_payload([mi])
        if not entries:
            continue
        name = ((entries[0].get("id") or {}).get("name") or "").strip()
        if not name:
            continue
        if name in seen:
            out.pop(name, None)
            out.pop(UNPLACED_MARK + name, None)
            continue
        seen.add(name)
        out[name] = i
        out[UNPLACED_MARK + name] = i
    return out


def read_anchors(base: str, playlist_id: str, labels: dict,
                 http_get=None) -> dict:
    """Read the playlist back and pair PP's own uuids with the runsheet.

    Returns `{"playlist": playlist_id, "by_uuid": {item uuid: index}}`, or
    {} when there is nothing worth saving. Call it right after the PUT: the
    names still match byte for byte, because Runsheet Pilot wrote them
    moments ago, and the uuids it hands back are what tracking uses from
    then on.
    """
    if not (base and playlist_id and labels):
        return {}
    if http_get is None:
        import requests
        http_get = requests.get
    try:
        r = http_get(f"{base}/v1/playlist/{playlist_id}", timeout=5)
        r.raise_for_status()
        items = (r.json() or {}).get("items") or []
    except Exception as e:
        log.info("Could not read the new playlist back (%s: %s) — auto-track "
                 "falls back to matching header names", type(e).__name__, e)
        return {}
    by_uuid = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        ident = it.get("id") or {}
        uuid = (ident.get("uuid") or "").strip()
        idx = labels.get((ident.get("name") or "").strip())
        # Every item carrying the label is anchored, not just the first:
        # update mode writes into a playlist that may already hold a header
        # by this name, ProPresenter gives each its own uuid, and whichever
        # one the operator clicks is the same segment either way.
        if uuid and isinstance(idx, int) and not isinstance(idx, bool):
            by_uuid[uuid] = idx
    if not by_uuid:
        # Worth a line of its own: the fallback is silent by design, so
        # without this the only symptom of ProPresenter handing back names
        # it has altered — a decoration dropped, a dash normalised — is
        # tracking that quietly stays on header matching for good.
        log.info("Auto-track recognised none of the %d names it had just "
                 "written in the playlist ProPresenter read back; following "
                 "by header name instead", len(set(labels.values())))
        return {}
    log.info("Auto-track anchored %d playlist item(s) by uuid, covering %d "
             "runsheet item(s)", len(by_uuid), len(set(by_uuid.values())))
    return {"playlist": playlist_id, "by_uuid": by_uuid}
