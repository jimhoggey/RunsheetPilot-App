"""The call that pairs a runsheet item with the file meant for it."""
import json

import pytest

import propresenterrunsheet.parsing.media_plan as mp

ITEMS = [{"title": "Countdown"}, {"title": "Opener Film"}, {"title": "Connect QR"}]
OFFERED = [
    {"uuid": "U1", "name": "WTSSF_Summit2026_Opener_1920x1080"},
    {"uuid": "U2", "name": "WTSSF_Summit2026_Screen_QR_1920x1080"},
]


@pytest.fixture
def model(openrouter):
    return openrouter.say


def test_the_pairings_come_back_as_given(model):
    model(json.dumps({"media": [{"media": 0, "item": 1}, {"media": 1, "item": 2}]}))
    assert mp.plan_media(ITEMS, OFFERED, "sk-or-x", "") == [
        {"media": 0, "item": 1}, {"media": 1, "item": 2}]


def test_both_lists_are_numbered_for_the_model(model):
    sent = model('{"media": []}')
    mp.plan_media(ITEMS, OFFERED, "sk-or-x", "")
    asked = sent["body"]["messages"][0]["content"]
    assert "1. Opener Film" in asked
    assert "0. WTSSF_Summit2026_Opener_1920x1080" in asked
    assert "U1" not in asked, "uuids are the code's business, not the model's"


def test_prose_around_the_json_is_tolerated(model):
    model('Sure!\n```json\n{"media": [{"media": 1, "item": 2}]}\n```\nHope that helps')
    assert mp.plan_media(ITEMS, OFFERED, "sk-or-x", "") == [{"media": 1, "item": 2}]


@pytest.mark.parametrize("payload", [
    "I can't help with that.", "{}", '{"media": "all of them"}', "", "{{{",
])
def test_an_unusable_answer_leaves_the_playlist_plain(model, payload):
    model(payload)
    assert not mp.plan_media(ITEMS, OFFERED, "sk-or-x", "")


def test_a_refused_call_leaves_the_playlist_plain(model):
    model('{"media": [{"media": 0, "item": 0}]}', status=429)
    assert not mp.plan_media(ITEMS, OFFERED, "sk-or-x", "")


def test_a_dead_connection_leaves_the_playlist_plain(openrouter):
    import requests
    openrouter.fail(requests.RequestException("no route to host"))
    assert not mp.plan_media(ITEMS, OFFERED, "sk-or-x", "")


@pytest.mark.parametrize("payload, status, want", [
    ('{"media": []}', 200, []),               # answered: nothing fits
    ('{}', 200, []),                          # answered, said nothing usable
    ('{"media": []}', 500, None),             # never answered
    ("I can't help with that.", 200, None),   # no JSON: not an answer
])
def test_no_answer_is_told_apart_from_nothing_fits(model, payload, status, want):
    """Placing media again only replaces what is placed on a real answer —
    a dropped call must not read as "remove every slide"."""
    model(payload, status=status)
    assert mp.plan_media(ITEMS, OFFERED, "sk-or-x", "") == want


@pytest.mark.parametrize("items, offered, key", [
    ([], OFFERED, "sk-or-x"),                 # no runsheet
    (ITEMS, [], "sk-or-x"),                   # nothing in the bin for this event
    (ITEMS, OFFERED, ""),                     # no key
])
def test_nothing_to_match_means_no_call(openrouter, items, offered, key):
    openrouter.forbid()
    assert mp.plan_media(items, offered, key, "") == []


def test_no_usable_model_means_no_call(openrouter):
    openrouter.no_model()
    assert mp.plan_media(ITEMS, OFFERED, "sk-or-x", "") is None
