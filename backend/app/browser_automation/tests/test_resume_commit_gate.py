"""Résumé-commit gate + DOM-keyed TeamTailor de-gate.

Both fix causes of a real false-SUBMITTED (2026-07-16, Mark Anderson →
careers.westerncomputer.com via RemoteRocketship):

  * the TeamTailor de-gate was keyed on ``self._platform == "teamtailor"``, but a
    job routed through an aggregator keeps the AGGREGATOR's platform all run, so
    ``activate_teamtailor_form`` never ran, the form stayed
    ``pointer-events-none``, and every click (consent box, submit) was swallowed
    while JS-setter fills still appeared to work;
  * the résumé "uploaded" because Dropzone rendered the filename instantly — the
    background PUT never committed, which an ATS silently rejects as "résumé
    required" with no DOM error.

The scorer here is real JS, so these drive it in real Chromium against real
markup — a fake page would prove nothing about the selectors.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.agent.loop import (  # noqa: E402
    _RESUME_GATE_BLOCK_LIMIT,
    AgentLoop,
)

pytestmark = pytest.mark.asyncio

_PROFILE = {"first_name": "Mark", "last_name": "Anderson", "email": "m@example.com"}


def _loop() -> AgentLoop:
    return AgentLoop(candidate_profile=dict(_PROFILE), job_context={"platform": "generic"})


async def _page(pw, html: str):
    b = await pw.chromium.launch(headless=True)
    pg = await (await b.new_context()).new_page()
    await pg.set_content(html)
    return b, pg


# ── _resume_upload_state ─────────────────────────────────────────────────────


async def test_no_file_widget_does_not_gate():
    """A form with no file input must not be blocked by this gate."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, pg = await _page(pw, "<form><input name=email></form>")
        assert await _loop()._resume_upload_state(pg) == "none"
        await b.close()


async def test_dropzone_filename_without_success_marker_is_NOT_committed():
    """THE bug: Dropzone shows the filename instantly; that is not an upload."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, pg = await _page(pw, """
            <div class="dropzone">
              <div class="dz-preview"><span>Mark_Anderson_Resume.pdf</span></div>
            </div>
            <input type=file class="dz-hidden-input">
        """)
        # Preview present, no .dz-success -> still in the silent-reject window.
        assert await _loop()._resume_upload_state(pg) == "missing"
        await b.close()


async def test_dropzone_with_dz_success_is_committed():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, pg = await _page(pw, """
            <div class="dropzone">
              <div class="dz-preview dz-success"><span>r.pdf</span></div>
            </div>
            <input type=file class="dz-hidden-input">
        """)
        assert await _loop()._resume_upload_state(pg) == "committed"
        await b.close()


async def test_dropzone_processing_reports_uploading():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, pg = await _page(pw, """
            <div class="dropzone">
              <div class="dz-preview dz-processing"><span>r.pdf</span></div>
            </div>
            <input type=file class="dz-hidden-input">
        """)
        assert await _loop()._resume_upload_state(pg) == "uploading"
        await b.close()


async def test_dropzone_error_is_surfaced():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, pg = await _page(pw, """
            <div class="dropzone">
              <div class="dz-preview dz-error"><span class="dz-error-message">too big</span></div>
            </div>
            <input type=file class="dz-hidden-input">
        """)
        assert await _loop()._resume_upload_state(pg) == "error"
        await b.close()


async def test_teamtailor_remote_url_token_is_proof_of_commit():
    """TeamTailor binds the committed upload's token into the remote-url field.

    Uses the real selector confirmed by read-only inspection of
    careers.westerncomputer.com, where it is a REQUIRED field.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, pg = await _page(pw, """
            <div class="dropzone"><div class="dz-preview"></div></div>
            <input type=file class="dz-hidden-input">
            <input id="candidate_resume_remote_url" value="https://s3/uploads/abc123/r.pdf">
        """)
        assert await _loop()._resume_upload_state(pg) == "committed"
        await b.close()


async def test_empty_remote_url_is_not_proof():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, pg = await _page(pw, """
            <div class="dropzone"><div class="dz-preview"></div></div>
            <input type=file class="dz-hidden-input">
            <input id="candidate_resume_remote_url" value="">
        """)
        assert await _loop()._resume_upload_state(pg) != "committed"
        await b.close()


async def test_plain_file_input_with_no_file_is_missing():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b, pg = await _page(pw, "<form><input type=file name=resume></form>")
        assert await _loop()._resume_upload_state(pg) == "missing"
        await b.close()


async def test_probe_never_raises_on_a_dead_page():
    class Boom:
        async def evaluate(self, *a, **k):
            raise RuntimeError("execution context destroyed")

    assert await _loop()._resume_upload_state(Boom()) == "none"


# ── DOM-keyed TeamTailor de-gate ─────────────────────────────────────────────


async def test_is_teamtailor_page_true_when_platform_label_says_so():
    lp = AgentLoop(candidate_profile=dict(_PROFILE), job_context={"platform": "teamtailor"})
    lp._platform = "teamtailor"

    class P:
        url = "https://careers.example.com/jobs/1"

    assert await lp._is_teamtailor_page(P()) is True


async def test_is_teamtailor_page_detects_via_dom_when_label_is_the_aggregator(monkeypatch):
    """The actual regression: platform is the aggregator, DOM says TeamTailor."""
    lp = _loop()
    lp._platform = "remoterocketship"
    calls = []

    async def _true(_page):
        calls.append(1)
        return True

    monkeypatch.setattr(
        "backend.app.browser_automation.adapters.teamtailor.is_teamtailor_dom", _true
    )

    class P:
        url = "https://careers.westerncomputer.com/jobs/635985"

    assert await lp._is_teamtailor_page(P()) is True
    # ...and it memoizes per host rather than probing every step.
    assert await lp._is_teamtailor_page(P()) is True
    assert len(calls) == 1


async def test_is_teamtailor_page_memoizes_negative_results(monkeypatch):
    lp = _loop()
    lp._platform = "greenhouse"
    calls = []

    async def _false(_page):
        calls.append(1)
        return False

    monkeypatch.setattr(
        "backend.app.browser_automation.adapters.teamtailor.is_teamtailor_dom", _false
    )

    class P:
        url = "https://boards.greenhouse.io/x/jobs/1"

    assert await lp._is_teamtailor_page(P()) is False
    assert await lp._is_teamtailor_page(P()) is False
    assert len(calls) == 1  # negative cached too — no per-step cost on other ATSes


async def test_is_teamtailor_page_survives_a_broken_page():
    lp = _loop()
    lp._platform = "generic"

    class P:
        @property
        def url(self):
            raise RuntimeError("page closed")

    assert await lp._is_teamtailor_page(P()) is False


async def test_resume_gate_block_limit_is_small():
    # Each block costs a turn; past a few the upload is broken, not slow.
    assert 1 <= _RESUME_GATE_BLOCK_LIMIT <= 5
