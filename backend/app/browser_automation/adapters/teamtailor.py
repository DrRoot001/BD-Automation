"""TeamTailor adapter — a hosted ATS white-labelled onto employer career sites.

TeamTailor (teamtailor.com) powers career sites on employer domains such as
``careers.westerncomputer.com`` (also ``<company>.teamtailor.com``). Each job
publishes a SINGLE-PAGE application form at ``/jobs/<id>-<slug>``, usually
revealed by an "Apply for this job" CTA: personal info, a mandatory
**Dropzone.js** résumé file input, screening questions
(``candidate[answers_attributes][N][...]``), a required privacy checkbox, then a
Submit control (``input[name=commit]``).

NOTE: handoff v7 called this ATS "Recruitee" — it is actually TeamTailor
(teamtailor-cdn.com assets, ``teamtailor-na`` S3 upload bucket, "Powered by
TeamTailor"). ``recruitee`` is kept as a registry alias because both are
Rails-based ATSes with near-identical ``candidate[...]`` markup; if a genuine
recruitee.com form ever needs different handling it can get its own adapter.

Detection is by DOM SIGNATURE, not host — the form lives on arbitrary employer
domains that carry no stable URL token (see ``is_teamtailor_dom`` +
``AgentLoop._detect_ats_from_dom``). The form itself is filled by the shared
loop using the detailed ``teamtailor`` hints in ``hints.py`` (verified against
the real Western Computer D365 form). The only platform-specific concern kept
here is deterministically REVEALING the inline form so the loop starts on it
(saves LLM turns) — the critical résumé-upload-completion wait is handled
generically in ``agent/loop.py::_await_async_upload``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter

logger = logging.getLogger(__name__)

# A form is considered present when TeamTailor's Rails candidate fields exist.
_FORM_PRESENT_SELECTOR = "#candidate_first_name, input[name^='candidate[']"

_APPLY_TRIGGER_LABELS = (
    "Apply for this job",
    "Apply for this position",
    "Apply now",
    "Apply",
)


async def is_teamtailor_dom(page: Any) -> bool:
    """DOM-signature detection for TeamTailor, usable from the loop's white-label
    reclassify path. Cheap, defensive, never raises."""
    try:
        return bool(await page.evaluate(r"""() => {
            const html = document.documentElement.outerHTML;
            // TeamTailor's own CDN/branding is zero-false-positive and present on
            // the JOB page immediately — before the "Apply for this job" CTA
            // reveals the candidate[...] form. Detect on it alone so the loop
            // gets the TeamTailor playbook (incl. the reveal step) up front.
            const tt = /teamtailor-cdn\.com|powered.{0,20}by.{0,20}teamtailor/i.test(html)
                || !!document.querySelector('script[src*="teamtailor"], link[href*="teamtailor"]');
            if (tt) return true;
            // Fallback: the distinctive Dropzone résumé input + Rails candidate
            // fields (covers a hypothetical proxied-assets case) — kept narrow to
            // avoid matching unrelated Rails forms.
            const dzResume = document.querySelector(
                '#candidate_resume_remote_url, input.dz-hidden-input[id*="resume"]'
            );
            const railsForm = document.querySelector('input[name^="candidate["], #candidate_first_name');
            return !!(dzResume && railsForm);
        }"""))
    except Exception:
        return False


async def activate_teamtailor_form(page: Any) -> bool:
    """Un-gate the TeamTailor application form so real interactions land.

    TeamTailor renders the form under a wrapper that stays
    ``opacity-50 cursor-not-allowed`` with the inner content ``pointer-events-none``
    until its realtime (Pusher) controller marks it "ready". In an automation
    session that ready-state often never fires, so every CLICK (radios, the
    experience slider, the submit button) is blocked — which is why phone/slider/
    radios silently don't fill. Removing the gating classes lets the loop's real
    clicks reach the fields. No-op when the form isn't present. Never raises.

    NOTE: this makes the fields fillable; some TeamTailor tenants additionally
    reject the SUBMIT server-side pending the realtime session (a separate wall).
    """
    try:
        return bool(await page.evaluate(r"""() => {
            let touched = false;
            document.querySelectorAll('[data-careersite--form-target="formContentWrapper"]').forEach(e => {
                if (/opacity-50|cursor-not-allowed/.test(e.className)) touched = true;
                e.classList.remove('opacity-50', 'cursor-not-allowed');
                e.style.opacity = '1'; e.style.pointerEvents = 'auto';
            });
            document.querySelectorAll('[data-careersite--form-target="formContent"], .pointer-events-none').forEach(e => {
                if (e.classList.contains('pointer-events-none')) touched = true;
                e.classList.remove('pointer-events-none'); e.style.pointerEvents = 'auto';
            });
            return touched;
        }"""))
    except Exception:
        return False


async def reveal_teamtailor_form(page: Page) -> None:
    """Click the 'Apply for this job' CTA to reveal the inline application form
    when it isn't already rendered. Deterministic (no LLM) so the loop starts on
    the form instead of spending turns discovering the Apply button. No-op when
    the form is already present. Never raises."""
    try:
        if await page.locator(_FORM_PRESENT_SELECTOR).count() > 0:
            return
    except Exception:
        pass
    for label in _APPLY_TRIGGER_LABELS:
        try:
            trigger = page.get_by_role("button", name=label, exact=False)
            if await trigger.count() == 0:
                trigger = page.get_by_role("link", name=label, exact=False)
            if await trigger.count() > 0 and await trigger.first.is_visible():
                logger.info(f"[teamtailor] revealing form via {label!r}")
                await trigger.first.click()
                try:
                    await page.locator(_FORM_PRESENT_SELECTOR).first.wait_for(
                        state="visible", timeout=6_000
                    )
                except Exception:
                    pass
                break
        except Exception:
            continue
    # Bring the form into view so the loop's first screenshot shows it.
    try:
        await page.locator("#candidate_first_name").first.scroll_into_view_if_needed(timeout=4_000)
    except Exception:
        pass
    # Un-gate the form so the loop's real clicks (radios/slider/submit) land.
    await activate_teamtailor_form(page)


class TeamTailorAdapter(AutonomousAdapter):
    platform_name = "teamtailor"
    hints_key = "teamtailor"
    container_selector = "form"

    async def prepare(self, page: Page) -> None:
        await reveal_teamtailor_form(page)
