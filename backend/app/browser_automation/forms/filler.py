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
    # Name fields — specific before generic
    (["first name", "fname", "given name", "first_name", "firstname"],               "_first_name"),
    (["last name", "lname", "surname", "family name", "last_name", "lastname"],      "_last_name"),
    (["middle name", "middle_name"],                                                  "_middle_name"),
    (["full name", "your name", "applicant name", "candidate name", "name"],          "name"),

    # Contact fields
    (["email address", "email", "e-mail", "email_address"],                           "email"),
    (["phone number", "phone", "telephone", "mobile", "cell", "contact number"],      "phone"),

    # Location fields
    (["current location", "location", "city", "city, state", "address", "zip"],       "location"),
    (["state", "province"],                                                            "state"),
    (["country"],                                                                      "country"),

    # Online presence
    (["linkedin", "linkedin profile", "linkedin url"],                                 "linkedin_url"),
    (["website", "portfolio", "personal website", "portfolio / website", 
     "portfolio/website", "personal site", "github"],                                  "website"),

    # Professional details
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

    # Dates
    (["start date", "earliest start date", "available start date",
     "date available", "availability"],                                                "_start_date"),

    # Cover letter / Additional
    (["additional information", "additional info", "message to hiring manager",
     "cover letter text", "additional comments", "notes",
     "additional information / message to hiring manager",
     "tell us why"],                                                                   "_additional_info"),

    # Referral source
    (["how did you hear", "heard about", "referral source", "source"],                "_referral_source"),

    # Screening questions (common on Indeed/Glassdoor/LinkedIn)
    (["authorized to work", "work authorization", "legally authorized",
     "eligible to work", "right to work"],                                             "_work_auth"),
    (["require sponsorship", "sponsorship", "visa sponsorship",
     "need sponsorship", "immigration sponsorship"],                                   "_sponsorship"),
    (["certify", "agree to", "terms and conditions", "terms & conditions",
     "i certify", "privacy policy", "acknowledge"],                                    "_agree_terms"),
    (["willing to relocate", "open to relocation", "relocate"],                        "_relocate"),
    (["background check", "consent to background"],                                    "_background_check"),
    (["drug test", "drug screening"],                                                  "_drug_test"),
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


def _best_select_match(options: List[str], target: str) -> Optional[str]:
    """Find the best matching option in a <select> dropdown.
    Tries exact match first, then containment, then fuzzy."""
    if not options or not target:
        return None

    target_lower = target.lower().strip()

    # Pass 1: Exact match (case-insensitive)
    for opt in options:
        if opt.strip().lower() == target_lower:
            return opt

    # Pass 2: Target contained in option text
    for opt in options:
        if target_lower in opt.lower():
            return opt

    # Pass 3: Option text contained in target
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
            
            if matched_key == "linkedin_url" and not value_to_fill:
                value_to_fill = "N/A"
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

        # ── Step 3: Skip fields we can't fill ──
        if not value_to_fill:
            if field.required and field.field_type not in ("file", "checkbox"):
                logger.warning(f"REQUIRED field unfilled: '{label}' (selector: {field.selector})")
                skipped_count += 1
                required_failures.append((label, matched_key))
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

            # Scroll into view first
            await locator.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.3, 0.8))

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
                target_option = _best_select_match(field.options or [], value_to_fill)
                if target_option:
                    await page.select_option(field.selector, label=target_option, timeout=ACTION_TIMEOUT_MS)
                    filled_count += 1
                    did_fill = True
                    logger.info(f"FILLED [select] '{label}' = '{target_option}'")
                else:
                    logger.warning(f"No matching option for '{label}': wanted '{value_to_fill}' from {field.options}")
                    if field.required:
                        required_failures.append((label, matched_key))

            elif field.field_type == "radio":
                # Radio groups have options like ["Yes", "No"]
                # value_to_fill should match one of the options
                if field.options:
                    target_val = _best_select_match(field.options, value_to_fill)
                    if target_val:
                        # Click the specific radio: input[name='...'][value='...']
                        name_part = field.selector  # e.g., input[name='work_authorization']
                        radio_selector = f"{name_part}[value='{target_val}']"
                        radio_locator = page.locator(radio_selector).first
                        await radio_locator.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.2, 0.5))
                        await radio_locator.click(force=True, timeout=ACTION_TIMEOUT_MS)
                        filled_count += 1
                        did_fill = True
                        logger.info(f"FILLED [radio] '{label}' = '{target_val}'")
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

            # Small pause between fields (human cadence)
            await asyncio.sleep(random.uniform(0.3, 1.0))

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
