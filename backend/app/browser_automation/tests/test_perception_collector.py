"""Unit tests for the shared perception layer (BrowserState + collector).

Uses a headless-Chromium file:// fixture (same pattern as test_detector_forms.py
and test_frame_swap.py). Proves the collector produces a correct, structured
BrowserState across the surfaces it must capture: forms, inputs, buttons,
validation errors, success messages, toasts, modals, navigation, visible text,
stability detection, and the screenshot pipeline.

Run: `pytest backend/app/browser_automation/tests/test_perception_collector.py -q`
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from playwright.async_api import async_playwright  # noqa: E402

from backend.app.browser_automation.perception import (  # noqa: E402
    BrowserState,
    BrowserStateCollector,
    ScreenshotConfig,
    StabilityConfig,
    save_screenshot,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

_FORM_HTML = """<!doctype html><html><head><title>Apply — Senior Engineer</title></head>
<body>
  <nav><input type="search" name="search" placeholder="Search jobs"></nav>
  <form action="/submit" method="post">
    <label for="fn">First name *<input id="fn" type="text" name="first_name" required></label>
    <label for="em">Email *<input id="em" type="email" name="email" required value="a@b.com"></label>
    <label for="ph">Phone<input id="ph" type="tel" name="phone"></label>
    <label for="country">Country
      <select id="country" name="country">
        <option>United States</option>
        <option>Canada</option>
        <option>United Kingdom</option>
      </select>
    </label>
    <label for="cv">Resume<input id="cv" type="file" name="resume"></label>
    <label><input type="checkbox" id="agree" name="agree" required> I agree to the terms</label>

    <!-- an inline validation error tied to a field -->
    <div class="form-group">
      <label for="ln">Last name *</label>
      <input id="ln" type="text" name="last_name" aria-invalid="true">
      <span class="field-error">Last name is required</span>
    </div>

    <button type="button">Next</button>
    <button type="submit" id="submit_app">Submit Application</button>
  </form>

  <!-- a toast / snackbar -->
  <div class="toast" role="status">Saved your progress</div>
</body></html>"""

_SUCCESS_HTML = """<!doctype html><html><head><title>Done</title></head>
<body><h1 class="headline">Thank you for applying!</h1>
<p>We have received your application and will be in touch.</p></body></html>"""

_MODAL_HTML = """<!doctype html><html><head><title>Careers</title></head>
<body>
  <div role="dialog" aria-modal="true" class="modal">
    <h2>Subscribe to job alerts</h2>
    <p>Get the latest roles in your inbox.</p>
    <button aria-label="Close">×</button>
  </div>
  <form><input name="email" type="email"></form>
</body></html>"""


def _file_url(html: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    return "file:///" + f.name.replace(os.sep, "/").lstrip("/"), f.name


async def _collect(html: str, *, quiet_ms: int = 150, timeout_ms: int = 2_000, **collect_kwargs) -> BrowserState:
    url, path = _file_url(html)
    collector = BrowserStateCollector(
        # small stability timeout keeps tests fast; a static file settles instantly
        stability=StabilityConfig(quiet_ms=quiet_ms, timeout_ms=timeout_ms, wait_network_idle=False),
        screenshot=ScreenshotConfig(fmt="png"),
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            return await collector.collect(page, **collect_kwargs)
        finally:
            await browser.close()
            os.unlink(path)


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_navigation_and_raw_surfaces():
    st = await _collect(_FORM_HTML)
    assert st.url.startswith("file:")
    assert st.title == "Apply — Senior Engineer"
    assert st.navigation.ready_state == "complete"
    assert st.navigation.is_loading is False
    assert "I agree to the terms" in st.visible_text
    assert "<form" in st.html.lower()
    assert st.error is None


@pytest.mark.asyncio
async def test_forms_and_inputs_captured():
    st = await _collect(_FORM_HTML)
    assert len(st.forms) == 1
    form = st.forms[0]
    assert form.method == "post"
    assert form.field_count >= 6

    by_id = {i.element_id: i for i in st.inputs if i.element_id}
    # required flags
    assert by_id["fn"].required is True
    assert by_id["em"].required is True
    assert by_id["ph"].required is False
    # pre-filled value surfaced
    assert by_id["em"].value == "a@b.com"
    assert by_id["em"].is_filled is True
    assert by_id["fn"].is_filled is False
    # select options surfaced
    assert by_id["country"].field_type == "select"
    assert by_id["country"].options == ["United States", "Canada", "United Kingdom"]
    # file input classified
    assert by_id["cv"].field_type == "file"
    # checkbox classified with checked state
    assert by_id["agree"].field_type == "checkbox"
    assert by_id["agree"].checked is False


@pytest.mark.asyncio
async def test_search_input_is_still_captured_but_labelled():
    # The perception layer is logic-free: unlike the agent-loop snapshot it does
    # NOT drop the nav search box — it just reports it. (Filtering is a caller
    # concern.) We assert it's present and typed so downstream code can choose.
    st = await _collect(_FORM_HTML)
    search = [i for i in st.inputs if i.name == "search"]
    assert len(search) == 1
    assert search[0].field_type == "search"


@pytest.mark.asyncio
async def test_buttons_and_submit_detection():
    st = await _collect(_FORM_HTML)
    texts = {b.text: b for b in st.buttons}
    assert "Submit Application" in texts
    assert texts["Submit Application"].is_submit is True
    assert "Next" in texts
    assert texts["Next"].is_submit is False
    # convenience view
    subs = st.submit_buttons
    assert any(b.text == "Submit Application" for b in subs)


@pytest.mark.asyncio
async def test_validation_error_captured_and_associated():
    st = await _collect(_FORM_HTML)
    verrs = st.validation_errors
    assert any("Last name is required" in m.text for m in verrs), \
        f"expected validation error, got {[m.text for m in st.messages]}"
    # It should be classified as validation (tied to a field), not generic error.
    v = next(m for m in st.messages if "Last name is required" in m.text)
    assert v.kind == "validation"
    assert st.errors  # validation errors are included in .errors too


@pytest.mark.asyncio
async def test_toast_captured():
    st = await _collect(_FORM_HTML)
    assert any(m.text == "Saved your progress" for m in st.toasts), \
        f"expected toast, got {[(m.kind, m.text) for m in st.messages]}"


@pytest.mark.asyncio
async def test_success_message_captured():
    st = await _collect(_SUCCESS_HTML)
    assert st.success_messages, f"expected success msg, got {[m.text for m in st.messages]}"
    assert any("thank you for applying" in m.text.lower() for m in st.success_messages)


@pytest.mark.asyncio
async def test_modal_captured_with_close_button():
    st = await _collect(_MODAL_HTML)
    assert st.has_modal
    modal = st.modals[0]
    assert modal.role == "dialog"
    assert modal.has_close_button is True
    assert modal.close_selector
    assert "Subscribe to job alerts" in modal.text_preview
    # It's a newsletter modal, not an application form.
    assert modal.looks_like_application is False


@pytest.mark.asyncio
async def test_stability_detected_on_static_page():
    st = await _collect(_FORM_HTML)
    assert st.is_stable is True
    assert st.stability_reason in ("quiescent", "observer_unavailable")


_LATE_CONTENT_HTML = """<!doctype html><html><head><title>Loading</title></head>
<body><div id="root"></div>
<script>
  // Simulate an SPA that hydrates its form shortly after load. The delay is
  // inside the collector's quiet window, so the MutationObserver sees the
  // insertion, re-arms, and the collector waits for it before extracting.
  setTimeout(() => {
    const f = document.createElement('form');
    f.innerHTML = '<label for="late">Email<input id="late" name="email" type="email" required></label>'
                + '<button type="submit">Submit Application</button>';
    document.getElementById('root').appendChild(f);
  }, 200);
</script></body></html>"""


@pytest.mark.asyncio
async def test_waits_for_dynamic_content():
    # With stability waiting on, the collector must not perceive the empty shell
    # — it should wait for the late-hydrated form to settle, then capture it.
    # quiet window (350ms) > hydration delay (200ms), so the mutation lands in
    # the window and resets it; the collector settles only after the form exists.
    st = await _collect(_LATE_CONTENT_HTML, quiet_ms=350, timeout_ms=4_000)
    assert st.is_stable is True
    late = [i for i in st.inputs if i.element_id == "late"]
    assert len(late) == 1, f"late-rendered input not captured; inputs={[i.selector for i in st.inputs]}"
    assert late[0].required is True
    assert any(b.text == "Submit Application" for b in st.buttons)


@pytest.mark.asyncio
async def test_screenshot_pipeline_produces_bytes(tmp_path):
    st = await _collect(_FORM_HTML)
    assert st.has_screenshot
    assert isinstance(st.screenshot, (bytes, bytearray))
    assert len(st.screenshot) > 0
    # PNG magic number, since we configured fmt="png"
    assert st.screenshot[:8] == b"\x89PNG\r\n\x1a\n"
    # save helper round-trips
    out = str(tmp_path / "shot.png")
    assert save_screenshot(st, out) == out
    assert os.path.getsize(out) > 0


@pytest.mark.asyncio
async def test_screenshot_can_be_skipped_per_call():
    st = await _collect(_FORM_HTML, screenshot=False)
    assert st.screenshot is None
    assert st.has_screenshot is False
    # everything else still collected
    assert st.inputs


_HIDDEN_RECAPTCHA = """<!doctype html><html><head><title>Apply</title></head><body>
<form>
  <label>First name *<input id="fn" required></label>
  <!-- invisible reCAPTCHA artifacts present on every Greenhouse/Lever form -->
  <textarea id="g-recaptcha-response" style="display:none"></textarea>
  <div class="grecaptcha-badge" style="width:256px;height:60px;visibility:hidden">badge</div>
  <button type="submit">Submit Application</button>
</form></body></html>"""

_VISIBLE_RECAPTCHA = """<!doctype html><html><head><title>Apply</title></head><body>
<form>
  <label>First name *<input id="fn" required></label>
  <div class="g-recaptcha" data-sitekey="x"
       style="width:304px;height:78px;display:block">reCAPTCHA checkbox</div>
  <button type="submit">Submit Application</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_hidden_recaptcha_is_not_flagged_as_captcha():
    # The invisible reCAPTCHA artifacts must NOT set captcha_present — else the
    # agent halts on every Greenhouse/Lever form.
    st = await _collect(_HIDDEN_RECAPTCHA)
    assert st.captcha_present is False, "hidden reCAPTCHA must not be a captcha wall"
    assert st.inputs  # the form is still perceived normally


@pytest.mark.asyncio
async def test_visible_recaptcha_widget_is_flagged():
    st = await _collect(_VISIBLE_RECAPTCHA)
    assert st.captcha_present is True
    assert st.captcha_kind == "recaptcha"


@pytest.mark.asyncio
async def test_to_dict_is_json_safe_and_elides_screenshot():
    import json

    st = await _collect(_FORM_HTML)
    d = st.to_dict()
    # must be JSON-serialisable (no bytes leak into the dict)
    json.dumps(d)
    assert d["screenshot_present"] is True
    assert d["screenshot_size"] > 0
    assert "screenshot" not in d
    assert "html" not in d  # omitted by default
    assert d["counts"]["inputs"] == len(st.inputs)
    # include_html opt-in
    d2 = st.to_dict(include_html=True)
    assert "html" in d2 and len(d2["html"]) > 0


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
