import httpx
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse
import logging

logger = logging.getLogger(__name__)

async def is_action_allowed(url: str, user_agent: str = "BD-Automator-Agent") -> bool:
    """Check robots.txt rules for the target URL."""
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(robots_url)
            if resp.status_code == 404:
                return True # If no robots.txt, default to allowed
            
            rp = RobotFileParser()
            rp.parse(resp.text.splitlines())
            
        allowed = rp.can_fetch(user_agent, url)
        logger.info(f"[Robots.txt] Allowed to fetch {url}? -> {allowed}")
        return allowed
    except Exception as e:
        logger.warning(f"[Robots.txt] Failed check for {url}: {e}. Defaulting to ALLOW.")
        return True
