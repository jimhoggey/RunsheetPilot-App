"""Merge runsheet headers INTO a playlist the operator already built.

The other direction from `playlist.py`. That module builds a brand new
playlist from a runsheet; this one takes a playlist full of media the
operator has already assembled and ordered by hand, and adds the
runsheet's coloured section headers to it without moving, adding or
removing a single slide.

Why it exists: rebuilding is the wrong tool once the media is in place.
Re-creating the playlist means re-sorting and re-adding everything the
operator spent an evening on, to gain headers they could have had for
free. So update mode's entire output is headers — labels, times,
durations and category colours — and its entire promise is that nothing
else changes.

ProPresenter has no insert-item endpoint: the only write is
`PUT /v1/playlist/{uuid}` with the complete items list, which REPLACES
the playlist. Everything here is shaped by that one fact. The existing
items are echoed back verbatim (see `echo_existing_item`), their order
is never touched, and the route around this module snapshots and
verifies because a bad merge is not a bad suggestion — it is a
destroyed playlist.

THE HARD PART is placement: which existing item does a runsheet line
belong above? Three signals, in descending order of trust:

  recall  Where the operator dragged that header LAST time. Read back
          out of the live playlist before the old headers are stripped.
          Nothing beats being told.
  alias   The operator's own `template_aliases` table, already used at
          parse time for names that share no words.
  name    The shipped `resolve_object` rule — every word of the item's
          name appears in the runsheet title — run per candidate.

Playlist media is named things like "PRESERVICE LOOP" and "CTA_GIVING";
runsheet lines read "Pre-service" and "Offering & Announcements". The
name rule fires on a minority of items and that is expected, so the
design is built around the miss: an item we cannot place still gets its
header, marked `↕`, stacked in runsheet order next to its placed
neighbours. Headers are never spread evenly across the gaps — a
confident-looking wrong answer is worse than an obviously-unplaced one,
because the operator reads these at a glance mid-service.

Pure functions only — no HTTP, no disk. `routes/playlist.py` owns the
I/O and `update_safety.py` owns the snapshot/rollback."""

import copy
import re

from .playlist import _coloured_header_for, _color_dict, asset_uuid_of
from .playlist import ACTION_NEEDED_COLOR
from .templates import resolve_object, resolve_with_aliases


# Marks a header we could not line up with any existing item. One glyph,
# not a colour change: the category colour IS the deliverable, and the
# operator needs it to survive. Anything added here must also be taught
# to service_mate/pp_track.py::_clean_header_name, which reverses header
# decorations to match a live PP header back to a runsheet title.
UNPLACED_MARK = "↕ "

BANNER_LABEL = "⚠ DRAG THESE INTO PLACE — nothing matched by name"

# Words that appear in media filenames and carry no information about
# WHICH runsheet line the slide belongs to. Stripped before the name
# rule runs, so "WELCOME SLIDE" can match "Welcome and Connection Cards".
#
# Stripping is why a one-token leftover only ever scores a WEAK anchor:
# reduce "WELCOME SLIDE" to {welcome} and it will happily swallow every
# runsheet line containing the word "welcome", which is exactly the
# misfire resolve_object's all-words rule was written to prevent.
MEDIA_NOISE = frozenset({
    "loop", "slide", "slides", "bg", "background", "video", "still",
    "image", "graphic", "motion", "clip", "cue", "final", "copy",
    "master", "edit", "v1", "v2", "mp4", "mov", "m4v", "png", "jpg",
    "jpeg", "pro", "hd", "full", "new", "old",
})

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# The decorations header_label() and this module put on a label, so a
# header read back out of PP can be reduced to the runsheet title that
# produced it. Order matters: duration tail, then time tail.
#
# Neither pattern starts with \s*, and neither ends with one. A leading
# \s* in front of a $-anchored pattern makes re.sub retry the whitespace
# run from every start position — quadratic on a long run of spaces, and
# these labels come out of ProPresenter, i.e. out of anything anyone typed
# into a header. The surrounding whitespace is stripped in code instead
# (see recall_key), and the input is capped at _MAX_LABEL.
_DUR_TAIL_RE = re.compile(r"\(\s*\d+\s*min\s*\)$", re.IGNORECASE)
_TIME_TAIL_RE = re.compile(
    r"[—–-]\s*(?:\d{1,2}[:.]\d{2}\s*(?:[ap]\.?m\.?)?"
    r"|\d{1,2}\s*[ap]\.?m\.?)$", re.IGNORECASE)
# Longer than any real header; a pathological one is truncated rather
# than scanned in full.
_MAX_LABEL = 300
_LEAD_MARK_RE = re.compile(r"^\s*(?:[↕⇅]\s*|⚠\s*ACTION\s+NEEDED\s*[—–-]\s*|📖\s*)+")


def _norm_words(text: str) -> list:
    """Lowercased word tokens, punctuation and underscores split out,
    ORDER PRESERVED. `templates._title_tokens` returns a set; recall keys
    need a stable string, so this is the ordered sibling."""
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").lower()).replace("_", " ")
    return cleaned.split()


def recall_key(label: str) -> str:
    """Reduce a header label to the runsheet title that produced it.

    `header_label` renders "Offering & Announcements — 10:05 AM (6 min)".
    Next week the time and the duration have moved but the title has not,
    so the key is the title alone, normalised. That is what makes recall
    survive a re-parse: the operator's drag is remembered by WHAT the
    header says, not by when it happens."""
    s = _LEAD_MARK_RE.sub("", (label or "")[:_MAX_LABEL])
    s = _DUR_TAIL_RE.sub("", s.rstrip()).rstrip()
    s = _TIME_TAIL_RE.sub("", s).rstrip()
    return " ".join(_norm_words(s))


def anchor_tokens(name: str) -> set:
    """The tokens of an existing item's name that actually identify it.

    A leading run of digits is an ORDERING prefix, not part of the name:
    operators number their media so it sorts ("01_welcome", "03_song_1").
    Against a live ProPresenter playlist named exactly that way, keeping
    the prefix meant every name demanded its number appear in the
    runsheet line — "Welcome" could never find "01_welcome", and ten
    items anchored none. Only the FIRST token is treated this way, and
    only when something follows it, so "Song 2" and "Psalm 23" keep
    their numbers. File extensions and years are dropped as noise."""
    words = _norm_words(name)
    if len(words) > 1 and words[0].isdigit():
        words = words[1:]
    return {w for w in words
            if w not in MEDIA_NOISE and not _YEAR_RE.match(w)}


def title_token_set(title: str) -> set:
    """Runsheet-title tokens, plus every adjacent pair joined.

    The single loosening on the title side, and it buys exactly one
    class of match: hyphenated or spaced runsheet wording against a
    concatenated media name — "Pre-service" → {pre, service, preservice}
    so "PRESERVICE LOOP" can find it. Joins are computed in title order,
    so it cannot invent a word the operator never wrote."""
    words = _norm_words(title)
    out = set(words)
    for a, b in zip(words, words[1:]):
        out.add(a + b)
    return out


def is_header(item: dict) -> bool:
    return (item.get("type") or "").lower() == "header"


def identity_of(item: dict) -> tuple:
    """What makes a playlist item the same item across a write: its type
    and its name. No uuid of any kind.

    Established against a live ProPresenter 21.4 (Sept 2026), not assumed:
    every PUT mints a fresh playlist-item uuid AND a fresh media
    `target_uuid` — even when a playlist is written back into itself,
    unchanged, twice in a row. Only type, name and order survive. PP also
    resolves media by NAME on the way in (a made-up target_uuid was
    accepted and attached the right image), and the Media bin did not
    grow, so the new ids are a relabel, not new media.

    An earlier version compared the asset uuid too. Against real PP that
    reported every healthy update as ten missing slides and rolled it
    back — the test fake echoed uuids unchanged, so nothing caught it.
    Order is carried by the sequence `content_fingerprint` builds, so a
    reorder or a dropped slide is still caught."""
    idd = item.get("id") or {}
    return ((item.get("type") or "").lower(),
            (idd.get("name") or "").strip().casefold())


def content_fingerprint(items) -> list:
    """The identity sequence of the non-header items — what must survive
    a write untouched, in order. Headers are excluded because replacing
    them is the intended change."""
    return [identity_of(it) for it in items or [] if not is_header(it)]


def echo_existing_item(raw: dict) -> dict:
    """An existing playlist item, ready to send straight back to PP.

    A deep copy with exactly two corrections, both of them rules already
    proven live by `build_playlist_payload`:

      • `target_uuid` must be present on every item or PP's PUT 400s with
        "missing field `target_uuid`" — even on presentation and loop
        items, where PP's own GET omits it.
      • `id.uuid` must mirror the ASSET uuid, not the playlist-item uuid,
        which PP 404s on.

    Everything else passes through verbatim, INCLUDING fields this app
    has never seen. `_capture_item` in templates.py is the wrong tool
    here: it keeps eight known fields and two known types, so anything
    ProPresenter adds in a future version — or any item kind we haven't
    met — would be silently dropped out of the operator's playlist on
    the way back in. Verbatim echo is the only safe default when you are
    rewriting someone else's work."""
    item = copy.deepcopy(raw)
    item.setdefault("target_uuid", "")
    if item.get("target_uuid") is None:
        item["target_uuid"] = ""
    item.setdefault("is_hidden", False)
    item.setdefault("is_pco", False)
    if not is_header(item):
        idd = item.setdefault("id", {})
        if isinstance(idd, dict):
            asset = asset_uuid_of(raw)
            if asset:
                idd["uuid"] = asset
    return item


def split_existing(raw: list) -> tuple:
    """Separate an existing playlist into what we keep and what we learn.

    Returns `(kept, recalled)`.

    `kept` is every non-header item, verbatim and in order. Headers are
    dropped wholesale: the runsheet is the single source of truth for
    this playlist's organisation, so re-running is idempotent by
    construction and an edited runsheet moves labels rather than
    accumulating them.

    `recalled` is the payoff for reading the old headers before dropping
    them: `{recall_key(label): (asset_uuid, casefolded_name)}` for the
    slide each header sits DIRECTLY above. That pairing is the operator's
    own answer to "where does this line belong", recorded by the act of
    dragging it there last week.

    Only the header directly above a slide describes it. When several
    headers stack up before one slide, the ones higher up are the
    unplaced ones this module stacked there, waiting to be dragged — and
    a header still marked ↕ inside a stack was never placed at all. Live
    against ProPresenter, treating a whole stack as placed made a second
    run recall every line to the first slide, plan something different,
    and write again when nothing had changed. So a ↕ header counts only
    once it stands alone (the operator has dragged it out), the banner
    never counts, and each entry is claimed at most once."""
    kept, recalled = [], {}
    run = []          # header names since the last slide, in order
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        if is_header(it):
            name = ((it.get("id") or {}).get("name") or "").strip()
            if name != BANNER_LABEL:
                run.append(name)
            continue
        if run:
            nearest = run[-1]
            placed = (not nearest.startswith(UNPLACED_MARK.strip())
                      or len(run) == 1)
            key = recall_key(nearest)
            if placed and key:
                idd = it.get("id") or {}
                recalled.setdefault(key, (
                    asset_uuid_of(it),
                    (idd.get("name") or "").strip().casefold()))
            run = []
        kept.append(it)
    return kept, recalled


def anchor_candidates(kept: list) -> list:
    """The existing items a runsheet line could sit above."""
    out = []
    for pos, it in enumerate(kept):
        idd = it.get("id") or {}
        name = (idd.get("name") or "").strip()
        if not name:
            continue
        tokens = anchor_tokens(name)
        if not tokens:
            continue
        out.append({"pos": pos, "name": name, "tokens": tokens,
                    "uuid": asset_uuid_of(it),
                    "key": name.casefold()})
    return out


def score_pairs(matched: list, candidates: list, aliases=None,
                recalled=None, ai_anchors=None) -> list:
    """Every (runsheet item, existing item) pairing worth considering,
    with a score saying how much we trust it.

    Tiers, not a blended number — the tiers mean different things and a
    weak name overlap must never outrank being told where it goes:

        1000  recall — the operator dragged this header here before
         500  alias  — the operator's own taught phrase→name pairing
         200  ai     — the alignment pass read the slide and decided
        10*n  name   — every word of an n-token name (n≥2) is in the title
           5  weak   — the same, but only one token survived noise-stripping

    The AI tier sits deliberately BELOW both operator signals. It can
    read a slide that says GIVING and connect it to a line called
    "Offering", which no string rule will ever do — and it can also be
    confidently wrong. A thing the operator told us outranks a thing the
    model inferred, always.
    """
    recalled = recalled or {}
    ai = ai_anchors or {}
    pairs = []
    for n, mi in enumerate(matched or []):
        parsed = (mi.get("parsed") or {}) if isinstance(mi, dict) else {}
        title = parsed.get("title") or ""
        if not title.strip():
            continue
        ttokens = title_token_set(title)
        want = recalled.get(recall_key(title))
        for c in candidates:
            score, via = 0, ""
            if want and (
                    (want[0] and want[0] == c["uuid"])
                    or (want[1] and want[1] == c["key"])):
                score, via = 1000, "recall"
            elif (resolve_with_aliases(title, [c], aliases) is not None
                  and resolve_object(title, [c]) is None):
                # Reusing the shipped resolver one candidate at a time
                # turns "best object for this title" into "does THIS
                # object fit this title" — the primitive needed here —
                # without a second copy of the rule drifting from it.
                score, via = 500, "alias"
            elif ai.get(n) == c["pos"]:
                score, via = 200, "ai"
            elif c["tokens"] <= ttokens:
                score = 10 * len(c["tokens"]) if len(c["tokens"]) > 1 else 5
                via = "name" if len(c["tokens"]) > 1 else "weak"
            if score:
                pairs.append({"n": n, "pos": c["pos"], "score": score,
                              "via": via, "name": c["name"]})
    return pairs


def choose_anchors(pairs: list) -> dict:
    """Pick the best set of anchors that cannot cross each other.

    Both lists are in service order, so an anchor set is only coherent if
    it increases in BOTH the runsheet index and the playlist position.
    This is a maximum-weight strictly-increasing subsequence — solved
    exactly, because the greedy left-to-right alternative lets one weak
    early match at position 30 starve every later item in the runsheet,
    and that failure is invisible in the result.

    Sizes here are tiny (≈20 runsheet items × ≈80 playlist items), so an
    O(n²) DP over the candidate pairs is far below the noise floor of a
    single HTTP call."""
    if not pairs:
        return {}
    ordered = sorted(pairs, key=lambda p: (p["n"], p["pos"], -p["score"]))
    best = [0.0] * len(ordered)
    prev = [-1] * len(ordered)
    for i, pi in enumerate(ordered):
        best[i] = pi["score"]
        for j in range(i):
            pj = ordered[j]
            if pj["n"] < pi["n"] and pj["pos"] < pi["pos"]:
                if best[j] + pi["score"] > best[i]:
                    best[i] = best[j] + pi["score"]
                    prev[i] = j
    end = max(range(len(ordered)), key=lambda i: best[i])
    chain, i = [], end
    while i != -1:
        chain.append(ordered[i])
        i = prev[i]
    return {p["n"]: p for p in reversed(chain)}


def our_header_for(parsed: dict, placed: bool) -> dict:
    """The runsheet item's coloured header, marked when unplaced.

    A placed header is byte-identical to the one create mode writes —
    same label, same category colour — so the two modes produce the same
    thing in ProPresenter and Service Mate reads both the same way."""
    entry = _coloured_header_for(parsed)
    if not placed:
        entry["id"]["name"] = UNPLACED_MARK + entry["id"]["name"]
    return entry


def banner_header() -> dict:
    """The one line shown above a top-stacked block, in ACTION-NEEDED red
    so it reads as "this needs a human" rather than as a section."""
    return {
        "id":           {"uuid": "", "name": BANNER_LABEL, "index": 0},
        "type":         "header",
        "target_uuid":  "",
        "is_hidden":    False, "is_pco": False,
        "header_color": _color_dict(ACTION_NEEDED_COLOR),
    }


def build_update_payload(existing: list, matched: list, aliases=None,
                         ai_anchors=None) -> tuple:
    """The complete items list to PUT back, plus a report on what it did.

    Returns `(items, report)`. `items` is the merged playlist: every
    existing non-header item, untouched and in its original order, with
    one coloured header per runsheet item woven in. `report` is what the
    preview card and the result notice are built from — this function
    decides, the route only reports.

    Placement of a runsheet item with no anchor:

      before the first anchor → the very TOP of the playlist. It happens
        at the start of the service and the operator reads top-down;
        burying it just above a late anchor hides it.
      between two anchors → immediately above the later one, in runsheet
        order. NOT spread across the gap: spreading attaches specific
        slides to an item we failed to identify, which looks like
        knowledge and isn't.
      after the last anchor → the end.

    Nothing anchored at all is the same rule with no anchors to speak of
    — the whole runsheet stacks at the top, under a red banner."""
    kept, recalled = split_existing(existing)
    candidates = anchor_candidates(kept)
    pairs = score_pairs(matched, candidates, aliases, recalled, ai_anchors)
    anchors = choose_anchors(pairs)

    items_in = [mi for mi in (matched or []) if isinstance(mi, dict)]
    # An EMPTY playlist has nowhere else a header could go, so its order
    # is right by construction. Marking every header "↕ couldn't place
    # this" there would be untrue, and the banner would be advice with no
    # possible action behind it. A playlist that HAS items but anchors
    # nothing is the opposite case: there the marks are the whole point.
    nothing_to_place_against = not kept

    # Where each runsheet item's header is inserted, as an index into
    # `kept`; len(kept) means "after everything".
    first_anchored = min(anchors) if anchors else None
    last_anchored = max(anchors) if anchors else None
    inserts: dict = {}
    placements = []
    for n, mi in enumerate(items_in):
        parsed = mi.get("parsed") or {}
        placed = n in anchors or nothing_to_place_against
        if n in anchors:
            at = anchors[n]["pos"]
            via = anchors[n]["via"]
        elif first_anchored is None or n < first_anchored:
            at, via = 0, ""
        elif n > last_anchored:
            at, via = len(kept), ""
        else:
            nxt = min(k for k in anchors if k > n)
            at, via = anchors[nxt]["pos"], ""
        inserts.setdefault(at, []).append((n, our_header_for(parsed, placed)))
        placements.append({
            "index":  n,
            "title":  parsed.get("title") or "",
            "label":  (our_header_for(parsed, placed)["id"]["name"]),
            "placed": placed,
            "via":    via,
            "above":  anchors[n]["name"] if n in anchors else "",
            # The playlist index this header sits above, or None. The
            # alignment pass uses it to state the settled anchors as
            # facts rather than re-asking the model about them.
            "above_index": anchors[n]["pos"] if n in anchors else None,
        })

    anchored_count = len(anchors)
    out = []
    if anchored_count == 0 and items_in and not nothing_to_place_against:
        out.append(banner_header())
    for pos in range(len(kept) + 1):
        for _, header in sorted(inserts.get(pos, []), key=lambda t: t[0]):
            out.append(header)
        if pos < len(kept):
            out.append(echo_existing_item(kept[pos]))

    report = {
        "anchored":       anchored_count,
        "by_recall":      sum(1 for a in anchors.values()
                              if a["via"] == "recall"),
        "by_alias":       sum(1 for a in anchors.values()
                              if a["via"] == "alias"),
        "by_ai":          sum(1 for a in anchors.values()
                              if a["via"] == "ai"),
        "by_name":        sum(1 for a in anchors.values()
                              if a["via"] in ("name", "weak")),
        "unplaced":       0 if nothing_to_place_against
                          else len(items_in) - anchored_count,
        "headers_added":  len(items_in),
        "headers_removed": sum(1 for it in (existing or [])
                               if isinstance(it, dict) and is_header(it)),
        "content_count":  len(kept),
        "placements":     placements,
    }
    return out, report


def visible_signature(items) -> list:
    """What the operator can SEE of a playlist: every item's type and
    name in order, plus each header's colour. Nothing ProPresenter
    re-mints.

    The no-op check compares this, not the raw items. Against a live
    ProPresenter 21.4 every write re-mints the id of every item, headers
    included, and reads headers back with a `destination` field they
    were not sent — so comparing raw items meant "press it twice" always
    looked like a change and wrote again. Names (↕, —, 📖, ⚠ included)
    and header colours were checked and do round-trip exactly."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if is_header(it):
            c = it.get("header_color") or {}
            out.append(("header",
                        ((it.get("id") or {}).get("name") or "").strip(),
                        tuple(round(float(c.get(k) or 0), 4)
                              for k in ("red", "green", "blue", "alpha"))))
        else:
            out.append(identity_of(it))
    return out


def verify_content_preserved(before: list, after: list) -> dict:
    """Did the write keep every slide, in order?

    `before` is the snapshot taken before the PUT; `after` is what PP
    hands back when asked. Compares the non-header identity SEQUENCE, so
    a dropped slide, a duplicated one and a reordered one are all caught,
    while replacing the headers — the whole point of the write — is not
    mistaken for damage."""
    fb, fa = content_fingerprint(before), content_fingerprint(after)
    if fb == fa:
        return {"ok": True, "missing": [], "extra": [], "reordered": False}
    sb, sa = set(fb), set(fa)
    missing = [i[1] for i in fb if i not in sa]
    extra = [i[1] for i in fa if i not in sb]
    return {"ok": False, "missing": missing, "extra": extra,
            "reordered": not missing and not extra}
