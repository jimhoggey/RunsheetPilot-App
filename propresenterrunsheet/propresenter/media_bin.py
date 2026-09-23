"""Resolve template media against ProPresenter's Media bin before a PUT.

Established by bisecting live PUTs against ProPresenter 7 (its 404s carry
an empty body, so this took actual probing): **the playlist PUT resolves
media items by NAME against the Media bin and ignores the uuid field
entirely.** The template Countdown's real uuid with the name "Welcome"
was accepted; a random uuid with "Welcome" was accepted; every uuid with
"Countdown " was rejected — because "Welcome" is in the operator's Media
bin and "Countdown" is not (it was dragged straight into the template
playlist, never into Media).

Two consequences drive this module:

  1. Media references must be resolved by name against the bin BEFORE the
     PUT — that is the only identity PP honours.
  2. Media that isn't in the bin CANNOT be linked over the API, full
     stop. No refresh or retry changes that. The only honest handling is
     to drop the entry (the runsheet item keeps its coloured header) and
     tell the operator the one-time fix in plain words: drag that file
     into ProPresenter's Media area, then create again.

Presentation-type items are untouched here — they PUT correctly by uuid
(verified live, 204).
"""

import logging
from urllib.parse import quote

log = logging.getLogger("pp_runsheet")


def _norm_name(name: str) -> str:
    return (name or "").strip().casefold()


# PP returns at most this many items per call; `?start=` pages through the
# rest (API docs, confirmed live). The cap only stops a runaway loop.
_PAGE = 100
_MAX_ITEMS = 10_000


def _media_playlists(nodes):
    """Every playlist in the Media sidebar, folders ("group") opened at
    any depth — a playlist inside a folder holds bin media like any other."""
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "group":
            yield from _media_playlists(node.get("children"))
        elif (node.get("id") or {}).get("uuid"):
            yield node["id"]["uuid"]


def fetch_media_bin(base: str, http_get=None) -> list:
    """Every media asset in every Media-bin playlist: [{"uuid","name"},…].

    Reads every page of every playlist, inside folders too. Reading only
    the first page of the top level left anything past item 100, or in a
    folder, looking "not in Media" — on a production machine that dropped
    five slides the operator had in Media from a new playlist.

    Returns [] on any failure — bin resolution is an upgrade, and a PP
    hiccup here must not block playlist creation (the caller just skips
    relinking, which is the pre-fix behaviour)."""
    if http_get is None:
        import requests
        http_get = requests.get
    out = []
    try:
        r = http_get(f"{base}/v1/media/playlists", timeout=6)
        r.raise_for_status()
        for uuid in _media_playlists(r.json()):
            # ProPresenter's own id, but still one path segment and no more.
            segment, seen = quote(str(uuid), safe=""), set()
            for start in range(0, _MAX_ITEMS, _PAGE):
                r2 = http_get(f"{base}/v1/media/playlist/{segment}?start={start}",
                              timeout=6)
                r2.raise_for_status()
                page = [m.get("id") or {} for m in
                        (r2.json() or {}).get("items") or [] if isinstance(m, dict)]
                fresh = [mid for mid in page if mid.get("uuid") not in seen]
                for mid in fresh:
                    seen.add(mid.get("uuid"))
                    # Keep the name EXACTLY as PP stores it — trailing
                    # spaces and all. PP matches media by byte-for-byte
                    # name, so stripping here silently 404s any media the
                    # operator named with stray whitespace ("Countdown ").
                    if mid.get("uuid") and (mid.get("name") or "").strip():
                        out.append({"uuid": mid["uuid"], "name": mid["name"]})
                # A short page is the last one; a page with nothing new
                # means `start` was ignored — stop rather than spin.
                if len(page) < _PAGE or not fresh:
                    break
    except Exception as e:
        log.warning("Could not read PP media bin (%s: %s) — media linking "
                    "will be skipped this run", type(e).__name__, e)
        return []
    return out


def relink_media(matched: list, bin_items: list) -> list:
    """Swap every matched media entry to its Media-bin identity, in place.

    For each `matched[i].parsed.library_match` section: media-type entries
    whose name (trimmed, case-insensitive) exists in the bin get the bin's
    uuid AND the bin's exact name — the name is what PP actually matches
    on, so echoing the bin's spelling guarantees the PUT lands. Entries
    with no bin counterpart are removed and reported; a section left empty
    collapses to None so the runsheet item falls back to a plain header
    and the create as a whole still succeeds.

    Returns [{"item_title", "media_name"}, …] for everything dropped."""
    by_name = {_norm_name(b["name"]): b for b in bin_items or []}
    unlinked = []
    for mi in matched or []:
        parsed = mi.get("parsed") or {}
        lib = parsed.get("library_match")
        if not (isinstance(lib, dict) and isinstance(lib.get("items"), list)):
            continue
        kept = []
        for entry in lib["items"]:
            if (entry.get("type") or "").lower() != "media":
                kept.append(entry)
                continue
            hit = by_name.get(_norm_name(entry.get("name")))
            if hit:
                entry["uuid"] = hit["uuid"]
                entry["target_uuid"] = hit["uuid"]
                entry["name"] = hit["name"]
                kept.append(entry)
            else:
                unlinked.append({
                    "item_title": parsed.get("title", ""),
                    "media_name": (entry.get("name") or "").strip(),
                })
        lib["items"] = kept
        if not kept:
            parsed["library_match"] = None
    return unlinked


def unresolvable_media(names, bin_items: list) -> list:
    """The media names from `names` that are NOT in PP's Media bin.

    The read-only half of this module. `relink_media` REWRITES items onto
    the bin's identity and DROPS the ones with no counterpart — correct
    when the items are a template's suggestion, catastrophic when they
    are the operator's own hand-built playlist, which is what update mode
    points at. Update mode calls this instead: same rule, same
    normalisation, no mutation, so it can warn before writing rather than
    delete after.

    An EMPTY bin is treated as "nothing to say" by the caller, not "none
    of it resolves" — `fetch_media_bin` returns [] on failure too, and
    the two are indistinguishable from here."""
    have = {_norm_name(b.get("name")) for b in bin_items or []}
    out, seen = [], set()
    for n in names or []:
        key = _norm_name(n)
        if not key or key in have or key in seen:
            continue
        seen.add(key)
        out.append((n or "").strip())
    return out
