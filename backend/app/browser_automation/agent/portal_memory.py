"""Cross-run portal memory — the AI's long-term experience layer.

Every application run ends with a structured outcome. This module distils that
outcome into a compact, per-portal memory and, on the NEXT run against the same
portal, injects it back into the AgentLoop's system prompt as "prior
experience". The effect: the AI stops re-discovering each portal from scratch —
it remembers what flow worked, which submit control was real, and what went
wrong last time, and is told to avoid repeating the failure.

This is deliberately DIFFERENT from two neighbouring stores:
  * ``learned_fixes`` — a raw per-ATS selector cache (apply_button / submit).
  * ``forms/memory`` (field_memory) — per-field ANSWER recall (what to type).
Portal memory is the FLOW/strategy layer that sits above both: "on this portal,
the shape of a successful application looks like X; last failure was Y."

Keying
------
The memory key is the **effective platform slug** when we have a dedicated or
recognised adapter (``smartrecruiters``, ``greenhouse``, …) so knowledge is
shared across every tenant of that ATS. For a truly unknown portal driven by
the generic adapter, we key by **host** instead, so each unknown site keeps its
own memory rather than pooling every unknown site under "generic".

Storage: ``backend/app/browser_automation/portal_memory/<key>.json`` (gitignored
runtime cache, human-readable, safe to delete to reset a portal's memory).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[1] / "portal_memory"
_lock = threading.Lock()

# Platform slugs that are NOT specific enough to pool across sites — for these
# we key portal memory by host so each unknown portal keeps its own lessons.
_GENERIC_SLUGS = {"", "generic", "unknown"}

# Bounds — keep the store small and the recall prompt block cheap.
_MAX_OUTCOMES = 12          # rolling window of outcomes kept per portal
_MAX_NOTE_CHARS = 320
_RECENT_FLOW_STEPS = 14


def memory_key(platform: Optional[str], url: Optional[str]) -> Optional[str]:
    """Compute the portal-memory key from the effective platform + landed URL.

    Returns a slug for recognised ATSes, else the host for unknown portals,
    else None when neither is usable (nothing to key on → skip memory)."""
    slug = (platform or "").strip().lower()
    if slug and slug not in _GENERIC_SLUGS:
        return slug
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        host = ""
    return host or None


def _safe_name(key: str) -> str:
    return "".join(c if (c.isalnum() or c in ".-_") else "_" for c in key)[:120] or "unknown"


def _path_for(key: str) -> Path:
    return _BASE_DIR / f"{_safe_name(key)}.json"


def _load(key: str) -> Dict[str, Any]:
    path = _path_for(key)
    if not path.exists():
        return {"_meta": {"key": key}, "stats": {"success": 0, "fail": 0}, "outcomes": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("stats", {"success": 0, "fail": 0})
        data.setdefault("outcomes", [])
        return data
    except Exception as exc:
        logger.warning(f"[PortalMemory] could not load {path.name}: {exc}")
        return {"_meta": {"key": key}, "stats": {"success": 0, "fail": 0}, "outcomes": []}


def _persist(key: str, data: Dict[str, Any], now: int) -> None:
    path = _path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["_meta"] = {"key": key, "updated": now}
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(path)
    except Exception as exc:
        logger.warning(f"[PortalMemory] could not persist {path.name}: {exc}")


def _summarise_actions(actions: List[Any]) -> Dict[str, Any]:
    """Derive factual signals + winning selectors from the run's action trace."""
    kinds: Dict[str, int] = {}
    filled = 0
    uploaded = False
    captcha = False
    login = False
    winning_submit: Optional[str] = None
    winning_apply: Optional[str] = None
    flow: List[str] = []
    for a in actions or []:
        kind = getattr(a, "kind", None) or (a.get("kind") if isinstance(a, dict) else None)
        if not kind:
            continue
        ok = getattr(a, "ok", True) if not isinstance(a, dict) else a.get("ok", True)
        sel = getattr(a, "selector", None) if not isinstance(a, dict) else a.get("selector")
        label = getattr(a, "field_label", None) if not isinstance(a, dict) else a.get("field_label")
        kinds[kind] = kinds.get(kind, 0) + 1
        flow.append(kind if not label else f"{kind}:{label}")
        if kind == "fill_field" and ok:
            filled += 1
            if label and "password" in label.lower():
                login = True
        elif kind == "upload_file" and ok:
            uploaded = True
        elif kind == "solve_captcha":
            captcha = True
        elif kind in ("click_apply",) and ok and sel:
            winning_apply = sel
        elif kind in ("click", "submit") and ok and sel:
            # Heuristic: the last successful click before a done/submit is the
            # most likely real submit control.
            winning_submit = sel
    return {
        "kinds": kinds,
        "filled_fields": filled,
        "uploaded_file": uploaded,
        "captcha_solved": captcha,
        "login": login,
        "winning_apply": winning_apply,
        "winning_submit": winning_submit,
        "flow": flow[-_RECENT_FLOW_STEPS:],
    }


def _deterministic_note(ok: bool, status: str, steps: int, sig: Dict[str, Any], error: Optional[str]) -> str:
    bits: List[str] = []
    if ok:
        bits.append(f"Reached {status} in {steps} steps")
    else:
        bits.append(f"Ended {status} after {steps} steps (no confirmed submit)")
    bits.append(f"filled {sig.get('filled_fields', 0)} field(s)")
    if sig.get("uploaded_file"):
        bits.append("uploaded resume")
    if sig.get("captcha_solved"):
        bits.append("solved a captcha")
    if sig.get("login"):
        bits.append("logged in")
    if ok and sig.get("winning_submit"):
        bits.append(f"submit control was {sig['winning_submit']!r}")
    if not ok and error:
        bits.append(f"error: {error[:120]}")
    return "; ".join(bits) + "."


def record_outcome(
    key: Optional[str],
    *,
    host: Optional[str],
    ok: bool,
    status: str,
    steps: int,
    actions: Optional[List[Any]] = None,
    error: Optional[str] = None,
    ai_note: Optional[str] = None,
) -> None:
    """Persist one run's outcome to the portal's memory. Never raises."""
    if not key:
        return
    try:
        sig = _summarise_actions(actions or [])
        note = _deterministic_note(ok, status, steps, sig, error)
        if ai_note:
            note = f"{note} AI lesson: {ai_note.strip()[:_MAX_NOTE_CHARS]}"
        now = int(time.time())
        with _lock:
            data = _load(key)
            data["stats"]["success" if ok else "fail"] += 1
            data["outcomes"].insert(0, {
                "ts": now,
                "ok": ok,
                "status": status,
                "steps": steps,
                "host": host or "",
                "signals": {k: sig[k] for k in ("filled_fields", "uploaded_file", "captcha_solved", "login")},
                "winning_apply": sig.get("winning_apply"),
                "winning_submit": sig.get("winning_submit"),
                "flow": sig.get("flow"),
                "note": note[:_MAX_NOTE_CHARS + 160],
            })
            data["outcomes"] = data["outcomes"][:_MAX_OUTCOMES]
            _persist(key, data, now)
        logger.info(
            f"[PortalMemory] recorded {'success' if ok else 'failure'} for "
            f"{key!r} (now {data['stats']['success']}✓/{data['stats']['fail']}✗)"
        )
    except Exception as exc:  # memory is best-effort — never break a run
        logger.debug(f"[PortalMemory] record_outcome failed (non-fatal): {exc}")


def recall(key: Optional[str]) -> str:
    """Return a compact prompt block of prior experience, or '' on cold start."""
    if not key:
        return ""
    try:
        with _lock:
            data = _load(key)
    except Exception:
        return ""
    outcomes = data.get("outcomes") or []
    if not outcomes:
        return ""
    stats = data.get("stats", {})
    ok_n, fail_n = stats.get("success", 0), stats.get("fail", 0)
    last_success = next((o for o in outcomes if o.get("ok")), None)
    last_failure = next((o for o in outcomes if not o.get("ok")), None)
    lines = [
        "\n\n══════════════════════════════════════════════════════════════════════\n"
        f"PRIOR EXPERIENCE ON THIS PORTAL ({key} — {ok_n}✓ / {fail_n}✗ over "
        f"{ok_n + fail_n} past runs; the LIVE page is still authoritative)\n"
        "══════════════════════════════════════════════════════════════════════"
    ]
    if last_success:
        lines.append(f"  ✓ A PAST SUCCESS: {last_success.get('note', '')}")
        flow = last_success.get("flow") or []
        if flow:
            lines.append(f"    flow that worked: {' → '.join(flow)}")
        if last_success.get("winning_submit"):
            lines.append(f"    reliable submit selector: {last_success['winning_submit']}")
    if last_failure and (not last_success or last_failure.get("ts", 0) >= last_success.get("ts", 0)):
        lines.append(
            f"  ✗ LAST FAILURE: {last_failure.get('note', '')} "
            "— do NOT repeat this; try a different approach for that step."
        )
    if not last_success and last_failure:
        lines.append(
            "  ⚠ No success recorded yet on this portal — earlier attempts failed. "
            "Read the live page carefully and avoid the failure above."
        )
    return "\n".join(lines) + "\n"
