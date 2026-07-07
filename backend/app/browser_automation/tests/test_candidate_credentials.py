"""Unit tests for the per-candidate credential login path.

Covers the resolution contract without a DB or browser:
  * candidate-provided credentials win over env vars,
  * env vars are the fallback when no candidate credential is present,
  * empty string when neither source has a value,
  * load_candidate_credentials degrades safely (no DB) to empty strings.

Run: pytest backend/app/browser_automation/tests/test_candidate_credentials.py -q
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.adapters.base import BasePlatformAdapter  # noqa: E402
from backend.app.browser_automation.adapters.session_utils import (  # noqa: E402
    load_candidate_credentials,
)


class _DummyAdapter(BasePlatformAdapter):
    """Minimal concrete adapter so we can exercise the shared credential API."""

    async def navigate_to_application(self, page, job_url):  # pragma: no cover
        return None

    async def detect_application_type(self, page):  # pragma: no cover
        return "EXTERNAL_FORM"

    async def fill_application(self, page, profile, resume_path, cover_letter_path,
                               screening_answers, pre_detected_form=None, candidate_id=None):  # pragma: no cover
        return True

    async def submit(self, page):  # pragma: no cover
        return True

    async def verify_success(self, page):  # pragma: no cover
        return True, None


def test_candidate_credentials_take_precedence_over_env(monkeypatch):
    monkeypatch.setenv("DICE_EMAIL", "env@dice.com")
    monkeypatch.setenv("DICE_PASSWORD", "env-pw")
    a = _DummyAdapter()
    a.set_candidate_credentials({"login_email": "candidate@gmail.com", "password": "cand-pw", "gmail": "candidate@gmail.com"})
    assert a._login_credential("login_email", "DICE_EMAIL") == "candidate@gmail.com"
    assert a._login_credential("password", "DICE_PASSWORD") == "cand-pw"


def test_env_is_fallback_when_no_candidate_credential(monkeypatch):
    monkeypatch.setenv("WORKDAY_USERNAME", "env-user")
    monkeypatch.setenv("WORKDAY_PASSWORD", "env-pw")
    a = _DummyAdapter()
    # No candidate creds injected at all.
    assert a._login_credential("login_email", "WORKDAY_USERNAME") == "env-user"
    assert a._login_credential("password", "WORKDAY_PASSWORD") == "env-pw"
    # Injected-but-empty must not shadow the env fallback.
    a.set_candidate_credentials({"login_email": "", "password": ""})
    assert a._login_credential("login_email", "WORKDAY_USERNAME") == "env-user"


def test_empty_when_neither_source(monkeypatch):
    monkeypatch.delenv("GLASSDOOR_EMAIL", raising=False)
    monkeypatch.delenv("GLASSDOOR_PASSWORD", raising=False)
    a = _DummyAdapter()
    assert a._login_credential("login_email", "GLASSDOOR_EMAIL") == ""
    assert a._login_credential("password", "GLASSDOOR_PASSWORD") == ""


def test_multiple_env_fallback_keys_first_nonempty_wins(monkeypatch):
    monkeypatch.delenv("A_KEY", raising=False)
    monkeypatch.setenv("B_KEY", "from-b")
    a = _DummyAdapter()
    assert a._login_credential("password", "A_KEY", "B_KEY") == "from-b"


def test_load_candidate_credentials_none_is_safe_empty():
    # No candidate id -> stable empty shape, never raises, no DB touched.
    out = asyncio.run(load_candidate_credentials(None))
    assert out == {"login_email": "", "password": "", "gmail": ""}


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
