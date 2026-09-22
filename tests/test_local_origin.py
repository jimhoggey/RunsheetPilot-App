"""Tests for the local-page-only guard in routes/core.py.

The server binds to 127.0.0.1, so the LAN cannot reach it. A web page in
a browser on the same machine can, through DNS rebinding — and every
route trusts its caller (folders to scan and export into, a quit
endpoint, writes to ProPresenter). These pin that only this app's own
page gets through, and that the app's own page always does.
"""
import pytest


@pytest.fixture
def client(app_module):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.mark.parametrize("host", [
    "localhost", "localhost:5757", "127.0.0.1:5757", "[::1]:5757",
])
def test_this_apps_own_page_is_always_served(client, host):
    """The native window loads 127.0.0.1, the browser fallback localhost."""
    r = client.get("/api/health", headers={"Host": host})
    assert r.status_code == 200


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:5757", "192.168.1.20:5757",
    "127.0.0.1.evil.example", "localhost.evil.example",
])
def test_a_rebinding_hostname_is_refused(client, host):
    """DNS rebinding points the attacker's own name at 127.0.0.1; the
    browser still sends that name as Host."""
    r = client.get("/api/health", headers={"Host": host})
    assert r.status_code == 403


@pytest.mark.parametrize("origin", [
    "http://evil.example", "https://evil.example:5757", "null",
])
def test_a_cross_origin_request_is_refused(client, origin):
    r = client.post("/api/settings", json={}, headers={"Origin": origin})
    assert r.status_code == 403


def test_a_same_origin_post_goes_through(client, isolated_state):
    r = client.post("/api/settings", json={},
                    headers={"Origin": "http://127.0.0.1:5757"})
    assert r.status_code != 403


def test_quit_cannot_be_triggered_from_another_site(client, monkeypatch):
    """A page elsewhere could otherwise shut the app down mid-service."""
    import os
    monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(
        AssertionError("quit must not run")))
    r = client.post("/api/quit", headers={"Host": "evil.example"})
    assert r.status_code == 403
