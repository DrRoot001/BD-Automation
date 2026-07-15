"""AutonomousAgent — the perception-driven observe → reason → act loop.

This replaces the old ``act → assume next step → act`` script with a strict:

    OBSERVE → REASON → ACT → OBSERVE → REASON → ACT → …

cycle. On every turn the agent COLLECTS the live browser state (perception),
DETECTS blocking/terminal conditions from that state (deterministic) AND asks
the reasoner (LLM) for the next action justified by evidence, then EXECUTES a
single action — and immediately loops back to OBSERVE to judge the effect. It
never assumes an action worked or that "submit means success".

Guarantees mapped to the requirements:
  • observe before every action — the loop head collects state before reasoning.
  • observe after every action  — the executed action's effect is judged by the
    NEXT iteration's observe (and the loop always re-observes after ACT).
  • never assume workflow progression — no fixed step order; the reasoner picks
    the next action from what is on screen, and SUBMIT is not terminal.
  • detects (from observed evidence, deterministic + reasoner-merged):
    unexpected UI, validation failures, successful completion, navigation
    failures, OTP, email verification, MFA, duplicate accounts, required fields,
    captcha.

The reasoner (``engine``) and the two optional handlers are injectable, so the
whole loop is testable end-to-end against local fixtures without an LLM.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from ..perception import BrowserState, BrowserStateCollector
from ..reasoning import ConversationMemory
from ..reasoning.models import ActionType
from .action_executor import ActionExecutor
from .conditions import detect_conditions, merge_reasoning_signals
from .models import AgentRunResult, AgentStatus, DetectedConditions
from .recovery import RecoveryController

logger = logging.getLogger(__name__)

# handler signatures
VerificationHandler = Callable[[Any, Any, BrowserState, DetectedConditions], Awaitable[bool]]
CaptchaHandler = Callable[[Any, Any, BrowserState], Awaitable[bool]]

_MUTATING = {
    ActionType.CLICK, ActionType.CLICK_APPLY, ActionType.FILL,
    ActionType.SELECT_OPTION, ActionType.UPLOAD, ActionType.NEXT_STEP,
    ActionType.NAVIGATE, ActionType.SUBMIT,
}


class AutonomousAgent:
    def __init__(
        self,
        engine: Any = None,
        collector: Optional[BrowserStateCollector] = None,
        executor: Optional[ActionExecutor] = None,
        *,
        max_steps: int = 40,
        stuck_threshold: int = 3,
        invalid_threshold: int = 3,
        verification_handler: Optional[VerificationHandler] = None,
        captcha_handler: Optional[CaptchaHandler] = None,
        enable_recovery: bool = True,
    ):
        self._engine = engine
        self.collector = collector or BrowserStateCollector()
        self.executor = executor or ActionExecutor()
        self.max_steps = max_steps
        self.stuck_threshold = stuck_threshold
        self.invalid_threshold = invalid_threshold
        self.verification_handler = verification_handler
        self.captcha_handler = captcha_handler
        self.enable_recovery = enable_recovery

    @property
    def engine(self) -> Any:
        if self._engine is None:
            from ..reasoning import DecisionEngine
            self._engine = DecisionEngine()
        return self._engine

    # ─────────────────────────────────────────────────────────────────────────
    async def run(
        self,
        page: Any,
        objective: str,
        *,
        frame: Optional[Any] = None,
        browser_memory: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
        stop_before_submit: bool = False,
        verification_handler: Optional[VerificationHandler] = None,
        captcha_handler: Optional[CaptchaHandler] = None,
    ) -> AgentRunResult:
        """Drive the page autonomously toward ``objective``.

        ``verification_handler`` / ``captcha_handler`` override the ones passed at
        construction for THIS run (adapters bind them per-application once the
        candidate id is known)."""
        if verification_handler is not None:
            self.verification_handler = verification_handler
        if captcha_handler is not None:
            self.captcha_handler = captcha_handler
        result = AgentRunResult(status=AgentStatus.MAX_STEPS)
        conversation = ConversationMemory(objective=objective)
        context = context or {}
        recovery = RecoveryController() if self.enable_recovery else None

        # ── Popup / new-tab adoption (Apply buttons that target=_blank) ──────
        # In iframe mode the loop is bound to `frame` (in the original page), so
        # switching pages would desync perception — ignore popups there.
        popup_state: Dict[str, Any] = {"pending": None}
        if frame is None:
            try:
                page.context.on("page", lambda p: popup_state.__setitem__("pending", p))
            except Exception:
                pass

        prev_fp: Optional[str] = None
        last_mutating = False
        stuck = 0
        invalid = 0
        # Per-target failure ledger: stops the reasoner fixating on an action
        # that keeps failing (e.g. uploading an OPTIONAL cover-letter field with
        # no file). After a few failures we stop executing it and tell the
        # reasoner explicitly so it moves on instead of looping to STUCK.
        fail_counts: Dict[str, int] = {}
        _REPEAT_FAIL_CAP = 3

        for step in range(1, self.max_steps + 1):
            result.steps_taken = step

            # ── RECOVERY: adopt a newly-opened tab before we perceive ────────
            page = await self._maybe_adopt_popup(page, frame, popup_state, result, step)

            # ── OBSERVE (before action; also the observe-after of last action) ──
            state = await self.collector.collect(page, frame)
            turn_cond = detect_conditions(state)
            result.conditions.merge(turn_cond)

            # ── stuck detection: a mutating action last turn changed nothing ──
            fp = self._fingerprint(state)
            if last_mutating and prev_fp is not None and fp == prev_fp:
                stuck += 1
                if stuck >= self.stuck_threshold:
                    # RECOVERY: before giving up, try to unstick the page.
                    if recovery and recovery.can_recover_stall() and \
                            await recovery.recover_from_stall(page, frame, state):
                        self._record(result, step, state, "RECOVER", "stall recovery nudge", None, turn_cond)
                        stuck = 0
                        prev_fp = None   # force a fresh perceive next turn
                        last_mutating = False
                        continue
                    result.status = AgentStatus.STUCK
                    result.error = "page stopped changing after mutating actions"
                    self._record(result, step, state, "STUCK", "no DOM change", None, turn_cond)
                    break
            else:
                stuck = 0
                if recovery:
                    recovery.note_progress()
            prev_fp = fp
            last_mutating = False

            # ── RECOVERY: clear cookie/consent overlays + blocking modals ────
            if recovery and (turn_cond.unexpected_ui or state.modals) and recovery.can_dismiss():
                what = await recovery.dismiss_blocking_ui(page, frame, state)
                if what:
                    self._record(result, step, state, "RECOVER", f"dismissed {what}", None, turn_cond)
                    continue  # re-observe the now-unblocked page

            # ── RECOVERY: slow-loading / still-hydrating page → wait, re-observe
            if recovery and recovery.can_wait_for_load() and recovery.page_looks_unready(state) \
                    and not turn_cond.navigation_failed:
                await recovery.wait_for_load(page)
                self._record(result, step, state, "RECOVER", "waiting for slow load", None, turn_cond)
                continue

            # ── evidence-based terminal / blocker checks (deterministic) ──
            if turn_cond.completed_successfully:
                result.status = AgentStatus.SUBMITTED
                result.confirmation = turn_cond.success_evidence
                self._record(result, step, state, "OBSERVE", "success confirmed", None, turn_cond)
                break
            if turn_cond.navigation_failed:
                result.status = AgentStatus.NAVIGATION_FAILED
                result.error = turn_cond.navigation_evidence
                self._record(result, step, state, "OBSERVE", "navigation failed", None, turn_cond)
                break
            verdict = await self._check_blockers(page, frame, state, turn_cond, result)
            if verdict == "halt":
                self._record(result, step, state, "OBSERVE", f"blocked:{result.status.value}", None, turn_cond)
                break
            if verdict == "handled":
                self._record(result, step, state, "HANDLE", "cleared a wall", None, turn_cond)
                continue

            # ── REASON ──
            reasoning = await self.engine.decide(
                state, objective,
                browser_memory=browser_memory,
                conversation=conversation,
                step=step, max_steps=self.max_steps,
            )
            merge_reasoning_signals(turn_cond, reasoning)
            result.conditions.merge(turn_cond)

            # Reasoner may catch a wall the detector missed.
            if turn_cond.completed_successfully and reasoning.action.type != ActionType.DONE:
                result.status = AgentStatus.SUBMITTED
                result.confirmation = turn_cond.success_evidence
                self._record(result, step, state, "OBSERVE", "success (reasoner)", None, turn_cond)
                break
            verdict = await self._check_blockers(page, frame, state, turn_cond, result)
            if verdict == "halt":
                self._record(result, step, state, reasoning.action.type.value,
                             f"blocked:{result.status.value}", None, turn_cond)
                break
            if verdict == "handled":
                self._record(result, step, state, "HANDLE", "cleared a wall", None, turn_cond)
                continue

            action = reasoning.action

            # ── honor terminal / special reasoned actions ──
            if action.type == ActionType.ABORT:
                result.status = AgentStatus.ABORTED
                result.error = action.reason or "aborted by reasoner"
                self._record(result, step, state, "ABORT", reasoning.why, None, turn_cond)
                break
            if action.type == ActionType.DONE:
                if reasoning.valid:   # grounding requires a real success confirmation
                    result.status = AgentStatus.SUBMITTED
                    result.confirmation = action.confirmation or turn_cond.success_evidence or "confirmed"
                    self._record(result, step, state, "DONE", reasoning.why, None, turn_cond)
                    break
                invalid += 1
                self._record(result, step, state, "DONE?", "DONE without confirmation — re-observing", None, turn_cond)
                if invalid >= self.invalid_threshold:
                    result.status = AgentStatus.ERROR
                    result.error = "reasoner claimed DONE without observable confirmation"
                    break
                continue
            if action.type == ActionType.HANDLE_VERIFICATION:
                handled = await self._try_verification(page, frame, state, turn_cond)
                if handled:
                    self._record(result, step, state, "HANDLE_VERIFICATION", "handled", None, turn_cond)
                    continue
                result.status = (
                    AgentStatus.OTP_REQUIRED if turn_cond.otp_requested
                    else AgentStatus.EMAIL_VERIFICATION_REQUIRED
                )
                result.error = "verification requested but no handler available"
                self._record(result, step, state, "HANDLE_VERIFICATION", "no handler", None, turn_cond)
                break
            if action.type == ActionType.SOLVE_CAPTCHA:
                handled = await self._try_captcha(page, frame, state)
                if handled:
                    self._record(result, step, state, "SOLVE_CAPTCHA", "solved", None, turn_cond)
                    continue
                result.status = AgentStatus.CAPTCHA_REQUIRED
                result.error = "captcha present but no solver handler available"
                self._record(result, step, state, "SOLVE_CAPTCHA", "no handler", None, turn_cond)
                break

            # ── validity guard: never execute an ungrounded / malformed action ──
            if not reasoning.valid:
                invalid += 1
                self._record(result, step, state, action.type.value,
                             "invalid reasoning — not executed: " + "; ".join(reasoning.warnings[:2]),
                             None, turn_cond)
                if invalid >= self.invalid_threshold:
                    result.status = AgentStatus.ERROR
                    result.error = "repeated invalid/ungrounded reasoning: " + "; ".join(reasoning.warnings[:3])
                    break
                continue
            invalid = 0

            # dry-run: stop right before the real submit click
            if action.type == ActionType.SUBMIT and stop_before_submit:
                result.status = AgentStatus.FORM_COMPLETED
                result.confirmation = "stopped_before_submit"
                self._record(result, step, state, "SUBMIT", "dry-run: not clicked", None, turn_cond)
                break

            # ── Dead-action guard: stop repeating an action that keeps failing ─
            tgt = f"{action.type.value}:{(action.selector or action.value or '')[:60]}"
            if fail_counts.get(tgt, 0) >= _REPEAT_FAIL_CAP:
                conversation.set_last_outcome(
                    f"REPEATEDLY FAILED ({fail_counts[tgt]}x) and was NOT executed again — "
                    "this target is not working; do NOT retry it. If the field is optional "
                    "(not marked required), skip it and move on / submit."
                )
                self._record(result, step, state, action.type.value,
                             f"skipped repeatedly-failing action ({fail_counts[tgt]}x)", None, turn_cond)
                if action.type in _MUTATING:
                    last_mutating = True  # let the stall detector make progress toward abort
                continue

            # ── ACT ──
            ares = await self.executor.execute(page, frame, action, context)
            # ── RECOVERY: a failed action (missing element / timeout / covered
            # target) → dismiss overlays + scroll-to-reveal + bounded retry,
            # instead of losing the turn.
            if recovery and not ares.ok:
                retried = await recovery.recover_action(
                    page, frame, state, action, self.executor, context
                )
                if retried is not None:
                    if retried.ok:
                        self._record(result, step, state, "RECOVER",
                                     f"retried {action.type.value} -> ok", None, turn_cond)
                    ares = retried
            self._record(result, step, state, action.type.value, reasoning.why, ares, turn_cond)

            # ── Feed the ACTUAL outcome back to the reasoner so it learns from
            # failures (the reasoner's own memory otherwise only reflects its
            # guesses). This is what stops the "keep trying the same broken
            # action" loop.
            if not ares.ok:
                fail_counts[tgt] = fail_counts.get(tgt, 0) + 1
                conversation.set_last_outcome(
                    f"action FAILED: {ares.note or 'no effect'}. If this field is "
                    "optional or unfillable, do NOT repeat — move to the next field."
                )
            else:
                conversation.set_last_outcome("action succeeded")

            if action.type in _MUTATING:
                last_mutating = True
            # (the OBSERVE-after happens at the top of the next iteration)

        logger.info(f"[AutonomousAgent] {result.summary()}")
        return result

    # ─────────────────────────────────────────────────────────────────────────
    async def _check_blockers(self, page, frame, state, cond: DetectedConditions, result: AgentRunResult) -> str:
        """Return '' (none), 'halt' (result set — stop), or 'handled' (continue).
        Precedence: duplicate → MFA → captcha → OTP/email. Terminal walls without
        a handler halt the run with a specific status; a handler may clear it."""
        if cond.duplicate_account:
            result.status = AgentStatus.DUPLICATE_ACCOUNT
            result.error = cond.duplicate_evidence or "duplicate account / already applied"
            return "halt"
        if cond.mfa_requested:
            result.status = AgentStatus.MFA_REQUIRED
            result.error = cond.mfa_evidence or "MFA/2FA challenge"
            return "halt"
        if cond.captcha_present:
            if await self._try_captcha(page, frame, state):
                return "handled"
            result.status = AgentStatus.CAPTCHA_REQUIRED
            result.error = cond.captcha_evidence or "captcha present"
            return "halt"
        if cond.otp_requested or cond.email_verification_requested:
            if await self._try_verification(page, frame, state, cond):
                return "handled"
            if cond.otp_requested:
                result.status = AgentStatus.OTP_REQUIRED
                result.error = cond.otp_evidence or "OTP requested"
            else:
                result.status = AgentStatus.EMAIL_VERIFICATION_REQUIRED
                result.error = cond.email_verification_evidence or "email verification requested"
            return "halt"
        return ""

    async def _maybe_adopt_popup(self, page, frame, popup_state, result, step):
        """If a click opened the real application in a NEW tab, switch to it so
        perception + actions target the right page. Returns the page to use."""
        if frame is not None:
            return page  # iframe-scoped: never switch pages
        new_pg = popup_state.get("pending")
        if new_pg is None:
            return page
        popup_state["pending"] = None
        try:
            if new_pg.is_closed():
                return page
            try:
                await new_pg.wait_for_load_state("domcontentloaded", timeout=6_000)
            except Exception:
                pass
            url = new_pg.url or ""
            if not url or url == "about:blank":
                return page
            logger.info(f"[AutonomousAgent] adopting new tab: {url}")
            self._record(result, step, None, "RECOVER", f"switched to new tab {url[:80]}", None, None)
            try:
                await new_pg.bring_to_front()
            except Exception:
                pass
            return new_pg
        except Exception as exc:
            logger.debug(f"[AutonomousAgent] popup adopt failed: {exc}")
            return page

    async def _try_verification(self, page, frame, state, cond) -> bool:
        if not self.verification_handler:
            return False
        try:
            return bool(await self.verification_handler(page, frame, state, cond))
        except Exception as exc:
            logger.warning(f"[AutonomousAgent] verification handler raised: {exc}")
            return False

    async def _try_captcha(self, page, frame, state) -> bool:
        if not self.captcha_handler:
            return False
        try:
            return bool(await self.captcha_handler(page, frame, state))
        except Exception as exc:
            logger.warning(f"[AutonomousAgent] captcha handler raised: {exc}")
            return False

    @staticmethod
    def _fingerprint(state: BrowserState) -> str:
        parts = [state.url]
        for i in state.inputs:
            parts.append(f"{i.selector}={i.value}:{i.checked}")
        parts.append("btn:" + ",".join(b.selector for b in state.buttons))
        parts.append("msg:" + "|".join(m.text for m in state.messages))
        return hashlib.sha1("§".join(parts).encode("utf-8", "ignore")).hexdigest()[:16]

    @staticmethod
    def _record(result, step, state, action, note, ares, cond) -> None:
        result.history.append({
            "step": step,
            "url": (state.url if state else ""),
            "action": action,
            "note": (note or "")[:200],
            "action_ok": (ares.ok if ares else None),
            "action_note": (ares.note if ares else None),
            "required_fields": (len(cond.required_fields) if cond else 0),
            "detected": [
                k for k, v in (
                    ("validation", cond.validation_failed),
                    ("otp", cond.otp_requested),
                    ("email_verify", cond.email_verification_requested),
                    ("mfa", cond.mfa_requested),
                    ("duplicate", cond.duplicate_account),
                    ("captcha", cond.captcha_present),
                    ("unexpected_ui", cond.unexpected_ui),
                    ("nav_failed", cond.navigation_failed),
                    ("success", cond.completed_successfully),
                ) if v
            ] if cond else [],
        })
