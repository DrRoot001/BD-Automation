"""Match an incoming email to an existing application record."""
from __future__ import annotations

import logging
import re
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


def _extract_domain(addr: str) -> str:
    """e.g. 'noreply@greenhouse.io' → 'greenhouse.io'"""
    match = re.search(r"@([\w.\-]+)", addr)
    return match.group(1).lower() if match else ""


def _normalize_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


async def match_email_to_application(
    candidate_id: str,
    from_addr: str,
    subject: str,
    body_text: str,
    db_session,
) -> Optional[str]:
    """
    Return application_id of the best match, or None.

    Strategy (highest → lowest priority):
    1. from_addr domain matches a company domain in applications
    2. Subject contains a company name from applications
    3. Body contains a company name from applications
    """
    from sqlalchemy import text

    sql = text("""
        SELECT a.id, j.company, j.source
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        WHERE a.candidate_id = :cid
        ORDER BY a.created_at DESC
    """)
    result = await db_session.execute(sql, {"cid": candidate_id})
    apps: List[Dict] = [{"id": str(r.id), "company": r.company, "source": r.source}
                        for r in result.fetchall()]

    if not apps:
        return None

    from_domain = _extract_domain(from_addr)
    subject_lower = subject.lower()
    body_lower = body_text[:2000].lower()

    for app in apps:
        company_norm = _normalize_company(app["company"])
        company_lower = app["company"].lower()

        # Domain match (e.g. @stripe.com → Stripe)
        if company_norm and company_norm in from_domain.replace(".", ""):
            logger.info(f"[Matcher] Domain match: {from_domain} → {app['company']} ({app['id']})")
            return app["id"]

        # Subject contains company name
        if len(company_lower) > 3 and company_lower in subject_lower:
            logger.info(f"[Matcher] Subject match: '{app['company']}' in subject ({app['id']})")
            return app["id"]

        # Body contains company name
        if len(company_lower) > 3 and company_lower in body_lower:
            logger.info(f"[Matcher] Body match: '{app['company']}' in body ({app['id']})")
            return app["id"]

    # ATS platform fallback: greenhouse.io, lever.co → most recent unmatched app
    ats_domains = ["greenhouse.io", "lever.co", "workday.com", "ashby.io", "icims.com", "smartrecruiters.com"]
    if any(ats in from_domain for ats in ats_domains):
        logger.info(f"[Matcher] ATS platform fallback: {from_domain} → most recent app ({apps[0]['id']})")
        return apps[0]["id"]

    logger.info(f"[Matcher] No match found for from={from_addr}, subject={subject[:60]}")
    return None
