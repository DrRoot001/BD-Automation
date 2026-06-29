import os
import json
import asyncio
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

# Whether to inject the custom STEALTH_JS above. DEFAULT OFF.
#
# Rationale (resolves the long-standing contradiction in this file): the
# canvas-noise + chrome.runtime overrides in STEALTH_JS were found to make
# Greenhouse's react-select widgets refuse to open (they detect the tampering
# as a bot). Meanwhile the executor applies `playwright-stealth` v2 to every
# page, which already patches navigator.webdriver / chrome.runtime / WebGL /
# permissions in a way real sites tolerate. So by default we rely on
# playwright-stealth and DON'T inject this script. Set INJECT_CUSTOM_STEALTH_JS=true
# only for sites where you've verified it helps and doesn't break widgets.
_INJECT_CUSTOM_STEALTH_JS = os.getenv("INJECT_CUSTOM_STEALTH_JS", "false").lower() == "true"


class BrowserContextManager:
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis = None
        self._playwright = None
        self._browser = None
        # Persistent (extension) context, when rektCaptcha mode is active. Must be
        # tracked so close() can shut it down — in extension mode self._browser is
        # None and the persistent Chrome process would otherwise leak.
        self._persistent_ctx = None
        # Serializes lazy playwright/browser launch so two concurrent get_context()
        # calls don't each launch a browser and leak the first.
        self._init_lock = asyncio.Lock()

    async def _get_redis(self):
        if self._redis is None:
            kwargs = {
                "decode_responses": True,
                # Tolerate slow cold TLS handshakes to managed Redis (Upstash).
                "socket_connect_timeout": 20,
                "socket_timeout": 20,
                "retry_on_timeout": True,
            }
            if "rediss://" in self.redis_url:
                kwargs["ssl_cert_reqs"] = "none"
            self._redis = redis.from_url(self.redis_url, **kwargs)
        return self._redis

    async def _ensure_browser_launched(self) -> None:
        """Launch the shared browser exactly once, even under concurrent callers.

        Double-checked locking: two coroutines that both see ``self._browser is
        None`` will serialize on ``self._init_lock``; the second sees the browser
        already launched and returns. Without this, the second launch overwrote
        ``self._browser`` and leaked the first browser process.
        """
        async with self._init_lock:
            if self._browser is not None:
                return
            # Use the real installed Chrome (channel="chrome") to get an authentic TLS
            # fingerprint that bypasses CloudFront/Akamai WAF bot detection which blocks
            # bundled Chromium. headless=False avoids the HeadlessChrome UA token.
            headless = os.getenv("PLAYWRIGHT_HEADLESS", "false").lower() == "true"
            # PLAYWRIGHT_SLOW_MO=250 inserts a 250ms pause between every Playwright
            # action so a human can watch the run. Default 0 = full speed.
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
                    # Force the window to the top-left of the primary monitor.
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

    async def get_context(self, candidate_id: str, platform: str) -> BrowserContext:
        config: StealthConfig = get_stealth_config(candidate_id)

        # Session cookie restore is best-effort. A transient Redis outage / DNS
        # blip must NOT prevent launching the browser — proceed with a fresh
        # context (cookies just won't be pre-restored this run).
        session_key = f"session:{candidate_id}:{platform}"
        session_data = None
        try:
            redis_client = await self._get_redis()
            session_data = await redis_client.get(session_key)
        except Exception as exc:
            logger.warning(f"[Browser] Redis unavailable for session restore ({exc}) — fresh context")

        if self._playwright is None:
            async with self._init_lock:
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
            if _INJECT_CUSTOM_STEALTH_JS:
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
            await self._ensure_browser_launched()

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

        # Stealth: by default rely on playwright-stealth (applied per-page in the
        # executor). The custom STEALTH_JS is opt-in because its canvas-noise
        # tampering breaks Greenhouse react-select widgets. See note above.
        if _INJECT_CUSTOM_STEALTH_JS:
            await context.add_init_script(STEALTH_JS)

        if session_data and not storage_state_used:
            try:
                cookies = json.loads(session_data)
                await context.add_cookies(cookies)
            except Exception as exc:
                logger.warning(f"[Browser] Could not restore cookies from redis (corrupt JSON?): {exc}")

        return context

    async def save_session(self, candidate_id: str, platform: str, context: BrowserContext) -> None:
        # Best-effort: failing to persist cookies must not fail the application.
        try:
            cookies = await context.cookies()
            session_key = f"session:{candidate_id}:{platform}"
            redis_client = await self._get_redis()
            await redis_client.set(session_key, json.dumps(cookies), ex=604800)
        except Exception as exc:
            logger.warning(f"[Browser] Could not persist session cookies (non-fatal): {exc}")

    async def destroy_context(self, context: BrowserContext) -> None:
        await context.close()

    async def close(self):
        # Close the persistent (extension) context first — in rektCaptcha mode
        # self._browser is None and this is the only handle to the Chrome process,
        # so skipping it leaks the browser.
        if self._persistent_ctx:
            try:
                await self._persistent_ctx.close()
            except Exception as exc:
                logger.debug(f"[Browser] persistent_ctx close failed: {exc}")
            self._persistent_ctx = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        if self._redis:
            await self._redis.close()
            self._redis = None
