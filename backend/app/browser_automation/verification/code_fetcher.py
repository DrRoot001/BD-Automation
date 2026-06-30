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

# Greenhouse / Vercel verification codes are ALPHANUMERIC and case-sensitive
# (real example: "3T3PKP3f"), NOT plain digits. An earlier digits-only regex
# (\d{4,8}) silently missed these and grabbed a year like "2026" from the
# email body instead. Extraction strategy, in priority order:
#   1. The token that immediately follows a "code … :" cue phrase.
#   2. A standalone alphanumeric token 5–10 chars that MIXES letters+digits
#      (strong signal it's a code, not a word or a year).
#   3. A standalone 6–8 digit run (covers ATSes that DO use numeric codes).
# Years (19xx / 20xx) are explicitly excluded.
_CODE_CUE_RE = re.compile(
    r"(?:security\s+code|verification\s+code|your\s+code|this\s+code|"
    r"code\s+(?:is|field[^:]*)|code)\s*[:\-]?\s*\*?\s*([A-Za-z0-9]{5,10})\b",
    re.I,
)
_CODE_MIXED_RE = re.compile(r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)([A-Za-z0-9]{5,10})\b")
_CODE_DIGITS_RE = re.compile(r"\b(\d{6,8})\b")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

# Common English words 5-10 chars that show up RIGHT AFTER cue phrases like
# "enter the code field below:" — the cue regex would otherwise capture them
# as the code, and the loop would type the word into the OTP boxes (the actual
# "code='field'" bug observed against Greenhouse on Reddit). All-letter tokens
# matching this set are rejected; the real code comes later in the email.
_ENGLISH_NOISE = frozenset({
    "field", "below", "above", "right", "input", "enter", "shown", "valid",
    "value", "click", "press", "place", "where", "which", "after", "before",
    "expires", "minutes", "second", "minute", "hours", "today", "email",
    "address", "please", "thanks", "thank", "regards", "subject", "verify",
    "verification", "security", "account", "applied", "applic", "appli",
    "reset", "reply", "submit", "submitted", "follow", "following",
})

# HTML/CSS stripping — many ATS emails are HTML-only and stuffed with styling
# noise that the naive code regexes mistake for the code:
#   * hex colors like #F0F0F3 / #676767 (the latter is ALSO 6 digits!),
#   * px/em sizes like 600px,
#   * MIXED tokens like 691F74 / bf041352 (CSS).
# We strip <style>/<head>, tags, hex colors, and size units to recover the
# VISIBLE text before extracting the code.
_HTML_STYLE_RE = re.compile(r"<(style|head|script)[^>]*>.*?</\1>", re.I | re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HEXCOLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_CSSUNIT_RE = re.compile(r"\b\d+(?:px|em|rem|pt|vh|vw|%)\b", re.I)
# "6-digit code" / "enter the 6 digit code" → the code length the email declares.
_NDIGIT_RE = re.compile(r"(\d)\s*-?\s*digit", re.I)


def _html_to_text(s: str) -> str:
    """Strip an HTML email to its visible text, removing styling noise (hex
    colors, css units, tags) that the code regexes would otherwise grab."""
    if not s:
        return ""
    import html as _htmllib
    s = _HTML_STYLE_RE.sub(" ", s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _htmllib.unescape(s)
    s = _HEXCOLOR_RE.sub(" ", s)
    s = _CSSUNIT_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _pick_numeric_code(text: str, n: Optional[int] = None) -> Optional[str]:
    """Pick a numeric OTP from cleaned visible text. When the email declares an
    exact length ("6-digit code") we extract exactly-N-digit runs; otherwise
    6–8 digit runs. The real OTP is UNIQUE, while leftover styling numbers
    (e.g. a repeated #676767 color that survived as bare digits) recur — so we
    prefer the least-frequent candidate, breaking ties by first appearance.
    Years are excluded."""
    if not text:
        return None
    if n and 4 <= n <= 10:
        runs = re.findall(rf"(?<!\d)(\d{{{n}}})(?!\d)", text)
    else:
        runs = re.findall(r"(?<!\d)(\d{6,8})(?!\d)", text)
    runs = [r for r in runs if not _YEAR_RE.match(r)]
    if not runs:
        return None
    from collections import Counter
    freq = Counter(runs)
    uniq = list(dict.fromkeys(runs))
    uniq.sort(key=lambda r: (freq[r], text.index(r)))
    return uniq[0]


def _pick_code(text: str) -> Optional[str]:
    """Extract the most likely verification code from a blob of text.

    Case is PRESERVED — the Greenhouse code field is case-sensitive.

    Priority:
      1. A MIXED alphanumeric token (has BOTH a letter and a digit), 5–10
         chars — this is the strongest possible code signal (real example
         "3T3PKP3f"). English words ("field", "application", "security")
         are all-alpha and never match; years are all-digit and never match.
      2. Cue-anchored token that follows a "code …:" phrase (covers
         all-letter or all-digit codes that lack the mixed signal).
      3. A standalone 6–8 digit run, excluding years (numeric-code ATSes).
    """
    if not text:
        return None
    # 1. Mixed alphanumeric token — return the FIRST one that isn't a year.
    for m in _CODE_MIXED_RE.finditer(text):
        tok = m.group(1)
        if not _YEAR_RE.match(tok):
            return tok
    # 2. Cue-anchored. Require a colon/space-delimited token after the cue;
    #    prefer the LAST hit (the code usually follows the final phrase).
    #    REJECT all-letter tokens that look like English words — these are
    #    almost always noise from phrases like "enter the code field below"
    #    (the literal bug observed against a Greenhouse/Reddit run where the
    #    word "field" was typed into the OTP boxes).
    def _looks_like_code(tok: str) -> bool:
        if _YEAR_RE.match(tok):
            return False
        # All-letter token AND in the noise blocklist → reject. Real all-letter
        # codes are rare; the few we've seen are like "XZQPLMN" (random caps)
        # which won't be in the blocklist.
        if tok.isalpha() and tok.lower() in _ENGLISH_NOISE:
            return False
        return True
    cue_hits = [m.group(1) for m in _CODE_CUE_RE.finditer(text)
                if _looks_like_code(m.group(1))]
    if cue_hits:
        return cue_hits[-1]
    # 3. Pure-digit fallback (6–8 digits, excluding years).
    for m in _CODE_DIGITS_RE.finditer(text):
        if not _YEAR_RE.match(m.group(1)):
            return m.group(1)
    return None


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
    // Robustly detect the post-submit verification-code screen. Greenhouse
    // renders N separate single-char boxes with ids like security-input-0..7
    // (NOT always maxlength=1), so detection must not rely on maxlength alone.
    function visible(el) {
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }
    function labelFor(el) {
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
        if (el.id) {
            const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (l) return (l.textContent || '').trim();
        }
        return el.placeholder || el.name || el.id || '';
    }
    function cssStr(v){ return (v||'').replace(/\\/g,'\\\\').replace(/"/g,'\\"'); }
    let _otpStamp = 0;
    function selFor(el){
        if (el.id) return '#' + CSS.escape(el.id);
        if (el.name) return 'input[name="' + cssStr(el.name) + '"]';
        // Many modern OTP widgets render id-less, name-less <input maxlength=1>
        // boxes. Their aria-label/placeholder is often NON-UNIQUE across the
        // cluster (Talent.com labels all 6 boxes "Phone number"), so using it
        // would make every selector resolve to the FIRST box — the whole code
        // then lands in box 1 (last char wins). Always stamp a UNIQUE marker
        // attribute so each box gets its own selector. The stamp persists on
        // the DOM node and is consumed by fill_code() moments later.
        const mark = 'otpx-' + (_otpStamp++);
        el.setAttribute('data-otpx', mark);
        return 'input[data-otpx="' + mark + '"]';
    }
    const CODE_KEY = /(verif|confirm|otp|one[-\s]?time|security|access[-\s]?code|\bcode\b|pin)/i;

    // ── 1. Greenhouse-style id-prefix cluster (security-input-0,1,2,…) ──
    // Group all visible text/tel/number inputs by the non-digit prefix of
    // their id/name. Any group of 4–10 sharing a prefix that looks code-ish
    // is a split code input — regardless of maxlength.
    const allInputs = Array.from(document.querySelectorAll(
        'input[type="text"],input[type="tel"],input[type="number"],'
      + 'input[inputmode="numeric"],input:not([type])'
    )).filter(visible);
    const groups = {};
    for (const el of allInputs) {
        const key = (el.id || el.name || '');
        const prefix = key.replace(/[-_]?\d+$/, '');   // strip trailing index
        if (!prefix) continue;
        (groups[prefix] = groups[prefix] || []).push(el);
    }
    for (const [prefix, els] of Object.entries(groups)) {
        if (els.length < 4 || els.length > 10) continue;
        const looksCode = CODE_KEY.test(prefix) || els.every(e => {
            const ml = parseInt(e.getAttribute('maxlength')||'0',10); return ml===1;
        });
        if (!looksCode) continue;
        const sels = els.map(selFor);
        if (sels.every(s => s)) return { kind:'split', selectors: sels, digits: els.length };
    }

    // ── 2. maxlength=1 cluster fallback (any ids) ──
    const ones = allInputs.filter(e => parseInt(e.getAttribute('maxlength')||'0',10) === 1);
    if (ones.length >= 4 && ones.length <= 10) {
        const sels = ones.map(selFor);
        if (sels.every(s => s)) return { kind:'split', selectors: sels, digits: ones.length };
    }

    // ── 3. Single code input — match by label OR id/name/placeholder ──
    for (const el of allInputs) {
        const hay = (labelFor(el) + ' ' + (el.id||'') + ' ' + (el.name||'') + ' ' + (el.placeholder||'')).toLowerCase();
        if (!CODE_KEY.test(hay)) continue;
        const sel = selFor(el);
        if (!sel) continue;
        let digits = parseInt(el.getAttribute('maxlength') || '0', 10);
        if (!digits || digits > 12) digits = 8;
        return { kind:'single', selectors:[sel], digits };
    }

    // ── 4. Page-text cue + ANY lone visible text input ──
    // If the page clearly says a code was sent but the inputs are unusual,
    // fall back to the first visible text input on the screen.
    const bodyTxt = (document.body.innerText || '').toLowerCase();
    const CUE = /(security code|verification code|enter the code|we (sent|emailed)|check your email|code (we|that) (sent|emailed)|sent (you )?a code)/;
    if (CUE.test(bodyTxt)) {
        // Prefer a small cluster if present, else the first lone text input.
        const lone = allInputs.find(e => visible(e));
        if (lone) {
            const sel = selFor(lone);
            if (sel) {
                const ml = parseInt(lone.getAttribute('maxlength')||'0',10);
                return { kind:'single', selectors:[sel], digits: (ml&&ml<=12)?ml:8 };
            }
        }
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
    # IMPORTANT: codes are ALPHANUMERIC and case-sensitive (e.g. "3T3PKP3f").
    # Do NOT strip letters — only strip surrounding whitespace.
    chars = code.strip()
    if not chars:
        return False
    ctx = frame or page

    if shape.kind == "split":
        # One character per box (alphanumeric, NOT digits-only). If we have
        # more chars than boxes, take the first N. If fewer, type into the
        # first box (some widgets auto-advance on keystroke).
        boxes = shape.selectors
        if len(chars) < len(boxes):
            try:
                first = ctx.locator(boxes[0]).first
                await first.click(timeout=2000)
                await first.type(chars, delay=80)
                await asyncio.sleep(0.4)
                return True
            except Exception as exc:
                logger.debug(f"[verify] split fallback type failed: {exc}")
                return False
        for sel, ch in zip(boxes, chars):
            try:
                loc = ctx.locator(sel).first
                await loc.click(timeout=2000)
                # Use type (not press) so uppercase letters / mixed case
                # commit correctly — press('T') is a key event, type('T')
                # inserts the literal character the field expects.
                await loc.type(ch, delay=40, timeout=2000)
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
            chars,
        )
        if not committed:
            await loc.fill(chars, timeout=3000)
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


def _collect_bodies(payload: dict) -> List[str]:
    """Recursively decode every text/html and text/plain body part."""
    out: List[str] = []

    def walk(p: dict) -> None:
        if not p:
            return
        body = p.get("body") or {}
        data = body.get("data")
        if data:
            try:
                out.append(base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace"))
            except Exception:
                pass
        for sub in (p.get("parts") or []):
            walk(sub)

    walk(payload or {})
    return out


def _extract_code_from_message(msg: dict) -> Optional[str]:
    """Pull the verification code out of a Gmail message.

    HTML emails are stripped to visible text first (removing hex colors, css
    units, tags) so styling noise like #F0F0F3 / #676767 / 600px is never
    mistaken for the code. If the email DECLARES a digit length ("6-digit
    code") we extract the unique N-digit run; otherwise we fall back to the
    general cue/mixed/digit extractor.
    """
    headers = {h["name"]: h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
    subj = headers.get("Subject") or ""
    snip = msg.get("snippet") or ""

    raw_bodies = _collect_bodies(msg.get("payload", {}))
    # Clean HTML bodies to visible text; keep plain-ish bodies as-is.
    cleaned_parts = [
        _html_to_text(b) if ("<" in b and ">" in b) else b
        for b in raw_bodies
    ]
    # Combine snippet (carries the "N-digit" cue) with the visible body text.
    combined = " ".join([snip] + cleaned_parts).strip()

    # If the email declares an exact code length, prefer that numeric run.
    nm = _NDIGIT_RE.search(combined)
    if nm:
        try:
            n = int(nm.group(1))
        except Exception:
            n = None
        code = _pick_numeric_code(combined, n)
        if code:
            return code

    # General extractor over the CLEANED combined text (cue → mixed → digits).
    code = _pick_code(combined)
    if code:
        return code

    # Subject last — least likely to contain the code.
    return _pick_code(subj)


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
    q = f"in:inbox is:unread newer_than:1h after:{after_q}"
    try:
        listing = service.users().messages().list(
            userId="me", q=q, maxResults=max_results,
        ).execute()
    except Exception as exc:
        err_msg = str(exc).lower()
        if "invalid_grant" in err_msg or "unauthorized" in err_msg or "invalid client" in err_msg:
            logger.error(f"[verify] Gmail token invalid or expired: {exc}")
            raise RuntimeError(f"GMAIL_AUTH_FAILED: {exc}")
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
    try:
        refresh_token = await _load_refresh_token(candidate_id)
    except RuntimeError as exc:
        # _load_refresh_token raises on transient DB failures (closed event
        # loop, pool exhaustion) — distinct from "candidate has no token".
        # Treat as "code not yet available, try next poll" so the agent loop's
        # wait-budget keeps ticking instead of bailing out as
        # GMAIL_NOT_CONNECTED. Real auth failures surface as GMAIL_AUTH_FAILED
        # downstream.
        logger.warning(f"[verify] transient token-load failure ({exc}) — will retry next poll")
        return None
    if not refresh_token:
        logger.info(
            f"[verify] candidate {candidate_id[:8]} has no google_refresh_token; "
            "skipping Gmail fetch"
        )
        return None
    try:
        return await _fetch_code_async(refresh_token, after_epoch, timeout_s=timeout_s)
    except Exception as exc:
        if "GMAIL_AUTH_FAILED" in str(exc):
            raise
        logger.warning(f"[verify] fetch_verification_code failed: {exc}")
        return None


async def _load_refresh_token(candidate_id: str) -> Optional[str]:
    """Read `candidates.google_refresh_token` via a NullPool session safe for Celery workers."""
    try:
        from sqlalchemy import select
        from app.database import task_session
        from app.models.candidate import Candidate
    except Exception as exc:
        logger.warning(f"[verify] cannot import DB session: {exc}")
        return None
    try:
        import uuid
        if isinstance(candidate_id, str):
            try:
                candidate_id = uuid.UUID(candidate_id)
            except ValueError:
                pass
        async with task_session() as s:
            row = (
                await s.execute(select(Candidate).where(Candidate.id == candidate_id))
            ).scalar_one_or_none()
            if not row:
                return None
            return row.google_refresh_token or None
    except Exception as exc:
        # IMPORTANT: a query failure (closed event loop, transient DB drop, pool
        # exhaustion) is NOT the same as "no token". Raise a distinct sentinel
        # so the caller can degrade-open instead of mistakenly aborting the
        # apply as GMAIL_NOT_CONNECTED. The pre-check in AgentLoop catches
        # this and assumes the token IS present.
        logger.warning(f"[verify] refresh-token query failed: {exc}")
        raise RuntimeError(f"REFRESH_TOKEN_QUERY_FAILED: {exc}")
