"""/api/create_playlist must keep request text out of paths, URLs and replies.

Four things the route takes from the request and used to use as-is:

  - the export FOLDER was a path in the request body; it now comes only
    from the folder saved in Settings;
  - the playlist NAME became the export file name, so "../../x" wrote the
    .playlist file outside the folder the operator chose;
  - the same name was the fallback playlist id in the URL of the pushes
    to ProPresenter, so "../timer/x" could walk out of the playlist API;
  - any unexpected failure sent the raw exception text back to the page.
"""
import os
import time
import types

import pytest

import propresenterrunsheet.routes.playlist as playlist_mod


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def pp(monkeypatch):
    """Fake ProPresenter: every POST answers with `post_payload`, every PUT
    is recorded by URL and succeeds."""
    state = {"put_urls": [], "post_payload": {"id": {"uuid": "NEW-PL"}},
             "playlists": []}

    import requests

    monkeypatch.setattr(playlist_mod, "fetch_pp_playlists",
                        lambda _base: state["playlists"])

    monkeypatch.setattr(requests, "post",
                        lambda url, json=None, timeout=0:
                        _Resp(200, state["post_payload"]))

    def fake_put(url, json=None, timeout=0):
        state["put_urls"].append(url)
        return _Resp(204)

    monkeypatch.setattr(requests, "put", fake_put)
    monkeypatch.setattr(playlist_mod, "fetch_media_bin", lambda _base: [])
    return state


def _create(client, name, **extra):
    body = {"host": "localhost", "port": "1", "name": name,
            "matched": [{"parsed": {"type": "other", "title": "Welcome",
                                    "library_match": None},
                         "match": None}],
            "create_timers": False, "matching": False}
    body.update(extra)
    return client.post("/api/create_playlist", json=body).get_json()


@pytest.fixture
def pp_library(tmp_path, monkeypatch):
    """A fake ProPresenter playlist folder holding one freshly written
    playlist file, and no one-second wait for PP to write it."""
    pdir = tmp_path / "pp" / "Playlists"
    pdir.mkdir(parents=True)
    fresh = pdir / "Library"
    fresh.write_bytes(b"playlist bytes")
    future = time.time() + 60          # newer than the request's start
    os.utime(fresh, (future, future))
    monkeypatch.setattr(playlist_mod, "find_pp_root",
                        lambda: tmp_path / "pp")
    monkeypatch.setattr(playlist_mod, "find_playlist_dir",
                        lambda _root: pdir)
    monkeypatch.setattr(playlist_mod, "time", types.SimpleNamespace(
        time=time.time, sleep=lambda _s: None))
    return tmp_path


def _save_export_folder(folder):
    """What the Settings panel does when the operator picks a folder."""
    from propresenterrunsheet.settings import save_settings
    save_settings({"export_dir": str(folder)})


def test_a_traversal_name_is_exported_inside_the_chosen_folder(
        client, pp, pp_library):
    export_dir = pp_library / "exports" / "inner"
    _save_export_folder(export_dir)
    body = _create(client, "../../escaped", export=True)
    assert body.get("ok") is True, body

    written = list(export_dir.iterdir())
    assert len(written) == 1, written
    assert written[0].parent == export_dir
    assert body["export_path"] == str(written[0])
    assert not (pp_library / "escaped.playlist").exists()
    assert not (pp_library / "exports" / "escaped.playlist").exists()


def test_an_ordinary_name_is_exported_unchanged(client, pp, pp_library):
    export_dir = pp_library / "exports"
    _save_export_folder(export_dir)
    body = _create(client, "Sunday Service", export=True)
    assert body.get("ok") is True, body
    assert (export_dir / "Sunday Service.playlist").is_file()


def test_a_folder_in_the_request_is_never_written_to(client, pp, pp_library):
    """The request says whether to export; only Settings says where."""
    saved, sent = pp_library / "exports", pp_library / "elsewhere"
    _save_export_folder(saved)
    body = _create(client, "Sunday Service", export=True, export_dir=str(sent))
    assert body["export_path"] == str(saved / "Sunday Service.playlist")
    assert not sent.exists()


def test_no_export_unless_the_request_asks(client, pp, pp_library):
    _save_export_folder(pp_library / "exports")
    body = _create(client, "Sunday Service")
    assert body.get("ok") is True and body["export_path"] is None
    assert not (pp_library / "exports").exists()


@pytest.mark.parametrize("name, expected", [
    ("Sunday Service", "Sunday Service"),
    ("Youth 21/09", "Youth 21-09"),
    ("../../x", "-..-x"),
    ("..\\..\\x", "-..-x"),
    ("C:x", "C-x"),
    ("/etc/passwd", "-etc-passwd"),
    (".hidden", "hidden"),
    ("..", "Playlist"),
    ("   ", "Playlist"),
    ("", "Playlist"),
    ("line\nbreak", "line-break"),
])
def test_export_file_name_is_a_plain_file_name(name, expected):
    got = playlist_mod._export_file_name(name)
    assert got == expected
    for bad in ("/", "\\", ":", "\n"):
        assert bad not in got
    assert not got.startswith(".")


def test_a_traversal_name_never_reaches_the_playlist_url(client, pp):
    """PP answering without an id used to fall back to the typed name as
    the id in /v1/playlist/<id>. A name that isn't a plain id now stops
    the create with a plain message instead of being sent."""
    pp["post_payload"] = {}
    body = _create(client, "../timer/x")
    assert pp["put_urls"] == []
    assert "ProPresenter" in body["error"]
    assert "timer" not in body["error"]


def test_an_ordinary_name_still_works_when_pp_returns_no_uuid(client, pp):
    """A real name has spaces, so it can't be the id in the URL — but PP
    can say which uuid the playlist it just made has. That used to work
    by sending the name; it must not become a failure."""
    pp["post_payload"] = {}
    pp["playlists"] = [{"uuid": "OTHER", "name": "Last Week"},
                       {"uuid": "PP-UUID", "name": "Sunday Service"}]
    body = _create(client, "Sunday Service")
    assert body.get("ok") is True, body
    assert pp["put_urls"] and all(u.endswith("/v1/playlist/PP-UUID")
                                  for u in pp["put_urls"])


def test_no_uuid_anywhere_asks_to_delete_the_empty_playlist_first(client, pp):
    """The playlist exists, so a bare "click Create again" would make a
    second one."""
    pp["post_payload"] = {}
    body = _create(client, "Sunday Service")
    assert pp["put_urls"] == []
    assert "Delete the empty playlist" in body["error"]


def test_a_failed_export_does_not_fail_the_create(client, pp, pp_library,
                                                  monkeypatch):
    """The playlist is already built when the export runs. An unplugged
    drive must not turn that into an error telling them to Create again."""
    def no_space(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr(playlist_mod.shutil, "copy2", no_space)
    _save_export_folder(pp_library / "exports")
    body = _create(client, "Sunday Service", export=True)
    assert body.get("ok") is True, body
    assert body["export_path"] is None


def test_the_uuid_pp_returns_is_still_used(client, pp):
    body = _create(client, "Sunday Service")
    assert body.get("ok") is True, body
    assert pp["put_urls"] and all(u.endswith("/v1/playlist/NEW-PL")
                                  for u in pp["put_urls"])


def test_an_unexpected_failure_does_not_echo_exception_text(
        client, pp, monkeypatch):
    def boom(_matched):
        raise RuntimeError("/Users/someone/secret/path kaboom")

    monkeypatch.setattr(playlist_mod, "build_playlist_payload", boom)
    body = _create(client, "Sunday Service")
    assert "error" in body
    assert "kaboom" not in body["error"]
    assert "/Users" not in body["error"]
    assert "ProPresenter" in body["error"]
