"""Tests for universal form discovery (agent/form_discoverer.py).

The gap being closed: `adapters/generic.py` declares no `iframe_selector`, and
`AutonomousAdapter._resolve_frame` used to early-return when it was unset — so
an unknown portal that renders its form in an iframe was invisible to the loop.

These tests drive the scorer with realistic page shapes, because the property
that matters is discrimination: a real application form must win, and a search
box / newsletter signup must NOT.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.agent.form_discoverer import (  # noqa: E402
    MIN_FORM_SCORE,
    FormDiscoverer,
    FormLocation,
)


class FakeFrame:
    """A frame whose evaluate() returns a canned score payload."""

    def __init__(self, payload, url="https://portal.example/form", element=None):
        self.payload = payload
        self.url = url
        self._element = element

    async def evaluate(self, js, *args):
        if "APPLY" in js or "role=button" in js and "texts" in js:
            return {"found": False}
        return self.payload

    async def frame_element(self):
        if self._element is None:
            raise RuntimeError("detached")
        return self._element


class FakeElement:
    def __init__(self, attrs):
        self.attrs = attrs

    async def get_attribute(self, name):
        return self.attrs.get(name)


class FakePage:
    """Minimal page: a main frame plus child frames."""

    def __init__(self, main_payload, child_frames=(), apply_probe=None):
        self.main = FakeFrame(main_payload)
        self._children = list(child_frames)
        self.frames = [self.main] + self._children
        self.main_frame = self.main
        self.apply_probe = apply_probe or {"found": False}
        self.clicked = False

    async def evaluate(self, js, *args):
        if "shadowRoot" in js:
            return {"n": 0, "file": False, "email": False}
        if "aria-label" in js and "texts" in str(args):
            return self.apply_probe
        # The apply probe is the only one taking an arg list.
        if args and isinstance(args[0], list):
            return self.apply_probe
        return self.main.payload

    async def wait_for_load_state(self, *a, **k):
        return None

    async def wait_for_timeout(self, *a, **k):
        return None


def _payload(score, fields=6, email=True, file=True, name=True, submits=1):
    return {
        "score": score, "fields": fields, "hasEmail": email, "hasFile": file,
        "hasName": name, "submits": submits, "resumeish": True,
        "url": "https://portal.example/form",
    }


# ── The scorer must discriminate ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_page_level_form_is_found():
    page = FakePage(_payload(11))
    loc = await FormDiscoverer.discover(page)
    assert loc.found is True
    assert loc.where == "page"
    assert loc.iframe_selector is None


@pytest.mark.asyncio
async def test_a_search_box_is_not_mistaken_for_an_application_form():
    """One text input + a submit button must score below the threshold."""
    page = FakePage(_payload(3, fields=1, email=False, file=False, name=False, submits=1))
    loc = await FormDiscoverer.discover(page)
    assert loc.found is False
    assert loc.score < MIN_FORM_SCORE


@pytest.mark.asyncio
async def test_newsletter_signup_is_not_an_application_form():
    """Email + submit alone (no name, no file, few fields) must not win."""
    page = FakePage(_payload(4, fields=1, email=True, file=False, name=False, submits=1))
    loc = await FormDiscoverer.discover(page)
    assert loc.found is False


# ── The actual gap: an iframed form on an unknown portal ─────────────────────


@pytest.mark.asyncio
async def test_iframed_form_is_discovered_and_yields_a_selector():
    """THE case generic.py could not handle: form lives in an iframe."""
    child = FakeFrame(_payload(11), element=FakeElement({"id": "app_frame"}))
    page = FakePage(_payload(1, fields=0, email=False, file=False, name=False, submits=0),
                    child_frames=[child])
    loc = await FormDiscoverer.discover(page)
    assert loc.found is True
    assert loc.where == "iframe"
    assert loc.iframe_selector == "iframe#app_frame"


@pytest.mark.asyncio
async def test_iframe_selector_falls_back_to_src_when_no_id_or_name():
    child = FakeFrame(_payload(11), url="https://ats.example/embed/123?x=1",
                      element=FakeElement({}))
    page = FakePage(_payload(0, fields=0, email=False, file=False, name=False, submits=0),
                    child_frames=[child])
    loc = await FormDiscoverer.discover(page)
    assert loc.found is True
    assert loc.iframe_selector == 'iframe[src*="https://ats.example/embed/123"]'


@pytest.mark.asyncio
async def test_highest_scoring_frame_wins_over_a_weak_main_page():
    """A page with a nav search box + an iframed real form picks the iframe."""
    child = FakeFrame(_payload(11), element=FakeElement({"name": "apply"}))
    page = FakePage(_payload(3, fields=1, email=False, file=False, name=False),
                    child_frames=[child])
    loc = await FormDiscoverer.discover(page)
    assert loc.where == "iframe"
    assert loc.iframe_selector == 'iframe[name="apply"]'


# ── Robustness: never raise, never lie ───────────────────────────────────────


@pytest.mark.asyncio
async def test_no_form_anywhere_returns_found_false_with_evidence():
    page = FakePage(_payload(0, fields=0, email=False, file=False, name=False, submits=0))
    loc = await FormDiscoverer.discover(page)
    assert loc.found is False
    assert "no application form found" in loc.summary()


@pytest.mark.asyncio
async def test_discovery_survives_a_frame_that_throws():
    class Boom:
        url = "https://x/y"

        async def evaluate(self, *a, **k):
            raise RuntimeError("detached frame")

        async def frame_element(self):
            raise RuntimeError("detached")

    page = FakePage(_payload(11), child_frames=[Boom()])
    loc = await FormDiscoverer.discover(page)
    assert loc.found is True  # main page still wins; the bad frame is skipped


@pytest.mark.asyncio
async def test_discovery_survives_a_totally_broken_page():
    class Boom:
        frames = []
        main_frame = None

        async def evaluate(self, *a, **k):
            raise RuntimeError("page closed")

    loc = await FormDiscoverer.discover(Boom())
    assert loc.found is False


def test_form_location_summary_is_human_readable():
    loc = FormLocation(found=True, where="iframe", iframe_selector="iframe#x",
                       score=11, fields=7, has_file=True, has_email=True)
    s = loc.summary()
    assert "iframe#x" in s and "score=11" in s and "fields=7" in s
