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
            "EEO / demographic fields (gender, race, veteran, disability) appear at the bottom — answer them using the candidate's DECLARED values in the identity card (do NOT auto-decline): gender inferred from first name, race=South Asian (else Asian), veteran=No, disability=No, transgender=No.",
            "Multi-step: some forms have a 'Continue' button between sections — use next_step action.",
            "On job-boards.greenhouse.io an 'I agree to the terms' checkbox can be required before the Resume upload appears — tick it first.",
            "The EEO/demographic block has exactly 6 fields — count them and verify all 6 are answered before submitting.",
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
            "button.template-btn-submit, input.template-btn-submit",
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
            "Some location/school/company/source fields are TYPE-TO-SEARCH autocomplete, not a fixed dropdown list — the option list is empty until you type into the field. If a combobox's initial option scan comes back empty, still emit fill_field with your target value: the runner types it as a search query first, THEN reads whatever options that search reveals, before picking the closest match.",
            "Resume upload can trigger Ashby's own autofill of name/email/phone/location from the parsed resume a few seconds AFTER you attach the file. If a field you already filled correctly shows a DIFFERENT value on a later turn, that's Ashby's autofill overwriting you — re-fill it with the candidate's correct identity-card value; do not assume the new value is right just because it appeared after your action.",
            "Submit button stays DISABLED (not just unclickable) until every required field — including the resume upload finishing — is complete. If Submit does nothing when clicked, do not repeat-click it: check the FORM STATUS / STILL EMPTY list for what's missing instead.",
            "A red banner reading something like \"flagged as possible spam\" after submit means Ashby's anti-bot rejected it — this is terminal, do not click Submit again.",
            "A message like \"you've already applied\" / \"already submitted an application\" means the candidate has an existing application on file for this job — this is an expected outcome, not a form-filling failure; do not try to work around it.",
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

    "builtin": {
        "apply_selectors": [
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "button:has-text('Easy Apply')",
            "a:has-text('Easy Apply')",
        ],
        # Submit selectors are for the DELEGATED ATS, not Built In itself —
        # Built In's own flow ends at the mini-form + Continue. Once the
        # adapter delegates to the real ATS (iCIMS / Greenhouse / Lever /
        # etc.) the underlying adapter's own submit selectors take over.
        # These stay as a safety net for the "generic" fallback path.
        "submit_selectors": [
            "button:has-text('Submit Application')",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
            "button[type='submit']",
        ],
        "success_patterns": [
            "application submitted",
            "application received",
            "thank you for applying",
            "thanks for applying",
            "we've received your application",
        ],
        "url_hint": (
            "Built In (builtin.com/job/...) is a CLICK-THROUGH JOB BOARD, "
            "NOT a native ATS. The adapter's navigate_to_application does "
            "a 3-field mini-form dance (first-name / last-name / email + "
            "Continue) which opens the REAL ATS (iCIMS / Greenhouse / "
            "Lever / Workable) in a new tab, then re-navigates the "
            "current page to that resolved ATS URL. From that point on, "
            "the target ATS's own adapter drives everything — hints for "
            "the target ATS take over automatically."
        ),
        "quirks": [
            "Built In is a JOB BOARD wrapper, not an ATS. The actual "
            "application form lives on whatever ATS the employer uses "
            "(iCIMS, Greenhouse, Lever, Workable, etc.). The adapter's "
            "navigate_to_application handles the click-through: fills "
            "the 3-field inline mini-form (first-name / last-name / "
            "email), clicks Continue, catches the new-tab handoff, and "
            "re-navigates to that ATS URL. AgentLoop never really sees "
            "the Built In DOM — by the time it starts perceiving, the "
            "page is already on the target ATS.",

            "If the adapter's click-through fails (Continue doesn't open "
            "a new tab within 15s, or the resolved URL isn't a known "
            "ATS host), a generic fallback runs and the AgentLoop's "
            "vision agent has to salvage whatever is on-screen. This is "
            "a best-effort recovery — most Built In postings depend on "
            "the click-through path working.",

            "Compliance policy answers (sponsorship / government "
            "official / gov-official-relative / conflict-of-interest — "
            "all default NO) live in loop.py's _policy_fields and apply "
            "on the target ATS's form. No Built In-specific compliance "
            "wiring is needed here.",
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

    "dice": {
        "container": "main",
        "apply_selectors": [
            # The Easy Apply CTA is clicked by the DiceAdapter BEFORE the loop
            # runs; these are here only for the PageAgent recovery path.
            "apply-button-wc button",
            "button:has-text('Easy apply')",
            "button:has-text('Easy Apply')",
        ],
        "submit_selectors": [
            "button[data-cy='submit-application']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
            "button[type='submit']",
        ],
        "success_patterns": [
            "application submitted",
            "your application has been submitted",
            "application has been submitted",
            "thank you for applying",
            "we've received your application",
        ],
        "url_hint": (
            "Dice Easy Apply is a 3-step wizard. URL paths (NEVER rely on the "
            "application id or query params): wizard = '/job-applications/<id>/wizard', "
            "success = '/job-applications/<id>/wizard/success'. You start on Step 1 "
            "after the adapter has already clicked 'Easy apply' and logged in."
        ),
        "quirks": [
            "FLOW IS FORWARD-ONLY, 2 OR 3 STEPS depending on the employer: Step 1 'Resume & Cover Letter' → [optional 'Additional Information' (questions)] → final 'Review your application' step → Submit → success page. A step indicator like 'Step 1 of 2' / 'Step 1 of 3' or section titles tells you where you are. NEVER navigate_url BACK to a step you already passed — the loop blocks it. To advance, click the primary 'Next' / 'Continue' button; to finish, click 'Submit'.",
            "PRE-FILLED IS NOT CORRECT: Dice pre-fills the resume, Work Authorization and Current Location from the ACCOUNT profile, which may belong to a different person than the candidate you are applying for. A field being filled does NOT mean it is right — verify VALUES, not presence.",
            "STABILITY OVER SPEED: after EVERY action wait for the next element to be visible and the page to settle before acting again. Validate each step before clicking Next. Do not rush.",
            "STEP 1 — RESUME (required): find the resume file input (label 'Upload Resume' or 'Resume') and upload the candidate's resume via the upload_file action. After upload, VERIFY the uploaded filename appears on the page before continuing. If you see 'Upload failed' / 'File too large' / 'Unsupported file', retry the upload ONCE; if it fails again, abort with reason='upload_failure'.",
            "STEP 1 — COVER LETTER (optional): only upload a cover letter if one was provided to you. If no cover letter is available, SKIP it — do not block on it. If uploaded, verify its filename appears.",
            "STEP 1 — before clicking Next: confirm the resume filename is visible AND there are no validation errors on the page. Then click 'Next' and wait for Step 2 to load.",
            "STEP 2 — ADDITIONAL INFORMATION is a DYNAMIC question set. The current observed fields are 'Work Authorization' and 'Current Location', but future jobs may add more (years of experience, current/expected salary, notice period, sponsorship requirement, relocation willingness, remote preference). Classify EVERY visible question by its input type (text, textarea, dropdown, radio, checkbox, autocomplete) and answer it from the candidate profile / pre-resolved screening answers. NEVER hardcode an answer.",
            "WORK AUTHORIZATION is a DROPDOWN. Valid options are exactly: 'US Citizen', 'Green Card Holder', 'H1B', 'OPT', 'TN Visa', 'Other'. Choose the option matching the candidate's work_authorization_type field (already provided in the profile). Pick the closest valid option; never invent a value outside this list.",
            "CURRENT LOCATION is a Google-Places-style AUTOCOMPLETE, not a plain text field. Algorithm: (1) type the candidate's location into the input, (2) WAIT for the suggestion dropdown to appear, (3) click the first suggestion that matches, (4) verify the input still shows the chosen value. If no suggestions appear, clear and retype (up to 2 retries). Just typing without selecting a suggestion often fails validation — you MUST pick a suggestion.",
            "VALIDATION ERRORS: before moving to the next step, scan the page for 'Required', 'This field is required', 'Please enter', 'Invalid value'. If any are present, the step is NOT complete — fix the offending field(s) (fill the missing value / re-select the autocomplete) and only then click Next. Do not advance past unresolved validation errors.",
            "REVIEW STEP (final step before Submit): the page shows cards — Resume, Cover Letter, Work Authorization, Current Location — each with a small edit (pencil) button. VERIFY VALUES before submitting: (1) the Resume filename must contain the CANDIDATE'S name — if it shows another person's name, use its edit control to replace it with the provided resume; (2) if a cover letter was provided to you, the Cover Letter card must NOT say 'No cover letter uploaded' — edit and upload it; (3) Work Authorization must NOT be 'Prefer Not to Answer' — click its pencil, pick the candidate's status in the 'workAuthorization' dropdown (options: 'US Citizen', 'Green Card Holder', 'Have H1 Visa', 'Employment Auth Document', 'TN Permit Holder'), then click 'Update'; (4) Current Location should match the candidate's location when one was provided. This is NOT the success page — the application is only submitted AFTER you click the final Submit button.",
            "SUBMIT: the final action is a large primary button labelled 'Submit', 'Submit Application', or 'Apply'. After clicking, a SUBMITTING state follows (loading spinner / disabled button / network activity / redirect). Wait for it.",
            "SUCCESS is confirmed ONLY when the URL contains '/wizard/success' OR the page text shows 'Application Submitted' (or a similar confirmation). Only then emit done with the confirmation text. Reaching the Review page is NOT success.",
            "Do not toggle marketing / 'save my answers' / job-alert checkboxes unless the candidate policy explicitly opts in. Default them OFF.",
            "Use accessible selectors in your reasoning (role, label, placeholder, text) rather than brittle CSS — Dice's class names are generated and change between builds.",
        ],
    },

    "remoterocketship": {
        "verify_gate_limit": 20,
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

    "remote100k": {
        "verify_gate_limit": 20,
        "apply_selectors": [
            "a:has-text('Apply for This Job')",
            "a:has-text('Apply for this Job')",
            "a:has-text('Apply')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply')",
        ],
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
            "Listing pages live on remote100k.com. Each posting's Apply "
            "button is an anchor pointing at the underlying ATS (Greenhouse, "
            "Lever, Ashby, etc.). The adapter pre-resolves that URL via HTTP "
            "and navigates the browser directly to the ATS form — you should "
            "never see remote100k.com once the form loads."
        ),
        "quirks": [
            "Listing page has NO form fields — only a job description and an Apply button.",
            "The 'underlying' ATS host determines the actual form layout; treat that ATS's quirks as authoritative.",
            "If you ever land on a remote100k.com URL during fill, the HTTP pre-resolve failed — abort or click Apply with the vision agent.",
        ],
    },

    # Careers Page — the hosted employer ATS powered by Manatal (careers-page.com).
    # A single-page application form reached directly or via a job-board external
    # redirect (e.g. Remote100K). Detect by HOST ONLY; never by job id/slug/query.
    "careerspage": {
        "container": "form",
        "apply_selectors": [
            # The .../apply URL usually renders the form directly; these only
            # matter if a listing/description page loads first.
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply Now')",
        ],
        "submit_selectors": [
            # Real Manatal form's primary button is labelled 'Apply'.
            "button:has-text('Apply')",
            "button:has-text('Send Application')",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "button[type='submit']",
        ],
        "success_patterns": [
            "thank you for your application",
            "application submitted",
            "your application has been submitted",
            "we have received your application",
            "we've received your application",
        ],
        "url_hint": (
            "Careers Page (Manatal ATS) form lives on careers-page.com "
            "(path .../job/<id>/apply). Identify the platform by HOST ONLY — "
            "NEVER rely on the job id, company slug, or query params. The whole "
            "form is on ONE page (no wizard). Fields have NO <label> elements — "
            "they are identified by PLACEHOLDER text. Order: Full Name, Phone, "
            "Email, LinkedIn, then Professional Info (native <select>s), Resume "
            "upload, a required terms checkbox, then the 'Apply' button."
        ),
        "quirks": [
            # NOTE: verified against the real Manatal form 2026-07-14 — it differs
            # from the generic spec (single Full Name field, native <select>s,
            # placeholder-only labels, 'N+ year' / 'NN days' option sets).
            "SINGLE-PAGE FORM (Manatal). All fields on one page; fill top-to-bottom, tick the terms checkbox, then click 'Apply' once.",
            "Fields have NO <label> elements — identify them by PLACEHOLDER text: 'Full Name', 'Phone', 'Email', 'LinkedIn Profile', 'Current Company', 'Current Salary', 'Expected Salary', 'Resume'.",
            "NAME: there is a SINGLE 'Full Name' field (placeholder 'Full Name') — NOT separate First/Last. Fill it with the candidate's full name.",
            "CONTACT: Phone, Email, and LinkedIn Profile are text inputs. LinkedIn is optional — leave blank if the candidate has none; never invent one.",
            "All dropdowns on this form are NATIVE <select> elements (NOT custom widgets) — set them with a normal select-by-visible-text; no click-open-then-click-option dance is needed.",
            "YEARS OF EXPERIENCE (required <select>): options read 'No experience', '1+ year', '2+ year', '3+ year', … up to about '10+ year'. Pick the HIGHEST 'N+ year' whose N does not exceed the candidate's years of experience (e.g. 4 yrs -> '4+ year'); use 'No experience' only if the candidate truly has none.",
            "CURRENT COMPANY (text): the candidate's current_company, or their most recent employer from the resume.",
            "SALARY: Current Salary and Expected Salary each have an AMOUNT input plus a CURRENCY <select> and a FREQUENCY <select>. Enter digits only in the amount (strip '$' and commas). CURRENCY is a full currency-name list — pick the option matching 'US Dollar' (USD) by default. FREQUENCY options are Hourly/Daily/Weekly/Monthly/Yearly — default 'Yearly'. If only one salary figure is known, use it for BOTH Current and Expected (standard format fields, not fabricated facts).",
            "NOTICE PERIOD (required <select>): options read 'Immediately', '10 days', '20 days', '30 days', '40 days', … Map the candidate's notice period to the nearest option; if unknown default to '30 days' (~1 month). Treat '2 weeks' as the closest of '10 days'/'20 days'.",
            "RESUME UPLOAD is MANDATORY (file input, placeholder 'Resume'). Upload the candidate's resume via upload_file, then VERIFY the uploaded filename becomes visible.",
            "TERMS/PRIVACY: a REQUIRED checkbox (name 'terms_and_condition', the terms-and-conditions/privacy agreement) MUST be checked before the form will submit. Check it and verify it is checked.",
            "BEFORE SUBMIT: scan the page for validation text ('required', 'invalid', 'please complete') and fix any flagged field.",
            "SUBMIT: click the primary button labelled 'Apply' (there is also a 'Company Website' link — do NOT click that). After clicking, wait for server validation. SUCCESS is confirmed ONLY by the post-submit page text 'Thank you for your application!' — a filled form is NOT success.",
            "Do NOT toggle any marketing / newsletter opt-in checkboxes — only the required terms checkbox.",
        ],
    },

    # CareerPlug (careerplug.com) — a Rails-based ATS. Verified against the real
    # S-R-International D365 form 2026-07-15.
    "careerplug": {
        "apply_selectors": [
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "a:has-text('Apply here')",
            "a:has-text('apply here')",
        ],
        "submit_selectors": [
            "input[type=submit][value='commit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "input[type=submit]",
            "button[type=submit]",
        ],
        "success_patterns": [
            "thank you for applying",
            "application received",
            "your application has been submitted",
            "thanks for applying",
            "we have received your application",
            "we've received your application",
        ],
        "url_hint": (
            "CareerPlug (careerplug.com) Rails ATS. The application form is at "
            ".../jobs/<id>/apps/new (the job URL redirects there). ONE page: "
            "personal info, address, resume/cover upload, free-text screening "
            "questions, then a Google reCAPTCHA and Submit."
        ),
        "quirks": [
            "SINGLE-PAGE Rails form: fill top-to-bottom, solve the reCAPTCHA, submit ONCE.",
            "PERSONAL (required*): First Name, Last Name, Email, Phone — from the candidate profile. Address/City/ZIP and State (native <select>) are optional; fill City/State from the candidate's location when known.",
            "'Current location (city, state)?*' is a REQUIRED free-text field — enter the candidate's city, state.",
            "RESUME: there are file 'Upload File' inputs AND resume/cover-letter TEXTAREAS. Upload the candidate's resume PDF to the first file input (cover letter to the second if provided); you may leave the resume/cover TEXT areas blank once the files are uploaded.",
            "SCREENING QUESTIONS are free-text TEXTAREAS about specific experience (e.g. 'Do you have Microsoft Dynamics 365 …', 'Experience with Power BI / SQL Server / Power Apps …'). Answer each CONCISELY and truthfully from the candidate's resume/profile — one or two sentences with years/context when they have it. Do not leave a REQUIRED* one blank.",
            "STATE is a native <select> whose first option 'State' is the placeholder — pick the candidate's US state.",
            "CAPTCHA: this form uses a Google reCAPTCHA (a .g-recaptcha / #g-recaptcha-response). Solve it with solve_captcha captcha_type=\"recaptcha_v2\" BEFORE submitting. It is NOT a Cloudflare Turnstile — do not classify it as turnstile.",
            "SUBMIT: the primary button submits the Rails form (its value is 'commit'; visible text is usually 'Submit Application' or 'Apply'). Click it once. SUCCESS = a post-submit 'Thank you for applying' / 'Application received' page.",
        ],
    },

    # TeamTailor — a hosted ATS (teamtailor.com) white-labelled onto employer
    # career domains (e.g. careers.westerncomputer.com). Verified against the
    # real Western Computer D365 form 2026-07-15. NOTE: the handoff called this
    # "Recruitee" — it is actually TeamTailor (teamtailor-cdn.com assets,
    # teamtailor-na S3 upload bucket, "Powered by TeamTailor"). Recruitee is
    # kept as a registry alias since both are Rails ATSes with near-identical
    # candidate[...] markup.
    "teamtailor": {
        "container": "form",
        "apply_selectors": [
            "a:has-text('Apply for this job')",
            "button:has-text('Apply for this job')",
            "a:has-text('Apply for this position')",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
        ],
        "submit_selectors": [
            # The real form's primary control is <input type=submit name='commit'
            # value='Submit application'>.
            "input[type=submit][name='commit']",
            "button:has-text('Submit application')",
            "button:has-text('Send application')",
            "button:has-text('Submit')",
            "input[type=submit]",
            "button[type=submit]",
        ],
        "success_patterns": [
            "thank you for your application",
            "thank you for applying",
            "application received",
            "your application has been submitted",
            "we have received your application",
            "we've received your application",
            "we'll be in touch",
        ],
        "url_hint": (
            "TeamTailor hosted ATS, white-labelled onto employer career domains "
            "(careers.<company>.com; also *.teamtailor.com). Identify it by the "
            "DOM (teamtailor-cdn.com assets + Rails candidate[...] field names), "
            "NEVER by URL/job-id/slug. The application form is on the job page "
            "(path /jobs/<id>-<slug>), usually revealed by an 'Apply for this "
            "job' CTA. Single page — no wizard."
        ),
        "quirks": [
            "SINGLE-PAGE form (TeamTailor). If a form isn't visible, click 'Apply for this job' to reveal it, then fill top-to-bottom and submit ONCE.",
            "PERSONAL (all *Required): First name (#candidate_first_name), Last name (#candidate_last_name), Email (#candidate_email), Phone (#candidate_phone / a tel input). Fill from the candidate profile.",
            "RESUME is MANDATORY and uses a DROPZONE.JS file input: <input type=file id='candidate_resume_remote_url' class='dz-hidden-input'> (its name looks like a URL field but it IS the real file input). Upload the resume PDF via upload_file. CRITICAL: the file uploads to storage (S3) in the BACKGROUND after you attach it — WAIT until a filename preview with a success tick appears (Dropzone '.dz-success') BEFORE clicking Submit. Submitting while it is still uploading makes the server SILENTLY reject the application as 'resume required' with no on-page error. (The deterministic uploader already waits for this — do not re-trigger the file input.)",
            "There is a SECOND optional file input 'Additional files' (#candidate_file_remote_url) — leave it empty; only the resume is needed.",
            "CONSENT: a REQUIRED privacy checkbox #candidate_consent_given (name 'candidate[consent_given]', label starts 'By submitting this application, I agree…') MUST be checked or the form will not submit. There is also an OPTIONAL 'future job opportunities' checkbox (#candidate_consent_given_future_jobs) — leave it UNCHECKED.",
            "MANDATORY questions are marked by data-question-mandatory=\"true\" on the enclosing .question div (NOT by the input's `required` attr and NOT always by a '*' in a <label>). EVERY mandatory question must be answered or the server SILENTLY rejects the submit (200 → bounce back to a blank form, no visible error). Typical mandatory set: work-authorization radio, sponsorship radio, 'Your Current Location' choice radio, 'LinkedIn profile URL' text, and the years-of-experience RANGE.",
            "PHONE (#candidate_phone) is an international-telephone widget (intl-tel-input). Fill it with the candidate's number; it auto-formats. It is *Required — never leave it blank.",
            "EXPERIENCE SLIDER: the 'How many years of experience…' question is an <input type=range> (id candidate_answers_attributes_N_range, min 0 / max ~20) paired with a visible number box (name 'range-custom_number'). It DEFAULTS to 0, which counts as UNANSWERED and gets rejected. You MUST set it to the candidate's real years of experience (>0): focus the slider and press ArrowRight to the right value, or type the number into the companion number box. Verify the value is non-zero before submit.",
            "SCREENING QUESTIONS use name='candidate[answers_attributes][N][...]'. Types: Yes/No boolean RADIOS ([N][boolean] value true/false — click the option's <label>), single-CHOICE radios ([N][choice], e.g. country US/Canada/Other), *Required TEXT ([N][text], e.g. 'LinkedIn profile URL'), a DATE ([N][date]), and the RANGE slider above. Answer EACH mandatory one truthfully from the resume/profile.",
            "The form starts DISABLED (greyed, cursor-not-allowed) until it becomes 'ready'; the adapter un-gates it so your clicks land. If a radio/slider click seems to do nothing, the option's <label> is the reliable click target.",
            "The 'LinkedIn profile URL*Required' text field is REQUIRED — fill it with the candidate's real LinkedIn URL from their profile; do not invent one.",
            "CAPTCHA: this form usually has NO visible captcha. TeamTailor may run an INVISIBLE reCAPTCHA at submit; if a reCAPTCHA/hCaptcha appears, solve it with solve_captcha (recaptcha_v2 / hcaptcha). A bare challenges.cloudflare.com iframe here is NOT a Turnstile — do not classify it as turnstile or reload the page.",
            "SUBMIT: click the primary control once (input name='commit', text 'Submit application'). Do NOT click 'Apply with LinkedIn'. SUCCESS is confirmed ONLY by a post-submit 'Thank you for your application' / 'Application received' page — a filled form is NOT success.",
            "NEVER reload the page mid-fill — TeamTailor keeps form state in JS and a reload wipes every entered field and the uploaded resume.",
        ],
    },

    "talent": {
        "container": "main",
        "apply_selectors": [
            "button:has-text('Easy Apply')",
            "a:has-text('Easy Apply')",
            "button:has-text('Apply Now')",
            "a:has-text('Apply Now')",
            "button:has-text('Quick Apply')",
            "button:has-text('Apply')",
            "[data-testid*='apply']",
        ],
        "submit_selectors": [
            "button:has-text('Send application')",
            "button:has-text('Send Application')",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
            "button[type='submit']",
        ],
        "success_patterns": [
            "application submitted",
            "application sent",
            "thank you",
            "your application has been sent",
            "we've received your application",
            "successfully applied",
        ],
        "url_hint": (
            "Talent.com native Easy Apply. Job pages live on www.talent.com "
            "(/jobs?...&id=<id>). The 'Apply Now' / 'Easy Apply' button on the "
            "job page is clicked by the TalentAdapter BEFORE the loop runs, which "
            "redirects to the apply flow (may briefly show a near-blank page with "
            "a captcha while it loads). Validate which page you're on by HEADING "
            "and STEP INDICATOR text — NEVER by URL/ids/query params."
        ),
        "quirks": [
            "FLOW IS FORWARD-ONLY: Apply -> (captcha, auto-handled) -> Email verification ('Check your email') -> OTP code (6 boxes, AUTO-FILLED by the runner from Gmail) -> Step 1 of 2 'Add your contact information' -> Step 2 of 2 'Review and send application' -> Submit -> Success. Never navigate_url BACK to a page you already passed.",
            "PAGE IDENTIFICATION (lowercased heading / step text): 'sign in to apply' / 'check your email' => email-verification step; a row of 6 single-character boxes / 'verification code' / 'enter the code' => OTP step; 'step 1 of 2' + 'add your contact information' => contact step; 'step 2 of 2' + 'review and send application' => review step.",
            "NEVER USE GOOGLE / SSO SIGN-IN. Do NOT click 'Continue with Google', 'Sign in with Google', or any SSO button, and NEVER navigate to accounts.google.com. The application uses EMAIL + OTP only. If you ever land on a Google sign-in page, you took a wrong turn — do not enter anything there.",
            "EMAIL-VERIFICATION STEP ('Sign in to apply' / 'Check your email'): the captcha AND the email entry are normally ALREADY handled for you by the adapter before you start — so you will usually begin at the OTP step or later. If (and only if) you still see an email <input> with a 'Continue' button: fill it with the candidate's EMAIL from the identity card, then click the button whose text is EXACTLY 'Continue' (use an exact-text match like button:text-is('Continue')) — NOT 'Continue with Google'. This emails the OTP.",
            "OTP STEP — DO NOT FILL IT YOURSELF and DO NOT click 'Resend'. The runner detects the 6-box verification-code widget, fetches the code from the candidate's Gmail inbox automatically, fills the boxes, and submits for you. When you see the 'Check your email' / 'Enter the 6-digit code' screen (a row of 6 single-character boxes), return a `wait` action and KEEP returning `wait` for several turns — the runner needs time to fetch + fill + validate. NEVER click 'Resend', 'Back', 'Sign in', or navigate away while on this screen.",
            "OTP BOX MISLABELING — IMPORTANT: in some Talent.com builds the 6 verification-code boxes are mislabeled 'Phone number' (type=tel). They are NOT a phone field. NEVER type a phone number into a row of 6 single-character boxes — that is the OTP widget, which the runner fills. Only treat a SINGLE wide phone input as a real phone field.",
            "NEVER use the `navigate_url` action during the apply flow, and never click 'Back'/'Exit'/'Resend'/'Sign in'/'Sign in with Google'. The flow is forward-only: each step's primary orange button ('Continue' / 'Send application') advances it. If a step looks stuck, prefer `wait` over navigating or signing in.",
            "CONTACT STEP (heading 'Add your contact information'; the step counter may read 'Step 1 of 2' OR 'Step 1 of 3' depending on the build): required fields are First Name, Last Name, and PHONE NUMBER. Email is usually pre-filled and disabled — leave it. Fill First/Last from the identity card. Fill the Phone field from the identity card. Then UPLOAD THE RESUME via the 'Upload Resume' control (upload_file action, value='resume') and VERIFY the resume filename appears.",
            "PHONE FIELD is a react-tel-input widget (#phone-input, pre-set to '+1', placeholder like '1 (702) 123-4567') and is REQUIRED. It REFORMATS as you type (e.g. '+1 (341) 008-4746') — that reformatted display is CORRECT, do NOT consider it a failure and do NOT re-type it repeatedly. Fill it ONCE with the candidate's phone; if it shows the digits (in any format), it is done — move on. Never loop on the phone field.",
            "CONTACT STEP — before Continue: confirm First Name, Last Name, Phone are populated AND the resume filename is visible AND there are no validation errors ('Required', 'Please enter', 'Invalid email'). Then click the orange primary 'Continue' button and wait for the next step.",
            "COVER LETTER: Talent.com's native Easy Apply typically does NOT ask for a cover letter. If (and only if) a cover-letter upload control is present, upload it (upload_file value='cover_letter'); otherwise skip — do not block on it.",
            "EMPLOYER-QUESTIONS STEP (may appear as 'Step 2 of 3', heading 'Answer employer questions'): a DYNAMIC set of fields — commonly Address, City, State, Postal/ZIP code and screening questions like 'Will you now or in the future require employer sponsorship for employment visa status?'. FILL EVERY VISIBLE FIELD using the candidate's location from the identity card and the PRE-RESOLVED ANSWERS block (the candidate is US-authorized → sponsorship = No, authorized to work in the US = Yes). DO NOT click 'Continue' until ALL fields on this step are filled — clicking with empty required fields just shows 'Required' and wastes turns. Work top-to-bottom: fill address, then city, then state, then postal/ZIP, then each question; THEN click Continue. Never invent a street address or ZIP that isn't in the profile/answers — if a required address value is genuinely unknown, abort with reason='MISSING_CANDIDATE_DATA' rather than guessing.",
            "THERE IS NO CONSENT CHECKBOX TO TICK. Consent is IMPLICIT via the text 'By continuing/applying, I agree to Talent.com's Terms…'. There is a HIDDEN field named 'user_consent' in the DOM — it is display:none and NOT clickable. NEVER attempt to click 'user_consent' or hunt for an 'I agree' checkbox. Clicking it will fail repeatedly and get you stuck. To proceed from the contact step, just click 'Continue'; on the review step, just click 'Send application'.",
            "REVIEW STEP (Step 2 of 2, heading 'Review and send application'): summarizes Contact Information (First Name, Last Name, Email, resume cv_id). NO editing and NO checkbox is needed. Your ONLY action here is to click the orange 'Send application' button. This is NOT the success page — the application is only sent AFTER clicking 'Send application'.",
            "SUBMIT: the final button reads 'Send application' (getByRole button, name=/send application/i). NOTE: it may briefly be DISABLED while an invisible Cloudflare Turnstile bot-check settles (a hidden 'cf-turnstile-response' field). If 'Send application' is disabled, WAIT a few seconds for it to enable, then click it. After clicking, a SUBMITTING state follows (spinner / network / redirect). Wait for it.",
            "SUCCESS is confirmed ONLY by a post-submit confirmation: page text 'Application Submitted' / 'Application Sent' / 'Thank you' / 'Success', or a URL change away from the review step. Reaching the Review page is NOT success. Emit `done` with the confirmation text only after you see one of these.",
            "SELECTORS: Talent.com class names are generated/hashed and change between builds. Prefer accessible selectors in your reasoning — role, label, placeholder, visible text — over brittle CSS / nth-child / deep selectors.",
            "Do not toggle marketing / 'save my answers' / job-alert / newsletter checkboxes unless the candidate policy explicitly opts in. Default them OFF.",
        ],
    },

    "glassdoor": {
        "container": ".modal-content, [data-test='JobApplicationModal']",
        "apply_selectors": [
            "button[data-test='applyButtonGDP']",
            "button:has-text('Easy Apply')",
            "button:has-text('Apply Now')",
            "a:has-text('Apply on company site')",
        ],
        "submit_selectors": [
            "button[data-test='submit-application']",
            "button:has-text('Submit application')",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "button[type='submit']",
        ],
        "success_patterns": [
            "application submitted",
            "your application has been submitted",
            "application sent",
            "thank you for applying",
        ],
        "url_hint": (
            "Job pages live on glassdoor.com (/job-listing/... or /Job/...). "
            "Two branches: (a) EASY APPLY — button[data-test='applyButtonGDP'] "
            "opens an in-page modal (.modal-content / "
            "[data-test='JobApplicationModal']); (b) EXTERNAL — 'Apply on "
            "company site' links out to the employer ATS; the GlassdoorAdapter "
            "resolves that URL and delegates to the target ATS adapter, whose "
            "hints then take over."
        ),
        "quirks": [
            "Account required: set GLASSDOOR_EMAIL and GLASSDOOR_PASSWORD — the adapter logs in BEFORE the job page loads. Never type credentials yourself.",
            "Cloudflare Turnstile challenge may appear on login (and occasionally on apply) — the CaptchaService handles it; if the page looks stalled on a 'verify you are human' widget, wait rather than acting.",
            "Some listings redirect to an external ATS ('Apply on company site') — treat as passthrough; the target ATS's quirks are authoritative once the form loads.",
            "Easy Apply opens a MODAL within the page — scope all selectors inside .modal-content / [data-test='JobApplicationModal'].",
        ],
    },

    "ziprecruiter": {
        "container": "form[data-testid='apply-form'], .apply-modal",
        "apply_selectors": [
            "button[data-testid='joblist-apply-button']",
            "button:has-text('1-Click Apply')",
            "button:has-text('Apply')",
            "a:has-text('Apply Now')",
        ],
        "submit_selectors": [
            "button:has-text('Apply Now')",
            "button:has-text('Submit Application')",
            "button[type='submit']",
        ],
        "success_patterns": [
            "applied successfully",
            "application submitted",
            "you've applied",
            "you have applied",
        ],
        "url_hint": (
            "Job pages live on ziprecruiter.com. Clicking Apply either "
            "completes IMMEDIATELY (1-Click Apply using the pre-built account "
            "profile — a 'You've applied' confirmation appears with NO form) "
            "or opens an apply modal/form with screening questions."
        ),
        "quirks": [
            "Account required: ZIPRECRUITER_EMAIL + ZIPRECRUITER_PASSWORD — the adapter logs in BEFORE the job page loads.",
            "US phone number required at account setup (one-time, pre-configured MANUALLY by the operator — SMS verification is not automatable).",
            "Only Easy Apply postings are driven — skip listings whose CTA reads 'Apply on company site'.",
            "hCaptcha may appear at login — the CaptchaService handles it; wait rather than acting if a captcha widget is visible.",
            "1-Click Apply may finish with NO form at all — if a confirmation like 'You've applied' appears right after the Apply click, the application is DONE; do not hunt for fields.",
            "Rate limit: ~5 applications/hour per account — aggressive throttling; expect the platform rate limiter to slow runs down.",
        ],
    },

    "himalayas": {
        "apply_selectors": [
            "button:has-text('Quick Apply')",
            "button:has-text('Quick apply')",
            "a:has-text('Quick apply')",
            "a:has-text('Apply')",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thanks for applying",
            "application sent",
        ],
        "url_hint": (
            "Listings live on himalayas.app. Two branches: (a) a native "
            "'Quick Apply' form hosted by Himalayas (name/email/resume, "
            "sometimes screening questions); (b) an external Apply link to "
            "the employer ATS (Greenhouse/Lever/Ashby) — the adapter resolves "
            "and delegates, so the target ATS's hints take over."
        ),
        "quirks": [
            "No auth wall and no captcha observed — anonymous apply.",
            "If the 'Quick Apply' button is missing, the listing is an external-ATS passthrough; the adapter scans page anchors for a known ATS host and delegates.",
            "Native Quick Apply form is plain HTML — standard fill + button[type='submit'].",
        ],
    },

    "remoteok": {
        "apply_selectors": [
            "a:has-text('Apply')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply')",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
        ],
        "url_hint": (
            "remoteok.com is a PURE AGGREGATOR — every listing's Apply "
            "button links out to the underlying ATS (Greenhouse/Lever/Ashby) "
            "or an email address. The RemoteRocketship passthrough resolves "
            "the external ATS link and delegates."
        ),
        "quirks": [
            "Pure aggregator — resolve the external ATS link and delegate; the target ATS's quirks are authoritative once the form loads.",
            "Listings whose Apply resolves to a mailto: link cannot be driven — abort those.",
        ],
    },

    "adzuna": {
        "apply_selectors": [
            "a:has-text('Apply Now')",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
        ],
        "url_hint": (
            "adzuna.com is a PURE LINK AGGREGATOR — 'Apply Now' always "
            "redirects to the external employer ATS. The RemoteRocketship "
            "passthrough resolves the link and delegates."
        ),
        "quirks": [
            "Pure aggregator — resolve the external ATS link and delegate; the target ATS's quirks are authoritative once the form loads.",
            "The redirect may hop through an adzuna tracking URL before landing on the ATS — judge the page by its FINAL host, not the click target.",
        ],
    },

    "hiringcafe": {
        "apply_selectors": [
            "button:has-text('Apply')",
            "a:has-text('Apply')",
            "a:has-text('Apply Now')",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
        ],
        "url_hint": (
            "thehiring.cafe is an AGGREGATOR — listings embed or link to an "
            "underlying ATS (Ashby/Greenhouse/Lever). The RemoteRocketship "
            "passthrough resolves the external ATS link and delegates."
        ),
        "quirks": [
            "Pure aggregator — resolve the external ATS link and delegate; the target ATS's quirks are authoritative once the form loads.",
            "Some listings EMBED the ATS form in-page — if form fields are visible on a thehiring.cafe URL, look for the ATS iframe (e.g. #grnhse_iframe, #ashby_embed_iframe) before assuming a native form.",
        ],
    },

    "workable": {
        "apply_selectors": [
            "button:has-text('Apply for this job')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply')",
            "a:has-text('Apply')",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
            "your application has been submitted",
            "we've received your application",
        ],
        "url_hint": (
            "Workable-hosted forms live on apply.workable.com/<company>/j/<id>/ "
            "(also *.workable.com). Single-page application form: contact fields, "
            "resume upload, then screening questions. No login wall for apply."
        ),
        "quirks": [
            "Resume upload auto-parses and pre-fills name/email/phone — verify the parsed values instead of retyping over them.",
            "Country/phone use a custom intl-tel-input widget; target the visible combobox, not the hidden input.",
            "Screening answers render as custom radio/checkbox/select groups below the resume — scroll to the bottom before submit.",
            "OneTrust/cookie consent banner often overlays the form — dismiss it first.",
        ],
    },

    "smartrecruiters": {
        "apply_selectors": [
            "a[href*='/oneclick-ui/']",
            "a:has-text(\"I'm interested\")",
            "button:has-text(\"I'm interested\")",
            "a:has-text('Interested')",
            "button:has-text('Apply')",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
            "thanks for applying",
            "your application was sent",
            "application received",
        ],
        "url_hint": (
            "SmartRecruiters postings live on jobs.smartrecruiters.com/<Company>/"
            "<id>-<slug>. The \"I'm interested\" CTA is a plain <a> whose href is "
            "the real application form: jobs.smartrecruiters.com/oneclick-ui/"
            "company/<Company>/publication/<uuid>. No login wall for apply."
        ),
        "quirks": [
            "The whole oneclick-ui form is built from <spl-*> web components with SHADOW DOM — raw CSS ids often resolve stale; prefer get_by_label/role selectors (they pierce shadow roots). Known stable inner ids: #first-name-input, #last-name-input, #email-input, #confirm-email-input, #linkedin-input, #website-input, #hiring-manager-message-input.",
            "'Confirm your email' is REQUIRED and must exactly repeat the email address.",
            "A REQUIRED privacy-consent checkbox (#noPolicy) sits above Submit — it must be checked or Submit silently fails.",
            "NEVER click 'Apply With Indeed', 'Apply with LinkedIn' or any autofill/import widget — manual field fill only (same policy as SSO).",
            "Resume goes into the spl-dropzone file input inside the 'Resume' section; the profile-image upload near the top is NOT the resume — skip it.",
            "Which fields are required varies per company (City/Phone/Resume may or may not be) — trust the * markers on the live form.",
            "Phone uses a country-code widget: pick the country first, then type the national number into the tel input.",
            "Experience/Education 'Add' sections are optional — skip them unless marked required.",
            "After Submit some companies ask extra screening questions or send an email-verification code — answer them / fetch the code from Gmail, do not stop at the first Submit click.",
        ],
    },

    "jobvite": {
        "apply_selectors": [
            "a[href*='/apply']",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
        ],
        "submit_selectors": [
            "button:has-text('Send Application')",
            "button[type='submit']",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "thank you for applying",
            "application has been sent",
            "application has been received",
            "successfully submitted",
        ],
        "url_hint": (
            "Jobvite postings live on jobs.jobvite.com/<company>/job/<id> (or a "
            "company careers domain powered by Jobvite). The Apply CTA is a "
            "plain <a> to the same URL + /apply — the application is a single "
            "page at jobs.jobvite.com/<company>/job/<id>/apply. No login wall."
        ),
        "quirks": [
            "Field ids/names are per-tenant random tokens (jv-field-XXXX / input-XXXX) — NEVER reuse ids across jobs; target fields by their visible label.",
            "Resume section is usually REQUIRED: click the 'Select' button and use its file input (#file-input-0), or paste resume text into the 'Type or paste your Resume here' textarea as fallback.",
            "NEVER click the 'LinkedIn' import button — manual field fill only (same policy as SSO).",
            "Screening dropdowns (work authorization, sponsorship, etc.) are per-tenant native <select> elements and often REQUIRED — answer every one marked *.",
            "The form embeds reCAPTCHA v2 (g-recaptcha-response) — solve it via the captcha service before the final submit.",
            "A 'Next →' button steps through sections; the FINAL submit is the 'Send Application' button — keep advancing until it appears.",
            "Cover letter attaches via the 'Add Cover Letter' button in Additional Files (optional).",
        ],
    },

    "rippling": {
        "apply_selectors": [
            "button:has-text('Apply')",
            "a:has-text('Apply')",
            "button:has-text('Apply for this role')",
            "a:has-text('Apply Now')",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
            "we've received your application",
        ],
        "url_hint": (
            "Rippling-hosted ATS forms live on ats.rippling.com/<company>/jobs/<id>. "
            "Bespoke single-page form (contact + resume + questions). No login wall."
        ),
        "quirks": [
            "Bespoke React form with non-standard class names — prefer accessible selectors (label/aria) over CSS classes.",
            "Resume upload may pre-fill contact fields; verify parsed values.",
        ],
    },

    "pinpointhq": {
        "apply_selectors": [
            "button:has-text('Apply')",
            "a:has-text('Apply')",
            "button:has-text('Apply for this job')",
            "a:has-text('Apply Now')",
        ],
        "submit_selectors": [
            "button[type='submit']",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
        ],
        "success_patterns": [
            "application submitted",
            "thank you for applying",
            "application received",
        ],
        "url_hint": (
            "PinpointHQ-hosted careers pages live on <company>.pinpointhq.com "
            "(and *.pinpoint.hr). Apply opens a hosted form: contact + resume + "
            "screening questions. No login wall for apply."
        ),
        "quirks": [
            "Standard hosted form; resume upload auto-parses contact fields — verify rather than overwrite.",
            "Screening questions render below the fold — scroll to the bottom before submit.",
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
            "thank you for your application",
            "application received",
            "we have received your application",
            "we've received your application",
            "successfully applied",
        ],
        "quirks": [
            "UNKNOWN PORTAL — there is no scripted playbook. Decide the flow at "
            "runtime PURELY from the screenshot + DOM you are shown each turn. Do "
            "not assume any specific layout.",
            "THINK ONE STEP AHEAD before every action. Before you click, predict "
            "what it should do: 'this Apply button should open the application "
            "form', 'this Next button should reveal the next section', 'this "
            "Submit should send the application and show a confirmation'. After "
            "the action, CHECK the new screenshot against your prediction. If the "
            "page did NOT change the way you expected (same page, an error banner "
            "appeared, a modal opened, a new tab opened), do NOT blindly repeat "
            "the same action — change approach: scroll to find the real control, "
            "pick a different selector, dismiss the modal, or switch to the new "
            "tab. Repeating an action that already did nothing is the #1 way runs "
            "get stuck — never do the exact same thing twice in a row.",
            "STEP 1 — REACH THE FORM. If you see a job description with an "
            "Apply/Apply Now/Easy Apply/'I'm interested' button and no form yet, "
            "click it once and wait for the form (it may open inline, in a modal, "
            "on a new page, or in a NEW TAB — if a new tab opens, work in it). If "
            "a form is already visible, start filling immediately — do not hunt "
            "for an Apply button.",
            "STEP 2 — LOGIN WALL (if any). If a sign-in/login form blocks the "
            "application, log in with the candidate's EMAIL + PASSWORD (see the "
            "LOGIN / SIGN-IN HANDLING section). Manual login ONLY — never click "
            "'Continue with Google' or any social/SSO button, and never leave the "
            "site to an OAuth page. Ignore optional 'Apply with LinkedIn/Indeed' "
            "autofill/import widgets — always fill the fields manually.",
            "STEP 3 — FILL EVERY REQUIRED FIELD intelligently from the identity "
            "card, resume, and pre-resolved answers: name, email, phone, location, "
            "work authorization, screening questions, EEO/demographics. Upload the "
            "resume to any resume/CV file input (and the cover letter to a cover "
            "letter input if one exists). Use ONE fill_field per field; for "
            "dropdowns/comboboxes emit fill_field with the option text (never click "
            "through options).",
            "FIELD-FINDING when a selector goes stale or nothing matches: prefer "
            "the field's VISIBLE LABEL over brittle CSS ids. Emit fill_field with "
            "the human field_label (e.g. 'First name', 'Email') and a best-guess "
            "selector — the runner resolves by label/placeholder/aria automatically "
            "and pierces open shadow DOM. Random-looking ids (react-aria..., "
            "jv-field-..., spl-form-element_...) are auto-generated and CHANGE "
            "between page loads — never rely on them; describe the field by label.",
            "HARD FIELD TYPES: (a) custom dropdown / combobox / typeahead — emit "
            "fill_field with the exact option TEXT; if a listbox pops open, the "
            "runner selects the matching option. (b) date pickers — fill the ISO "
            "value or the format the placeholder shows. (c) intl phone widgets — "
            "the country is usually pre-set to the candidate's country; just type "
            "the number. (d) required consent / policy / privacy CHECKBOXES — these "
            "are easy to miss and silently block submit; scan for any unchecked "
            "required checkbox near the submit button and check it. (e) 'confirm "
            "email' fields — repeat the email exactly.",
            "STEP 4 — MULTI-STEP. If the form spans multiple steps/pages, complete "
            "the visible step, then use next_step (Next/Continue/Save & Continue) "
            "to advance, and repeat until the final Submit. Never treat a 'Next' "
            "button as the final submit.",
            "STEP 5 — SUBMIT once every required field is filled: click the final "
            "Submit/Send application button. If the submit button is DISABLED, a "
            "required field or checkbox is still incomplete — scroll the whole form "
            "and fill what is missing rather than clicking a dead button. If an "
            "email verification code screen appears after submit, it is fetched and "
            "filled for you — just wait. If a CAPTCHA appears, emit solve_captcha.",
            "BEFORE giving up: if you cannot find a field or control, SCROLL "
            "(both directions) and re-read — most 'missing' fields are just below "
            "the fold or inside a section that must be expanded first. Only stop "
            "when you have genuinely exhausted the page.",
            "Only emit 'done' when you can SEE a real confirmation (e.g. "
            "'application submitted' / 'thank you for applying' / a confirmation "
            "page). Do NOT claim done on a job-description page, an error banner, "
            "or a still-empty form.",
            "If the page is a hard bot-wall / CAPTCHA you cannot clear, or offers "
            "ONLY social-SSO login with no email/password option, stop rather than "
            "guessing — do not loop.",
        ],
    },
}


def get_platform_hints(platform: str) -> Dict[str, Any]:
    """Return hint dict for the given platform, falling back to 'generic'.

    The pipeline stores ``jobs.source`` as a host (e.g. 'www.dice.com',
    'boards.greenhouse.io'), NOT the canonical adapter key ('dice',
    'greenhouse'). So after the exact-key lookup we host-substring match the
    same way the adapter registry does — otherwise a real Dice job would route
    to DiceAdapter but the AI would get GENERIC hints and lose the Dice wizard
    knowledge. Keep this in sync with adapters/registry.get_adapter().
    """
    key = (platform or "generic").lower().strip()
    if key in _HINTS:
        return _HINTS[key]
    # Dots are stripped too so dotted hosts match compact keys (e.g.
    # jobs.source='thehiring.cafe' → 'thehiringcafe' → matches 'hiringcafe').
    normalized = key.replace("-", "").replace("_", "").replace(".", "")
    # Most specific (longest key) first so e.g. 'smartapply' wins over
    # 'indeed' for 'smartapply.indeed.com'. Plain insertion order used to
    # let whichever entry was defined earlier shadow the more specific one.
    for k in sorted(_HINTS, key=len, reverse=True):
        if k == "generic":
            continue
        if k in key or k in normalized:
            return _HINTS[k]
    return _HINTS["generic"]


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
