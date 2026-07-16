"""The 'done' action must never produce SUBMITTED without a real submit click.

Regression test for a live false-SUBMITTED (2026-07-16, Mark Anderson →
careers.westerncomputer.com / TeamTailor via RemoteRocketship):

  * the engine filled the form correctly (name, email, phone, résumé),
  * it never clicked submit (operator watched it live),
  * the model emitted ``done``,
  * ``_classify_submit_visual`` returned ``None`` (it is capped at 2 calls per
    run and returns None on ANY failure) — and None is not in
    ``("error", "still_on_form")``, so every guard fell through,
  * the loop returned ``status="SUBMITTED"`` and the row was recorded SUBMITTED.

The company never received that application. Handoff §1 is explicit: "never a
silent half-submit". A form that was not submitted is not an application, so the
submit click is now a HARD precondition checked before the soft signals.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

_LOOP = os.path.join(
    os.path.dirname(__file__), "..", "agent", "loop.py"
)


def _source() -> str:
    with open(os.path.abspath(_LOOP), encoding="utf-8") as fh:
        return fh.read()


def _done_handler_block() -> str:
    """The body of the `if action.kind == "done":` handler."""
    src = _source()
    start = src.index('if action.kind == "done":')
    # Up to the LoopResult(...) that returns SUBMITTED for a confirmed done.
    end = src.index('status="SUBMITTED"', start)
    return src[start:end]


def test_done_handler_checks_submit_fired_before_anything_else():
    """The submit-click precondition must be the FIRST gate in the handler.

    Ordering matters: it is a structural fact, so it must not sit behind a
    soft signal that can return None and fall through.
    """
    block = _done_handler_block()
    assert "if not submit_fired:" in block, (
        "the 'done' handler no longer checks submit_fired — a model claiming "
        "'done' without clicking submit would again be recorded as SUBMITTED"
    )
    # It must come BEFORE the visual classifier / rejection-banner checks.
    i_submit = block.index("if not submit_fired:")
    i_visual = block.index("_classify_submit_visual")
    assert i_submit < i_visual, (
        "submit_fired must be checked BEFORE the visual classifier, which "
        "returns None on failure and cannot be relied on as the only gate"
    )


def test_done_without_submit_returns_a_non_submitted_status():
    """The bail-out must NOT report success."""
    block = _done_handler_block()
    seg = block[block.index("if not submit_fired:"):]
    assert 'status="FORM_COMPLETED"' in seg, (
        "a done-without-submit must report FORM_COMPLETED (filled, not "
        "submitted) — never SUBMITTED"
    )
    assert "success=False" in seg, "done-without-submit must not report success=True"


def test_done_without_submit_is_bounded():
    """A model that keeps insisting 'done' must not spin the loop forever."""
    block = _done_handler_block()
    assert "_DONE_WITHOUT_SUBMIT_LIMIT" in block
    src = _source()
    m = re.search(r"_DONE_WITHOUT_SUBMIT_LIMIT\s*=\s*(\d+)", src)
    assert m, "_DONE_WITHOUT_SUBMIT_LIMIT constant is missing"
    assert 1 <= int(m.group(1)) <= 3, "limit should be small — it costs steps"


def test_counter_is_initialised_on_the_loop():
    assert "self._done_without_submit: int = 0" in _source()


def test_submit_fired_is_still_initialised_false_each_run():
    """The guard is only sound if submit_fired starts False per run()."""
    assert re.search(r"^\s+submit_fired = False\s*$", _source(), re.M), (
        "submit_fired must be initialised False at the top of run()"
    )


def test_is_submit_predicate_still_excludes_the_apply_button():
    """'Apply' opens the form; it is NOT a submit.

    If this regressed, clicking "Apply Here" would set submit_fired and the new
    guard above would be satisfied by a click that submits nothing — silently
    restoring the bug.
    """
    src = _source()
    m = re.search(
        r"_is_submit = action\.kind == \"click\" and bool\(re\.search\(\s*\n\s*r\"([^\"]+)\"",
        src,
    )
    assert m, "could not locate the _is_submit predicate"
    pattern = m.group(1)
    assert not re.search(pattern, "Apply Here", re.I), (
        f"_is_submit pattern {pattern!r} matches 'Apply Here' — the form-reveal "
        "button would be mistaken for a submit"
    )
    assert not re.search(pattern, "Apply Now", re.I)
    # ...but a real submit must still match.
    assert re.search(pattern, "Submit application", re.I)


def _executor_source() -> str:
    p = os.path.join(os.path.dirname(__file__), "..", "services", "executor.py")
    with open(os.path.abspath(p), encoding="utf-8") as fh:
        return fh.read()


def _terminal_markers_block() -> str:
    """The _terminal_markers tuple body (comments inside it contain parens, so
    match to the tuple's own closing paren, not the first one)."""
    src = _executor_source()
    start = src.index("_terminal_markers = (")
    end = src.index("\n                    )", start)
    return src[start:end]


def test_form_not_submitted_propagates_past_the_scripted_fallback():
    """Every terminal exception the executor RAISES must also be allowed to escape.

    Regression: the loop's honest "filled but not submitted" bail-out raised
    FORM_NOT_SUBMITTED, but the except-block allowlist didn't know that marker,
    so it was swallowed and the scripted pipeline re-ran on the agent's
    already-filled DOM ("Submit click failed — no submit button found") and
    Celery retried it. That is the exact fallback conflict the allowlist exists
    to prevent.
    """
    markers = _terminal_markers_block()
    assert '"FORM_NOT_SUBMITTED"' in markers
    assert '"BUDGET_EXHAUSTED"' in markers


def test_every_raised_terminal_marker_is_in_the_allowlist():
    """Guard the whole class of bug, not just the one instance of it.

    Scoped to raises INSIDE the AgentLoop try-block — only those can be caught
    by its except and diverted into the scripted pipeline. Pre-flight raises
    (e.g. PLATFORM_NEEDS_REVIEW at the top of execute()) sit outside it and
    propagate on their own, so they are correctly absent from the allowlist.
    """
    src = _executor_source()
    markers = _terminal_markers_block()
    guarded = src[src.index("use_agent_loop = os.getenv") : src.index("_terminal_markers = (")]
    listed = re.findall(r'"([^"]+)"', markers)
    # Markers raised as `raise Exception("MARKER: ...")` within that window.
    # Prefix match: the allowlist may carry a more specific entry (e.g. the
    # raise says "BLOCKED: Email verification…" and the entry is that full string).
    for raised in re.findall(r'raise Exception\(\s*\n?\s*f?"([A-Z_]{6,}):', guarded):
        assert any(m.startswith(raised) for m in listed), (
            f"executor raises {raised!r} but it is not in _terminal_markers — it "
            "would be swallowed and the scripted pipeline would run on the "
            "agent's filled DOM"
        )
