import os
import json
import redis.asyncio as redis
from playwright.async_api import async_playwright, BrowserContext, Playwright
from dotenv import load_dotenv
from .stealth_config import get_stealth_config, StealthConfig

load_dotenv()

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
            self._browser = await self._playwright.chromium.launch(headless=True)

        context = await self._browser.new_context(
            viewport=config.viewport,
            user_agent=config.user_agent,
            timezone_id=config.timezone,
            locale=config.locale
        )

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
