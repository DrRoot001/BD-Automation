import os
import json
import logging
from typing import Optional
from urllib.parse import urlparse, unquote
import redis.asyncio as redis
from playwright.async_api import async_playwright, BrowserContext, Playwright
from dotenv import load_dotenv
from .stealth_config import get_stealth_config, build_stealth_init_script, StealthConfig

load_dotenv()
logger = logging.getLogger(__name__)


def _parse_proxy_url(proxy_url: str) -> Optional[dict]:
    """Parse PROXY_URL into a Playwright proxy dict.

    Playwright IGNORES inline ``user:pass@`` credentials embedded in the
    ``server`` field — they MUST be split into separate ``username`` /
    ``password`` keys, otherwise proxy auth silently fails (the upstream
    returns 407 and every navigation dies). This normalizes any of:

        http://user:pass@host:port
        socks5://user:pass@host:port
        host:port                     (scheme defaults to http)

    into ``{"server": "scheme://host:port", "username": ..., "password": ...}``
    with the username/password keys OMITTED when absent. Returns None when the
    URL is unusable (no host) so the caller can cleanly skip the proxy.
    """
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    # A bare "host:port" (no scheme) makes urlparse treat "host" as the scheme;
    # prepend http:// so host/port/credentials parse correctly.
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
    except Exception as exc:
        logger.warning(f"[Browser] PROXY_URL parse failed ({exc}); ignoring proxy")
        return None
    scheme = (parsed.scheme or "http").lower()
    # Playwright accepts http/https/socks5 proxy schemes. Anything else falls
    # back to http rather than passing an invalid scheme straight through.
    if scheme not in ("http", "https", "socks5", "socks5h"):
        scheme = "http"
    host = parsed.hostname
    if not host:
        logger.warning("[Browser] PROXY_URL has no host; ignoring proxy")
        return None
    server = f"{scheme}://{host}"
    if parsed.port:
        server = f"{server}:{parsed.port}"
    cfg: dict = {"server": server}
    # unquote so %-encoded credentials (e.g. a password with '@' or ':')
    # are passed to the proxy verbatim.
    if parsed.username:
        cfg["username"] = unquote(parsed.username)
    if parsed.password:
        cfg["password"] = unquote(parsed.password)
    return cfg


def _session_ttl_s() -> int:
    """Redis session TTL in seconds. `SESSION_TTL_DAYS` env overrides the 7-day
    default. The TTL is (re)set on both restore and save so a session in active
    use never expires mid-batch, even across runs that fail before save."""
    try:
        days = int(os.getenv("SESSION_TTL_DAYS", "7"))
    except (TypeError, ValueError):
        days = 7
    return max(1, days) * 86400


class BrowserContextManager:
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis = None
        self._playwright = None
        self._browser = None
        # Set only on the rektCaptcha persistent-context path (see get_context);
        # declared here so close() can tear it down without a getattr guard.
        self._persistent_ctx = None

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
        if session_data:
            # Refresh the TTL the moment we restore: any active use resets the
            # 7-day clock, so a long-running batch (or a candidate whose runs
            # keep failing before save_session) doesn't silently lose its
            # authenticated session between attempts.
            try:
                await redis_client.expire(session_key, _session_ttl_s())
            except Exception as exc:
                logger.debug(f"[Browser] session TTL refresh skipped: {exc}")

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
            # NOTE: no user_agent override here either — the persistent path
            # also launches real Chrome (channel="chrome"), and overriding the
            # UA desyncs it from the browser's native sec-ch-ua/userAgentData
            # (the Greenhouse react-select lesson). Real Chrome's genuine UA
            # is always coherent; viewport/timezone/locale are safe to set.
            launch_context_kwargs = dict(
                user_data_dir=user_data_dir,
                headless=headless,
                args=ext_args,
                viewport=config.viewport,
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
            await persistent_ctx.add_init_script(build_stealth_init_script(config))
            if session_data:
                try:
                    # launch_persistent_context does NOT accept storage_state,
                    # so we can only restore cookies here. localStorage /
                    # sessionStorage persist automatically via user_data_dir,
                    # so nothing is lost for the persistent path.
                    parsed = json.loads(session_data)
                    if isinstance(parsed, dict):
                        # NEW format: a Playwright storage_state dict
                        cookies = parsed.get("cookies", [])
                    else:
                        # OLD format: a bare list of cookies
                        cookies = parsed
                    if cookies:
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
            # Playwright IGNORES inline user:pass@ creds in the server field —
            # they must be split into username/password keys. _parse_proxy_url
            # does that and returns None if the URL is unusable.
            proxy_config = _parse_proxy_url(proxy_url)
            if proxy_config:
                # Log host:port only — never the credentials.
                logger.info(f"[Browser] Using proxy: {proxy_config['server']}")
            else:
                logger.warning("[Browser] PROXY_URL set but could not be parsed; proceeding without a proxy")

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
        # color_scheme and device_scale_factor are safe Playwright context
        # options — they don't contradict any HTTP header or UA axis, and
        # device_scale_factor doesn't conflict with viewport.
        context_kwargs = dict(
            viewport=config.viewport,
            locale=config.locale,
            timezone_id=config.timezone,
            color_scheme=config.color_scheme,
            device_scale_factor=config.device_scale_factor,
        )
        logger.info(
            f"[Browser] Randomize context — viewport={config.viewport}, "
            f"locale={config.locale}, timezone={config.timezone}, "
            f"color_scheme={config.color_scheme}, dsf={config.device_scale_factor}"
        )
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

        # ── Redis session restore (dict = new storage_state, list = legacy) ──
        # The redis blob can be either the NEW full storage_state (a dict with
        # "cookies"/"origins" — carries localStorage too) or the OLD bare
        # cookie list. A dict must be passed via context_kwargs BEFORE
        # new_context(); a list is applied afterwards via add_cookies(). The
        # file-based storage_state (above) always wins — if it was used we
        # don't also apply the redis blob.
        redis_session_parsed = None
        redis_session_is_dict = False
        if session_data and not storage_state_used:
            try:
                redis_session_parsed = json.loads(session_data)
                if isinstance(redis_session_parsed, dict):
                    redis_session_is_dict = True
                    context_kwargs["storage_state"] = redis_session_parsed
                    logger.info(f"[Browser] Restoring redis storage_state for {platform} (full state)")
            except Exception as exc:
                logger.warning(f"[Browser] Could not parse redis session for {platform}: {exc}")
                redis_session_parsed = None

        try:
            context = await self._browser.new_context(**context_kwargs)
        except Exception as exc:
            # A corrupted/structurally-invalid redis storage_state dict can make
            # new_context() itself raise, failing context creation entirely.
            # Only for the redis-dict case: drop the bad storage_state and
            # recreate the context bare so the run can still proceed (a fresh
            # login just gets triggered downstream). File-based storage_state
            # precedence is untouched — this only fires when we injected the
            # redis dict above.
            if redis_session_is_dict and context_kwargs.get("storage_state") is redis_session_parsed:
                logger.warning(
                    f"[Browser] new_context failed with redis storage_state for {platform} "
                    f"({exc}); retrying without it"
                )
                context_kwargs.pop("storage_state", None)
                redis_session_is_dict = False
                redis_session_parsed = None
                context = await self._browser.new_context(**context_kwargs)
            else:
                raise

        # Inject stealth scripts
        await context.add_init_script(build_stealth_init_script(config))

        # Legacy list format: cookies must be added after the context exists.
        if redis_session_parsed is not None and not redis_session_is_dict:
            try:
                await context.add_cookies(redis_session_parsed)
                logger.info(f"[Browser] Restored redis cookies for {platform} (legacy list)")
            except Exception as exc:
                logger.warning(f"[Browser] Could not restore legacy redis cookies for {platform}: {exc}")

        return context

    async def save_session(self, candidate_id: str, platform: str, context: BrowserContext) -> None:
        # Persist the FULL storage_state (cookies + localStorage + origins) so
        # auth-walled ATSes don't force a fresh login (and bot-throttle) each
        # run. Fall back to a cookie-only blob if storage_state is unavailable
        # (e.g. a persistent context in some Playwright versions). Same redis
        # key + 7-day TTL either way.
        try:
            state = await context.storage_state()
        except Exception as exc:
            logger.warning(f"[Browser] storage_state() unavailable for {platform} ({exc}); saving cookies only")
            state = {"cookies": await context.cookies()}
        session_key = f"session:{candidate_id}:{platform}"
        redis_client = await self._get_redis()
        await redis_client.set(session_key, json.dumps(state), ex=_session_ttl_s())

    async def destroy_context(self, context: BrowserContext) -> None:
        # Idempotent + exception-safe. A context may already be closed (the
        # browser teardown in close() cascades to its contexts), and a
        # double-close raises in some Playwright versions. Cleanup must never
        # raise back into the caller's success/error path.
        if context is None:
            return
        try:
            await context.close()
        except Exception as exc:
            logger.debug(f"[Browser] destroy_context: close skipped ({exc})")

    async def close(self):
        # Full end-of-run teardown. destroy_context() only closes the CONTEXT;
        # WITHOUT this the underlying Chrome process, the node driver, and the
        # redis connection leak once per run. Under Celery concurrency that
        # exhausts RAM/FDs and surfaces as "Connection closed while reading
        # from the driver" / "Target page/context/browser has been closed".
        #
        # This is correct END-OF-RUN lifecycle management and does NOT violate
        # the "never close mid-run" guardrail — that rule governs the AgentLoop
        # action space (which has no close action), not the executor's teardown.
        #
        # Idempotent: each handle is dropped to None after closing so a second
        # call is a no-op, and every step is guarded so one failure can't strand
        # the others (or raise into the caller's finally).
        if self._persistent_ctx is not None:
            try:
                await self._persistent_ctx.close()
            except Exception as exc:
                logger.debug(f"[Browser] close: persistent ctx skipped ({exc})")
            self._persistent_ctx = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.debug(f"[Browser] close: browser skipped ({exc})")
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.debug(f"[Browser] close: playwright stop skipped ({exc})")
            self._playwright = None
        if self._redis is not None:
            try:
                await self._redis.close()
            except Exception as exc:
                logger.debug(f"[Browser] close: redis skipped ({exc})")
            self._redis = None
