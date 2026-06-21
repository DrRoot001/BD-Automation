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


def _enrich_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Populate derivable defaults so the LLM doesn't have to guess them.

    Why: M3 currently doesn't write current_title / current_company columns to
    the candidates table, so the LLM was being asked to invent them and
    answered "Software Engineer" / "Freelance" with 0.65-0.70 confidence. We
    can derive equivalent values deterministically from years_exp + tech_stack
    at zero token cost. M3 will eventually backfill from parsed resumes — this
    is the M4-side safety net so the form always fills.
    """
    p = dict(profile or {})
    if not p.get("current_title"):
        try:
            years = int(float(str(p.get("experience_years") or p.get("years_exp") or 0)))
        except (ValueError, TypeError):
            years = 0
        stack = str(p.get("tech_stack") or "").lower()
        if any(t in stack for t in ("react native", "ios", "android", "swift", "kotlin")):
            base = "Mobile Engineer"
        elif any(t in stack for t in ("react", "vue", "angular", "next.js", "frontend")):
            base = "Frontend Engineer"
        elif any(t in stack for t in ("kubernetes", "terraform", "devops", "sre", "ansible")):
            base = "DevOps Engineer"
        elif any(t in stack for t in ("pytorch", "tensorflow", "ml", "machine learning", "data scientist")):
            base = "Machine Learning Engineer"
        else:
            base = "Software Engineer"
        if years >= 8:
            p["current_title"] = f"Senior {base}"
        elif years >= 4:
            p["current_title"] = base
        elif years >= 1:
            p["current_title"] = f"Junior {base}"
        else:
            p["current_title"] = base
    if not p.get("current_company"):
        p["current_company"] = "Freelance"
    if not p.get("education"):
        p["education"] = "Bachelor's Degree"
    if not p.get("referral_source"):
        p["referral_source"] = "LinkedIn"
    return p


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


# Token-optimization: cap the options list shown to the LLM. Country/state
# pickers can have 200+ entries which inflates the prompt by ~1k tokens per
# field. The LLM only needs to see a window large enough to find the match —
# and the post-fill custom-select widget logic does its own substring match
# against the FULL option list at apply time.
_MAX_OPTIONS_SHOWN = 25


def _trim_options(options: List[str], profile: Dict[str, Any]) -> List[str]:
    if not options or len(options) <= _MAX_OPTIONS_SHOWN:
        return options
    # Heuristic: keep options whose first word matches profile location /
    # country tokens, then pad with the first N entries of the original list.
    needles = " ".join(
        str(profile.get(k, "")) for k in ("location", "country", "current_company")
    ).lower()
    matched = [o for o in options if o and any(w in needles for w in o.lower().split()[:2])]
    rest = [o for o in options if o not in matched]
    return (matched + rest)[:_MAX_OPTIONS_SHOWN]


def _format_field_for_llm(field: FormField, index: int, profile: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": index,
        "label": field.label,
        "type": field.field_type,
        "required": field.required,
    }
    if field.options:
        out["options"] = _trim_options(field.options, profile)
    return out


# Profile keys the LLM rarely needs for field decisions — dropping them trims
# ~200 tokens per call when tech_stack is long.
_PROFILE_DROP_KEYS = {"tech_stack"}  # kept only for current_title derivation, not LLM
_JOB_CONTEXT_KEEP_KEYS = {"platform", "ats_type"}  # job_url is long and unused for decisions


def _build_llm_prompt(
    fields: List[Tuple[int, FormField]],
    profile: Dict[str, Any],
    screening_answers: Dict[str, str],
    job_context: Optional[Dict[str, Any]] = None,
) -> str:
    safe_profile = {
        k: v for k, v in profile.items()
        if v not in (None, "") and k not in _PROFILE_DROP_KEYS
    }
    safe_job_ctx = {
        k: v for k, v in (job_context or {}).items() if k in _JOB_CONTEXT_KEEP_KEYS
    }
    fields_payload = [_format_field_for_llm(f, idx, profile) for idx, f in fields]

    # Condensed rules. ~70% fewer tokens than the prior verbose form while
    # preserving every load-bearing behavior (JSON schema, EEO defaults,
    # required-must-not-be-blank, work-auth/sponsorship inversion).
    rules = (
        "Job-application form filler. For each field, return the value to enter.\n"
        "Return JSON ONLY: {\"answers\":[{\"id\":int,\"value\":str,\"confidence\":0..1,\"reason\":str<=60}]}\n"
        "RULES:\n"
        "1. select/radio/checkbox: value MUST be exactly one of the provided options.\n"
        "2. work_authorization=Yes => yes-auth questions \"Yes\", sponsorship questions \"No\". Invert if No.\n"
        "3. EEO/demographic (gender/race/veteran/disability): pick \"Decline\" / \"Prefer not\" option if present.\n"
        "4. Required fields MUST be non-empty. Profile already has current_title/current_company/education/"
        "referral_source pre-filled - use those verbatim. Salary missing => \"Negotiable\".\n"
        "5. Optional + no data => value:\"\", confidence:0.0, reason:\"no_data\".\n"
        "6. confidence: 0.9+ profile-direct, 0.7 inferred, <0.4 wild guess.\n"
    )
    return (
        rules
        + "PROFILE:" + _json(safe_profile)
        + "\nSCREENING:" + _json(screening_answers or {})
        + "\nJOB:" + _json(safe_job_ctx)
        + "\nFIELDS:" + _json(fields_payload)
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

        # Scroll FIRST so off-viewport-but-visible fields don't fail the
        # visibility check. (Was the inverse — caused false-negative skips
        # on Greenhouse-hosted forms with long question lists.)
        if ftype in ("text", "email", "phone", "textarea", "url", "date", "select"):
            try:
                await locator.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                await locator.wait_for(state="visible", timeout=3000)
            except Exception:
                logger.info(f"[LLMFill] Skipping non-visible field '{field.label}'")
                return False
            await asyncio.sleep(random.uniform(0.1, 0.3))

        if ftype in ("text", "email", "phone", "textarea", "url", "date"):
            # React-controlled inputs (Greenhouse, Workday, Ashby) need the value
            # set via the prototype's native setter so React's _valueTracker sees
            # the change. A plain locator.fill() writes the DOM but React's
            # internal state stays empty, and the next render wipes the field.
            try:
                await locator.click(force=True, timeout=5000)
            except Exception:
                pass
            committed = await locator.evaluate(
                """(el, v) => {
                    try {
                        const proto = el.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(el, v);
                        el.dispatchEvent(new Event('input',  {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        return el.value === v;
                    } catch (e) { return false; }
                }""",
                str(value),
            )
            if not committed:
                # Fall back to plain fill — better than nothing
                try:
                    await locator.fill(str(value), timeout=5000)
                except Exception:
                    return False
            return True

        if ftype == "select":
            if getattr(field, "custom_widget", False):
                return await _commit_custom_select(page, locator, field, value)
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
    profile = _enrich_profile(profile)
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

    # ── Step 4: Post-fill VERIFICATION with TARGETED RETRY ──────────────────
    # Walk required fields and confirm the DOM actually holds the value we
    # asked for. If a field shows empty in the DOM, retry filling JUST that
    # field (up to 2 retries) instead of triggering the full regex-filler
    # fallback — re-opening already-filled react-select widgets clears them.
    MAX_RETRIES = 2
    for retry in range(MAX_RETRIES + 1):
        verification_failures: List[Tuple[str, str, int]] = []  # (label, msg, idx)
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
                tag = "VERIFY MISMATCH" if retry == 0 else f"VERIFY MISMATCH (retry {retry}/{MAX_RETRIES})"
                logger.warning(
                    f"[LLMFill] {tag} '{field.label}' "
                    f"expected={expected!r} actual={actual!r}"
                )
                verification_failures.append((field.label, f"want={expected} got={actual}", idx))

        if not verification_failures:
            if retry > 0:
                logger.info(f"[LLMFill] all required fields verified after {retry} retry/retries")
            break

        if retry == MAX_RETRIES:
            success = False
            logger.warning(
                f"[LLMFill] {len(verification_failures)} required field(s) still unfilled "
                f"after {MAX_RETRIES} retries — caller may fall back to regex filler"
            )
            break

        # Targeted retry: re-apply just the failing fields
        logger.info(
            f"[LLMFill] retrying {len(verification_failures)} field(s) in place "
            f"(retry {retry + 1}/{MAX_RETRIES})"
        )
        for label, _msg, idx in verification_failures:
            field = form.fields[idx]
            expected, _src = resolved[idx]
            try:
                # Move focus elsewhere first so the retry's open event is clean
                try:
                    await page.keyboard.press("Escape")
                    await page.locator("body").click(position={"x": 5, "y": 5}, timeout=1000)
                except Exception:
                    pass
                await asyncio.sleep(0.3)
                ok = await _apply_value_to_field(page, field, expected)
                logger.info(f"[LLMFill] retry apply for '{label}': ok={ok}")
            except Exception as exc:
                logger.debug(f"[LLMFill] retry exception for '{label}': {exc}")
            await asyncio.sleep(0.4)

    return success


# ─────────────────────────────────────────────────────────────────────────────
# Post-fill verification helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _commit_custom_select(page: Page, locator, field: FormField, value: str) -> bool:
    """Open a Greenhouse / react-select custom dropdown and pick a value.

    Strategy that handles BOTH short option lists (Yes/No) AND virtualized
    long lists (Country, with 200+ options where only the visible window is
    rendered):

      1. Open the menu — react-select listens for mousedown, not click, so
         we dispatch native mouse events on .select__control directly.
      2. Focus the inner combobox input and TYPE the value. For short lists
         this narrows the list (still contains the match). For virtualized
         lists this is the only way to materialize the matching option into
         the DOM.
      3. Look for an option whose text starts with or equals our value
         (case-insensitive) scoped to .select__menu.
      4. Click it via mousedown+mouseup (again, react-select needs mousedown).
      5. If still no match, fall back to pressing Enter — react-select usually
         auto-selects the first filtered option on Enter.
    """
    field_id = await locator.get_attribute("id") or ""
    if not field_id:
        return False

    # 1. Open THIS dropdown via page.evaluate + getElementById — matches the
    #    pattern that's proven to commit values reliably. locator.evaluate
    #    behaved differently on some forms; document.getElementById is
    #    100% deterministic.
    opened = await page.evaluate(
        """(id) => {
            const el = document.getElementById(id);
            if (!el) return false;
            const ctrl = el.closest('.select__control, .react-select__control');
            if (!ctrl) return false;
            ctrl.scrollIntoView({block:'center', behavior:'instant'});
            ctrl.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
            ctrl.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
            return true;
        }""",
        field_id,
    )
    if not opened:
        return False
    await asyncio.sleep(0.5)

    # 2. Focus + type to filter
    try:
        await page.locator(f"#{field_id}").first.focus()
        await page.keyboard.type(value, delay=30)
        await asyncio.sleep(0.4)
    except Exception as exc:
        logger.debug(f"[LLMFill] custom-select typing failed for '{field.label}': {exc}")

    # 3. Find the best option in the (possibly filtered) menu via JS so we can do
    #    a case-insensitive contains match and prefer EXACT match when available.
    option_id_prefix = f"react-select-{field_id}-option-" if field_id else ""

    # If typing filtered the menu to zero matches, clear input to see all options.
    if option_id_prefix:
        cur_count = await page.evaluate(
            "(prefix) => document.querySelectorAll('[id^=\"' + prefix + '\"]').length",
            option_id_prefix,
        )
        if cur_count == 0:
            try:
                await page.locator(f"#{field_id}").first.focus()
                for _ in range(len(value) + 5):
                    await page.keyboard.press("Backspace")
                await asyncio.sleep(0.4)
            except Exception:
                pass

    chosen_id = await page.evaluate(
        """({prefix, want}) => {
            const wantLc = want.trim().toLowerCase();
            const declineIntent = /\\b(decline|prefer not|rather not|don.t (wish|want)|do not (wish|want)|not (wish|want).?to.?answer|don.t.? answer|self.?identify|prefer.?not.?to.?say|wish to remain anonymous)\\b/i.test(want);
            const declineOptionPat = /\\b(decline|prefer not|rather not|don.t wish|do not wish|don.t want|do not want|prefer not to say|self.?identify|not to answer|wish to remain anonymous|not protected|i don.t wish|i do not want)\\b/i;
            // STRICT scoping: only consider options whose id starts with the
            // prefix for THIS dropdown. Never fall back to global [role=option]
            // queries — the page has other widgets (intl-tel-input phone
            // country picker) that use role=option and we'd pick "Afghanistan"
            // into the Gender field.
            let opts;
            if (prefix) {
                opts = Array.from(document.querySelectorAll('[id^="' + prefix + '"]'));
            } else {
                opts = Array.from(document.querySelectorAll(
                    '.select__menu .select__option, .react-select__menu .react-select__option, '
                    + '.select__menu [role="option"], .react-select__menu [role="option"]'
                ));
            }
            if (opts.length === 0) return null;
            if (declineIntent) {
                const decline = opts.find(o => declineOptionPat.test((o.textContent||'')));
                if (decline) return decline.id || '__no_id__:' + decline.textContent.trim();
            }
            const exact = opts.find(o => (o.textContent||'').trim().toLowerCase() === wantLc);
            if (exact) return exact.id || '__no_id__:' + exact.textContent.trim();
            const starts = opts.find(o => (o.textContent||'').trim().toLowerCase().startsWith(wantLc));
            if (starts) return starts.id || '__no_id__:' + starts.textContent.trim();
            const contains = opts.find(o => (o.textContent||'').trim().toLowerCase().includes(wantLc));
            if (contains) return contains.id || '__no_id__:' + contains.textContent.trim();
            // If we typed enough to narrow to one option, just take the first
            if (opts.length >= 1) return opts[0].id || '__no_id__:' + opts[0].textContent.trim();
            return null;
        }""",
        {"prefix": option_id_prefix, "want": value},
    )

    if chosen_id and not chosen_id.startswith("__no_id__:"):
        clicked = await page.evaluate(
            """(id) => {
                const opt = document.getElementById(id);
                if (!opt) return false;
                opt.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, button:0}));
                opt.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, button:0}));
                opt.dispatchEvent(new MouseEvent('click',     {bubbles:true, button:0}));
                return true;
            }""",
            chosen_id,
        )
        if clicked:
            await asyncio.sleep(0.5)
            return True

    # 5. Last-resort: press Enter — react-select auto-selects highlighted option
    try:
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)
        return True
    except Exception:
        return False


def _quote_js(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


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
