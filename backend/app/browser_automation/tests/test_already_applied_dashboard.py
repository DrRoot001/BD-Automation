"""An already-submitted job must read as SUBMITTED, and must not be re-picked.

Live failure (2026-07-17, Mark Anderson → Softthink/CareerPlug):
  * the application WAS submitted (thank-you email from CareerPlug on file),
  * but the dashboard showed FAILED (recorded BOT_DETECTED),
  * and every Auto Apply re-picked the same job, re-filled it, and the portal
    rejected it with "You cannot apply to the same job within 90 days" — which
    matched none of the already-applied patterns, so it was misclassified again.

Two independent breaks, both fixed:
  1. `_ALREADY_APPLIED_PATTERNS` didn't cover CareerPlug's 90-day wording.
  2. `/jobs/for-matching` (the Auto-Apply matcher's source) had NO applied-jobs
     exclusion — that lived only in the dashboard listing endpoint.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.agent.loop import (  # noqa: E402
    _ALREADY_APPLIED_PATTERNS,
)


def _matches(msg: str) -> bool:
    m = msg.lower()
    return any(p in m for p in _ALREADY_APPLIED_PATTERNS)


# ── pattern coverage ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "You cannot apply to the same job within 90 days.",
    "You can't apply to the same job within 90 days.",
    "You cannot apply to this job again for 90 days.",
    "You have already applied to this position.",
    "You've already applied.",
    "You already have an application on file for this role.",
])
def test_duplicate_rejections_are_recognised(msg):
    assert _matches(msg) is True


@pytest.mark.parametrize("msg", [
    "This role requires you to relocate within 90 days of hire.",
    "Applications reviewed within 90 days.",
    "We aim to respond to every applicant.",
    "Apply now to join our team.",
])
def test_boilerplate_does_not_false_positive(msg):
    assert _matches(msg) is False


# ── the two code-level fixes ─────────────────────────────────────────────────


def _src(rel: str) -> str:
    p = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..", rel))
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_matcher_endpoint_excludes_already_applied_jobs():
    """/jobs/for-matching — the matcher's source — must filter applied jobs."""
    src = _src("backend/app/routers/jobs.py")
    block = src[src.index("async def get_jobs_for_matching") :]
    block = block[: block.index("@router", 1) if "@router" in block[10:] else len(block)]
    assert "Job.id.not_in(applied_ids)" in block, (
        "for-matching must exclude jobs the candidate already applied to — "
        "otherwise Auto Apply re-picks a submitted job every run"
    )
    assert "same_position" in block, (
        "must also exclude the SAME position scraped into a different job row "
        "(RemoteRocketship/talent.com/hiring.cafe list the same opening)"
    )
    assert "Job.is_duplicate.isnot(True)" in block


def test_already_applied_is_recorded_submitted_not_failed():
    """The portal confirming a duplicate = a submission exists = SUBMITTED."""
    src = _src("backend/app/tasks/browser_automation.py")
    seg = src[src.index('if "ALREADY_APPLIED" in err_msg:') :]
    seg = seg[: seg.index("PLATFORM_NEEDS_REVIEW")]
    assert '"SUBMITTED"' in seg, (
        "an already-applied job must be recorded SUBMITTED (it IS on file), "
        "not FAILED — a genuinely submitted job was showing as failed"
    )
    assert "transition_status" in seg
