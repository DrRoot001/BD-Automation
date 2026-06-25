"""Static per-platform hints for the AgentLoop.

Each entry captures knowledge that took time to discover empirically:
iframe quirks, reliable selector lists, navigation patterns, custom-widget
behaviour, and success/failure signals.

The agent uses these as a cheat sheet — it still observes the live page and
makes its own decisions, but the hints save several discovery steps and
prevent known failure modes (e.g. filling the phone-country picker instead
of the Country dropdown on Greenhouse).

Usage::

    from .hints import get_platform_hints
    hints = get_platform_hints("greenhouse")
    # hints is a dict ready to inject into the AgentLoop prompt
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Hint schema (plain dicts — no Pydantic overhead for static data)
# ─────────────────────────────────────────────────────────────────────────────
#
# Keys (all optional — omit unknown ones rather than guessing):
#   iframe_selector   str   CSS selector for the iframe wrapping the form
#   container         str   CSS selector scoping the form within the page/frame
#   apply_selectors   list  CSS selectors for the Apply / Easy-Apply button,
#                           ordered best-first
#   submit_selectors  list  CSS selectors for the final Submit button
#   success_patterns  list  Substrings (lower-case) that appear in confirmation
#   url_hint          str   How the URL changes when the form is reached
#   quirks            list  Short strings describing known platform behaviours
#                           the agent should be aware of

_HINTS: Dict[str, Dict[str, Any]] = {

    "greenhouse": {
        "iframe_selector": "#grnhse_iframe",
        "apply_selectors": [
            "a#apply_button",
            "#nav_apply",
            "a[href*='#app']:has-text('Apply')",
            "a:has-text('Apply for this Job')",
            "button:has-text('Apply for this Job')",
            "a:has-text('Apply Now')",
            "[data-qa='btn-apply']",
        ],
        "submit_selectors": [
            "#submit_app",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
            "your application has been received",
            "we have received your application",
        ],
        "url_hint": "Form lives on boards.greenhouse.io or embedded via #grnhse_iframe on company site.",
        "quirks": [
            "Uses react-select custom dropdowns — target the combobox <input> inside, not the wrapper div.",
            "Phone country-picker (.iti widget) looks like a select — do NOT fill it; it is not a form field.",
            "File inputs are hidden; after upload, Greenhouse replaces them with a filename-display element.",
            "EEO / demographic fields (gender, race, veteran, disability) appear at the bottom; pick 'Decline to self-identify' options.",
            "Multi-step: some forms have a 'Continue' button between sections — use next_step action.",
        ],
    },

    "lever": {
        "container": "#application-form",
        "apply_selectors": [
            "a.postings-btn[href*='/apply']",
            "a.template-btn-submit[href*='/apply']",
            "a[href$='/apply']",
            "a:has-text('Apply for this job')",
            "a:has-text('Apply')",
        ],
        "submit_selectors": [
            "button[data-qa='btn-submit']",
            ".template-btn-submit",
            "button[type='submit']",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "thanks for applying",
            "thank you for applying",
            "application submitted",
            "we've received your application",
        ],
        "url_hint": "Job description URL + '/apply' leads directly to the form (e.g. jobs.lever.co/<co>/<id>/apply).",
        "quirks": [
            "Form is plain HTML inside #application-form — no iframe, no react-select.",
            "File inputs are standard <input type=file> — use upload_file action directly.",
            "Single-page form; no multi-step navigation needed.",
            "Name field may be a single 'Full name' input or split first/last.",
        ],
    },

    "ashby": {
        "iframe_selector": "#ashby_embed_iframe",
        "apply_selectors": [
            "a[href*='/application']",
            "a:has-text('Apply for this Job')",
            "button:has-text('Apply for this Job')",
            "button:has-text('Apply Now')",
            ".ashby-job-posting-apply-button",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit application')",
            ".ashby-application-submit-button",
        ],
        "success_patterns": [
            "application received",
            "you've applied",
            "you have applied",
            "thank you for applying",
            "we've received your application",
        ],
        "url_hint": "Job page URL + '/application' leads to the form.",
        "quirks": [
            "React-heavy with Ashby custom select widgets — target combobox inputs by id.",
            "May embed via iframe on company careers pages.",
            "Work-authorization questions use Yes/No radio buttons.",
        ],
    },

    "workday": {
        "apply_selectors": [
            "a[data-automation-id='applyNowButton']",
            "button[data-automation-id='applyNowButton']",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "[aria-label*='Apply']",
        ],
        "submit_selectors": [
            "button[data-automation-id='bottom-navigation-next-button']",
            "button[data-automation-id='pageFooter'] button:has-text('Submit')",
            "button:has-text('Submit')",
            "button:has-text('Next')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
            "your application has been submitted",
        ],
        "quirks": [
            "Heavily multi-step — each section has a Next button. Use next_step for each.",
            "Uses Workday custom components — fields have data-automation-id attributes; prefer those.",
            "File upload uses a custom widget, not a standard file input — look for 'Upload' button.",
            "May require login/account creation before the form appears.",
        ],
    },

    "linkedin": {
        "container": ".jobs-easy-apply-modal",
        "apply_selectors": [
            ".jobs-apply-button",
            "button:has-text('Easy Apply')",
            "[aria-label*='Easy Apply']",
        ],
        "submit_selectors": [
            "button[aria-label='Submit application']",
            "footer button:has-text('Submit application')",
            "button:has-text('Submit application')",
        ],
        "success_patterns": [
            "application submitted",
            "your application was sent",
        ],
        "quirks": [
            "Easy Apply opens a modal (.jobs-easy-apply-modal) — scope all selectors inside it.",
            "Multi-step modal — use next_step to advance through sections.",
            "Some jobs redirect to an external ATS instead of Easy Apply.",
        ],
    },

    "icims": {
        "container": ".iCIMS_MainWrapper",
        "apply_selectors": [
            "a#applyButton",
            "a.iCIMS_Anchor_ApplyOnline",
            "a:has-text('Apply for this job')",
            "a:has-text('Apply for this Job')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply for this job')",
            "input.iCIMS_PrimaryButton[value*='Apply']",
        ],
        "submit_selectors": [
            "input#cp_form_submit_i",
            "input.iCIMS_PrimaryButton[type='submit']",
            "input.iCIMS_PrimaryButton[value='Submit Profile']",
            "input.iCIMS_PrimaryButton[value='Submit']",
            "button.iCIMS_PrimaryButton",
            "button[type='submit']",
        ],
        "success_patterns": [
            "thank you for applying",
            "your application has been submitted",
            "your application was submitted",
            "application received",
            "thanks for your interest",
        ],
        "url_hint": (
            "Hosts: careers.icims.com, <tenant>.icims.com, globalcareers-customerN.icims.com. "
            "Posting URLs look like /jobs/<id>/... — the form is reached via 'Apply for this job'. "
            "Apply click usually leads to an email-consent screen FIRST, then the candidate "
            "profile form (step 1 of 2), then a candidate-questions screen (step 2 of 2)."
        ),
        "quirks": [
            "FLOW IS FORWARD-ONLY: listing → email-consent → candidate profile (step 1) → candidate questions (step 2) → confirmation. NEVER use navigate_url to go BACK to a page you already left — the loop will block it. If you land on a page that looks confusing, scroll and re-read the screenshot; do NOT navigate elsewhere.",
            "RIGHT AFTER you click 'Apply for this job' the URL changes to 'globalcareers-customer<N>.icims.com/jobs/<id>/login' and an EMAIL-CONSENT screen appears inside form#enterEmailForm. Exact field IDs: '#email' (the email input, name='css_loginName'), '#accept_privacy' (the privacy-consent checkbox), '#enterEmailSubmitButton' (the Next submit). The Next button is INITIALLY disabled='' — it only enables AFTER the checkbox is ticked. Order: fill #email first, THEN tick #accept_privacy, THEN click #enterEmailSubmitButton.",
            "The email-consent form has an INVISIBLE hCaptcha attached to onsubmit (sitekey on '.h-captcha[data-sitekey]'). When the Next button is clicked, the form's onsubmit handler calls hcaptcha.execute() and only submits AFTER a token is appended as 'h-captcha-response'. The browser_automation captcha service handles this automatically — but the AI MUST click the visible Next button (not call form.submit() directly) so the hCaptcha flow fires. If submit appears to do nothing, that's the hCaptcha solving in the background; wait 5-10s before re-acting.",
            "If after clicking Apply you see only an email field and a checkbox (no name/phone/resume fields), that IS the consent screen — proceed as above. Don't treat it as a wrong page.",
            "Multi-step wizard. The progress bar lives in '.iCIMS_Steps' with li#Step_profileStep (step 1) and #Step_personQuestionsStep (step 2). After submitting step 1, step 2 loads on the same URL — keep filling and submit again.",
            "Email-consent screen: an email <input> + a single consent checkbox + a 'Next' button. Tick the checkbox BEFORE clicking Next or the form rejects.",
            "Resume upload: '#PortalProfileFields.Resume_File' is a hidden <input type=file>. Trigger it via the visible '.iCIMS_FileFieldButton' (label text 'My Computer'). Avoid the Google Drive / Dropbox / OneDrive buttons — they open OAuth popups.",
            "Login section: '#PersonProfileFields.Login' (username) + '#PersonProfileFields.Password' + '#PersonProfileFields.Password_Confirm'. Password MUST satisfy: ≥8 chars, ≥1 alpha, ≥1 lowercase, ≥1 uppercase, ≥1 digit, ≥1 special. Use the candidate's email as Login if no dedicated username is requested.",
            "Country / State dropdowns are iCIMS custom widgets (class 'dropdown-select', not <select>). To choose: click the '.dropdown-select' anchor → type into '.dropdown-search' → click the matching li.dropdown-result.result-selectable. The native <select> stays hidden; setting its value alone does NOT update the visible widget.",
            "State dropdown is parent-linked to Country (data-ddd-parent-link). Fill Country first, wait for the State list to populate, THEN open State.",
            "'How did you hear about us?' = '#rcf3048' (native <select>). It already has a default 'globalapply' value — overwrite it explicitly if the operator policy demands a specific source.",
            "Recruiter-custom-fields use IDs like '#rcf<N>' (e.g. rcf2008 'Preferred First Name', rcf2092 'Full Legal Name'). Treat the for-attribute label, NOT the id, as authoritative.",
            "Phone group is collection-indexed: '#-1_PersonProfileFields.PhoneType' (select) + '#-1_PersonProfileFields.PhoneNumber' (text). Type='Mobile' for the operator policy.",
            "Address group is collection-indexed too: '#-1_PersonProfileFields.AddressType' / .AddressStreet1 / .AddressCity / .AddressZip / .AddressCountry / .AddressState.",
            "Submit button on step 1 is '<input id=\"cp_form_submit_i\" value=\"Submit Profile\">' — clicking it advances to step 2, not the final confirmation.",
            "iCIMS sometimes opens in an iframe when embedded on a company careers page (in_iframe=1 in the URL). The form itself behaves identically; treat the iframe as the operating context if present.",
            "Cookie / consent banners: OneTrust ('#onetrust-accept-btn-handler') is the most common; click it before scrolling so it does not intercept pointer events.",
        ],
    },

    "indeed": {
        "apply_selectors": [
            # In-Indeed Easy Apply CTA (the only branch we drive ourselves)
            "button#indeedApplyButton",
            "button[data-testid='indeedApplyButton']",
            "button:has-text('Apply with Indeed')",
            "button:has-text('Easily apply')",
            "a:has-text('Easily apply')",
            # External-apply CTAs (we click these, then follow the redirect/popup
            # and delegate to the destination ATS adapter)
            "a:has-text('Apply on company site')",
            "a:has-text('Apply on employer site')",
            "button:has-text('Apply on company site')",
            "a:has-text('Apply now')",
            "button:has-text('Apply now')",
        ],
        # Submit on Indeed job-detail is a no-op — the actual submit lives on
        # SmartApply or the external ATS. Kept for interface compatibility.
        "submit_selectors": [],
        "success_patterns": [],
        "url_hint": (
            "Indeed job-detail URLs look like https://www.indeed.com/viewjob?jk=<key> "
            "or https://<region>.indeed.com/cmp/<co>/jobs/<...>. Two branches: "
            "(a) EASY_APPLY — clicking the blue 'Apply with Indeed' button navigates "
            "to smartapply.indeed.com (same tab). (b) EXTERNAL_APPLY — clicking "
            "'Apply on company site' / 'Apply now' opens the employer ATS in a new "
            "tab or redirects the current tab to a non-indeed.com domain."
        ),
        "quirks": [
            "PRODUCT TARGET IS US CANDIDATES. Default country=United States, US phone format, US ZIP. Never claim US citizenship / work auth / sponsorship / EEO / veteran / disability / clearance unless those fields are explicit in the candidate profile.",
            "Apply-button classification is the FIRST decision on this page. Read the button label BEFORE clicking: 'Apply with Indeed' / 'Easily apply' → EASY_APPLY branch (stay in Indeed, expect smartapply.indeed.com). 'Apply on company site' / 'Apply on employer site' / 'Apply now' → EXTERNAL_APPLY branch (a new tab or non-indeed domain follows; the IndeedAdapter resolves the destination ATS and delegates).",
            "External apply may open in a new tab OR redirect the current tab. The adapter handles popup vs same-tab via page.context.expect_page(); if you (AgentLoop) see the URL change off indeed.com, that IS the external branch — keep going.",
            "If the job-detail page shows 'This job is no longer available' / 'Job expired' / 'Position filled' — abort with reason='JOB_UNAVAILABLE'. Do not retry.",
            "If you see a login wall, CAPTCHA, Cloudflare 'verify you are human', or a security challenge BEFORE the apply button — abort with reason='BLOCKED_HUMAN_REQUIRED'. NEVER attempt to bypass CAPTCHA / reCAPTCHA / hCaptcha / Cloudflare / MFA / email-verification gates on Indeed.",
            "Do not toggle any 'Save my answers for pre-filling', 'Get job alerts', 'Subscribe' or marketing checkboxes unless candidate policy explicitly opts in. Default OFF.",
            "Chrome address autofill can fire during typing on the Indeed location form. Prefer setInputFiles / explicit fill_field over keystroke simulation; if a value other than the candidate's appears, clear and retype.",
        ],
    },

    "smartapply": {
        "url_hint": (
            "Easy Apply form lives on https://smartapply.indeed.com/.../form. "
            "Sequence: location → contact info → resume → (cover letter, optional) "
            "→ employer questions → voluntary self-ID → 'Preparing review' loader "
            "→ 'Review your application' → 'Your application has been submitted!'"
        ),
        "apply_selectors": [],  # already on the form; no separate apply click
        "submit_selectors": [
            "button:has-text('Submit your application')",
            "button:has-text('Submit application')",
            "button[data-testid='indeedApplyButton-submit']",
            "button[type='submit']:has-text('Submit')",
        ],
        # Note: 'Review your application' is the page TITLE that comes BEFORE
        # submit — do NOT treat it as a success signal. Only the post-submit
        # confirmation strings below are valid success markers.
        "success_patterns": [
            "your application has been submitted",
            "application has been submitted",
            "we've sent your application",
            "we have sent your application",
            "thank you for applying",
        ],
        "quirks": [
            "MULTI-STEP, FORWARD-ONLY. Each page has a 'Continue' / 'Review your application' / 'Submit your application' primary button. Use next_step / click — never navigate_url BACK to a page you already passed; the wizard rejects it.",
            "Page identification by visible heading (lowercased): 'add your location' → location step; 'add a resume' → resume step; 'voluntary self identification questions from the employer' → EEO/self-ID consent; 'review your application' → final review (NOT yet submitted); 'preparing review' → transient loader, just wait, do not act.",
            "Location step: country MUST be 'United States'. Fill ZIP, city/state, street from candidate.location data. If country is anything else, click 'Change' and pick US. If candidate has no US address, abort with reason='MISSING_CANDIDATE_DATA'.",
            "Resume step: a previously-uploaded resume card may already be selected. Verify the filename matches the candidate's current resume before clicking Continue; if not, upload the candidate's resume via the file input.",
            "Voluntary self-ID consent: the 'Agree' / consent checkbox is REQUIRED to proceed but the demographic answers underneath are NOT. Tick the consent checkbox only — leave gender/race/veteran/disability blank or 'Prefer not to answer' unless the candidate profile has explicit values.",
            "'Save my answers for pre-filling' checkbox: leave OFF by default. Job-alert opt-in on the review page: also OFF.",
            "'Review your application' is NOT the confirmation page. The application is only submitted AFTER clicking 'Submit your application' on that review page AND seeing the 'Your application has been submitted!' screen.",
            "If a question is required and the candidate profile has no explicit answer (work auth, sponsorship, clearance, salary, years of <tech>, demographics) — abort with reason='UNKNOWN_REQUIRED_QUESTION'. NEVER guess.",
            "reCAPTCHA / 'verify you are human' / Cloudflare challenge appearing mid-flow → abort with reason='BLOCKED_HUMAN_REQUIRED'. No bypass.",
            "DRY_RUN mode: if env DRY_RUN=true OR ENABLE_PRODUCTION_SUBMIT!=true, STOP at the 'Review your application' page. Emit done with confirmation='DRY_RUN_READY_TO_SUBMIT' and the review-page heading screenshot. Do NOT click 'Submit your application'.",
        ],
    },

    "remoterocketship": {
        "apply_selectors": [
            "button[aria-label='Apply']",
            "button:has-text('Apply')",
            "a.link:has-text('Apply')",
            "a:has-text('Apply Now')",
        ],
        # Submit/success keys delegate to whichever ATS the listing links out
        # to (Greenhouse / Lever / Ashby / …). The RR adapter pre-resolves
        # that URL via HTTP before the browser opens, so by the time the
        # loop sees the form it's already on the ATS host.
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
            "your application has been received",
        ],
        "url_hint": (
            "Listing pages live on remoterocketship.com. Each posting's Apply "
            "button is an anchor pointing at the underlying ATS (Greenhouse, "
            "Lever, Ashby, etc.). The adapter pre-resolves that URL via HTTP "
            "and navigates the browser directly to the ATS form — you should "
            "never see remoterocketship.com once the form loads."
        ),
        "quirks": [
            "Listing page has NO form fields — only a job description and an Apply button.",
            "The 'underlying' ATS host determines the actual form layout; treat that ATS's quirks as authoritative.",
            "If you ever land on a remoterocketship.com URL during fill, the HTTP pre-resolve failed — abort or click Apply with the vision agent.",
        ],
    },

    "generic": {
        "apply_selectors": [
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply Now')",
            "[class*='apply']",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Submit')",
            "button:has-text('Apply')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
            "application received",
        ],
        "quirks": [
            "Unknown platform — rely on DOM observation and screenshot to guide actions.",
        ],
    },
}


def get_platform_hints(platform: str) -> Dict[str, Any]:
    """Return hint dict for the given platform, falling back to 'generic'."""
    key = (platform or "generic").lower().strip()
    return _HINTS.get(key, _HINTS["generic"])


def format_hints_for_prompt(hints: Dict[str, Any]) -> str:
    """Render hints as a compact prompt section the LLM can scan quickly."""
    lines: List[str] = []

    if hints.get("iframe_selector"):
        lines.append(f"  iframe: form may live inside {hints['iframe_selector']}")
    if hints.get("container"):
        lines.append(f"  container: form is scoped to {hints['container']}")
    if hints.get("url_hint"):
        lines.append(f"  url: {hints['url_hint']}")

    apply_sels = hints.get("apply_selectors", [])
    if apply_sels:
        lines.append(f"  apply button selectors (try in order): {', '.join(apply_sels[:4])}")

    submit_sels = hints.get("submit_selectors", [])
    if submit_sels:
        lines.append(f"  submit selectors (try in order): {', '.join(submit_sels[:4])}")

    success = hints.get("success_patterns", [])
    if success:
        lines.append(f"  success signals: {' | '.join(success[:3])}")

    quirks = hints.get("quirks", [])
    if quirks:
        lines.append("  known quirks:")
        for q in quirks:
            lines.append(f"    - {q}")

    return "\n".join(lines) if lines else "  (no specific hints for this platform)"
