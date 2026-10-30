"""The call that picks which Media-bin folders hold a service's media.

Step 2 of five (propresenter/media_match.py): code has already narrowed
the folders, the model makes the choice, and every number it answers
with is held to the list it was shown.
"""
import json

import pytest

import propresenterrunsheet.parsing.folder_plan as fp

FOLDERS = [
    {"name": "Summit 2026 Friday", "count": 41},
    {"name": "Summit 2026 Saturday", "count": 38},
    {"name": "Current media", "count": 12},
]
ITEMS = [{"title": "Opener film"},
         {"title": "Announcements", "notes": "promo Summit 2026"}]


@pytest.fixture
def model(openrouter):
    return openrouter.say


def test_the_chosen_folders_come_back_by_name_in_the_models_order(model):
    model(json.dumps({"folders": [1, 0]}))
    assert fp.plan_folders("Summit", ITEMS, FOLDERS, "sk-or-x", "") == [
        "Summit 2026 Saturday", "Summit 2026 Friday"]


def test_the_model_reads_folder_names_and_the_runsheet_never_files(model):
    """Folder names number in the dozens where files run to thousands —
    the whole reason this is its own small call."""
    sent = model('{"folders": []}')
    fp.plan_folders("Summit", ITEMS, FOLDERS, "sk-or-x", "",
                    title="Summit 2026 — Saturday Night")
    asked = sent["body"]["messages"][0]["content"]
    assert "0. Summit 2026 Friday (41 files)" in asked
    assert "KIND OF SERVICE: Summit" in asked
    assert "TITLE: Summit 2026 — Saturday Night" in asked, \
        "the title is what tells Friday's folder from Saturday's"
    assert "promo Summit 2026" in asked, "the tech note is where a line asks"


def test_only_providers_that_keep_nothing_are_asked(model):
    sent = model('{"folders": []}')
    fp.plan_folders("Summit", ITEMS, FOLDERS, "sk-or-x", "")
    assert sent["body"]["provider"] == {"data_collection": "deny"}


@pytest.mark.parametrize("picks, want", [
    ([0, 0, 0], ["Summit 2026 Friday"]),          # said three times, used once
    ([9, -1, 2], ["Current media"]),               # out of range dropped
    ([True, "1", None, 1.0], []),                  # not an index at all
])
def test_every_number_is_held_to_the_list_it_was_shown(model, picks, want):
    model(json.dumps({"folders": picks}))
    assert fp.plan_folders("Summit", ITEMS, FOLDERS, "sk-or-x", "") == want


@pytest.mark.parametrize("payload", [
    "I can't help with that.", "{}", '{"folders": "all of them"}', "", "{{{", "[1, 2]",
])
def test_an_unusable_answer_chooses_nothing(model, payload):
    model(payload)
    assert fp.plan_folders("Summit", ITEMS, FOLDERS, "sk-or-x", "") == []


def test_a_failed_call_chooses_nothing(model):
    model('{"folders": [0]}', status=500)
    assert fp.plan_folders("Summit", ITEMS, FOLDERS, "sk-or-x", "") == []


@pytest.mark.parametrize("service, items, folders, key", [
    ("Summit", ITEMS, [], "sk-or-x"),       # nothing to choose from
    ("Summit", ITEMS, FOLDERS, ""),          # no key
    ("", [], FOLDERS, "sk-or-x"),            # nothing to choose FOR
])
def test_no_call_is_made_with_nothing_to_ask(openrouter, service, items, folders, key):
    openrouter.forbid()
    assert fp.plan_folders(service, items, folders, key, "") == []


def test_a_long_bin_is_cut_to_the_prompt_cap(model):
    many = [{"name": f"Folder {i}", "count": 1} for i in range(fp._MAX_FOLDERS + 50)]
    sent = model(json.dumps({"folders": [fp._MAX_FOLDERS + 10]}))
    assert fp.plan_folders("Summit", ITEMS, many, "sk-or-x", "") == [], \
        "a folder it was never shown can't be chosen"
    assert f"{fp._MAX_FOLDERS}. " not in sent["body"]["messages"][0]["content"]
