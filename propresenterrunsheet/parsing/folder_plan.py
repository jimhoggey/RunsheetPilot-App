"""Ask which Media-bin folders hold this service's media.

The first of two calls; parsing/media_plan.py is the second. A bin can
hold thousands of files, and offering them all to the asset call costs
roughly twenty times as much and answers worse. Folder NAMES number in
the dozens, so this call reads those instead — and reads what word
matching can't: "WTSSF Night 1" is Summit Friday, and a runsheet line
saying "promo Summit" wants Summit's slide where one mentioning the
youth band does not want the youth screens.

The model is shown only the folders code has already allowed
(media_match.eligible_folders): a folder named for another service that
the runsheet never mentions is gone before the prompt is built, so no
line can talk it into Sunday's media on a Summit night. It answers with
numbers, and every number is checked against that list.

An operator's saved folders for a service are final, so the caller
skips this call entirely when there are some (owner, 30 Oct 2026).
"""
from .ask import ask_json
from .media_plan import _item_row

_MAX_FOLDERS = 200
_MAX_ITEMS = 60

# Asks about RELEVANCE — whose folder is this? — and leaves selectivity to
# the next call, which judges every file and drops what doesn't fit. A
# first version told the model "pick none rather than guess"; live against
# GPT-4.1 mini (30 Oct 2026) that made it skip a Sunday service's own
# "Sunday Service Media", and skip both Summit nights when it couldn't tell
# which night it was — a folder missed here is media that can't be placed.
PROMPT = (
    "A church files its ProPresenter media into folders, listed below with "
    "how many files each holds. Below them are one service — what kind of "
    "service it is, and its full title — and its runsheet. Which folders "
    "hold media for THIS service?\n"
    "Include:\n"
    "- folders made for this service: named for its kind (\"Sunday Service "
    "Media\" belongs to a Sunday morning), or for the event and the night or "
    "day in its title (\"Summit 2026 Saturday\" on a Summit Saturday). If "
    "the title doesn't say which night or day, include all of the event's;\n"
    "- this service's own pre-service and after-service folders;\n"
    "- for a line asking for another event's media (\"promo Summit\"), that "
    "event's main folders, where its promo would be — every night's. Never "
    "another event's pre-service or after-service folders: those only ever "
    "play at that event.\n"
    "Leave out folders for other services and events, and for this event's "
    "other nights or days. A line that only mentions something (\"youth "
    "band\") is not asking for its media. A kind can name the audience as "
    "well as the event: \"Youth Summit\" is the Summit, run for young people "
    "— Summit's folders, not the weekly youth service's.\n"
    "The next step chooses the individual files and leaves out any that "
    "don't fit, so include a folder whenever it is this service's.\n"
    "Answer with JSON only: {\"folders\": [0, 3]}"
)


def plan_folders(service: str, items: list, folders: list, or_key: str,
                 configured_model: str, title: str = "", post=None) -> list:
    """The folder names the model chose, in its order, de-duplicated and
    held to `folders` ([{"name", "count"}], from eligible_folders).

    `service` is the kind ("Summit"), `title` the dated name ("Summit 2026
    — Saturday Night") — the title is what tells Friday's folder from
    Saturday's. Empty when there is nothing to choose from, no key, no
    model, or the call fails; the caller then matches folder names instead.
    """
    shown = (folders or [])[:_MAX_FOLDERS]
    if not (shown and or_key and ((service or "").strip() or items)):
        return []
    listing = "\n".join(f"{i}. {f['name']} ({f['count']} files)"
                        for i, f in enumerate(shown))
    lines = "\n".join(f"- {_item_row(it)}" for it in (items or [])[:_MAX_ITEMS]
                      if isinstance(it, dict) and _item_row(it))
    data = ask_json(f"{PROMPT}\n\nKIND OF SERVICE: {(service or '').strip()[:200]}\n"
                    f"TITLE: {(title or service or '').strip()[:200]}\n\n"
                    f"RUNSHEET:\n{lines}\n\nFOLDERS:\n{listing}",
                    or_key, configured_model, label="Folder plan",
                    timeout_s=45, post=post)
    picks = (data or {}).get("folders")
    out = []
    for n in picks if isinstance(picks, list) else []:
        if isinstance(n, int) and not isinstance(n, bool) and 0 <= n < len(shown):
            name = shown[n]["name"]
            if name not in out:
                out.append(name)
    return out
