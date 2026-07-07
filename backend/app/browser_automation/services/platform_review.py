import json
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

REVIEWS_FILE = Path(__file__).resolve().parents[1] / "learned_fixes" / "platform_reviews.json"

# Failures that are JOB-specific or infrastructure-specific and must NOT count
# toward the platform failure threshold.
_SKIP_PATTERNS = [
    "JOB_EXPIRED",
    "ROBOTS_BLOCKED",
    "resume_url is empty",
    "Target page, context or browser has been closed",
    "Event loop is closed",
    "PLATFORM_NEEDS_REVIEW",
    # ── Transient infrastructure / connectivity (NOT a platform fault) ──
    "APPLICATION_TIMEOUT",
    "getaddrinfo failed",
    "Timeout connecting to server",
    "All connection attempts failed",
    "Connection error",
    "Temporary failure in name resolution",
    "credit balance is too low",     # LLM provider out of credit
    "LLM API key missing",
    "LLMUnavailable",
    "Page.goto",                      # navigation timeout / nav error
    "net::ERR_",                      # Chromium network errors
    "Timeout 20000ms",
    "Timeout 30000ms",
    # ── Job-level (NOT platform) failures ──
    # "Apply button not found" almost always means the job posting is closed or
    # its DOM was changed for THAT listing — not that the ATS is broken. Counting
    # it would lock out healthy hosts (especially wrappers like RemoteRocketship
    # where many inner listings expire) after only a handful of stale jobs.
    "Apply button not found",
    "Apply now button not found",
    "Workday Apply button not found",
    # Missing credentials for account-walled ATSes — operator config gap, not a
    # platform outage. Charging this to the platform would lock out Workday/
    # iCIMS/Dice the moment we hit 5 jobs without creds configured.
    "LOGIN_REQUIRED",
    "Workday requires an account",
    "iCIMS requires an account",
    "Dice requires an account",
    "DICE_EMAIL",
    # Candidate-level or self-inflicted outcomes — the ATS behaved correctly:
    # "already applied" duplicate rejections come from OUR retry of a job that
    # actually submitted, and a missing Gmail connection is a candidate-setup
    # gap. Neither says anything about the platform's health.
    "SPAM_FLAGGED",
    "already applied",
    "GMAIL_NOT_CONNECTED",
]

# How many platform-relevant failures within the rolling window trigger a flag.
_PLATFORM_FLAG_THRESHOLD = 5

# Rolling window for counting recent failures (hours).
_FAILURE_WINDOW_HOURS = 24


def _canonical_spam_key(platform: str) -> str:
    """Canonicalize a platform value (bare slug OR host) to the same known ATS
    slug on BOTH the read and write side of the SPAM backoff, so the key an
    armed cooldown is stored under always matches the key the pre-flight gate
    reads back.

    The failure/success paths in executor arm the backoff under the resolved
    INNER ATS name (e.g. 'greenhouse'/'lever') for wrapper aggregators like
    RemoteRocketship, while the pre-flight gate only knows the raw wrapper
    value (e.g. 'remoterocketship') or a host. Substring-matching the
    normalized value against the full known-slug universe (adapters.hints
    keys — same set the registry routes on) collapses both to one canonical
    slug. Falls back to the normalized value when nothing matches, mirroring
    the get_platform_hints() contract.
    """
    key = (platform or "").lower().strip()
    if not key:
        return key
    try:
        from ..adapters.hints import _HINTS
        known_slugs = _HINTS.keys()
    except Exception:
        return key
    if key in known_slugs:
        return key
    normalized = key.replace("-", "").replace("_", "").replace(".", "")
    # Most specific first so e.g. 'smartapply' wins before any looser match.
    for slug in known_slugs:
        if slug == "generic":
            continue
        if slug in key or slug in normalized:
            return slug
    return key


def _spam_backoff_base_s() -> int:
    """Base backoff (seconds) after the FIRST SPAM_FLAGGED anti-bot rejection.
    Overridable via SPAM_BACKOFF_BASE_S. Defaults to 300s (5 min)."""
    try:
        return max(1, int(os.getenv("SPAM_BACKOFF_BASE_S", "300")))
    except (TypeError, ValueError):
        return 300


def _spam_backoff_max_s() -> int:
    """Cap (seconds) on the exponential SPAM backoff. Overridable via
    SPAM_BACKOFF_MAX_S. Defaults to 14400s (4h)."""
    try:
        return max(1, int(os.getenv("SPAM_BACKOFF_MAX_S", "14400")))
    except (TypeError, ValueError):
        return 14400


def get_platform_reviews() -> dict:
    if not REVIEWS_FILE.exists():
        return {}
    try:
        with open(REVIEWS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read platform reviews file: {e}")
        return {}


def save_platform_reviews(reviews: dict):
    REVIEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(REVIEWS_FILE, "w", encoding="utf-8") as f:
            json.dump(reviews, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write platform reviews file: {e}")


def _recent_failures(failures: list) -> list:
    """Return only failures within the rolling window."""
    cutoff = datetime.utcnow() - timedelta(hours=_FAILURE_WINDOW_HOURS)
    result = []
    for f in failures:
        try:
            ts = datetime.fromisoformat(f["timestamp"])
            if ts > cutoff:
                result.append(f)
        except Exception:
            pass
    return result


def record_platform_failure(platform: str, error_msg: str):
    platform = platform.lower().strip()

    # Skip job-specific and infrastructure errors — these are NOT platform problems.
    if any(p in error_msg for p in _SKIP_PATTERNS):
        logger.debug(
            f"[PlatformReview] Skipping non-platform failure for '{platform}': {error_msg[:80]}"
        )
        return

    reviews = get_platform_reviews()

    if platform not in reviews:
        reviews[platform] = {"needs_review": False, "failures": []}

    reviews[platform]["failures"].append({
        "timestamp": datetime.utcnow().isoformat(),
        "error": error_msg,
    })

    recent = _recent_failures(reviews[platform]["failures"])

    if len(recent) >= _PLATFORM_FLAG_THRESHOLD:
        reviews[platform]["needs_review"] = True
        logger.error(
            f"[PlatformReview] Platform '{platform}' flagged after "
            f"{len(recent)} platform-level failures in the last {_FAILURE_WINDOW_HOURS}h. "
            f"Latest: {error_msg[:120]}"
        )
    else:
        logger.warning(
            f"[PlatformReview] Recorded platform-level failure #{len(recent)}/{_PLATFORM_FLAG_THRESHOLD} "
            f"for '{platform}': {error_msg[:80]}"
        )

    save_platform_reviews(reviews)


def is_platform_flagged(platform: str) -> bool:
    # Operator kill-switch. When set, the breaker NEVER gates execution — every
    # platform is allowed to run regardless of historical failure count. Failures
    # are still recorded for visibility, but the pre-flight check in
    # executor.execute() passes through. Use this when you'd rather let a
    # wrapper aggregator (RemoteRocketship, Remote100k) keep trying instead of
    # locking it out after a handful of inner-ATS hiccups.
    if os.getenv("DISABLE_PLATFORM_BREAKER", "").lower() in ("1", "true", "yes", "on"):
        return False

    platform = platform.lower().strip()
    reviews = get_platform_reviews()
    entry = reviews.get(platform, {})

    if not entry.get("needs_review", False):
        return False

    # Auto-clear: if fewer than threshold recent failures exist, the flag has
    # aged out — unflag and let the platform try again.
    recent = _recent_failures(entry.get("failures", []))
    if len(recent) < _PLATFORM_FLAG_THRESHOLD:
        entry["needs_review"] = False
        reviews[platform] = entry
        save_platform_reviews(reviews)
        logger.info(
            f"[PlatformReview] Platform '{platform}' auto-unflagged "
            f"(only {len(recent)} recent failures, threshold={_PLATFORM_FLAG_THRESHOLD})"
        )
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5.3 — per-platform exponential backoff after SPAM_FLAGGED.
#
# A SPAM_FLAGGED result is an ATS anti-bot rejection (not a generic fault), so
# it is deliberately NOT in _SKIP_PATTERNS and NOT folded into the 5-failure
# breaker. Instead, each SPAM_FLAGGED arms a growing cooldown so we stop
# hammering a host that just told us it thinks we're a bot. Backoff state lives
# in the SAME platform_reviews.json under a per-platform "spam_backoff" object:
#   {"count": N, "until": "<ISO-8601 utcnow + delay>"}
# delay = base * 2**(count-1), capped at the max. A successful application (or
# an expired cooldown that is then re-armed) resets count to 1.
# ─────────────────────────────────────────────────────────────────────────────

def record_spam_backoff(platform: str):
    """Arm/extend the exponential SPAM backoff for a platform after an anti-bot
    rejection. Increments count and pushes "until" = utcnow + delay."""
    platform = _canonical_spam_key(platform)
    if not platform:
        return

    reviews = get_platform_reviews()
    entry = reviews.get(platform)
    if not isinstance(entry, dict):
        entry = {"needs_review": False, "failures": []}

    backoff = entry.get("spam_backoff") or {}
    prev_count = 0
    try:
        prev_count = int(backoff.get("count", 0))
    except (TypeError, ValueError):
        prev_count = 0

    # If the previous cooldown already elapsed, treat this as a fresh strike so
    # a platform that recovered isn't punished with a stale exponent.
    until_raw = backoff.get("until")
    if prev_count > 0 and until_raw:
        try:
            if datetime.utcnow() >= datetime.fromisoformat(until_raw):
                prev_count = 0
        except Exception:
            prev_count = 0

    count = prev_count + 1
    base = _spam_backoff_base_s()
    cap = _spam_backoff_max_s()
    # Guard the shift so a runaway count can't overflow before the min() clamps.
    delay = base * (2 ** min(count - 1, 30))
    delay = min(delay, cap)

    until = datetime.utcnow() + timedelta(seconds=delay)
    entry["spam_backoff"] = {"count": count, "until": until.isoformat()}
    reviews[platform] = entry
    save_platform_reviews(reviews)

    logger.warning(
        f"[PlatformReview] SPAM_FLAGGED backoff #{count} for '{platform}': "
        f"holding for {delay}s (until {until.isoformat()}, base={base}s, cap={cap}s)"
    )


def get_spam_backoff_remaining(platform: str) -> int:
    """Return remaining SPAM backoff for a platform in whole seconds, or 0 if
    none is active (or it has elapsed)."""
    platform = _canonical_spam_key(platform)
    if not platform:
        return 0

    reviews = get_platform_reviews()
    entry = reviews.get(platform, {})
    backoff = entry.get("spam_backoff") if isinstance(entry, dict) else None
    if not backoff:
        return 0

    until_raw = backoff.get("until")
    if not until_raw:
        return 0
    try:
        until = datetime.fromisoformat(until_raw)
    except Exception:
        return 0

    remaining = (until - datetime.utcnow()).total_seconds()
    if remaining <= 0:
        return 0
    return int(remaining)


def reset_spam_backoff(platform: str):
    """Clear any SPAM backoff for a platform (e.g. after a successful apply)."""
    platform = _canonical_spam_key(platform)
    if not platform:
        return

    reviews = get_platform_reviews()
    entry = reviews.get(platform)
    if not isinstance(entry, dict) or "spam_backoff" not in entry:
        return

    entry.pop("spam_backoff", None)
    reviews[platform] = entry
    save_platform_reviews(reviews)
    logger.info(f"[PlatformReview] Cleared SPAM backoff for '{platform}'")
