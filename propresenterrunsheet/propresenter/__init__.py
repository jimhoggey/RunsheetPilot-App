"""ProPresenter integration — filesystem paths, library scan, fuzzy match,
REST timer creation, and playlist payload assembly.

Submodule responsibilities:
  paths.py        — discover the user's local PP folder + libraries + playlists
  library.py      — scan .pro files for UUIDs; fuzzy-match by title
  templates.py    — find the service's template and match its slides to lines
  media_bin.py    — read the Media bin; link template media to it
  media_match.py  — each media folder's job, and where every file goes
  timers.py       — duration-based [RB] countdown timers via the REST API
  playlist.py     — assemble the items list for PUT /v1/playlist/{uuid}

Callers import the submodules directly; this package re-exports nothing.
"""
