# Digital Service Mate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the lighting heads-up window into the digital Service Mate. It
is one always-on-top window showing what the ticked stations' clocks are
sent, plus optional sections.

**Architecture:**
- A new `service_mate/mate.py` builds the whole view (`mate_view`) from
  `build_state_payload`, `heads_up` and the item data. Nothing is derived a
  second way.
- The countdown comes from the loop's held deadline through a read-only
  `EndsAtHolder.peek`.
- Routes, the native window and the settings row are renamed from "lighting"
  to "mate". The old `lighting_window` key is read as `mate`.

**Tech Stack:** Flask, pywebview, vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-10-06-digital-service-mate-design.md`

## Global Constraints

- Window stations in v1: `screen`, `lights`. Show options: `timing`,
  `notes`, `next_cues`, `later`.
- Defaults: stations `["screen"]`, show `["timing"]`.
  `lighting_window: true` → `{on: true, stations: ["lights"], show: ["timing"]}`.
- The clocks' payloads, `lights_view` and the stock render keep their
  behaviour exactly. The existing tests must pass unchanged.
- The page uses `textContent` only, no native dialogs, and existing colour
  tokens. Accents: screen `rgb(59,130,246)`, lights `rgb(245,158,11)`.
- Every POST stays behind `_check_sm_enabled`.
- File edits go through Edit/Write (repo hooks).

## Review Focus

1. **A runsheet loaded the day before.** Item 0's `current_started_at` is the
   load time, so Timing must not claim "1,000 min behind". Drift beyond 60
   minutes isn't shown (Task 2 test).
2. **Start times with no am/pm ("6:10").** Drift picks the nearer of the am
   and pm readings (Task 2 test).
3. **The window opened before the loop's first tick, or after an item change.**
   `peek` returns None and the page shows no countdown rather than a stale
   one (Task 1 test).
4. **A stale, hand-edited or unknown `mate` config** (non-list stations,
   unknown names). It is cleaned, never crashes (Task 2 test).
5. **Lights ticked on a runsheet with no lighting steps.** The section shows
   the Lights cues, not an empty card (Task 2 test).

---

### Task 1: `EndsAtHolder.peek`

**Files:** Modify `propresenterrunsheet/service_mate/protocol.py` (class
`EndsAtHolder`). Test in `tests/test_digital_service_mate.py`.

**Produces:** `EndsAtHolder.peek(state: dict) -> datetime | None`

- [ ] Test: after `resolve(state, now)`, `peek(state)` returns the same
  deadline. `peek` on a state with another `current_started_at` returns
  None. A fresh holder returns None. Calling `peek` doesn't change the next
  `resolve`.
- [ ] Implement:
  `return self._ends_at if self._key == self._item_key(state) else None`.
- [ ] Run `pytest tests/test_digital_service_mate.py -q` and confirm it
  passes; commit.

### Task 2: `mate.py`: config, timing, view

**Files:** Create `propresenterrunsheet/service_mate/mate.py`. Test in
`tests/test_digital_service_mate.py`.

**Consumes:** `build_state_payload` (protocol), `heads_up`, `done_for`,
`current_index`, `has_lighting` (lighting), `_cues_for` (state),
`_extract_duration_min` (parsing.duration), `_minutes` (parsing.timed_rows).

**Produces:**
- `STATIONS = ("screen", "lights")` and `SHOW = ("timing", "notes", "next_cues", "later")`.
- `mate_config(cfg: dict) -> {"on": bool, "stations": list, "show": list}`.
  It reads `cfg["mate"]`, falls back to `lighting_window`, and drops unknown
  names.
- `segment_timing(item: dict, started_at: str | None, now) -> dict | None`.
  It returns `{"start": str, "length_min": int, "drift_min": int | None}`.
  `drift_min` is positive when behind and None when unknown or over 60
  minutes.
- `mate_view(state, stations, show, ends_at, now) -> dict`. It returns
  `{"state": "live"|"standby"|"empty", "stations": {role: payload},
  "lighting": {"next","then","done","here"} | None, "extras": {...}}`.

- [ ] Tests:
  - **Config:** the defaults; the `lighting_window` carry-over; a saved
    `mate` winning; junk in `stations`/`show` dropped; a non-list value
    handled.
  - **Timing:**
    - on time within a minute; 3 behind; 2 ahead;
    - a "6:10" start without am/pm compared at 18:13 reads 3 behind;
    - over 60 minutes gives `drift_min` None;
    - no start time gives length only; no start time and no length gives None;
    - a start time that won't parse gives length only.
  - **View:**
    - **parity:** each station's payload equals `build_state_payload(role,
      "detailed", state, ends_at, now)`;
    - **lighting:** present only with lights ticked and steps in the
      runsheet; lights payload cues still present when there are no steps;
    - **extras:** only ticked ones, and only when non-empty;
    - **states:** standby when `state["standby"]`, empty with no items.
- [ ] Implement (about 80 lines). The view builds payloads from the plain
  state, never from `lights_view`.
- [ ] Run tests, commit.

### Task 3: Routes, config save, loop gate, native window

**Files:**
- Modify `propresenterrunsheet/routes/clocks.py`, `native.py`, `server.py`
  (the closed hook), `service_mate/daemon.py` (the gate).
- Test in `tests/test_digital_service_mate.py`. Update the window and
  `/api/lighting` tests in `tests/test_lighting_heads_up.py`.

**Produces:**
- `GET /mate` renders `mate.html`; `GET /lighting` redirects (302) to `/mate`.
- `GET /api/mate` returns `{"state": "off"}` when blocked; otherwise
  `mate_view(...)` plus `pp_ok`.
  - `ends_at` = `daemon._ENDS_AT.peek(state)`.
  - `pp_ok` = `PP_REACHABLE["ok"] or not state.auto_track.enabled`.
- `POST /api/mate/window {on, stations?, show?, popup?}` saves
  `cfg["mate"]`, pops `lighting_window`, and opens or closes the native
  window. It returns `{ok, on, native, mate}`.
- `native.open_mate_window(url, height)` / `native.close_mate_window()`. The
  window title is "Service Mate". `_forget_mate` sets `mate.on` False.
- `GET /api/lighting` and `POST /api/lighting/window` are removed.
- Turning the master switch off closes the mate window and sets `mate.on`
  False.
- The daemon gate is `cfg.get("clocks") or mate_config(cfg)["on"]`.

- [ ] Tests:
  - `/api/mate` off when the master switch is off;
  - only the ticked stations and extras;
  - `pp_ok` follows `PP_REACHABLE` only while auto-track is on;
  - the countdown is None before a loop tick and equals the held deadline after;
  - opening, closing and the closed-by-button handler switch `mate.on` off;
  - a popup opens no native window;
  - in a browser, `native` is False;
  - the master switch off closes it;
  - saving stations or show while it's on doesn't reopen it;
  - `/lighting` redirects;
  - the loop runs with only `mate.on`.
- [ ] Update `tests/test_lighting_heads_up.py`:
  - `_card()` reads `/api/mate` with lights ticked;
  - the window tests move to the new routes;
  - `test_closing_the_main_window_takes_the_card_but_keeps_it_on` asserts
    `native.close_mate_window`.
- [ ] Implement; run the full suite; commit.

### Task 4: The page and the settings row

**Files:**
- Create `templates/mate.html`; delete `templates/lighting.html`.
- Modify `templates/index.html` (Service Mate row) and `static/app.js`
  (toggle, stations, show, sync).

The page:
- polls `/api/mate` each second, redrawing only when the response (minus
  `now`) changes;
- counts down locally from `ends_at − now` at fetch;
- builds sections in the spec's order, showing a section only when it has
  something to say, and labels only with two stations;
- uses the tick card from `lighting.html`, minus NOW, with undo on the label
  line after a tick;
- drops THEN first, then up next, below 190 px of height.

The settings row:
- "Service Mate on this computer" switch, plus Screens / Lights tick boxes
  and a Show row (Timing, Tech notes, Up next in full, Coming later);
- the Show row is visible only while the switch is on;
- changes post `{on, stations, show}`;
- restore on start as `_restoreLightingWindow` does today, and the sync reads
  `/api/clocks`'s `mate.on`.

- [ ] Browser check at 280×150 and 360×260:
  - Screens alone, Screens + Lights, and Lights alone;
  - every show box;
  - no console errors.
- [ ] Test: `/mate` is served and references `/api/mate` and `/api/lighting/done`.
- [ ] Full suite, commit.

### Task 5: Review and ship

- [ ] Security review of the branch diff (POSTs gated, `textContent` only,
  config cleaned).
- [ ] Push, open the PR (based on `lighting-guide-steps` until #152 merges),
  turn on Auto-fix, post the self-review comment.
