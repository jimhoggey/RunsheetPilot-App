"""Playlist payload assembly for ProPresenter's REST API.

`build_playlist_payload` is a pure function — it takes the operator's
`matched` list (each item is `{parsed, match}` where `parsed` is the
LLM-extracted runsheet item and `match` is the fuzzy-match against the
song library) and returns the JSON-serialisable list PP's
`PUT /v1/playlist/{uuid}` expects.

Three branches inside the loop:
  song + match  → presentation item pointing at the matched .pro file
  song, no match → red ACTION-NEEDED header so the volunteer notices
  any other type → coloured header (green for scripture, blue for song,
                   etc.) with optional notes appended

The actual HTTP calls + .playlist export still live in the route
handler — this module only builds the body."""

import re


# RGBA (0-1 floats) for ProPresenter playlist header items, by type.
# Mirrors the tag colours in the UI table so volunteers see the same colour
# in the builder and in PP.
TYPE_COLORS = {
    "song":         (0.06, 0.24, 0.55, 1.0),  # blue
    "mc_on_stage":  (0.05, 0.45, 0.50, 1.0),  # teal
    "announcement": (0.85, 0.50, 0.05, 1.0),  # amber
    "sermon":       (0.45, 0.20, 0.65, 1.0),  # purple
    "prayer_and_ministry": (0.55, 0.35, 0.75, 1.0),  # lavender
    "other":        (0.30, 0.30, 0.40, 1.0),  # gray
    # Legacy types — the parse route now clamps everything to the six
    # above, but runsheet states saved by older versions can still hold
    # these; keep their colours so old data renders unchanged.
    "prayer":       (0.55, 0.35, 0.75, 1.0),  # lavender
    "scripture":    (0.15, 0.50, 0.30, 1.0),  # green
    "offering":     (0.70, 0.55, 0.10, 1.0),  # gold
    "video":        (0.55, 0.20, 0.20, 1.0),  # rust
}
ACTION_NEEDED_COLOR = (0.86, 0.15, 0.15, 1.0)  # bright red


def _color_for_type(t: str) -> dict:
    r, g, b, a = TYPE_COLORS.get(t or "other", TYPE_COLORS["other"])
    return {"red": r, "green": g, "blue": b, "alpha": a}


def _color_dict(rgba: tuple) -> dict:
    r, g, b, a = rgba
    return {"red": r, "green": g, "blue": b, "alpha": a}


def asset_uuid_of(item: dict) -> str:
    """The ASSET uuid PP identifies a playlist item by — NOT the item's own
    playlist-item uuid, which PP 404s on when sent back.

    Where it lives depends on the item's type, established by live
    testing against PP 7 (see the expander in build_playlist_payload):

        media        → top-level `target_uuid`
        presentation → `presentation_info.presentation_uuid`
        loop         → presentation + `duration`, same location

    Public and extracted rather than inlined because update mode echoes
    existing playlist items back to PP and must apply the identical rule.
    A second copy of it is exactly how two paths drift apart."""
    pinfo = item.get("presentation_info") or {}
    return (pinfo.get("presentation_uuid")
            or item.get("target_uuid")
            or (item.get("id") or {}).get("uuid")
            or item.get("uuid", "")
            or "")


# A time of day at the START of the notes field. Parses made before
# `start_time` existed carried the time there — the model happened to lead
# the notes with it — so this recovers it for playlists built from older
# saved state. Matches "6:25 PM", "6:25PM", "19:40", "9:05 am".
_LEADING_TIME = re.compile(
    r"^\s*(\d{1,2}[:.]\d{2}\s*(?:[ap]\.?m\.?)?|\d{1,2}\s*[ap]\.?m\.?)",
    re.IGNORECASE)


def _start_time_of(p: dict) -> str:
    """The item's start time, preferring the real field over the fallback.

    Trailing punctuation is stripped either way: models emit "6:25 PM."
    even when the runsheet has no period, and that dot ends up in every
    ProPresenter header. Deterministic cleanup beats asking nicely.
    """
    explicit = (p.get("start_time") or "").strip()
    if not explicit:
        m = _LEADING_TIME.match(p.get("notes") or "")
        explicit = m.group(1).strip() if m else ""
    return explicit.rstrip(".,;:").strip()


def header_label(p: dict) -> str:
    """The text of a ProPresenter section header: `Title — 6:25 PM (5 min)`.

    Read at a glance, mid-service, by the person running screens. It
    answers what is coming up and when — nothing else. The people's names
    ride along inside the title ("MC Welcome: Ollie & Elliot"), which is
    where the model already puts them.

    `notes` is deliberately NOT included. It holds the runsheet's bullet
    points ("Invite Night coming up", "be prepped beforehand"), which
    turned headers into four lines of prose. Those stay visible in
    Runsheet Pilot's own table and in the Service Mate clock cues.

    Public because the section-expansion path and the unmatched-song path
    both need this exact string — building it in two places is what let
    them drift apart in the first place.
    """
    title = (p.get("title") or "").strip()
    if p.get("type") == "scripture":
        title = f"📖 {title}"

    label = title
    start = _start_time_of(p)
    if start:
        label += f" — {start}"
    # 0 means the runsheet didn't say — not a zero-length item.
    try:
        minutes = int(p.get("duration_min") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes > 0:
        label += f" ({minutes} min)"
    return label


def _coloured_header_for(p: dict) -> dict:
    """Build a single coloured-header entry from a parsed runsheet item.
    Pulled out so both the no-match fallback path AND the section-expansion
    path can use the runsheet's own labelling without duplicating the
    title/notes/scripture-emoji logic."""
    label = header_label(p)
    return {
        "id":           {"uuid": "", "name": label, "index": 0},
        "type":         "header",
        "target_uuid":  "",
        "is_hidden":    False, "is_pco": False,
        "header_color": _color_for_type(p.get("type")),
    }


def _presentation(eff: dict) -> dict:
    """A matched song or single reused slide, as a playlist entry.
    is_hidden / is_pco are required by the PP API; we never produce hidden
    items and don't integrate with Planning Center, so both stay False."""
    pres_uuid = eff.get("uuid", "")
    return {"id": {"uuid": pres_uuid, "name": eff.get("name", ""),
                   "index": eff.get("index", 0)},
            "type": "presentation", "target_uuid": pres_uuid,
            "is_hidden": False, "is_pco": False}


def build_playlist_payload(matched: list) -> list:
    """Assemble the items list to PUT to PP's /v1/playlist/{uuid}.

    Pure function — no I/O, no globals — so it's straightforward to test.
    Each input element is a dict like:

        {"parsed": {"type": "...", "title": "...", "notes": "...",
                    "library_match": <section-dict | single-pres-dict | None>},
         "match":  {"uuid": "...", "name": "...", "index": 0}}    # may be None

    `library_match` can be either:

      • A SECTION dict — `{"header": {"name", "uuid", "color"},
                            "items": [{"name","uuid","target_uuid",...}, ...]}`
        The LLM tagged the runsheet item with a section name from the
        operator's template playlist. We emit the runsheet's own
        coloured header (preserving its title + notes + colour) and
        then the template's media items underneath. Operators stop
        dragging the same "Culture" / "Welcome" slides into every
        week's playlist.

      • A SINGLE-PRESENTATION dict — `{"uuid", "name", "index"}`. Older
        shape from the library-scan path; still honoured (emits one
        presentation entry).

      • None / missing → fall through to the original logic.

    Original resolution order (unchanged):
      • `match` set on a song → presentation pointing at the matched .pro
      • song with no `match` → red ACTION NEEDED placeholder
      • anything else → coloured header from the runsheet's own labelling."""
    items = []
    for mi in matched:
        p = mi.get("parsed") or {}
        m = mi.get("match")
        lib = p.get("library_match")
        song = p.get("type") == "song"
        # A song's own match — never a section: a song's match is the .pro
        # file, whatever media sits on its line.
        found = (m if song and isinstance(m, dict) and m.get("uuid")
                 and not m.get("header") else None)
        # Every runsheet item gets a labelled slot in PP, header FIRST
        # (field report 2026-08-04: "every item needs a header, and then
        # the matched or reused section is if we can populate that header
        # or not"). An unmatched song's is a red ACTION NEEDED placeholder
        # so the volunteer notices and adds it by hand.
        items.append(_action_needed(p) if song and not found
                     else _coloured_header_for(p))

        # Section expansion — the template section's or the bin's items
        # under the runsheet's own header (so "Culture: Jamie and Alex —
        # 6:45 PM" keeps its context), echoed in PP's native shape. On a
        # song line the song comes first, after only a loop that opens
        # the service: until v2.20 the section replaced the song.
        if (isinstance(lib, dict)
                and lib.get("header")
                and isinstance(lib.get("items"), list)):
            entries = [_echo(ti) for ti in lib["items"] if isinstance(ti, dict)]
            if found:
                lead = next((k for k, ti in enumerate(lib["items"])
                             if not (isinstance(ti, dict)
                                     and ti.get("job") == "start")),
                            len(lib["items"]))
                entries.insert(lead, _presentation(found))
            items.extend(entries)
        elif isinstance(lib, dict) and lib.get("uuid"):
            # Single-presentation reuse (legacy shape).
            items.append(_presentation(lib))
        elif found:
            items.append(_presentation(found))
    return items


def _action_needed(p: dict) -> dict:
    """The red header for a song the library has no match for. Uses the
    shared header_label so it stays as short as every other header."""
    return {"id": {"uuid": "", "name": f"⚠ ACTION NEEDED — {header_label(p)}",
                   "index": 0},
            "type": "header", "target_uuid": "",
            "is_hidden": False, "is_pco": False,
            "header_color": _color_dict(ACTION_NEEDED_COLOR)}


def _echo(ti: dict) -> dict:
    """A template or bin item in PP's native shape. Rules locked in by
    live testing:
      • `id.uuid` mirrors the ASSET uuid (not the template's playlist-item
        uuid — PP 404s on that); asset_uuid_of knows where it lives.
      • The original `type` is kept: a media uuid sent as
        `type=presentation` is looked up in the .pro library and 404s.
      • `target_uuid` is always sent — PP's PUT 400s without it, even on
        presentation items where PP's own GET omits it.
      • `presentation_info`, `duration` and `destination` are echoed when
        present, so loops keep looping (PP's GET sometimes omits
        `duration` even when the loop is set; loops still work)."""
    entry = {"id": {"uuid": asset_uuid_of(ti), "name": ti.get("name", ""),
                    "index": ti.get("index", 0)},
             "type": ti.get("type") or "presentation",
             "target_uuid": ti.get("target_uuid", "") or "",
             "is_hidden": False, "is_pco": False}
    if ti.get("presentation_info"):
        entry["presentation_info"] = ti["presentation_info"]
    if ti.get("duration") is not None:
        entry["duration"] = ti["duration"]
    if ti.get("destination"):
        entry["destination"] = ti["destination"]
    return entry
