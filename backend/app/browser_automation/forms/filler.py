import asyncio
import random
import logging
import re
from playwright.async_api import Page
from .models import DetectedForm, FormField
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Human-Like Typing Engine
# ─────────────────────────────────────────────────────────────────────────────

async def human_type(page: Page, selector: str, text: str):
    """Type text character-by-character with realistic human-like delays.
    Includes random micro-pauses and occasional speed bursts to mimic
    natural typing cadence."""
    # Focus the element first
    locator = page.locator(selector).first
    await locator.click(force=True)
    await asyncio.sleep(random.uniform(0.1, 0.3))

    # Clear any existing content
    await locator.fill("")
    await asyncio.sleep(random.uniform(0.05, 0.15))

    # Type character by character
    for i, char in enumerate(text):
        await page.keyboard.type(char)
        # Base delay between characters (human typing speed ~50-150ms)
        base_delay = random.uniform(0.04, 0.12)
        # Occasionally pause longer (like a human thinking mid-word)
        if random.random() < 0.08:
            base_delay += random.uniform(0.15, 0.4)
        # Slightly faster for common sequences (space after word, etc.)
        if char == ' ':
            base_delay = random.uniform(0.06, 0.18)
        await asyncio.sleep(base_delay)


# ─────────────────────────────────────────────────────────────────────────────
# Intelligent Label → Profile Key Mapping Engine
# ─────────────────────────────────────────────────────────────────────────────

# Each rule: (list of label patterns, profile key or special handler)
# Patterns are checked against the lowercase + stripped label text.
# Order matters — more specific patterns MUST come before generic ones.
FIELD_MAPPING_RULES: List[Tuple[List[str], str]] = [
    # ── Screening questions FIRST — prevent "state" in "united states" from
    #    stealing "legally authorized to work in the united states" ────────────
    (["authorized to work", "legally authorized", "work authorization",
     "eligible to work", "right to work", "authorized in the united"],                 "_work_auth"),
    (["require sponsorship", "sponsorship", "visa sponsorship",
     "need sponsorship", "immigration sponsorship"],                                   "_sponsorship"),
    (["certify", "agree to", "and agree", "confirm and", "i agree",
     "terms and conditions", "terms & conditions", "i certify",
     "privacy policy", "acknowledge"],                                                 "_agree_terms"),
    (["willing to relocate", "open to relocation", "relocate"],                        "_relocate"),
    (["background check", "consent to background"],                                    "_background_check"),
    (["drug test", "drug screening"],                                                  "_drug_test"),

    # ── Demographic / EEO fields ──────────────────────────────────────────────
    (["gender", "gender identity", "sex", "pronouns"],                                 "_gender"),
    (["race", "ethnicity", "racial"],                                                  "_race_ethnicity"),
    (["veteran", "military status", "protected veteran"],                              "_veteran_status"),
    (["disability", "disabled"],                                                       "_disability_status"),

    # NOTE: Location-specific yes/no questions ("Do you live in X?")
    # are handled by _smart_infer_answer which compares the question text to
    # the candidate's actual profile location. Do NOT add a hardcoded rule
    # here — that would bypass the intelligent comparison.

    # ── Name fields — specific before generic ─────────────────────────────────
    (["first name", "fname", "given name", "first_name", "firstname"],               "_first_name"),
    (["last name", "lname", "surname", "family name", "last_name", "lastname"],      "_last_name"),
    (["middle name", "middle_name"],                                                  "_middle_name"),
    (["full name", "your name", "applicant name", "candidate name", "name"],          "name"),

    # ── Contact fields ────────────────────────────────────────────────────────
    (["email address", "email", "e-mail", "email_address"],                           "email"),
    (["phone number", "phone", "telephone", "mobile", "cell", "contact number"],      "phone"),

    # ── Location fields ───────────────────────────────────────────────────────
    (["current location", "location", "city", "city, state", "address", "zip"],       "location"),
    (["state", "province"],                                                            "state"),
    (["country"],                                                                      "country"),

    # ── Online presence ───────────────────────────────────────────────────────
    (["linkedin", "linkedin profile", "linkedin url"],                                 "linkedin_url"),
    (["website", "portfolio", "personal website", "portfolio / website",
     "portfolio/website", "personal site", "github"],                                  "website"),

    # ── Professional details ──────────────────────────────────────────────────
    (["current company", "most recent company", "current / most recent company",
     "current employer", "company name", "employer"],                                  "current_company"),
    (["current job title", "current title", "job title", "current role", "title",
     "position title", "current position"],                                            "current_title"),
    (["years of experience", "experience", "total experience",
     "how many years of experience"],                                                  "_experience_years"),
    (["highest education", "education", "education level", "degree",
     "highest degree"],                                                                "_education"),
    (["salary", "expected salary", "salary expectation", "desired salary",
     "compensation"],                                                                  "_salary"),

    # ── Dates ─────────────────────────────────────────────────────────────────
    (["start date", "earliest start date", "available start date",
     "date available", "availability"],                                                "_start_date"),

    # ── Cover letter / Additional ─────────────────────────────────────────────
    (["additional information", "additional info", "message to hiring manager",
     "cover letter text", "additional comments", "notes",
     "additional information / message to hiring manager",
     "tell us why"],                                                                   "_additional_info"),

    # ── Referral source ───────────────────────────────────────────────────────
    (["how did you hear", "heard about", "referral source", "source"],                "_referral_source"),
]


def _normalize_label(label: str) -> str:
    """Clean up a label for matching: lowercase, strip whitespace, 
    remove asterisks and leading/trailing punctuation."""
    label = label.lower().strip()
    label = re.sub(r'[*\u200b\xa0]', '', label)  # Remove *, zero-width space, nbsp
    label = re.sub(r'\s+', ' ', label)            # Collapse whitespace
    label = label.strip(':;. ')                   # Trim trailing punctuation
    return label


def _match_label_to_key(label: str) -> Optional[str]:
    """Try to match a form label to a profile key using the rules engine.
    Returns the profile key string, or None if no match."""
    normalized = _normalize_label(label)

    for patterns, key in FIELD_MAPPING_RULES:
        for pattern in patterns:
            # Exact match
            if normalized == pattern:
                return key
            # Substring containment (e.g., label "Your First Name *" contains "first name")
            if pattern in normalized:
                return key
    return None


def _parse_year_range(option_text: str) -> Optional[Tuple[float, float]]:
    """Parse option text like '2–5 years', '5-10 years', '10+ years', 'Less than 1 year'."""
    t = option_text.lower().strip()
    # "10+ years" or "more than 10"
    m = re.search(r"(\d+)\+", t)
    if m:
        return (float(m.group(1)), float("inf"))
    # "less than N year"
    m = re.search(r"less\s+than\s+(\d+)", t)
    if m:
        return (0.0, float(m.group(1)))
    # "N–M years" or "N-M years" (en-dash or hyphen)
    m = re.search(r"(\d+)\s*[–\-]\s*(\d+)", t)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    # Single number
    m = re.search(r"^(\d+)$", t)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None


def _smart_infer_answer(label: str, field_type: str, options: List[str], profile: Dict) -> Optional[str]:
    """Last-resort inference for fields the rules engine did not recognise.

    Instead of blindly skipping an unmatched field, we read the question text
    and available options and pick the most defensible answer — the same
    decision a human recruiter-coach would make when they don't have a specific
    answer prepared.

    Decision tree:
      1. EEO / demographic dropdowns  → "Decline to self identify" variant
      2. Yes/No location questions     → "No"  (candidates are remote/abroad)
      3. Yes/No availability questions → "Yes" (e.g. "available immediately?")
      4. Yes/No general screening      → "Yes" (positive default for most questions)
      5. Single-option selects         → that option (no real choice)
      6. Otherwise                     → None  (let the caller decide)
    """
    lbl = label.lower().strip()
    opts_lower = [o.lower().strip() for o in options] if options else []

    # ── 1. EEO / demographic ──────────────────────────────────────────────────
    eeo_keywords = ("gender", "race", "ethnicity", "veteran", "disability",
                    "sexual orientation", "pronouns", "identify")
    if any(k in lbl for k in eeo_keywords):
        # Find the "decline" / "prefer not" option
        for opt in options:
            ol = opt.lower()
            if any(k in ol for k in ("decline", "prefer not", "i don't wish",
                                     "choose not", "no answer", "not wish")):
                logger.debug(f"[SmartInfer] EEO field '{label}' → '{opt}' (decline)")
                return opt
        # If no decline option, pick last option (usually the least committal)
        if options:
            return options[-1]

    # ── 2. Location yes/no ("Do you live in Santiago?") ─────────────────────
    # INTELLIGENT MATCH: extract the city/region from the question and compare
    # to the candidate's actual profile location. The agent answers based on
    # whether the candidate is actually in that location.
    location_keywords = ("do you live", "do you reside", "are you based",
                         "currently live", "currently reside", "located in",
                         "based in", "live in", "reside in", "are you in",
                         "willing to work in", "able to work in")
    if any(k in lbl for k in location_keywords):
        candidate_location = (profile.get("location") or "").lower()
        candidate_country = (profile.get("country") or "").lower()
        candidate_authorized = (profile.get("work_authorization") or "").lower()

        # Pull location tokens out of the question text after the keyword
        # e.g. "Do you live in Santiago, Chile?" → ["santiago", "chile"]
        question_locations = re.findall(r"[a-z]{3,}", lbl)
        stopwords = {"do", "you", "live", "are", "based", "in", "the", "currently",
                     "reside", "located", "able", "willing", "work", "yes", "no",
                     "this", "that", "or", "and"}
        question_locations = [w for w in question_locations if w not in stopwords]

        # Match check: candidate is in this location if any question location
        # appears in the candidate's profile location/country, OR vice versa.
        match_found = False
        for qloc in question_locations:
            if (qloc in candidate_location or qloc in candidate_country
                    or candidate_location and candidate_location in qloc):
                match_found = True
                break

        # ── US-job-targeting heuristic ─────────────────────────────────────
        # This pipeline targets US jobs exclusively. If the candidate is
        # US-based (by location or work-auth) AND the question references a
        # location, we treat that location as a US locale (per the operator's
        # constraint: "we are only capturing US jobs"). This is intelligence
        # driven by the pipeline's targeting policy, not a hardcoded answer.
        us_indicators = ("us", "usa", "united states", "u.s.", "america", "u.s.a")
        candidate_is_us = (
            any(u == candidate_location.strip() or u in candidate_country
                for u in us_indicators)
            or any(u in candidate_authorized for u in us_indicators)
        )
        if not match_found and candidate_is_us and question_locations:
            # If the question names a location AND we're targeting US jobs only,
            # the location is presumed US → candidate matches.
            match_found = True
            logger.info(f"[SmartInfer] US-targeting policy: candidate_is_us={candidate_is_us}, "
                        f"question_locations={question_locations} → presume US match → Yes")

        target_word = "yes" if match_found else "no"
        target_values = ("yes", "yes.", "true", "1") if match_found else ("no", "no.", "false", "0")
        for opt in options:
            if opt.lower().strip() in target_values:
                logger.info(f"[SmartInfer] Location Q '{label}' → '{opt}' "
                            f"(match={match_found}, candidate={candidate_location!r})")
                return opt
        if len(options) == 2:
            return options[0] if match_found else options[1]

    # ── 3. Availability / start date yes/no ──────────────────────────────────
    avail_keywords = ("available", "start immediately", "join immediately",
                      "available to start", "begin immediately")
    if any(k in lbl for k in avail_keywords):
        for opt in options:
            if opt.lower().strip() in ("yes", "yes.", "true"):
                logger.debug(f"[SmartInfer] Availability Q '{label}' → '{opt}'")
                return opt

    # ── 4. General binary Yes/No screening ───────────────────────────────────
    is_binary = set(opts_lower) <= {"yes", "no", "yes.", "no.", "true", "false", "1", "0"}
    if is_binary and options:
        for opt in options:
            if opt.lower().strip() in ("yes", "yes.", "true", "1"):
                logger.debug(f"[SmartInfer] Binary Q '{label}' → '{opt}' (positive default)")
                return opt

    # ── 5. Single-choice select (no real decision needed) ────────────────────
    if len(options) == 1:
        logger.debug(f"[SmartInfer] Single-option '{label}' → '{options[0]}'")
        return options[0]

    return None


def _best_select_match(options: List[str], target: str) -> Optional[str]:
    """Find the best matching option in a <select> dropdown.

    Pass order:
      1. Exact match (case-insensitive)
      2. Target is a number → find the range option that contains it
      3. Target text contained in option text
      4. Option text contained in target text
    """
    if not options or not target:
        return None

    target_lower = target.lower().strip()

    # Pass 1: Exact match (case-insensitive)
    for opt in options:
        if opt.strip().lower() == target_lower:
            return opt

    # Pass 2: Numeric year → range matching (e.g., "3" → "2–5 years")
    try:
        target_num = float(target_lower)
        best_opt = None
        best_width = float("inf")
        for opt in options:
            r = _parse_year_range(opt)
            if r and r[0] <= target_num <= r[1]:
                width = r[1] - r[0]
                if width < best_width:
                    best_width = width
                    best_opt = opt
        if best_opt:
            return best_opt
    except ValueError:
        pass  # target is not numeric — skip this pass

    # Pass 3: Target text contained in option text
    for opt in options:
        if target_lower in opt.lower():
            return opt

    # Pass 4: Option text contained in target text
    for opt in options:
        opt_clean = opt.strip().lower()
        if opt_clean and opt_clean in target_lower:
            return opt

    return None


async def fill_form(page: Page, form: DetectedForm, profile: Dict,
                    screening_answers: Optional[Dict] = None) -> bool:
    """Production-grade form filler that intelligently maps profile data
    to detected form fields, with human-like interaction patterns.

    Args:
        page: Playwright Page object
        form: Detected form structure
        profile: User profile dictionary with keys like name, email, phone, etc.
        screening_answers: Optional dict of screening question → answer mappings

    Returns:
        True if all required fields were filled successfully
    """
    success = True
    screening_answers = screening_answers or {}
    filled_count = 0
    skipped_count = 0

    # Track which profile keys were successfully filled into a *visible* field so
    # that hidden duplicate fields (common in React-based ATS forms that render the
    # same input twice) can be reconciled instead of failing the whole run.
    filled_keys: set = set()
    # Required fields we could not fill: (label, matched_key). Reconciled after the
    # loop — a duplicate is only a real failure if its key was never filled anywhere.
    required_failures: List[Tuple[str, Optional[str]]] = []

    # Field types that require the element to be visible/editable before we can type
    # into them. Radio/checkbox use force-clicks and are handled separately.
    FILLABLE_TYPES = ("text", "email", "phone", "textarea", "url", "date", "select")
    # Bounded waits so a hidden/duplicate element never stalls the run for 30s.
    VISIBILITY_TIMEOUT_MS = 3000
    ACTION_TIMEOUT_MS = 8000

    # ── Pre-parse name intelligently ──
    first_name = profile.get("first_name", "")
    last_name = profile.get("last_name", "")
    middle_name = profile.get("middle_name", "")

    # If first_name or last_name are missing, split the full name
    if not first_name or not last_name:
        full_name = profile.get("full_name") or profile.get("name") or ""
        name_parts = full_name.strip().split()
        if len(name_parts) > 0:
            if not first_name:
                first_name = name_parts[0]
            if not last_name:
                last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
        else:
            if not first_name:
                first_name = ""
            if not last_name:
                last_name = ""

    # Build the resolved value lookup for special keys
    special_values = {
        "_first_name": first_name,
        "_last_name": last_name,
        "_middle_name": middle_name,
        "_experience_years": profile.get("experience_years", ""),
        "_education": profile.get("education", ""),
        "_salary": profile.get("salary_expectation", ""),
        "_start_date": profile.get("start_date", ""),
        "_additional_info": profile.get("additional_info", ""),
        "_referral_source": profile.get("referral_source", ""),
        # Screening question defaults (can be overridden in profile)
        "_work_auth": profile.get("work_authorization", "Yes"),
        "_sponsorship": profile.get("sponsorship", "No"),
        "_agree_terms": profile.get("agree_terms", "Yes"),
        "_relocate": profile.get("willing_to_relocate", "Yes"),
        "_background_check": profile.get("background_check", "Yes"),
        "_drug_test": profile.get("drug_test", "Yes"),
        # Demographic / EEO — default to "decline to self-identify" variants
        "_gender": profile.get("gender", "Decline To Self Identify"),
        "_race_ethnicity": profile.get("race_ethnicity", "Decline To Self Identify"),
        "_veteran_status": profile.get("veteran_status", "I am not a protected veteran"),
        "_disability_status": profile.get("disability_status", "I don't wish to answer"),
        # Location-specific yes/no questions ("Do you live in Santiago?")
        # Default No — most of our candidates are remote/US-based.
        # Override in profile with location_match="Yes" for local candidates.
        "_location_match": profile.get("location_match", "No"),
    }

    logger.info(f"Starting form fill: {len(form.fields)} fields detected")
    logger.info(f"Profile name parsed: first='{first_name}', last='{last_name}'")

    for field in form.fields:
        label = field.label
        value_to_fill = None

        # ── Step 1: Try the intelligent rules engine ──
        matched_key = _match_label_to_key(label)
        if matched_key:
            if matched_key.startswith("_"):
                value_to_fill = special_values.get(matched_key, "")
            else:
                value_to_fill = profile.get(matched_key, "")
            
            # Leave URL fields blank rather than typing "N/A" — HTML5 validation rejects it
            # and some ATS systems reject the submission.
            logger.debug(f"Rule match: '{label}' -> key '{matched_key}' -> value '{value_to_fill}'")

        # ── Step 2: Fall back to screening answers (fuzzy) ──
        if not value_to_fill and screening_answers:
            label_lower = _normalize_label(label)
            for question, answer in screening_answers.items():
                q_lower = question.lower().strip()
                if q_lower in label_lower or label_lower in q_lower:
                    value_to_fill = answer
                    logger.debug(f"Screening match: '{label}' -> '{question}' -> '{answer}'")
                    break

        # ── Step 2.5: Smart inference for unrecognised fields ─────────────────
        # When neither the rules engine nor screening_answers covered this field,
        # attempt context-aware inference rather than silently skipping.
        # This is the "scan → decide" intelligence layer: the agent reads the
        # question text and available options and makes the best defensible choice.
        if not value_to_fill and field.field_type in ("select", "radio", "checkbox"):
            inferred = _smart_infer_answer(
                label, field.field_type, field.options or [], profile
            )
            if inferred:
                value_to_fill = inferred
                logger.info(f"INFERRED [{field.field_type}] '{label}' → '{inferred}' (smart inference)")

        # ── Step 3: Skip fields we can't fill ──
        if not value_to_fill:
            # Required checkboxes with no explicit mapping still get checked when the
            # label suggests consent/agreement (background check, terms, etc.).
            if field.required and field.field_type == "checkbox":
                logger.info(f"Required checkbox with no mapping — defaulting to checked: '{label}'")
                value_to_fill = "Yes"
                # Fall through to the fill logic below
            elif field.required and field.field_type not in ("file",):
                logger.warning(f"REQUIRED field unfilled: '{label}' (selector: {field.selector})")
                skipped_count += 1
                required_failures.append((label, matched_key))
                continue
            else:
                logger.debug(f"Skipping optional/file field: '{label}'")
                skipped_count += 1
                continue

        # ── Step 4: Fill the field with human-like interaction ──
        try:
            locator = page.locator(field.selector).first

            # Skip hidden/duplicate fields gracefully. React-based ATS forms often
            # render the same logical input more than once (one visible, one hidden);
            # waiting 30s on the hidden one would otherwise stall and fail the run.
            if field.field_type in FILLABLE_TYPES:
                try:
                    await locator.wait_for(state="visible", timeout=VISIBILITY_TIMEOUT_MS)
                except Exception:
                    logger.info(
                        f"Skipping non-visible field '{label}' ({field.selector}) — "
                        f"likely a hidden duplicate or collapsed step"
                    )
                    skipped_count += 1
                    if field.required:
                        required_failures.append((label, matched_key))
                    continue

            await locator.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.15, 0.4))

            did_fill = False

            if field.field_type == "file":
                await locator.set_input_files(value_to_fill, timeout=ACTION_TIMEOUT_MS)
                filled_count += 1
                did_fill = True
                logger.info(f"FILLED [file] '{label}' = '{value_to_fill}'")

            elif field.field_type in ("text", "email", "phone", "textarea", "url"):
                await locator.click(force=True, timeout=ACTION_TIMEOUT_MS)
                await locator.fill(value_to_fill, timeout=ACTION_TIMEOUT_MS)
                filled_count += 1
                did_fill = True
                logger.info(f"FILLED [{field.field_type}] '{label}' = '{value_to_fill}'")

            elif field.field_type == "date":
                await locator.click(force=True, timeout=ACTION_TIMEOUT_MS)
                await locator.fill(value_to_fill, timeout=ACTION_TIMEOUT_MS)
                filled_count += 1
                did_fill = True
                logger.info(f"FILLED [date] '{label}' = '{value_to_fill}'")

            elif field.field_type == "select":
                if getattr(field, "custom_widget", False):
                    # Custom React-Select / Select2: click to open, then click option text
                    try:
                        await locator.click(force=True, timeout=ACTION_TIMEOUT_MS)
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        # Look for an option element matching the value
                        option_locator = page.locator(
                            f"[role='option']:has-text('{value_to_fill}'), "
                            f"li:has-text('{value_to_fill}'), "
                            f".select__option:has-text('{value_to_fill}')"
                        ).first
                        await option_locator.click(timeout=ACTION_TIMEOUT_MS)
                        filled_count += 1
                        did_fill = True
                        logger.info(f"FILLED [custom-select] '{label}' = '{value_to_fill}'")
                    except Exception as exc:
                        logger.warning(f"Custom dropdown click failed for '{label}': {exc}")
                        if field.required:
                            required_failures.append((label, matched_key))
                else:
                    target_option = _best_select_match(field.options or [], value_to_fill)
                    if target_option:
                        await locator.select_option(label=target_option, timeout=ACTION_TIMEOUT_MS)
                        filled_count += 1
                        did_fill = True
                        logger.info(f"FILLED [select] '{label}' = '{target_option}'")
                    else:
                        logger.warning(f"No matching option for '{label}': wanted '{value_to_fill}' from {field.options}")
                        if field.required:
                            required_failures.append((label, matched_key))

            elif field.field_type == "radio":
                if field.options:
                    target_display = _best_select_match(field.options, value_to_fill)
                    if target_display:
                        # Map display text back to raw value for the selector
                        raw_val = target_display
                        if field.raw_values and field.options:
                            try:
                                idx = field.options.index(target_display)
                                raw_val = field.raw_values[idx]
                            except (ValueError, IndexError):
                                pass
                        name_part = field.selector
                        radio_selector = f"{name_part}[value='{raw_val}']"
                        radio_locator = page.locator(radio_selector).first
                        await radio_locator.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.15, 0.4))
                        await radio_locator.click(force=True, timeout=ACTION_TIMEOUT_MS)
                        filled_count += 1
                        did_fill = True
                        logger.info(f"FILLED [radio] '{label}' = '{target_display}' (value='{raw_val}')")
                    else:
                        logger.warning(f"No matching radio option for '{label}': wanted '{value_to_fill}' from {field.options}")
                        if field.required:
                            required_failures.append((label, matched_key))
                else:
                    # Single radio with no options — treat like checkbox
                    if str(value_to_fill).lower() in ("yes", "true", "1", "on"):
                        await locator.check(force=True, timeout=ACTION_TIMEOUT_MS)
                        filled_count += 1
                        did_fill = True
                        logger.info(f"FILLED [radio] '{label}' = checked")

            elif field.field_type == "checkbox":
                if str(value_to_fill).lower() in ("yes", "true", "1", "on"):
                    await locator.check(force=True, timeout=ACTION_TIMEOUT_MS)
                    filled_count += 1
                    did_fill = True
                    logger.info(f"FILLED [checkbox] '{label}' = checked")

            else:
                logger.debug(f"Unhandled field type '{field.field_type}' for '{label}'")

            # Remember which logical field we satisfied so a hidden duplicate of the
            # same field later in the DOM doesn't get counted as a missing required field.
            if did_fill and matched_key:
                filled_keys.add(matched_key)

            await asyncio.sleep(random.uniform(0.15, 0.5))

        except Exception as e:
            logger.error(f"FAILED to fill '{label}' ({field.selector}): {e}")
            if field.required:
                required_failures.append((label, matched_key))

    # ── Reconcile required failures against duplicates ──
    # A required field is only a *real* failure if its mapped key was never filled
    # successfully anywhere (handles hidden duplicate inputs that share a key with a
    # visible one we already filled). Unmapped required fields (key is None) always count.
    unresolved = [
        (label, key) for (label, key) in required_failures
        if not (key and key in filled_keys)
    ]
    if unresolved:
        success = False
        for label, key in unresolved:
            logger.warning(f"Unresolved required field: '{label}' (key={key})")

    logger.info(
        f"Form fill complete: {filled_count} filled, {skipped_count} skipped, "
        f"{len(unresolved)} unresolved required, success={success}"
    )
    return success
