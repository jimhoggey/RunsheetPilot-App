# Digital Service Mate: one Service Mate, on a clock or on this computer

Date: 2026-10-06
Status: draft, for the owner's review
Builds on: PR #152 (lighting guide and one-step card). Implement after it merges.

## Why

Three displays grew up separately, and two of them draw the same lighting
data in different ways:

| Display | Shows | Where |
|---|---|---|
| Screens / Sound clock | segment, countdown, that station's cues, next item | ESP32 |
| Lights clock | segment, countdown, "Now: 30%", the next change | ESP32 |
| Lighting heads-up window | NOW / NEXT / THEN lighting only, with ticking | the ProPresenter computer |

The window is really the Lights station drawn on the computer instead of an
ESP32, yet it has its own toggle, its own page and its own idea of what
matters. The owner's concern: we are building independent features where
linked ones are needed.

There is also a gap the window can fill. Many churches have no ESP32 for the
person at the ProPresenter computer. On most Sundays that person runs the
screens and the lights. On a big event a dedicated screens person may sit
there instead, with lights run elsewhere.

## The idea

**Service Mate = stations** (Screens, Sound, Lights). Each station shows on a
physical clock, on this computer, or both. The lighting window becomes the
**digital Service Mate**: one floating, always-on-top window for whoever sits
at the ProPresenter computer, showing the stations ticked for this desk.

## Principle: less on screen

The window exists to answer three questions at a glance, and nothing else:

1. What does the runsheet say this desk needs for the segment we're in?
2. What's the next lighting change, and the one after? (only when Lights is ticked)
3. What's up next?

Rules that follow from it:

- **A section appears only when it has something to say.** There are no
  placeholders such as "nothing for screens".
- **Only what came from the runsheet.** At parse time every item is given a
  generic cue for each station when the runsheet says nothing ("Cue song
  slides" for any song, "Stand by" for anything else, from
  `ROLE_CUE_TABLES`). The window hides any cue that equals its station's
  generic cue for that item type. The clocks keep showing them, as today.
- **No NOW row for lights.** The operator knows where the lights are. The
  card shows NEXT (large) and THEN (greyed).
- **No countdown.** ProPresenter and the clocks already show it.

## What the window shows

```
┌ Service Mate ───────────────────────────┐
│ Worship                                 │  the segment we're in (muted, one line)
│ • Trailer video ready                   │  Screens: from the tech notes, if any
│ LIGHTS                           undo   │  only when Lights is ticked
│ NEXT  8%  first worship song   done ✓   │  click to tick it off
│ then 2%                                 │  greyed
│ UP NEXT  Welcome · Slide — Welcome      │  next item + its first runsheet cue
└─────────────────────────────────────────┘
```

- **Segment:** the current item's title, muted, one line. It gives the rest
  its context.
- **Screens cues:** the item's `cues.screen` minus the generic one, as a
  short list of up to 4, all shown at once rather than rotated.
- **Lights:**
  - The one-step card from PR #152, without the NOW row.
  - Undo is a small button, right-aligned on the line above NEXT. It appears
    only once a step in this segment has been ticked. With two stations
    ticked, it shares that line with the LIGHTS label. With Lights alone, the
    line holds only undo and is absent until a tick.
  - Ticking behaves exactly as today (`POST /api/lighting/done`, refused when
    the card was stale).
  - If the runsheet gives no lighting steps but does give runsheet lights
    cues, those cues show instead.
  - If it gives neither, the section doesn't appear.
- **Up next:** the next item's title, plus its first non-generic cue from
  the ticked stations (Screens first). One line.
- **Labels:** a station label in its clock's accent colour appears only when
  two stations are ticked, so a single-station window has no labels at all.
- **Small windows:** a short window drops THEN first, then UP NEXT.

The Sound station is not offered in the window in v1. Sound runs at the sound
desk, not the ProPresenter computer. `station_view` (below) handles it, so
adding it later is a settings change, not a rewrite.

### Plain states

The whole window shows one short line instead of the sections when:

- Service Mate is switched off, or its licence or trial has ended: "Service Mate is off."
- the operator pressed Standby: "Standby"
- no runsheet is loaded yet: "No runsheet loaded"

When "Follow ProPresenter" is on and ProPresenter has stopped answering
(`PP_REACHABLE`), a muted line at the bottom says "ProPresenter isn't
answering". That way the operator knows the segment may be out of date.

## Settings

In the Service Mate panel, the "Lighting heads-up window" switch becomes:

**Service Mate on this computer** [switch] — ☑ Screens ☐ Lights

- The clocks table, brightness and the lighting guide row are unchanged.
- Saved in `clocks.json` as `"mate": {"on": bool, "stations": ["screen", "lights"]}`.
- A new setup defaults to Screens ticked.
- **Carrying over:** when the config is read, `lighting_window: true` with no
  `mate` key becomes `{"on": true, "stations": ["lights"]}`. Someone who used
  the lighting window gets the same lights card, minus the NOW row, with no
  setup. The old key is dropped the next time the config is saved.
- Ticking a station updates an open window within one poll. The window reads
  its stations from the config, not from its URL.
- Licence: the window stays under the Service Mate licence and trial, as
  the lighting window is today.

## How it fits together

**One source of facts.** A new `service_mate/station.py` holds
`station_view(role, state) -> dict`, built only from helpers that already
exist:

- `current_index` and `heads_up` (lighting.py) for the segment and lighting steps;
- `done_for` for ticks;
- `_cues_for` for cues;
- `_next_visible_item` (state.py) for up next.

The generic-cue filter is one helper beside it,
`runsheet_cues(role, item)`. It returns `_cues_for(role, item)` minus
`ROLE_CUE_TABLES[role]` for the item's type and minus "Get ready".

The clocks keep building their payload as today (`build_state_payload`,
`lights_view`, the stock render). They read the same facts from the same
helpers, so the window and the clocks can't disagree. The firmware payload is
deliberately not rewritten around `station_view`: it is a device contract,
and the change would be risk with nothing to show for it.

**Routes** (`routes/clocks.py`):

| Route | Change |
|---|---|
| `GET /mate` | new page, `templates/mate.html`, replacing `lighting.html` |
| `GET /api/mate` | new: `{state: "live"\|"off"\|"standby"\|"empty", section, screen: {cues}, lights: {next, then, done, cues}, up_next: {title, cue}, pp_ok}`, with only the ticked stations present |
| `POST /api/mate/window` | replaces `/api/lighting/window`: `{on, stations}` |
| `GET /lighting` | redirects to `/mate` |
| `GET /api/lighting` | removed; nothing else reads it |
| `POST /api/lighting/done`, `/api/lighting/guide` | unchanged |

**The native window** (`native.py`):

- `open_lighting_window` / `close_lighting_window` become `open_mate_window` /
  `close_mate_window`, with the title "Service Mate".
- Width 360. Height 170 with one station ticked and 260 with two. Minimum
  (280, 150).
- It still closes with the main window, keeps its on/off setting, and opens as
  a popup in a plain browser.

**The loop** (`daemon.py`) runs when clocks are set up or `mate.on` is true
(today: `lighting_window`). That keeps ProPresenter followed for the window
on a desk with no clocks.

**The page** polls `/api/mate` once a second and redraws only on a change,
as `lighting.html` does now. It uses only `textContent` and the existing
colour tokens, and has no native dialogs.

## Not in v1

- A countdown in the window.
- The Sound station in the window.
- The runsheet's raw notes. The cues are already written from them; the raw
  text would be the overload this design avoids.
- Phone or tablet pages over the network. The app listens on 127.0.0.1 only,
  and opening it to the network needs its own locked-down read-only page.
- An agent or extra model calls during the service. The understanding happens
  once, at parse time.
- Guides for Screens and Sound (the lighting guide, generalised).
- Ticking anything but lighting changes.

## Testing

- `station_view` / `runsheet_cues`:
  - each station with and without runsheet cues;
  - the generic cues hidden;
  - lighting present or absent, before and after ticks;
  - up next taking the first non-generic cue;
  - the last item, with no up next.
- `GET /api/mate`:
  - only the ticked stations present, unknown stations ignored;
  - the off, standby and empty states;
  - `pp_ok` following `PP_REACHABLE`.
- Settings: `lighting_window: true` reads as `{on: true, stations: ["lights"]}`.
  A saved `mate` wins over the old key.
- The loop runs with only `mate.on` (no clocks), and not with it off.
- `/lighting` redirects; `/api/mate/window` opens and closes the fake webview
  as the lighting window tests do now.
- **Regression guard:** the existing Lights-clock and payload tests pass
  unchanged. The clocks show exactly what they did before.
- **Browser:**
  - Screens alone, Screens + Lights, and Lights alone, at 280×150 and the
    default size;
  - a runsheet with no tech notes, which should show only the segment and up next;
  - no console errors.

## Success looks like

On an ordinary Sunday, the person at ProPresenter, with Screens and Lights
ticked, sees four things and nothing more:
- the segment;
- anything the runsheet's tech notes say screens needs;
- the next light change and the one after;
- what's up next.

A runsheet with no tech notes gives an almost empty window, and that's
correct. People who used the lighting window carry on without touching a
setting. The physical clocks don't change.
