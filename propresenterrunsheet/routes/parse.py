"""Runsheet parsing routes.

/api/upload_and_parse takes the operator's PDF, extracts text, sends it
to OpenRouter with the (user-customised or default) prompt, parses the
JSON response, fills any per-role cue gaps, and seeds the Service Mate
runsheet state so the clocks start showing the items even before
playlist creation.

/api/match runs each parsed song title through fuzzy_match against the
library."""

import base64
import datetime as _dt
import json
import logging
import re
import threading
import time

from flask import Blueprint, jsonify, request

from ..config import APP_NAME, UPLOAD_FOLDER
from ..parsing.ai import (
    DEFAULT_PROMPT, assemble_prompt, canonicalize_item_type,
    parse_ai_response,
)
from ..parsing.models import (
    dollars, estimate_cost, fetch_catalogue, is_router, key_is_funded,
    model_reading, next_usable_model, provider_failure, resolve_model,
)
from ..parsing.openrouter import Stopped, chat
from .flags import matching_enabled
from ..parsing.ocr import (
    OCR_UNAVAILABLE_MESSAGE, OCRUnavailable, image_to_text, images_to_text,
)
from ..parsing.pdf import extract_pdf_text, pdf_text_or_images, render_pdf_pages
from ..parsing.timed_rows import rescue_missing_rows, service_header
from .. import stats
from ..propresenter.library import fuzzy_match
from ..propresenter.net import UnreachableHost, pp_base
from ..propresenter.templates import (
    auto_detect_template_uuid, fetch_pp_playlist_items, fetch_pp_playlists,
    link_items_to_template, playlist_to_objects, playlist_to_sections,
    resolve_section, resolve_with_aliases, template_candidates,
)
from ..service_mate.state import _ensure_item_cues, _write_runsheet_state
from ..logging_setup import log_safe
from ..settings import load_settings, save_settings


bp = Blueprint("parse", __name__)
log = logging.getLogger("pp_runsheet")

# What the upload accepts. PDFs go through pdfplumber; images (and PDFs
# pdfplumber can't read) go through local OCR. Deliberately NOT here:
# .docx and .doc — Word runsheets are almost always tables, which is a
# separate extraction problem, and HEIC, which needs another dependency
# for a case screenshots already cover.
PDF_EXTS = (".pdf",)
IMAGE_EXTS = (".png", ".jpg", ".jpeg")
ALLOWED_EXTS = PDF_EXTS + IMAGE_EXTS


class UploadError(ValueError):
    """A failure whose message was written FOR the operator.

    Exists so `_extracted_or_error` can tell OUR failures apart from a
    third-party library's. ocrmac raises ValueError too ("Invalid image
    format…"), and passing that straight to the UI leaks internals while
    telling the operator nothing they can act on.

    `_extracted_or_error` answers each subclass with a message it builds
    itself rather than reading one back out of the exception, so no
    exception text ever reaches the client (CodeQL
    py/stack-trace-exposure) — not even ours.
    """


class UnsupportedUpload(UploadError):
    """The file type isn't one we read."""


class UnreadablePdf(UploadError):
    """A PDF with no text layer and nothing we could rasterise."""


_UNREADABLE_PDF = ("Couldn't read any text from that PDF. If it's a scan, "
                   "try a clearer copy or upload a screenshot instead.")

# How long one model call may run before it is dropped. A runsheet takes a
# working model 5-15 s; a minute is a model stuck thinking (the owner's
# call, Sept 2026, after GPT-5 nano reasoned for ages).
_AI_TIMEOUT_S = 60

# Parses still running, by the id the page gave each one, so Start over
# can stop the model call (api_parse_cancel) instead of leaving it to
# finish, bill, and write the Service Mate state for a runsheet that's gone.
_running: dict = {}


def _parse_id(value) -> str:
    """The page's id for a parse — a UUID — or "" for anything else."""
    text = str(value or "")
    return text if len(text) <= 64 and text.replace("-", "").isalnum() \
        and text.isascii() else ""


def _unsupported_message(filename: str) -> str:
    return (f"{_display_ext(filename) or 'That file'} isn't supported. "
            "Upload a PDF, or a PNG or JPG screenshot of the runsheet.")


def _safe_ext(filename: str) -> str:
    """Return the whitelisted extension this filename ends with, or "".

    The return value is always a literal from `ALLOWED_EXTS` — never a
    slice of the caller's string. That matters because it is concatenated
    into a temp-file path: deriving it from user input is a path-injection
    hole, and the whitelist check alone leaves the safety implicit.
    """
    lowered = (filename or "").lower()
    for ext in ALLOWED_EXTS:
        if lowered.endswith(ext):
            return ext
    return ""


def _display_ext(filename: str) -> str:
    """A sanitised extension to quote back in an error message."""
    tail = (filename or "").rsplit(".", 1)[-1] if "." in (filename or "") else ""
    return "." + re.sub(r"[^A-Za-z0-9]", "", tail)[:10] if tail else ""


def _upload_to_text(upload):
    """Extract text from one uploaded file. Returns `(text, source)`.

    `source` is "pdf" when pdfplumber read embedded text, or "ocr" when
    the text came from a screenshot or a rasterised scan — the caller
    turns that into `needs_review`, because OCR is the only path where
    the operator should check the result before spending a request.

    Raises ValueError with an operator-facing message for anything that
    cannot be read, and OCRUnavailable on a platform with no OS engine.
    """
    ext = _safe_ext(upload.filename)
    if not ext:
        raise UnsupportedUpload(_unsupported_message(upload.filename))

    # Keep the real extension: ocrmac opens by path, and a .pdf suffix on
    # a PNG is a trap for whoever debugs this next.
    tmp_path = UPLOAD_FOLDER / f"runsheet_{int(time.time() * 1000)}{ext}"
    upload.save(str(tmp_path))
    try:
        if ext in PDF_EXTS:
            # extract_pdf_text is passed explicitly rather than left to
            # default, so tests (and the parse_client fixture) can swap
            # the module-level name and still be honoured here.
            text, pages = pdf_text_or_images(
                str(tmp_path), extract=extract_pdf_text,
                render=render_pdf_pages)
            if (text or "").strip():
                return text, "pdf"
            if not pages:
                raise UnreadablePdf(_UNREADABLE_PDF)
            return images_to_text(pages), "ocr"
        return image_to_text(str(tmp_path)), "ocr"
    finally:
        tmp_path.unlink(missing_ok=True)


# Bigger than any runsheet; a file past this isn't sent to the model whole.
_ATTACH_MAX = 8 * 1024 * 1024
_PDF_MIME = "application/pdf"
_MIME = {".pdf": _PDF_MIME, ".png": "image/png",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _attachment(upload):
    """The upload itself as `(mime type, bytes)`, for a model to read when
    our extracted text won't do. None when oversized or unreadable."""
    mime = _MIME.get(_safe_ext(upload.filename))
    if not mime:
        return None
    try:
        upload.stream.seek(0)
        data = upload.stream.read(_ATTACH_MAX + 1)
    except Exception:
        return None
    return (mime, data) if 0 < len(data) <= _ATTACH_MAX else None


def _file_reader(mime: str, current, api_key: str):
    """A model to read the file itself — `current` if it can — or None.
    Paid models only, so only on a key with credit."""
    reader = model_reading(fetch_catalogue(), current,
                           "file" if mime == _PDF_MIME else "image")
    return reader if reader and api_key and key_is_funded(api_key) else None


def _extracted_or_error(upload):
    """`_upload_to_text` with every failure mapped to a plain message.

    Returns `(text, source, error)`. An engine crash must never reach the
    operator as a stack trace — "Vision framework exploded" is not
    actionable at 9am on a Sunday.
    """
    try:
        text, source = _upload_to_text(upload)
    # Our own failures, each answered with a message built HERE rather
    # than read back out of the exception — see UploadError.
    except OCRUnavailable:
        return "", "", OCR_UNAVAILABLE_MESSAGE
    except UnsupportedUpload:
        return "", "", _unsupported_message(upload.filename)
    except UnreadablePdf:
        return "", "", _UNREADABLE_PDF
    except Exception:
        # Everything else — including a bare ValueError from ocrmac or
        # Pillow — is logged in full and replaced. Library text is not
        # actionable at 9am on a Sunday, and echoing it back leaks
        # internals to whoever can reach the port.
        log.exception("extraction failed for %s", log_safe(upload.filename))
        return "", "", ("Something went wrong reading that file. Try a PDF, "
                        "or a PNG screenshot of the runsheet.")
    if not (text or "").strip():
        return "", "", ("Couldn't read any text from that file. Try a bigger "
                        "or clearer screenshot.")
    return text, source, ""


@bp.route("/api/extract_text", methods=["POST"])
def api_extract_text():
    """Turn an upload into text, without spending an OpenRouter request.

    Split out of /api/upload_and_parse so the operator can SEE what was
    read off a screenshot and fix a misread before parsing. A free
    OpenRouter account gets 50 requests a day; burning one on a garbled
    OCR result is the failure this prevents.
    """
    upload = request.files.get("file") or request.files.get("pdf")
    if upload is None or not upload.filename:
        return jsonify({"error": "No file uploaded."}), 400

    text, source, error = _extracted_or_error(upload)
    if error:
        stats.track("extract_failed", kind=_safe_ext(upload.filename) or "none")
        return jsonify({"error": error}), 400

    # With credit on the key, a screenshot or a scan goes to the model as
    # the picture itself (see upload_and_parse), so there is no OCR text to
    # check and no request to save.
    model_reads = source == "ocr" and bool(_file_reader(
        _MIME.get(_safe_ext(upload.filename), ""), None,
        ((load_settings() or {}).get("or_key") or "").strip()))
    needs_review = source == "ocr" and not model_reads
    stats.track("runsheet_uploaded", source=source,
                needs_review=needs_review, chars=len(text))
    if source == "ocr":
        stats.track("ocr_used", chars=len(text),
                    kind=_safe_ext(upload.filename) or "none")

    log.info(f"Extracted {len(text)} chars from "
             f"{log_safe(upload.filename)} via {source}")
    return jsonify({
        "text":         text,
        "source":       source,
        # Only OCR output is worth a human's eyes. A text PDF is exact,
        # so showing a review panel for it would add a click to the
        # path every Sunday runsheet takes.
        "needs_review": needs_review,
        "model_reads":  model_reads,
        "filename":     upload.filename,
    })


def _unusable_reply_message(used_model: str, snippet: str, what: str) -> str:
    """Explain that a model answered but not with a runsheet.

    Names the model that actually replied and quotes it, because the two ways
    this fails are indistinguishable otherwise: a model that simply isn't up to
    the job, versus a router that happened to pick one that isn't. The router
    case gets an extra line, since "it worked last time" is the confusing part
    — `openrouter/free` chooses a different model on every request, so the same
    settings genuinely do succeed and fail at random.
    """
    msg = f"The model '{used_model}' {what}"
    if snippet:
        msg += f' — it replied: "{snippet}"'
    msg += ". "
    if is_router(model_id=used_model):
        msg += ("That id picks a different model at random each time, so it "
                "will keep failing intermittently. ")
    msg += "Open Settings and choose a model from the list."
    return msg


def _rejects_response_format(resp) -> bool:
    """True when a 400 is the provider refusing our `response_format`.

    Deliberately narrow: only a message that names the parameter (or its
    concept) counts, so an unrelated 400 — context length, malformed
    request — is never silently masked by a second attempt.
    """
    try:
        err = (resp.json() or {}).get("error") or {}
        text = str(err.get("message") or "")
    except Exception:
        text = getattr(resp, "text", "") or ""
    text = text.lower()
    return any(k in text for k in (
        "response_format", "response format", "json_object", "json mode",
        "structured output"))


def _provider_failure_message(model: str, failure: dict,
                              backup: str = None,
                              backup_failure: dict = None) -> str:
    """Tell the operator the truth: the model's provider broke, not their key.

    Sending someone to rotate a working key is the worst kind of wrong — the
    "fix" changes nothing, so they conclude the app itself is broken. Name
    whose fault it is, and when the automatic backup failed too, name that
    as well so "pick a different model" doesn't send them straight to the
    one we already tried.
    """
    msg = (f"The service behind '{model}' is having problems right now "
           f"(provider {failure['provider']} returned {failure['code']}). ")
    if backup and backup_failure:
        msg += (f"A backup model '{backup}' failed too (provider "
                f"{backup_failure['provider']} returned "
                f"{backup_failure['code']}). ")
    msg += ("Your API key is fine — try again in a minute, or pick a "
            "different model in Settings.")
    return msg


def _rate_limit_message(resp) -> str:
    """Turn OpenRouter's 429 into instructions a volunteer can act on.

    Measured live 2026-08-03: the free tier allows 50 free-model requests
    per DAY per ACCOUNT (metadata.limit_source
    "openrouter_free_tier_daily"). Because it is account-wide, swapping to
    a different API key on the same account — the operator's natural first
    move — changes nothing, and neither does picking a different free
    model. The raw "429 Client Error" invited exactly that wasted effort.
    """
    import datetime as dt
    limit_source, reset_ms = "", None
    try:
        meta = (resp.json().get("error") or {}).get("metadata") or {}
        limit_source = meta.get("limit_source") or ""
        reset_ms = int((meta.get("headers") or {}).get("X-RateLimit-Reset"))
    except Exception:
        # Whatever was read before the failure still picks the wording
        # below. One INFO line (the logger runs at INFO, so DEBUG would be
        # dropped) so a change to OpenRouter's 429 body shows up in the
        # log; no traceback, since this can repeat on every limited parse.
        log.info("429 body had no usable rate-limit metadata")
    if "daily" in limit_source:
        when = "tomorrow"
        if reset_ms:
            try:
                when = dt.datetime.fromtimestamp(reset_ms / 1000).strftime(
                    "%-I:%M %p tomorrow" if dt.datetime.fromtimestamp(
                        reset_ms / 1000).date() != dt.date.today()
                    else "%-I:%M %p today")
            except Exception:
                # "%-I" is not portable (Windows rejects it); the message
                # falls back to plain "tomorrow".
                log.debug("Could not format the rate-limit reset time",
                          exc_info=True)
        return ("You've used all 50 free AI requests for today — OpenRouter's "
                "free tier daily limit, shared across every "
                "API key on your account, so a different key or model "
                f"won't help. The counter resets at {when}.")
    if "min" in limit_source:
        return ("OpenRouter is rate-limiting free models right now — "
                "wait a minute, then click Parse again.")
    return ("OpenRouter is receiving too many requests at the moment "
            "(rate limited). Wait a little and try again.")


@bp.route("/api/upload_and_parse", methods=["POST"])
def api_upload_and_parse():
    import requests as req

    # 1. Validate request. Two ways in: a file, or text the operator has
    #    already reviewed and corrected in the OCR panel. Reviewed text
    #    wins when both arrive — it IS the corrected version of the file.
    reviewed_text = (request.form.get("runsheet_text") or "")
    upload = request.files.get("pdf") or request.files.get("file")
    upload_name = (request.form.get("filename") or "").strip()
    if upload is not None and upload.filename and not upload_name:
        upload_name = upload.filename

    if "runsheet_text" in request.form and not reviewed_text.strip():
        # The operator cleared the textarea. Parsing an empty runsheet
        # would spend one of a free account's 50 daily requests to be
        # told there is nothing in it.
        return jsonify({"error":
            "There's no runsheet text to parse. Paste or re-upload the "
            "runsheet and try again."}), 400

    if not reviewed_text.strip() and (upload is None or not upload.filename):
        return jsonify({"error": "No runsheet uploaded"}), 400

    # Whether to link items to ProPresenter at all. Off means headers
    # only — see matching_enabled().
    do_matching = matching_enabled(request.form)

    # 3. Resolve API key + model (form values override saved settings)
    settings = load_settings()
    or_key = (request.form.get("or_key") or settings.get("or_key") or "").strip()
    configured = (request.form.get("or_model")
                  or settings.get("or_model") or "").strip()
    # Blank means "pick one for me". Also rescues installs still holding a
    # model id that OpenRouter has since retired. The catalogue is cached for
    # hours and the fetch fails soft, so this costs one HTTP round-trip on the
    # first parse after launch and nothing afterwards. A key with credit runs
    # on a paid model (see resolve_model).
    model = resolve_model(configured, fetch_catalogue(), api_key=or_key)

    if not or_key:
        return jsonify({"error": "OpenRouter API key required."}), 400

    if not model:
        return jsonify({"error":
            "No AI model is set, and the list of free models could not be "
            "reached. Check your internet connection, or set a model "
            "manually in Settings."}), 400

    # Bound before the try so the error handlers can name the model that
    # actually answered and quote what it said. `used_model` diverges from
    # `model` whenever the operator points at a router id like
    # `openrouter/free`, which dispatches to a different underlying model on
    # every request — without this the logs only ever showed "openrouter/free"
    # and a misbehaving model was impossible to identify.
    used_model = model
    content = ""
    attachment = lead = None     # the file itself; `lead` when it goes first
    read_from = ""               # "PDF" / "picture" when the model read it
    parse_id, stop = _parse_id(request.form.get("parse_id")), threading.Event()
    if parse_id:
        _running[parse_id] = stop

    try:
        # 4. Get the runsheet text. Either the operator already reviewed
        # it (screenshot / scan, corrected in the panel) or we extract it
        # from the upload now — which for a text PDF is the same
        # pdfplumber call this route has always made.
        if reviewed_text.strip():
            raw = reviewed_text
        else:
            raw, source, error = _extracted_or_error(upload)
            if error:
                return jsonify({"error": error}), 400
            attachment = _attachment(upload)
            # A screenshot or a scan, on a key with credit: the model reads
            # the picture itself. Local OCR loses what a table shows plainly
            # (small grey numbers, seen live), and there is no request to
            # save by making someone check its text first.
            reader = (_file_reader(attachment[0], model, or_key)
                      if source == "ocr" and attachment else None)
            if reader:
                lead, model = attachment, reader
                used_model = model

        # 5. Assemble the prompt — user-customised or default, plus the
        # Service Mate cue addendum so the model also emits per-role cues.
        prompt_template = (settings.get("ai_prompt") or "").strip() or DEFAULT_PROMPT
        runsheet_text = raw[:7000]

        # 5a. If the operator has (or we can auto-detect) a "template
        # playlist" in PP — typically named "<Service> - Library" —
        # fetch it and feed the section header names into the prompt.
        # The model can then tag runsheet items with a section name; at
        # playlist-build time we expand each tagged item into that
        # section's full media list, so the operator doesn't drag the
        # same "Culture" / "Welcome" / "Worship" slides in every week.
        # Best-effort: any failure here (PP not running, no playlists,
        # template gone) drops back to parse-without-template.
        #
        # Skipped entirely when the operator turned "Populate with media
        # from PP" off: a brand-new event has no template and no reusable
        # media, so this whole block is round-trips for nothing. Skipping
        # also means parse works with ProPresenter closed, and on a
        # 1,261-item library that is a real speed difference.
        sections: list = []
        objects: list = []
        pp_host = (settings.get("pp_host") or "localhost").strip()
        pp_port = (settings.get("pp_port") or "50001").strip()
        try:
            base = pp_base(pp_host, pp_port)
        except UnreachableHost:
            # Saved host is outside loopback/LAN (settings are writable
            # over the API, so this is the second-order path). Parse
            # must still work — it already does with PP closed — so
            # just skip everything that would have talked to PP.
            log.warning("Saved ProPresenter host refused; parsing "
                        "without template context")
            base = ""
            do_matching = False
        tmpl_uuid = (settings.get("template_playlist_uuid") or "").strip()
        # A uuid from settings is the operator PINNING the dropdown — an
        # explicit instruction, never second-guessed by the confirmation
        # pass below. Only an Auto pick is ours to revise.
        tmpl_pinned = bool(tmpl_uuid)
        # None means "not fetched yet" — distinct from [] ("PP has no
        # playlists"), so the confirmation pass below doesn't re-ask a
        # ProPresenter that already answered.
        pp_playlists = None
        if not do_matching:
            tmpl_uuid = ""
        elif not tmpl_uuid:
            # Auto-pick the template based on runsheet content. The hint
            # combines filename + the start of the extracted text — both
            # usually say "youth" / "sunday" / "wednesday" / etc., which
            # lets the picker route a youth runsheet to "Youth Service -
            # Library" and a sunday runsheet to "Sunday Morning Library"
            # automatically. Fall back to the first template-named
            # playlist on tie or no signal.
            # This runs BEFORE the model has read anything, so the hint is
            # whatever the raw text can prove: the filename and the
            # runsheet's MASTHEAD — the lines above the first timed row,
            # where it names itself ("Youth Service : EVANGELISM 101").
            #
            # It used to be the first 500 characters, which is the body as
            # much as the header. That let one row's notes decide the
            # template: a young adults runsheet with "THIS IS YOUTH" in a
            # setup note scored a confident hit on the youth library.
            # The masthead is the runsheet saying what it IS; the body is
            # it saying what happens. Only the first one answers this
            # question.
            #
            # Precise rather than generous on purpose: a runsheet with no
            # masthead now hints on the filename alone and may resolve to
            # nothing, and the confirmation pass below — which has the
            # model's own reading of the service — is what recovers it.
            detect_hint = " ".join(filter(None, [
                upload_name, service_header(raw)]))
            try:
                pp_playlists = fetch_pp_playlists(base)
                tmpl_uuid = auto_detect_template_uuid(
                    pp_playlists, hint=detect_hint) or ""
            except Exception:
                log.exception("template auto-detect failed; continuing without")
        if tmpl_uuid:
            try:
                raw_items = fetch_pp_playlist_items(base, tmpl_uuid)
                sections = playlist_to_sections(raw_items)
                # Item-level repository view of the same playlist. Real
                # operators often build the template FLAT — one named object
                # per reusable thing (Welcome slide, Countdown loop) with no
                # headers at all — which yields zero sections above. Objects
                # are matched per runsheet item further down.
                objects = playlist_to_objects(raw_items)
            except Exception:
                log.exception("template playlist fetch failed; "
                              "continuing without template context")
        section_names = [s["header"]["name"] for s in sections
                         if s.get("header") and s["header"].get("name")]

        prompt = assemble_prompt(prompt_template, runsheet_text,
                                 library_names=section_names)

        # 6. Call OpenRouter
        # Specific 4xx responses become friendly JSON errors (HTTP 200 so the
        # JS reads the message); everything else falls through to raise_for_status
        # and surfaces as a generic 500. But first: any error status can be
        # OpenRouter relaying its *provider's* failure (see provider_failure)
        # — that is not the operator's key/credit/model-id problem, so it gets
        # one retry on the next-ranked free model and an honest message,
        # before the per-status mapping below gets a chance to misdiagnose it.
        def _openrouter_post(model_id, json_mode=True, attach=None):
            log.info(f"OpenRouter request: model={log_safe(model_id)}, "
                     f"raw_chars={len(raw)}, json_mode={json_mode}, "
                     f"file={attach[0] if attach else None}")
            content_ = prompt
            if attach:
                # The model reads the file itself instead of our extracted
                # text — a picture from step 4, or a PDF in step 7a.
                mime, data = attach
                url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
                content_ = [
                    {"type": "text", "text": assemble_prompt(
                        prompt_template, f"(The runsheet is the attached "
                        f"{'PDF' if mime == _PDF_MIME else 'picture'}.)",
                        library_names=section_names)},
                    {"type": "file", "file": {"filename": "runsheet.pdf",
                                              "file_data": url}}
                    if mime == _PDF_MIME else
                    {"type": "image_url", "image_url": {"url": url}}]
            body = {
                "model":       model_id,
                "messages":    [{"role": "user", "content": content_}],
                "temperature": 0.1,
                # Ask for the real billed cost of this call. Free models
                # report 0, so this is the honest answer to "is the paid
                # model worth it?" rather than an estimate from a
                # pricing table.
                "usage":       {"include": True},
                # Runsheets carry people's names. Only route to providers
                # that neither store nor train on requests — the owner's
                # call, tested live (Sept 2026) on GPT-4.1 mini, Claude
                # Haiku, OpenRouter Auto, and a PDF and a picture.
                "provider":    {"data_collection": "deny"},
            }
            # JSON mode. The model picker has always filtered for models
            # that advertise structured output, but the request never
            # ASKED for it — so a compliant model was still free to wrap
            # the answer in prose or markdown fences, one of the two ways
            # a parse fails outright. Asking costs nothing and removes
            # that failure mode on every model that honours it.
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            if attach and attach[0] == _PDF_MIME:
                body["plugins"] = [{"id": "file-parser",
                                    "pdf": {"engine": "native"}}]
            # Streamed so it can be dropped: after _AI_TIMEOUT_S, or when
            # the operator starts over (see openrouter.chat).
            return chat(
                req.post, "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization":  f"Bearer {or_key}",
                    "HTTP-Referer":   "runsheet-pilot",
                    "X-Title":        APP_NAME,
                    "Content-Type":   "application/json",
                },
                body=body, timeout_s=_AI_TIMEOUT_S, stop=stop)

        ai_t0 = time.time()
        refused_json = None          # a model that 400'd on response_format
        resp = _openrouter_post(model, attach=lead)
        # Some free-tier providers advertise structured output and still
        # 400 on `response_format`. That is OUR parameter being refused,
        # not the operator's key or model — so retry the same model once,
        # plainly; the regex-tolerant parser copes with unfenced-or-not
        # replies exactly as it did before JSON mode existed. Only a 400
        # that names the parameter earns this; an unrelated 400 falls
        # through to the normal error handling below.
        if resp.status_code == 400 and _rejects_response_format(resp):
            log.info(f"{log_safe(model)} rejected response_format — "
                     f"retrying without JSON mode")
            refused_json = model
            resp = _openrouter_post(model, json_mode=False, attach=lead)
        failure = provider_failure(resp)
        if failure:
            backup = next_usable_model(model, fetch_catalogue())
            if not backup:
                stats.track("parse_failed", reason="provider", model=model,
                            code=int(failure.get("code") or 0))
                return jsonify({"error":
                    _provider_failure_message(model, failure)}), 200
            log.warning(f"Provider behind {log_safe(model)} failed "
                        f"({log_safe(failure['provider'])} returned "
                        f"{failure['code']}) — retrying with {log_safe(backup)}")
            resp = _openrouter_post(backup)
            backup_failure = provider_failure(resp)
            if backup_failure:
                stats.track("parse_failed", reason="provider_both",
                            model=model, code=int(
                                backup_failure.get("code") or 0))
                return jsonify({"error": _provider_failure_message(
                    model, failure, backup, backup_failure)}), 200
            # The backup answered; from here on it is the model of record —
            # any later error message must name the model that actually
            # produced the response. It was sent our OCR text, not the
            # picture: a free backup can't read one.
            model = used_model = backup
            lead = None

        if resp.status_code == 429:
            stats.track("parse_failed", reason="rate_limit", model=used_model)
            return jsonify({"error": _rate_limit_message(resp)}), 200
        if resp.status_code == 401:
            return jsonify({"error":
                "OpenRouter rejected the API key (401). "
                "Check the key in the sidebar."}), 200
        if resp.status_code == 402:
            return jsonify({"error":
                "OpenRouter says this account has no credit / model is paid (402). "
                "Try a different model — a free one is in the sidebar by default."}), 200
        if resp.status_code == 404:
            return jsonify({"error":
                f"OpenRouter says model '{model}' not found (404). "
                "Check the model id at openrouter.ai/models."}), 200
        resp.raise_for_status()

        # 7. Parse the AI response — strips markdown fences, accepts either
        # {service_name, items} (preferred) or a bare items array.
        body = resp.json()
        # OpenRouter echoes the model that actually served the request. For a
        # plain model id it matches what we asked for; for a router it names
        # the model the router chose.
        used_model = body.get("model") or model
        # What the call cost. Preference order matters: OpenRouter's own
        # billed figure is the truth, and the estimate from catalogue
        # pricing is the fallback for providers that don't report one —
        # labelled, so a dashboard never mixes a real number with a
        # guess and presents them as the same thing.
        try:
            spent = float((body.get("usage") or {}).get("cost"))
            cost_source = "billed"
        except (TypeError, ValueError):
            spent, cost_source = None, "unknown"
        if spent is None:
            known = {m.get("id"): m for m in
                     (fetch_catalogue() or {}).get("data") or []
                     if isinstance(m, dict)}
            guess = estimate_cost(known.get(used_model))
            if guess is not None:
                spent, cost_source = guess, "estimated"
        if used_model != model:
            log.info(f"OpenRouter routed {log_safe(model)} -> {log_safe(used_model)}")
        content = (body["choices"][0]["message"].get("content") or "")
        try:
            items, service_name, service_type = parse_ai_response(content)
            unreadable = None
        except json.JSONDecodeError as e:
            # Kept, not raised yet: reading the file itself (7a) may rescue it.
            items, service_name, service_type, unreadable = [], "", "", e

        # 7a. The text gave nothing. A key with credit gets a second look,
        # at the file itself: extraction flattens tables and columns, and
        # the page layout is often what the model needed. Only ever on a
        # failed parse, so a working runsheet never pays for it twice.
        second = (_file_reader(attachment[0], used_model, or_key)
                  if not items and attachment and not lead else None)
        rescued = False
        if second:
            log.info(f"No items from the text — reading the file itself "
                     f"with {log_safe(second)}")
            try:
                # A refusal, or a provider error dressed as a 200, has no
                # choices — so it parses to nothing and changes nothing.
                again = _openrouter_post(second, json_mode=second != refused_json,
                                         attach=attachment)
                file_body = again.json() if again.status_code < 400 else {}
                file_content = (((file_body.get("choices") or [{}])[0]
                                 .get("message") or {}).get("content") or "")
                got = parse_ai_response(file_content)
            except Exception:
                log.info("Reading the file itself failed", exc_info=True)
                got = ([], "", "")
            if got[0]:
                items, service_name, service_type = got
                content, rescued, lead = file_content, True, attachment
                extra = dollars((file_body.get("usage") or {}).get("cost"))
                if extra is not None:
                    # One runsheet, two calls: a total only when both were
                    # billed to the same model, else the file read's own cost.
                    same = second == used_model
                    spent = extra + ((spent or 0.0) if same else 0.0)
                    cost_source = cost_source if same else "billed"
                used_model = file_body.get("model") or second
        if unreadable is not None and not rescued:
            raise unreadable
        if lead:
            read_from = "PDF" if lead[0] == _PDF_MIME else "picture"

        # A reply can be perfectly valid JSON and still not be a runsheet —
        # `{"safety": "safe"}` parses fine and yields zero items. Without this
        # guard the route treated that as success and fell through to the
        # Service Mate state seed below, which is an unconditional overwrite:
        # a junk parse silently wiped the live clock state mid-service.
        if not items:
            snippet = content.strip().replace("\n", " ")[:160]
            log.error(f"AI returned no runsheet items. model={log_safe(used_model)} "
                      f"reply={log_safe(snippet)!r}")
            stats.track("parse_failed", reason="no_items", model=used_model)
            return jsonify({"error": _unusable_reply_message(
                used_model, snippet, "returned no runsheet items")}), 200

        # 7b. The timed-row guard. On the 14 Aug 2026 runsheet the model
        # dropped the three pre-service rows (their notes held a
        # volunteer roster, which looks like the credits block the prompt
        # says to skip) — with the prompt ALREADY forbidding exactly
        # that, so firmer wording is not a fix. Every timed row in the
        # raw text must come back; any the model lost is synthesized and
        # slotted in by time. Runs BEFORE the matching loop below so a
        # rescued "Youth Arrival + Hangout" still picks up its template
        # link like any parsed item.
        items, rescued_rows = rescue_missing_rows(items, raw)
        if rescued_rows:
            log.warning(f"Model dropped {rescued_rows} timed row(s); "
                        f"restored from raw text. model={log_safe(used_model)}")
            # How often the guard has to fire IS the measure of model
            # quality — the number to watch when picking a paid model.
            stats.track("rows_rescued", count=rescued_rows,
                        model=used_model, items=len(items))

        # 7c. Re-resolve the template now that the model has told us WHICH
        # SERVICE this is. The pick above was made before anything had read
        # the runsheet — filename plus the first 500 characters — which is
        # weak evidence in both directions: it misses a youth runsheet
        # whose filename says nothing, and it matches on a stray "youth"
        # in a young adults runsheet. A label the model assigns after
        # reading the whole document does neither.
        #
        # This is also what stops the three Auto call sites disagreeing.
        # Parse, /api/match and create each used to build their own hint
        # from whatever they had to hand; they now all resolve from this
        # one label, so create can't silently re-attach a template parse
        # correctly declined. It costs one extra field in a reply we are
        # already paying for — no second round-trip.
        #
        # A pinned dropdown is an explicit instruction and is left alone.
        #
        # `service_name` is the fallback hint: a customised prompt or a
        # model that ignores the new field leaves service_type empty, and
        # without a second source of evidence those users would lose
        # template matching altogether now that a weak hint declines
        # instead of guessing. The name nearly always carries the service
        # words too ("Sunday Service — 3 May 2026").
        confirm_hint = (service_type or service_name or "").strip()
        if do_matching and not tmpl_pinned and confirm_hint and base:
            try:
                if pp_playlists is None:
                    pp_playlists = fetch_pp_playlists(base)
                confirmed = auto_detect_template_uuid(
                    pp_playlists, hint=confirm_hint) or ""
            except Exception:
                log.exception("template confirmation failed; keeping the "
                              "parse-time pick")
                confirmed = tmpl_uuid
            if confirmed != tmpl_uuid:
                log.info("Model read the service as %r — template %s -> %s",
                         log_safe(confirm_hint),
                         tmpl_uuid or "(none)", confirmed or "(none)")
                tmpl_uuid, sections, objects = confirmed, [], []
                if tmpl_uuid:
                    # Adopted a template the pre-AI hint couldn't reach.
                    # Its section names never made it into the prompt, so
                    # the model tagged nothing — but the deterministic
                    # title match in the loop below still links items.
                    try:
                        raw_items = fetch_pp_playlist_items(base, tmpl_uuid)
                        sections = playlist_to_sections(raw_items)
                        objects = playlist_to_objects(raw_items)
                    except Exception:
                        log.exception("revised template fetch failed; "
                                      "continuing without template context")

        # Templates existed but none of them is for this service. Worth
        # telling the operator, because the playlist they are about to
        # build has no template media in it and they should know why.
        # NOT an error: a brand-new event legitimately has no template,
        # and a ProPresenter with no templates at all says nothing.
        template_declined = bool(
            do_matching and not tmpl_pinned and not tmpl_uuid
            and template_candidates(pp_playlists or []))
        tmpl_name = next((p.get("name", "") for p in (pp_playlists or [])
                          if p.get("uuid") == tmpl_uuid), "") if tmpl_uuid else ""

        # 8. If the AI didn't supply a service name, derive one from the filename
        if not service_name and upload_name:
            stem = re.sub(r"\.(pdf|png|jpe?g)$", "", upload_name,
                          flags=re.IGNORECASE)
            service_name = re.sub(r"[_]+", " ", stem).strip()

        # Fill any per-role cue gaps from the rule table so every item has
        # cues for the Service Mate clocks. Also resolve any `library_match`
        # name the model emitted back to a real section dict (header +
        # media items), so build_playlist_payload can expand it into the
        # template's slides. Hallucinated names (no section hit) get
        # dropped to None and the item falls back to existing paths.
        resolved_section_hits = 0
        resolved_object_hits = 0
        # Section headers as matchable pseudo-objects: a title hit on the
        # header name expands that whole section. link_items_to_template
        # has always done this (its comment even says "exactly as at parse
        # time") — parse itself did not, so the same runsheet could link
        # differently depending on whether it went through parse or the
        # create-time rescue. It matters more now: when the template is
        # adopted AFTER the model replies, its section names were never in
        # the prompt, so nothing is tagged and title matching is the only
        # way in.
        header_objects = [{"name": s_["header"]["name"], "_section": s_}
                          for s_ in sections
                          if s_.get("header") and s_["header"].get("name")]
        for it in items:
            if not isinstance(it, dict):
                continue
            # Clamp the type to the fixed list FIRST — everything after
            # this point (cue lookup, song-vs-object routing, tag colours,
            # timer creation) keys off it, and the model demonstrably
            # invents types no matter what the prompt says.
            raw_type = it.get("type")
            it["type"] = canonicalize_item_type(raw_type)
            if raw_type != it["type"]:
                log.info(f"Item type clamped: {log_safe(raw_type, 60)!r} -> "
                         f"{it['type']!r} ({log_safe(it.get('title'), 40)!r})")
            _ensure_item_cues(it)
            raw_match = it.get("library_match")
            # The model sometimes returns the full dict, sometimes a bare
            # string, sometimes null, sometimes the literal "null" str.
            # We only care about the string case — resolve it to a section.
            name = ""
            if isinstance(raw_match, str):
                name = raw_match.strip()
                if name.lower() in ("", "null", "none"):
                    name = ""
            elif isinstance(raw_match, dict):
                # Defensive: some models try to be helpful and return a
                # {"name": "Culture"} dict instead of the bare string.
                name = (raw_match.get("name") or "").strip()
            section = resolve_section(name, sections) if name else None
            if section:
                it["library_match"] = section
                resolved_section_hits += 1
                continue
            # Title hit on a section header — same rule, no tag needed.
            # Songs excluded for the same reason as objects below.
            hdr = (resolve_with_aliases(it.get("title", ""), header_objects,
                                        settings.get("template_aliases"))
                   if it.get("type") != "song" else None)
            if hdr and hdr.get("_section"):
                it["library_match"] = hdr["_section"]
                resolved_section_hits += 1
                continue
            # Item-level fallback: match the runsheet title against the
            # template's named objects ("Welcome and Connection Cards" →
            # the "Welcome" slide). Wrapped in the SECTION shape — header
            # + one item — because everything downstream (the /api/match
            # passthrough, the ♻ render in the UI, build_playlist_payload's
            # expander with its PP asset-UUID rules) already handles that
            # shape; the generated playlist keeps the runsheet's own
            # coloured header with the template object underneath.
            # Songs are deliberately excluded: they belong to the
            # fuzzy-match + Pick flow, and a template slide named
            # "Worship" must not hijack a song titled "Worship Medley".
            obj = (resolve_with_aliases(it.get("title", ""), objects,
                                        settings.get("template_aliases"))
                   if it.get("type") != "song" else None)
            if obj:
                it["library_match"] = {
                    "header": {"name": obj["name"], "uuid": obj["uuid"],
                               "color": {}},
                    "items":  [obj],
                }
                resolved_object_hits += 1
            else:
                it["library_match"] = None
        if sections or objects:
            log.info(f"Template-context parse: "
                     f"{resolved_section_hits} section + "
                     f"{resolved_object_hits} object links across "
                     f"{len(items)} items (template: {len(sections)} "
                     f"sections, {len(objects)} objects)")

        # Started over while this ran: the page has moved on, so nothing
        # below — the clocks' state, the cost, the stats — is for anyone.
        if stop.is_set():
            raise Stopped("cancelled")

        # Also seed the Service Mate runsheet state on parse — so the user can
        # test the clock cue flow without going through Create Playlist (which
        # requires ProPresenter to be running). Create Playlist later overwrites
        # this with the timer-name-stamped version for auto-track.
        try:
            sm_state = {
                "service_name":       service_name or upload_name or "Runsheet",
                "items":              items,
                "current_index":      0,
                "current_started_at": _dt.datetime.now().isoformat(),
                "auto_track":         {"enabled": True},
            }
            _write_runsheet_state(sm_state)
            log.info(f"Service Mate state seeded from parse: {len(items)} items")
        except Exception:
            log.exception("Service Mate parse-time state write failed")

        # `model` is the one that ACTUALLY answered — for a router that
        # is the model it dispatched to, not the router's own id, which
        # is the whole point of recording it. `chosen` says whether a
        # human picked it or Automatic did, so adoption of the
        # recommendation is visible; `paid` and `cost_usd` answer
        # "was paying for it worth it?" with billing, not a guess.
        stats.track("parse_completed",
                    ai_ms=int((time.time() - ai_t0) * 1000),
                    items=len(items),
                    songs=sum(1 for i in items
                              if isinstance(i, dict) and i.get("type") == "song"),
                    model=used_model,
                    chosen="auto" if not configured else "pinned",
                    paid=bool(spent),
                    cost_usd=round(spent, 6) if spent is not None else -1,
                    cost_source=cost_source,
                    rescued=rescued_rows,
                    template_links=resolved_section_hits + resolved_object_hits,
                    source="text" if reviewed_text.strip() else "file",
                    read_from=read_from,
                    matching=do_matching,
                    # How often Auto has to say "none of these are for
                    # this service" — the measure of whether the decline
                    # rule is earning its place or over-firing. A bool,
                    # never the service label: that is church content.
                    template_declined=template_declined)
        # What this parse really cost, so Settings can say what a runsheet
        # costs on each model: billed figures only, the last 20. Bookkeeping
        # — it must never turn a parse that worked into an error.
        if cost_source == "billed" and dollars(spent) is not None:
            try:
                kept = load_settings().get("parse_costs")
                save_settings({"parse_costs": (
                    (kept if isinstance(kept, list) else [])
                    + [{"model": used_model, "usd": round(spent, 8)}])[-20:]})
            except Exception as e:
                log.info("Could not record the parse cost (%s)", type(e).__name__)

        log.info(f"AI parsed {len(items)} runsheet items, "
                 f"suggested name: {log_safe(service_name)!r}")
        return jsonify({
            "items":          items,
            "rescued_rows":   rescued_rows,
            "read_from":      read_from,
            "filename":       upload_name,
            "suggested_name": service_name,
            # The template verdict, resolved ONCE here and carried by the
            # client into /api/match and /api/create_playlist so those
            # steps never re-derive it and reach a different answer.
            # `service_label` doubles as the shared hint and as the words
            # the banner uses ("No template for Young Adults").
            "template": {
                "uuid":          tmpl_uuid,
                "name":          tmpl_name,
                "declined":      template_declined,
                "service_label": service_type,
            },
        })

    except json.JSONDecodeError:
        # The operator used to see the raw decoder message here ("Expecting
        # value: line 1 column 1 (char 0)"), which told them nothing. What
        # they need is which model answered and what it actually said.
        snippet = (content or "").strip().replace("\n", " ")[:160]
        log.error(f"AI returned non-JSON. model={log_safe(used_model)} "
                  f"reply={log_safe(snippet)!r}")
        stats.track("parse_failed", reason="not_json", model=used_model)
        return jsonify({"error": _unusable_reply_message(
            used_model, snippet, "didn't return a runsheet")}), 200
    except Stopped as e:
        if e.reason == "cancelled":
            log.info("Parse stopped: the operator started over")
            return jsonify({"cancelled": True,
                            "error": "Stopped — you started over."}), 200
        stats.track("parse_failed", reason="timeout", model=used_model)
        return jsonify({"error":
            f"{used_model} was still working after {_AI_TIMEOUT_S} seconds, "
            "so it was stopped. Try again, or pick a faster model in "
            "Settings."}), 200
    except req.exceptions.Timeout:
        stats.track("parse_failed", reason="timeout", model=used_model)
        return jsonify({"error":
            "OpenRouter request timed out. Try again, or pick a faster model."}), 200
    except req.exceptions.ConnectionError:
        # Before the catch-all below, because "no internet" is the one
        # failure with an obvious fix — and the catch-all's generic
        # wording would send the operator off to change models instead.
        stats.track("parse_failed", reason="offline", model=used_model)
        return jsonify({"error":
            "Couldn't reach OpenRouter. Check this computer's internet "
            "connection, then try again."}), 200
    except Exception as e:
        # The full detail goes to the log. The operator gets a message
        # they can act on — raw exception text is library internals (and
        # sometimes URLs) that mean nothing on a Sunday morning.
        log.exception("Parse failed")
        stats.report_error(e, where_kind="route", route="upload_and_parse")
        return jsonify({"error":
            "Something went wrong while parsing the runsheet. Try again "
            "in a moment, or pick a different model in Settings if it "
            "keeps failing."}), 500
    finally:
        _running.pop(parse_id, None)


@bp.route("/api/parse/cancel", methods=["POST"])
def api_parse_cancel():
    """Start over while a parse is running: stop its model call."""
    body = request.get_json(silent=True)
    stop = _running.get(_parse_id(body.get("parse_id") if isinstance(body, dict) else ""))
    if stop is not None:
        stop.set()
    return jsonify({"ok": stop is not None})


@bp.route("/api/match", methods=["POST"])
def api_match():
    body = request.get_json(silent=True) or {}
    parsed = body.get("parsed", [])
    library = body.get("library", [])
    threshold = float(body.get("threshold", 0.55))

    # "Populate with media from PP" is off — nothing here applies. The
    # front end already skips this call, so this is the belt to its
    # braces: a stale client must not resurrect matching the operator
    # turned off. Re-match is exempt below because pressing Re-match IS
    # an explicit request to match now.
    if not matching_enabled(body) and not body.get("rematch_template"):
        stats.track("matching_disabled", items=len(parsed))
        return jsonify({"matches": [{"index": i, "match": None,
                                     "confidence": 0.0}
                                    for i, _it in enumerate(parsed)]})

    # Re-match: recompute template links against ProPresenter as it is
    # RIGHT NOW, without re-parsing. The operator renamed a slide and
    # wants the links refreshed; the runsheet text hasn't changed, and a
    # free-tier account only gets 50 AI parses a day, so spending one to
    # pick up a rename in PP would be the wrong trade.
    if body.get("rematch_template"):
        settings = load_settings()
        base = pp_base(body.get("host") or settings.get("pp_host"),
                       body.get("port") or settings.get("pp_port"))
        tmpl = (body.get("template_playlist_uuid")
                or settings.get("template_playlist_uuid") or "").strip()
        if not tmpl:
            # "Auto" — resolve from the SAME hint parse used: the service
            # label the model reported, forwarded by the client. Item
            # titles used to stand in for it here, which is why this could
            # reach a different verdict than parse did on the same
            # runsheet. Titles remain the fallback for a client that
            # doesn't send a label (or a model that didn't give one).
            hint = (body.get("service_label") or "").strip()
            if not hint:
                hint = " ".join((it.get("title") or "") for it in parsed)
            try:
                tmpl = auto_detect_template_uuid(fetch_pp_playlists(base),
                                                 hint=hint) or ""
            except Exception:
                tmpl = ""
        n = link_items_to_template(parsed, base, tmpl,
                                   aliases=settings.get("template_aliases"),
                                   force=True)
        log.info("Re-match: %d/%d items linked to the template", n,
                 len(parsed))
        stats.track("rematch_used", linked=n, items=len(parsed))

    results = []
    for item in parsed:
        # Priority 1: the parse step already linked this item to a template
        # section via the LLM (`library_match` is a section dict with
        # `header` + `items` after parse-time resolution). Surface it as
        # the match so the UI can show ♻ + slide count and so
        # build_playlist_payload can expand it. Confidence 1.0 — the LLM
        # had the section names in front of it and we already validated.
        lib = item.get("library_match")
        if isinstance(lib, dict) and lib.get("header") and lib.get("items") is not None:
            results.append({"parsed": item, "match": lib, "confidence": 1.0})
            continue
        # Priority 2: existing song-only fuzzy match against whatever
        # library the UI sent in this request — unchanged behaviour for
        # songs the LLM didn't pre-link.
        if item.get("type") == "song" and library:
            match, conf = fuzzy_match(item.get("title", ""), library, threshold)
        else:
            match, conf = None, 0.0
        results.append({"parsed": item, "match": match,
                        "confidence": round(conf, 3)})

    songs = sum(1 for r in results
                if (r["parsed"] or {}).get("type") == "song")
    stats.track("match_completed",
                items=len(results),
                songs=songs,
                songs_matched=sum(1 for r in results
                                  if (r["parsed"] or {}).get("type") == "song"
                                  and r["match"]),
                template_links=sum(1 for r in results
                                   if (r["parsed"] or {}).get("type") != "song"
                                   and r["match"]),
                library=len(library),
                rematch=bool(body.get("rematch_template")))
    return jsonify({"items": results})
