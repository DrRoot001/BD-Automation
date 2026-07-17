"""'Auto Apply did nothing' must be impossible to hit silently.

Live failure (2026-07-17, two operators on the shared DB): frontend Auto Apply
with max_apps=1 → toast said "Auto-Apply started" → nothing ever happened.
Root causes, in order of discovery:

  * one in-flight application row for the candidate made
    ``get_active_application_count(inflight_only=True)`` return 1, so the
    dispatched dynamic_apply hit ``limit_reached`` and queued NOTHING — while
    the API had already answered {"status": "queued"} and the frontend showed a
    success toast. The no-op was invisible end to end.
  * rows are per-CANDIDATE in the shared DB, so one machine's in-flight row
    blocks every teammate testing with the same candidate.
  * ``FOUND`` was counted as in-flight but had NO staleness threshold — a FOUND
    row whose pipeline died pre-tailoring pinned the cap forever.
  * stop_all.bat kills workers un-acked, so a click's task can strand in Redis
    and execute minutes later when the next stack starts — consuming the slot
    invisibly.

These tests pin the two code-level fixes: the honest trigger response and the
FOUND staleness cutoff.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))


def _src(rel: str) -> str:
    p = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..", rel))
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# ── the cap's staleness map ──────────────────────────────────────────────────


def test_found_has_a_staleness_threshold_in_the_cap():
    """FOUND counts toward the in-flight cap, so it MUST be able to go stale."""
    src = _src("backend/app/services/matching.py")
    block = src[src.index("STALE_THRESHOLDS_MIN = {") :]
    block = block[: block.index("}")]
    assert '"FOUND"' in block, (
        "FOUND is in active_statuses(inflight_only) but missing from "
        "STALE_THRESHOLDS_MIN — a dead FOUND row would pin the cap forever and "
        "silently block every Auto Apply for the candidate"
    )
    m = re.search(r'"FOUND":\s*(\d+)', block)
    assert m and int(m.group(1)) == 30, (
        "FOUND staleness must mirror the watchdog's 30-min FOUND threshold "
        "(app/services/state_machine.py) so the cap never counts a row the "
        "watchdog is about to reap"
    )


def test_every_inflight_status_can_go_stale():
    """The whole class: anything counted as in-flight must have a cutoff."""
    src = _src("backend/app/services/matching.py")
    infl = src[src.index("if inflight_only:") :]
    infl = infl[: infl.index("}")]
    statuses = re.findall(r'"([A-Z_]+)"', infl)
    stale = src[src.index("STALE_THRESHOLDS_MIN = {") :]
    stale = stale[: stale.index("}")]
    for s in statuses:
        assert f'"{s}"' in stale, (
            f"{s} is counted as in-flight but has no staleness threshold — "
            "it can pin the application cap forever"
        )


# ── the honest trigger response ──────────────────────────────────────────────


def test_trigger_apply_precounts_inflight_and_returns_it():
    src = _src("backend/app/routers/candidates.py")
    block = src[src.index("async def trigger_apply") :]
    block = block[: block.index("PIPELINE_PAUSABLE_STATUSES")]
    assert "get_active_application_count" in block, (
        "trigger_apply must pre-count in-flight applications — otherwise a "
        "limit_reached run is dispatched with a success response and the "
        "operator sees 'pipeline not starting'"
    )
    assert '"inflight": inflight_count' in block


def test_trigger_apply_warns_when_no_slot_is_free():
    src = _src("backend/app/routers/candidates.py")
    block = src[src.index("async def trigger_apply") :]
    block = block[: block.index("PIPELINE_PAUSABLE_STATUSES")]
    assert 'response["warning"]' in block
    # The warning must fire on >=, not >: inflight == max_apps also leaves zero
    # slots (that is exactly the max_apps=1 with one in-flight row case).
    assert "inflight_count >= (request_body.max_apps or 1)" in block


def test_frontend_surfaces_the_warning_not_a_success_toast():
    src = _src("frontend/app/candidates/[id]/page.tsx")
    assert "res?.warning" in src, (
        "the Auto-Apply click handler must check the response warning — a "
        "success toast for a limit_reached run is the exact 'nothing happened' "
        "experience this fixes"
    )
