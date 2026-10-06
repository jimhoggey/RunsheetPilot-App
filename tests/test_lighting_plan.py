"""The guide mapped onto a runsheet by its own model call.

Owner's spec (7 Oct 2026): the lighting notes are moving from the church's
service-transitions one-pager into the runsheet, and the one-pager is what
keeps them consistent in the meantime. One prompt doing both traded them
against each other on their real runsheet — sharpen the runsheet's
precedence and the guide stopped filling gaps, soften it and the guide
overwrote levels the runsheet stated. So the guide gets its own call, and
precedence stops depending on the model: an item whose lighting the
runsheet already set is never offered to it.
"""
import json

import pytest

from propresenterrunsheet.parsing.lighting_plan import moments_of, plan_lighting

GUIDE = """1. Walk-in and countdown — when: countdown on, band getting ready — 30%
2. Band starts — when: countdown ends, band starts — 20%
3. First praise song (1st song of the set) — when: halfway through first song — 12%
4. Praise into worship (2nd song of the set) — when: first worship song starts — 8%
5. Preach HARD SWITCH — when: preacher says "Thanks band", everything changes — 30%"""

ITEMS = [
    {"title": "Countdown"},
    {"title": "Worship and Ministry Time"},
    {"title": "Jesus=Joy"},
    {"title": "Worthy"},
    {"title": "Preach - Matt", "lighting_steps": [{"level": "25%", "when": "thanks band"}]},
]


def _reply(steps):
    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": json.dumps({"steps": steps})}}]}
    return _R


@pytest.fixture
def model(monkeypatch):
    import propresenterrunsheet.parsing.lighting_plan as mod
    monkeypatch.setattr(mod, "fetch_catalogue", lambda: None)
    monkeypatch.setattr(mod, "resolve_model", lambda *a, **k: "openai/gpt-4.1-mini")
    monkeypatch.setattr(mod, "reasoning_for", lambda *a, **k: None)

    def _say(steps):
        sent = {}
        monkeypatch.setattr(mod, "chat", lambda *a, **k: (sent.update(k), _reply(steps))[1])
        return sent
    return _say


# ── The guide is read here, not asked of the model ──────────────────────────

def test_the_moments_keep_the_churchs_own_levels_and_wording():
    """The name is what matches a runsheet item; the cue is what the
    operator reads on the card. Both are kept, for different jobs."""
    assert moments_of(GUIDE)[2] == {
        "n": 3, "name": "First praise song (1st song of the set)",
        "when": "halfway through first song", "level": "12%"}


def test_the_model_is_shown_what_each_moment_is(model):
    sent = model([])
    plan_lighting(ITEMS, GUIDE, "sk-or-x", "")
    asked = sent["body"]["messages"][0]["content"]
    assert "5. Preach HARD SWITCH — preacher says \"Thanks band\", everything changes" in asked
    assert "30%" not in asked.split("RUNSHEET:")[0]      # levels aren't the model's to choose


@pytest.mark.parametrize("line", [
    "Lights are usually dim for worship",      # not numbered
    "6. Something — when: a moment",           # no level
    "",
])
def test_a_line_that_isnt_a_moment_is_left_out(line):
    assert moments_of(line) == []


# ── The mapping ─────────────────────────────────────────────────────────────

def test_each_moment_lands_on_the_item_the_model_picked(model):
    model([{"moment": 1, "item": 0}, {"moment": 3, "item": 2}, {"moment": 4, "item": 3}])
    plan = plan_lighting(ITEMS, GUIDE, "sk-or-x", "")
    assert plan == {0: [{"level": "30%", "when": "countdown on, band getting ready"}],
                    2: [{"level": "12%", "when": "halfway through first song"}],
                    3: [{"level": "8%", "when": "first worship song starts"}]}


def test_an_item_the_runsheet_already_lit_is_never_touched(model):
    """Precedence without trusting the answer: the guide's 30% is dropped
    because the runsheet set that item's lighting itself."""
    model([{"moment": 5, "item": 4}])
    assert plan_lighting(ITEMS, GUIDE, "sk-or-x", "") == {}


def test_the_items_already_lit_are_named_as_such_in_the_prompt(model):
    sent = model([])
    plan_lighting(ITEMS, GUIDE, "sk-or-x", "")
    asked = sent["body"]["messages"][0]["content"]
    assert "4. Preach - Matt  (the runsheet sets this item's lighting itself)" in asked
    assert "2. Jesus=Joy" in asked and "halfway through first song" in asked


@pytest.mark.parametrize("steps, want", [
    ([{"moment": 3, "item": 2}, {"moment": 2, "item": 1}], {2: [{"level": "12%", "when": "halfway through first song"}]}),
    ([{"moment": 9, "item": 0}], {}),                       # no such moment
    ([{"moment": 1, "item": 99}], {}),                      # no such item
    ([{"moment": 1, "item": -1}], {}),
    ([{"moment": 1, "item": 0}, {"moment": 1, "item": 1}], {0: [{"level": "30%", "when": "countdown on, band getting ready"}]}),
    (["nonsense", {"moment": None, "item": 0}], {}),
])
def test_an_answer_that_doesnt_hold_up_is_dropped(model, steps, want):
    """Backwards, out of range, a moment twice, junk: all refused — the
    model picks, the code decides what's allowed."""
    model(steps)
    assert plan_lighting(ITEMS, GUIDE, "sk-or-x", "") == want


def test_a_reply_that_is_not_json_leaves_the_runsheet_as_it_is(monkeypatch):
    import propresenterrunsheet.parsing.lighting_plan as mod
    monkeypatch.setattr(mod, "fetch_catalogue", lambda: None)
    monkeypatch.setattr(mod, "resolve_model", lambda *a, **k: "m")
    monkeypatch.setattr(mod, "reasoning_for", lambda *a, **k: None)

    class _R:
        status_code = 200
        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "Sorry, I can't help with that."}}]}
    monkeypatch.setattr(mod, "chat", lambda *a, **k: _R())
    assert plan_lighting(ITEMS, GUIDE, "sk-or-x", "") == {}


@pytest.mark.parametrize("items, guide, key", [
    (ITEMS, "", "sk-or-x"),                                  # no guide
    (ITEMS, GUIDE, ""),                                      # no key
    ([], GUIDE, "sk-or-x"),                                  # no runsheet
    ([{"title": "x", "lighting_steps": [{"level": "5%"}]}], GUIDE, "sk-or-x"),   # nothing blank
])
def test_nothing_to_do_means_no_call(monkeypatch, items, guide, key):
    import propresenterrunsheet.parsing.lighting_plan as mod

    def _boom(*a, **k):
        raise AssertionError("asked the model with nothing to map")
    monkeypatch.setattr(mod, "chat", _boom)
    assert plan_lighting(items, guide, key, "") == {}
