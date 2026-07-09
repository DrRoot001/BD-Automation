"""Regression tests for the Phase-1 (stabilize) browser-automation hardening.

Covers the six fixes landed in this phase:
  1. Browser lifecycle teardown — BrowserContextManager.close()/destroy_context()
     are idempotent + exception-safe and also tear down the persistent context;
     ApplicationExecutor.execute() calls close() in finally.
  2. PROXY_URL parsing — inline user:pass@ creds split into username/password
     keys (Playwright ignores them in `server`), http/socks5 support, host-only
     logging, clean-skip on unusable input.
  3. Widened perception — DOM snapshot MAX_FIELDS raised to 250; reCAPTCHA and
     hCaptcha surfaced as first-class captcha entries (not just Turnstile).
  4. Basic stall recovery — AgentLoop._attempt_stall_recovery exists and is wired
     into the stuck handler before the hard STUCK abort.
  5. Submit safety — both heuristic auto-submit nets gate on
     _required_fields_complete (never fire on a partial form).
  6. Captcha clean-fail seam — CAPTCHA_UNSUPPORTED resolves to a clean terminal
     (loop flags it on the action; executor maps it to BLOCKED w/o traceback).

Plus a guardrail: the AgentLoop action space has no close/quit/destructive verb.

Pure-Python + tiny async fakes only (no real browser, no network).
Run: `pytest backend/app/browser_automation/tests/test_phase1_stabilize.py -q`
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.agent import loop as agent_loop  # noqa: E402
from backend.app.browser_automation.browser import context_manager as cm  # noqa: E402
from backend.app.browser_automation.services import executor as executor_mod  # noqa: E402


# ── Item 2: PROXY_URL parsing ────────────────────────────────────────────────

def test_parse_proxy_url_splits_credentials():
    # Playwright ignores inline creds in `server`; they MUST be split out.
    assert cm._parse_proxy_url("http://user:pass@1.2.3.4:8080") == {
        "server": "http://1.2.3.4:8080", "username": "user", "password": "pass",
    }


def test_parse_proxy_url_socks5():
    assert cm._parse_proxy_url("socks5://u:p@host.example:1080") == {
        "server": "socks5://host.example:1080", "username": "u", "password": "p",
    }


def test_parse_proxy_url_bare_host_defaults_http():
    assert cm._parse_proxy_url("1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}


def test_parse_proxy_url_no_credentials_omits_keys():
    got = cm._parse_proxy_url("http://1.2.3.4:8080")
    assert got == {"server": "http://1.2.3.4:8080"}
    assert "username" not in got and "password" not in got


def test_parse_proxy_url_percent_encoded_credentials():
    # A password containing '@'/':' must be %-decoded, not truncated.
    assert cm._parse_proxy_url("http://u%40x:p%3Aq@h:3128") == {
        "server": "http://h:3128", "username": "u@x", "password": "p:q",
    }


def test_parse_proxy_url_unusable_returns_none():
    assert cm._parse_proxy_url("") is None
    assert cm._parse_proxy_url("http://:8080") is None  # no host


# ── Item 5: required-fields completeness gate ────────────────────────────────

def test_required_fields_complete_ready():
    assert agent_loop._required_fields_complete(
        "FORM STATUS: 12/12 required filled (100%) — READY FOR SUBMIT\n#x | text | Name"
    ) is True
    # tolerant of spacing around the slash
    assert agent_loop._required_fields_complete(
        "FORM STATUS: 5 / 5 required filled (100%) — READY FOR SUBMIT"
    ) is True


def test_required_fields_complete_not_ready():
    assert agent_loop._required_fields_complete(
        "FORM STATUS: 3/12 required filled (25%) — NOT READY (9 required field(s) still empty)"
    ) is False


def test_required_fields_complete_conservative_when_unknown():
    # No FORM STATUS block (no required fields detected / snapshot unavailable)
    # → gate stays quiet so the auto-submit nets defer to the AI.
    assert agent_loop._required_fields_complete("(no interactive elements found)") is False
    assert agent_loop._required_fields_complete("") is False
    assert agent_loop._required_fields_complete("FORM STATUS: 0/0 required filled") is False


def test_both_auto_submit_nets_gate_on_required_complete():
    # Lock in that neither heuristic auto-submit fires on a partial form.
    src = inspect.getsource(agent_loop.AgentLoop.run)
    # Net #1 (auto-submit-on-READY) keys off the completeness gate, not raw text.
    assert 'if action.kind in ("scroll", "wait") and _required_fields_complete(dom)' in src
    # Net #2 (scroll-guard last-ditch) gates on the completeness helper, and the
    # old dangerous "any field was filled" trigger is gone.
    assert "form_ready = _required_fields_complete(dom)" in src
    assert "any_fills = any(a.kind == \"fill_field\"" not in src


# ── Item 3: widened perception + captcha detection ───────────────────────────

def test_dom_snapshot_max_fields_raised():
    assert "const MAX_FIELDS = 250;" in agent_loop._DOM_SNAPSHOT_JS


def test_dom_snapshot_detects_recaptcha_and_hcaptcha():
    js = agent_loop._DOM_SNAPSHOT_JS
    # reCAPTCHA
    assert "recaptcha_captcha" in js
    assert ".g-recaptcha" in js
    assert 'iframe[src*="recaptcha"]' in js
    assert 'captcha_type="recaptcha_v2"' in js
    # hCaptcha
    assert "hcaptcha_captcha" in js
    assert ".h-captcha" in js
    assert 'iframe[src*="hcaptcha"]' in js
    assert 'captcha_type="hcaptcha"' in js
    # Turnstile still present (not regressed)
    assert "turnstile_captcha" in js


# ── Item 4: stall recovery wiring ────────────────────────────────────────────

def test_stall_recovery_method_exists_and_is_wired():
    assert hasattr(agent_loop.AgentLoop, "_attempt_stall_recovery")
    src = inspect.getsource(agent_loop.AgentLoop.run)
    # Attempted before the hard abort, one-shot, and resets on success.
    assert "stall_recovery_done" in src
    assert "await self._attempt_stall_recovery(" in src
    rec_src = inspect.getsource(agent_loop.AgentLoop._attempt_stall_recovery)
    # Uses the existing vision tools + mechanical fallbacks.
    assert "classify_page" in rec_src and "suggest_selectors" in rec_src
    assert "reload" in rec_src and "go_back" in rec_src and "scrollBy" in rec_src


def test_stall_recovery_never_closes_browser():
    # Guardrail: recovery must NEVER tear anything down.
    rec_src = inspect.getsource(agent_loop.AgentLoop._attempt_stall_recovery)
    for forbidden in ("page.close(", "context.close(", "browser.close(", ".close()"):
        assert forbidden not in rec_src, f"recovery must not call {forbidden}"


# ── Item 1: browser lifecycle teardown ───────────────────────────────────────

class _FakeClosable:
    def __init__(self, name, raise_on_close=False):
        self.name = name
        self.closed = 0
        self.raise_on_close = raise_on_close

    async def close(self):
        self.closed += 1
        if self.raise_on_close:
            raise RuntimeError(f"{self.name} boom")


class _FakePlaywright:
    def __init__(self):
        self.stopped = 0

    async def stop(self):
        self.stopped += 1


@pytest.mark.asyncio
async def test_close_tears_down_all_handles_including_persistent_ctx():
    m = cm.BrowserContextManager()
    browser = _FakeClosable("browser")
    pw = _FakePlaywright()
    redis = _FakeClosable("redis")
    # persistent ctx raises on close — must NOT prevent the others closing.
    pctx = _FakeClosable("pctx", raise_on_close=True)
    m._browser, m._playwright, m._redis, m._persistent_ctx = browser, pw, redis, pctx

    await m.close()

    assert browser.closed == 1 and pw.stopped == 1 and redis.closed == 1
    assert pctx.closed == 1  # persistent context WAS closed (item 1 requirement)
    assert m._browser is None and m._playwright is None
    assert m._redis is None and m._persistent_ctx is None


@pytest.mark.asyncio
async def test_close_is_idempotent():
    m = cm.BrowserContextManager()
    browser = _FakeClosable("browser")
    m._browser = browser
    await m.close()
    await m.close()  # second call must be a no-op, never raise
    assert browser.closed == 1


@pytest.mark.asyncio
async def test_destroy_context_is_none_safe_and_exception_safe():
    m = cm.BrowserContextManager()
    await m.destroy_context(None)  # no raise
    boom = _FakeClosable("ctx", raise_on_close=True)
    await m.destroy_context(boom)  # swallows the exception
    assert boom.closed == 1


def test_executor_finally_closes_context_mgr():
    # The leak fix: execute() must close the manager (browser+driver+redis) in
    # finally, not merely destroy the context.
    src = inspect.getsource(executor_mod.ApplicationExecutor.execute)
    assert "await context_mgr.close()" in src
    # ...and inside the finally block (after the last destroy_context path).
    assert src.index("finally:") < src.index("await context_mgr.close()")


# ── Item 6: captcha clean-fail seam ──────────────────────────────────────────

class _FakeSolution:
    def __init__(self, success, error=None):
        self.success = success
        self.solve_time_seconds = 0.0
        if error is not None:
            self.error = error


def _make_fake_captcha_service(solution):
    class _FakeCaptchaService:
        def __init__(self, *a, **k):
            pass

        async def solve(self, page, captcha_type):
            return solution

    return _FakeCaptchaService


@pytest.mark.asyncio
async def test_solve_captcha_flags_unsupported_on_action(monkeypatch):
    import backend.app.browser_automation.captcha.service as svc
    monkeypatch.setattr(
        svc, "CaptchaService",
        _make_fake_captcha_service(
            _FakeSolution(False, "CAPTCHA_UNSUPPORTED: turnstile managed-mode on datacenter IP")
        ),
    )
    action = agent_loop.AgentAction(kind="solve_captcha", captcha_type="turnstile")
    ok = await agent_loop._execute_action(action, page=object(), frame=None,
                                          resume_path=None, cover_letter_path=None)
    assert ok is False
    assert action.raw.get("captcha_unsupported", "").startswith("CAPTCHA_UNSUPPORTED")


@pytest.mark.asyncio
async def test_solve_captcha_other_failure_does_not_flag(monkeypatch):
    # A transient/unknown solve failure (no CAPTCHA_UNSUPPORTED error) must NOT
    # set the terminal sentinel — the loop may legitimately retry/move on.
    import backend.app.browser_automation.captcha.service as svc
    monkeypatch.setattr(
        svc, "CaptchaService",
        _make_fake_captcha_service(_FakeSolution(False)),  # no .error attr at all
    )
    action = agent_loop.AgentAction(kind="solve_captcha", captcha_type="hcaptcha")
    ok = await agent_loop._execute_action(action, page=object(), frame=None,
                                          resume_path=None, cover_letter_path=None)
    assert ok is False
    assert "captcha_unsupported" not in action.raw


def test_loop_run_aborts_on_captcha_unsupported_sentinel():
    src = inspect.getsource(agent_loop.AgentLoop.run)
    assert 'action.raw.get("captcha_unsupported")' in src
    # Surfaced with a CAPTCHA_UNSUPPORTED error the executor maps to BLOCKED.
    assert "CAPTCHA_UNSUPPORTED" in src


def test_executor_maps_captcha_unsupported_to_clean_blocked():
    src = inspect.getsource(executor_mod.ApplicationExecutor.execute)
    # Helper exists and encodes a BLOCKED terminal with the right failure_reason.
    assert "_captcha_unsupported_terminal" in src
    assert 'status="BLOCKED"' in src
    assert '"failure_reason": "CAPTCHA_UNSUPPORTED"' in src
    # Reached from all three call sites (definition + page-agent + fallback + loop).
    assert src.count("_captcha_unsupported_terminal(") >= 4


# ── Guardrail: no destructive action verb in the AgentLoop action space ──────

def test_action_space_has_no_destructive_member():
    # Directive #1: the LLM literally cannot close/quit anything.
    kinds = set(agent_loop.ActionKind.__args__) | agent_loop._VALID_KINDS
    forbidden = {"close", "quit", "close_browser", "close_page", "close_tab",
                 "terminate", "kill", "destroy", "exit", "shutdown"}
    assert kinds.isdisjoint(forbidden), f"destructive action verb present: {kinds & forbidden}"


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
