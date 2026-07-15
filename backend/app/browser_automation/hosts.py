"""Single source of truth for bot-walled / login-walled host classification.

This knowledge was previously duplicated (and drifting out of sync) between:
  * ``services/executor.py`` — which hosts to SKIP the lightweight httpx
    liveness pre-flight for (a plain GET gets a 403 / challenge / login-redirect
    and false-kills a live job as "expired"); and
  * ``browser/context_manager.py`` — which hosts need the residential proxy
    (they block datacenter IPs at a WAF).

The two are related but not identical, so we keep the *distinction* explicit
while sharing one definition:

  BOT_WALLED_HOSTS   — behind a WAF/anti-bot (Cloudflare / PerimeterX / Akamai /
                       CloudFront) that BLOCKS DATACENTER IPS. These need BOTH
                       the pre-flight skip AND the residential proxy. Adding a
                       host here (e.g. simplyhired) is what makes the proxy —
                       and therefore Anti-Captcha's proxied Cloudflare solve —
                       actually engage for it.
  LOGIN_WALLED_HOSTS — gated by an account (login redirect). httpx also
                       false-negatives here, so they skip pre-flight too, but
                       they do NOT need a residential proxy (the gate is the
                       account, not the IP) — keeping them direct preserves
                       throughput.
  PREFLIGHT_SKIP_HOSTS — the union: everything that should skip the httpx
                       liveness pre-flight.

Matching is by case-insensitive substring so a token ("talent", "workday")
matches both the bare slug and the full host ("www.talent.com",
"acme.wd1.myworkdayjobs.com").
"""
from __future__ import annotations

# WAF / anti-bot hosts that block datacenter IPs → NEED the residential proxy.
BOT_WALLED_HOSTS: tuple[str, ...] = (
    "simplyhired",          # Cloudflare "verify you are human" Turnstile wall
    "remoterocketship",
    "remote100k",
    "remoteok",
    "adzuna",
    "hiring.cafe",
    "himalayas",
    "talent",               # CloudFront WAF on external-apply redirects
    "linkedin",
    "indeed",
    "glassdoor",            # PerimeterX
    "ziprecruiter",         # PerimeterX
    "builtin",              # Cloudflare
    "workday",              # bot-detects datacenter IPs (also matches myworkdayjobs)
    "myworkdayjobs",
    "monks",
    "teamtailor",           # silently bounces datacenter-IP submits (200 → /applications/new)
    "westerncomputer",      # TeamTailor white-label host (careers.westerncomputer.com)
)

# Account/login-walled hosts — skip pre-flight, but proxy is optional (gate is
# the account, not the IP), so leave them direct for speed.
LOGIN_WALLED_HOSTS: tuple[str, ...] = (
    "dice",
    "icims",
    "smartrecruiters",
)

# Everything that should skip the lightweight httpx liveness pre-flight.
PREFLIGHT_SKIP_HOSTS: tuple[str, ...] = BOT_WALLED_HOSTS + LOGIN_WALLED_HOSTS


def _matches(value: str, hosts: tuple[str, ...]) -> bool:
    v = (value or "").lower()
    return any(h in v for h in hosts)


def is_bot_walled(host_or_platform: str) -> bool:
    """True when a host/platform sits behind a WAF that blocks datacenter IPs
    (→ needs the residential proxy)."""
    return _matches(host_or_platform, BOT_WALLED_HOSTS)


def should_skip_preflight(host_or_platform: str) -> bool:
    """True when a plain httpx liveness GET would false-negative for this host."""
    return _matches(host_or_platform, PREFLIGHT_SKIP_HOSTS)
