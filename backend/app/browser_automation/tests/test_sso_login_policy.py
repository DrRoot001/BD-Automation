"""Global 'manual email+password login only — never Google/SSO' policy tests.

Every adapter runs through the shared engines, so the guard lives there:
  * the SSO text/href classifiers,
  * the new-framework action executor refuses to click an SSO control.

The legacy AgentLoop's click + navigate_url guards are enforced inside its big
execute path; here we pin the shared classifiers and the new-framework executor
(which had no guard before).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.agent.loop import _is_sso_text, _is_sso_href  # noqa: E402
from backend.app.browser_automation.autonomous.action_executor import ActionExecutor  # noqa: E402


@pytest.mark.parametrize("text", [
    "Continue with Google", "Sign in with Google", "Log in with Apple",
    "Continue with LinkedIn", "Sign in with Microsoft", "Use Google Account",
])
def test_sso_text_detected(text):
    assert _is_sso_text(text) is True


@pytest.mark.parametrize("text", [
    "Continue", "Sign in", "Submit application", "Email", "Password",
])
def test_non_sso_text_allowed(text):
    assert _is_sso_text(text) is False


@pytest.mark.parametrize("href", [
    "https://accounts.google.com/o/oauth2/v2/auth?client_id=x",
    "https://appleid.apple.com/auth/authorize",
    "https://www.facebook.com/v12.0/dialog/oauth",
    "https://login.microsoftonline.com/common/oauth2/authorize",
    "https://example.com/auth/google/callback",
])
def test_sso_href_detected(href):
    assert _is_sso_href(href) is True


def test_non_sso_href_allowed():
    assert _is_sso_href("https://jobs.lever.co/acme/123/apply") is False
    assert _is_sso_href("https://www.dice.com/dashboard/login") is False


@pytest.mark.asyncio
async def test_new_framework_executor_refuses_google_click():
    # The upfront guard fires before the locator is touched, so ctx can be None.
    ex = ActionExecutor()
    res = await ex._click_selector(ctx=None, selector=None, text="Continue with Google")
    assert res.ok is False
    assert "sso" in (res.note or "").lower()
