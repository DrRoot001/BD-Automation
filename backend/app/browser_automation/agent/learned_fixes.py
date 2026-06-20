"""Learned-selector cache.

On a selector miss we ask Gemini for a new selector candidate and append it
here. On subsequent runs the adapter loads this file at startup and tries the
learned selectors **before** its hardcoded list, so a one-time human-in-the-loop
fix becomes permanent for that ATS.

Storage layout::

    backend/app/browser_automation/learned_fixes/
        greenhouse.json     # { "apply_button": ["a.x", ...], "submit": [...] }
        lever.json
        ...
        proposed_patches/   # Gemini-authored diffs (Phase 5)

The file is human-editable JSON — the operator can curate selectors by hand
without touching code.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[1] / "learned_fixes"
_lock = threading.Lock()


def _path_for(ats: str) -> Path:
    safe = ats.strip().lower().replace("/", "_") or "unknown"
    return _BASE_DIR / f"{safe}.json"


class LearnedFixes:
    """Per-ATS selector cache.

    Keys are "channels" — short identifiers used by the adapter for a
    particular operation (e.g. "apply_button", "submit", "resume_input").
    Values are lists of CSS / Playwright selectors, ordered by recency.
    """

    def __init__(self, ats: str):
        self.ats = ats.strip().lower() or "unknown"
        self._data: Dict[str, List[str]] = {}
        self._loaded = False

    def _load(self) -> None:
        with _lock:
            path = _path_for(self.ats)
            if not path.exists():
                self._data = {}
                self._loaded = True
                return
            try:
                with path.open("r", encoding="utf-8") as f:
                    raw = json.load(f) or {}
                # Schema: {channel: [selectors] | {selectors:[...], last_used:..}}
                cleaned: Dict[str, List[str]] = {}
                for k, v in raw.items():
                    if isinstance(v, list):
                        cleaned[k] = [s for s in v if isinstance(s, str) and s.strip()]
                    elif isinstance(v, dict) and isinstance(v.get("selectors"), list):
                        cleaned[k] = [s for s in v["selectors"] if isinstance(s, str)]
                self._data = cleaned
            except Exception as exc:
                logger.warning(f"[LearnedFixes] Could not load {path.name}: {exc}")
                self._data = {}
            self._loaded = True

    def get(self, channel: str) -> List[str]:
        if not self._loaded:
            self._load()
        return list(self._data.get(channel, []))

    def add(self, channel: str, selector: str) -> None:
        """Prepend a newly-learned selector to the channel list (deduped)."""
        if not selector or not selector.strip():
            return
        if not self._loaded:
            self._load()
        current = [s for s in self._data.get(channel, []) if s != selector]
        current.insert(0, selector)
        # Cap at 10 to avoid unbounded growth on a flaky site
        self._data[channel] = current[:10]
        self._persist()
        logger.info(f"[LearnedFixes] {self.ats}.{channel} learned new selector: {selector!r}")

    def _persist(self) -> None:
        path = _path_for(self.ats)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_meta": {"ats": self.ats, "last_updated": int(time.time())},
        }
        payload.update(self._data)
        # Filter out the meta when reloading — but we WRITE it for human readers
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            tmp.replace(path)
        except Exception as exc:
            logger.warning(f"[LearnedFixes] Could not persist {path.name}: {exc}")


_cache: Dict[str, LearnedFixes] = {}


def get_learned_fixes(ats: str) -> LearnedFixes:
    key = (ats or "unknown").lower()
    if key not in _cache:
        _cache[key] = LearnedFixes(key)
    return _cache[key]
