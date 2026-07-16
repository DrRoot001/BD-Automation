"""Dynamic ATS detection from the live DOM.

Used by the loop's white-label reclassify path: some ATSes are served from an
employer's own domain and carry no stable URL token, so the URL-based
``adapters/registry.py`` lookup can't see them. This module looks at the page
itself instead.

WHY THIS IS DELIBERATELY CONSERVATIVE
-------------------------------------
A hit here makes the loop **swap platforms mid-run** and inject a different
ATS's selector cheat-sheet (``adapters/hints.py``). A false positive is
therefore worse than no detection at all: it feeds the agent selectors for the
wrong ATS and it does so silently. So:

* Signatures are **structural** (``querySelector`` on vendor-specific assets,
  hidden inputs, and form actions) — not substring scans of ``innerHTML``.
  Scanning raw HTML for fragments like ``jv-``, ``spl-``, ``workday-`` or
  ``id="application-form"`` matches ordinary markup on unrelated sites: a plain
  ``<form id="application-form">`` is not Lever, and ``<turbo-frame>`` is any
  Rails/Hotwire app, not TeamTailor. (The same HTML-substring trap produced a
  real MFA false-positive in this codebase before — see handoff §8.)
* Every signature keys off a **vendor-owned domain or namespaced attribute**
  that a non-customer has no reason to emit.
* Unrecognised → ``None``. The caller keeps whatever the registry decided;
  "I don't know" is a safe answer, a confident wrong answer is not.

Returns a **hints key** (matching ``adapters/hints.get_platform_hints``) or
``None``. Never raises.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Structural signatures, evaluated in order. Each entry is:
#   (hints_key, JS expression returning a truthy value)
#
# Rule for adding one: it must be an asset/attribute the ATS vendor owns. If
# you cannot name the vendor-specific token it keys off, do not add it —
# inspect the real DOM first (see handoff §11 "Offline DOM inspection").
_SIGNATURES: list[tuple[str, str]] = [
    # Greenhouse: embed iframe or board assets on greenhouse.io.
    (
        "greenhouse",
        """!!(document.querySelector('#grnhse_iframe, #grnhse_app')
            || document.querySelector('script[src*="boards.greenhouse.io"],'
                                    + 'link[href*="boards.greenhouse.io"],'
                                    + 'iframe[src*="greenhouse.io"]')
            || document.querySelector('form[action*="greenhouse.io"]'))""",
    ),
    # Lever: assets/postings on lever.co. NOT '#application-form', which is a
    # generic id many unrelated forms use.
    (
        "lever",
        """!!(document.querySelector('script[src*="lever.co"], link[href*="lever.co"],'
                                  + 'iframe[src*="lever.co"]')
            || document.querySelector('form[action*="lever.co"]')
            || document.querySelector('input[name="lever-source"], [data-qa="posting-name"]'))""",
    ),
    # Ashby: assets on ashbyhq.com, or its namespaced embed root.
    (
        "ashby",
        """!!(document.querySelector('script[src*="ashbyhq.com"], link[href*="ashbyhq.com"],'
                                  + 'iframe[src*="ashbyhq.com"]')
            || document.querySelector('#ashby_embed, [class*="ashby-job-posting"]')
            || document.querySelector('form[action*="ashbyhq.com"]'))""",
    ),
    # Workday: myworkdayjobs.com assets/iframe, or its wd-namespaced automation ids.
    (
        "workday",
        """!!(document.querySelector('iframe[src*="myworkdayjobs.com"],'
                                  + 'script[src*="myworkdayjobs.com"]')
            || document.querySelector('[data-automation-id="jobPostingHeader"],'
                                    + '[data-automation-id="applyFlowContainer"]'))""",
    ),
    # iCIMS: icims.com-hosted iframe/assets or its namespaced container.
    (
        "icims",
        """!!(document.querySelector('iframe[src*="icims.com"], script[src*="icims.com"]')
            || document.querySelector('#icims_content_iframe, .iCIMS_MainWrapper')
            || document.querySelector('form[action*="icims.com"]'))""",
    ),
    # SmartRecruiters: smartrecruiters.com assets or its spl- web components.
    # Require the vendor domain OR a real custom element (not a 'spl-' substring).
    (
        "smartrecruiters",
        """!!(document.querySelector('script[src*="smartrecruiters.com"],'
                                  + 'link[href*="smartrecruiters.com"],'
                                  + 'iframe[src*="smartrecruiters.com"]')
            || document.querySelector('form[action*="smartrecruiters.com"]')
            || document.querySelector('spl-job-title, spl-apply-button'))""",
    ),
    # Jobvite: jobvite.com assets or its jv- namespaced page root.
    (
        "jobvite",
        """!!(document.querySelector('script[src*="jobvite.com"], link[href*="jobvite.com"],'
                                  + 'iframe[src*="jobvite.com"]')
            || document.querySelector('form[action*="jobvite.com"]')
            || document.querySelector('.jv-page, .jv-job-detail-page'))""",
    ),
    # Workable: workable.com assets, or its namespaced data attr.
    (
        "workable",
        """!!(document.querySelector('script[src*="workable.com"], link[href*="workable.com"],'
                                  + 'iframe[src*="workable.com"]')
            || document.querySelector('form[action*="workable.com"]')
            || document.querySelector('[data-ui="job-application"]'))""",
    ),
]


class ATSDetector:
    """Identify the ATS from the live DOM. Conservative by design."""

    @classmethod
    async def detect(cls, page: Any, frame: Optional[Any] = None) -> Optional[str]:
        """Return a hints key (e.g. ``"greenhouse"``) or ``None`` if unsure.

        ``None`` means "no confident signature" — the caller must keep the
        registry's decision rather than guess.
        """
        ctx = frame or page

        # TeamTailor first: it has its own DOM-verified detector (the reason
        # this whole path exists — white-labelled onto employer domains).
        try:
            from ..adapters.teamtailor import is_teamtailor_dom

            if await is_teamtailor_dom(page):
                return "teamtailor"
        except Exception as exc:
            logger.debug(f"[ATSDetector] teamtailor probe skipped: {exc}")

        # <meta name="generator"> — self-declared, so trust an exact vendor name.
        try:
            generator = await ctx.evaluate(
                """() => {
                    const m = document.querySelector('meta[name="generator"]');
                    return m ? (m.getAttribute('content') || '').toLowerCase() : '';
                }"""
            )
            for key in ("greenhouse", "workable", "bamboohr", "teamtailor"):
                if key and key in (generator or ""):
                    return key
        except Exception as exc:
            logger.debug(f"[ATSDetector] generator probe skipped: {exc}")

        for hints_key, js in _SIGNATURES:
            try:
                if await ctx.evaluate(f"() => {js}"):
                    logger.info(f"[ATSDetector] matched {hints_key} via DOM signature")
                    return hints_key
            except Exception as exc:
                logger.debug(f"[ATSDetector] {hints_key} probe skipped: {exc}")

        return None
