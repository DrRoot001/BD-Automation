"""Tests for the shared bot-walled / login-walled host classification.

Pins the SimplyHired captcha-bypass fix: SimplyHired (Cloudflare) must be
classified bot-walled so the residential proxy engages (Anti-Captcha's proxied
Cloudflare solve runs through the SAME residential IP). Fast, non-walled ATSes
(Greenhouse/Lever/Ashby) must stay direct. Login-walled hosts skip the httpx
pre-flight but do NOT get the (slow) proxy.

Run: `pytest backend/app/browser_automation/tests/test_hosts_classification.py -q`
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.hosts import (  # noqa: E402
    BOT_WALLED_HOSTS,
    LOGIN_WALLED_HOSTS,
    PREFLIGHT_SKIP_HOSTS,
    is_bot_walled,
    should_skip_preflight,
)


def test_simplyhired_is_bot_walled_and_proxied():
    # The exact host that motivated the fix.
    assert is_bot_walled("www.simplyhired.com") is True
    assert is_bot_walled("simplyhired") is True
    assert should_skip_preflight("www.simplyhired.com") is True


def test_walled_aggregators_are_proxied():
    for h in ("remoterocketship.com", "himalayas.app", "hiring.cafe",
              "www.talent.com", "glassdoor.com", "builtin.com"):
        assert is_bot_walled(h) is True, h


def test_fast_atses_stay_direct():
    # Greenhouse/Lever/Ashby are NOT walled — proxying them would 3-4x latency.
    for h in ("boards.greenhouse.io", "job-boards.greenhouse.io",
              "jobs.lever.co", "jobs.ashbyhq.com"):
        assert is_bot_walled(h) is False, h
        assert should_skip_preflight(h) is False, h


def test_login_walled_skip_preflight_but_no_proxy():
    for h in ("dice.com", "careers.icims.com", "jobs.smartrecruiters.com"):
        assert should_skip_preflight(h) is True, h
        assert is_bot_walled(h) is False, h  # login gate, not an IP wall


def test_preflight_is_superset_of_bot_walled():
    for h in BOT_WALLED_HOSTS:
        assert h in PREFLIGHT_SKIP_HOSTS
    for h in LOGIN_WALLED_HOSTS:
        assert h in PREFLIGHT_SKIP_HOSTS
    # no overlap between the two sets
    assert not (set(BOT_WALLED_HOSTS) & set(LOGIN_WALLED_HOSTS))


def test_empty_input_is_safe():
    assert is_bot_walled("") is False
    assert is_bot_walled(None) is False  # type: ignore[arg-type]
    assert should_skip_preflight("") is False


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
