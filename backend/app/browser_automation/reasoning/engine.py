"""DecisionEngine — evidence-grounded reasoning over a BrowserState.

The engine is the replacement for scattered assumption-based rules. Given the
current :class:`~..perception.BrowserState` (screenshot + structured DOM), the
objective, and memory, it:

  1. builds the perception prompt (:mod:`.prompt`);
  2. calls the LLM for one structured reasoning turn;
  3. parses + structurally validates the output (:class:`ReasoningOutput`);
  4. **grounds** the output against what was actually observed — a chosen
     selector that does not appear anywhere in the captured state is flagged as
     a possible hallucination, and unsupported signal claims are noted;
  5. records the turn into conversation memory so the next turn can answer
     "what changed?" / "did the previous action succeed?".

It never raises for an LLM/parse failure: it returns a safe OBSERVE decision
with ``valid=False`` and ``parse_error`` set, so the caller (a future loop) can
retry or back off rather than crash.

The LLM client is injectable for testing — pass any object exposing
``async generate_json(prompt, image_bytes, temperature, timeout_s, system)``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Set

from ..perception import BrowserState
from .memory import BrowserMemory, ConversationMemory, ConversationTurn
from .models import ActionType, ReasoningOutput
from .prompt import SYSTEM_PROMPT, build_user_turn

logger = logging.getLogger(__name__)

# Selectors that are semantic/text-based rather than structural — we can't (and
# shouldn't) ground these against captured element selectors.
_SEMANTIC_SELECTOR_RE = re.compile(r":has-text\(|:has\(|text=|>>|:visible")


class DecisionEngine:
    def __init__(
        self,
        llm_client: Any = None,
        *,
        temperature: float = 0.0,
        timeout_s: float = 30.0,
        strict_grounding: bool = True,
    ):
        """
        Args:
            llm_client: object with ``async generate_json(...)``. Defaults to the
                shared ``get_llm()`` singleton (resolved lazily so importing this
                module never constructs a client).
            temperature: sampling temperature (0.0 — reasoning should be stable).
            timeout_s: per-call LLM timeout.
            strict_grounding: when True, a concrete (id/attribute) action selector
                that is absent from the observed state marks the decision invalid.
        """
        self._client = llm_client
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.strict_grounding = strict_grounding

    def _llm(self) -> Any:
        if self._client is None:
            from ..llm import get_llm
            self._client = get_llm()
        return self._client

    # ─────────────────────────────────────────────────────────────────────────
    async def decide(
        self,
        state: BrowserState,
        objective: str,
        *,
        browser_memory: Optional[BrowserMemory] = None,
        conversation: Optional[ConversationMemory] = None,
        step: Optional[int] = None,
        max_steps: Optional[int] = None,
        screenshot: Optional[bytes] = None,
    ) -> ReasoningOutput:
        """Produce one grounded :class:`ReasoningOutput` for the current state."""
        from ..llm import LLMUnavailable

        user_msg = build_user_turn(
            state, objective,
            browser_memory=browser_memory,
            conversation=conversation,
            step=step, max_steps=max_steps,
        )
        image = screenshot if screenshot is not None else state.screenshot

        try:
            raw = await self._llm().generate_json(
                prompt=user_msg,
                image_bytes=image,
                temperature=self.temperature,
                timeout_s=self.timeout_s,
                system=SYSTEM_PROMPT,
            )
        except LLMUnavailable as exc:
            logger.info(f"[Reasoning] LLM unavailable: {exc}")
            return self._fallback(f"llm_unavailable:{exc}", conversation, step)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[Reasoning] LLM call failed: {exc}")
            return self._fallback(f"llm_error:{exc}", conversation, step)

        output = ReasoningOutput.from_raw(raw)
        if output.parse_error:
            logger.warning(f"[Reasoning] parse error: {output.parse_error}")

        # Ground the decision against what was actually observed.
        self._add_grounding_warnings(output, state)

        # Record the turn so the NEXT decision can reason about it.
        self._record_turn(output, state, conversation, step)

        logger.debug(f"[Reasoning] {output.summary()}")
        return output

    # ─────────────────────────────────────────────────────────────────────────
    # Grounding
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _known_selectors(state: BrowserState) -> Set[str]:
        known: Set[str] = set()
        for i in state.inputs:
            if i.selector:
                known.add(i.selector)
        for b in state.buttons:
            if b.selector:
                known.add(b.selector)
        for f in state.forms:
            if f.selector:
                known.add(f.selector)
            known.update(f.field_selectors or [])
        for m in state.modals:
            if m.selector:
                known.add(m.selector)
            if m.close_selector:
                known.add(m.close_selector)
        for msg in state.messages:
            if msg.selector:
                known.add(msg.selector)
        return known

    def _is_grounded(self, selector: str, known: Set[str]) -> bool:
        if not selector:
            return True
        if _SEMANTIC_SELECTOR_RE.search(selector):
            return True  # text/semantic selectors can't be matched to captured ids
        if selector in known:
            return True
        # Loose containment both ways (model may append/trim a suffix like .first).
        for k in known:
            if selector in k or k in selector:
                return True
        return False

    def _add_grounding_warnings(self, output: ReasoningOutput, state: BrowserState) -> None:
        a = output.action
        known = self._known_selectors(state)

        # 1. Selector hallucination check for element-targeting actions.
        if a.type in (
            ActionType.FILL, ActionType.SELECT_OPTION, ActionType.UPLOAD,
            ActionType.CLICK, ActionType.CLICK_APPLY, ActionType.SUBMIT,
        ) and a.selector:
            if not self._is_grounded(a.selector, known):
                output.warnings.append(
                    f"action selector {a.selector!r} not found in observed page "
                    "state (possible hallucination)"
                )
                if self.strict_grounding:
                    output.valid = False

        # 2. Soft cross-checks: a claimed signal with no corroborating structured
        #    evidence. These do NOT invalidate (the screenshot may show it) — they
        #    surface a divergence between the model's claim and the DOM capture.
        if output.signals.error.present and not state.errors:
            output.warnings.append("error claimed but no error/validation message in captured state")
        if output.signals.captcha_present.present:
            hay = (state.visible_text or "").lower() + " " + (state.html or "").lower()
            if not any(k in hay for k in ("captcha", "recaptcha", "hcaptcha", "turnstile", "challenges.cloudflare")):
                output.warnings.append("captcha claimed but no captcha marker in captured DOM/text")

        # 3. Guard the highest-risk action: never SUBMIT with unfilled required
        #    fields or a live validation error visible in the captured state.
        if a.type == ActionType.SUBMIT:
            unfilled = [i for i in state.required_inputs if not i.is_filled and i.field_type != "file"]
            if unfilled:
                labels = ", ".join((i.label or i.selector) for i in unfilled[:6])
                output.warnings.append(
                    f"SUBMIT chosen but {len(unfilled)} required field(s) look unfilled: {labels}"
                )
                if self.strict_grounding:
                    output.valid = False
            if state.validation_errors:
                output.warnings.append(
                    "SUBMIT chosen while validation errors are visible on the page"
                )
                if self.strict_grounding:
                    output.valid = False

        # 4. Guard DONE: a success claim needs a positive confirmation signal.
        if a.type == ActionType.DONE and not state.success_messages:
            # allow if the model quoted a confirmation URL/text in its why/confirmation
            corroborated = bool(
                (a.confirmation and len(a.confirmation) > 8)
                and re.search(r"receiv|submitt|thank you|success|confirm", a.confirmation, re.I)
            )
            if not corroborated:
                output.warnings.append(
                    "DONE chosen without a success/confirmation message in captured state"
                )
                if self.strict_grounding:
                    output.valid = False

    # ─────────────────────────────────────────────────────────────────────────
    # Memory bookkeeping
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _action_summary(output: ReasoningOutput) -> str:
        a = output.action
        parts = [a.type.value]
        if a.field_label:
            parts.append(f"'{a.field_label}'")
        if a.selector:
            parts.append(a.selector)
        if a.value and a.type in (ActionType.FILL, ActionType.SELECT_OPTION, ActionType.UPLOAD):
            parts.append(f"= {str(a.value)[:40]!r}")
        if a.url:
            parts.append(a.url)
        if a.direction:
            parts.append(a.direction)
        return " ".join(parts)

    def _record_turn(
        self,
        output: ReasoningOutput,
        state: BrowserState,
        conversation: Optional[ConversationMemory],
        step: Optional[int],
    ) -> None:
        if conversation is None:
            return
        # Backfill the PREVIOUS turn's outcome with this turn's observed change.
        if conversation.turns and output.observation.what_changed:
            conversation.set_last_outcome(output.observation.what_changed)
        conversation.add(ConversationTurn(
            step=step if step is not None else len(conversation.turns) + 1,
            action_type=output.action.type.value,
            action_summary=self._action_summary(output),
            reasoning_summary=output.why or (output.action.reason or ""),
            page_type=output.observation.page_type.value,
        ))

    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _fallback(
        reason: str,
        conversation: Optional[ConversationMemory],
        step: Optional[int],
    ) -> ReasoningOutput:
        out = ReasoningOutput()
        out.action = out.action  # OBSERVE by default
        out.valid = False
        out.parse_error = reason
        out.warnings.append(f"reasoning fallback: {reason}")
        out.why = "LLM produced no usable reasoning this turn; observing again."
        if conversation is not None:
            conversation.add(ConversationTurn(
                step=step if step is not None else len(conversation.turns) + 1,
                action_type=ActionType.OBSERVE.value,
                action_summary="OBSERVE (fallback)",
                reasoning_summary=reason,
            ))
        return out
