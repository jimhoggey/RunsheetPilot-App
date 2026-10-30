"""The one JSON question every *_plan module asks OpenRouter.

What lives here are the guarantees no caller may drop: only providers that
keep nothing, temperature 0, and None — never an exception — on failure.
"""
import pytest

from propresenterrunsheet.parsing import ask


def test_every_question_goes_only_to_providers_that_keep_nothing(openrouter):
    sent = openrouter.say('{"ok": true}')
    assert ask.ask_json("Q", "sk-or-x", "", label="Test") == {"ok": True}
    body = sent["body"]
    assert body["provider"] == {"data_collection": "deny"}
    assert body["temperature"] == 0
    assert body["messages"] == [{"role": "user", "content": "Q"}]


def test_a_models_reasoning_setting_is_passed_on(openrouter, monkeypatch):
    monkeypatch.setattr(ask, "reasoning_for", lambda *a, **k: {"effort": "low"})
    sent = openrouter.say("{}")
    ask.ask_json("Q", "sk-or-x", "", label="Test")
    assert sent["body"]["reasoning"] == {"effort": "low"}


def test_prose_and_fences_around_the_json_are_tolerated(openrouter):
    openrouter.say('Sure!\n```json\n{"folders": [1]}\n```\nHope that helps')
    assert ask.ask_json("Q", "sk-or-x", "", label="Test") == {"folders": [1]}


@pytest.mark.parametrize("reply", ["no json here", "", "[1, 2]", "{{{", "{broken"])
def test_a_reply_with_no_json_object_is_none(openrouter, reply):
    openrouter.say(reply)
    assert ask.ask_json("Q", "sk-or-x", "", label="Test") is None


@pytest.mark.parametrize("status", [401, 429, 500])
def test_a_refused_request_is_none(openrouter, status):
    openrouter.say('{"ok": true}', status=status)
    assert ask.ask_json("Q", "sk-or-x", "", label="Test") is None


def test_a_dead_connection_is_none_not_an_exception(openrouter):
    import requests
    openrouter.fail(requests.RequestException("no route to host"))
    assert ask.ask_json("Q", "sk-or-x", "", label="Test") is None


def test_no_key_or_no_model_means_nothing_is_asked(openrouter):
    openrouter.forbid()
    assert ask.ask_json("Q", "", "", label="Test") is None
    openrouter.no_model()
    assert ask.ask_json("Q", "sk-or-x", "", label="Test") is None


def test_the_key_never_reaches_the_log(openrouter, caplog):
    openrouter.say("{}", status=500)
    ask.ask_json("Q", "sk-or-SECRET", "", label="Test")
    assert "SECRET" not in caplog.text
    assert "Test: HTTP 500" in caplog.text
