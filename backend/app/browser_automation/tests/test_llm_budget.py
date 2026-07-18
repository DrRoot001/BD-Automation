"""Tests for the per-application LLM cost budget (llm/budget.py).

The operator's requirement is a hard $0.10 ceiling per application, with the
Pro ("deep") tier allowed only as an exception paid out of that same $0.10.
These tests pin the properties that requirement depends on:

  * tokens are priced into dollars correctly;
  * an UNPRICED model is billed expensively, never as free;
  * the deep tier is refused once it would eat the fast-tier reserve;
  * the ledger is isolated per thread (the browser worker runs
    --pool=threads --concurrency=2, so two applies share one process).
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.llm import budget  # noqa: E402
from backend.app.browser_automation.llm import telemetry  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_ledger(monkeypatch):
    # Both flags are read from the environment at import time, so an operator
    # toggling them in .env would otherwise silently flip these tests between
    # "asserts enforcement" and "asserts nothing". Pin soft OFF. HARD_STOP is
    # pinned ON here because most tests below assert the terminal-ceiling
    # semantics (exhausted() True over cap); the degrade-by-default tests opt
    # back out explicitly.
    monkeypatch.setattr(budget, "BUDGET_SOFT_MODE", False)
    monkeypatch.setattr(budget, "BUDGET_HARD_STOP", True)
    budget.end_run()
    yield
    budget.end_run()


# ── Pricing ──────────────────────────────────────────────────────────────────


def test_cost_usd_prices_tokens_from_the_table():
    # 1M input @ $0.30 + 1M output @ $2.50 = $2.80
    usd = budget.cost_usd("gemini", "gemini-3.5-flash", 1_000_000, 1_000_000)
    assert usd == pytest.approx(2.80)


def test_cost_scales_linearly_below_1m_tokens():
    usd = budget.cost_usd("gemini", "gemini-3.5-flash", 10_000, 1_000)
    # 10k in @ $0.30/1M = $0.003 ; 1k out @ $2.50/1M = $0.0025
    assert usd == pytest.approx(0.0055)


def test_pro_model_is_priced_higher_than_flash():
    flash = budget.cost_usd("gemini", "gemini-3.5-flash", 100_000, 2_000)
    pro = budget.cost_usd("gemini", "gemini-2.5-pro", 100_000, 2_000)
    assert pro > flash


def test_dated_model_suffix_falls_back_to_prefix_price():
    _in, _out, known = budget.price_for("gemini", "gemini-2.5-pro-preview-06-05")
    assert known is True
    assert (_in, _out) == (1.25, 10.00)


def test_unknown_model_is_billed_expensively_not_free():
    """An unpriced model must never look free — that fails the cap open."""
    _in, _out, known = budget.price_for("gemini", "some-brand-new-model")
    assert known is False
    assert budget.cost_usd("gemini", "some-brand-new-model", 1_000_000, 0) > 0.0


def test_unknown_model_flags_the_run_summary_as_an_upper_bound():
    rb = budget.start_run(application_id="app-1")
    budget.charge("gemini", "totally-unknown", 1000, 100)
    assert rb.saw_unknown_model is True
    assert "UPPER BOUND" in rb.summary()


# ── The ledger ───────────────────────────────────────────────────────────────


def test_charge_accumulates_spend_and_call_count():
    rb = budget.start_run(application_id="app-1", limit_usd=0.10)
    budget.charge("gemini", "gemini-3.5-flash", 10_000, 1_000)
    budget.charge("gemini", "gemini-3.5-flash", 10_000, 1_000)
    assert rb.calls == 2
    assert rb.spent_usd == pytest.approx(0.011)
    assert rb.remaining_usd == pytest.approx(0.089)


def test_charge_outside_a_run_is_a_noop_but_still_returns_cost():
    assert budget.get_run() is None
    usd = budget.charge("gemini", "gemini-3.5-flash", 10_000, 1_000)
    assert usd == pytest.approx(0.0055)
    assert budget.get_run() is None


def test_exhausted_is_false_until_the_cap_is_hit():
    budget.start_run(application_id="app-1", limit_usd=0.10)
    assert budget.exhausted() is False
    budget.charge("gemini", "gemini-2.5-pro", 1_000_000, 0)  # $1.25 — way over
    assert budget.exhausted() is True


def test_exhausted_is_false_with_no_active_run():
    assert budget.exhausted() is False


# ── degrade-not-abandon is the DEFAULT (the Dice-at-step-23 fix) ─────────────


def test_over_budget_does_not_abandon_the_run_by_default(monkeypatch):
    """Default (hard-stop OFF): blowing the cap must NOT abandon the run.

    A multi-step wizard (Dice/iCIMS/Workday) needs far more than the ~22-25
    steps $0.10 buys. exhausted() staying False keeps the loop filling on the
    fast tier instead of failing the application at ~step 23.
    """
    monkeypatch.setattr(budget, "BUDGET_HARD_STOP", False)
    budget.start_run(application_id="app-1", limit_usd=0.10)
    budget.charge("gemini", "gemini-2.5-pro", 1_000_000, 0)  # $1.25, way over
    assert budget.exhausted() is False           # run continues
    assert budget.over_budget() is True          # ...but we KNOW we're over
    assert budget.allow_deep(estimated_usd=0.001) is False  # deep tier stops


def test_hard_stop_opt_in_makes_the_ceiling_terminal(monkeypatch):
    monkeypatch.setattr(budget, "BUDGET_HARD_STOP", True)
    budget.start_run(application_id="app-1", limit_usd=0.10)
    budget.charge("gemini", "gemini-2.5-pro", 1_000_000, 0)
    assert budget.exhausted() is True


def test_over_budget_is_false_before_the_cap():
    budget.start_run(application_id="app-1", limit_usd=0.10)
    assert budget.over_budget() is False


def test_over_budget_is_false_with_no_run():
    assert budget.over_budget() is False


# ── The governor: deep tier is a rationed exception ──────────────────────────


def test_deep_is_allowed_early_when_the_budget_is_fresh():
    budget.start_run(application_id="app-1", limit_usd=0.10)
    assert budget.allow_deep(estimated_usd=0.02) is True


def test_deep_is_refused_once_it_would_eat_the_fast_tier_reserve():
    """With a 40% reserve on a $0.10 cap, only $0.06 is ever spendable on deep."""
    rb = budget.start_run(application_id="app-1", limit_usd=0.10)
    assert rb.spendable_on_deep_usd == pytest.approx(0.06)

    # Spend $0.05 → remaining $0.05, reserve $0.04, spendable on deep $0.01.
    budget.charge("gemini", "gemini-2.5-pro", 40_000, 0)  # $0.05
    assert rb.spendable_on_deep_usd == pytest.approx(0.01)
    assert budget.allow_deep(estimated_usd=0.02) is False
    # ...but a cheap deep call still fits.
    assert budget.allow_deep(estimated_usd=0.005) is True


def test_deep_is_refused_when_budget_is_blown():
    budget.start_run(application_id="app-1", limit_usd=0.10)
    budget.charge("gemini", "gemini-2.5-pro", 1_000_000, 0)
    assert budget.allow_deep(estimated_usd=0.001) is False


def test_deep_is_allowed_with_no_active_run():
    """Non-run callers (tests, one-off scripts) keep their existing behaviour."""
    assert budget.allow_deep(estimated_usd=99.0) is True


def test_note_deep_call_counts_only_issued_deep_calls():
    rb = budget.start_run(application_id="app-1")
    budget.note_deep_call()
    budget.note_deep_call()
    assert rb.deep_calls == 2


# ── Isolation: the property --pool=threads depends on ────────────────────────


def test_ledgers_are_isolated_between_threads():
    """Two concurrent applies in one process must not bill each other.

    This is the exact worker topology: --pool=threads --concurrency=2.
    """
    results: dict = {}
    barrier = threading.Barrier(2)

    def _run(name: str, tokens: int) -> None:
        rb = budget.start_run(application_id=name, limit_usd=0.10)
        barrier.wait()  # force real interleaving
        budget.charge("gemini", "gemini-3.5-flash", tokens, 0)
        barrier.wait()
        results[name] = (rb.spent_usd, rb.calls, budget.get_run().application_id)

    t1 = threading.Thread(target=_run, args=("app-A", 10_000))
    t2 = threading.Thread(target=_run, args=("app-B", 50_000))
    t1.start(), t2.start()
    t1.join(), t2.join()

    # Each thread sees ONLY its own spend and its own application id.
    assert results["app-A"] == (pytest.approx(0.003), 1, "app-A")
    assert results["app-B"] == (pytest.approx(0.015), 1, "app-B")


def test_start_run_resets_a_previous_ledger_in_the_same_thread():
    """A second application on a reused worker thread starts from zero."""
    budget.start_run(application_id="app-1", limit_usd=0.10)
    budget.charge("gemini", "gemini-3.5-flash", 100_000, 0)
    rb2 = budget.start_run(application_id="app-2", limit_usd=0.10)
    assert rb2.spent_usd == 0.0
    assert rb2.calls == 0
    assert rb2.application_id == "app-2"


# ── Soft mode ────────────────────────────────────────────────────────────────


def test_soft_mode_meters_but_never_enforces(monkeypatch):
    monkeypatch.setattr(budget, "BUDGET_SOFT_MODE", True)
    budget.start_run(application_id="app-1", limit_usd=0.10)
    budget.charge("gemini", "gemini-2.5-pro", 1_000_000, 0)  # $1.25, way over
    assert budget.exhausted() is False        # not enforced...
    assert budget.allow_deep(estimated_usd=9) is True
    assert budget.get_run().spent_usd > 0.10  # ...but still measured


# ── The meter: telemetry is the single choke point ───────────────────────────


def test_telemetry_record_bills_the_budget_ledger():
    """telemetry.record() is the single choke point every provider calls."""
    rb = budget.start_run(application_id="app-1", limit_usd=0.10)
    telemetry.reset_session()
    telemetry.record("gemini", "gemini-3.5-flash", 10_000, 1_000, has_image=True)
    assert rb.calls == 1
    assert rb.spent_usd == pytest.approx(0.0055)
    # ...and the token ledger carries the dollar figure too.
    assert telemetry.get_session().total_cost_usd == pytest.approx(0.0055)


def test_telemetry_sessions_are_isolated_between_threads():
    """One apply's reset_session()/set_label() must not disturb the other's."""
    results: dict = {}
    barrier = threading.Barrier(2)

    def _run(name: str, tokens: int) -> None:
        telemetry.reset_session()
        telemetry.set_label(name)
        barrier.wait()
        telemetry.record("gemini", "gemini-3.5-flash", tokens, 0)
        barrier.wait()
        s = telemetry.get_session()
        results[name] = (len(s.calls), s.total_input, s.calls[0].label)

    t1 = threading.Thread(target=_run, args=("app-A", 10_000))
    t2 = threading.Thread(target=_run, args=("app-B", 50_000))
    t1.start(), t2.start()
    t1.join(), t2.join()

    assert results["app-A"] == (1, 10_000, "app-A")
    assert results["app-B"] == (1, 50_000, "app-B")


# ── Price table overrides ────────────────────────────────────────────────────


def test_price_table_env_override_is_parsed(monkeypatch):
    monkeypatch.setenv(
        "LLM_PRICE_TABLE", '{"gemini/custom-model": {"input": 1.0, "output": 2.0}}'
    )
    table = budget._load_price_table()
    assert table["gemini/custom-model"] == {"input": 1.0, "output": 2.0}


def test_malformed_price_table_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_TABLE", "{not valid json")
    table = budget._load_price_table()
    assert "gemini/gemini-3.5-flash" in table  # defaults survived


def test_malformed_price_entry_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_TABLE", '{"gemini/x": {"input": 1.0}}')
    table = budget._load_price_table()
    assert "gemini/x" not in table
    assert "gemini/gemini-3.5-flash" in table
