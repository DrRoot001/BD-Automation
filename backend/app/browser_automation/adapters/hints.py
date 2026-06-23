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
