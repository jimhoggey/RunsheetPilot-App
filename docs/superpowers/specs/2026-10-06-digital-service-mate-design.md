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

## Principle: the clock's content, drawn for a window

The window shows what that station's clock is sent. It uses the same title,
countdown, cues and next item, worked out by the same code from the same
state, generic cues included. Which cues are actually useful, and where a
generic cue helps when an item has no tech notes, gets refined once it has
been used on real Sundays. That needs real data, not a rule guessed now.

Where it differs from the clock, it differs only to stay uncluttered:

- **All of a station's cues at once**, rather than rotated five seconds each:
  a window has the room.
- **Lights as the one-step card:** NEXT (large) and THEN (greyed), with no
  "now" row. This is the owner's call for the window; the Lights clock keeps
  its "Now: 30%".
- **A section appears only when it has something to say.** There are no
  placeholders.

## What the window shows

```
┌ Service Mate ───────────────────────────┐
│ Worship                           03:42 │  segment + countdown, one line
│ • Cue song slides                       │  Screens: the screen clock's cues
│ • Trailer video ready                   │
│ LIGHTS                           undo   │  only when Lights is ticked
│ NEXT  8%  first worship song   done ✓   │  click to tick it off
│ then 2%                                 │  greyed
│ UP NEXT  Welcome · Slide — Welcome      │  the clock's next title + next cue
└─────────────────────────────────────────┘
```

- **Segment and countdown:** the clock's title and countdown on one line.
  They use the same deadline the clocks are given, so the window and the
  clocks tick together.
- **Screens:** the Screens clock's cues, up to 4.
- **Lights:**
  - When the runsheet has lighting steps, this is the one-step card from
    PR #152 without the NOW row. Ticking behaves exactly as today
    (`POST /api/lighting/done`, refused when the card was stale).
  - When it has none, it shows the Lights clock's cues instead.
  - Undo is a small button, right-aligned on the line above NEXT. It appears
    only once a step in this segment has been ticked. With two stations
    ticked, it shares that line with the LIGHTS label. With Lights alone, the
    line holds only undo and is absent until a tick.
- **Up next:** the next title and next cue from the Screens payload when
  Screens is ticked, otherwise from the Lights payload. Both are built from
  the plain state, so this is always the next item, never the next lighting
  change. One line.
- **Labels:** a station label in its clock's accent colour appears only when
  two stations are ticked, so a single-station window has no labels at all.
- **Small windows:** a short window drops THEN first, then UP NEXT.

### Optional sections: try them, keep what earns its place

A window isn't limited to 240×240 pixels, so it can show what the clock has
to leave out. Which of these is worth the space isn't known yet, so none is
hard-coded. Each is a "Show" tick box in settings (below), to be tried on
real Sundays:

| Section | Shows | Default |
|---|---|---|
| **Timing** | This segment's planned start and length, and how far ahead or behind the service is running, e.g. "6:10 PM · 20 min · 3 min behind". | on |
| **Tech notes** | The runsheet's own notes for this segment in full, e.g. "Drop lights to 12% for worship." Muted, under the cues; long notes clamp to 3 lines and expand on click. | off |
| **Up next in full** | Every cue for the next segment, not just the first. | off |
| **Coming later** | The two or three segments after the next one, with their start times. | off |

- **Timing** compares when the segment actually started (`current_started_at`,
  set as ProPresenter moves) with the start time the runsheet gave it.
  Within a minute it reads "on time". With no start time on the segment,
  it shows only the length. With neither, it doesn't appear.
- A ticked section with nothing to say for this segment doesn't appear, the
  same rule as everything else.
- Order on the window: segment line, Timing, cues, Tech notes, Lights, Up next
  (or Up next in full), Coming later.

The Sound station is not offered in the window in v1. Sound runs at the sound
desk, not the ProPresenter computer. The window builds every section from
`build_state_payload`, which already handles Sound, so adding it later is a
settings change, not a rewrite.

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
Show: ☑ Timing ☐ Tech notes ☐ Up next in full ☐ Coming later

- The clocks table, brightness and the lighting guide row are unchanged.
- Saved in `clocks.json` as
  `"mate": {"on": bool, "stations": ["screen", "lights"], "show": ["timing"]}`.
  Unknown names in `stations` or `show` are ignored.
- A new setup defaults to Screens ticked and shows Timing.
- The "Show" row appears only while the switch is on.
- **Carrying over:** when the config is read, `lighting_window: true` with no
  `mate` key becomes `{"on": true, "stations": ["lights"], "show": ["timing"]}`.
  Someone who used the lighting window gets the same lights card, minus the
  NOW row, with no setup. The old key is dropped the next time the config is
  saved.
- Ticking a station or a "Show" box updates an open window within one poll.
  The window reads both from the config, not from its URL.
- Licence: the window stays under the Service Mate licence and trial, as
  the lighting window is today.

## How it fits together

**One function builds the whole view.** A new `service_mate/mate.py` holds
`mate_view(state, stations, show, ends_at, now) -> dict`. `GET /api/mate` only
reads the config and calls it. It is kept out of the route so a bigger
physical display can be fed the same view later (see "Not in v1").

**The clock's own payload is the window's data.** For each ticked station,
`mate_view` calls `build_state_payload(role, "detailed", state, ends_at,
now)`, the same function, from the same state, that builds what that
station's clock is sent. The page draws the segment, countdown, cues and up
next straight from those payloads. There is no second derivation that could
drift from the clocks.

- **Lights** is built from the plain state, not `lights_view`. The window
  draws its own lighting card from `heads_up` and `done_for`, as the lighting
  window does today. Up next therefore stays the real next item.
- **Countdown:** `EndsAtHolder` gains a read-only
  `peek(state) -> datetime | None`. It returns the deadline the loop is
  holding, when that deadline belongs to the state's current item. The route
  never calls `resolve`, which changes the holder's state, so the window can't
  nudge the clocks' deadline. Until the loop's next tick (0.5 s or less)
  there is no countdown. The payload's `now` lets the page correct for clock
  offset, as the firmware does.
- **Extras** come from the same state the payloads are built from, and are
  built only when ticked:
  - Tech notes are the payload's own `notes`.
  - Up next in full is `_cues_for(role, next item)`.
  - Coming later is the titles and `start_time` of the items after the next.
  - Timing comes from the current item's `start_time`, `duration_min` and the
    state's `current_started_at`. One small helper, `segment_timing(state, now)`,
    holds the ahead/behind sum, so it can be tested on its own.
- **The clocks are untouched.** `build_state_payload`, `lights_view` and the
  stock render keep their behaviour. The existing tests are the guard.

**Routes** (`routes/clocks.py`):

| Route | Change |
|---|---|
| `GET /mate` | new page, `templates/mate.html`, replacing `lighting.html` |
| `GET /api/mate` | new: `{state: "live"\|"off"\|"standby"\|"empty", stations: {screen: <payload>, lights: <payload>}, lighting: {next, then, done} \| null, extras: {timing, notes, next_cues, later}, pp_ok}`, with only the ticked stations and ticked extras present |
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
(today: `lighting_window`). That keeps ProPresenter followed, and the
deadline held, for the window on a desk with no clocks.

**The page** polls `/api/mate` once a second and redraws only on a change,
as `lighting.html` does now. The countdown ticks locally between polls. It
uses only `textContent` and the existing colour tokens, and has no native
dialogs.

## Not in v1

- The Sound station in the window.
- Deciding which cues and which optional sections to keep, drop or merge.
  That comes after real use, with the data in hand (see the principle above).
- Tracking which sections people tick. Usage stats stay as they are; the
  owner judges by using it.
- A bigger physical Service Mate: the yellow ESP32 development boards with a
  2.8" 320×240 touchscreen ("Cheap Yellow Display", ESP32-2432S028), instead
  of today's 240×240 clock. It could show the window's richer view, and its
  touchscreen could tick off a lighting step. `mate_view` is the hook for
  it. It needs new firmware, so it's a project of its own.
- Phone or tablet pages over the network. The app listens on 127.0.0.1 only,
  and opening it to the network needs its own locked-down read-only page.
- An agent or extra model calls during the service. The understanding happens
  once, at parse time.
- Guides for Screens and Sound (the lighting guide, generalised).
- Ticking anything but lighting changes.

## Testing

- **Parity:** for each ticked station, `/api/mate`'s payload equals what
  `build_state_payload` gives that station's clock for the same state, with
  generic cues included.
- **`GET /api/mate`:**
  - only the ticked stations present, unknown stations ignored;
  - `lighting` present only with Lights ticked and lighting steps in the runsheet;
  - the off, standby and empty states;
  - `pp_ok` following `PP_REACHABLE`.
- **`EndsAtHolder.peek`:**
  - it returns the held deadline for the current item and None for another item;
  - it never changes what `resolve` returns next.
- **Extras:**
  - each appears only when ticked, and only when it has something to say;
  - `segment_timing`: on time within a minute, ahead and behind, no start
    time (length only), neither (absent), and a start time that won't parse
    (length only).
- **Settings:** `lighting_window: true` reads as
  `{on: true, stations: ["lights"], show: ["timing"]}`. A saved `mate` wins
  over the old key. Unknown names in `stations` or `show` are dropped.
- **The loop** runs with only `mate.on` (no clocks), and not with it off.
- **Window routes:** `/lighting` redirects; `/api/mate/window` opens and
  closes the fake webview as the lighting window tests do now.
- **Regression guard:** the existing Lights-clock and payload tests pass
  unchanged.
- **Browser:**
  - Screens alone, Screens + Lights, and Lights alone, at 280×150 and the
    default size;
  - the countdown matching the clock preview;
  - no console errors.

## Success looks like

On an ordinary Sunday, the person at ProPresenter, with Screens and Lights
ticked, sees on one small window what their clocks would show them:
- the segment and its countdown;
- the screens cues;
- the next light change and the one after;
- what's up next;
- whether the service is running on time.

Anything more is one tick box away. People who used the lighting window
carry on without touching a setting. The physical clocks don't change. After
a few Sundays, the owner decides which cues and sections earn their place.
