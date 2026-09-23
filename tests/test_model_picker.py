"""Tests for the model shortlist and key-funding detection.

Two rules carry the design:

  * **Automatic is free-only, always.** It has to work on a fresh
    install with an unfunded key, and it must never start spending
    money without being asked.
  * **Paid models are offered only to a key that can pay.** Offering
    them otherwise produces a 402 on the operator's first parse — a much
    worse first impression than a shorter list. "Couldn't tell" counts
    as can't-pay.
"""
import pytest

from propresenterrunsheet.parsing import models as m


CATALOGUE = {"data": [
    {"id": "free/one:free", "context_length": 100000,
     "pricing": {"prompt": "0", "completion": "0"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "openai/gpt-4.1-mini", "context_length": 1000000,
     "pricing": {"prompt": "0.0000004", "completion": "0.0000016"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "qwen/qwen3-30b-a3b-instruct-2507", "context_length": 262144,
     "pricing": {"prompt": "0.00000005", "completion": "0.00000019"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "anthropic/claude-haiku-4.5", "context_length": 200000,
     "pricing": {"prompt": "0.000001", "completion": "0.000005"},
     "supported_parameters": ["structured_outputs"]},
    {"id": "openrouter/auto", "context_length": 2000000,
     "pricing": {"prompt": "-1", "completion": "-1"},
     "supported_parameters": ["structured_outputs"]},
]}


# ── automatic never spends ───────────────────────────────────────────────

def test_automatic_picks_a_free_model_even_when_paid_ones_exist():
    """The catalogue above is mostly paid; automatic must still land on
    the free one."""
    assert m.pick_default_model(CATALOGUE) == "free/one:free"


def test_automatic_never_returns_a_router():
    """openrouter/auto dispatches to a different model per request, so
    it can't be the unattended default — that was the original bug."""
    assert m.pick_default_model(CATALOGUE) != "openrouter/auto"


# ── the shortlist ────────────────────────────────────────────────────────

def test_the_shortlist_stays_short():
    """A wall of models is not a choice, it's a research project."""
    assert 2 <= len(m.RECOMMENDED) <= 6


def test_exactly_one_model_is_starred():
    assert sum(1 for r in m.RECOMMENDED if r["starred"]) == 1


def test_every_recommendation_explains_itself():
    for r in m.RECOMMENDED:
        assert r["why"].strip() and r["label"].strip(), r["id"]


def test_recommendations_are_priced_from_the_live_catalogue():
    """Hardcoded prices go stale silently; these are computed."""
    by_id = {r["id"]: r for r in m.recommended_models(CATALOGUE)}
    assert by_id["openai/gpt-4.1-mini"]["cost_per_parse"] == pytest.approx(
        0.0000004 * m._EST_PROMPT_TOKENS + 0.0000016 * m._EST_COMPLETION_TOKENS)
    assert (by_id["openai/gpt-4.1-mini"]["cost_per_parse"]
            < by_id["anthropic/claude-haiku-4.5"]["cost_per_parse"])


def test_the_shortlist_is_the_owners_three():
    """GPT-4.1 mini first, Claude Haiku as the alternative, OpenRouter Auto
    as the last resort — and a paid key falls back in that same order."""
    assert [r["id"] for r in m.RECOMMENDED] == list(m.PAID_DEFAULTS)


def test_a_pdf_goes_to_a_model_that_can_read_one():
    reads = {"architecture": {"input_modalities": ["text", "file"]}}
    cat = {"data": [{"id": "anthropic/claude-haiku-4.5", **reads},
                    {"id": "text/only"}]}
    assert m.pdf_reader(cat, "text/only") == "anthropic/claude-haiku-4.5"
    assert m.pdf_reader(cat, "anthropic/claude-haiku-4.5") == \
        "anthropic/claude-haiku-4.5"
    assert m.pdf_reader({"data": [{"id": "text/only"}]}, "text/only") is None
    assert m.pdf_reader(None, "x") is None
    # The catalogue is third-party data: odd shapes mean "can't", not a crash.
    junk = {"data": [{"id": "a", "architecture": "text+file"},
                     {"id": "b", "architecture": {"input_modalities": None}},
                     {"id": "c", "architecture": {"input_modalities": "file"}},
                     "not a model"]}
    assert m.pdf_reader(junk, "a") is None


def test_a_router_has_no_price_of_its_own():
    """Routers carry sentinel pricing (-1); computing with it yields a
    confident, enormous, NEGATIVE number."""
    by_id = {r["id"]: r for r in m.recommended_models(CATALOGUE)}
    assert by_id["openrouter/auto"]["cost_per_parse"] is None


def test_a_recommendation_missing_from_the_catalogue_is_dropped():
    """Offering an id that 404s is the exact failure this module exists
    to prevent."""
    thin = {"data": [d for d in CATALOGUE["data"]
                     if d["id"] != "anthropic/claude-haiku-4.5"]}
    ids = [r["id"] for r in m.recommended_models(thin)]
    assert "anthropic/claude-haiku-4.5" not in ids
    assert "openai/gpt-4.1-mini" in ids


def test_nothing_survives_a_catalogue_that_omits_it_not_even_the_router():
    """No exceptions. A hardcoded id that OpenRouter has withdrawn is
    exactly how this app once shipped a default that 404'd on every
    install — and an operator cannot diagnose that."""
    thin = {"data": [d for d in CATALOGUE["data"]
                     if d["id"] != "openrouter/auto"]}
    assert "openrouter/auto" not in [r["id"] for r in m.recommended_models(thin)]


def test_the_star_moves_when_the_starred_model_is_withdrawn():
    """There is always exactly one recommendation, or the group is a
    list with nothing marked."""
    thin = {"data": [d for d in CATALOGUE["data"]
                     if d["id"] != "openai/gpt-4.1-mini"]}
    out = m.recommended_models(thin)
    assert sum(1 for r in out if r["starred"]) == 1
    assert out[0]["starred"] is True


def test_an_unreachable_catalogue_offers_nothing():
    """Offline must not fall back to a hardcoded list — that IS the
    stale list this guards against."""
    assert m.recommended_models(None) == []
    assert m.recommended_models({}) == []


@pytest.mark.parametrize("model_id", [r["id"] for r in m.RECOMMENDED])
def test_every_recommended_id_exists_on_openrouter_today(model_id):
    """A live check, skipped when offline. This is the test that catches
    a shortlist going stale between releases — the runtime filter hides
    it from operators, but the maintainer should still be told."""
    catalogue = m.fetch_catalogue()
    if not catalogue:
        pytest.skip("OpenRouter unreachable")
    ids = {d.get("id") for d in catalogue.get("data") or []}
    assert model_id in ids, (
        f"{model_id} is no longer on OpenRouter — update RECOMMENDED")


# ── funding detection ────────────────────────────────────────────────────

def _key_response(payload):
    class R:
        status_code = 200
        def json(self): return payload
        def raise_for_status(self): return None
    return lambda *a, **k: R()


def test_a_paid_key_is_detected_as_funded():
    info = m.fetch_key_info("sk-or-x", http_get=_key_response(
        {"data": {"is_free_tier": False, "usage": 0.0033}}))
    assert info["funded"] is True
    assert info["usage"] == pytest.approx(0.0033)


def test_a_free_tier_key_is_detected():
    info = m.fetch_key_info("sk-or-x", http_get=_key_response(
        {"data": {"is_free_tier": True, "usage": 0}}))
    assert info["funded"] is False


def test_no_key_means_unknown_not_funded():
    assert m.fetch_key_info("")["funded"] is None


def test_an_unreachable_openrouter_means_unknown():
    """Offline must degrade to the free list, never to a paid default."""
    def boom(*a, **k):
        raise OSError("no network")
    assert m.fetch_key_info("sk-or-x", http_get=boom)["funded"] is None


def test_a_missing_tier_field_means_unknown():
    info = m.fetch_key_info("sk-or-x", http_get=_key_response({"data": {}}))
    assert info["funded"] is None


# ── what Settings shows under the key ────────────────────────────────────

def _openrouter(key, credits=None, key_status=200):
    """/api/v1/key and /api/v1/credits answered separately."""
    class R:
        def __init__(self, payload, status=200):
            self.status_code, self._payload = status, payload
        def json(self): return self._payload
        def raise_for_status(self):
            if self.status_code >= 400:
                raise OSError(self.status_code)

    def get(url, **_k):
        if url == m.CREDITS_URL:
            return R({"data": credits} if credits else {"error": {}}, 200 if credits else 403)
        return R({"data": key}, key_status)
    return get


def test_a_funded_key_reports_what_is_left_and_spent():
    info = m.fetch_key_info("k", http_get=_openrouter(
        {"is_free_tier": False, "limit": None, "usage": 0.1},
        {"total_credits": 5, "total_usage": 0.27}))
    assert info["state"] == "paid" and info["funded"] is True
    assert info["balance"] == pytest.approx(4.73) == info["credit"]
    assert info["usage"] == pytest.approx(0.27) and info["limit"] is None


def test_a_weekly_limit_on_the_key_is_reported_beside_the_account_credit():
    """The owner's key: $10 bought, $5.01 used, a $2 weekly limit on the key.
    Both figures are shown; what can be spent now is the lower."""
    info = m.fetch_key_info("k", http_get=_openrouter(
        {"is_free_tier": False, "limit": 2, "limit_reset": "weekly",
         "limit_remaining": 2},
        {"total_credits": 10, "total_usage": 5.01273376}))
    assert info["credit"] == pytest.approx(4.98726624)
    assert info["limit"] == {"amount": 2.0, "remaining": 2.0, "reset": "weekly"}
    assert info["balance"] == pytest.approx(2.0) and info["capped"] is True


def test_a_key_limit_is_never_shown_as_money_when_credit_cant_be_read():
    """The owner's key has a $50 monthly limit on an empty account. With
    /credits unreadable, "$50.00 left" would be invented."""
    info = m.fetch_key_info("k", http_get=_openrouter(
        {"is_free_tier": False, "limit": 50, "limit_remaining": 50, "usage": 0}))
    assert info["state"] == "paid" and info["balance"] is None


# ── a paid key runs on a paid model ──────────────────────────────────────

@pytest.fixture
def funded(monkeypatch):
    """key_is_funded, scripted; records whether it was asked at all."""
    asked = []

    def set_to(value):
        monkeypatch.setattr(m, "key_is_funded",
                            lambda key: asked.append(key) or value)
        return asked
    return set_to


@pytest.mark.parametrize("configured", ["", "free/one:free"])
def test_a_funded_key_turns_automatic_and_free_picks_into_gpt_4_1_mini(
        funded, configured):
    funded(True)
    assert m.resolve_model(configured, CATALOGUE, api_key="k") == "openai/gpt-4.1-mini"


def test_haiku_then_openrouter_auto_stand_in_when_gpt_4_1_mini_is_gone(funded):
    funded(True)
    cat = {"data": [d for d in CATALOGUE["data"] if d["id"] != "openai/gpt-4.1-mini"]}
    assert m.resolve_model("", cat, api_key="k") == "anthropic/claude-haiku-4.5"
    cat["data"] = [d for d in cat["data"] if d["id"] != "anthropic/claude-haiku-4.5"]
    assert m.resolve_model("", cat, api_key="k") == "openrouter/auto"


def test_a_paid_model_chosen_deliberately_is_kept_and_the_key_not_checked(funded):
    asked = funded(True)
    assert m.resolve_model("anthropic/claude-haiku-4.5", CATALOGUE,
                           api_key="k") == "anthropic/claude-haiku-4.5"
    assert asked == []


def test_without_credit_automatic_stays_free(funded):
    funded(False)
    assert m.resolve_model("", CATALOGUE, api_key="k") == "free/one:free"


def test_models_route_offers_only_paid_models_to_a_funded_key(client, monkeypatch):
    import propresenterrunsheet.routes.settings as settings_mod
    from propresenterrunsheet.settings import save_settings

    monkeypatch.setattr(settings_mod, "fetch_catalogue", lambda **_k: CATALOGUE)
    monkeypatch.setattr(settings_mod, "fetch_key_info",
                        lambda key: {"state": "paid", "funded": True, "balance": 2.0})
    save_settings({"or_key": "k", "or_model": "free/one:free"})
    body = client.get("/api/models").get_json()
    assert body["auto"] == "openai/gpt-4.1-mini" and body["models"] == []
    assert body["free_saved"] is True


@pytest.mark.parametrize("bad", ["NaN", "Infinity", float("nan"), -1, True, None, "x"])
def test_only_real_amounts_count_as_dollars(bad):
    assert m.dollars(bad) is None
    assert m.measured_costs([{"model": "a", "usd": bad}]) == {}


def test_credit_used_up_is_not_funded():
    """Seen live: $5 added, $5.01 used. Offering paid models there would
    402 on the first parse; Settings shows it as free."""
    info = m.fetch_key_info("k", http_get=_openrouter(
        {"is_free_tier": False, "limit": 50, "limit_remaining": 50,
         "free_model_daily_requests": {"used": 19, "limit": 50, "remaining": 31}},
        {"total_credits": 5, "total_usage": 5.01273376}))
    assert info["balance"] == 0.0 and info["funded"] is False
    assert info["free_today"]["remaining"] == 31


def test_a_free_key_reports_todays_free_requests():
    info = m.fetch_key_info("k", http_get=_openrouter(
        {"is_free_tier": True,
         "free_model_daily_requests": {"used": 1, "limit": 50, "remaining": 49}}))
    assert info["state"] == "free" and info["funded"] is False
    assert info["free_today"] == {"used": 1, "limit": 50, "remaining": 49}
    assert info["balance"] is None


def test_a_rejected_key_says_so():
    info = m.fetch_key_info("k", http_get=_openrouter({}, key_status=401))
    assert info["state"] == "invalid" and info["funded"] is None


def test_measured_costs_average_per_model_and_skip_junk():
    got = m.measured_costs([{"model": "a", "usd": 0.0002}, {"model": "a", "usd": 0.0004},
                            {"model": "b", "usd": 0}, {"model": "c"}, "junk"])
    assert got == {"a": pytest.approx(0.0003), "b": 0.0}
