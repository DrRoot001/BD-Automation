import hashlib
import os
import re
from pydantic import BaseModel
from typing import Dict, List, Sequence

# NOTE ON IDENTITY PHILOSOPHY (learned the hard way — see context_manager.py):
# This system launches REAL Chrome (channel="chrome"). Real Chrome already
# supplies genuine, mutually consistent values for user-agent, sec-ch-ua,
# navigator.userAgentData/brands, navigator.getBattery, navigator.connection,
# the real GPU string, etc. Overriding ANY one of those axes (e.g. a UA
# override claiming "macOS Chrome/133" while sec-ch-ua says otherwise)
# creates a multi-axis inconsistency that Greenhouse's react-select detects
# and responds to by silently refusing to open its dropdowns. Therefore we
# deliberately do NOT patch userAgentData, brands, sec-ch-ua, getBattery or
# connection anywhere in this module. The `user_agent` field below is kept
# on the model purely for reference/logging — it is never applied as a
# Playwright override.

USER_AGENTS = [
    # Current Chrome 136 (most common as of mid-2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864}
]

TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles"
]

# Timezones above are US-only, so the locale pool must be coherent with a
# North-American identity. en-GB / en-AU + a US timezone is an incoherent
# pairing that a fingerprint scorer can flag.
LOCALES = ["en-US", "en-CA"]

# Plausible WebGL vendor/renderer pairs — only used when the (opt-in)
# STEALTH_WEBGL_SPOOF block is enabled in build_stealth_init_script().
WEBGL_VENDORS = ["Intel Inc.", "Google Inc. (Intel)", "Google Inc. (NVIDIA)"]
WEBGL_RENDERERS = [
    "Intel Iris OpenGL Engine",
    "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)",
]


def _seeded_choice(candidate_id: str, options: Sequence, offset: int):
    """Deterministically pick from `options` for a given candidate.

    Hashes f"{candidate_id}:{offset}" so the same candidate always gets the
    same identity across runs (sticky fingerprint == fewer "new device"
    signals), while different offsets decorrelate the individual attributes.
    """
    digest = hashlib.sha256(f"{candidate_id}:{offset}".encode("utf-8")).digest()
    idx = int.from_bytes(digest[:8], "big") % len(options)
    return options[idx]


class StealthConfig(BaseModel):
    viewport: Dict[str, int]
    # Kept for reference/logging only — NEVER applied as a Playwright
    # user_agent override (real Chrome supplies the genuine UA; overriding
    # it desyncs sec-ch-ua/userAgentData and trips Greenhouse).
    user_agent: str
    timezone: str
    locale: str
    webgl_vendor: str
    canvas_noise: bool
    webdriver_patch: bool
    # New deterministic fields (defaults keep older construction sites working)
    webgl_renderer: str = "Intel Iris OpenGL Engine"
    color_scheme: str = "light"           # "light" | "dark"
    device_scale_factor: float = 1.0      # 1.0 | 1.25 | 1.5
    hardware_concurrency: int = 8         # informational — not spoofed in JS
    device_memory: int = 8                # informational — not spoofed in JS
    canvas_seed: int = 0                  # 0-255, seeds the canvas-noise LCG
    chrome_version: str = ""              # major version parsed from user_agent


def get_stealth_config(candidate_id: str) -> StealthConfig:
    """Build a per-candidate browser identity that is deterministic across runs."""
    viewport = _seeded_choice(candidate_id, VIEWPORTS, 1)
    timezone = _seeded_choice(candidate_id, TIMEZONES, 2)
    locale = _seeded_choice(candidate_id, LOCALES, 3)
    user_agent = _seeded_choice(candidate_id, USER_AGENTS, 4)
    hardware_concurrency = _seeded_choice(candidate_id, [4, 8, 12, 16], 5)
    device_memory = _seeded_choice(candidate_id, [4, 8], 6)
    color_scheme = _seeded_choice(candidate_id, ["light", "dark"], 9)
    device_scale_factor = _seeded_choice(candidate_id, [1.0, 1.25, 1.5], 10)
    webgl_vendor = _seeded_choice(candidate_id, WEBGL_VENDORS, 11)
    webgl_renderer = _seeded_choice(candidate_id, WEBGL_RENDERERS, 12)
    # canvas_seed: stable 0-255 value straight off the candidate hash — used
    # by the JS-side LCG so the canvas noise pattern is per-candidate stable.
    canvas_seed = hashlib.sha256(candidate_id.encode("utf-8")).digest()[0]

    # Major Chrome version from the (reference-only) UA — handy for logging
    # and for sanity-checking against the real browser build if ever needed.
    m = re.search(r"Chrome/(\d+)", user_agent)
    chrome_version = m.group(1) if m else ""

    return StealthConfig(
        viewport=viewport,
        user_agent=user_agent,
        timezone=timezone,
        locale=locale,
        webgl_vendor=webgl_vendor,
        canvas_noise=True,
        webdriver_patch=True,
        webgl_renderer=webgl_renderer,
        color_scheme=color_scheme,
        device_scale_factor=device_scale_factor,
        hardware_concurrency=hardware_concurrency,
        device_memory=device_memory,
        canvas_seed=canvas_seed,
        chrome_version=chrome_version,
    )


def build_stealth_init_script(config: StealthConfig) -> str:
    """Assemble the stealth init script for a given candidate identity.

    Deliberately minimal — we run REAL Chrome, so most fingerprint surfaces
    (UA, sec-ch-ua, userAgentData, battery, connection, fonts, GPU) are
    genuine and mutually consistent. We only patch:
      1. navigator.webdriver (the direct automation tell),
      2. window.chrome.runtime (Playwright contexts lack it; real Chrome has it),
      3. canvas noise (per-candidate deterministic, imperceptible).
    WebGL vendor/renderer spoofing is included but OPT-IN via the
    STEALTH_WEBGL_SPOOF env var: real Chrome reports a real GPU, and lying
    about it risks the same Greenhouse-style multi-axis inconsistency, so it
    stays off by default.
    """
    base_js = """
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

    // Canvas noise — deterministic per candidate via an LCG seeded with
    // CANVAS_SEED (no Math.random: the noise pattern must be identical on
    // every run so the canvas hash is a STABLE per-candidate value, not a
    // fresh anomaly each session). +/-1 per channel over a 4x4 region is
    // visually imperceptible but changes the fingerprint hash.
    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type, ...args) {
        try {
            const context = this.getContext('2d');
            if (context && this.width >= 4 && this.height >= 4) {
                // Simple LCG (Numerical Recipes constants), seeded per candidate
                let lcg = (__CANVAS_SEED__ + 1) >>> 0;
                const nextRand = () => {
                    lcg = (Math.imul(lcg, 1664525) + 1013904223) >>> 0;
                    return lcg;
                };
                const imageData = context.getImageData(0, 0, 4, 4);
                for (let i = 0; i < imageData.data.length; i++) {
                    if ((i + 1) % 4 === 0) continue; // leave alpha untouched
                    const delta = (nextRand() % 3) - 1; // -1, 0, or +1
                    const v = imageData.data[i] + delta;
                    imageData.data[i] = v < 0 ? 0 : (v > 255 ? 255 : v);
                }
                context.putImageData(imageData, 0, 0);
            }
        } catch (e) {
            // Canvas without a 2d context (e.g. webgl canvas) — leave untouched
        }
        return originalToDataURL.apply(this, [type, ...args]);
    };
})();
""".replace("__CANVAS_SEED__", str(int(config.canvas_seed) & 0xFF))

    # WebGL vendor/renderer spoofing — OPT-IN ONLY. Real Chrome reports the
    # machine's real GPU, which is coherent with every other hardware signal.
    # Claiming a different GPU risks the same multi-axis inconsistency that
    # broke Greenhouse's react-select when we overrode the UA. Enable via
    # STEALTH_WEBGL_SPOOF=1 only if a target demonstrably fingerprints GPUs.
    if os.getenv("STEALTH_WEBGL_SPOOF", "").strip().lower() in ("1", "true", "yes", "on"):
        webgl_js = """
(() => {
    try {
        const patchGetParameter = (proto) => {
            const original = proto.getParameter;
            proto.getParameter = function(parameter) {
                // 37445 = UNMASKED_VENDOR_WEBGL, 37446 = UNMASKED_RENDERER_WEBGL
                if (parameter === 37445) return '__WEBGL_VENDOR__';
                if (parameter === 37446) return '__WEBGL_RENDERER__';
                return original.apply(this, arguments);
            };
        };
        if (window.WebGLRenderingContext) patchGetParameter(WebGLRenderingContext.prototype);
        if (window.WebGL2RenderingContext) patchGetParameter(WebGL2RenderingContext.prototype);
    } catch (e) {}
})();
""".replace("__WEBGL_VENDOR__", config.webgl_vendor).replace(
            "__WEBGL_RENDERER__", config.webgl_renderer
        )
        base_js += webgl_js

    # Font-enumeration countermeasure — OPT-IN ONLY via STEALTH_FONT_MASK.
    #
    # Chrome exposes no enumerable font API; fingerprinters infer the installed
    # font set by rendering probe strings and measuring the resulting text
    # dimensions (per-font width deltas of a fraction of a pixel). The standard
    # low-risk defense is NOT to fake a font list (impossible/incoherent) but to
    # quantize the text-measurement side channel so those sub-pixel deltas
    # collapse onto a stable grid.
    #
    # SCOPE DECISION (deliberately narrow): we ONLY wrap
    # CanvasRenderingContext2D.prototype.measureText and round its reported
    # `width` to the nearest whole pixel. We do NOT patch getBoundingClientRect,
    # getClientRects, offsetWidth/offsetHeight or any layout-affecting read.
    # Globally quantizing element geometry risks breaking real layout and
    # absolutely-positioned widgets (e.g. Greenhouse's react-select), which is
    # the exact class of bug that spoofing identity caused before. measureText is
    # a pure off-DOM measurement API that layout does not depend on, so rounding
    # its width is safe. The quantization is deterministic (seeded from
    # config.canvas_seed) so the measured widths are a STABLE per-candidate
    # value across runs rather than a fresh anomaly each session, consistent
    # with the canvas-noise approach above.
    #
    # This overrides a DOM API, so it is fingerprint-risky in the same class as
    # the WebGL spoof (a patched measureText is itself detectable) — which is
    # precisely why it stays OFF by default.
    if os.getenv("STEALTH_FONT_MASK", "").strip().lower() in ("1", "true", "yes", "on"):
        font_js = """
(() => {
    try {
        const proto = (window.CanvasRenderingContext2D
            && window.CanvasRenderingContext2D.prototype) || null;
        if (!proto || typeof proto.measureText !== 'function') return; // no-op
        const originalMeasureText = proto.measureText;
        // Deterministic per-candidate sub-pixel offset in [0,1) derived from the
        // canvas seed, so the rounding grid is stable but not identical to the
        // default machine. Kept tiny so quantized widths stay physically plausible.
        const seedOffset = ((__CANVAS_SEED__ & 0xFF) / 256);
        proto.measureText = function(text) {
            const metrics = originalMeasureText.apply(this, arguments);
            try {
                const w = metrics && metrics.width;
                if (typeof w === 'number' && isFinite(w)) {
                    // Quantize to the nearest whole pixel (stable grid); this
                    // collapses the per-font sub-pixel width deltas fingerprinters
                    // rely on. All other TextMetrics properties are preserved by
                    // returning the original object and only redefining `width`.
                    const quantized = Math.round(w + seedOffset) - Math.round(seedOffset);
                    Object.defineProperty(metrics, 'width', {
                        get: () => quantized,
                        configurable: true,
                    });
                }
            } catch (e) {
                // If width is non-writable or anything else fails, return the
                // untouched original metrics — never break the measurement.
            }
            return metrics;
        };
    } catch (e) {
        // Missing API / unexpected environment — no-op, keep native behavior.
    }
})();
""".replace("__CANVAS_SEED__", str(int(config.canvas_seed) & 0xFF))
        base_js += font_js

    return base_js
