"""Ask which Media-bin asset belongs to which runsheet item.

Its own call, for the same reason the lighting guide has one: the main
parse prompt is already long, and this is a different question — not
"what does the runsheet say" but "which of these files is the one for
this segment". The candidates are narrowed first by
propresenter/media_match.py, so the model is choosing among the event's
own media rather than searching a bin holding years of it.

The model only pairs numbers. Names and uuids are read from the bin
(media_match.sections), so it cannot invent an asset ProPresenter would
404 on, and what it answers is held to what the code allows.
"""
from .ask import ask_json

_MAX_ROWS = 150

PROMPT = (
    "Below are a church service's runsheet items, numbered, and the media "
    "files available for it in ProPresenter, numbered. The files are named "
    "by whoever made them, so match on meaning: \"WTSSF_Summit2026_Screen_"
    "QR_1920x1080\" is the QR screen, \"...Opener...\" is the opener film. "
    "\"has:\" lists the screens an item already has from the service's "
    "template.\n"
    "Files marked MUST PLACE are this week's announcements: every one goes "
    "in. Put each under the item it belongs to — the item that names it or "
    "its event (a Junior Youth screen under \"Junior Youth Out\"), else the "
    "announcements item.\n"
    "Place an unmarked file only where it clearly belongs: a wrong file on "
    "screen is worse than none. Leave an item out if nothing fits it. A "
    "hold screen, a loop, a lower third or a QR slide can recur through a "
    "service; a film, a sermon bumper or anything made for one moment "
    "belongs to one item only.\n"
    "The tech notes are hints, not the full list: an item with no note can "
    "still have a file that fits it.\n"
    "Also list, by number, as \"stale\": files whose name says they are for "
    "a date or event that has passed by the service date.\n"
    "Answer with JSON only: {\"media\": [{\"media\": 0, \"item\": 2}], "
    "\"stale\": [5]}"
)


def _rows(label_of, rows: list) -> str:
    return "\n".join(f"{i}. {label_of(r)}" for i, r in enumerate(rows[:_MAX_ROWS]))


def _item_row(it: dict) -> str:
    """The title, and the tech note when there is one. The note is where
    a line says what it actually wants on screen — "promo Summit" lives
    there far more often than in the title."""
    title = (it.get("title") or "").strip()
    note = " ".join((it.get("notes") or "").split())[:120]
    return f"{title} — {note}" if note else title


def _line_row(it: dict) -> str:
    """An item as the placement call sees it: title, note, and the
    template screens it already has — so the model neither doubles one
    up nor reports one as missing."""
    lm = it.get("library_match")
    has = [str(e.get("name") or "") for e in (lm or {}).get("items") or []
           if isinstance(e, dict)] if isinstance(lm, dict) else []
    has = ", ".join(n for n in has if n)[:160]
    return f"{_item_row(it)} (has: {has})" if has else _item_row(it)


def _media_row(a: dict) -> str:
    """The file, under the folder it sits in. The folder is the operator's
    own filing — "Summit 2026", "Pre-service loops" — and is often the
    only thing that says what a file like WTSSF_B2_v3 is for."""
    name = a.get("name") or ""
    folder = (a.get("folder") or "").strip()
    row = f"{folder} / {name}" if folder else name
    return f"{row} — MUST PLACE" if a.get("job") == "every" else row


def plan_media(items: list, offered: list, or_key: str, configured_model: str,
               title: str = "", today: str = "", post=None):
    """{"media": [{"media", "item"}], "stale": [n]} — the model's answer,
    unvalidated (media_match.place and media_match.notes hold it to what
    was offered).

    No "missing screen" question: asked which screens a line's note wants
    that nothing supplies, GPT-4.1 mini named a screen it had just placed
    on that very line (21 Nov 2026), and a wrong red header on a Sunday
    is worse than none. The office keeps the folders current; the model
    only says where each file goes.

    Empty lists when there is nothing to place or no key, or the model
    answered and nothing fits; None when the model DIDN'T answer (no
    model, the call failed, no JSON). Placing media again keeps what is
    already placed on None, and only replaces it on a real answer
    (routes/parse.place_bin_media).

    `title` is the service's dated name and `today` the date, which is
    how a file for "Young Adults 29th Nov" can be told apart from a past
    event.
    """
    empty = {"media": [], "stale": []}
    if not (items and offered and or_key):
        return empty
    data = ask_json(f"{PROMPT}\n\nSERVICE: {str(title or '').strip()[:200]}\n"
                    f"TODAY: {today}\n\nRUNSHEET:\n{_rows(_line_row, items)}\n\n"
                    f"MEDIA:\n{_rows(_media_row, offered)}",
                    or_key, configured_model, label="Media plan", post=post)
    if data is None:
        return None
    return {k: data.get(k) if isinstance(data.get(k), list) else []
            for k in empty}
