import os
import json
import logging
import redis.asyncio as redis
from playwright.async_api import async_playwright, BrowserContext, Playwright
from dotenv import load_dotenv
from .stealth_config import get_stealth_config, StealthConfig

load_dotenv()
logger = logging.getLogger(__name__)

STEALTH_JS = """
(() => {
    // navigator.webdriver = false
    Object.defineProperty(navigator, 'webdriver', {
        get: () => false,
    });

    // Remove webdriver property from navigator prototype
    if (navigator.webdriver !== undefined) {
        delete (navigator.__proto__.webdriver);
    }

    // Patch chrome.runtime
    window.chrome = {
        runtime: {
            OnInstalledReason: {
                CHROME_UPDATE: 'chrome_update',
                INSTALL: 'install',
                SHARED_MODULE_UPDATE: 'shared_module_update',
                UPDATE: 'update',
            },
            OnRestartRequiredReason: {
                APP_UPDATE: 'app_update',
                OS_UPDATE: 'os_update',
                PERIODIC: 'periodic',
            },
            PlatformArch: {
                ARM: 'arm',
                ARM64: 'arm64',
                MIPS: 'mips',
                MIPS64: 'mips64',
                X86_32: 'x86-32',
                X86_64: 'x86-64',
            },
            PlatformNaclArch: {
                ARM: 'arm',
                MIPS: 'mips',
                MIPS64: 'mips64',
                X86_32: 'x86-32',
                X86_64: 'x86-64',
            },
            PlatformOs: {
                ANDROID: 'android',
                CROS: 'cros',
                LINUX: 'linux',
                MAC: 'mac',
                OPENBSD: 'openbsd',
                WIN: 'win',
            },
            RequestUpdateCheckStatus: {
                NO_UPDATE: 'no_update',
                THROTTLED: 'throttled',
                UPDATE_AVAILABLE: 'update_available',
            },
            connect: () => {},
            sendMessage: () => {},
        },
    };

    // Canvas noise
    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type, ...args) {
        const context = this.getContext('2d');
        if (context) {
            const imageData = context.getImageData(0, 0, 1, 1);
            imageData.data[0] = (imageData.data[0] + 1) % 256;
            context.putImageData(imageData, 0, 0);
        }
        return originalToDataURL.apply(this, [type, ...args]);
    };
})();
"""

class BrowserContextManager:
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis = None
        self._playwright = None
        self._browser = None

    async def _get_redis(self):
        if self._redis is None:
            kwargs = {"decode_responses": True}
            if "rediss://" in self.redis_url:
                kwargs["ssl_cert_reqs"] = "none"
            self._redis = redis.from_url(self.redis_url, **kwargs)
        return self._redis

    async def get_context(self, candidate_id: str, platform: str) -> BrowserContext:
        config: StealthConfig = get_stealth_config(candidate_id)
        
        redis_client = await self._get_redis()
        session_key = f"session:{candidate_id}:{platform}"
        session_data = await redis_client.get(session_key)
        
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        
        if self._browser is None:
            # Use the real installed Chrome (channel="chrome") to get an authentic TLS fingerprint
            # that bypasses CloudFront/Akamai WAF bot detection which blocks bundled Chromium.
            # headless=False avoids the HeadlessChrome user-agent token and related signals.
            headless = os.getenv("PLAYWRIGHT_HEADLESS", "false").lower() == "true"
            launch_kwargs = dict(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions-except=",
                    "--start-maximized",
                ],
            )
            # Prefer the real installed Chrome; fall back to bundled Chromium if unavailable
            try:
                self._browser = await self._playwright.chromium.launch(
                    channel="chrome", **launch_kwargs
                )
                logger.info("[Browser] Using real Chrome (channel=chrome)")
            except Exception as exc:
                logger.warning(f"[Browser] Real Chrome unavailable ({exc}); falling back to bundled Chromium")
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        # Proxy support — set PROXY_URL in .env for residential/rotating proxies.
        # Format: http://user:pass@host:port  or  socks5://user:pass@host:port
        # Required for production use against sites with CloudFront/Akamai WAF that
        # block IPs after repeated automated requests (monks.com, LinkedIn, etc.)
        proxy_url = os.getenv("PROXY_URL", "").strip()
        proxy_config = None
        if proxy_url:
            # Playwright proxy dict: server is required; username/password are optional
            # They can be encoded in the URL or split out separately
            proxy_config = {"server": proxy_url}
            logger.info(f"[Browser] Using proxy: {proxy_url.split('@')[-1]}")  # hide creds in log

        context_kwargs = dict(
            viewport=config.viewport,
            user_agent=config.user_agent,
            timezone_id=config.timezone,
            locale=config.locale,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        if proxy_config:
            context_kwargs["proxy"] = proxy_config

        context = await self._browser.new_context(**context_kwargs)

        # Inject stealth scripts
        await context.add_init_script(STEALTH_JS)

        if session_data:
            cookies = json.loads(session_data)
            await context.add_cookies(cookies)

        return context

    async def save_session(self, candidate_id: str, platform: str, context: BrowserContext) -> None:
        cookies = await context.cookies()
        session_key = f"session:{candidate_id}:{platform}"
        redis_client = await self._get_redis()
        await redis_client.set(session_key, json.dumps(cookies), ex=604800)

    async def destroy_context(self, context: BrowserContext) -> None:
        await context.close()

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        if self._redis:
            await self._redis.close()
