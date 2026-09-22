"""Read the text off a playlist's slides, locally.

Update mode has to answer "which slide does this runsheet line belong
above?", and the honest problem is that playlist media is named things
like `IMG_4021.mov` and `Comp 1_1`. A name rule has nothing to work
with. The slide itself usually does — a giving graphic says GIVING, a
bumper carries the series title, an announcement slide says what it
announces.

ProPresenter renders a thumbnail for any playlist item by INDEX:

    GET /v1/playlist/{uuid}/{index}/thumbnail/{cue}?quality=N

which is exactly the number this app already holds while merging, so
there is no uuid resolution and no branching on item type. The pixels
come back, the platform OCR engine reads them, and the text goes into
the alignment prompt — text tokens, not image tokens.

SONGS ARE SKIPPED ON PURPOSE. A song's first slide is usually a line of
lyrics, and OCR'ing it hands the model a wall of verse that says nothing
about where the header goes. Song presentations are also the one case
the plain name rule already handles, because the `.pro` file is normally
named after the song. They still appear in the alignment prompt as
positional markers — see `parsing/align.py` — just without OCR text.

CONCURRENCY, and why it is lopsided: the fetches run in a thread pool
and the OCR does not. `requests` is thread-safe; `winocr` is not
comfortably so. It wraps WinRT's async OCR API behind a synchronous
shim, and driving that from pool workers invites COM-apartment and
"no event loop in this thread" failures on the exact machine this
feature is for — ProPresenter here runs on Windows. Fetching is the
part with the latency anyway, so serialising the OCR costs little and
removes a class of bug that would only ever appear on the operator's
machine, minutes before a service."""

import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from ..parsing.ocr import OCRUnavailable, observations_to_text, pick_backend
from .playlist_update import is_header


log = logging.getLogger("pp_runsheet")

# Pixels on the longer edge. 256 is ProPresenter's own default and is far
# too small to OCR anything but a huge title; 768 reads body text without
# turning 40 fetches into a download.
THUMB_QUALITY = 768

# Whole-pass ceiling. This runs on the machine driving ProPresenter,
# possibly minutes before a service starts, so it gives up and returns
# what it has rather than holding the operator up. Partial text is still
# useful — the alignment prompt simply has fewer rows filled in.
OCR_BUDGET_S = 25.0

# How many thumbnails to pull at once. Small: this is the operator's own
# ProPresenter on localhost, and hammering it while they are working in
# it is a poor trade for a second of wall-clock.
FETCH_WORKERS = 4

# OCR text longer than this per slide is a lyric wall or a scripture
# passage, not a label. Truncated so one chatty slide cannot dominate
# the alignment prompt.
MAX_TEXT_CHARS = 220

# Video extensions, when a playlist item's name happens to carry one.
# Operator's rule (Sept 2026): this playlist is stills and single frames;
# a video is always called out on the runsheet itself, so reading its
# first frame adds nothing. The check is deliberately narrow — an
# explicit extension only. ProPresenter usually strips extensions from
# playlist item names, so most videos will not be caught here and will
# simply be OCR'd anyway. That is the right way for this to fail: OCR is
# local and costs milliseconds, so a video we miss costs nothing, while
# a guess that wrongly skipped a still would lose real text. Detecting
# video by `duration` was rejected for exactly that reason — PP sets it
# on looping STILLS too.
_VIDEO_EXTS = (".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm", ".mpg",
               ".mpeg", ".wmv", ".prores")


def looks_like_video(name: str) -> bool:
    return (name or "").strip().lower().endswith(_VIDEO_EXTS)


def ocr_targets(items: list) -> list:
    """The playlist indexes worth OCR'ing: still media, with a position.

    Headers carry their own text, presentations are skipped for the
    reason in the module docstring, and anything plainly named as a
    video is skipped because the runsheet already calls videos out by
    name. Returns `[(index, name), ...]` where `index` is the item's
    position in the playlist as ProPresenter's thumbnail endpoint counts
    it."""
    out = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict) or is_header(it):
            continue
        if (it.get("type") or "").lower() != "media":
            continue
        name = ((it.get("id") or {}).get("name") or "").strip()
        if looks_like_video(name):
            continue
        out.append((i, name))
    return out


def fetch_thumbnail(base: str, playlist_uuid: str, index: int, cue: int = 0,
                    quality: int = THUMB_QUALITY, http_get=None):
    """One item's thumbnail as JPEG bytes, or None.

    Never raises: a slide that will not render is a slide with no text,
    which is a perfectly normal outcome here."""
    try:
        get = http_get
        if get is None:
            import requests as req
            get = req.get
        r = get(f"{base}/v1/playlist/{playlist_uuid}/{index}/thumbnail/{cue}",
                params={"quality": quality, "thumbnail_type": "jpeg"},
                timeout=6)
        if not getattr(r, "ok", False):
            return None
        data = r.content
        return data if data else None
    except Exception:
        log.debug(f"thumbnail fetch failed for item {index}", exc_info=True)
        return None


def _read(blob: bytes, backend) -> str:
    """OCR one thumbnail into a single line of text.

    Deliberately NOT `parsing.ocr.image_to_text`: that function lets
    engine failures propagate, which is right when the operator uploaded
    a screenshot and an empty result would silently produce an empty
    runsheet. Here one unreadable slide out of forty must not end the
    pass, so failures are swallowed per item."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(blob)) as img:
            text = observations_to_text(backend(img.convert("RGB")))
    except Exception:
        log.debug("thumbnail OCR failed", exc_info=True)
        return ""
    # Collapse to one line: the alignment prompt wants a label, and the
    # row/column layout observations_to_text preserves is meaningless on
    # a slide.
    flat = " ".join(text.split())
    return flat[:MAX_TEXT_CHARS]


def ocr_playlist_media(base: str, playlist_uuid: str, items: list,
                       backend=None, http_get=None,
                       quality: int = THUMB_QUALITY,
                       budget_s: float = OCR_BUDGET_S) -> dict:
    """Read what it can off the playlist's media slides.

    Returns `{playlist_index: text}` for every media item that yielded
    any text at all. Best-effort throughout — an unavailable OCR engine,
    an unreachable ProPresenter or a blown time budget all return
    whatever has been read so far, because this is an upgrade to
    placement and never a precondition for it.

    `backend` and `http_get` are injectable so the whole pass is
    testable without a ProPresenter or a platform OCR engine."""
    targets = ocr_targets(items)
    if not targets:
        return {}
    try:
        backend = backend or pick_backend()
    except OCRUnavailable:
        # Linux, or an OS too old. Placement falls back to names alone.
        log.info("No OCR engine on this platform — skipping slide text")
        return {}

    started = time.time()
    out: dict = {}
    # Fetch in parallel, OCR serially. See the module docstring for why
    # the halves are treated differently.
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        blobs = pool.map(
            lambda t: (t[0], fetch_thumbnail(base, playlist_uuid, t[0],
                                             quality=quality,
                                             http_get=http_get)),
            targets)
        for index, blob in blobs:
            if time.time() - started > budget_s:
                log.info("Slide-text budget spent after %d of %d items",
                         len(out), len(targets))
                break
            if not blob:
                continue
            text = _read(blob, backend)
            if text:
                out[index] = text
    log.info("Slide text read from %d of %d media items in %.1fs",
             len(out), len(targets), time.time() - started)
    return out
