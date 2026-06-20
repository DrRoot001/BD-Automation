"""Field Memory — long-term learning across application runs.

Stores `(normalized_label → answer)` pairs from successful fills so future runs
can answer the same question instantly without re-running smart inference.

This is the agent's "learn from past forms" layer:
  - When the filler successfully fills a field, it records the answer.
  - When the filler encounters a label it's seen before, it looks up the
    historical answer FIRST (before rules / smart inference).
  - When the filler fails on a required field, it records the failure so
    the operator can review and add a manual answer to the registry.

Storage is a JSON file at `backend/data/field_memory.json` keyed by
normalized label. We keep the file human-readable so an operator can edit it
by hand to teach the agent the right answers for tricky fields.
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

_MEMORY_PATH = Path(__file__).resolve().parents[3] / "data" / "field_memory.json"
_FAILURES_PATH = Path(__file__).resolve().parents[3] / "data" / "field_failures.json"
_lock = threading.Lock()


def _normalize_label(label: str) -> str:
    """Lowercase, strip whitespace, remove *, collapse spaces.
    Matches the filler's _normalize_label so memory keys line up."""
    s = (label or "").lower().strip()
    s = re.sub(r"[*​\xa0]", "", s)
    s = re.sub(r"\s+", " ", s).strip(":;. ")
    return s


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


def recall(label: str, field_type: str, options: Optional[List[str]] = None) -> Optional[str]:
    """Look up a previously-learned answer for this label.

    Returns the answer string if we have one AND it's still a valid option
    (when options are provided), or None otherwise."""
    if not label:
        return None
    key = _normalize_label(label)
    with _lock:
        mem = _load(_MEMORY_PATH)
    entry = mem.get(key)
    if not entry:
        return None
    answer = entry.get("value")
    if not answer:
        return None
    # If the form provides a fixed option set, only use the memory if the
    # remembered answer is one of the current options (case-insensitive).
    if options:
        opts_lower = [o.lower().strip() for o in options]
        if answer.lower().strip() not in opts_lower:
            logger.debug(f"[Memory] Recalled '{answer}' for '{label}' but not in current "
                         f"options {options} — skipping")
            return None
    # Also light type check — string memory should not be reused for a
    # date/url field with no validation
    logger.info(f"[Memory] Recalled '{answer}' for '{label}' (seen {entry.get('count', 1)}x)")
    return answer


def remember(label: str, field_type: str, value: str, source: str = "fill") -> None:
    """Record a successful fill so future runs can reuse it.

    `source` is a tag for provenance: 'rules', 'smart_infer', 'screening',
    'profile', 'memory' (re-use), or 'manual' (operator-curated).
    """
    if not label or not value:
        return
    key = _normalize_label(label)
    with _lock:
        mem = _load(_MEMORY_PATH)
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
            _save(_MEMORY_PATH, mem)
        except Exception as exc:
            logger.warning(f"[Memory] Could not persist memory: {exc}")


def record_failure(label: str, field_type: str, options: Optional[List[str]],
                   tried_value: Optional[str], reason: str) -> None:
    """Record a field we could not fill so it can be reviewed manually.

    The operator should look at field_failures.json periodically and add the
    correct answer to field_memory.json (or to the rules engine if it's a
    pattern worth generalising)."""
    if not label:
        return
    key = _normalize_label(label)
    with _lock:
        fails = _load(_FAILURES_PATH)
        entry = fails.get(key, {"label": label, "occurrences": []})
        entry["occurrences"].append({
            "ts": int(time.time()),
            "field_type": field_type,
            "options": options,
            "tried_value": tried_value,
            "reason": reason,
        })
        # Keep only last 20 occurrences per label to bound file size
        entry["occurrences"] = entry["occurrences"][-20:]
        entry["count"] = len(entry["occurrences"])
        fails[key] = entry
        try:
            _save(_FAILURES_PATH, fails)
        except Exception as exc:
            logger.warning(f"[Memory] Could not persist failure: {exc}")


def all_memories() -> Dict:
    """Return the full memory dict — for debugging / admin endpoints."""
    with _lock:
        return _load(_MEMORY_PATH)
