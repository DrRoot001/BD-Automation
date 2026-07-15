"""Routing + wiring tests for the two new adapters:

  * Careers Page (Manatal ATS) — careers-page.com hosted forms, reached directly
    or via an aggregator external-apply redirect.
  * Remote100K — an external-apply job SOURCE that resolves the inner ATS and
    hands off to the shared loop (dedicated RemoteRocketship passthrough).

These pin the integration contract the AgentLoop relies on: registry routing,
URL->ATS detection (which drives both aggregator handoff AND the AgentLoop's
mid-run platform reclassify), host-substring hints resolution, and the
CareersPage /apply URL normalization.

Run: `pytest backend/app/browser_automation/tests/test_new_source_adapters.py -q`
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.adapters.careerspage import CareersPageAdapter  # noqa: E402
from backend.app.browser_automation.adapters.registry import get_adapter  # noqa: E402
from backend.app.browser_automation.adapters.remote100k import Remote100KAdapter  # noqa: E402
from backend.app.browser_automation.adapters.remoterocketship import (  # noqa: E402
    RemoteRocketshipAdapter,
    _detect_ats_from_url,
)
from backend.app.browser_automation.adapters.hints import get_platform_hints  # noqa: E402


# ── registry routing ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [
    "careerspage", "careers-page", "manatal", "careers-page.com",
    "https://careers-page.com/hr-pod/job/7XV7985X/apply",
])
def test_careerspage_routing(value):
    assert isinstance(get_adapter(value), CareersPageAdapter)


@pytest.mark.parametrize("value", ["remote100k", "remote100k.com", "www.remote100k.com"])
def test_remote100k_routing(value):
    a = get_adapter(value)
    assert isinstance(a, Remote100KAdapter)
    # dedicated identity, but still IS-A passthrough
    assert a.platform_name == "remote100k"
    assert isinstance(a, RemoteRocketshipAdapter)


def test_remoterocketship_still_routes_itself():
    from backend.app.browser_automation.adapters.remoterocketship import RemoteRocketshipAdapter as RR
    a = get_adapter("remoterocketship")
    assert isinstance(a, RR) and not isinstance(a, Remote100KAdapter)


# ── URL -> ATS detection (aggregator handoff + AgentLoop reclassify) ──────────
def test_detect_careerspage_by_host_only():
    # detected regardless of slug/job-id/query (spec: host only)
    assert _detect_ats_from_url("https://careers-page.com/hr-pod/job/7XV7985X/apply") == "careerspage"
    assert _detect_ats_from_url("https://careers-page.com/acme/job/ABC123") == "careerspage"
    assert _detect_ats_from_url("https://careers-page.com/x/job/1?utm=y&ref=z") == "careerspage"


def test_detect_still_maps_known_ats():
    assert _detect_ats_from_url("https://jobs.ashbyhq.com/co/uuid") == "ashby"
    assert _detect_ats_from_url("https://boards.greenhouse.io/co/jobs/1") == "greenhouse"
    assert _detect_ats_from_url("https://jobs.lever.co/co/uuid") == "lever"


def test_source_hosts_are_not_ats():
    # remote100k.com is a SOURCE, not an ATS — must NOT be mistaken for a form.
    assert _detect_ats_from_url("https://remote100k.com/job/1") is None


def test_careerplug_routing_and_detection():
    from backend.app.browser_automation.adapters.careerplug import (
        CareerPlugAdapter,
        careerplug_apply_url,
    )
    for v in ("careerplug", "careerplug.com", "https://x.careerplug.com/jobs/123"):
        assert isinstance(get_adapter(v), CareerPlugAdapter)
    assert _detect_ats_from_url("https://x.careerplug.com/jobs/3479286") == "careerplug"
    # not-yet-an-apply job path is normalized to the Rails /apps/new form
    assert careerplug_apply_url("https://x.careerplug.com/jobs/123") == "https://x.careerplug.com/jobs/123/apps/new"
    assert careerplug_apply_url("https://x.careerplug.com/jobs/123/apps/new") == "https://x.careerplug.com/jobs/123/apps/new"


# ── hints resolution (host-substring, longest-first) ─────────────────────────
def test_careerspage_hints_resolve_from_host():
    h = get_platform_hints("careers-page.com")
    assert any("thank you for your application" in s for s in h.get("success_patterns", []))
    joined = " ".join(h.get("quirks", [])).lower()
    # the Manatal-specific playbook (verified against the real DOM) is present
    assert "notice period" in joined
    assert "terms" in joined            # required terms_and_condition checkbox
    assert "full name" in joined        # single Full Name field, not First/Last
    assert "native <select>" in joined  # native selects, not custom widgets


# ── CareersPage /apply URL normalization ─────────────────────────────────────
@pytest.mark.asyncio
async def test_careerspage_appends_apply_to_job_path():
    a = CareersPageAdapter()
    out = await a._resolve_target_url(None, "https://careers-page.com/acme/job/7XV7985X")
    assert out == "https://careers-page.com/acme/job/7XV7985X/apply"


@pytest.mark.asyncio
async def test_careerspage_leaves_apply_url_untouched():
    a = CareersPageAdapter()
    url = "https://careers-page.com/acme/job/7XV7985X/apply"
    assert await a._resolve_target_url(None, url) == url


@pytest.mark.asyncio
async def test_careerspage_strips_query_when_appending_apply():
    a = CareersPageAdapter()
    out = await a._resolve_target_url(None, "https://careers-page.com/acme/job/ID?utm=x")
    assert out == "https://careers-page.com/acme/job/ID/apply"


@pytest.mark.asyncio
async def test_careerspage_leaves_non_job_path_untouched():
    a = CareersPageAdapter()
    url = "https://careers-page.com/acme/careers"
    assert await a._resolve_target_url(None, url) == url


# ── Manatal salary currency/frequency deterministic fixup ────────────────────
import tempfile  # noqa: E402

from backend.app.browser_automation.adapters.careerspage import (  # noqa: E402
    apply_manatal_salary_format,
    careerspage_apply_url,
)

# Faithful to the real Manatal DOM: a CURRENCY <select> defaulting to a non-USD
# first option, and a FREQUENCY <select> defaulting to 'Hourly'. The currency
# options are injected 400ms LATE to exercise the poll (matching the real form).
_SALARY_HTML = """<!doctype html><html><head><title>t</title></head><body>
<form>
  <select id="freq"><option>Hourly</option><option>Daily</option><option>Weekly</option>
    <option>Monthly</option><option>Yearly</option></select>
  <select id="cur"></select>
</form>
<script>
  // frequency ready immediately; currency populated late (like Manatal).
  setTimeout(() => {
    const c = document.getElementById('cur');
    ['Barbados dollar','Euro','US Dollar','Kuwaiti Dinar'].forEach(t => {
      const o = document.createElement('option'); o.text = t; c.add(o);
    });
  }, 400);
</script></body></html>"""


def test_careerspage_apply_url_normalization():
    assert careerspage_apply_url("https://careers-page.com/x/job/ID") == "https://careers-page.com/x/job/ID/apply"
    assert careerspage_apply_url("https://careers-page.com/x/job/ID/apply") == "https://careers-page.com/x/job/ID/apply"
    assert careerspage_apply_url("https://careers-page.com/x/job/ID?q=1") == "https://careers-page.com/x/job/ID/apply"
    # non-job path untouched
    assert careerspage_apply_url("https://careers-page.com/x/careers") == "https://careers-page.com/x/careers"


@pytest.mark.asyncio
async def test_manatal_salary_format_sets_usd_yearly_after_poll():
    from playwright.async_api import async_playwright
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(_SALARY_HTML)
    f.close()
    url = "file:///" + f.name.replace(os.sep, "/").lstrip("/")
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True)
            page = await b.new_page()
            await page.goto(url)
            changed = await apply_manatal_salary_format(page)  # must poll past the 400ms delay
            freq = await page.locator("#freq").evaluate("s => s.options[s.selectedIndex].text")
            cur = await page.locator("#cur").evaluate("s => s.options[s.selectedIndex].text")
            await b.close()
        assert changed is True
        assert freq == "Yearly"
        assert cur == "US Dollar"          # NOT the default 'Barbados dollar'
    finally:
        os.unlink(f.name)


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
