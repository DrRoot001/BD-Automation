"""Portal-memory 'learn from mistakes' tests.

The self-learned playbook now records not just what WORKED on a host but the
dead-ends the agent TRIED that FAILED, and injects a "DO NOT REPEAT" block into
the next run's prompt — so repeat applies (e.g. 10 Dice jobs) stop making the
same wrong move every time. This pins that behaviour.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.agent import portal_memory as pm  # noqa: E402


class _A:
    """Minimal stand-in for AgentAction."""
    def __init__(self, kind, selector, ok, reason=""):
        self.kind = kind
        self.selector = selector
        self.ok = ok
        self.reason = reason


@pytest.fixture()
def isolated_pm(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_BASE_DIR", Path(tmp_path))
    monkeypatch.setenv("PORTAL_MEMORY_ENABLED", "true")
    return pm


def test_failed_selectors_extracted_not_the_ones_that_worked(isolated_pm):
    actions = [
        _A("click", "button.wrong-apply", False),      # dead-end
        _A("fill_field", "#bad-name", False),          # dead-end
        _A("fill_field", "#candidate_name", True),     # worked
        _A("click", "button:has-text('Submit')", True),  # worked
    ]
    avoid = pm.extract_failed_selectors(actions)
    assert avoid.get("click") == ["button.wrong-apply"]
    assert avoid.get("fill_field") == ["#bad-name"]
    # a selector that failed then WORKED in the same run is not a mistake
    mixed = [_A("click", "#retry", False), _A("click", "#retry", True)]
    assert pm.extract_failed_selectors(mixed) == {}


def test_record_persists_avoid_and_prompt_renders_it(isolated_pm):
    actions = [
        _A("click", "button.wrong-apply", False),
        _A("click", "button:has-text('Submit')", True),
    ]
    pm.record("https://www.dice.com/job-applications/1/wizard",
              outcome="SUBMITTED", actions=actions, ats="dice")
    pb = pm.recall("www.dice.com")
    assert pb["avoid_selectors"]["click"] == ["button.wrong-apply"]
    block = pm.format_for_prompt("www.dice.com")
    assert "DO NOT REPEAT" in block
    assert "button.wrong-apply" in block
    assert "USE-FIRST" in block


def test_avoid_is_recorded_even_on_a_failed_run(isolated_pm):
    # A run that did NOT submit still teaches the dead-end it hit.
    actions = [_A("click", "button.dead", False)]
    pm.record("https://www.dice.com/x", outcome="STUCK", actions=actions, ats="dice")
    pb = pm.recall("www.dice.com")
    assert pb.get("avoid_selectors", {}).get("click") == ["button.dead"]


def test_avoid_pruned_when_selector_later_works(isolated_pm):
    pm.record("https://www.dice.com/a", outcome="STUCK",
              actions=[_A("click", "button.maybe", False)], ats="dice")
    assert pm.recall("www.dice.com")["avoid_selectors"]["click"] == ["button.maybe"]
    # next visit: the same selector works -> must be pruned from avoid
    pm.record("https://www.dice.com/b", outcome="SUBMITTED",
              actions=[_A("click", "button.maybe", True)], ats="dice")
    pb = pm.recall("www.dice.com")
    assert "click" not in (pb.get("avoid_selectors") or {})
