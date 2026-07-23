import os
import sys
import json
import secrets
import logging
import weakref
from typing import Optional
from urllib.parse import urlparse, unquote
import redis.asyncio as redis
from playwright.async_api import async_playwright, BrowserContext, Playwright
from dotenv import load_dotenv
from .stealth_config import get_stealth_config, build_stealth_init_script, StealthConfig

load_dotenv()

# Maps each live BrowserContext → the EXACT proxy dict it was created with
# (post sticky-session, so the credentials pin the SAME upstream residential
# IP). The captcha service reads this to solve a Cloudflare managed challenge
# (AntiCloudflareTask) through the identical IP the browser uses — a
# cf_clearance cookie is only valid for the IP+UA that solved it. Keyed weakly
# so entries evict when the context is GC'd; concurrency-safe (per-context).
_CONTEXT_PROXY: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def get_proxy_for_context(context) -> Optional[dict]:
    """Return the proxy dict a context was created with, or None (no proxy /
    unknown context). Never raises."""
    try:
        return _CONTEXT_PROXY.get(context)
    except Exception:
        return None
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


def _apply_sticky_session(proxy_config: Optional[dict]) -> Optional[dict]:
    """Append a fresh random session token to the proxy password so every
    request in THIS context shares ONE upstream residential IP, while the NEXT
    context (i.e. the next apply run — get_context() is called once per apply)
    gets a brand-new IP. Without this, a "randomize IP" residential plan hands
    out a different IP per request and breaks multi-step ATS forms mid-submit.

    IPRoyal carries session state in the PASSWORD field, not the username:
        <password>_session-<id>_lifetime-<ttl>
    (the existing `_country-us` suffix is another such param and is preserved).

    Controlled by env:
      PROXY_STICKY_SESSION   — "true"/"false". Default: auto-on when the proxy
                               host looks like IPRoyal (geo.iproyal.com).
      PROXY_SESSION_LIFETIME — IPRoyal lifetime suffix, e.g. "30m" (default),
                               "1h". Hold long enough to cover one full apply.

    No-op (returns the dict unchanged) when disabled or when there is no
    password to attach the session to.
    """
    if not proxy_config or "password" not in proxy_config:
        return proxy_config
    server = proxy_config.get("server", "").lower()
    default_on = "iproyal" in server
    enabled = os.getenv("PROXY_STICKY_SESSION", str(default_on)).lower() == "true"
    if not enabled:
        return proxy_config
    lifetime = (os.getenv("PROXY_SESSION_LIFETIME", "30m").strip() or "30m")
    token = secrets.token_hex(8)
    cfg = dict(proxy_config)
    cfg["password"] = f"{cfg['password']}_session-{token}_lifetime-{lifetime}"
    logger.info(
        f"[Browser] Sticky proxy session: id={token} lifetime={lifetime} "
        f"(one held IP for this apply run)"
    )
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


async def clear_redis_session(candidate_id: str, platform: str) -> int:
    """Delete a candidate's persisted Redis session blob(s) so a proven-stale
    session isn't auto-restored on the next run (see get_context's restore).

    Deletes the exact ``session:{candidate_id}:{platform}`` key plus any
    host-variant keys for the same candidate — standard runs key on the bare
    host (``builtin.com``), ad-hoc URL runs key on the full job URL, and both
    must go or get_context re-restores a dead blob. Returns the number of keys
    removed; never raises (session hygiene must not break a run).
    """
    try:
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        kwargs = {"ssl_cert_reqs": None} if url.startswith("rediss://") else {}
        client = redis.from_url(url, **kwargs)
        host = urlparse(platform if "//" in platform else f"//{platform}").hostname or platform
        token = host.split(":")[0]
        deleted = await client.delete(f"session:{candidate_id}:{platform}")
        async for k in client.scan_iter(match=f"session:{candidate_id}:*{token}*", count=200):
            deleted += await client.delete(k)
        if deleted:
            logger.warning(
                f"[Browser] Cleared {deleted} stale session key(s) for "
                f"{candidate_id} ({token}) — next run starts clean."
            )
        await client.aclose()
        return deleted
    except Exception as exc:
        logger.debug(f"[Browser] clear_redis_session skipped: {exc}")
        return 0


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

    async def get_context(self, candidate_id: str, platform: str, target_url: str = "") -> BrowserContext:
        config: StealthConfig = get_stealth_config(candidate_id)

        redis_client = await self._get_redis()
        session_key = f"session:{candidate_id}:{platform}"
        # The persisted browser session is a CACHE (reuse a logged-in session
        # across runs), NOT a hard dependency. If Redis is unreachable/slow, we
        # must DEGRADE to a fresh context — never crash the whole application
        # run on a broker blip (matches the state-machine/DB fail-open policy).
        session_data = None
        try:
            session_data = await redis_client.get(session_key)
        except Exception as exc:
            logger.warning(
                f"[Browser] session cache unavailable ({type(exc).__name__}: "
                f"{str(exc)[:80]}) — starting a fresh browser context (no session reuse)"
            )
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
            # Fall back to headless=True if:
            # 1. PLAYWRIGHT_HEADLESS or HEADLESS env is explicitly set to true, OR
            # 2. Environment is production, OR
            # 3. Running on Linux without a DISPLAY environment variable set.
            raw_headless = os.getenv("PLAYWRIGHT_HEADLESS")
            if raw_headless is None:
                raw_headless = os.getenv("HEADLESS")

            if raw_headless is not None:
                headless = raw_headless.strip().lower() in ("true", "1", "yes")
            else:
                is_production = os.getenv("ENVIRONMENT", "").lower() == "production"
                has_display = bool(os.getenv("DISPLAY"))
                is_linux = sys.platform.startswith("linux")
                headless = is_production or (is_linux and not has_display)
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
        # Required for sites with CloudFront/Akamai/Cloudflare WAF that block
        # datacenter IPs (himalayas, talent, linkedin, ...).
        #
        # SCOPING (important for throughput): a residential proxy adds ~3-4x
        # latency PER REQUEST. Routing EVERY apply through it made the fast,
        # non-walled hosts (Dice/Greenhouse/Lever/Ashby/SmartRecruiters) so slow
        # that the 2 browser slots saturated and QUEUED jobs were reaped by the
        # 15-min watchdog (observed live 2026-07-12). So the proxy is applied
        # ONLY to hosts that actually need it:
        #   PROXY_HOSTS      — comma-separated host substrings to proxy. If unset
        #                      (and PROXY_URL is set) a built-in bot-walled set is
        #                      used. Empty string ("") also means the default set.
        #   PROXY_ALL_HOSTS  — "true" restores the old proxy-everything behavior.
        proxy_url = os.getenv("PROXY_URL", "").strip()
        proxy_config = None
        if proxy_url:
            _plat = (platform or "").lower()
            # Key the proxy decision on the HOST WE ACTUALLY NAVIGATE TO, not the
            # aggregator source label. An aggregator like RemoteRocketship is
            # bot-walled (so a RAW remoterocketship.com URL still gets proxied to
            # scrape it), but its listings resolve to inner ATS sites that are
            # usually NOT bot-walled (careers.westerncomputer.com, jobs.lever.co,
            # …). Forcing the slow residential proxy on those inner sites made the
            # full browser page-load (JS+fonts+assets) time out — net::ERR_TIMED_OUT
            # — even though the site loads fine directly. So when we know the
            # target URL, decide from its host; the aggregator label is only the
            # fallback (e.g. no URL yet).
            _target_host = ""
            if target_url:
                try:
                    _target_host = (urlparse(target_url if "//" in target_url else "//" + target_url).hostname or "").lower()
                except Exception:
                    _target_host = ""
            _subject = _target_host or _plat
            _all = os.getenv("PROXY_ALL_HOSTS", "false").strip().lower() in ("1", "true", "yes", "on")
            _hosts_env = os.getenv("PROXY_HOSTS", "").strip()
            if _hosts_env:
                # Operator override — proxy exactly these host substrings.
                _proxy_hosts = [h.strip().lower() for h in _hosts_env.split(",") if h.strip()]
                _needs_proxy = _all or any(h in _subject for h in _proxy_hosts)
            else:
                # Default: the shared bot-walled host set (WAF hosts that block
                # datacenter IPs — Cloudflare/PerimeterX/Akamai). Includes
                # simplyhired etc. so their Cloudflare wall is proxied (a
                # residential IP usually passes silently, and Anti-Captcha's
                # proxied Cloudflare solve runs through the SAME IP).
                from ..hosts import is_bot_walled
                _needs_proxy = _all or is_bot_walled(_subject)
            if not _needs_proxy:
                logger.info(
                    f"[Browser] Proxy configured but SKIPPED for '{platform}' "
                    f"(not a bot-walled host; keeps fast hosts fast). "
                    f"Set PROXY_ALL_HOSTS=true or add to PROXY_HOSTS to force."
                )
            else:
                # Playwright IGNORES inline user:pass@ creds in the server field —
                # they must be split into username/password keys. _parse_proxy_url
                # does that and returns None if the URL is unusable.
                proxy_config = _parse_proxy_url(proxy_url)
                if proxy_config:
                    # Hold ONE residential IP for this whole apply run; the next
                    # apply (next get_context call) rotates to a fresh IP.
                    proxy_config = _apply_sticky_session(proxy_config)
                    # Log host:port only — never the credentials.
                    logger.info(f"[Browser] Using proxy for '{platform}': {proxy_config['server']}")
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

        # ── Persistent storage_state (cookies + localStorage), PER CANDIDATE ──
        # Auth-walled ATSes (Dice, LinkedIn, Workday) bot-throttle repeated
        # logins, so reusing a saved storage_state avoids re-login churn. This
        # MUST be scoped per candidate: a single, candidate-agnostic file makes
        # every candidate apply inside whatever account seeded it — the root
        # cause of wrong-account submissions (e.g. one candidate's job submitted
        # under the account that bootstrapped the shared file). The default path
        # is therefore backend/data/sessions/<platform>/<candidate_id>.json.
        # When a file IS used we DON'T also restore the redis blob below
        # (storage_state already carries the cookies).
        #
        # "<PLATFORM>_STORAGE_STATE" is kept as an explicit operator override,
        # but it is GLOBAL — applied to EVERY candidate — so it is only safe for
        # single-account/testing setups. We warn loudly whenever it is used.
        storage_state_used = False
        try:
            from pathlib import Path as _Path
            backend_dir = _Path(__file__).resolve().parents[3]
            sessions_dir = backend_dir / "data" / "sessions"

            env_override = os.getenv(f"{platform.upper()}_STORAGE_STATE", "").strip()
            per_candidate_path = sessions_dir / platform / f"{candidate_id}.json"

            if env_override and os.path.isfile(env_override):
                context_kwargs["storage_state"] = env_override
                storage_state_used = True
                logger.warning(
                    f"[Browser] Using GLOBAL {platform.upper()}_STORAGE_STATE override "
                    f"← {env_override} — this session is applied to ALL candidates "
                    f"(current candidate_id={candidate_id}); only safe for a "
                    "single-account/testing setup, never multi-candidate."
                )
            elif per_candidate_path.is_file():
                context_kwargs["storage_state"] = str(per_candidate_path)
                storage_state_used = True
                logger.info(
                    f"[Browser] Loaded per-candidate storage_state for {platform} "
                    f"← {per_candidate_path}"
                )
            else:
                # Legacy candidate-agnostic file (backend/data/sessions/<platform>.json)
                # is intentionally NOT loaded any more — it caused cross-candidate
                # account bleed. Warn so operators who seeded it know why it no
                # longer applies; the correct per-candidate redis session (or a
                # fresh per-candidate login) governs instead.
                legacy_shared = sessions_dir / f"{platform}.json"
                if legacy_shared.is_file():
                    logger.warning(
                        f"[Browser] Ignoring legacy SHARED session file {legacy_shared} "
                        f"— storage_state is now per-candidate. Delete it, or move it to "
                        f"{per_candidate_path} for this candidate, or set "
                        f"{platform.upper()}_STORAGE_STATE for an intentional single-account setup."
                    )
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

        # Remember the exact proxy this context uses so the captcha service can
        # solve a Cloudflare managed challenge (AntiCloudflareTask) through the
        # SAME residential IP (cf_clearance is bound to IP+UA).
        if proxy_config:
            try:
                _CONTEXT_PROXY[context] = proxy_config
            except Exception:
                pass

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
        # Persisting the session is best-effort — a Redis outage must not fail an
        # otherwise-successful application (worst case: next run logs in again).
        try:
            redis_client = await self._get_redis()
            await redis_client.set(session_key, json.dumps(state), ex=_session_ttl_s())
        except Exception as exc:
            logger.warning(
                f"[Browser] could not persist session for {platform} "
                f"({type(exc).__name__}: {str(exc)[:80]}) — skipping (next run re-auths)"
            )

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
