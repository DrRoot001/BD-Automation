"""AgentLoop test: committing an Ashby/React "labeled-radio" group option.

Ashby renders single-choice screening questions as a radio GROUP where every
option is a visually-hidden ``<input type=radio value="on">`` (all options share
value="on") and the human-readable choice lives in a sibling ``<label for=id>``.
The live SentiLink (Ashby) run STUCK on exactly this widget: the loop ``.check()``
-ed whatever option INDEX the LLM named (…-labeled-radio-0) without mapping the
target value, and read the radio's value back as "on" (never the option text),
so it looped on the wrong option until the repetition guard aborted.

``_commit_radio_group`` fixes it: resolve the option whose LABEL matches the
target value within the same group, then click that <label> so the SPA's change
handler fires. This pins that behaviour against a faithful fixture.

Run: `pytest backend/app/browser_automation/tests/test_labeled_radio_commit.py -q`
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from playwright.async_api import async_playwright  # noqa: E402

from backend.app.browser_automation.agent.loop import (  # noqa: E402
    _commit_checkbox,
    _commit_radio_group,
)


# Faithful to the real Ashby DOM: opacity:0 inputs, value="on" on every option,
# option text in a separate label[for=id]. A 'change' listener records the
# committed option text (simulating the SPA/React state that gates validation) —
# native <label for> clicks toggle the input AND fire 'change', which is exactly
# what the fix relies on.
def _fixture(options):
    opts_html = []
    for i, text in enumerate(options):
        opts_html.append(
            f'<span class="_container" data-disabled="false">'
            f'<span class="_circle"></span>'
            f'<input type="radio" id="grp-labeled-radio-{i}" name="grp" '
            f'style="opacity:0;width:24px;height:24px">'
            f'</span><label for="grp-labeled-radio-{i}">{text}</label>'
        )
    return (
        "<!doctype html><html><head><title>t</title></head><body>"
        "<fieldset><legend>When using Python for data analysis, rate your proficiency</legend>"
        + "".join(opts_html) +
        '</fieldset><div id="committed"></div>'
        "<script>"
        "document.querySelectorAll('input[type=radio][name=grp]').forEach(inp => {"
        "  inp.addEventListener('change', () => {"
        "    const l = document.querySelector('label[for=\"'+inp.id+'\"]');"
        "    document.getElementById('committed').textContent = l ? l.textContent : '';"
        "  });"
        "});"
        "</script></body></html>"
    )


async def _run(options, target_selector, value):
    html = _fixture(options)
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    url = "file:///" + f.name.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            loc = page.locator(target_selector).first
            ok = await _commit_radio_group(page, loc, value)
            committed = await page.locator("#committed").inner_text()
            # which input ended up actually checked
            checked_idx = await page.evaluate(
                "() => { const r = Array.from(document.querySelectorAll('input[type=radio][name=grp]'))"
                ".findIndex(i => i.checked); return r; }"
            )
            return ok, committed, checked_idx
        finally:
            await browser.close()
            os.unlink(f.name)


OPTIONS = ["None", "Low", "Medium", "High"]


# Lever "card" radios: NO id, input wrapped in a <label>, DISTINCT value attr.
def _lever_fixture(options):
    opts = "".join(
        f'<label><input type="radio" name="grp" value="{t}" required>'
        f'<span class="application-answer-alternative">{t}</span></label>'
        for t in options
    )
    return (
        "<!doctype html><html><head><title>t</title></head><body>"
        "<div class='q'>" + opts + "</div><div id='committed'></div>"
        "<script>"
        "document.querySelectorAll('input[type=radio][name=grp]').forEach(inp => {"
        "  inp.addEventListener('change', () => {"
        "    document.getElementById('committed').textContent = inp.value;"
        "  });"
        "});"
        "</script></body></html>"
    )


async def _run_lever(options, value, target_value):
    html = _lever_fixture(options)
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    url = "file:///" + f.name.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            loc = page.locator(f'input[name="grp"][value="{target_value}"]').first
            ok = await _commit_radio_group(page, loc, value)
            committed = await page.locator("#committed").inner_text()
            checked_val = await page.evaluate(
                "() => { const i = Array.from(document.querySelectorAll('input[type=radio][name=grp]'))"
                ".find(x => x.checked); return i ? i.value : null; }"
            )
            return ok, committed, checked_val
        finally:
            await browser.close()
            os.unlink(f.name)


# Recruitee-style consent: a styled checkbox with a label[for] + the Rails
# hidden-input pair; commits only on a real change event.
_CONSENT = """<!doctype html><html><head><title>t</title></head><body>
<div class="consent">
  <input name="candidate[consent_given]" type="hidden" value="0" tabindex="-1">
  <input id="candidate_consent_given" name="candidate[consent_given]" type="checkbox" value="1">
</div>
<label for="candidate_consent_given">Required. By submitting this application, I agree.</label>
<div id="committed">unchecked</div>
<script>
document.getElementById('candidate_consent_given').addEventListener('change', function(){
  document.getElementById('committed').textContent = this.checked ? 'CHECKED' : 'unchecked';
});
</script></body></html>"""


async def _run_checkbox(html, selector, want):
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    url = "file:///" + f.name.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            ok = await _commit_checkbox(page, page.locator(selector).first, want)
            committed = await page.locator("#committed").inner_text()
            checked = await page.locator(selector).is_checked()
            return ok, committed, checked
        finally:
            await browser.close()
            os.unlink(f.name)


@pytest.mark.asyncio
async def test_consent_checkbox_commits_and_fires_change():
    ok, committed, checked = await _run_checkbox(_CONSENT, "#candidate_consent_given", True)
    assert ok is True
    assert checked is True
    assert committed == "CHECKED"     # the framework's change handler fired


@pytest.mark.asyncio
async def test_lever_card_radio_commits_by_value():
    # Lever work-auth card: no id, wrapping label, value="Yes"/"No". Must commit
    # (fire change) and end up checked — the fix for the STUCK re-fill loop.
    ok, committed, checked_val = await _run_lever(["Yes", "No"], "Yes", "Yes")
    assert ok is True
    assert committed == "Yes"
    assert checked_val == "Yes"


@pytest.mark.asyncio
async def test_lever_card_radio_selects_no():
    ok, committed, checked_val = await _run_lever(["Yes", "No"], "No", "No")
    assert ok is True
    assert committed == "No"
    assert checked_val == "No"


@pytest.mark.asyncio
async def test_exact_option_committed_and_change_fired():
    # target selector is the FIRST option, but the value is "Low" (index 1):
    # the fix must select by LABEL, not by the index the caller happened to name.
    ok, committed, checked_idx = await _run(OPTIONS, "#grp-labeled-radio-0", "Low")
    assert ok is True
    assert committed == "Low"          # the SPA change handler fired for "Low"
    assert checked_idx == 1            # radio-1 ("Low") is checked, NOT radio-0


@pytest.mark.asyncio
async def test_wrong_index_still_picks_right_option():
    # caller names index 0 ("None") but wants "High" (index 3).
    ok, committed, checked_idx = await _run(OPTIONS, "#grp-labeled-radio-0", "High")
    assert ok is True
    assert committed == "High"
    assert checked_idx == 3


@pytest.mark.asyncio
async def test_closest_match_snaps_to_real_option():
    # a fuzzy AI value must snap to the nearest real option label.
    ok, committed, _ = await _run(OPTIONS, "#grp-labeled-radio-0", "medium proficiency")
    assert ok is True
    assert committed == "Medium"


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
