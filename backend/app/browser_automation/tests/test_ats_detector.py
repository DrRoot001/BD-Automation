"""Tests for DOM-signature ATS detection (agent/ats_detector.py).

A hit here makes the loop swap platforms mid-run and inject a different ATS's
selector cheat-sheet, so the property under test is mostly NEGATIVE: ordinary
markup on unrelated sites must NOT be classified. "I don't know" is safe; a
confident wrong answer is not.

Uses a fake page whose ``evaluate`` runs the signature JS against a real HTML
string via a minimal selector matcher, so we exercise the actual expressions.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.agent.ats_detector import ATSDetector  # noqa: E402


class FakePage:
    """Evaluates the detector's JS against an HTML string.

    Only supports what the signatures actually use: querySelector with tag /
    id / class / attribute-contains selectors, and the meta[name=generator]
    read. Good enough to prove which markup does and doesn't match.
    """

    def __init__(self, html: str):
        self.html = html

    async def evaluate(self, js: str, *args):
        # meta generator probe
        if "meta[name=\"generator\"]" in js:
            m = re.search(
                r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)',
                self.html, re.I,
            )
            return (m.group(1).lower() if m else "")
        # Signature probes: extract every quoted selector and test each.
        selectors: list[str] = []
        for chunk in re.findall(r"querySelector\(\s*(.*?)\s*\)", js, re.S):
            for q in re.findall(r"'([^']+)'", chunk):
                selectors.extend(s.strip() for s in q.split(",") if s.strip())
        return any(self._matches(s) for s in selectors)

    def _matches(self, sel: str) -> bool:
        # [attr*="value"]  /  tag[attr*="value"]
        m = re.match(r'^(\w*)\[([\w-]+)\*=["\']([^"\']+)["\']\]$', sel)
        if m:
            tag, attr, val = m.groups()
            for t, attrs in self._tags():
                if tag and t.lower() != tag.lower():
                    continue
                if val.lower() in (attrs.get(attr, "").lower()):
                    return True
            return False
        # [attr="value"]
        m = re.match(r'^(\w*)\[([\w-]+)=["\']([^"\']+)["\']\]$', sel)
        if m:
            tag, attr, val = m.groups()
            for t, attrs in self._tags():
                if tag and t.lower() != tag.lower():
                    continue
                if attrs.get(attr, "").lower() == val.lower():
                    return True
            return False
        # #id
        if sel.startswith("#"):
            return bool(re.search(rf'id=["\']{re.escape(sel[1:])}["\']', self.html, re.I))
        # .class  /  [class*="x"]
        if sel.startswith("."):
            return bool(re.search(rf'class=["\'][^"\']*\b{re.escape(sel[1:])}\b', self.html, re.I))
        m = re.match(r'^\[class\*=["\']([^"\']+)["\']\]$', sel)
        if m:
            return bool(re.search(rf'class=["\'][^"\']*{re.escape(m.group(1))}', self.html, re.I))
        # bare tag / custom element
        if re.match(r"^[\w-]+$", sel):
            return bool(re.search(rf"<{re.escape(sel)}[\s>]", self.html, re.I))
        return False

    def _tags(self):
        for m in re.finditer(r"<(\w+)([^>]*)>", self.html):
            tag, raw = m.group(1), m.group(2)
            attrs = dict(re.findall(r'([\w-]+)=["\']([^"\']*)["\']', raw))
            yield tag, attrs


@pytest.fixture(autouse=True)
def _no_teamtailor(monkeypatch):
    """Neutralise the TeamTailor probe unless a test opts in."""
    async def _false(_page):
        return False

    monkeypatch.setattr(
        "backend.app.browser_automation.adapters.teamtailor.is_teamtailor_dom", _false
    )


# ── Negative cases: the whole point of the module ────────────────────────────


@pytest.mark.asyncio
async def test_generic_application_form_is_not_lever():
    """`<form id="application-form">` is a common generic id, NOT a Lever tell."""
    page = FakePage('<form id="application-form"><input name="email"></form>')
    assert await ATSDetector.detect(page) is None


@pytest.mark.asyncio
async def test_plain_rails_turbo_page_is_not_teamtailor():
    """<turbo-frame> is any Rails/Hotwire app."""
    page = FakePage('<turbo-frame id="x"><form><input name="q"></form></turbo-frame>')
    assert await ATSDetector.detect(page) is None


@pytest.mark.asyncio
async def test_unrelated_class_prefixes_do_not_match():
    """'jv-', 'spl-', 'workday-', 'wkb-' as bare class substrings must not match."""
    page = FakePage(
        '<div class="jv-nav"></div><div class="spl-container"></div>'
        '<div class="workday-banner"></div><div class="wkb-thing"></div>'
        '<div class="breezy-footer"></div>'
    )
    assert await ATSDetector.detect(page) is None


@pytest.mark.asyncio
async def test_empty_page_returns_none():
    assert await ATSDetector.detect(FakePage("<html><body></body></html>")) is None


@pytest.mark.asyncio
async def test_detector_never_raises_on_a_broken_page():
    class Boom:
        async def evaluate(self, *a, **k):
            raise RuntimeError("page closed")

    assert await ATSDetector.detect(Boom()) is None


# ── Positive cases: real vendor-owned signatures ─────────────────────────────


@pytest.mark.asyncio
async def test_greenhouse_iframe_is_detected():
    page = FakePage('<div><iframe id="grnhse_iframe" src="https://boards.greenhouse.io/x"></iframe></div>')
    assert await ATSDetector.detect(page) == "greenhouse"


@pytest.mark.asyncio
async def test_lever_is_detected_by_vendor_asset_not_generic_id():
    page = FakePage('<script src="https://jobs.lever.co/assets/app.js"></script>')
    assert await ATSDetector.detect(page) == "lever"


@pytest.mark.asyncio
async def test_ashby_embed_root_is_detected():
    page = FakePage('<div id="ashby_embed"></div>')
    assert await ATSDetector.detect(page) == "ashby"


@pytest.mark.asyncio
async def test_workday_automation_id_is_detected():
    page = FakePage('<div data-automation-id="jobPostingHeader">Role</div>')
    assert await ATSDetector.detect(page) == "workday"


@pytest.mark.asyncio
async def test_icims_iframe_is_detected():
    page = FakePage('<iframe id="icims_content_iframe" src="https://careers-x.icims.com/"></iframe>')
    assert await ATSDetector.detect(page) == "icims"


@pytest.mark.asyncio
async def test_meta_generator_is_trusted():
    page = FakePage('<meta name="generator" content="Greenhouse">')
    assert await ATSDetector.detect(page) == "greenhouse"


@pytest.mark.asyncio
async def test_teamtailor_delegates_to_the_verified_dom_probe(monkeypatch):
    async def _true(_page):
        return True

    monkeypatch.setattr(
        "backend.app.browser_automation.adapters.teamtailor.is_teamtailor_dom", _true
    )
    assert await ATSDetector.detect(FakePage("<html></html>")) == "teamtailor"
