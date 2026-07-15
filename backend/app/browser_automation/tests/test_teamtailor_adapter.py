"""TeamTailor adapter + Dropzone résumé-upload tests (no LLM, no network).

Handoff v7 §5 tasked "a Recruitee adapter". Offline inspection of the live form
(careers.westerncomputer.com) proved the ATS is actually **TeamTailor**, and that
the résumé field ``#candidate_resume_remote_url`` is the REAL Dropzone.js file
input (not a paste-URL text field). The true submit-rejection cause was an
ASYNC-UPLOAD RACE: Dropzone uploads the file to storage in the background and
binds the attachment token only when that completes; submitting first yields a
silent server-side "resume required" reject.

These tests pin:
  * registry routing (teamtailor + the recruitee alias) and URL detection;
  * the teamtailor hints playbook (Dropzone résumé + wait, consent, submit);
  * ``AgentLoop._await_async_upload`` — waits for Dropzone completion
    (``.dz-success``) before proceeding, bails on ``.dz-error``, else networkidle;
  * ``AgentLoop._deterministic_file_upload`` targets the Dropzone résumé input
    (NOT the "Additional files" input) and blocks on the async upload.

Run: `pytest backend/app/browser_automation/tests/test_teamtailor_adapter.py -q`
"""
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from playwright.async_api import async_playwright  # noqa: E402

from backend.app.browser_automation.adapters.registry import get_adapter  # noqa: E402
from backend.app.browser_automation.adapters.teamtailor import TeamTailorAdapter  # noqa: E402
from backend.app.browser_automation.adapters.remoterocketship import (  # noqa: E402
    _detect_ats_from_url,
)
from backend.app.browser_automation.adapters.hints import get_platform_hints  # noqa: E402
from backend.app.browser_automation.agent.loop import AgentLoop  # noqa: E402


# ── registry routing ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [
    "teamtailor",
    "recruitee",                       # handoff alias → same adapter
    "https://acme.teamtailor.com/jobs/1",
    "careers.acme.teamtailor.com",
])
def test_teamtailor_routing(value):
    assert isinstance(get_adapter(value), TeamTailorAdapter)


def test_recruitee_alias_maps_to_teamtailor():
    a = get_adapter("recruitee")
    assert isinstance(a, TeamTailorAdapter)
    assert a.platform_name == "teamtailor"
    assert a.hints_key == "teamtailor"


# ── URL → ATS detection ──────────────────────────────────────────────────────
def test_detect_teamtailor_own_host():
    assert _detect_ats_from_url("https://acme.teamtailor.com/jobs/123-x") == "teamtailor"


def test_whitelabel_host_not_detected_by_url():
    # White-label career domains carry NO URL token — detection is by DOM at
    # runtime (AgentLoop._detect_ats_from_dom), so URL mapping must return None
    # here rather than mis-routing to some other ATS.
    assert _detect_ats_from_url(
        "https://careers.westerncomputer.com/jobs/635985-x"
    ) is None


# ── hints playbook ───────────────────────────────────────────────────────────
def test_teamtailor_hints_playbook():
    h = get_platform_hints("teamtailor")
    quirks = " ".join(h.get("quirks", [])).lower()
    # The Dropzone résumé + async-upload-wait guidance (the crux) is present.
    assert "dropzone" in quirks
    assert "candidate_resume_remote_url" in quirks
    assert "background" in quirks and "before" in quirks  # wait-before-submit
    # Required consent checkbox + submit control + success signal.
    assert "consent" in quirks
    assert any("commit" in s for s in h.get("submit_selectors", []))
    assert any("thank you" in s for s in h.get("success_patterns", []))
    # Captcha guidance must NOT misclassify a Cloudflare iframe as turnstile.
    assert "turnstile" in quirks


# ── _await_async_upload: branch logic (stubbed, no browser) ──────────────────
class _StubPage:
    def __init__(self):
        self.networkidle_called = False

    async def wait_for_load_state(self, state, timeout=0):
        self.networkidle_called = True


class _StubCtx:
    """ctx.evaluate is called first for the is-dropzone probe, then repeatedly
    for the state poll. Distinguish by a token in the script."""

    def __init__(self, is_dropzone, states):
        self.is_dropzone = is_dropzone
        self.states = list(states)
        self.poll = 0

    async def evaluate(self, script):
        if "dz-hidden-input" in script and "done" not in script:
            return self.is_dropzone
        st = self.states[min(self.poll, len(self.states) - 1)]
        self.poll += 1
        return st


@pytest.mark.asyncio
async def test_await_upload_returns_on_dropzone_success():
    page, ctx = _StubPage(), _StubCtx(
        is_dropzone=True,
        states=[
            {"done": 0, "error": 0, "errMsg": [], "processing": 1, "previews": 1},
            {"done": 1, "error": 0, "errMsg": [], "processing": 0, "previews": 1},
        ],
    )
    await AgentLoop._await_async_upload(object(), page, ctx)
    # Committed via dz-success → must NOT fall back to networkidle.
    assert page.networkidle_called is False
    assert ctx.poll >= 2


@pytest.mark.asyncio
async def test_await_upload_bails_on_dropzone_error():
    page, ctx = _StubPage(), _StubCtx(
        is_dropzone=True,
        states=[{"done": 0, "error": 1, "errMsg": ["too big"], "processing": 0, "previews": 1}],
    )
    await AgentLoop._await_async_upload(object(), page, ctx)
    assert page.networkidle_called is False  # error surfaced; no silent networkidle


@pytest.mark.asyncio
async def test_await_upload_networkidle_when_not_dropzone():
    page, ctx = _StubPage(), _StubCtx(is_dropzone=False, states=[{}])
    await AgentLoop._await_async_upload(object(), page, ctx)
    assert page.networkidle_called is True  # plain file input → settle network


# ── Playwright fixtures ──────────────────────────────────────────────────────
def _write_temp(html: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    return "file:///" + f.name.replace(os.sep, "/").lstrip("/")


def _tiny_pdf() -> str:
    p = os.path.join(tempfile.gettempdir(), "tt_test_resume.pdf")
    with open(p, "wb") as fh:
        fh.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
    return p


# Faithful to the real TeamTailor DOM: two Dropzone.js file inputs
# (dz-hidden-input); the résumé one is named ..._remote_url yet IS the real file
# input. A 'change' listener simulates Dropzone's async upload by appending a
# .dz-preview.dz-success element a beat later — exactly the completion marker
# _await_async_upload waits on.
_DROPZONE_FORM = """<!doctype html><html><head><title>t</title></head><body>
<form>
  <input type="file" class="dz-hidden-input" id="candidate_resume_remote_url"
         accept=".pdf,.doc,.docx" required style="opacity:0">
  <input type="file" class="dz-hidden-input" id="candidate_file_remote_url"
         multiple style="opacity:0">
</form>
<div class="dropzone"></div>
<script>
document.getElementById('candidate_resume_remote_url')
  .addEventListener('change', () => {
    setTimeout(() => {
      const d = document.createElement('div');
      d.className = 'dz-preview dz-success dz-complete';
      d.textContent = 'resume.pdf';
      document.body.appendChild(d);
    }, 300);
  });
</script></body></html>"""


@pytest.mark.asyncio
async def test_deterministic_upload_targets_dropzone_resume_and_waits():
    url = _write_temp(_DROPZONE_FORM)
    resume = _tiny_pdf()
    lp = AgentLoop(
        {"name": "Mark Anderson", "email": "m@e.com"},
        {"job_url": "https://careers.westerncomputer.com/jobs/1"},
        resume_path=resume,
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            actions = []
            t0 = time.monotonic()
            uploaded = await lp._deterministic_file_upload(page, None, actions)
            elapsed = time.monotonic() - t0

            # Exactly the résumé slot uploaded (never the "Additional files" input).
            assert uploaded == 1
            resume_files = await page.eval_on_selector(
                "#candidate_resume_remote_url", "el => el.files.length"
            )
            extra_files = await page.eval_on_selector(
                "#candidate_file_remote_url", "el => el.files.length"
            )
            assert resume_files == 1
            assert extra_files == 0

            # The upload action targeted the Dropzone résumé input.
            up = [a for a in actions if a.kind == "upload_file" and a.ok]
            assert up and up[0].selector == "#candidate_resume_remote_url"

            # It actually WAITED for the async Dropzone completion marker
            # (~300ms) before returning — the core race fix.
            assert elapsed >= 0.3
            has_success = await page.query_selector(".dz-success")
            assert has_success is not None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_await_upload_detects_real_dz_success_marker():
    # DOM that flips from processing → success after 400ms; the helper must
    # detect the real .dz-success selector and return (not time out / networkidle).
    html = """<!doctype html><html><body>
      <input type="file" class="dz-hidden-input" id="r">
      <div class="dz-preview dz-processing" id="pv">uploading…</div>
      <script>setTimeout(() => {
        document.getElementById('pv').className = 'dz-preview dz-success';
      }, 400);</script></body></html>"""
    url = _write_temp(html)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            t0 = time.monotonic()
            await AgentLoop._await_async_upload(object(), page, page)
            elapsed = time.monotonic() - t0
            assert 0.3 <= elapsed < 10.0  # waited for success, well under the 25s cap
        finally:
            await browser.close()
