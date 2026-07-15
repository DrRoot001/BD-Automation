"""Executor test: committing values into custom react-select dropdowns.

Greenhouse/Ashby/Lever/Workday render screening-question dropdowns as custom
react-select widgets (a <div> combobox + a portal option menu), NOT native
<select>. The live Greenhouse run STUCK because the executor only handled native
selects. This pins the fix: _commit_dropdown opens the widget and clicks the
matching option (native selects still work too).

Run: `pytest backend/app/browser_automation/tests/test_dropdown_commit.py -q`
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from playwright.async_api import async_playwright  # noqa: E402

from backend.app.browser_automation.autonomous import ActionExecutor  # noqa: E402


_REACT_SELECT = """<!doctype html><html><head><title>t</title></head><body>
<div class="select__control" id="ctrl" tabindex="0" style="border:1px solid #ccc;width:260px">
  <span class="select__placeholder" id="ph">Select…</span>
  <input id="rs-input" role="combobox" style="opacity:0;width:2px">
</div>
<div id="menu" style="display:none">
  <div class="select__option" role="option">Yes</div>
  <div class="select__option" role="option">No</div>
  <div class="select__option" role="option">Prefer not to say</div>
</div>
<div id="committed"></div>
<script>
const ctrl=document.getElementById('ctrl'), menu=document.getElementById('menu');
ctrl.addEventListener('click', () => { menu.style.display='block'; });
document.querySelectorAll('.select__option').forEach(o => {
  o.addEventListener('click', () => {
    document.getElementById('committed').textContent = o.textContent;
    document.getElementById('ph').textContent = o.textContent;
    menu.style.display='none';
  });
});
</script></body></html>"""

_NATIVE = """<!doctype html><html><head><title>t</title></head><body>
<select id="sel"><option value="">--</option><option>United States</option><option>Canada</option></select>
</body></html>"""

# Ashby-style labeled-radio group: opacity:0 inputs all sharing value="on",
# option text in a <label for=id>. A change listener records the committed label.
_LABELED_RADIO = """<!doctype html><html><head><title>t</title></head><body>
<fieldset>
  <input type="radio" id="grp-labeled-radio-0" name="grp" style="opacity:0"><label for="grp-labeled-radio-0">None</label>
  <input type="radio" id="grp-labeled-radio-1" name="grp" style="opacity:0"><label for="grp-labeled-radio-1">Low</label>
  <input type="radio" id="grp-labeled-radio-2" name="grp" style="opacity:0"><label for="grp-labeled-radio-2">Medium</label>
  <input type="radio" id="grp-labeled-radio-3" name="grp" style="opacity:0"><label for="grp-labeled-radio-3">High</label>
</fieldset>
<div id="committed"></div>
<script>
document.querySelectorAll('input[type=radio][name=grp]').forEach(inp => {
  inp.addEventListener('change', () => {
    const l = document.querySelector('label[for="'+inp.id+'"]');
    document.getElementById('committed').textContent = l ? l.textContent : '';
  });
});
</script></body></html>"""


async def _commit(html, selector, value):
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    url = "file:///" + f.name.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            res = await ActionExecutor()._commit_dropdown(page, page, selector, value)
            committed = None
            try:
                committed = await page.locator("#committed").inner_text()
            except Exception:
                pass
            sel_val = None
            try:
                sel_val = await page.locator("#sel").input_value()
            except Exception:
                pass
            return res, committed, sel_val
        finally:
            await browser.close()
            os.unlink(f.name)


# Ashby-location-style autocomplete combobox: an input[role=combobox]; typing
# reveals a listbox of [role=option]s; clicking one sets the input value, records
# #committed, and CLOSES the menu. A stuck-open menu (#menu visible) would block
# later clicks — the STUCK spiral. This exercises the _do_fill combobox commit.
_AUTOCOMPLETE = """<!doctype html><html><head><title>t</title></head><body>
<input id="loc" role="combobox" aria-autocomplete="list" placeholder="Start typing...">
<div id="menu" role="listbox" style="display:none">
  <div role="option">United States</div>
  <div role="option">United Kingdom</div>
  <div role="option">Canada</div>
</div>
<div id="committed"></div>
<script>
const inp=document.getElementById('loc'), menu=document.getElementById('menu');
inp.addEventListener('input', () => { menu.style.display = inp.value ? 'block' : 'none'; });
document.querySelectorAll('#menu [role=option]').forEach(o => {
  o.addEventListener('click', () => {
    inp.value = o.textContent;
    document.getElementById('committed').textContent = o.textContent;
    menu.style.display='none';          // committing CLOSES the menu
  });
});
</script></body></html>"""


async def _fill(html, selector, value):
    """Drive a FILL NextAction through the full executor (exercises _do_fill →
    combobox commit)."""
    from backend.app.browser_automation.reasoning.models import ActionType, NextAction
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    url = "file:///" + f.name.replace(os.sep, "/").lstrip("/")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            action = NextAction(type=ActionType.FILL, selector=selector, value=value, field_label="Location")
            res = await ActionExecutor().execute(page, page, action, {})
            committed = await page.locator("#committed").inner_text()
            menu_open = await page.locator("#menu").is_visible()
            input_val = await page.locator("#loc").input_value()
            return res, committed, menu_open, input_val
        finally:
            await browser.close()
            os.unlink(f.name)


@pytest.mark.asyncio
async def test_commits_react_select_option():
    res, committed, _ = await _commit(_REACT_SELECT, "#ctrl", "No")
    assert res.ok is True, res.note
    assert committed == "No"


@pytest.mark.asyncio
async def test_react_select_closest_match():
    res, committed, _ = await _commit(_REACT_SELECT, "#ctrl", "prefer not")
    assert res.ok is True
    assert committed == "Prefer not to say"


@pytest.mark.asyncio
async def test_native_select_still_works():
    res, _, sel_val = await _commit(_NATIVE, "#sel", "Canada")
    assert res.ok is True
    assert sel_val == "Canada"


@pytest.mark.asyncio
async def test_commits_labeled_radio_group_by_label():
    # SELECT_OPTION targeting index 0 ("None") but value "High" → the fix must
    # pick the option by LABEL, not the named index, and fire the change event.
    res, committed, _ = await _commit(_LABELED_RADIO, "#grp-labeled-radio-0", "High")
    assert res.ok is True, res.note
    assert committed == "High"


@pytest.mark.asyncio
async def test_labeled_radio_closest_match():
    res, committed, _ = await _commit(_LABELED_RADIO, "#grp-labeled-radio-0", "medium proficiency")
    assert res.ok is True
    assert committed == "Medium"


@pytest.mark.asyncio
async def test_fill_combobox_commits_and_closes_menu():
    # The Ashby-location STUCK fix: a FILL on an autocomplete must pick the option
    # AND close the suggestion menu (so it can't block later clicks).
    res, committed, menu_open, input_val = await _fill(_AUTOCOMPLETE, "#loc", "United States")
    assert res.ok is True, res.note
    assert committed == "United States"     # an option was actually selected
    assert menu_open is False               # menu closed → no stuck-open overlay
    assert input_val == "United States"


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
