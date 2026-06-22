"""Gmail-backed verification-code fetcher for ATS submit walls.

Flow:
  1. Look up the candidate's `google_refresh_token` in the DB.
  2. Use GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (env) + that refresh token
     to mint a short-lived access token (no user interaction needed).
  3. Poll the Gmail inbox for a recent unread message that smells like a
     verification code (from greenhouse.io / lever.co / ashbyhq.com / no-reply
     senders, or whose body contains "verification code"). Retries with a
     short backoff because the email usually arrives within 10–60s of submit.
  4. Regex out the first 4–8 digit code.
  5. Detect the code input shape on the page (a single field, or 6/8 boxes).
  6. Fill the boxes and return so the AI can click submit.

Failure modes are non-fatal: if anything goes wrong we return None / False
and the AgentLoop falls back to its existing behavior (which is "ABORT
email_verification_code_required"). This lets us ship the feature without
risking regressions on candidates who don't have Gmail connected.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Senders / subject patterns that strongly suggest an ATS verification email.
# Order matters — first hit wins when ranking candidate messages.
_SENDER_HINTS = (
    "greenhouse.io",
    "myworkday.com",
    "lever.co",
    "ashbyhq.com",
    "no-reply",
    "noreply",
    "verification",
    "verify",
    "security",
)
_SUBJECT_HINTS_RE = re.compile(
    r"\b(verif|confirm|code|security|one[-\s]?time)\b",
    re.I,
)
# Most ATSes use 6 digits; Greenhouse Vercel-style uses 8. Accept 4–8.
_CODE_RE = re.compile(r"\b(\d{4,8})\b")


# ──────────────────────────────────────────────────────────────────────────
# Page-side: detect & fill the code input
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class CodeInputShape:
    """Describes the code-entry widget on the page so we can fill it.

    `kind` is "single" for one <input> taking the whole code, or "split" for
    N adjacent boxes (one digit per box — common on Greenhouse).
    """
    kind: str  # "single" | "split"
    selectors: List[str]  # one entry for "single", N entries for "split"
    digits: int  # expected code length (best-effort)


async def detect_code_input(page, frame=None) -> Optional[CodeInputShape]:
    """Scan the page for a verification-code input shape.

    Returns None if no obvious code input is visible — the AgentLoop should
    then keep treating the page as a regular form.
    """
    ctx = frame or page
    try:
        shape = await ctx.evaluate(_DETECT_JS)
    except Exception as exc:
        logger.debug(f"[verify] detect_code_input eval failed: {exc}")
        return None
    if not shape:
        return None
    kind = shape.get("kind")
    sels = shape.get("selectors") or []
    digits = int(shape.get("digits") or 0)
    if kind not in ("single", "split") or not sels:
        return None
    return CodeInputShape(kind=kind, selectors=sels, digits=digits)


_DETECT_JS = r"""() => {
    // Strategy:
    //  1. Look for N>=4 adjacent <input maxlength=1> boxes whose ids share a
    //     common prefix (e.g. #security-input-0 ... #security-input-7) —
    //     classic split-code shape.
    //  2. Otherwise look for ONE visible input whose label / placeholder /
    //     name / id mentions "code" / "verif" / "OTP".
    function visible(el) {
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }
    function labelFor(el) {
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
        if (el.id) {
            const l = document.querySelector('label[for="' + el.id + '"]');
            if (l) return (l.textContent || '').trim();
        }
        return el.placeholder || el.name || el.id || '';
    }

    // --- Split shape ---
    const candidates = Array.from(document.querySelectorAll(
        'input[maxlength="1"], input[type="tel"][maxlength="1"], '
      + 'input[type="number"][maxlength="1"], '
      + 'input[inputmode="numeric"][maxlength="1"]'
    )).filter(visible);
    if (candidates.length >= 4 && candidates.length <= 12) {
        // Make sure they're broadly clustered — share a parent within 3 levels
        const first = candidates[0];
        const cluster = candidates.filter(el => {
            let p = el, q = first;
            for (let i = 0; i < 4; i++) {
                if (p === q.parentElement || q === p.parentElement) return true;
                if (p.parentElement === q.parentElement) return true;
                p = p.parentElement; q = q.parentElement;
                if (!p || !q) break;
            }
            return false;
        });
        if (cluster.length >= 4) {
            const sels = cluster.map(el => el.id ? '#' + CSS.escape(el.id)
                                : (el.name ? 'input[name="' + el.name + '"]' : ''));
            const allOk = sels.every(s => s);
            if (allOk) return { kind: 'split', selectors: sels, digits: cluster.length };
        }
    }

    // --- Single-input fallback ---
    const KEY = /(verif|confirm|otp|one[-\s]?time|security|access|code)/i;
    const inputs = Array.from(document.querySelectorAll(
        'input[type="text"], input[type="tel"], input[type="number"], '
      + 'input[inputmode="numeric"], input:not([type])'
    )).filter(visible);
    for (const el of inputs) {
        const lbl = labelFor(el);
        if (!KEY.test(lbl)) continue;
        const sel = el.id ? '#' + CSS.escape(el.id)
                  : (el.name ? 'input[name="' + el.name + '"]' : '');
        if (!sel) continue;
        let digits = parseInt(el.getAttribute('maxlength') || '0', 10);
        if (!digits || digits > 12) digits = 6;
        return { kind: 'single', selectors: [sel], digits };
    }
    return null;
}"""


async def fill_code(page, frame, shape: CodeInputShape, code: str) -> bool:
    """Fill the code into the detected input shape. Returns True on success.

    The runner uses the React-aware native-setter pattern (same as
    AgentLoop's text fill) so controlled inputs commit reliably.
    """
    if not shape or not code:
        return False
    digits = re.sub(r"\D", "", code)
    if not digits:
        return False
    ctx = frame or page

    if shape.kind == "split":
        # One digit per box. If we have more digits than boxes, take the
        # first N. If fewer, fall back to typing in the first box (some
        # widgets auto-advance on keystroke).
        boxes = shape.selectors
        if len(digits) < len(boxes):
            try:
                first = ctx.locator(boxes[0]).first
                await first.click(timeout=2000)
                await first.type(digits, delay=80)
                await asyncio.sleep(0.4)
                return True
            except Exception as exc:
                logger.debug(f"[verify] split fallback type failed: {exc}")
                return False
        for sel, d in zip(boxes, digits):
            try:
                loc = ctx.locator(sel).first
                # focus + key press preserves React's auto-advance behavior.
                await loc.click(timeout=2000)
                await loc.press(d, timeout=2000)
                await asyncio.sleep(0.05)
            except Exception as exc:
                logger.debug(f"[verify] split fill {sel} failed: {exc}")
                return False
        await asyncio.sleep(0.3)
        return True

    # Single input — use the native setter so React picks it up.
    sel = shape.selectors[0]
    try:
        loc = ctx.locator(sel).first
        await loc.click(timeout=2000)
        committed = await loc.evaluate(
            """(el, v) => {
                try {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    return el.value === v;
                } catch(e) { return false; }
            }""",
            digits,
        )
        if not committed:
            await loc.fill(digits, timeout=3000)
        await asyncio.sleep(0.3)
        return True
    except Exception as exc:
        logger.warning(f"[verify] single-input fill failed: {exc}")
        return False


# ──────────────────────────────────────────────────────────────────────────
# Gmail-side: fetch the latest verification email
# ──────────────────────────────────────────────────────────────────────────

def _make_creds(refresh_token: str):
    """Build a `google.oauth2.credentials.Credentials` from a stored refresh
    token + the app's client id/secret. Refreshes the access token in-process.
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID") or ""
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or ""
    if not (client_id and client_secret):
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set in env; "
            "cannot mint Gmail access token"
        )
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


def _is_likely_verification(payload_headers: dict, snippet: str) -> bool:
    blob = (
        (payload_headers.get("From") or "")
        + " " + (payload_headers.get("Subject") or "")
        + " " + (snippet or "")
    ).lower()
    if any(h in blob for h in _SENDER_HINTS):
        return True
    if _SUBJECT_HINTS_RE.search(blob):
        return True
    return False


def _extract_code_from_message(msg: dict) -> Optional[str]:
    """Pull the first 4–8 digit number out of a Gmail message payload."""
    # Prefer the subject — Greenhouse / Lever often put the code there.
    headers = {h["name"]: h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
    subj = headers.get("Subject") or ""
    snip = msg.get("snippet") or ""

    for hay in (subj, snip):
        m = _CODE_RE.search(hay)
        if m:
            return m.group(1)

    # Fall back: decode the body parts and scan.
    parts = msg.get("payload", {}).get("parts") or [msg.get("payload", {})]
    for part in parts:
        body = (part or {}).get("body") or {}
        data = body.get("data")
        if not data:
            continue
        try:
            decoded = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
        except Exception:
            continue
        m = _CODE_RE.search(decoded)
        if m:
            return m.group(1)
    return None


def _gmail_search_code_sync(
    refresh_token: str,
    after_epoch: int,
    max_results: int = 8,
) -> Optional[str]:
    """Blocking Gmail call — list recent inbox messages and return the first
    one whose content looks like an ATS verification code.

    Wrapped by `_fetch_code_async` so the AgentLoop can await it.
    """
    from googleapiclient.discovery import build
    creds = _make_creds(refresh_token)
    # Auto-refreshing happens lazily on first request.
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    # `after:` accepts a unix timestamp in seconds. We pad by 60s to catch
    # mail-server clock skew.
    after_q = max(0, after_epoch - 60)
    q = f"in:inbox newer_than:1h after:{after_q}"
    try:
        listing = service.users().messages().list(
            userId="me", q=q, maxResults=max_results,
        ).execute()
    except Exception as exc:
        logger.warning(f"[verify] gmail list failed: {exc}")
        return None
    ids = [m["id"] for m in (listing.get("messages") or [])]
    for mid in ids:
        try:
            msg = service.users().messages().get(
                userId="me", id=mid, format="full",
            ).execute()
        except Exception as exc:
            logger.debug(f"[verify] gmail get {mid} failed: {exc}")
            continue
        headers = {h["name"]: h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
        if not _is_likely_verification(headers, msg.get("snippet") or ""):
            continue
        code = _extract_code_from_message(msg)
        if code:
            logger.info(
                f"[verify] Gmail code {code!r} from sender={headers.get('From')!r} "
                f"subject={headers.get('Subject')!r}"
            )
            return code
    return None


async def _fetch_code_async(
    refresh_token: str,
    after_epoch: int,
    timeout_s: float = 90.0,
    poll_interval_s: float = 6.0,
) -> Optional[str]:
    """Poll Gmail until a code arrives or `timeout_s` elapses."""
    started = time.monotonic()
    attempts = 0
    while time.monotonic() - started < timeout_s:
        attempts += 1
        loop = asyncio.get_event_loop()
        code = await loop.run_in_executor(
            None, _gmail_search_code_sync, refresh_token, after_epoch
        )
        if code:
            return code
        await asyncio.sleep(poll_interval_s)
    logger.warning(
        f"[verify] Gmail polling timed out after {timeout_s:.0f}s "
        f"({attempts} attempt(s))"
    )
    return None


# ──────────────────────────────────────────────────────────────────────────
# Public entry point — call from the AgentLoop
# ──────────────────────────────────────────────────────────────────────────

async def fetch_verification_code(
    candidate_id: str,
    after_epoch: int,
    timeout_s: float = 90.0,
) -> Optional[str]:
    """Get the latest ATS verification code for this candidate.

    Returns None when:
      - candidate_id missing
      - candidate has no google_refresh_token (Gmail not connected)
      - GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not configured
      - No matching email arrives within timeout_s
    Caller MUST treat None as "fall back to existing email-verify abort".
    """
    if not candidate_id:
        return None
    refresh_token = await _load_refresh_token(candidate_id)
    if not refresh_token:
        logger.info(
            f"[verify] candidate {candidate_id[:8]} has no google_refresh_token; "
            "skipping Gmail fetch"
        )
        return None
    try:
        return await _fetch_code_async(refresh_token, after_epoch, timeout_s=timeout_s)
    except Exception as exc:
        logger.warning(f"[verify] fetch_verification_code failed: {exc}")
        return None


async def _load_refresh_token(candidate_id: str) -> Optional[str]:
    """Read `candidates.google_refresh_token` via the existing async session."""
    try:
        from sqlalchemy import select
        from app.database import AsyncSessionLocal
        from app.models.candidate import Candidate
    except Exception as exc:
        logger.warning(f"[verify] cannot import DB session: {exc}")
        return None
    try:
        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(select(Candidate).where(Candidate.id == candidate_id))
            ).scalar_one_or_none()
            if not row:
                return None
            return row.google_refresh_token or None
    except Exception as exc:
        logger.warning(f"[verify] refresh-token query failed: {exc}")
        return None
