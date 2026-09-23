"""Playlist creation + ProPresenter connection test routes.

/api/create_playlist creates the playlist in PP, pushes the items
(payload built by build_playlist_payload), optionally exports a
.playlist file to the user's chosen folder, optionally creates [RB]
countdown timers, and persists the Service Mate runsheet state.

/api/update_playlist is the other direction: the operator already built
a playlist full of media, and all they want from Runsheet Pilot is the
runsheet's coloured section headers woven into it. It never creates,
never removes, and re-orders only when the operator says yes to putting
the playlist in runsheet order — see propresenter/playlist_update.py
for the merge and propresenter/update_safety.py for the snapshot and
rollback that surround the write.

The two are deliberately separate routes rather than a mode flag on
one. Their doctrines are inverted: when ProPresenter refuses the items,
create strips the unlinkable ones and carries on, because the operator
must end up with a playlist; update aborts with nothing written,
because the operator already HAS one and it is the thing at risk.
Threading a flag through create's recovery ladder is how the
destructive path would inherit the forgiving path's instincts.

/api/test_connection is a one-call ping to PP's /v1/libraries — used
by the sidebar's "Test connection" button."""

import datetime as _dt
import logging
import os
import re
import shutil
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from .flags import matching_enabled
from .. import stats
from ..logging_setup import log_safe
from ..propresenter.media_bin import (
    fetch_media_bin, relink_media, unresolvable_media,
)
from ..propresenter.discovery import resolve_port
from ..propresenter.net import pp_base, pp_id
from ..propresenter.paths import find_playlist_dir, find_pp_root
from ..propresenter.playlist import build_playlist_payload
from ..propresenter.playlist_update import (
    build_update_payload, is_header, is_placed_header,
    verify_content_preserved, visible_signature,
)
from ..propresenter import update_safety as safety
from ..propresenter.thumbnails import ocr_playlist_media
from ..parsing.align import align_playlist
from ..parsing.models import fetch_catalogue, next_usable_model, resolve_model
from ..propresenter.update_safety import UpdateAborted
from ..propresenter.templates import (
    auto_detect_template_uuid, fetch_pp_playlist_items, fetch_pp_playlist_raw,
    fetch_pp_playlists, playlist_to_objects, playlist_to_sections,
    resolve_object, resolve_with_aliases, template_uuids,
)
from ..propresenter.timers import _create_pp_timers
from ..service_mate.state import (
    _ensure_item_cues, _read_runsheet_state, _write_runsheet_state,
)


bp = Blueprint("playlist", __name__)
log = logging.getLogger("pp_runsheet")

# Characters that would let a playlist name act as a PATH rather than a
# file name: both separators (whichever OS the name was typed on), the
# colon ("C:x" on Windows is a path on another drive), and control
# characters, which no file name should carry.
_NOT_IN_FILE_NAME = re.compile(r"[\\/:\x00-\x1f]")


def _export_file_name(name) -> str:
    """The playlist name reduced to a plain file name for the export folder.

    The name is whatever the operator typed, and it used to go into the
    export path as-is — so "../../x" wrote the .playlist file outside the
    folder they chose. Separators become dashes, so "Sunday 21/09" is
    saved as "Sunday 21-09" instead of failing on a folder that doesn't
    exist.
    Leading dots go too: ".." is not a name, and a leading dot would hide
    the file in Finder. Nothing usable left falls back to a plain default."""
    safe = _NOT_IN_FILE_NAME.sub("-", str(name or "")).strip()
    return safe.lstrip(". ") or "Playlist"


def _rematch_template(matched, base, tmpl_uuid, aliases=None, hint=""):
    """Re-run the deterministic template match for items that missed it.
    Returns how many items it linked.

    Template links are normally attached at PARSE time — but if
    ProPresenter wasn't running then, that lookup failed silently and the
    parsed items arrived here without a single library_match. The old
    behaviour was to build exactly what it was given: a headers-only
    playlist, even though PP was up by the time the operator clicked
    Create (their exact report). Clicking "Refresh playlists" couldn't
    help — it only refills the dropdown.

    So the same title-vs-template-object rule from parse (every word of
    the object's name in the item's title; sections win over single
    objects) runs again HERE, but only when at least one non-song item
    is unmatched — a fully-matched parse costs nothing extra. Best-effort
    throughout: template still unreachable -> unchanged behaviour.

    `hint` is the service label the model reported at parse time,
    forwarded by the client. Because this rescue re-resolves "Auto" from
    scratch, it was the step that could undo a decline parse got right:
    given only item titles to go on, it would re-attach a template that
    is not for this service. Resolving from the same label parse used
    makes the two agree by construction. Titles remain the fallback when
    no label is available."""
    needs = [mi for mi in matched
             if isinstance(mi.get("parsed"), dict)
             and mi["parsed"].get("type") != "song"
             and not mi["parsed"].get("library_match")]
    if not needs:
        return 0
    try:
        if not tmpl_uuid:
            hint = (hint or "").strip() or " ".join(
                (mi["parsed"].get("title") or "") for mi in needs)
            tmpl_uuid = auto_detect_template_uuid(
                fetch_pp_playlists(base), hint=hint) or ""
        if not tmpl_uuid:
            return 0
        raw = fetch_pp_playlist_items(base, tmpl_uuid)
        sections = playlist_to_sections(raw)
        objects = playlist_to_objects(raw)
        # Section headers as matchable pseudo-objects: a title hit on the
        # header name expands the whole section, same as parse time.
        headers = [{"name": s_["header"]["name"], "_section": s_}
                   for s_ in sections]
        hits = 0
        for mi in needs:
            parsed = mi["parsed"]
            title = parsed.get("title") or ""
            hdr = resolve_object(title, headers)
            if hdr:
                parsed["library_match"] = hdr["_section"]
                hits += 1
                continue
            obj = resolve_with_aliases(title, objects, aliases)
            if obj:
                parsed["library_match"] = {
                    "header": {"name": obj["name"], "uuid": obj["uuid"],
                               "color": {}},
                    "items": [obj],
                }
                hits += 1
        if hits:
            log.info("Create-time template re-match linked %d item(s) "
                     "the parse missed (PP was likely closed then)", hits)
        return hits
    except Exception:
        log.exception("Create-time template re-match failed; continuing")
        return 0


def _write_sm_state(name, matched, timer_result, keep_position=False) -> None:
    """Persist the Service Mate runsheet state — what the GeekMagic clocks
    display on the LAN. We strip the "match" wrappers and keep only the
    parsed items, plus stamp each item with the exact PP timer name we
    created for it (so auto-track can match by name later).

    Shared by create and update mode. `keep_position` is update mode's
    concession to being run mid-service: when the item list is unchanged
    from what is already on disk, the live clock's position is preserved,
    so re-running at 10:05 to fix one header does not send every clock
    in the building back to the top of the service. Never fatal — clocks
    are an add-on and a failure here must not fail the playlist."""
    try:
        timer_names = (timer_result or {}).get("timer_names") or {}
        sm_items = []
        for i, mi in enumerate(matched):
            p = dict((mi.get("parsed") or {}))
            if i in timer_names:
                p["pp_timer_name"] = timer_names[i]
            _ensure_item_cues(p)
            sm_items.append(p)
        current_index, started_at = 0, _dt.datetime.now().isoformat()
        if keep_position:
            prev = _read_runsheet_state() or {}
            prev_titles = [(it or {}).get("title")
                           for it in (prev.get("items") or [])]
            if prev_titles == [it.get("title") for it in sm_items]:
                current_index = prev.get("current_index", 0) or 0
                started_at = prev.get("current_started_at") or started_at
        _write_runsheet_state({
            "service_name":       name,
            "items":              sm_items,
            "current_index":      current_index,
            "current_started_at": started_at,
            "auto_track":         {"enabled": True},
        })
        log.info(f"Service Mate state written: {len(sm_items)} items")
    except Exception:
        log.exception("Service Mate state write failed (non-fatal)")


@bp.route("/api/create_playlist", methods=["POST"])
def api_create_playlist():
    import requests as req
    body = request.get_json(silent=True) or {}
    host = body.get("host") or "localhost"
    port = body.get("port") or "50001"
    base = pp_base(host, port)
    name = (body.get("name") or "").strip()
    matched = body.get("matched") or []
    do_matching = matching_enabled(body)
    before = lap_from = time.time()
    steps = {}           # seconds per step, logged: where a slow build goes

    def lap(step):
        nonlocal lap_from
        now = time.time()
        steps[step], lap_from = round(now - lap_from, 1), now

    if not name:
        return jsonify({"error": "Playlist name required."}), 200
    if not matched:
        return jsonify({"error": "No items to add to the playlist."}), 200

    try:
        # 0. Resolve template media against PP's Media bin BEFORE anything
        # else. PP's playlist PUT matches media items by NAME against the
        # Media bin and ignores the uuid (established by live bisection —
        # its 404s carry an empty body, so nothing else would have told
        # us). Media that isn't in the bin cannot be linked over the API
        # at all; relink_media drops those entries (their runsheet items
        # keep their coloured headers) and reports them so the UI can give
        # the operator the one-time fix in plain words. Bin fetch failing
        # just skips this step — worst case is the old behaviour.
        # An empty bin result is indistinguishable from a transient PP
        # hiccup (fetch_media_bin returns [] on failure), so relinking is
        # skipped rather than applied — applying it against [] would drop
        # every linked slide on a blip. If PP then refuses the template
        # identities, the safe-mode retry below still saves the create.
        # 0a. Items may have arrived unmatched because PP was closed at
        # parse time — re-run the deterministic template match now that
        # PP is (presumably) up. No-op when everything already matched.
        #
        # Both steps are skipped when "Populate with media from PP" is
        # off. The rescue especially: it exists to recover links the
        # operator wanted and didn't get, so firing it here would hand
        # back exactly what they just turned off. With no links there is
        # also no media to relink, but the bin fetch is skipped explicitly
        # rather than left to be a harmless no-op — on the production
        # machine that call walks a 1,261-item library.
        unlinked = []
        template_relinked = 0
        if do_matching:
            from ..settings import load_settings as _ls
            template_relinked = _rematch_template(
                matched, base,
                (body.get("template_playlist_uuid") or "").strip(),
                (_ls() or {}).get("template_aliases"),
                hint=(body.get("service_label") or "").strip()) or 0

            bin_items = fetch_media_bin(base)
            unlinked = relink_media(matched, bin_items) if bin_items else []
        lap("template and Media")
        if unlinked:
            log.info("Media not in PP's Media bin, left as headers: %s",
                     log_safe(", ".join(u["media_name"] for u in unlinked)))

        # 1. Create the playlist
        r = req.post(f"{base}/v1/playlists",
                     json={"name": name, "type": "playlist"}, timeout=6)
        r.raise_for_status()
        pid = r.json().get("id", {})
        if isinstance(pid, dict):
            playlist_id = pid.get("uuid") or pid.get("name") or name
        else:
            playlist_id = str(pid) or name
        # This id goes into the URL PATH of the pushes below. PP normally
        # answers with its own uuid, but the fallbacks reach the name the
        # operator typed, and a name like "../timer/x" would walk out of
        # the playlist API. pp_id lets only a plain id through.
        #
        # An ordinary name ("Sunday Service") is not a plain id — it has a
        # space — so when PP answered without a uuid, ask PP for the uuid
        # of the playlist it just made rather than refuse a create that
        # used to work. Only when that also fails is it an error.
        try:
            playlist_id = pp_id(playlist_id)
        except ValueError:
            found = next((p["uuid"] for p in fetch_pp_playlists(base)
                          if p.get("name") == name), "")
            try:
                playlist_id = pp_id(found)
            except ValueError:
                log.error("PP gave no usable id for the new playlist "
                          "(got %s)", log_safe(playlist_id, 80))
                stats.track("playlist_failed", reason="no_playlist_id",
                            items=len(matched))
                # Not "click Create again" on its own: the playlist exists,
                # so a second create would leave two.
                return jsonify({"error":
                    "ProPresenter made the playlist but didn't say how to "
                    "find it, so nothing was added to it. Delete the empty "
                    "playlist it just made in ProPresenter, then click "
                    "Create again."}), 200

        # 2. Build items list — pure function in propresenter/playlist.py
        items = build_playlist_payload(matched)

        # 3. Push items to playlist
        r2 = req.put(f"{base}/v1/playlist/{playlist_id}",
                     json=items, timeout=10)
        if r2.status_code in (400, 404):
            # Shouldn't happen now that media is bin-resolved up front —
            # but if PP still refuses, recover instead of stranding the
            # operator: strip every linked slide (headers stay), push
            # again, and say plainly which slides were left out. The old
            # message here blamed "song UUIDs" and told them to re-scan
            # the library, which was wrong on both counts and
            # unactionable for a non-developer.
            log.error("PP refused playlist items (HTTP %s, body=%r) — "
                      "retrying without linked slides",
                      r2.status_code, log_safe(r2.text, 300))
            dropped = []
            for mi in matched:
                parsed = mi.get("parsed") or {}
                lib = parsed.get("library_match")
                if isinstance(lib, dict) and lib.get("items"):
                    for entry in lib["items"]:
                        dropped.append({
                            "item_title": parsed.get("title", ""),
                            "media_name": (entry.get("name") or "").strip(),
                        })
                    parsed["library_match"] = None
            items = build_playlist_payload(matched)
            r2 = req.put(f"{base}/v1/playlist/{playlist_id}",
                         json=items, timeout=10)
            if r2.status_code in (400, 404):
                log.error("PP refused even the headers-only playlist "
                          "(HTTP %s, body=%r)",
                          r2.status_code, log_safe(r2.text, 300))
                stats.track("playlist_failed", reason="pp_refused_headers",
                            items=len(matched))
                return jsonify({"error":
                    "ProPresenter wouldn't accept the playlist items. "
                    "Try restarting ProPresenter, then click Create "
                    "again — the app will rebuild everything fresh."}), 200
            unlinked = unlinked + dropped
        r2.raise_for_status()
        lap("playlist")

        songs = sum(1 for mi in matched
                    if (mi.get("parsed") or {}).get("type") == "song"
                    and mi.get("match"))
        needs_action = sum(1 for mi in matched
                           if (mi.get("parsed") or {}).get("type") == "song"
                           and not mi.get("match"))
        headers = sum(1 for mi in matched
                      if (mi.get("parsed") or {}).get("type") != "song")

        # 4. Try to export the .playlist file
        #
        # The playlist is already built in ProPresenter by now, so a failed
        # export must not fail the create: the catch-all at the bottom would
        # tell the operator to click Create again and leave them with two
        # playlists. An unplugged drive, a read-only folder or a Windows-
        # invalid name all land here; the UI already says "Could not find
        # the exported file" whenever export_path comes back empty.
        #
        # The folder is the one saved in Settings. The request only says
        # WHETHER to export: a create call carrying a filesystem path would
        # let whoever sends it choose where the app writes.
        export_path = None
        export_dir = ""
        if body.get("export"):
            from ..settings import load_settings
            export_dir = str(load_settings().get("export_dir") or "").strip()
        if export_dir:
            try:
                pdir = find_playlist_dir(find_pp_root())
                if pdir:
                    time.sleep(1.0)
                    candidates = [f for f in Path(pdir).iterdir()
                                  if f.is_file()
                                  and f.stat().st_mtime > before]
                    if candidates:
                        newest = max(candidates,
                                     key=lambda f: f.stat().st_mtime)
                        folder = os.path.normpath(export_dir)
                        Path(folder).mkdir(parents=True, exist_ok=True)
                        dest = os.path.normpath(os.path.join(
                            folder, f"{_export_file_name(name)}.playlist"))
                        # _export_file_name already keeps the name a plain
                        # file name; this says so where CodeQL can see it.
                        # join(folder, "") ends in exactly one separator,
                        # so a drive root such as E:\ still works.
                        if not dest.startswith(os.path.join(folder, "")):
                            raise OSError("export name left the folder")
                        shutil.copy2(newest, dest)
                        export_path = dest
            except OSError:
                log.exception("Playlist export failed (playlist itself "
                              "was created)")
                export_path = None
        lap("export")

        # 5. Optional: create duration-based countdown timers
        timer_result = {"created": 0, "deleted": 0, "no_duration": 0,
                        "total_items": 0, "errors": [], "timer_names": {}}
        if body.get("create_timers"):
            timer_result = _create_pp_timers(
                base, name, matched, key_only=bool(body.get("timers_key_only")))
        lap("timers")

        # 6. Persist Service Mate runsheet state — what the GeekMagic clocks
        # display on the LAN.
        _write_sm_state(name, matched, timer_result)
        log.info("Create took %.1fs — %s", time.time() - before,
                 ", ".join(f"{k} {v}s" for k, v in steps.items()))

        log.info(f"Playlist created: '{log_safe(name)}' → {songs} songs, "
                 f"{headers} headers, "
                 f"{needs_action} action-needed, {timer_result['created']} timers "
                 f"(deleted {timer_result['deleted']} old, "
                 f"{timer_result['no_duration']} skipped no-duration), "
                 f"export={log_safe(export_path, 500)}")

        # The numbers that describe a real run: how long the import took,
        # how much landed in ProPresenter, and how much of it is section
        # headers vs linked slides.
        pp_sections = sum(1 for it in items
                          if (it or {}).get("type") == "header")
        stats.track("playlist_created",
                    import_ms=int((time.time() - before) * 1000),
                    pp_items=len(items),
                    pp_sections=pp_sections,
                    songs=songs,
                    headers=headers,
                    needs_action=needs_action,
                    timers=timer_result["created"],
                    unlinked=len(unlinked),
                    matching=do_matching,
                    exported=bool(export_path))
        if unlinked:
            # The "couldn't attach this media" case. COUNT only — the
            # media names carry event branding ("C3 SUMMIT 2025 …"), which
            # is church content and stays in app.log where the operator
            # can read it.
            stats.track("media_unlinked", count=len(unlinked),
                        items=len(matched))

        return jsonify({
            "ok":                  True,
            "songs":               songs,
            "headers":             headers,
            "needs_action":        needs_action,
            # Template slides that couldn't be attached because their
            # media isn't in PP's Media bin — the UI turns this into a
            # plain-English "drag these into Media, then Create again".
            "unlinked":            unlinked,
            # Items the create-time rescue linked that the parse missed —
            # normally because ProPresenter was closed when they parsed.
            "template_relinked":   template_relinked,
            "timers_created":      timer_result["created"],
            "timers_deleted":      timer_result["deleted"],
            "timers_no_duration":  timer_result["no_duration"],
            "timers_total_items":  timer_result["total_items"],
            "timer_errors":        timer_result["errors"],
            "export_path":         export_path,
        })

    except req.exceptions.ConnectionError:
        stats.track("playlist_failed", reason="pp_unreachable")
        return jsonify({"error":
            f"Cannot connect to ProPresenter at {host}:{port}. "
            "Make sure ProPresenter is running and Network is enabled in "
            "Preferences → Integrations → Network."}), 200
    except Exception as e:
        log.exception("Playlist create failed")
        stats.report_error(e, where_kind="route", route="create_playlist")
        # Never hand the exception text to the page: it can carry paths
        # and internals, and it tells a volunteer nothing they can act on.
        # The full traceback is in the log line above.
        # It can fire after PP already made the playlist (a failed push),
        # so the advice has to cover the half-built one it may leave.
        return jsonify({"error":
            "Something went wrong creating the playlist. Check "
            "ProPresenter is running and try again. If a half-built "
            "playlist was left behind in ProPresenter, delete it "
            "first."}), 200


@bp.route("/api/test_connection", methods=["POST"])
def api_test_connection():
    """Test the link to ProPresenter, finding the port if need be.

    ProPresenter does not always listen on 50001 — a real machine here
    ran on 55416, which made every library and template lookup fail with
    nothing on screen to explain it. When the configured port doesn't
    answer and PP is on this machine, its own preferences say which port
    it chose; the UI writes the discovered value back into the box so the
    fix sticks.
    """
    import requests as req
    from ..settings import load_settings

    body = request.get_json(silent=True) or {}
    host = body.get("host") or "localhost"
    port = str(body.get("port") or "50001")

    # The probe IS the connection test — it calls the same endpoint the
    # route needs anyway and caches the result, so discovery adds no
    # extra outbound request. `pp_base` clamps host to hostname
    # characters and port to digits, so neither can smuggle a path,
    # scheme or second URL into the address.
    seen = {}

    def _probe(h, p) -> bool:
        # pp_base raises UnreachableHost for anything outside
        # loopback/LAN. Build the URL OUTSIDE the try so that refusal
        # propagates to the app-level handler — swallowing it here would
        # report "didn't answer", sending the operator to check a port
        # when the real problem is the address.
        url = f"{pp_base(h, p)}/v1/libraries"
        try:
            r = req.get(url, timeout=3)
            if r.ok:
                seen["libs"] = r.json()
                return True
        except Exception:
            # Unreachable, refused, or not ProPresenter — all of which
            # mean the same thing to the caller: not listening here.
            pass
        return False

    note = ""
    if (load_settings().get("auto_port") is not False):
        original = port
        port, note = resolve_port(host, port, probe=_probe)
        if port != original:
            # The port BOX is a free-text field, so `original` is
            # whatever the operator typed — never send it. Only the
            # digits we actually connected on, and whether the old value
            # was the shipped default, which is the thing worth knowing.
            stats.track("port_discovered",
                        now=int(re.sub(r"\D", "", port) or 0),
                        was_default=(str(original).strip() == "50001"))

    try:
        libs = seen.get("libs")
        if libs is None:                      # auto_port off, or it failed
            if not _probe(host, port):
                raise req.exceptions.ConnectionError()
            libs = seen.get("libs")
        return jsonify({"ok": True,
                        "count": len(libs) if hasattr(libs, "__len__") else 0,
                        "port": port, "note": note})
    except req.exceptions.ConnectionError:
        # The raw requests error ("HTTPConnectionPool… Max retries exceeded
        # … Errno 61") reads like a stack trace to a volunteer. Say only
        # what happened and what to do — `note` usually already does.
        return jsonify({"ok": False, "port": port, "note": note, "error":
            note or f"Can't reach ProPresenter at {host}:{port}."})
    except Exception:
        # Never hand the exception text to the caller: it can carry paths
        # and internals, and it tells a volunteer nothing they can act on.
        log.exception("connection test failed")
        return jsonify({"ok": False, "port": port, "note": note,
                        "error": note or "Couldn't reach ProPresenter."})


@bp.route("/api/pp/playlists", methods=["GET"])
def api_pp_playlists():
    """List the operator's PP playlists, plus a peek at each as a "template"
    (how many sections + items it would contribute if chosen). Used by the
    sidebar dropdown so the operator can pick the right "<Service> - Library"
    playlist. Pulls host/port from the query string (the UI already knows
    them) and falls back to the standard PP defaults."""
    host = (request.args.get("host") or "localhost").strip()
    port = (request.args.get("port") or "50001").strip()
    base = pp_base(host, port)
    playlists = fetch_pp_playlists(base)
    if not playlists:
        # Common failure: PP not running, Network off, or wrong port.
        # We return ok=True with empty list so the UI can show "0
        # playlists — is PP running?" instead of an error banner.
        return jsonify({"ok": True, "playlists": [], "auto_detected": ""})
    # For every playlist, count how many sections it would give us if
    # used as a template. Operators glance at this to spot their actual
    # template playlist vs. a one-shot service playlist.
    #
    # `item_count` / `header_count` are what the UPDATE picker reads. A
    # hand-built service playlist has no sections at all, so without
    # them every option in update mode would read "(no sections)" — the
    # picker would carry no information in the one mode it exists for.
    # The items are already fetched for the sections peek, so this costs
    # no extra HTTP.
    #
    # `is_template` separates the playlists the app builds runsheets FROM
    # (named "… Library" / "… Template", or pinned by the operator) from
    # everything else, so the dropdown can group them instead of burying
    # three templates among forty services.
    from ..settings import load_settings
    pinned = ((load_settings() or {}).get("template_playlist_uuid") or "")
    templates = template_uuids(playlists, pinned)
    named = template_uuids(playlists)          # by name alone, no pin
    enriched = []
    for p in playlists:
        try:
            raw = fetch_pp_playlist_items(base, p["uuid"])
            sections = playlist_to_sections(raw)
        except Exception:
            log.exception("sections peek failed for %s",
                          log_safe(repr(p.get("name"))))
            raw, sections = [], []
        enriched.append({
            **p,
            "section_count": len(sections),
            "media_count":   sum(len(s.get("items", [])) for s in sections),
            "item_count":    sum(1 for it in raw
                                 if isinstance(it, dict) and not is_header(it)),
            "header_count":  sum(1 for it in raw
                                 if isinstance(it, dict) and is_header(it)),
            "is_template":   p["uuid"] in templates,
            # Why it counts: its name, or because the operator pinned it.
            # The dropdown says which, so a pinned oddly-named playlist
            # doesn't look like a mistake.
            "template_by":   ("name" if p["uuid"] in named
                              else "pinned" if p["uuid"] in templates
                              else ""),
        })
    auto = auto_detect_template_uuid(playlists) or ""
    return jsonify({"ok": True, "playlists": enriched, "auto_detected": auto})


# ── Update an existing playlist ───────────────────────────────────────────
# Everything below writes into a playlist the operator built by hand.
# Read update_safety.py's module docstring before changing any of it.

def _aliases():
    from ..settings import load_settings
    return (load_settings() or {}).get("template_aliases")


def _runsheet(body: dict) -> list:
    """The parsed runsheet from a request, lines only. Every index — the
    model's, the placements', the slide reading's — counts these, so
    anything that isn't a line is dropped here, once."""
    matched = body.get("matched")
    return [m for m in matched if isinstance(m, dict)] \
        if isinstance(matched, list) else []


def _resolve_target(base: str, client_uuid) -> tuple:
    """The playlist to act on, as PROPRESENTER names it.

    Returns `(uuid, name, playlists)` where `uuid` and `name` are PP's own
    values, never the browser's. Two reasons, one safety and one
    correctness:

      • the id goes into a URL path on routes that PUT, so it is checked
        (pp_id) and then swapped for PP's copy of it — see net.pp_id for
        what an unchecked id could reach;
      • a destructive write should only ever target a playlist
        ProPresenter confirms exists right now.

    `playlists` is returned so callers that need the full list (the
    template warning) reuse this read instead of making a second one
    that could fail on its own. A failed list read aborts: for a write
    that replaces the playlist, "couldn't confirm it exists" is a stop."""
    try:
        want = pp_id(client_uuid)
    except ValueError:
        raise UpdateAborted("no_playlist",
                            "Choose the playlist you want to add headers to.")
    playlists = fetch_pp_playlists(base)
    for p_ in playlists:
        if p_.get("uuid") == want:
            return p_["uuid"], p_.get("name") or "playlist", playlists
    raise UpdateAborted(
        "read_failed",
        "Couldn't find that playlist in ProPresenter, so nothing was "
        "changed. Check ProPresenter is running, press ↻ Refresh "
        "playlists, then try again.")


def _read_target(base: str, playlist_uuid: str) -> list:
    """The playlist we are about to rewrite, or raise.

    `playlist_uuid` must already be ProPresenter's own id (from
    _resolve_target). `fetch_pp_playlist_raw` returns None for a failed
    read and [] for a genuinely empty playlist, and that distinction is
    the single most important line in this feature: reading a network
    hiccup as "empty" and then PUTting headers against that belief
    deletes every slide the operator owns."""
    raw = fetch_pp_playlist_raw(base, playlist_uuid)
    if raw is None:
        raise UpdateAborted(
            "read_failed",
            "Couldn't read that playlist from ProPresenter, so nothing was "
            "changed. Check ProPresenter is running, then try again.")
    return raw


def _sane_sections(raw_sections, n_runsheet: int, n_items: int) -> dict:
    """The slide reading the client hands back, `{slide: runsheet line}`,
    re-checked: whole numbers, in range.

    The preview computes it and the write reuses it rather than calling
    the model a second time — that keeps "press it twice" a genuine no-op,
    and the operator confirms exactly the plan that gets written. It
    arrives over HTTP, so nothing about it is trusted; the worst a forged
    one can do is file slides under the wrong lines, because
    runsheet_order only ever permutes the slides already there."""
    out = {}
    for pos, n in (raw_sections.items() if isinstance(raw_sections, dict) else ()):
        try:
            pos, n = int(pos), int(n)
        except (TypeError, ValueError):
            continue
        if 0 <= pos < n_items and 0 <= n < n_runsheet:
            out[pos] = n
    return out


def _ai_sections(base: str, playlist_uuid: str, raw: list, matched: list,
                 report: dict) -> tuple:
    """Read every still and ask a model which runsheet line each slide
    belongs to. Returns ({slide: line}, the model asked or None).

    Media file names in a working playlist are often out of date, so a
    name match is NOT a fact here: the model sees each item's name and
    what its slide reads, and trusts the slide. The facts it works around
    are the ones someone vouches for — an alias the operator taught, and
    a song, whose .pro name is its title. Existing headers go in as
    context rather than facts: when slides are moved around them, where
    they sit stops meaning anything. Positions count only non-header
    items, as the payload builder does; ProPresenter's own indexes count
    headers too. Entirely best-effort: no key, model, OCR engine or
    answer returns {}."""
    from ..settings import load_settings
    settings = load_settings() or {}
    or_key = (settings.get("or_key") or "").strip()
    at = [i for i, it in enumerate(raw) if isinstance(it, dict) and not is_header(it)]
    kept = [raw[i] for i in at]
    if not or_key or not kept:
        return {}, None
    known = {p["above_index"]: p["index"] for p in report.get("placements", [])
             if p.get("above_index") is not None and p.get("via") != "recall"
             and (p.get("via") == "alias"
                  or (kept[p["above_index"]].get("type") or "").lower() == "presentation")}
    if len(known) >= len(kept):
        return known, None              # every slide already vouched for
    catalogue = fetch_catalogue()
    model = resolve_model((settings.get("or_model") or "").strip(), catalogue,
                          api_key=or_key)       # a paid key runs on a paid model
    if not model:
        return {}, None
    read = ocr_playlist_media(base, playlist_uuid, raw)     # by PP's index
    slide_text = {pos: read[i] for pos, i in enumerate(at) if i in read}
    context = [it for it in raw if isinstance(it, dict)
               and (not is_header(it) or is_placed_header(it))]
    found = align_playlist(matched, context, slide_text, known, is_header,
                           or_key, model,
                           backup=next_usable_model(model, catalogue))
    return ({**found, **known} if found else {}), model


def _plan_update(base: str, playlist_uuid: str, matched: list,
                 sections=None, use_ai: bool = False,
                 reorder: bool = False) -> dict:
    """Work out the new playlist without sending anything.

    One engine for both the preview and the write, so what the operator
    confirms is what gets sent — a preview computed by different code
    from the write is a preview of nothing. `sections` is a slide reading
    the client hands back from the preview; `reorder` is the operator's
    yes to putting the playlist in runsheet order."""
    playlist_uuid, playlist_name, playlists = _resolve_target(
        base, playlist_uuid)
    raw = _read_target(base, playlist_uuid)
    aliases = _aliases()
    sections = _sane_sections(sections, len(matched),
                              sum(1 for it in raw if isinstance(it, dict)
                                  and not is_header(it)))
    ai_model = None
    if use_ai and not sections:
        # Asked even when the names placed everything: a stale name
        # "matches" just as confidently as a right one. A first pass
        # without the model says which placements are vouched for.
        _, first = build_update_payload(raw, matched, aliases)
        sections, ai_model = _ai_sections(base, playlist_uuid, raw, matched,
                                          first)
    items, report = build_update_payload(raw, matched, aliases, sections,
                                         reorder)
    return {
        "uuid":         playlist_uuid,       # ProPresenter's, not the client's
        "name":         playlist_name,
        "playlists":    playlists,
        "raw":          raw,
        "items":        items,
        "report":       report,
        "sections":     sections,
        "ai_model":     ai_model,            # the model that read the slides
        "fingerprint":  safety.fingerprint(raw),
        # Clicking the button twice is the most common operator
        # behaviour there is, and the safest destructive write is the
        # one that never happens. Compared on what is VISIBLE — PP
        # re-mints every id on every write, see visible_signature.
        "no_change":    visible_signature(items) == visible_signature(raw),
    }


def _bin_preflight(base: str, raw: list) -> list:
    """Media in this playlist that ProPresenter's Media bin doesn't know
    about — read-only, and advisory rather than a gate.

    PP resolves media in a playlist PUT by NAME against the Media bin
    (see media_bin.py). Whether it also refuses media it just handed us
    back out of that same playlist has not been established against a
    live install, so this warns and lets the operator decide instead of
    blocking a playlist that may well write fine. The snapshot and the
    rollback are what make that an acceptable bet.

    `relink_media` is deliberately NOT called here, ever: it rewrites
    items onto the bin's identity and DELETES the ones with no
    counterpart. Against a template's suggestion that discards a guess;
    against the operator's own playlist it is precisely the data loss
    this feature exists to prevent."""
    names = [((it.get("id") or {}).get("name") or "").strip()
             for it in raw or []
             if isinstance(it, dict) and (it.get("type") or "").lower() == "media"]
    bin_items = fetch_media_bin(base)
    if not bin_items:
        # [] is indistinguishable from a PP hiccup — say nothing rather
        # than accuse every slide of being missing.
        return []
    return unresolvable_media(names, bin_items)


@bp.route("/api/update_playlist/preview", methods=["POST"])
def api_update_playlist_preview():
    """The plan, and not one byte written.

    The preview is not decoration. The write replaces a playlist the
    operator assembled by hand, so they see where every header is going
    and confirm it first."""
    body = request.get_json(silent=True) or {}
    base = pp_base(body.get("host") or "localhost",
                   body.get("port") or "50001")
    playlist_uuid = (body.get("playlist_uuid") or "").strip()
    matched = _runsheet(body)
    if not matched:
        return jsonify({"error": "Parse a runsheet first."}), 200
    try:
        plan = _plan_update(base, playlist_uuid, matched,
                            sections=body.get("ai_sections"),
                            use_ai=bool(body.get("use_ai")),
                            reorder=bool(body.get("reorder")))
    except UpdateAborted as e:
        return jsonify({"ok": False, "error": e.message,
                        "reason": e.reason}), 200
    except Exception as e:
        log.exception("update preview failed")
        stats.report_error(e, where_kind="route", route="update_preview")
        return jsonify({"ok": False, "reason": "unexpected", "error":
            "Couldn't work out the changes for that playlist."}), 200
    # A slide reading handed back is slide POSITIONS in the playlist it was
    # read from. Applied to a playlist that has changed since, it files the
    # wrong slides — so the reorder question's answer carries the reading's
    # fingerprint, and a changed playlist stops here.
    expect = body.get("expect_fingerprint")
    if expect is not None and expect != plan["fingerprint"]:
        return jsonify({"ok": False, "reason": "concurrent_edit", "error":
            "ProPresenter changed while this was on screen, so nothing was "
            "changed. Press Add Section Headers again."}), 200

    warnings = []
    active = safety.active_playlist_uuid(base)
    if active and active == plan["uuid"]:
        warnings.append("live")
    # Update mode replaces every header, and a template's headers ARE its
    # sections — the thing create mode reads to find "Welcome", "Culture"
    # and the rest. Organising a template by mistake would quietly break
    # next week's build. Not blocked (the operator asked for every
    # playlist to be available here), but said out loud before confirm.
    #
    # Uses the playlist list the plan already read to confirm the target
    # exists, so this check cannot fail open on a second read of its own:
    # if that list could not be read, the plan aborted before we got here.
    from ..settings import load_settings
    pinned = ((load_settings() or {}).get("template_playlist_uuid") or "")
    if plan["uuid"] in template_uuids(plan["playlists"], pinned):
        warnings.append("template")
    if any(it.get("is_pco") for it in plan["raw"] if isinstance(it, dict)):
        warnings.append("pco")
    unbinned = _bin_preflight(base, plan["raw"])
    if unbinned:
        log.info("Media not in PP's Media bin for update: %s",
                 log_safe(", ".join(unbinned)))

    rep = plan["report"]
    # Out of runsheet order: what yes would look like, for the question.
    new_order = []
    if rep["out_of_order"] and not rep["moved"]:
        alt, _ = build_update_payload(plan["raw"], matched, _aliases(),
                                      plan["sections"], reorder=True)
        new_order = [[is_header(it), ((it.get("id") or {}).get("name") or "")]
                     for it in alt]
    return jsonify({
        "ok":          True,
        "no_change":   plan["no_change"],
        "fingerprint": plan["fingerprint"],
        "unbinned":    unbinned,
        "warnings":    warnings,
        # Handed back so the write reuses this exact reading instead of
        # calling the model again. Re-asking would cost a second request,
        # could answer differently, and would mean the operator confirmed
        # a plan that is not the one sent.
        "ai_sections": plan["sections"],
        "ai_model":    plan["ai_model"],
        "new_order":   new_order,
        **{k: rep[k] for k in
           ("anchored", "by_recall", "by_alias", "by_ai", "by_name",
            "unplaced", "headers_added", "headers_removed", "content_count",
            "placements", "out_of_order", "moved")},
    })


@bp.route("/api/update_playlist", methods=["POST"])
def api_update_playlist():
    """Weave the runsheet's headers into an existing playlist.

    Order is load-bearing. Every guard runs BEFORE the snapshot, the
    snapshot is on disk before the PUT, and the read-back happens
    whatever status code came back — a 400 is not a promise that
    nothing was applied."""
    import requests as req
    body = request.get_json(silent=True) or {}
    host = body.get("host") or "localhost"
    port = body.get("port") or "50001"
    base = pp_base(host, port)
    playlist_uuid = (body.get("playlist_uuid") or "").strip()
    matched = _runsheet(body)
    force = bool(body.get("force"))
    before = time.time()
    snap_path = None

    if not matched:
        return jsonify({"error": "Parse a runsheet first."}), 200

    def _abort(reason, message, **extra):
        log.info("Update refused (%s) — nothing written", reason)
        stats.track("playlist_update_failed", reason=reason,
                    items=len(matched))
        return jsonify({"ok": False, "error": message, "reason": reason,
                        **extra}), 200

    try:
        # No `use_ai` here on purpose: the write reuses the reading the
        # operator just confirmed in the preview. Calling the model again
        # could return a different answer than the one on screen.
        plan = _plan_update(base, playlist_uuid, matched,
                            sections=body.get("ai_sections"),
                            reorder=bool(body.get("reorder")))
        # From here on only ProPresenter's own id and name are used — in
        # the URL, the snapshot filename, the rollback and the logs. The
        # client's strings stop at _resolve_target.
        playlist_uuid, playlist_name = plan["uuid"], plan["name"]
        service_name = (body.get("name") or "").strip() or playlist_name

        # Guards. Each one aborts with NOTHING sent.
        if plan["no_change"]:
            return jsonify({"ok": True, "no_change": True,
                            "headers_added": plan["report"]["headers_added"]})
        if not force:
            active = safety.active_playlist_uuid(base)
            if active and active == playlist_uuid:
                return _abort("playlist_active",
                    "That playlist is live in ProPresenter right now, so "
                    "nothing was changed. Switch away from it first, or "
                    "choose Update anyway.")
        expect = body.get("expect_fingerprint")
        if expect is not None and expect != plan["fingerprint"]:
            return _abort("concurrent_edit",
                "ProPresenter changed while this was on screen, so nothing "
                "was changed. Press Update again to see the new plan.")

        # Snapshot, then write. From here on something may have changed
        # in ProPresenter, and every message has to be honest about it.
        snap_path = safety.write_snapshot(
            playlist_uuid, playlist_name, plan["raw"])
        r = req.put(f"{base}/v1/playlist/{playlist_uuid}",
                    json=plan["items"], timeout=10)
        http_ok = r.status_code < 400

        # Read back ALWAYS — a refusal is not proof that nothing landed.
        # Checked against what was SENT: the same slides, in the order
        # the operator chose (theirs, or the runsheet's if they said yes).
        after = fetch_pp_playlist_raw(base, playlist_uuid)
        if after is None:
            check = {"ok": False, "missing": [], "extra": [],
                     "reordered": False}
        else:
            check = verify_content_preserved(plan["items"], after)

        if not http_ok or not check["ok"]:
            # `check` carries media NAMES pulled straight out of
            # ProPresenter; a CR/LF in an asset name would forge a log
            # line, which is exactly what log_safe exists to stop.
            log.error("Update rejected or unverified (HTTP %s, ok=%s, "
                      "missing=%s, extra=%s, reordered=%s) — rolling back",
                      r.status_code, check["ok"],
                      log_safe(", ".join(check["missing"]), 200),
                      log_safe(", ".join(check["extra"]), 200),
                      check["reordered"])
            rb = safety.rollback(base, playlist_uuid, plan["raw"])
            if rb["verified"]:
                safety.mark_snapshot(snap_path, "rolled_back")
                stats.track("playlist_update_failed",
                            reason="rolled_back", items=len(matched))
                return jsonify({"ok": False, "rolled_back": True,
                    "rollback_verified": True,
                    "snapshot_path": str(snap_path),
                    "reason": "pp_refused" if not http_ok else "verify_failed",
                    "error":
                        "ProPresenter wouldn't accept the change, so your "
                        f"playlist was put back exactly as it was — all "
                        f"{plan['report']['content_count']} items, same "
                        "order, checked. Nothing was lost."})
            stats.track("playlist_update_failed", reason="rollback_failed",
                        items=len(matched))
            return jsonify({"ok": False, "rolled_back": True,
                "rollback_verified": False,
                "snapshot_path": str(snap_path),
                "snapshot_items": [
                    ((it.get("id") or {}).get("name") or "").strip()
                    for it in plan["raw"] if isinstance(it, dict)],
                "reason": "rollback_failed",
                "error":
                    "Something went wrong saving the playlist and it could "
                    "not be put back automatically. Don't close Runsheet "
                    "Pilot. A copy of the playlist as it was is saved on "
                    "this machine — the file is named below, and "
                    "ProPresenter's own autosave may still hold the "
                    "previous version too."}), 200

        safety.mark_snapshot(snap_path, "verified")
        safety.prune_snapshots()

        # Timers and clocks: both independent of the playlist write, and
        # both honour the same switches create mode does.
        timer_result = {"created": 0, "deleted": 0, "no_duration": 0,
                        "total_items": 0, "errors": [], "timer_names": {}}
        if body.get("create_timers"):
            timer_result = _create_pp_timers(
                base, service_name, matched,
                key_only=bool(body.get("timers_key_only")))
        _write_sm_state(service_name, matched, timer_result,
                        keep_position=True)

        rep = plan["report"]
        log.info("Playlist updated: %r → +%d headers (%d placed, %d marked), "
                 "%d old headers replaced, %d items preserved",
                 log_safe(playlist_name), rep["headers_added"],
                 rep["anchored"], rep["unplaced"], rep["headers_removed"],
                 rep["content_count"])
        stats.track("playlist_updated",
                    import_ms=int((time.time() - before) * 1000),
                    content_items=rep["content_count"],
                    headers_added=rep["headers_added"],
                    headers_removed=rep["headers_removed"],
                    anchored=rep["anchored"],
                    by_recall=rep["by_recall"],
                    by_alias=rep["by_alias"],
                    by_ai=rep["by_ai"],
                    unplaced=rep["unplaced"],
                    moved=rep["moved"],
                    timers=timer_result["created"])
        return jsonify({
            "ok":                 True,
            "content_preserved":  True,
            "snapshot_path":      str(snap_path),
            "headers_added":      rep["headers_added"],
            "headers_removed":    rep["headers_removed"],
            "anchored":           rep["anchored"],
            "by_recall":          rep["by_recall"],
            "by_alias":           rep["by_alias"],
            "by_ai":              rep["by_ai"],
            "by_name":            rep["by_name"],
            "unplaced":           rep["unplaced"],
            "content_count":      rep["content_count"],
            "moved":              rep["moved"],
            "timers_created":     timer_result["created"],
            "timers_deleted":     timer_result["deleted"],
            "timers_no_duration": timer_result["no_duration"],
            "timers_total_items": timer_result["total_items"],
            "timer_errors":       timer_result["errors"],
        })

    except UpdateAborted as e:
        return _abort(e.reason, e.message)
    except req.exceptions.ConnectionError:
        return _abort("pp_unreachable",
            f"Cannot connect to ProPresenter at {host}:{port}. Nothing was "
            "changed. Make sure ProPresenter is running and Network is "
            "enabled in Preferences → Integrations → Network.")
    except Exception as e:
        log.exception("Playlist update failed")
        stats.report_error(e, where_kind="route", route="update_playlist")
        # Never hand the exception text out — it carries paths and
        # internals and tells a volunteer nothing they can act on.
        return _abort("unexpected",
            "Something went wrong while updating that playlist."
            + (f" A backup was saved first: {snap_path}" if snap_path else
               " Nothing was changed."),
            snapshot_path=str(snap_path) if snap_path else "")


@bp.route("/api/restore_playlist", methods=["POST"])
def api_restore_playlist():
    """Put a snapshot back — the Undo behind the result notice.

    Wiping the operator's own section headers is the one part of update
    mode that cannot be undone by running it again, so the undo is a
    button rather than a support email."""
    body = request.get_json(silent=True) or {}
    base = pp_base(body.get("host") or "localhost",
                   body.get("port") or "50001")
    path = (body.get("snapshot_path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "No backup to restore."}), 200
    try:
        # load_snapshot only reads inside the backups folder — the path
        # came over HTTP, and see snapshot_file for what it could reach.
        snap = safety.load_snapshot(path)
    except ValueError:
        log.info("Restore refused a path outside the backups folder")
        return jsonify({"ok": False, "error":
            "That isn't one of Runsheet Pilot's playlist backups."}), 200
    except Exception:
        log.exception("snapshot read failed")
        return jsonify({"ok": False, "error":
            "That backup file couldn't be read."}), 200
    # The SNAPSHOT decides which playlist it goes back into — never the
    # dropdown. The result notice with its Undo button survives a change
    # of selection, so trusting the client's uuid meant undoing playlist
    # A after selecting playlist B would write A's contents over B. That
    # is the same class of harm this whole feature is built to avoid,
    # arrived at through the one control the operator reaches for when
    # something already went wrong.
    try:
        uuid = pp_id(snap.get("playlist_uuid"))
    except ValueError:
        return jsonify({"ok": False, "error":
            "That backup doesn't say which playlist it came from."}), 200
    asked = (body.get("playlist_uuid") or "").strip()
    if asked and asked != uuid:
        log.info("Restore target differs from the snapshot's playlist — "
                 "using the snapshot's")
        return jsonify({"ok": False, "error":
            "That backup is for a different playlist than the one now "
            "selected. Nothing was changed — reselect the playlist you "
            "updated, then undo."}), 200
    rb = safety.rollback(base, uuid, snap.get("items") or [])
    if rb["verified"]:
        safety.mark_snapshot(path, "rolled_back")
        n = len(snap.get("items") or [])
        return jsonify({"ok": True, "error": "", "restored": n, "message":
            f"Put back as it was — all {n} items, same order, checked."})
    return jsonify({"ok": False, "error":
        "ProPresenter didn't accept the restore. The backup file is still "
        "on this machine, so nothing is lost — try again in a moment."}), 200
