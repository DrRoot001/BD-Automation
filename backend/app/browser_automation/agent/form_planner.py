"""Pre-fill form structure analysis and planning.

Before the AgentLoop starts filling fields one-by-one, this module runs a
**planning phase**: a single Gemini 2.5 Pro call that receives the full-page
DOM snapshot and returns a structured form plan. The plan tells the agent:

  * Whether this is a single-page or multi-step wizard
  * How many sections the form has and what fields each contains
  * Whether conditional fields were detected (and what triggers them)
  * Where the submit button is
  * Whether a CAPTCHA is present
  * Any special notes about the form layout

This eliminates "scroll-loop discovering fields one at a time" and lets the
agent fill strategically instead of top-to-bottom blindly.

Usage::

    planner = FormPlanner(llm_client)
    plan = await planner.plan(page, frame, dom_snapshot, platform_hints)
    # plan.sections, plan.form_type, plan.submit_selector, etc.
    # inject into the system prompt for all subsequent turns

Design:
  * One LLM call per form (amortised across 20-60 action steps).
  * Uses Gemini 2.5 Pro (``tier="deep"``) for best form-understanding.
  * Falls back gracefully: if the LLM call fails, returns an empty plan
    and the loop continues with its existing behaviour.
  * The plan is injected into the system prompt as a compact block, not
    as a separate message — keeps the prompt assembly path unchanged.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FormSection:
    """A logical section of the form (e.g. 'Personal Info', 'Experience')."""

    name: str
    fields: List[str] = field(default_factory=list)
    is_conditional: bool = False
    condition_note: Optional[str] = None


@dataclass
class FormPlan:
    """Structured understanding of a form's layout and requirements."""

    form_type: str = "single_page"  # single_page | multi_step_wizard | tabbed | modal
    total_sections: int = 0
    sections: List[FormSection] = field(default_factory=list)
    has_conditional_fields: bool = False
    conditional_notes: Optional[str] = None
    submit_selector: Optional[str] = None
    captcha_detected: Optional[str] = None  # hcaptcha | recaptcha_v2 | turnstile | None
    estimated_field_count: int = 0
    has_file_upload: bool = False
    has_cover_letter_upload: bool = False
    has_demographic_section: bool = False
    has_eeo_section: bool = False
    notes: Optional[str] = None
    # Set to True when the plan was generated successfully.
    is_valid: bool = False

    def format_for_prompt(self) -> str:
        """Render the plan as a compact block for the system prompt."""
        if not self.is_valid:
            return ""

        lines = [
            "══════════════════════════════════════════════════════════════════════",
            "FORM PLAN (analysed on first page load — use as your roadmap)",
            "══════════════════════════════════════════════════════════════════════",
            f"  Type: {self.form_type}",
            f"  Estimated fields: {self.estimated_field_count}",
        ]

        if self.sections:
            lines.append("  Sections:")
            for i, sec in enumerate(self.sections, 1):
                cond = " [CONDITIONAL]" if sec.is_conditional else ""
                fields_str = ", ".join(sec.fields[:8])
                if len(sec.fields) > 8:
                    fields_str += f", ... (+{len(sec.fields) - 8} more)"
                lines.append(f"    {i}. {sec.name}{cond}: {fields_str}")
                if sec.condition_note:
                    lines.append(f"       ↳ {sec.condition_note}")

        if self.has_conditional_fields and self.conditional_notes:
            lines.append(f"  ⚠ Conditional fields: {self.conditional_notes}")

        if self.submit_selector:
            lines.append(f"  Submit button: {self.submit_selector}")

        if self.captcha_detected:
            lines.append(f"  Captcha: {self.captcha_detected}")

        flags = []
        if self.has_file_upload:
            flags.append("resume upload")
        if self.has_cover_letter_upload:
            flags.append("cover letter upload")
        if self.has_demographic_section:
            flags.append("demographic block")
        if self.has_eeo_section:
            flags.append("EEO section")
        if flags:
            lines.append(f"  Detected: {', '.join(flags)}")

        if self.notes:
            lines.append(f"  Notes: {self.notes}")

        lines.append("")
        return "\n".join(lines)


_PLAN_PROMPT = """You are analyzing a job application form's structure. You will receive a DOM snapshot of the form.

Analyze the form and return a JSON object with this EXACT schema:
{
  "form_type": "single_page" | "multi_step_wizard" | "tabbed" | "modal",
  "total_sections": <integer>,
  "sections": [
    {
      "name": "<section name, e.g. 'Personal Information'>",
      "fields": ["<field labels in this section>"],
      "is_conditional": false,
      "condition_note": null
    }
  ],
  "has_conditional_fields": false,
  "conditional_notes": null,
  "submit_selector": "<CSS selector for the submit button>",
  "captcha_detected": null | "hcaptcha" | "recaptcha_v2" | "turnstile",
  "estimated_field_count": <integer>,
  "has_file_upload": false,
  "has_cover_letter_upload": false,
  "has_demographic_section": false,
  "has_eeo_section": false,
  "notes": "<any important observations about this form>"
}

Rules:
- Identify ALL sections/groups of related fields (by fieldset, visual grouping, headings).
- For multi-step wizards, describe each step/page as a section.
- Flag any fields that appear to be conditionally shown (hidden by default, revealed by another answer).
- Detect file upload fields (resume, cover letter).
- Detect demographic/EEO sections (gender, race, veteran, disability).
- Pick the most reliable submit button selector.
- If you see a captcha widget, identify its type.
- Count all visible form fields.

DOM SNAPSHOT:
"""

_PLAN_SYSTEM = (
    "You are a form-analysis expert. Respond with exactly one JSON object "
    "matching the schema in the user prompt. No prose, no markdown fences."
)


class FormPlanner:
    """Analyze a form's structure before filling begins."""

    def __init__(self, llm_client: Any = None):
        self._client = llm_client

    def _llm(self) -> Any:
        if self._client is None:
            from ..llm import get_llm
            self._client = get_llm()
        return self._client

    async def plan(
        self,
        page: Any,
        frame: Optional[Any],
        dom_snapshot: str,
        screenshot: Optional[bytes] = None,
        platform: Optional[str] = None,
    ) -> FormPlan:
        """Run the planning phase. Returns a FormPlan (may be invalid on failure)."""
        from ..llm import LLMUnavailable

        if not dom_snapshot or dom_snapshot.startswith("("):
            logger.info("[FormPlanner] No DOM snapshot available, skipping plan")
            return FormPlan()

        prompt = _PLAN_PROMPT + dom_snapshot
        if platform:
            prompt += f"\n\nDetected ATS platform: {platform}"

        try:
            raw = await self._llm().generate_json(
                prompt=prompt,
                image_bytes=screenshot,
                temperature=0.0,
                timeout_s=30.0,
                system=_PLAN_SYSTEM,
            )
        except LLMUnavailable as exc:
            logger.info(f"[FormPlanner] LLM unavailable: {exc}")
            return FormPlan()
        except Exception as exc:
            logger.warning(f"[FormPlanner] Planning failed: {exc}")
            return FormPlan()

        return self._parse_plan(raw)

    @staticmethod
    def _parse_plan(raw: Any) -> FormPlan:
        """Parse the LLM's JSON response into a FormPlan."""
        if not isinstance(raw, dict):
            logger.warning(f"[FormPlanner] Expected dict, got {type(raw).__name__}")
            return FormPlan()

        try:
            sections = []
            for sec_data in raw.get("sections") or []:
                if not isinstance(sec_data, dict):
                    continue
                sections.append(
                    FormSection(
                        name=str(sec_data.get("name") or "Unknown"),
                        fields=[
                            str(f) for f in (sec_data.get("fields") or []) if f
                        ],
                        is_conditional=bool(sec_data.get("is_conditional")),
                        condition_note=sec_data.get("condition_note"),
                    )
                )

            plan = FormPlan(
                form_type=str(raw.get("form_type") or "single_page"),
                total_sections=int(raw.get("total_sections") or len(sections)),
                sections=sections,
                has_conditional_fields=bool(raw.get("has_conditional_fields")),
                conditional_notes=raw.get("conditional_notes"),
                submit_selector=raw.get("submit_selector"),
                captcha_detected=raw.get("captcha_detected"),
                estimated_field_count=int(raw.get("estimated_field_count") or 0),
                has_file_upload=bool(raw.get("has_file_upload")),
                has_cover_letter_upload=bool(raw.get("has_cover_letter_upload")),
                has_demographic_section=bool(raw.get("has_demographic_section")),
                has_eeo_section=bool(raw.get("has_eeo_section")),
                notes=raw.get("notes"),
                is_valid=True,
            )
            logger.info(
                f"[FormPlanner] Plan: type={plan.form_type} sections={plan.total_sections} "
                f"fields≈{plan.estimated_field_count} captcha={plan.captcha_detected}"
            )
            return plan
        except Exception as exc:
            logger.warning(f"[FormPlanner] Parse error: {exc}")
            return FormPlan()
