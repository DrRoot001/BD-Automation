import asyncio
import random
import logging
import re
from playwright.async_api import Page
from .models import DetectedForm, FormField
from . import memory as field_memory
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Human-Like Typing Engine
# ─────────────────────────────────────────────────────────────────────────────

async def _commit_react_select_widget(page: Page, locator, value: str) -> bool:
    """Drive a react-select (Greenhouse / Ashby / Workday) custom dropdown.

    Mirrors the proven sequence from diag_two_in_sequence.py:
      1. page.evaluate finds wrapper via getElementById, dispatches mousedown+mouseup
      2. Focus the input via page.locator(#id)
      3. page.keyboard.type to filter / materialize options
      4. page.evaluate finds the right option by id prefix and dispatches
         mousedown+mouseup+click (the click is critical — without it the
         option never commits)
    """
    field_id = await locator.get_attribute("id") or ""
    if not field_id:
        logger.warning(f"[react-select] no field_id on locator — value='{value}'")
        return False

    open_info = await page.evaluate(
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
    if not open_info:
        return False
    await asyncio.sleep(0.5)

    try:
        await page.locator(f"#{field_id}").first.focus()
        await page.keyboard.type(value, delay=30)
        await asyncio.sleep(0.4)
    except Exception as exc:
        logger.debug(f"[react-select] type failed for #{field_id}: {exc}")

    option_id_prefix = f"react-select-{field_id}-option-" if field_id else ""

    # If typing FILTERED OUT all options (e.g. "Prefer not to answer" doesn't
    # match any Gender option), clear the input so we can see and decline-
    # match against the FULL option list.
    if option_id_prefix:
        current_options_count = await page.evaluate(
            "(prefix) => document.querySelectorAll('[id^=\"' + prefix + '\"]').length",
            option_id_prefix,
        )
        if current_options_count == 0:
            logger.debug(f"[react-select] typed value filtered to 0 options; clearing input")
            try:
                await page.locator(f"#{field_id}").first.focus()
                # Press backspace enough times to clear whatever we typed
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
            // STRICT scoping: when we know our dropdown's option-id prefix,
            // ONLY consider options whose id starts with that prefix. Never
            // fall back to a global [role=option] query — the page may have
            // other widgets (intl-tel-input phone country picker, etc.) that
            // also use role=option, and matching their options would assign
            // wrong values silently (e.g. "Afghanistan" into the Gender field).
            let opts;
            if (prefix) {
                opts = Array.from(document.querySelectorAll('[id^="' + prefix + '"]'));
            } else {
                opts = Array.from(document.querySelectorAll(
                    '.select__menu .select__option, .react-select__menu .react-select__option, '
                    + '.select__menu [role="option"], .react-select__menu [role="option"]'
                ));
            }
            if (opts.length === 0) return null;  // menu not yet open; caller falls back to Enter
            if (declineIntent) {
                const decline = opts.find(o => declineOptionPat.test((o.textContent||'')));
                if (decline) return decline.id || null;
            }
            const exact = opts.find(o => (o.textContent||'').trim().toLowerCase() === wantLc);
            if (exact) return exact.id || null;
            const starts = opts.find(o => (o.textContent||'').trim().toLowerCase().startsWith(wantLc));
            if (starts) return starts.id || null;
            const contains = opts.find(o => (o.textContent||'').trim().toLowerCase().includes(wantLc));
            if (contains) return contains.id || null;
            return opts[0].id || null;
        }""",
        {"prefix": option_id_prefix, "want": value},
    )
    logger.debug(f"[react-select] chosen_id for #{field_id} value='{value}': {chosen_id}")
    if chosen_id:
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
            # Read back the committed value for debugging
            committed = await page.evaluate(
                """(id) => {
                    const el = document.getElementById(id);
                    if (!el) return '';
                    const ctrl = el.closest('.select__control');
                    const sv = ctrl ? ctrl.querySelector('.select__single-value') : null;
                    return sv ? sv.textContent.trim() : '';
                }""",
                field_id,
            )
            logger.debug(f"[react-select] post-click read for #{field_id}: {committed!r}")
            return True

    try:
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)
        return True
    except Exception:
        return False


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

    # ── 0. Legal-restriction questions ("non-compete", "currently bound by") ──
    # Default to "No" — candidates without active legal restrictions can apply
    # freely. This is the safe positive answer for both candidate and employer.
    restriction_keywords = (
        "non-compete", "non compete", "noncompete", "currently bound",
        "non-solicit", "any agreements that may restrict",
        "any agreement that may restrict", "restrict your ability to work",
        "restrictive covenant", "garden leave",
    )
    if any(k in lbl for k in restriction_keywords):
        logger.debug(f"[SmartInfer] legal-restriction question '{label}' -> 'No'")
        return "No"

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


def _ensure_profile_defaults(profile: Dict) -> Dict:
    """Mirror of llm_filler._enrich_profile for the regex path so the rules
    engine can fill 'current_title' / 'current_company' deterministically
    instead of leaving the field unresolved when M3 hasn't backfilled those
    columns on the candidates table yet.
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
        elif any(t in stack for t in ("kubernetes", "terraform", "devops", "sre")):
            base = "DevOps Engineer"
        elif any(t in stack for t in ("pytorch", "tensorflow", "ml ", "machine learning")):
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
    return p


async def fill_form(page: Page, form: DetectedForm, profile: Dict,
                    screening_answers: Optional[Dict] = None,
                    candidate_id: Optional[str] = None) -> bool:
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
    profile = _ensure_profile_defaults(profile)
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
        answer_source = None  # for memory provenance

        # ── Step 0: Check field memory (learn-from-past-runs layer) ──────
        # If we've successfully answered this label before AND the answer is
        # still a valid option (when options are constrained), reuse it.
        # Skip memory for file inputs (path varies per run) and for fields
        # where the profile has an explicit value (let profile override).
        if field.field_type not in ("file",):
            remembered = field_memory.recall(label, field.field_type, field.options, candidate_id=candidate_id)
            if remembered:
                value_to_fill = remembered
                answer_source = "memory"
                matched_key = None  # don't re-fetch from profile

        # ── Step 1: Try the intelligent rules engine ──
        if not value_to_fill:
            matched_key = _match_label_to_key(label)
            if matched_key:
                # BUG C fix: yes/no underscore-prefixed defaults
                # ("_work_auth", "_relocate", "_agree_terms", etc.) must NEVER
                # be written into long free-text fields. A label like
                # "What is the address from which you plan on working? If you
                # would need to relocate, please type 'relocating'." matched
                # "relocate" → "_relocate" → filled "Yes" into an address text
                # box. Confine those defaults to select/radio/checkbox AND
                # short text fields (boolean Yes/No questions are <=80 chars).
                BOOLEAN_KEYS = {
                    "_work_auth", "_sponsorship", "_agree_terms", "_relocate",
                    "_background_check", "_drug_test", "_location_match",
                }
                is_long_free_text = (
                    field.field_type in ("text", "textarea")
                    and len(label) > 80
                )
                if matched_key in BOOLEAN_KEYS and is_long_free_text:
                    logger.debug(
                        f"Rule match suppressed: '{label}' -> '{matched_key}' "
                        f"would write yes/no into a long free-text field"
                    )
                    matched_key = None
                elif matched_key.startswith("_"):
                    value_to_fill = special_values.get(matched_key, "")
                else:
                    value_to_fill = profile.get(matched_key, "")
                if value_to_fill:
                    answer_source = "rules"
                logger.debug(f"Rule match: '{label}' -> key '{matched_key}' -> value '{value_to_fill}'")

        # ── Step 2: Fall back to screening answers (fuzzy) ──
        if not value_to_fill and screening_answers:
            label_lower = _normalize_label(label)
            for question, answer in screening_answers.items():
                q_lower = question.lower().strip()
                if q_lower in label_lower or label_lower in q_lower:
                    value_to_fill = answer
                    answer_source = "screening"
                    logger.debug(f"Screening match: '{label}' -> '{question}' -> '{answer}'")
                    break

        # ── Step 2.5: Smart inference for unrecognised fields ─────────────────
        if not value_to_fill and field.field_type in ("select", "radio", "checkbox"):
            inferred = _smart_infer_answer(
                label, field.field_type, field.options or [], profile
            )
            if inferred:
                value_to_fill = inferred
                answer_source = "smart_infer"
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
                try:
                    field_memory.record_failure(
                        label, field.field_type, field.options, None,
                        "no_value_resolved (rules/screening/smart_infer all empty)",
                        candidate_id=candidate_id,
                    )
                except Exception:
                    pass
                continue
            else:
                logger.debug(f"Skipping optional/file field: '{label}'")
                skipped_count += 1
                continue

        # ── Step 4: Fill the field with human-like interaction ──
        try:
            locator = page.locator(field.selector).first

            # Scroll FIRST, then check visibility. A field that's below the
            # fold is "not visible" to Playwright's visibility check even
            # though it's a perfectly legitimate required input — we were
            # mis-classifying off-viewport fields as hidden duplicates and
            # failing the run. Scrolling first eliminates that false negative.
            if field.field_type in FILLABLE_TYPES:
                try:
                    await locator.scroll_into_view_if_needed(timeout=VISIBILITY_TIMEOUT_MS)
                except Exception:
                    pass
                try:
                    await locator.wait_for(state="visible", timeout=VISIBILITY_TIMEOUT_MS)
                except Exception:
                    # Genuinely hidden / display:none — likely a React duplicate
                    # or a collapsed multi-step. Skip without claiming required-fail
                    # if a sibling with the same key was already filled.
                    logger.info(
                        f"Skipping non-visible field '{label}' ({field.selector}) — "
                        f"likely a hidden duplicate or collapsed step"
                    )
                    skipped_count += 1
                    if field.required:
                        required_failures.append((label, matched_key))
                    continue

            await asyncio.sleep(random.uniform(0.15, 0.4))

            did_fill = False

            if field.field_type == "file":
                await locator.set_input_files(value_to_fill, timeout=ACTION_TIMEOUT_MS)
                filled_count += 1
                did_fill = True
                logger.info(f"FILLED [file] '{label}' = '{value_to_fill}'")

            elif field.field_type in ("text", "email", "phone", "textarea", "url"):
                # React-controlled inputs: use the native-setter trick so
                # React's internal _valueTracker registers the value. Plain
                # locator.fill() writes the DOM but React rerenders later
                # and clears the field if the value tracker is out of sync.
                try:
                    await locator.click(force=True, timeout=ACTION_TIMEOUT_MS)
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
                    value_to_fill,
                )
                if not committed:
                    try:
                        await locator.fill(value_to_fill, timeout=ACTION_TIMEOUT_MS)
                    except Exception:
                        pass
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
                    # CRITICAL: if a previous filler pass already set this
                    # dropdown, do NOT re-open it — re-opening a filled
                    # react-select clears the displayed value transiently and
                    # any race condition during the re-click loses it. Read
                    # the current .select__single-value and skip if present.
                    try:
                        existing = await locator.evaluate("""el => {
                            const ctrl = el.closest('.select__control, .react-select__control');
                            if (!ctrl) return '';
                            const sv = ctrl.querySelector('.select__single-value, .react-select__single-value');
                            return sv ? sv.textContent.trim() : '';
                        }""")
                    except Exception:
                        existing = ""
                    if existing:
                        logger.info(f"SKIPPED [custom-select] '{label}' — already filled with '{existing}'")
                        filled_count += 1
                        did_fill = True
                    else:
                        try:
                            ok = await _commit_react_select_widget(page, locator, value_to_fill)
                            if ok:
                                filled_count += 1
                                did_fill = True
                                logger.info(f"FILLED [custom-select] '{label}' = '{value_to_fill}'")
                            elif field.required:
                                required_failures.append((label, matched_key))
                        except Exception as exc:
                            logger.warning(f"Custom dropdown click failed for '{label}': {exc}")
                            try:
                                await page.keyboard.press("Escape")
                            except Exception:
                                pass
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

            if did_fill and matched_key:
                filled_keys.add(matched_key)

            # ── LEARN: record this answer for future runs ───────────────────
            # Only remember non-file fills, where we have a concrete value and
            # a defensible source (rules / smart_infer / screening / profile).
            # Don't store memory entries for memory recalls (already known).
            if did_fill and answer_source and answer_source != "memory" and field.field_type != "file":
                try:
                    field_memory.remember(label, field.field_type, str(value_to_fill), answer_source, candidate_id=candidate_id)
                except Exception as mem_exc:
                    logger.debug(f"[Memory] remember failed (non-fatal): {mem_exc}")

            await asyncio.sleep(random.uniform(0.15, 0.5))

        except Exception as e:
            logger.error(f"FAILED to fill '{label}' ({field.selector}): {e}")
            if field.required:
                required_failures.append((label, matched_key))
                try:
                    field_memory.record_failure(
                        label, field.field_type, field.options,
                        str(value_to_fill) if value_to_fill else None,
                        f"exception: {type(e).__name__}: {str(e)[:200]}",
                        candidate_id=candidate_id,
                    )
                except Exception:
                    pass

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
