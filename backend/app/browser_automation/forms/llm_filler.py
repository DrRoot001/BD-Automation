"""LLM-driven form filler.

For every detected field we ask Gemini, in batches, what value to type given:
  - the field's label / type / required-ness / option list
  - the candidate profile
  - pre-known screening answers
  - previously-learned answers from `field_memory.json`

Memory lookup runs first (free, instant). Only fields the memory could not
answer are forwarded to Gemini. After a successful fill we record the answer
back into memory so the next run gets it for free.

This module deliberately reuses the existing fill / type / upload primitives in
``filler.py`` — it only replaces the "what should I type into this field?"
decision. If the LLM is unavailable, callers MUST fall back to the regex path
in ``filler.fill_form``.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page

from . import memory as field_memory
from .filler import _best_select_match, _normalize_label
from .models import DetectedForm, FormField
from ..llm import LLMUnavailable, get_gemini

logger = logging.getLogger(__name__)


# Fields we never send to the LLM — the answer is mechanical (the resume path /
# cover-letter path) or unsafe to outsource (signature/agreement fields default
# to "Yes").
_HARDCODED_PROFILE_KEYS = {
    "name", "first_name", "last_name", "email", "phone",
    "location", "linkedin_url", "website",
}


def _profile_direct_value(field: FormField, profile: Dict[str, Any]) -> Optional[str]:
    """Cheap pre-LLM lookup for blindingly-obvious fields (name/email/phone).

    Returns a value if the field label clearly maps to a profile key, else None.
    """
    lbl = _normalize_label(field.label)
    table = [
        (("first name", "given name", "fname"), profile.get("first_name")),
        (("last name", "surname", "family name", "lname"), profile.get("last_name")),
        (("full name", "your name", "applicant name", "name"), profile.get("name")),
        (("email", "e-mail"), profile.get("email")),
        (("phone", "mobile", "telephone", "cell"), profile.get("phone")),
        (("linkedin",), profile.get("linkedin_url")),
        (("website", "portfolio", "github"), profile.get("website")),
        (("current company", "employer"), profile.get("current_company")),
        (("current title", "current role", "job title"), profile.get("current_title")),
        (("location", "city"), profile.get("location")),
    ]
    for needles, value in table:
        if value and any(n in lbl for n in needles):
            return str(value)
    return None


def _format_field_for_llm(field: FormField, index: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": index,
        "label": field.label,
        "type": field.field_type,
        "required": field.required,
    }
    if field.options:
        out["options"] = field.options
    return out


def _build_llm_prompt(
    fields: List[Tuple[int, FormField]],
    profile: Dict[str, Any],
    screening_answers: Dict[str, str],
    job_context: Optional[Dict[str, Any]] = None,
) -> str:
    safe_profile = {k: v for k, v in profile.items() if v not in (None, "")}
    fields_payload = [_format_field_for_llm(f, idx) for idx, f in fields]

    rules = """
You are an automated job-application agent filling a web form on behalf of a
candidate. For each field below, output the single best value to enter.

HARD RULES (do not violate):
  1. Return STRICT JSON only. Schema:
     {
       "answers": [
         {"id": <int>, "value": "<string>", "confidence": 0.0-1.0,
          "reason": "<<= 80 chars>"}, ...
       ]
     }
  2. One entry per field, in the same order as the input.
  3. For select / radio / checkbox: pick a value EXACTLY from the provided
     options list. Never invent a new option.
  4. For yes/no work-authorization questions, answer "Yes" if the profile's
     work_authorization is "Yes". For sponsorship questions, answer the
     OPPOSITE of work_authorization (authorized → no sponsorship).
  5. For EEO / demographic fields (gender, race, ethnicity, veteran status,
     disability), pick the "Decline to self-identify" / "Prefer not to answer"
     option when present. Never invent demographic data.
  6. For salary expectations, prefer the value in the profile. If none, use a
     reasonable USD range string like "Market rate" or "Negotiable".
  7. REQUIRED FIELDS WITH MISSING PROFILE DATA — DO NOT LEAVE BLANK.
     If a field is required:true and the profile lacks a direct value, INFER a
     defensible answer from what you DO have:
       - current_company missing -> "Freelance" or "Self-employed"
       - current_title missing  -> infer from years_exp + tech_stack
         (e.g. >=5y JS/React  -> "Senior Software Engineer";
                <3y any stack -> "Software Engineer";
                3-5y          -> "Software Engineer" or "Full-Stack Engineer")
       - education missing      -> "Bachelor's Degree"
       - referral_source missing -> "Company Website" or "LinkedIn"
       - any other required text -> pick the most generic plausible value
     Use confidence 0.55-0.75 when inferring, 0.85+ when the profile has it.
  8. If you are <0.4 confident, still output your best guess but lower the
     confidence; the caller may skip low-confidence answers on optional fields.
  9. NEVER output PII you weren't given. If the profile lacks a value and the
     field is OPTIONAL (required:false), use confidence 0.0 with value "" and
     reason="no_data".
"""
    return (
        rules
        + "\nCANDIDATE PROFILE (JSON):\n"
        + _json(safe_profile)
        + "\n\nPRE-ANSWERED SCREENING QUESTIONS (use these verbatim if a field matches):\n"
        + _json(screening_answers or {})
        + "\n\nJOB CONTEXT:\n"
        + _json(job_context or {})
        + "\n\nFIELDS TO FILL:\n"
        + _json(fields_payload)
        + "\n\nNow return the JSON object as described."
    )


def _json(obj: Any) -> str:
    # Compact JSON — every saved indent char is a token saved.
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


async def _ask_llm_for_values(
    fields: List[Tuple[int, FormField]],
    profile: Dict[str, Any],
    screening_answers: Dict[str, str],
    job_context: Optional[Dict[str, Any]] = None,
) -> Dict[int, Dict[str, Any]]:
    """Returns {field_id: {value, confidence, reason}}. Raises LLMUnavailable."""
    prompt = _build_llm_prompt(fields, profile, screening_answers, job_context)
    client = get_gemini()
    data = await client.generate_json(prompt, temperature=0.1, timeout_s=30.0)
    answers = (data or {}).get("answers") or []
    out: Dict[int, Dict[str, Any]] = {}
    for a in answers:
        try:
            fid = int(a["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[fid] = {
            "value": str(a.get("value", "")),
            "confidence": float(a.get("confidence", 0.5)),
            "reason": str(a.get("reason", ""))[:120],
        }
    return out


async def _apply_value_to_field(
    page: Page,
    field: FormField,
    value: str,
) -> bool:
    """Type / click / select the value into the field. Returns True on success."""
    try:
        locator = page.locator(field.selector).first
        ftype = field.field_type

        if ftype == "file":
            await locator.set_input_files(value, timeout=8000)
            return True

        # Visibility/scroll for typed fields
        if ftype in ("text", "email", "phone", "textarea", "url", "date", "select"):
            try:
                await locator.wait_for(state="visible", timeout=3000)
            except Exception:
                logger.info(f"[LLMFill] Skipping non-visible field '{field.label}'")
                return False
            await locator.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.1, 0.3))

        if ftype in ("text", "email", "phone", "textarea", "url", "date"):
            await locator.click(force=True, timeout=8000)
            await locator.fill(value, timeout=8000)
            return True

        if ftype == "select":
            if getattr(field, "custom_widget", False):
                safe_val = value.replace("'", "\\'").replace('"', '\\"')
                await locator.click(force=True, timeout=8000)
                await asyncio.sleep(random.uniform(0.3, 0.7))
                option_selectors = [
                    f"[role='option']:has-text('{safe_val}')",
                    f"li[role='option']:has-text('{safe_val}')",
                    f".select__option:has-text('{safe_val}')",
                    f".react-select__option:has-text('{safe_val}')",
                    f".select-shell-list-item:has-text('{safe_val}')",
                    f"li:has-text('{safe_val}')",
                ]
                for opt_sel in option_selectors:
                    try:
                        opt = page.locator(opt_sel).first
                        if await opt.count() > 0:
                            await opt.click(timeout=2500)
                            return True
                    except Exception:
                        continue
                # Fallback: type + Enter
                await page.keyboard.type(value, delay=50)
                await asyncio.sleep(0.4)
                await page.keyboard.press("Enter")
                return True
            target = _best_select_match(field.options or [], value)
            if not target:
                return False
            await locator.select_option(label=target, timeout=8000)
            return True

        if ftype == "radio":
            if field.options:
                target_display = _best_select_match(field.options, value)
                if not target_display:
                    return False
                raw_val = target_display
                if field.raw_values:
                    try:
                        idx = field.options.index(target_display)
                        raw_val = field.raw_values[idx]
                    except (ValueError, IndexError):
                        pass
                radio = page.locator(f"{field.selector}[value='{raw_val}']").first
                await radio.scroll_into_view_if_needed()
                await radio.click(force=True, timeout=8000)
                return True
            # No options → boolean radio
            if str(value).lower() in ("yes", "true", "1", "on"):
                await locator.check(force=True, timeout=8000)
                return True
            return False

        if ftype == "checkbox":
            if str(value).lower() in ("yes", "true", "1", "on", "i agree", "agreed"):
                await locator.check(force=True, timeout=8000)
                return True
            return False

    except Exception as exc:
        logger.warning(f"[LLMFill] Failed to apply value to '{field.label}': {exc}")
        return False

    return False


async def fill_form_with_llm(
    page: Page,
    form: DetectedForm,
    profile: Dict[str, Any],
    screening_answers: Optional[Dict[str, str]] = None,
    job_context: Optional[Dict[str, Any]] = None,
    resume_path: Optional[str] = None,
    cover_letter_path: Optional[str] = None,
) -> bool:
    """LLM-driven form fill. Returns True if every required field was filled.

    Layering:
      1. Memory recall (free).
      2. Profile-direct lookup (name/email/phone/etc — also free, deterministic).
      3. Hardcoded file-path mapping for file inputs.
      4. Anything left → ONE batched Gemini call.

    On LLM failure we surface a partial-success bool — the caller decides
    whether to retry with the regex filler.
    """
    screening_answers = screening_answers or {}
    success = True
    filled = 0
    skipped = 0
    unresolved_required: List[str] = []

    # ── Step 1: Pre-resolve every field using free/cheap sources ─────────────
    needs_llm: List[Tuple[int, FormField]] = []
    resolved: Dict[int, Tuple[str, str]] = {}  # idx -> (value, source)

    for idx, field in enumerate(form.fields):
        # File inputs are wired to caller-supplied paths
        if field.field_type == "file":
            lbl = field.label.lower()
            if "cover" in lbl and cover_letter_path:
                resolved[idx] = (cover_letter_path, "file_cover")
            elif resume_path:
                resolved[idx] = (resume_path, "file_resume")
            continue

        # Memory first
        remembered = field_memory.recall(field.label, field.field_type, field.options)
        if remembered:
            resolved[idx] = (remembered, "memory")
            continue

        # Cheap profile lookup
        direct = _profile_direct_value(field, profile)
        if direct:
            resolved[idx] = (direct, "profile_direct")
            continue

        # Screening answer match (substring)
        lbl_n = _normalize_label(field.label)
        sa_hit = None
        for q, a in screening_answers.items():
            q_n = q.lower().strip()
            if q_n and (q_n in lbl_n or lbl_n in q_n):
                sa_hit = a
                break
        if sa_hit:
            resolved[idx] = (sa_hit, "screening")
            continue

        # Everything else goes to the LLM
        needs_llm.append((idx, field))

    # ── Step 2: Ask Gemini in one batched call ────────────────────────────────
    if needs_llm:
        try:
            llm_answers = await _ask_llm_for_values(
                needs_llm, profile, screening_answers, job_context
            )
            for idx, field in needs_llm:
                ans = llm_answers.get(idx)
                if not ans or not ans.get("value"):
                    continue
                # Optional fields: skip low-confidence guesses.
                # Required fields: accept ANY non-empty guess — better to put
                # a defensible inference than to leave the form broken.
                if not field.required and ans["confidence"] < 0.4:
                    continue
                resolved[idx] = (ans["value"], f"llm:{ans['confidence']:.2f}")
        except LLMUnavailable as exc:
            logger.warning(f"[LLMFill] LLM unavailable — {exc}")
            # Return False so caller falls back to the regex filler
            return False
        except Exception as exc:
            logger.error(f"[LLMFill] Unexpected LLM error: {exc}")
            return False

    # ── Step 3: Apply values field by field ───────────────────────────────────
    for idx, field in enumerate(form.fields):
        entry = resolved.get(idx)
        if not entry:
            if field.required and field.field_type != "file":
                logger.warning(f"[LLMFill] REQUIRED unfilled: '{field.label}'")
                unresolved_required.append(field.label)
                try:
                    field_memory.record_failure(
                        field.label, field.field_type, field.options, None,
                        "no_value_resolved_by_llm_or_memory",
                    )
                except Exception:
                    pass
            else:
                skipped += 1
            continue

        value, source = entry
        ok = await _apply_value_to_field(page, field, value)
        if ok:
            filled += 1
            logger.info(f"[LLMFill] FILLED [{field.field_type}/{source}] "
                        f"'{field.label}' = '{str(value)[:60]}'")
            # Persist to memory unless it's a file path (varies per run) or already memory
            if (
                field.field_type != "file"
                and source not in ("memory",)
            ):
                try:
                    field_memory.remember(field.label, field.field_type, str(value), source)
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(0.1, 0.4))
        else:
            if field.required:
                unresolved_required.append(field.label)
                try:
                    field_memory.record_failure(
                        field.label, field.field_type, field.options, str(value),
                        f"apply_failed_source={source}",
                    )
                except Exception:
                    pass

    if unresolved_required:
        success = False

    logger.info(
        f"[LLMFill] complete: filled={filled} skipped={skipped} "
        f"unresolved_required={len(unresolved_required)} success={success}"
    )

    # ── Step 4: Post-fill VERIFICATION ────────────────────────────────────────
    # Walk required fields and confirm the DOM actually holds the value we
    # asked for. Custom widgets (react-select, file uploaders) are notorious
    # for silently swallowing input — typing into a combobox search box looks
    # successful but commits nothing. We catch that here instead of trusting
    # the apply step's True return.
    verification_failures: List[Tuple[str, str]] = []
    for idx, field in enumerate(form.fields):
        if not field.required or field.field_type == "file":
            continue
        entry = resolved.get(idx)
        if not entry:
            continue
        expected, _ = entry
        try:
            actual = await _read_field_value(page, field)
        except Exception as exc:
            logger.debug(f"[LLMFill] verify read failed for '{field.label}': {exc}")
            continue
        if not _values_match(expected, actual):
            logger.warning(
                f"[LLMFill] VERIFY MISMATCH '{field.label}' "
                f"expected={expected!r} actual={actual!r}"
            )
            verification_failures.append((field.label, f"want={expected} got={actual}"))

    if verification_failures:
        success = False
        logger.warning(
            f"[LLMFill] post-fill verification found {len(verification_failures)} "
            f"required field(s) that look unfilled in the DOM"
        )

    return success


# ─────────────────────────────────────────────────────────────────────────────
# Post-fill verification helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _read_field_value(page: Page, field: FormField) -> str:
    """Return whatever the DOM currently shows for this field's value.

    For custom widgets (react-select), read the rendered .select__single-value /
    .react-select__single-value text. For native inputs, read .value.
    Returns "" if we can't determine it (caller treats that as "skip verify").
    """
    locator = page.locator(field.selector).first
    if await locator.count() == 0:
        return ""

    ftype = field.field_type
    is_custom = getattr(field, "custom_widget", False)

    if ftype == "select" and is_custom:
        # Walk up to find the .select__control wrapper, then read the single-value
        rendered = await locator.evaluate("""el => {
            let p = el.closest('.select__control, .react-select__control') || el.parentElement;
            for (let i = 0; i < 4 && p; i++) {
                const v = p.querySelector(
                    '.select__single-value, .react-select__single-value, .select-shell-button-value'
                );
                if (v) return v.textContent.trim();
                p = p.parentElement;
            }
            return '';
        }""")
        return rendered or ""

    if ftype in ("text", "email", "phone", "url", "textarea", "date", "select"):
        return (await locator.input_value()) or ""

    if ftype == "checkbox":
        return "Yes" if await locator.is_checked() else ""

    if ftype == "radio":
        # Find which radio in the group is checked
        name = await locator.evaluate("el => el.name || ''")
        if not name:
            return ""
        checked = page.locator(f"input[type=radio][name='{name}']:checked").first
        if await checked.count() == 0:
            return ""
        return (await checked.get_attribute("value")) or "Yes"

    return ""


def _values_match(expected: str, actual: str) -> bool:
    """Loose equality for verification — case-insensitive, ignores surrounding
    whitespace and trailing punctuation, and accepts substring matches both
    ways. React-select often shows the option label exactly; native inputs
    sometimes normalize spaces/case.
    """
    if not actual:
        return False
    e = (expected or "").strip().lower().rstrip(".,*")
    a = (actual or "").strip().lower().rstrip(".,*")
    if not e:
        return True  # no expectation, no failure
    if e == a:
        return True
    if e in a or a in e:
        return True
    return False
