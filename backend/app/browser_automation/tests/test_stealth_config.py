"""Unit tests for the seeded per-candidate identity system.

These are pure-Python tests (no browser, no network) covering the properties the
identity subsystem *promises* and that nothing else verifies today:

  * determinism   — the same candidate_id yields a byte-identical identity on
                    every call and across process runs (sticky fingerprint).
  * coherence     — a US timezone is never paired with a non-North-American
                    locale (the fingerprint red-flag removed in commit 7bfd305).
  * canvas seed   — derived from the candidate hash, in range, stable.
  * decorrelation — different offsets/candidates spread across the option pools.
  * init script   — the assembled JS substitutes the seed, always patches the
                    two automation tells, and keeps WebGL/font spoofs opt-in.

Run: `pytest backend/app/browser_automation/tests/test_stealth_config.py -q`
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.browser.stealth_config import (  # noqa: E402
    LOCALES,
    TIMEZONES,
    USER_AGENTS,
    VIEWPORTS,
    StealthConfig,
    _seeded_choice,
    build_stealth_init_script,
    get_stealth_config,
)

# A spread of candidate ids exercised by every property test below.
_CANDIDATES = [
    "76a9f624-ad21-41ac-bf10-fad936413b75",
    "candidate-abc",
    "another_candidate_2",
    "00000000-0000-0000-0000-000000000000",
    "z",  # single char — edge case for hashing
    "🙂-unicode-candidate",
]


# ── determinism ──────────────────────────────────────────────────────────────

def test_get_stealth_config_is_deterministic_per_candidate():
    for cid in _CANDIDATES:
        a = get_stealth_config(cid)
        b = get_stealth_config(cid)
        # Byte-for-byte identity across repeated calls.
        assert a.model_dump() == b.model_dump(), f"identity drifted for {cid!r}"


def test_seeded_choice_is_deterministic_and_in_range():
    for cid in _CANDIDATES:
        for offset in range(0, 14):
            first = _seeded_choice(cid, VIEWPORTS, offset)
            second = _seeded_choice(cid, VIEWPORTS, offset)
            assert first == second
            assert first in VIEWPORTS


def test_seeded_choice_offset_decorrelates():
    # Different offsets should not all collapse to the same index. Use a large
    # pool (USER_AGENTS, 8 options) so a genuine spread is observable.
    picks = {_seeded_choice("stable-candidate", USER_AGENTS, off) for off in range(20)}
    assert len(picks) > 1, "offsets did not decorrelate — hashing may be broken"


# ── coherence ────────────────────────────────────────────────────────────────

def test_timezone_locale_coherence():
    # Every generated identity must pair a US timezone with a North-American
    # locale. The pools guarantee this structurally; assert it holds so a future
    # pool edit that reintroduces en-GB/en-AU fails loudly.
    assert set(LOCALES) <= {"en-US", "en-CA"}
    for cid in _CANDIDATES + [f"c{i}" for i in range(50)]:
        cfg = get_stealth_config(cid)
        assert cfg.timezone in TIMEZONES
        assert cfg.locale in LOCALES


def test_config_fields_have_expected_shapes():
    cfg = get_stealth_config("shape-check")
    assert isinstance(cfg, StealthConfig)
    assert set(cfg.viewport) == {"width", "height"}
    assert cfg.color_scheme in ("light", "dark")
    assert cfg.device_scale_factor in (1.0, 1.25, 1.5)
    assert cfg.hardware_concurrency in (4, 8, 12, 16)
    assert cfg.device_memory in (4, 8)
    assert cfg.canvas_noise is True
    assert cfg.webdriver_patch is True


# ── canvas seed ──────────────────────────────────────────────────────────────

def test_canvas_seed_is_first_hash_byte_and_in_range():
    import hashlib

    for cid in _CANDIDATES:
        cfg = get_stealth_config(cid)
        expected = hashlib.sha256(cid.encode("utf-8")).digest()[0]
        assert cfg.canvas_seed == expected
        assert 0 <= cfg.canvas_seed <= 255


def test_chrome_version_parsed_from_user_agent():
    for cid in _CANDIDATES:
        cfg = get_stealth_config(cid)
        assert cfg.chrome_version, "chrome_version should parse from the reference UA"
        m = re.search(r"Chrome/(\d+)", cfg.user_agent)
        assert m and cfg.chrome_version == m.group(1)


# ── init script assembly ─────────────────────────────────────────────────────

def test_init_script_always_patches_automation_tells():
    cfg = get_stealth_config("init-base")
    js = build_stealth_init_script(cfg)
    assert "navigator.webdriver" in js
    assert "window.chrome" in js and "runtime" in js
    assert "toDataURL" in js  # canvas noise present


def test_init_script_substitutes_canvas_seed_placeholder():
    cfg = get_stealth_config("init-seed")
    js = build_stealth_init_script(cfg)
    assert "__CANVAS_SEED__" not in js, "seed placeholder left unsubstituted"
    assert str(int(cfg.canvas_seed) & 0xFF) in js


def test_webgl_and_font_spoofs_are_opt_in(monkeypatch):
    cfg = get_stealth_config("opt-in-check")

    # Default: both opt-in blocks absent.
    for var in ("STEALTH_WEBGL_SPOOF", "STEALTH_FONT_MASK"):
        monkeypatch.delenv(var, raising=False)
    base = build_stealth_init_script(cfg)
    assert "getParameter" not in base  # WebGL spoof off
    assert "measureText" not in base   # font mask off

    # WebGL spoof on → getParameter patch + the candidate's vendor string appear.
    monkeypatch.setenv("STEALTH_WEBGL_SPOOF", "1")
    with_webgl = build_stealth_init_script(cfg)
    assert "getParameter" in with_webgl
    assert cfg.webgl_vendor in with_webgl
    monkeypatch.delenv("STEALTH_WEBGL_SPOOF", raising=False)

    # Font mask on → measureText wrapper appears.
    monkeypatch.setenv("STEALTH_FONT_MASK", "1")
    with_font = build_stealth_init_script(cfg)
    assert "measureText" in with_font


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
