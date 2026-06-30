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
]

# How many platform-relevant failures within the rolling window trigger a flag.
_PLATFORM_FLAG_THRESHOLD = 5

# Rolling window for counting recent failures (hours).
_FAILURE_WINDOW_HOURS = 24


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
