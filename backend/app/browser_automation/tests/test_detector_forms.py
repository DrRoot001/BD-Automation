"""Detector regression tests for checkbox consolidation + multi-step detection.

Uses a headless-Chromium file:// fixture (same pattern as test_frame_swap.py).

Proves the two behaviors that changed:
  * a lone named checkbox (consent/terms) stays a single boolean field —
    NO regression to the critical agree-to-terms path;
  * 2+ checkboxes sharing a `name` collapse into ONE multi_select field with
    the full option/value list (previously N independent Yes/No fields);
  * "Step N of M" captions yield an accurate step total instead of hardcoded 2.

Run: `pytest backend/app/browser_automation/tests/test_detector_forms.py -q`
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from playwright.async_api import async_playwright  # noqa: E402
from backend.app.browser_automation.forms.detector import detect_form  # noqa: E402

_FORM_HTML = """<!doctype html><html><body>
<div class="progress"><span>Step 2 of 4</span></div>
<form>
  <label>Full name <input type="text" name="full_name" required></label>

  <!-- Multi-select group: 3 checkboxes sharing name="skills" -->
  <fieldset><legend>Which languages do you know? *</legend>
    <label><input type="checkbox" name="skills" value="py"> Python</label>
    <label><input type="checkbox" name="skills" value="js"> JavaScript</label>
    <label><input type="checkbox" name="skills" value="go"> Go</label>
  </fieldset>

  <!-- Lone consent checkbox (named) -->
  <label><input type="checkbox" name="agree" id="agree" required> I agree to the terms</label>

  <!-- Unnamed checkbox -->
  <label><input type="checkbox" id="subscribe"> Subscribe to updates</label>

  <button type="button">Next</button>
  <button type="submit">Submit</button>
</form>
</body></html>"""


async def _detect_fixture():
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(_FORM_HTML)
        path = f.name
    file_url = "file:///" + path.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(file_url)
            return await detect_form(page, skip_scroll=True)
        finally:
            await browser.close()
            os.unlink(path)


@pytest.mark.asyncio
async def test_checkbox_group_consolidates_and_consent_stays_boolean():
    form = await _detect_fixture()
    by_type = {}
    for fld in form.fields:
        by_type.setdefault(fld.field_type, []).append(fld)
    checkboxes = by_type.get("checkbox", [])

    # The 3 skills checkboxes collapse to exactly ONE multi_select field.
    groups = [f for f in checkboxes if f.multi_select]
    assert len(groups) == 1, f"expected 1 multi-select group, got {len(groups)}"
    grp = groups[0]
    assert grp.selector == "input[name='skills']"
    assert grp.options and len(grp.options) == 3
    assert grp.raw_values and set(grp.raw_values) == {"py", "js", "go"}
    assert grp.required is True  # '*' in the legend

    # The consent checkbox remains a single boolean field (options None).
    consent = [f for f in checkboxes if not f.multi_select and "agree" in f.selector.lower()]
    assert len(consent) == 1, "consent checkbox must stay a single boolean field"
    assert consent[0].options is None
    assert consent[0].required is True

    # The unnamed checkbox is still detected as its own boolean field.
    unnamed = [f for f in checkboxes if "subscribe" in f.selector.lower()]
    assert len(unnamed) == 1


@pytest.mark.asyncio
async def test_multi_step_count_from_caption():
    form = await _detect_fixture()
    assert form.steps == 4, f"expected 4 steps from 'Step 2 of 4', got {form.steps}"
    assert form.current_step == 2


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
