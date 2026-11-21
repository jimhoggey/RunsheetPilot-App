"""Service Mate — GeekMagic SmallTV-Ultra clock integration.

Each "Service Mate" is a GeekMagic clock running stock firmware on the LAN.
It has no app-level "show this text" endpoint — only image upload + a
"display this image" toggle. So we render a 240×240 JPEG with the current
runsheet item, countdown, and a role-aware cue, then push it.

Submodule responsibilities:
  constants.py   — Display dimensions, font sizes, role accents, rule tables
  state.py       — runsheet_state.json + clocks.json I/O, item cue helpers
  render.py      — Pillow-based renderers (cue compact/detailed, test card,
                   standby) and drawing helpers
  geekmagic.py   — HTTP client for the device (upload, theme, brightness,
                   probe), with workarounds for v9.0.39 firmware quirks
  pp_track.py    — ProPresenter auto-tracking — playlist section / running
                   timer / active presentation, in priority order
  daemon.py      — Background thread that ticks every 500 ms, renders the
                   right image for each clock, and pushes via geekmagic

Callers import the submodules directly; this package re-exports nothing.
"""
