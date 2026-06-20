"""Failure diagnoser — Phase 5 of the autonomous agent.

When the executor hits an uncoverable failure (e.g. submit didn't fire even
after vision retries, or a required field was unresolvable), we capture a
screenshot + DOM + log tail and ask Gemini to diagnose the root cause.

The output is split into two streams:

  * "selector_fix" — append a candidate selector to learned_fixes/<ats>.json
    so the NEXT run tries it first. Pure data, no code changes.

  * "code_patch"   — write a UNIFIED DIFF as a `.diff` file under
    learned_fixes/proposed_patches/<ats>_<ts>.diff together with a
    `.md` rationale file. We DO NOT apply the diff at runtime — the operator
    reviews and applies via `git apply` (or rejects it). This is the safe
    alternative to runtime auto-patching.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import Frame, Page

from ..llm import LLMUnavailable, get_gemini
from .learned_fixes import get_learned_fixes

logger = logging.getLogger(__name__)


_PATCH_DIR = Path(__file__).resolve().parents[1] / "learned_fixes" / "proposed_patches"


_DIAGNOSE_PROMPT = """
You are a senior browser-automation engineer reviewing a failed job-application
attempt. The bot tried to {action} but {failure_reason}. Decide whether this is
a SELECTOR_FIX (just a stale CSS selector) or a CODE_PATCH (the handler logic
itself is broken).

Return STRICT JSON:
  {{
    "kind": "SELECTOR_FIX" | "CODE_PATCH" | "ENVIRONMENTAL" | "GIVE_UP",
    "explanation": "<= 200 chars",
    "selector_channel": "apply_button"|"submit"|"resume_input"|... (only if SELECTOR_FIX),
    "selector": "<css>" (only if SELECTOR_FIX),
    "patch_target_file": "backend/app/browser_automation/adapters/<x>.py"
                         (only if CODE_PATCH),
    "patch_diff": "<unified diff>" (only if CODE_PATCH),
    "patch_rationale": "<= 400 chars" (only if CODE_PATCH)
  }}

Rules:
  * Prefer SELECTOR_FIX if the DOM clearly contains a working selector under a
    different attribute / class than what the bot tried.
  * Use CODE_PATCH only when the logic must change (e.g. handler skipped a
    multi-step modal, didn't wait for navigation, ignored an iframe).
  * ENVIRONMENTAL covers WAF blocks, IP bans, captcha walls — return that and
    we'll surface it to the operator.
  * GIVE_UP if you cannot determine cause from the evidence.
  * Never invent file paths. patch_target_file must be a real existing file.
  * Diff format: standard unified diff, --- a/...  +++ b/...  with @@ hunks.

DOM SNIPPET:
{dom}

ADAPTER SOURCE (for the file the bot is using):
{adapter_source}

RECENT LOG TAIL:
{logs}
"""


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + " …"


async def _capture(page: Page, frame: Optional[Frame] = None):
    ctx = frame or page
    try:
        screenshot = await page.screenshot(
            full_page=False, type="jpeg", quality=50,
            clip={"x": 0, "y": 0, "width": 1280, "height": 720},
        )
    except Exception:
        screenshot = None
    try:
        dom = _truncate(await ctx.content(), 5_000)
    except Exception:
        dom = ""
    return screenshot, dom


async def diagnose_failure(
    page: Optional[Page],
    ats: str,
    action: str,
    failure_reason: str,
    adapter_source_path: Optional[Path] = None,
    log_tail: str = "",
    frame: Optional[Frame] = None,
) -> Optional[dict]:
    """Capture context, ask Gemini, and persist learnings.

    Returns the parsed diagnosis dict, or None on LLM failure.
    """
    if page is None:
        return None

    screenshot, dom = await _capture(page, frame)

    adapter_source = ""
    if adapter_source_path and adapter_source_path.exists():
        try:
            adapter_source = adapter_source_path.read_text(encoding="utf-8")
            adapter_source = _truncate(adapter_source, 6_000)
        except Exception as exc:
            logger.warning(f"[Diagnoser] Could not read adapter source: {exc}")

    prompt = _DIAGNOSE_PROMPT.format(
        action=action,
        failure_reason=failure_reason[:200],
        dom=dom or "(empty)",
        adapter_source=adapter_source or "(unavailable)",
        logs=_truncate(log_tail or "(no logs)", 1_500),
    )

    try:
        diag = await get_gemini().generate_json(
            prompt=prompt, image_bytes=screenshot, temperature=0.1, timeout_s=45.0
        )
    except LLMUnavailable as exc:
        logger.info(f"[Diagnoser] LLM unavailable: {exc}")
        return None

    if not isinstance(diag, dict):
        return None

    kind = str(diag.get("kind") or "GIVE_UP").upper()
    logger.info(f"[Diagnoser] {ats}/{action} -> kind={kind} :: "
                f"{(diag.get('explanation') or '')[:200]}")

    if kind == "SELECTOR_FIX":
        channel = str(diag.get("selector_channel") or "")
        selector = str(diag.get("selector") or "")
        if channel and selector:
            try:
                get_learned_fixes(ats).add(channel, selector)
            except Exception as exc:
                logger.warning(f"[Diagnoser] Could not persist learned selector: {exc}")

    elif kind == "CODE_PATCH":
        try:
            _write_proposed_patch(ats, diag)
        except Exception as exc:
            logger.warning(f"[Diagnoser] Could not persist proposed patch: {exc}")

    return diag


def _write_proposed_patch(ats: str, diag: dict) -> None:
    _PATCH_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    base = _PATCH_DIR / f"{ats}_{ts}"

    diff = str(diag.get("patch_diff") or "").strip()
    target = str(diag.get("patch_target_file") or "").strip()
    rationale = str(diag.get("patch_rationale") or "").strip()

    if diff:
        (base.with_suffix(".diff")).write_text(diff, encoding="utf-8")
    meta = (
        f"# Proposed patch — {ats} ({ts})\n\n"
        f"**Target file:** `{target or 'unknown'}`\n\n"
        f"**Rationale:** {rationale or '(none)'}\n\n"
        f"## How to apply (after human review)\n\n"
        f"```bash\n"
        f"git apply {base.name}.diff\n"
        f"```\n\n"
        f"This patch was authored by Gemini. Review carefully before applying.\n"
    )
    (base.with_suffix(".md")).write_text(meta, encoding="utf-8")
    logger.info(f"[Diagnoser] Wrote proposed patch: {base.name}.diff (+ .md)")
