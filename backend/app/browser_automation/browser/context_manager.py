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

        # ── rektCaptcha extension support ─────────────────────────────────────
        # When REKTCAPTCHA_EXT_PATH points at an unpacked extension directory
        # (or several, comma-separated), we launch a PERSISTENT context with the
        # extension loaded. Persistent context is required because Chrome
        # extensions cannot be loaded into the default (incognito) context.
        ext_path = os.getenv("REKTCAPTCHA_EXT_PATH", "").strip()
        use_extension = bool(ext_path) and any(
            os.path.isdir(p.strip()) for p in ext_path.split(",") if p.strip()
        )

        if use_extension:
            headless = False  # extensions require a head; rektCaptcha needs one too
            ext_args = [
                f"--disable-extensions-except={ext_path}",
                f"--load-extension={ext_path}",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
            ]
            user_data_dir = os.getenv(
                "PLAYWRIGHT_USER_DATA_DIR",
                os.path.join(os.getcwd(), ".pw-userdata", candidate_id),
            )
            os.makedirs(user_data_dir, exist_ok=True)
            launch_context_kwargs = dict(
                user_data_dir=user_data_dir,
                headless=headless,
                args=ext_args,
                viewport=config.viewport,
                user_agent=config.user_agent,
                timezone_id=config.timezone,
                locale=config.locale,
            )
            try:
                persistent_ctx = await self._playwright.chromium.launch_persistent_context(
                    channel="chrome", **launch_context_kwargs
                )
                logger.info(f"[Browser] Persistent context w/ rektCaptcha @ {ext_path}")
            except Exception as exc:
                logger.warning(f"[Browser] Chrome channel unavailable for persistent ctx ({exc}); falling back to chromium")
                persistent_ctx = await self._playwright.chromium.launch_persistent_context(
                    **launch_context_kwargs
                )
            await persistent_ctx.add_init_script(STEALTH_JS)
            if session_data:
                try:
                    cookies = json.loads(session_data)
                    await persistent_ctx.add_cookies(cookies)
                except Exception as exc:
                    logger.warning(f"[Browser] Could not restore cookies into persistent ctx: {exc}")
            # Stash so destroy_context() can close it cleanly
            self._persistent_ctx = persistent_ctx
            return persistent_ctx

        if self._browser is None:
            # Use the real installed Chrome (channel="chrome") to get an authentic TLS fingerprint
            # that bypasses CloudFront/Akamai WAF bot detection which blocks bundled Chromium.
            # headless=False avoids the HeadlessChrome user-agent token and related signals.
            headless = os.getenv("PLAYWRIGHT_HEADLESS", "false").lower() == "true"
            # PLAYWRIGHT_SLOW_MO=250 inserts a 250ms pause between every Playwright
            # action (click, fill, etc.) so a human can actually watch the run.
            # Default 0 = full speed. Set when demoing or debugging visually.
            slow_mo_ms = int(os.getenv("PLAYWRIGHT_SLOW_MO", "0") or "0")
            logger.info(
                f"[Browser] Launching Chrome — headless={headless} "
                f"slow_mo={slow_mo_ms}ms "
                f"(set PLAYWRIGHT_HEADLESS=true to hide, "
                f"PLAYWRIGHT_SLOW_MO=300 to slow down for watching)"
            )
            launch_kwargs = dict(
                headless=headless,
                slow_mo=slow_mo_ms,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions-except=",
                    "--start-maximized",
                    # Force the window to the top-left of your primary monitor
                    # so it doesn't end up off-screen on multi-monitor setups
                    "--window-position=0,0",
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

        # CRITICAL: When using real Chrome (channel="chrome"), DO NOT override
        # the user_agent or sec-ch-ua headers. The browser sends a consistent
        # fingerprint matching its actual version + OS. Overriding the UA to
        # claim "macOS Chrome/133" on a Windows host (or any version mismatch
        # with the real browser's native sec-ch-ua / userAgentData) creates a
        # multi-axis inconsistency that Greenhouse's react-select detects as a
        # bot and silently disables custom widget interactions.
        #
        # Per-candidate fingerprint determinism is preserved via viewport,
        # timezone, locale, and the canvas/webdriver patches in STEALTH_JS —
        # which are safe because they don't expose contradictory headers.
        # BARE MINIMUM context: only viewport (visible) — no header overrides,
        # no UA override, no timezone (browser's default is fine), no
        # add_init_script. Anything more turned out to make Greenhouse's
        # react-select refuse to open. Once dropdowns are working, we can
        # selectively re-add stealth signals that don't trip detection.
        context_kwargs = dict(
            viewport=config.viewport,
            locale=config.locale,
            timezone_id=config.timezone,
        )
        logger.info(f"[Browser] Randomize context — viewport={config.viewport}, locale={config.locale}, timezone={config.timezone}")
        if proxy_config:
            context_kwargs["proxy"] = proxy_config

        # ── Persistent storage_state (cookies + localStorage) per platform ──
        # Auth-walled ATSes (Dice, LinkedIn, Workday) bot-throttle repeated
        # logins. Logging in ONCE and reusing the full storage_state avoids
        # that. Path: env "<PLATFORM>_STORAGE_STATE" or backend/data/sessions/
        # <platform>.json. When present we DON'T also restore redis cookies
        # (storage_state already carries them).
        storage_state_used = False
        try:
            from pathlib import Path as _Path
            backend_dir = _Path(__file__).resolve().parents[3]
            storage_state_path = os.getenv(
                f"{platform.upper()}_STORAGE_STATE",
                str(backend_dir / "data" / "sessions" / f"{platform}.json"),
            )
            if storage_state_path and os.path.isfile(storage_state_path):
                context_kwargs["storage_state"] = storage_state_path
                storage_state_used = True
                logger.info(f"[Browser] Loaded storage_state for {platform} ← {storage_state_path}")
        except Exception as exc:
            logger.debug(f"[Browser] storage_state load skipped: {exc}")

        context = await self._browser.new_context(**context_kwargs)

        # Inject stealth scripts
        await context.add_init_script(STEALTH_JS)

        if session_data and not storage_state_used:
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
