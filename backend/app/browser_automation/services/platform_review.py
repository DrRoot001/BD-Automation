import json
import logging
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
