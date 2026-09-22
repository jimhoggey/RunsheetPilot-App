"""Tests for /api/update_playlist — the destructive path.

Update mode rewrites a playlist the operator built by hand, on a
machine where the only other copy is ProPresenter's own autosave. So
the behaviour pinned here is not "does it produce good headers" (that
is tests/test_playlist_update.py) but "what happens when it goes
wrong": every guard aborts with nothing written, a snapshot exists
before the PUT, the write is read back whatever ProPresenter said about
it, and a bad outcome restores the original rather than leaving the
operator with a half-written playlist twenty minutes before a service.
"""
import json
from pathlib import Path

import pytest

import propresenterrunsheet.routes.playlist as playlist_mod
from propresenterrunsheet.propresenter import update_safety as safety


def _media(name, uuid, **extra):
    return {"id": {"uuid": uuid, "name": name, "index": 0}, "type": "media",
            "target_uuid": f"T-{uuid}", "is_hidden": False, "is_pco": False,
            **extra}


EXISTING = [_media("PRESERVICE LOOP", "1"), _media("IMG_4021", "2"),
            _media("WELCOME SLIDE", "3")]

RUNSHEET = [{"parsed": {"type": "other", "title": "Pre-service"}},
            {"parsed": {"type": "mc_on_stage", "title": "Welcome"}}]


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text
        self.ok = status < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def pp(monkeypatch, tmp_path, isolated_state):
    """A fake ProPresenter that holds one playlist in memory.

    Patched at `requests` so EVERY caller goes through it — the route,
    the raw fetch, the active-playlist probe and the rollback's own
    read-back all share one view of the world, which is the only way a
    rollback test proves anything.
    """
    state = {
        "items":     [dict(i) for i in EXISTING],
        "puts":      [],
        "put_codes": [204],          # per-PUT status, last value repeats
        "active":    "",             # uuid PP is presenting from
        "bin":       [],             # Media-bin item names
        "corrupt_on": None,          # PUT number after which PP mangles it
    }
    import requests

    def fake_get(url, timeout=0, **kw):
        if url.endswith("/v1/playlists"):
            return _Resp(200, [{"id": {"uuid": "PL-1",
                                       "name": "Sunday 4 May"}}])
        if url.endswith("/v1/playlist/active"):
            return _Resp(200, {"presentation":
                               {"playlist": {"uuid": state["active"]}}})
        if "/v1/playlist/" in url:
            return _Resp(200, {"items": state["items"]})
        # PP's Media bin is two hops: the bin playlists, then each one's
        # items. fetch_media_bin walks both.
        if url.endswith("/v1/media/playlists"):
            return _Resp(200, [{"id": {"uuid": "BIN-1", "name": "Media"}}])
        if "/v1/media/playlist/" in url:
            return _Resp(200, {"items": [
                {"id": {"uuid": f"B{i}", "name": n}}
                for i, n in enumerate(state["bin"])]})
        if url.endswith("/v1/timers"):
            return _Resp(200, [])
        return _Resp(404, {})

    def fake_put(url, json=None, timeout=0, **kw):
        state["puts"].append(json)
        n = len(state["puts"])
        code = state["put_codes"][min(n - 1, len(state["put_codes"]) - 1)]
        if code < 400:
            state["items"] = [dict(i) for i in (json or [])]
        elif state["corrupt_on"] == n:
            # A refusal is not a promise that nothing was applied — PP can
            # reject the request and still leave the playlist changed.
            state["items"] = state["items"][:-1]
        return _Resp(code, text="")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "put", fake_put)
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _Resp(200, {"id": {"uuid": "X"}}))
    monkeypatch.setattr(safety, "SNAPSHOT_DIR", tmp_path / "backups")
    return state


@pytest.fixture
def client(app_module, pp):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _post(client, **over):
    body = {"host": "localhost", "port": "50001", "playlist_uuid": "PL-1",
            "playlist_name": "Sunday 4 May", "name": "Sunday 4 May",
            "matched": RUNSHEET}
    body.update(over)
    return client.post("/api/update_playlist", json=body).get_json()


def _content_names(items):
    return [(i.get("id") or {}).get("name") for i in items
            if i.get("type") != "header"]


# ── the happy path, and what it leaves behind ─────────────────────────────

def test_headers_are_added_and_every_slide_survives(client, pp):
    res = _post(client)
    assert res["ok"] is True and res["content_preserved"] is True
    assert res["headers_added"] == 2
    assert _content_names(pp["items"]) == [
        "PRESERVICE LOOP", "IMG_4021", "WELCOME SLIDE"]


def test_a_snapshot_exists_before_anything_is_written(client, pp):
    res = _post(client)
    snap_path = Path(res["snapshot_path"])
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert snap["state"] == "verified"
    assert [i["id"]["name"] for i in snap["items"]] == [
        "PRESERVICE LOOP", "IMG_4021", "WELCOME SLIDE"]
    # The .txt beside it is the only artefact a volunteer can act on:
    # ProPresenter cannot import our JSON, but a person can read a list.
    txt = snap_path.with_suffix(".txt").read_text(encoding="utf-8")
    assert "PRESERVICE LOOP" in txt and "Sunday 4 May" in txt


def test_running_it_twice_sends_no_second_write(client, pp):
    _post(client)
    writes = len(pp["puts"])
    res = _post(client)
    assert res["no_change"] is True
    assert len(pp["puts"]) == writes, "a no-op must not PUT"


# ── guards: nothing written ───────────────────────────────────────────────

def test_a_failed_read_never_writes(client, pp, monkeypatch):
    """The failure this whole feature is shaped around: PP hiccups, the
    read comes back empty, and headers-only gets PUT over a playlist the
    app believes is empty."""
    monkeypatch.setattr(playlist_mod, "fetch_pp_playlist_raw",
                        lambda *a, **k: None)
    res = _post(client)
    assert res["ok"] is False and res["reason"] == "read_failed"
    assert pp["puts"] == []
    assert "nothing was changed" in res["error"].lower()


def test_the_live_playlist_is_refused_unless_forced(client, pp):
    pp["active"] = "PL-1"
    res = _post(client)
    assert res["reason"] == "playlist_active" and pp["puts"] == []
    assert _post(client, force=True)["ok"] is True


def test_a_playlist_that_changed_underneath_us_is_refused(client, pp):
    res = _post(client, expect_fingerprint=[["media", "stale", "T-9"]])
    assert res["reason"] == "concurrent_edit" and pp["puts"] == []


def test_no_playlist_chosen_is_refused(client, pp):
    assert _post(client, playlist_uuid="")["reason"] == "no_playlist"
    assert pp["puts"] == []


# ── failure: the original comes back ──────────────────────────────────────

def test_a_refused_write_restores_the_original_and_says_so(client, pp):
    """PP rejects the merged list. The operator must end the request with
    exactly the playlist they started with, and be told that plainly."""
    pp["put_codes"] = [400, 204]        # merge refused, rollback accepted
    res = _post(client)
    assert res["ok"] is False and res["rolled_back"] is True
    assert res["rollback_verified"] is True
    assert _content_names(pp["items"]) == [
        "PRESERVICE LOOP", "IMG_4021", "WELCOME SLIDE"]
    snap = json.loads(Path(res["snapshot_path"]).read_text(encoding="utf-8"))
    assert snap["state"] == "rolled_back"


def test_a_write_that_loses_a_slide_is_rolled_back_even_on_HTTP_204(client, pp,
                                                                   monkeypatch):
    """A 2xx is not proof the playlist is intact. The read-back runs
    whatever ProPresenter said, and a missing slide triggers the same
    restore a refusal does."""
    import requests
    real_put = requests.put

    def lossy_put(url, json=None, timeout=0, **kw):
        resp = real_put(url, json=json, timeout=timeout, **kw)
        if len(pp["puts"]) == 1:        # only the first (merge) write
            pp["items"] = [i for i in pp["items"]
                           if i["id"]["name"] != "IMG_4021"]
        return resp

    monkeypatch.setattr(requests, "put", lossy_put)
    res = _post(client)
    assert res["ok"] is False and res["rollback_verified"] is True
    assert _content_names(pp["items"]) == [
        "PRESERVICE LOOP", "IMG_4021", "WELCOME SLIDE"]


def test_a_failed_rollback_hands_over_the_backup_and_the_item_names(client, pp):
    """The one case the operator must not be able to miss. No retry loop:
    a wedged ProPresenter plus retries turns one bad playlist into a
    destroyed one."""
    # The merge is refused AND leaves the playlist damaged, then the
    # restore is refused too — the only way a rollback genuinely fails.
    pp["put_codes"] = [400, 400]
    pp["corrupt_on"] = 1
    res = _post(client)
    assert res["rollback_verified"] is False
    assert res["reason"] == "rollback_failed"
    assert res["snapshot_path"]
    assert res["snapshot_items"] == [
        "PRESERVICE LOOP", "IMG_4021", "WELCOME SLIDE"]
    assert len(pp["puts"]) == 2, "exactly one rollback attempt"


def test_failure_messages_carry_no_jargon(client, pp):
    """Same bar as the create route: no HTTP status, no uuid, no
    exception text — a volunteer can act on none of it."""
    pp["put_codes"] = [400, 204]
    msg = _post(client)["error"]
    for jargon in ("HTTP", "404", "400", "uuid", "Traceback", "None",
                   "PL-1", "Exception"):
        assert jargon not in msg, f"{jargon!r} leaked into: {msg}"


# ── what update mode must never do ────────────────────────────────────────

def test_relink_media_is_never_called(client, pp, monkeypatch):
    """`relink_media` DELETES entries with no Media-bin counterpart.
    Against a template's suggestion that drops a guess; against the
    operator's own playlist it is precisely the data loss this feature
    exists to prevent."""
    def boom(*a, **k):
        raise AssertionError("relink_media must never run in update mode")
    monkeypatch.setattr(playlist_mod, "relink_media", boom)
    assert _post(client)["ok"] is True


def test_the_playlist_is_never_renamed(client, pp):
    """The Service name field feeds timers and Service Mate. The
    playlist keeps the name the operator gave it."""
    _post(client, name="Something Else Entirely")
    # A rename would be a POST /v1/playlists or a name field in the PUT;
    # the PUT body is a bare items list and carries no name at all.
    assert isinstance(pp["puts"][0], list)


def test_no_playlist_is_created(client, pp, monkeypatch):
    import requests

    def boom(*a, **k):
        raise AssertionError("update mode must never create a playlist")
    monkeypatch.setattr(requests, "post", boom)
    assert _post(client)["ok"] is True


# ── the preview ───────────────────────────────────────────────────────────

def test_preview_writes_nothing_and_returns_the_plan(client, pp):
    res = client.post("/api/update_playlist/preview", json={
        "host": "localhost", "port": "50001", "playlist_uuid": "PL-1",
        "matched": RUNSHEET}).get_json()
    assert res["ok"] is True and pp["puts"] == []
    assert res["headers_added"] == 2 and res["content_count"] == 3
    assert [p["title"] for p in res["placements"]] == ["Pre-service", "Welcome"]
    assert res["fingerprint"]


def test_preview_flags_the_live_playlist_without_blocking(client, pp):
    pp["active"] = "PL-1"
    res = client.post("/api/update_playlist/preview", json={
        "playlist_uuid": "PL-1", "matched": RUNSHEET}).get_json()
    assert "live" in res["warnings"]


def test_preview_names_media_missing_from_the_bin(client, pp):
    pp["bin"] = ["PRESERVICE LOOP"]
    res = client.post("/api/update_playlist/preview", json={
        "playlist_uuid": "PL-1", "matched": RUNSHEET}).get_json()
    assert sorted(res["unbinned"]) == ["IMG_4021", "WELCOME SLIDE"]


def test_an_empty_bin_accuses_nobody(client, pp):
    """`fetch_media_bin` returns [] on failure too, so an empty bin is
    indistinguishable from a ProPresenter hiccup."""
    pp["bin"] = []
    res = client.post("/api/update_playlist/preview", json={
        "playlist_uuid": "PL-1", "matched": RUNSHEET}).get_json()
    assert res["unbinned"] == []


# ── undo ──────────────────────────────────────────────────────────────────

def test_restore_puts_a_snapshot_back(client, pp):
    res = _post(client)
    pp["items"] = [_media("WRECKED", "9")]
    out = client.post("/api/restore_playlist", json={
        "playlist_uuid": "PL-1",
        "snapshot_path": res["snapshot_path"]}).get_json()
    assert out["ok"] is True and out["restored"] == 3
    assert _content_names(pp["items"]) == [
        "PRESERVICE LOOP", "IMG_4021", "WELCOME SLIDE"]


# ── templates in the picker ───────────────────────────────────────────────

def test_listing_marks_templates_by_name_and_by_pin(client, pp, monkeypatch):
    """The dropdown groups on these flags, so the rule lives server-side
    beside the Auto logic that uses it."""
    import requests
    from propresenterrunsheet import settings as pp_settings

    pls = [{"id": {"uuid": "T1", "name": "Sunday Morning Library"}},
           {"id": {"uuid": "S1", "name": "Service 21 Sep"}},
           {"id": {"uuid": "P1", "name": "Weird Name"}}]
    real_get = requests.get

    def get(url, timeout=0, **kw):
        if url.endswith("/v1/playlists"):
            return _Resp(200, pls)
        return real_get(url, timeout=timeout, **kw)

    monkeypatch.setattr(requests, "get", get)
    pp_settings.save_settings({"template_playlist_uuid": "P1"})
    res = client.get("/api/pp/playlists?host=localhost&port=50001").get_json()
    by = {p["uuid"]: (p["is_template"], p["template_by"])
          for p in res["playlists"]}
    assert by == {"T1": (True, "name"), "S1": (False, ""),
                  "P1": (True, "pinned")}


def test_preview_warns_before_rewriting_a_template(client, pp, monkeypatch):
    """Update mode replaces every header, and a template's headers ARE
    its sections. Listed and selectable, but never silently."""
    import requests
    real_get = requests.get

    def get(url, timeout=0, **kw):
        if url.endswith("/v1/playlists"):
            return _Resp(200, [{"id": {"uuid": "PL-1",
                                       "name": "Youth Service - Library"}}])
        return real_get(url, timeout=timeout, **kw)

    monkeypatch.setattr(requests, "get", get)
    res = client.post("/api/update_playlist/preview", json={
        "playlist_uuid": "PL-1", "matched": RUNSHEET}).get_json()
    assert "template" in res["warnings"]


def test_a_service_playlist_gets_no_template_warning(client, pp, monkeypatch):
    import requests
    real_get = requests.get

    def get(url, timeout=0, **kw):
        if url.endswith("/v1/playlists"):
            return _Resp(200, [{"id": {"uuid": "PL-1", "name": "Sunday 4 May"}}])
        return real_get(url, timeout=timeout, **kw)

    monkeypatch.setattr(requests, "get", get)
    res = client.post("/api/update_playlist/preview", json={
        "playlist_uuid": "PL-1", "matched": RUNSHEET}).get_json()
    assert "template" not in res["warnings"]


def test_a_failed_playlist_list_stops_the_preview_rather_than_guessing(
        client, pp, monkeypatch):
    """The target is confirmed against ProPresenter's own playlist list
    before anything else happens. If that list can't be read, the preview
    stops — so the template warning (which uses the same list) can never
    be silently skipped, and a write can never target a playlist
    ProPresenter didn't just confirm exists."""
    import requests
    real_get = requests.get

    def get(url, timeout=0, **kw):
        if url.endswith("/v1/playlists"):
            raise requests.exceptions.Timeout("busy")
        return real_get(url, timeout=timeout, **kw)

    monkeypatch.setattr(requests, "get", get)
    res = client.post("/api/update_playlist/preview", json={
        "playlist_uuid": "PL-1", "matched": RUNSHEET}).get_json()
    assert res["ok"] is False and res["reason"] == "read_failed"
    assert pp["puts"] == []


def test_a_pinned_template_is_warned_about_whatever_it_is_called(
        client, pp, monkeypatch):
    from propresenterrunsheet import settings as pp_settings
    pp_settings.save_settings({"template_playlist_uuid": "PL-1"})
    res = client.post("/api/update_playlist/preview", json={
        "playlist_uuid": "PL-1", "matched": RUNSHEET}).get_json()
    assert "template" in res["warnings"]


# ── the id and the backup path arrive over HTTP ───────────────────────────

@pytest.mark.parametrize("evil", [
    "../timer/abc", "PL-1/../../v1/timers", "PL-1?x=1", "a b", "", "x" * 65,
])
def test_a_malformed_playlist_id_never_reaches_a_url(client, pp, evil):
    """The id is spliced into /v1/playlist/{id} on a route that PUTs.
    Unchecked, "../" walks out of the playlist API into the rest of
    ProPresenter's."""
    res = _post(client, playlist_uuid=evil)
    assert res["ok"] is False and res["reason"] == "no_playlist"
    assert pp["puts"] == []


def test_a_playlist_proPresenter_does_not_list_is_refused(client, pp):
    res = _post(client, playlist_uuid="NOT-THERE")
    assert res["ok"] is False and res["reason"] == "read_failed"
    assert pp["puts"] == []


@pytest.mark.parametrize("evil", [
    "/etc/passwd",
    "../../settings.json",
    "settings.json",
    "C:\\Users\\x\\AppData\\Roaming\\Runsheet Pilot\\settings.json",
])
def test_restore_only_reads_inside_the_backups_folder(client, pp, evil):
    """Undo sends back a filesystem path. Unconfined, it could read any
    JSON file on the machine and — through mark_snapshot — rewrite it."""
    out = client.post("/api/restore_playlist", json={
        "snapshot_path": evil}).get_json()
    assert out["ok"] is False
    assert pp["puts"] == []


def test_restore_accepts_a_windows_style_path_to_a_real_backup(client, pp):
    """The browser holds whatever the server sent. On Windows that is a
    backslash path, and it must still resolve to the backup it names."""
    res = _post(client)
    name = Path(res["snapshot_path"]).name
    pp["items"] = [_media("WRECKED", "9")]
    winpath = "C:\\Users\\x\\AppData\\Roaming\\Runsheet Pilot\\playlist_backups\\" + name
    out = client.post("/api/restore_playlist", json={
        "snapshot_path": winpath}).get_json()
    assert out["ok"] is True
    assert _content_names(pp["items"]) == [
        "PRESERVICE LOOP", "IMG_4021", "WELCOME SLIDE"]
