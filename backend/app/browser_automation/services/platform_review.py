import json
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

REVIEWS_FILE = Path(__file__).resolve().parents[1] / "learned_fixes" / "platform_reviews.json"

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

def record_platform_failure(platform: str, error_msg: str):
    platform = platform.lower().strip()
    reviews = get_platform_reviews()
    
    if platform not in reviews:
        reviews[platform] = {
            "needs_review": False,
            "failures": []
        }
        
    reviews[platform]["failures"].append({
        "timestamp": datetime.utcnow().isoformat(),
        "error": error_msg
    })
    
    # If it fails more than once (len(failures) > 1), flag it
    if len(reviews[platform]["failures"]) > 1:
        reviews[platform]["needs_review"] = True
        logger.error(
            f"[PLATFORM REVIEW REQUIRED] Platform '{platform}' has failed {len(reviews[platform]['failures'])} times. "
            f"Failure pattern: {error_msg}"
        )
    else:
        logger.warning(f"Recorded failure for platform '{platform}': {error_msg}")
        
    save_platform_reviews(reviews)

def is_platform_flagged(platform: str) -> bool:
    platform = platform.lower().strip()
    reviews = get_platform_reviews()
    return reviews.get(platform, {}).get("needs_review", False)
