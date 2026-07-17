"""Lever captcha fixes (2026-07-18): turnstile misdetection + re-challenge wall.

Live Lever run (Mark → MCA Connect) showed the AgentLoop driving the form and
Anti-Captcha solving hCaptcha 4/4 — but two bugs kept it from finishing:

  1. `_settle_turnstile` ran the Turnstile solver EVERY turn on an hCaptcha form,
     because its widget check used a bare `[data-sitekey]` (hCaptcha and
     reCAPTCHA carry data-sitekey too). Fired 12x, each a wasted Anti-Captcha +
     Cloudflare call.
  2. The form re-issued a fresh hCaptcha after each solve, and nothing capped the
     re-solving — the loop spun to the 25-min wall timeout, then Celery retried.

These pin the source-level fixes (JS logic verified separately in real Chromium).
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

_LOOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "agent", "loop.py"))
_TASK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "tasks", "browser_automation.py"))


def _src(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# ── Bug 1: turnstile widget detection must exclude hCaptcha/reCAPTCHA ─────────


def test_settle_turnstile_does_not_use_bare_data_sitekey():
    src = _src(_LOOP)
    block = src[src.index("async def _settle_turnstile") :]
    block = block[: block.index("async def", 10)]
    # The old over-broad selector must be gone from the widget check.
    assert "'.cf-turnstile,[data-sitekey],[id*=\"turnstile\"]'" not in block, (
        "bare [data-sitekey] matches hCaptcha/reCAPTCHA — reintroduces the "
        "turnstile-solver-on-every-hCaptcha-form misfire"
    )


def test_settle_turnstile_excludes_hcaptcha_recaptcha():
    src = _src(_LOOP)
    block = src[src.index("async def _settle_turnstile") :]
    block = block[: block.index("async def", 10)]
    assert "skIsTurnstile" in block
    # A data-sitekey element is only a turnstile if NOT inside hCaptcha/reCAPTCHA.
    assert ".h-captcha" in block and ".g-recaptcha" in block


# ── Bug 2: bounded captcha re-solve guard ────────────────────────────────────


def test_max_captcha_solves_constant_exists_and_is_small():
    src = _src(_LOOP)
    m = re.search(r"_MAX_CAPTCHA_SOLVES\s*=\s*int\(.*?\"(\d+)\"", src)
    assert m, "_MAX_CAPTCHA_SOLVES constant missing"
    assert 2 <= int(m.group(1)) <= 6, "cap should be small — each solve costs time/money"


def test_loop_counts_and_caps_captcha_solves():
    src = _src(_LOOP)
    assert "self._captcha_solves" in src
    assert 'if action.kind == "solve_captcha" and ok:' in src
    assert "self._captcha_solves >= _MAX_CAPTCHA_SOLVES" in src
    # It must abort (terminal), not just log.
    seg = src[src.index("self._captcha_solves >= _MAX_CAPTCHA_SOLVES") :]
    seg = seg[: seg.index("actions=actions,") + 20]
    assert 'status="ABORTED"' in seg
    assert "CAPTCHA_UNSUPPORTED" in seg  # reuses the terminal captcha marker


def test_captcha_solves_counter_initialised():
    assert "self._captcha_solves: int = 0" in _src(_LOOP)


# ── Bug 2: the wall must be terminal (no Celery retry) ───────────────────────


def test_task_treats_captcha_wall_as_no_retry():
    src = _src(_TASK)
    # A dedicated handler must return terminal BEFORE the generic retry path.
    assert 'if "CAPTCHA_UNSUPPORTED" in err_msg or "captcha re-challenge wall" in _lower:' in src
    seg = src[src.index('"captcha re-challenge wall" in _lower'):]
    seg = seg[: seg.index("return")]
    assert "retry_eligible=False" in seg
    assert "BOT_DETECTED" in seg
