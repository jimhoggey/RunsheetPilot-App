"""fetch_pp_playlist_items must not put a request-supplied id in a URL
path unchecked. The template uuid can arrive in the create request body,
and "../timer/…" would walk out of the playlist API into any other
ProPresenter endpoint."""
import requests

from propresenterrunsheet.propresenter.templates import fetch_pp_playlist_items


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"items": [{"type": "header"}]}


def _record_gets(monkeypatch):
    urls = []

    def fake_get(url, **_kw):
        urls.append(url)
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    return urls


def test_traversal_id_is_refused_without_a_request(monkeypatch):
    urls = _record_gets(monkeypatch)
    assert fetch_pp_playlist_items("http://127.0.0.1:50001",
                                   "../timer/abc") == []
    assert urls == []


def test_real_uuid_still_fetches(monkeypatch):
    urls = _record_gets(monkeypatch)
    uuid = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
    assert fetch_pp_playlist_items("http://127.0.0.1:50001", uuid) == [
        {"type": "header"}]
    assert urls == [f"http://127.0.0.1:50001/v1/playlist/{uuid}"]
