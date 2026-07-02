"""Unit tests for the production-hardening helpers added across the module.

Pure-Python / filesystem only (no browser, no network):
  * field-memory TTL staleness (forms/memory.py)
  * OTP timeout env override (verification/code_fetcher.py)
  * Redis session TTL env override (browser/context_manager.py)
  * stale session-file invalidation (adapters/session_utils.py)

Run: `pytest backend/app/browser_automation/tests/test_production_hardening.py -q`
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from backend.app.browser_automation.forms import memory as field_memory  # noqa: E402


# ── field-memory TTL ─────────────────────────────────────────────────────────

def test_ttl_seconds_env_and_disable(monkeypatch):
    monkeypatch.delenv("FIELD_MEMORY_TTL_DAYS", raising=False)
    assert field_memory._ttl_seconds() == 180 * 86400  # default
    monkeypatch.setenv("FIELD_MEMORY_TTL_DAYS", "30")
    assert field_memory._ttl_seconds() == 30 * 86400
    monkeypatch.setenv("FIELD_MEMORY_TTL_DAYS", "0")  # disabled
    assert field_memory._ttl_seconds() == 0
    monkeypatch.setenv("FIELD_MEMORY_TTL_DAYS", "garbage")  # bad value → default
    assert field_memory._ttl_seconds() == 180 * 86400


def test_is_stale(monkeypatch):
    monkeypatch.setenv("FIELD_MEMORY_TTL_DAYS", "180")
    now = int(time.time())
    fresh = {"value": "x", "last_seen": now - 10 * 86400}       # 10 days old
    stale = {"value": "x", "last_seen": now - 200 * 86400}      # 200 days old
    undated = {"value": "x"}                                    # legacy record
    assert field_memory._is_stale(fresh) is False
    assert field_memory._is_stale(stale) is True
    assert field_memory._is_stale(undated) is False  # grandfathered
    # TTL disabled → nothing is stale
    monkeypatch.setenv("FIELD_MEMORY_TTL_DAYS", "0")
    assert field_memory._is_stale(stale) is False


def test_recall_skips_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(field_memory, "_MEMORY_DIR", tmp_path)
    monkeypatch.setenv("FIELD_MEMORY_TTL_DAYS", "180")
    now = int(time.time())
    cid = "cand-ttl-test"
    path = field_memory._memory_path(cid)
    # Two entries under this candidate: one fresh, one 300 days old.
    field_memory._save(path, {
        "current company / employer": {"value": "FreshCo", "last_seen": now - 5 * 86400,
                                       "field_type": "text", "count": 1},
        "location": {"value": "StaleCity", "last_seen": now - 300 * 86400,
                     "field_type": "text", "count": 1},
    })
    assert field_memory.recall("Current company", "text", candidate_id=cid) == "FreshCo"
    assert field_memory.recall("Location", "text", candidate_id=cid) is None  # stale → skipped


# ── OTP timeout override ─────────────────────────────────────────────────────

def test_verify_code_timeout_env(monkeypatch):
    from backend.app.browser_automation.verification import code_fetcher
    monkeypatch.delenv("VERIFY_CODE_TIMEOUT_S", raising=False)
    assert code_fetcher._default_code_timeout() == 90.0
    monkeypatch.setenv("VERIFY_CODE_TIMEOUT_S", "150")
    assert code_fetcher._default_code_timeout() == 150.0
    monkeypatch.setenv("VERIFY_CODE_TIMEOUT_S", "nope")
    assert code_fetcher._default_code_timeout() == 90.0


# ── Redis session TTL override ───────────────────────────────────────────────

def test_session_ttl_env(monkeypatch):
    from backend.app.browser_automation.browser import context_manager
    monkeypatch.delenv("SESSION_TTL_DAYS", raising=False)
    assert context_manager._session_ttl_s() == 7 * 86400
    monkeypatch.setenv("SESSION_TTL_DAYS", "3")
    assert context_manager._session_ttl_s() == 3 * 86400
    monkeypatch.setenv("SESSION_TTL_DAYS", "0")  # clamped to >= 1 day
    assert context_manager._session_ttl_s() == 1 * 86400


# ── session-file invalidation ────────────────────────────────────────────────

def test_invalidate_session_file(monkeypatch, tmp_path):
    from backend.app.browser_automation.adapters import session_utils
    sess = tmp_path / "linkedin.json"
    sess.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LINKEDIN_STORAGE_STATE", str(sess))
    assert session_utils.session_file_path("linkedin") == sess
    assert session_utils.invalidate_session_file("linkedin") is True
    assert not sess.exists()
    # Idempotent: a second call (file already gone) returns False, no raise.
    assert session_utils.invalidate_session_file("linkedin") is False


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
