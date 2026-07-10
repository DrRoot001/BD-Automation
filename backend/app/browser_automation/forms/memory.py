"""Field Memory — long-term learning across application runs.

Stores `(normalized_label → answer)` pairs from successful fills so future runs
can answer the same question instantly without re-running smart inference.

## Per-candidate isolation (Bug A fix)

Memory is now **namespaced by candidate_id** so values from candidate A never
leak into candidate B's run. Each candidate's identity-bearing answers (name,
email, phone, LinkedIn) live in their own file. A shared "global" namespace
still holds candidate-agnostic learnings (EEO defaults, "Decline to answer"
choices, generic Yes/No defaults).

Storage layout:
  data/field_memory/<candidate_id>.json    — per-candidate identity values
  data/field_memory/__global__.json        — candidate-agnostic learnings
  data/field_failures.json                 — unsolved fields (unchanged)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_MEMORY_DIR = _DATA_DIR / "field_memory"
_FAILURES_PATH = _DATA_DIR / "field_failures.json"
_lock = threading.Lock()

# Labels whose answer is identity-bearing — these must NEVER cross candidates.
# If the normalized label matches any of these patterns, the value is stored
# in the per-candidate namespace, not the global one. recall() for these
# patterns ONLY consults the per-candidate file.
_IDENTITY_PATTERNS = [
    re.compile(p) for p in (
        r"\b(first|last|middle|full|given|family|preferred)\s*name\b",
        r"\bname\b",
        r"\be-?mail\b",
        r"\bphone\b|\bmobile\b|\btelephone\b|\bcell\b",
        r"\blinked\s*in\b",
        r"\bcurrent\s+(job\s+title|company|employer)\b",
        r"\bwebsite\b|\bportfolio\b|\bgithub\b",
        r"\baddress\b",
        r"\blocation\b|\bcity\b|\bcountry\b",
        r"\bdate\s+of\s+birth\b|\bbirth\s*day\b",
        # Candidate-specific demographic / gender / race / orientation answers
        # are inferred from the candidate (e.g. gender from first name); they
        # MUST stay per-candidate so candidate A's "Male" doesn't get recalled
        # for candidate B who is female.
        r"\bgender\b|\bracial\b|\brace\b|\bethnic\b|\bsexual\s+orient",
        r"\btransgender\b|\bhispanic\b|\blatino\b",
        # Work authorization / sponsorship answers depend on the candidate's
        # country of residence — keep per-candidate too.
        r"\b(authoriz|authoris).+\bwork\b",
        r"\b(sponsor|visa)\b",
        # The canonical alias forms emitted by _normalize_label
        r"^current job title$",
        r"^current company / employer$",
    )
]


# Alias rules: collapse common verbose ATS phrasings to a canonical short
# form so memory recall hits across forms that ask the same question with
# different wording. Each rule is (regex, canonical_form). First match wins.
_ALIAS_RULES = [
    (re.compile(r"current\s+(?:or\s+(?:more|most)\s+recent\s+)?(?:job\s+)?title"),
     "current job title"),
    (re.compile(r"(?:current|most\s+recent|recent)\s+(?:or\s+(?:more|most)\s+recent\s+)?(?:company|employer|workplace)"),
     "current company / employer"),
    (re.compile(r"who\s+is\s+your\s+(?:current|most\s+recent|recent).+(?:employer|company)"),
     "current company / employer"),
    (re.compile(r"what\s+is\s+your\s+(?:current|most\s+recent|recent).+(?:title|role|position)"),
     "current job title"),
    # NOTE: we intentionally do NOT alias "authorized to work" or
    # "sponsorship/visa" questions to a single canonical key. Those answers are
    # COUNTRY-SPECIFIC — "authorized to work in the US?" → Yes, but
    # "authorized to work in the UK?" → No. Collapsing them to one key made a
    # single poisoned answer (a UK "No") get recalled for the US question via
    # memory pre-fill, silently filling "I am not authorized" and blocking
    # submit. Each work-auth / sponsorship question now keeps its full label as
    # its own key, so US and UK answers stay distinct.
]


def _normalize_label(label: str) -> str:
    """Lowercase, strip whitespace, remove *, collapse spaces, apply aliases."""
    s = (label or "").lower().strip()
    s = re.sub(r"[*​\xa0?]", "", s)
    s = re.sub(r"\s+", " ", s).strip(":;. ")
    for pat, canonical in _ALIAS_RULES:
        if pat.search(s):
            return canonical
    return s


def _is_identity_field(normalized_label: str) -> bool:
    return any(p.search(normalized_label) for p in _IDENTITY_PATTERNS)


def _safe_cid(candidate_id: Optional[str]) -> str:
    """Validate & sanitize candidate_id for use as a filename.

    Postgres UUIDs are safe by default; this is belt-and-suspenders against
    a caller accidentally passing path traversal characters.
    """
    if not candidate_id:
        return "__anonymous__"
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(candidate_id))
    return safe or "__anonymous__"


def _memory_path(candidate_id: Optional[str]) -> Path:
    if candidate_id:
        return _MEMORY_DIR / f"{_safe_cid(candidate_id)}.json"
    return _MEMORY_DIR / "__global__.json"


def _load(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"[Memory] Could not load {path.name}: {exc}")
        return {}


def _save(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
    tmp.replace(path)


def _ttl_seconds() -> int:
    """Recall staleness window in seconds. `FIELD_MEMORY_TTL_DAYS` env var
    (default 180) governs it; 0 (or negative) disables expiry entirely.

    Rationale: volatile answers (location, current company/title, "available
    to start" dates) go stale over months. Expiring an answer is cheap — the
    agent simply re-derives it from the current profile/inference on the next
    run — so a conservative but non-infinite default keeps memory honest
    without discarding freshly-learned answers during an active batch.
    """
    try:
        days = int(os.getenv("FIELD_MEMORY_TTL_DAYS", "180"))
    except (TypeError, ValueError):
        days = 180
    return days * 86400 if days > 0 else 0


def _is_stale(entry: Dict) -> bool:
    """True when `entry` is older than the configured TTL.

    Legacy records written before `last_seen` was stored (and any record when
    the TTL is disabled) are treated as fresh — we never discard an answer we
    can't date, to avoid wiping useful pre-existing memory on first upgrade.
    """
    ttl = _ttl_seconds()
    if ttl <= 0:
        return False
    last_seen = entry.get("last_seen")
    if not isinstance(last_seen, (int, float)) or last_seen <= 0:
        return False
    return (time.time() - last_seen) > ttl


def recall(
    label: str,
    field_type: str,
    options: Optional[List[str]] = None,
    candidate_id: Optional[str] = None,
) -> Optional[str]:
    """Look up a previously-learned answer for this label.

    Lookup order:
      1. The candidate's own memory file (always consulted first).
      2. The shared __global__.json — ONLY for non-identity fields, AND only
         when STRICT_MEMORY_ISOLATION is NOT enabled.

    Identity-bearing labels (name/email/phone/etc.) ALWAYS skip the global
    namespace. In addition, setting env var `STRICT_MEMORY_ISOLATION=true`
    disables ALL global recall — every answer must come from this
    candidate's own file. Use this when you're testing multiple candidates
    back-to-back and don't want ANY cross-candidate bleed (a referral
    source, a country preference, an acknowledgment text from candidate A
    being recalled into candidate B's run).

    Answers older than `FIELD_MEMORY_TTL_DAYS` (default 180) are ignored so
    stale locations/companies don't resurface — see `_is_stale`.
    """
    if not label:
        return None
    key = _normalize_label(label)
    is_identity = _is_identity_field(key)
    # Default ON: a per-candidate cache is always safe, but cross-candidate
    # global recall of free-text could leak one candidate's answer into
    # another's application. Opt OUT explicitly with STRICT_MEMORY_ISOLATION=false.
    strict = (os.getenv("STRICT_MEMORY_ISOLATION", "true").lower()
              not in ("0", "false", "no", "off"))

    with _lock:
        # Per-candidate first
        if candidate_id:
            mem = _load(_memory_path(candidate_id))
            entry = mem.get(key)
            if entry and not _is_stale(entry):
                ans = entry.get("value")
                if ans and _options_ok(ans, options):
                    logger.info(
                        f"[Memory] Recalled '{ans}' for '{label}' (cand={_safe_cid(candidate_id)[:8]}, seen {entry.get('count',1)}x)"
                    )
                    return ans
        # Fall back to global ONLY for non-identity fields AND only when
        # strict isolation is off. The strict mode is a hard guarantee that
        # nothing from another candidate's session can re-surface here.
        if not is_identity and not strict:
            mem = _load(_memory_path(None))
            entry = mem.get(key)
            if entry and not _is_stale(entry):
                ans = entry.get("value")
                if ans and _options_ok(ans, options):
                    logger.info(f"[Memory] Recalled '{ans}' for '{label}' (global, seen {entry.get('count',1)}x)")
                    return ans
    return None


def _options_ok(answer: str, options: Optional[List[str]]) -> bool:
    if not options:
        return True
    opts_lower = [o.lower().strip() for o in options]
    return answer.lower().strip() in opts_lower


def remember(
    label: str,
    field_type: str,
    value: str,
    source: str = "fill",
    candidate_id: Optional[str] = None,
) -> None:
    """Record a successful fill so future runs can reuse it.

    Identity-bearing answers persist ONLY to the per-candidate file.
    Non-identity answers persist to the per-candidate file (so the same
    candidate gets a fast-path on repeat runs) AND to the global file (so
    other candidates benefit from learnings about Yes/No EEO defaults etc).
    """
    if not label or not value:
        return
    key = _normalize_label(label)
    is_identity = _is_identity_field(key)
    # Only CONSTRAINED answers (a fixed option set) are safe to reuse verbatim.
    _constrained = field_type in ("select", "radio", "checkbox", "boolean")
    # Free-text, NON-identity answers are job-specific (e.g. "why do you want to
    # work here", cover-letter prose, salary, referral, start date). Reusing them
    # on a different posting is the cross-application poisoning the audit flagged,
    # so we never persist them anywhere — the LLM answers those fresh each time.
    if not is_identity and not _constrained:
        logger.debug(f"[Memory] Not memorizing job-specific free-text field '{label}'")
        return
    paths = [_memory_path(candidate_id)] if candidate_id else []
    # The shared cross-candidate global file gets ONLY candidate-agnostic
    # constrained answers (Yes/No, EEO decline, work-auth booleans) — never
    # free-text and never identity.
    if not is_identity and _constrained:
        paths.append(_memory_path(None))
    if not paths:
        paths = [_memory_path(None)]  # last-resort fallback
    with _lock:
        for path in paths:
            mem = _load(path)
            prior = mem.get(key, {})
            mem[key] = {
                "label": label,
                "field_type": field_type,
                "value": value,
                "count": int(prior.get("count", 0)) + 1,
                "last_seen": int(time.time()),
                "source": source,
            }
            try:
                _save(path, mem)
            except Exception as exc:
                logger.warning(f"[Memory] Could not persist memory to {path.name}: {exc}")


def record_failure(
    label: str,
    field_type: str,
    options: Optional[List[str]],
    tried_value: Optional[str],
    reason: str,
    candidate_id: Optional[str] = None,
) -> None:
    """Record a field we could not fill so it can be reviewed manually."""
    if not label:
        return
    key = _normalize_label(label)
    with _lock:
        fails = _load(_FAILURES_PATH)
        entry = fails.get(key, {"label": label, "occurrences": []})
        entry["occurrences"].append({
            "ts": int(time.time()),
            "candidate_id": _safe_cid(candidate_id) if candidate_id else None,
            "field_type": field_type,
            "options": options,
            "tried_value": tried_value,
            "reason": reason,
        })
        entry["occurrences"] = entry["occurrences"][-20:]
        entry["count"] = len(entry["occurrences"])
        fails[key] = entry
        try:
            _save(_FAILURES_PATH, fails)
        except Exception as exc:
            logger.warning(f"[Memory] Could not persist failure: {exc}")


def all_memories(candidate_id: Optional[str] = None) -> Dict:
    """Return the memory dict for the given candidate (or global if None)."""
    with _lock:
        return _load(_memory_path(candidate_id))


# ─────────────────────────────────────────────────────────────────────────────
# One-time migration: if the old monolithic field_memory.json exists, split
# its identity values into __anonymous__.json (we don't know which candidate
# they belonged to) and its non-identity values into __global__.json. This
# preserves prior learnings without leaking identity across candidates.
# ─────────────────────────────────────────────────────────────────────────────
def _migrate_legacy_memory() -> None:
    legacy = _DATA_DIR / "field_memory.json"
    if not legacy.exists():
        return
    try:
        with legacy.open("r", encoding="utf-8") as f:
            data = json.load(f)
        identity, generic = {}, {}
        for k, v in data.items():
            if _is_identity_field(k):
                identity[k] = v
            else:
                generic[k] = v
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if identity:
            id_path = _MEMORY_DIR / "__anonymous__.json"
            existing = _load(id_path)
            existing.update(identity)
            _save(id_path, existing)
        if generic:
            g_path = _MEMORY_DIR / "__global__.json"
            existing = _load(g_path)
            existing.update(generic)
            _save(g_path, existing)
        # Rename the legacy file so we don't re-migrate every import.
        legacy.rename(legacy.with_suffix(".json.migrated"))
        logger.info(
            f"[Memory] Migrated legacy field_memory.json: "
            f"{len(identity)} identity → __anonymous__.json, "
            f"{len(generic)} generic → __global__.json"
        )
    except Exception as exc:
        logger.warning(f"[Memory] Legacy migration skipped: {exc}")


_migrate_legacy_memory()
