"""CareerPlug (careerplug.com) adapter — a Rails-based hosted ATS.

Employers on CareerPlug publish a single-page application form at
``https://<company>.careerplug.com/jobs/<id>/apps/new`` (the ``/jobs/<id>`` job
URL auto-redirects there). The form has personal info + address, resume/cover
file uploads (plus optional resume/cover TEXTAREAS), several free-text screening
questions, and a Google **reCAPTCHA** before Submit — reached directly or via a
job-board external-apply redirect (e.g. Remote100K / RemoteRocketship).

Detection is by HOST (``careerplug.com``). The form is filled by the shared
perception loop / AgentLoop using the detailed ``careerplug`` hints in
``hints.py`` (verified against the real S-R-International D365 form). The only
platform-specific concern kept here is landing on the ``/apps/new`` form.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import Page

from .autonomous_base import AutonomousAdapter


def careerplug_apply_url(url: str) -> str:
    """Normalize a CareerPlug job URL to its ``/apps/new`` application form. Only
    ever appended to a ``/jobs/<id>`` path so we never fabricate a bad URL (the
    site also redirects there on its own, so this is belt-and-suspenders)."""
    try:
        path = (urlparse(url).path or "").rstrip("/")
    except Exception:
        return url
    if not path or path.endswith("/apps/new") or "/apps/" in path:
        return url
    if "/jobs/" in path:
        return url.split("?")[0].rstrip("/") + "/apps/new"
    return url


class CareerPlugAdapter(AutonomousAdapter):
    platform_name = "careerplug"
    hints_key = "careerplug"
    container_selector = "form"

    async def _resolve_target_url(self, page: Page, job_url: str) -> Optional[str]:
        return careerplug_apply_url(job_url)
