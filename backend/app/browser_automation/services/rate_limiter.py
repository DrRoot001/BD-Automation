import os
import redis.asyncio as aioredis

PLATFORM_LIMITS = {
    "linkedin":   {"hourly": 10,  "daily": 50},
    "indeed":     {"hourly": 20,  "daily": 100},
    "greenhouse": {"hourly": 30,  "daily": 150},
    "lever":      {"hourly": 30,  "daily": 150},
    "default":    {"hourly": 15,  "daily": 75},
}


class RateLimiter:
    """Checks and enforces per-platform, per-candidate hourly and daily rate limits."""

    def __init__(self):
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.redis = aioredis.from_url(redis_url, decode_responses=True)

    async def check_and_increment(self, platform: str, candidate_id: str) -> bool:
        """
        Returns True if the request is allowed and increments the counters.
        Returns False if either the hourly or daily limit has been reached.
        """
        limits = PLATFORM_LIMITS.get(platform.lower(), PLATFORM_LIMITS["default"])

        hourly_key = f"rate_limit:{platform}:{candidate_id}"
        daily_key  = f"rate_limit_daily:{platform}:{candidate_id}"

        # Read current counts (pipeline used for a single round-trip read)
        async with self.redis.pipeline() as pipe:
            hourly_count = await self.redis.get(hourly_key)
            daily_count  = await self.redis.get(daily_key)

        hourly_count = int(hourly_count or 0)
        daily_count  = int(daily_count  or 0)

        if hourly_count >= limits["hourly"]:
            return False
        if daily_count >= limits["daily"]:
            return False

        # Increment and (re-)set TTLs
        await self.redis.incr(hourly_key)
        await self.redis.expire(hourly_key, 3600)
        await self.redis.incr(daily_key)
        await self.redis.expire(daily_key, 86400)
        return True
