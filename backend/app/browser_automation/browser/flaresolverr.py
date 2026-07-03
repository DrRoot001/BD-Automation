"""FlareSolverr client — Cloudflare-clearance helper.

FlareSolverr (https://github.com/FlareSolverr/FlareSolverr) runs its own patched
Chromium to solve Cloudflare "Just a moment" / "you have been blocked" (403)
interstitials, then returns the clearance cookies (cf_clearance, __cf_bm), the
exact User-Agent it used, and the final HTML.

Those cookies are bound to (egress IP, User-Agent). To reuse them in our own
Playwright context we MUST (a) share the same egress IP — true when FlareSolverr
runs on the same host / behind the same VPN as us — and (b) set the SAME
User-Agent on our context. `clear_cloudflare()` returns everything the caller
needs to do that.

Configured via FLARESOLVERR_URL (e.g. http://localhost:18191/v1). No-op-safe:
returns None when the service is unreachable or errors, so callers degrade.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


def flaresolverr_url() -> str:
    return os.getenv("FLARESOLVERR_URL", "").strip()


async def clear_cloudflare(url: str, max_timeout_ms: int = 60_000) -> Optional[Dict[str, Any]]:
    """Ask FlareSolverr to fetch `url`, solving any Cloudflare wall.

    Returns a dict {cookies: [...], user_agent: str, status: int, html: str}
    on success, or None if FlareSolverr is unconfigured / unreachable / failed
    or still landed on a block page.
    """
    base = flaresolverr_url()
    if not base:
        logger.debug("[FlareSolverr] FLARESOLVERR_URL not set — skipping")
        return None
    payload = {"cmd": "request.get", "url": url, "maxTimeout": max_timeout_ms}
    try:
        async with httpx.AsyncClient(timeout=(max_timeout_ms / 1000) + 15) as client:
            resp = await client.post(base, json=payload)
            data = resp.json()
    except Exception as exc:
        logger.warning(f"[FlareSolverr] request to {base} failed: {exc}")
        return None

    if (data.get("status") or "").lower() != "ok":
        logger.warning(f"[FlareSolverr] non-ok status: {data.get('status')} — {str(data.get('message'))[:120]}")
        return None

    sol = data.get("solution") or {}
    html = sol.get("response") or ""
    http_status = sol.get("status") or 0
    cookies: List[Dict[str, Any]] = sol.get("cookies") or []
    ua = sol.get("userAgent") or ""

    # Detect a residual block page (FlareSolverr returned but couldn't clear it).
    low = html.lower()
    if "you have been blocked" in low or "attention required" in low:
        logger.warning(f"[FlareSolverr] still blocked after solve (http={http_status}) for {url[:80]}")
        return None

    logger.info(
        f"[FlareSolverr] cleared {url[:80]} — http={http_status}, "
        f"{len(cookies)} cookie(s), ua={ua[:40]!r}"
    )
    return {"cookies": cookies, "user_agent": ua, "status": http_status, "html": html}


def to_playwright_cookies(fs_cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert FlareSolverr cookie dicts to Playwright add_cookies() shape.

    FlareSolverr cookies look like Selenium's:
      {name, value, domain, path, expiry, secure, httpOnly, sameSite}
    Playwright wants: {name, value, domain, path, expires, httpOnly, secure, sameSite}
    with sameSite ∈ {Strict, Lax, None}.
    """
    out: List[Dict[str, Any]] = []
    for c in fs_cookies or []:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        if not (name and domain):
            continue
        pc: Dict[str, Any] = {
            "name": name,
            "value": value or "",
            "domain": domain,
            "path": c.get("path") or "/",
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }
        exp = c.get("expiry") or c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            pc["expires"] = int(exp)
        ss = (c.get("sameSite") or "").capitalize()
        if ss in ("Strict", "Lax", "None"):
            pc["sameSite"] = ss
        out.append(pc)
    return out
