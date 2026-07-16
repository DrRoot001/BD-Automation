"""Autonomous per-portal playbook memory.

Where `LearnedFixes` caches winning *selectors* per ATS and `forms.memory`
caches field *answers* per candidate, this module is the third memory layer:
a **self-authored playbook the agent writes for every portal it visits**,
keyed by the live **host** (e.g. ``boards.greenhouse.io``, ``jobs.lever.co``,
``acme.wd1.myworkdayjobs.com``) — finer than per-ATS.

After every run the executor calls :func:`record` with the run outcome and the
ordered action list. This module distills that into a compact playbook:

  * ``working_selectors`` — selectors that actually clicked/filled/submitted
  * ``flow``              — the ordered action kinds that got there
  * ``success_signal``    — the confirmation URL/text when the run CONFIRMED
  * ``captcha_types``     — captcha kinds seen on this host
  * ``login_required``    — whether the host gated on a login
  * ``last_outcome`` + rolling ``runs``/``successes`` counters

On the *next* visit to the same host, :func:`format_for_prompt` renders the
playbook into a short block that ``AgentLoop._build_system_prompt`` injects, so
the agent starts from what worked last time instead of rediscovering it.

Design notes / guarantees:
  * Best-effort and non-fatal — every public function swallows its own errors;
    a memory miss or a corrupt file never breaks an apply run.
  * Additive — until a host has data, :func:`format_for_prompt` returns ``""``,
    so behaviour is unchanged for never-seen portals.
  * Human-readable JSON, one file per host, atomic writes, thread-locked —
    same conventions as :mod:`learned_fixes`.
  * Env gate ``PORTAL_MEMORY_ENABLED`` (default on); set to ``0``/``false`` to
    disable read+write entirely.

Storage layout::

    backend/app/browser_automation/learned_fixes/portals/<host>.json
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[1] / "learned_fixes" / "portals"
_lock = threading.Lock()

# Caps to keep a flaky/high-traffic host's file bounded.
_MAX_SELECTORS_PER_KIND = 8
_MAX_AVOID_PER_KIND = 10
_MAX_FLOW_STEPS = 24
_MAX_NOTES = 12

# Action kinds whose successful selector is worth remembering as a reusable
# entry point. Scroll/wait/think/etc. carry no reusable selector.
_SELECTOR_KINDS = {
    "click", "fill_field", "upload_file", "submit", "select_option", "check",
}


def _enabled() -> bool:
    return os.getenv("PORTAL_MEMORY_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def host_of(url_or_host: str) -> str:
    """Normalise a URL (or bare host) to a lowercase host usable as a filename.

    ``https://boards.greenhouse.io/vercel/jobs/123`` → ``boards.greenhouse.io``
    A bare host is returned as-is (lowercased). ``www.`` is stripped so
    ``www.dice.com`` and ``dice.com`` share one playbook.
    """
    s = (url_or_host or "").strip()
    if not s:
        return ""
    host = urlparse(s).netloc if "//" in s or s.startswith("http") else s
    host = (host or s).split("@")[-1].split(":")[0].strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _path_for(host: str) -> Path:
    safe = host.replace("/", "_").replace("..", "_") or "unknown"
    return _BASE_DIR / f"{safe}.json"


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as exc:  # noqa: BLE001 - best-effort cache
        logger.warning(f"[PortalMemory] Could not load {path.name}: {exc}")
        return {}


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _dedup_prepend(existing: List[str], new: List[str], cap: int) -> List[str]:
    """Prepend `new` (most-recent-first), drop dups, cap length."""
    out: List[str] = []
    for s in list(new) + list(existing):
        s = (s or "").strip()
        if s and s not in out:
            out.append(s)
    return out[:cap]


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read `name` off an object OR a dict (actions may be either)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_working_selectors(actions: Optional[List[Any]]) -> Dict[str, List[str]]:
    """Pull the selectors that actually worked from a run's action list.

    Accepts a list of ``AgentAction`` objects or plain dicts. Only actions with
    ``ok`` truthy and a non-empty ``selector`` are kept, grouped by action kind.
    """
    by_kind: Dict[str, List[str]] = {}
    for a in actions or []:
        try:
            if not _attr(a, "ok", True):
                continue
            kind = str(_attr(a, "kind", "") or "").strip()
            sel = _attr(a, "selector")
            if kind in _SELECTOR_KINDS and isinstance(sel, str) and sel.strip():
                by_kind.setdefault(kind, [])
                if sel.strip() not in by_kind[kind]:
                    by_kind[kind].append(sel.strip())
        except Exception:  # noqa: BLE001 - never let one bad action break recall
            continue
    return by_kind


def extract_failed_selectors(actions: Optional[List[Any]]) -> Dict[str, List[str]]:
    """Pull the selectors the agent TRIED that FAILED — the dead-ends worth
    avoiding next time.

    A selector is only reported as an avoid-target if it failed (``ok`` falsy)
    AND never succeeded anywhere else in the SAME run (a selector that failed
    once then worked on retry is a transient, not a mistake). This is the
    signal that stops the agent repeating the same wrong move on every visit.
    """
    ok_selectors: set[str] = set()
    failed_by_kind: Dict[str, set] = {}
    for a in actions or []:
        try:
            kind = str(_attr(a, "kind", "") or "").strip()
            sel = _attr(a, "selector")
            if kind not in _SELECTOR_KINDS or not isinstance(sel, str) or not sel.strip():
                continue
            sel = sel.strip()
            if _attr(a, "ok", True):
                ok_selectors.add(sel)
            else:
                failed_by_kind.setdefault(kind, set()).add(sel)
        except Exception:  # noqa: BLE001 - never let one bad action break recall
            continue
    out: Dict[str, List[str]] = {}
    for kind, sels in failed_by_kind.items():
        keep = [s for s in sels if s not in ok_selectors]
        if keep:
            out[kind] = keep
    return out


def flow_of(actions: Optional[List[Any]]) -> List[str]:
    """Compact ordered list of action kinds that a run took."""
    flow: List[str] = []
    for a in actions or []:
        try:
            kind = str(_attr(a, "kind", "") or "").strip()
            if kind:
                flow.append(kind)
        except Exception:  # noqa: BLE001
            continue
    return flow[:_MAX_FLOW_STEPS]


def recall(url_or_host: str) -> Optional[Dict[str, Any]]:
    """Return the stored playbook for a host, or ``None`` if unknown/disabled."""
    if not _enabled():
        return None
    host = host_of(url_or_host)
    if not host:
        return None
    with _lock:
        data = _load(_path_for(host))
    return data or None


def record(
    url_or_host: str,
    *,
    outcome: str,
    actions: Optional[List[Any]] = None,
    ats: Optional[str] = None,
    login_required: Optional[bool] = None,
    captcha_types: Optional[List[str]] = None,
    success_signal: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    """Merge one run's learnings into this host's playbook. Best-effort.

    Called by the executor after every AgentLoop run — success OR failure — so
    the agent also learns "this host blocks with turnstile" or "login required".
    """
    if not _enabled():
        return
    host = host_of(url_or_host)
    if not host:
        return
    outcome = str(outcome or "UNKNOWN").strip()
    is_success = outcome in ("SUBMITTED", "CONFIRMED", "FORM_COMPLETED")
    try:
        with _lock:
            path = _path_for(host)
            pb = _load(path)
            meta = pb.get("_meta") or {}
            meta.update({
                "host": host,
                "updated": int(time.time()),
                "runs": int(meta.get("runs", 0)) + 1,
                "successes": int(meta.get("successes", 0)) + (1 if is_success else 0),
            })
            pb["_meta"] = meta

            if ats:
                pb["ats"] = ats
            if login_required is not None:
                pb["login_required"] = bool(login_required)
            if success_signal:
                pb["success_signal"] = str(success_signal)[:300]

            # Merge working selectors (only worth persisting on a run that got
            # somewhere — a fully-blocked run's clicks aren't a reliable flow).
            if actions and is_success:
                new_sel = extract_working_selectors(actions)
                merged = pb.get("working_selectors") or {}
                for kind, sels in new_sel.items():
                    merged[kind] = _dedup_prepend(
                        merged.get(kind, []), sels, _MAX_SELECTORS_PER_KIND
                    )
                pb["working_selectors"] = merged
                pb["flow"] = flow_of(actions)

            # Merge AVOID selectors (the dead-ends the agent tried that failed) —
            # recorded on EVERY run (success or not), because a run that
            # ultimately submitted still wasted steps on wrong moves we want the
            # next visit to skip. Also PRUNE anything now proven to work, so a
            # selector that failed once but later succeeds stops being avoided.
            if actions:
                avoid_new = extract_failed_selectors(actions)
                ok_now = extract_working_selectors(actions)
                ok_flat = {s for sels in ok_now.values() for s in sels}
                merged_avoid = pb.get("avoid_selectors") or {}
                for kind, sels in avoid_new.items():
                    merged_avoid[kind] = _dedup_prepend(
                        merged_avoid.get(kind, []), sels, _MAX_AVOID_PER_KIND
                    )
                # Prune proven-good selectors out of every avoid list.
                pruned = {
                    kind: [s for s in sels if s not in ok_flat]
                    for kind, sels in merged_avoid.items()
                }
                merged_avoid = {kind: sels for kind, sels in pruned.items() if sels}
                if merged_avoid:
                    pb["avoid_selectors"] = merged_avoid
                elif "avoid_selectors" in pb:
                    del pb["avoid_selectors"]

            if captcha_types:
                seen = pb.get("captcha_types") or []
                for c in captcha_types:
                    c = (c or "").strip()
                    if c and c not in seen:
                        seen.append(c)
                pb["captcha_types"] = seen[:8]

            if note:
                notes = pb.get("notes") or []
                note = str(note)[:200]
                if note not in notes:
                    notes.insert(0, note)
                pb["notes"] = notes[:_MAX_NOTES]

            pb["last_outcome"] = outcome
            _save(path, pb)
        logger.info(
            f"[PortalMemory] {host}: recorded outcome={outcome} "
            f"(runs={meta['runs']}, successes={meta['successes']})"
        )
    except Exception as exc:  # noqa: BLE001 - memory must never break a run
        logger.warning(f"[PortalMemory] record failed for {host!r}: {exc}")


def format_for_prompt(url_or_host: str) -> str:
    """Render the host's playbook as a compact block for the system prompt.

    Returns ``""`` when the host is unknown or memory is disabled, so the caller
    can concatenate unconditionally without changing behaviour for new portals.
    """
    pb = recall(url_or_host)
    if not pb:
        return ""
    host = (pb.get("_meta") or {}).get("host", host_of(url_or_host))
    meta = pb.get("_meta") or {}
    runs = meta.get("runs", 0)
    successes = meta.get("successes", 0)

    lines: List[str] = [
        "══════════════════════════════════════════════════════════════════════",
        f"SELF-LEARNED PLAYBOOK — {host}  "
        f"(from {runs} prior run(s), {successes} successful)",
        "══════════════════════════════════════════════════════════════════════",
        "This is what YOU learned on past visits to this exact portal. Prefer it,",
        "but the LIVE page is always authoritative — if it disagrees, trust the page.",
    ]
    if pb.get("login_required"):
        lines.append("  • Login: this portal REQUIRED a login before applying.")
    if pb.get("captcha_types"):
        lines.append(f"  • Captcha seen here: {', '.join(pb['captcha_types'])}.")
    sels = pb.get("working_selectors") or {}
    if sels:
        lines.append("  • USE-FIRST — selectors that WORKED here before (try these before hunting):")
        for kind, lst in sels.items():
            if lst:
                lines.append(f"      - {kind}: {lst[0]}")
    if pb.get("flow"):
        lines.append("  • Flow that worked: " + " → ".join(pb["flow"][:12]))
    avoid = pb.get("avoid_selectors") or {}
    if avoid:
        lines.append("  ❌ DO NOT REPEAT — these were tried here before and FAILED "
                     "(pick a different element/approach):")
        for kind, lst in avoid.items():
            for s in lst[:2]:
                lines.append(f"      - {kind}: {s}")
    if pb.get("success_signal"):
        lines.append(f"  • Success looked like: {pb['success_signal']}")
    if pb.get("last_outcome"):
        lines.append(f"  • Last outcome: {pb['last_outcome']}")
    for n in (pb.get("notes") or [])[:4]:
        lines.append(f"  • Note: {n}")
    return "\n" + "\n".join(lines) + "\n"
