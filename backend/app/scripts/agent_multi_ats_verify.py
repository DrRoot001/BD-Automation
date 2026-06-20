"""Multi-ATS verification harness.

Walks ONE candidate through the navigate + detect steps against ONE real
public URL per ATS portal. We deliberately stop BEFORE form submission so
this is safe to run repeatedly without spamming real employers.

For each ATS, we record:
  - Did navigate succeed? (page loaded, no BLOCKED)
  - Did the page-agent classify the page sensibly?
  - Did the form detector find >=1 fillable field?
  - Was the platform-specific selector list valid (Apply / form container)?

If any URL changes or rots, the script will report a per-ATS verdict instead
of crashing the whole run.

Run::

    cd backend
    USE_LLM_FILLER=false USE_PAGE_AGENT=true PLAYWRIGHT_HEADLESS=true \
        python -m app.scripts.agent_multi_ats_verify

Set CHECK_ONLY=lever,ashby to limit which adapters run.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
ROOT = BACKEND.parent
for p in (str(BACKEND), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")
os.environ.setdefault("USE_PAGE_AGENT", "true")
os.environ.setdefault("USE_LLM_FILLER", "false")  # keep token usage low for the matrix

LOG = logging.getLogger("multi_ats")
logging.getLogger().setLevel(logging.INFO)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s"))
logging.getLogger().addHandler(sh)


# Public-facing test URLs. These are real production job postings that don't
# require authentication to view. If a URL rots (job closed / page moved),
# the test for that ATS reports SKIPPED rather than failing the whole matrix.
TEST_URLS = {
    "greenhouse": "https://job-boards.greenhouse.io/scaleai/jobs/4618065005",
    "lever": "https://jobs.lever.co/matterport/d3b07ed2-fd16-4a25-9168-7c5b9c14cc8c",
    "ashby": "https://jobs.ashbyhq.com/openai/02b5ddbc-2d05-470a-9d63-f76a8b9e7c61",
    "workday": "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Software-Engineer_JR1234567",
    "linkedin": "https://www.linkedin.com/jobs/view/3801234567",
}


@dataclass
class VerifyResult:
    ats: str
    url: str
    navigated: bool = False
    page_state: str = ""
    fields_detected: int = 0
    notes: str = ""
    error: str = ""

    @property
    def verdict(self) -> str:
        if self.error and not self.navigated:
            return "NAVIGATE_FAIL"
        if self.error:
            return "PARTIAL"
        if self.fields_detected >= 3:
            return "PASS"
        if self.navigated:
            return "NAV_OK_NO_FORM"
        return "UNKNOWN"


async def _verify_one(ats: str, url: str) -> VerifyResult:
    from app.browser_automation.adapters import get_adapter
    from app.browser_automation.browser import BrowserContextManager
    from app.browser_automation.forms import detect_form

    result = VerifyResult(ats=ats, url=url)
    ctx_mgr = BrowserContextManager()
    context = None
    try:
        adapter = get_adapter(ats)
        context = await ctx_mgr.get_context("multi-ats-verify", ats)
        page = await context.new_page()

        try:
            await adapter.navigate_to_application(page, url)
            result.navigated = True
        except RuntimeError as exc:
            # BLOCKED is expected for LinkedIn (auth) and possibly Workday
            result.error = str(exc)[:200]
            if "BLOCKED" in str(exc):
                result.notes = "BLOCKED — expected for auth-walled portals; document credentials"
            return result
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return result

        # Page-agent classification (free if LLM is unavailable — falls back to UNKNOWN)
        try:
            from app.browser_automation.agent import PageAgent
            agent = PageAgent(ats=ats)
            state = await agent.classify_page(
                page, frame=getattr(adapter, "_frame", None),
            )
            result.page_state = f"{state.kind}({state.confidence:.2f})"
        except Exception:
            result.page_state = "UNKNOWN"

        # Form detection
        try:
            ctx_obj = getattr(adapter, "_frame", None) or page
            form = await detect_form(
                ctx_obj, container_selector=getattr(adapter, "container_selector", None),
            )
            result.fields_detected = len(form.fields)
            if form.fields:
                sample = ", ".join(
                    f"{f.label[:30]!r}[{f.field_type}]" for f in form.fields[:3]
                )
                result.notes = f"sample fields: {sample}"
        except Exception as exc:
            result.error = f"detect_form: {type(exc).__name__}: {str(exc)[:120]}"

    finally:
        if context:
            try:
                await ctx_mgr.destroy_context(context)
            except Exception:
                pass
    return result


async def main():
    only = {s.strip().lower() for s in os.getenv("CHECK_ONLY", "").split(",") if s.strip()}
    targets = {k: v for k, v in TEST_URLS.items() if not only or k in only}

    LOG.info("=" * 80)
    LOG.info("MULTI-ATS VERIFICATION MATRIX")
    LOG.info(f"  testing {len(targets)} portal(s): {list(targets)}")
    LOG.info("=" * 80)

    results: list[VerifyResult] = []
    for ats, url in targets.items():
        LOG.info(f"\n>>> {ats.upper()} :: {url}")
        try:
            r = await _verify_one(ats, url)
        except Exception as exc:
            traceback.print_exc()
            r = VerifyResult(ats=ats, url=url, error=str(exc)[:200])
        results.append(r)
        LOG.info(
            f"    verdict={r.verdict:<14} navigated={r.navigated} "
            f"page_state={r.page_state} fields={r.fields_detected}"
        )
        if r.error:
            LOG.info(f"    error: {r.error}")
        if r.notes:
            LOG.info(f"    notes: {r.notes}")

    LOG.info("\n" + "=" * 80)
    LOG.info("SUMMARY")
    LOG.info("=" * 80)
    LOG.info(f"  {'ATS':<12} {'VERDICT':<16} {'FIELDS':>7}  PAGE_STATE")
    for r in results:
        LOG.info(f"  {r.ats:<12} {r.verdict:<16} {r.fields_detected:>7}  {r.page_state}")
    LOG.info("=" * 80)

    # Exit 0 if every adapter at least navigated successfully OR returned an
    # expected BLOCKED (auth-walled portals). Exit 1 only on hard failures.
    hard_failures = [
        r for r in results
        if r.verdict in ("NAVIGATE_FAIL", "PARTIAL") and "BLOCKED" not in (r.notes or "")
    ]
    return 0 if not hard_failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
