"""Tests for the 2026-07-11 hardening pass:

- SmartRecruiters + Jobvite adapters (registry, hints, ATS-host resolution)
- Dice external-apply passthrough plumbing
- Gemini thinking-budget / truncated-JSON repair (claude_client)
- LearnedFixes submit-channel poison guard
- resume_enricher job-title-as-city regression
- loop._execute_action self.profile NameError regression
"""
import inspect
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")),
)

from backend.app.browser_automation.adapters.registry import (  # noqa: E402
    get_adapter,
)
from backend.app.browser_automation.adapters.smartrecruiters import (  # noqa: E402
    SmartRecruitersAdapter,
)
from backend.app.browser_automation.adapters.jobvite import JobviteAdapter  # noqa: E402
from backend.app.browser_automation.adapters.dice import DiceAdapter  # noqa: E402
from backend.app.browser_automation.adapters.remoterocketship import (  # noqa: E402
    _detect_ats_from_url,
)
from backend.app.browser_automation.adapters.hints import get_platform_hints  # noqa: E402
from backend.app.browser_automation.agent.learned_fixes import LearnedFixes  # noqa: E402
from backend.app.browser_automation.llm.claude_client import ClaudeClient  # noqa: E402
from backend.app.browser_automation.services.resume_enricher import (  # noqa: E402
    _scan_location,
)


# ── Registry ────────────────────────────────────────────────────────────────

def test_registry_resolves_smartrecruiters():
    assert isinstance(get_adapter("smartrecruiters"), SmartRecruitersAdapter)
    assert isinstance(get_adapter("jobs.smartrecruiters.com"), SmartRecruitersAdapter)


def test_registry_resolves_jobvite():
    assert isinstance(get_adapter("jobvite"), JobviteAdapter)
    assert isinstance(get_adapter("jobs.jobvite.com"), JobviteAdapter)


# ── ATS-host resolution (single source of truth for passthroughs) ───────────

def test_detect_ats_smartrecruiters_posting_and_form():
    assert _detect_ats_from_url(
        "https://jobs.smartrecruiters.com/SyncreonConsulting/744000136878444-x"
    ) == "smartrecruiters"
    assert _detect_ats_from_url(
        "https://jobs.smartrecruiters.com/oneclick-ui/company/InPost/publication/abc"
    ) == "smartrecruiters"


def test_detect_ats_jobvite_posting_and_apply():
    assert _detect_ats_from_url(
        "https://jobs.jobvite.com/uplight/job/oqgqAfwq"
    ) == "jobvite"
    assert _detect_ats_from_url(
        "https://jobs.jobvite.com/uplight/job/oqgqAfwq/apply"
    ) == "jobvite"


def test_detect_ats_jobvite_list_page_rejected():
    # A company's /jobs list page is not an application target.
    assert _detect_ats_from_url(
        "https://jobs.jobvite.com/ideapublicschools-english/jobs"
    ) is None


# ── Hints ───────────────────────────────────────────────────────────────────

def test_smartrecruiters_hints_carry_portal_knowledge():
    hints = get_platform_hints("smartrecruiters")
    quirk_text = " ".join(hints.get("quirks", [])).lower()
    assert "shadow dom" in quirk_text
    assert "confirm your email" in quirk_text
    assert "nopolicy" in quirk_text
    # Host form resolves to the same entry (mid-run reclassify path).
    assert get_platform_hints("jobs.smartrecruiters.com") is hints


def test_jobvite_hints_carry_portal_knowledge():
    hints = get_platform_hints("jobvite")
    quirk_text = " ".join(hints.get("quirks", [])).lower()
    assert "jv-field" in quirk_text
    assert "recaptcha" in quirk_text
    assert "send application" in " ".join(hints.get("submit_selectors", [])).lower()
    assert get_platform_hints("jobs.jobvite.com") is hints


def test_smartapply_not_shadowed_by_indeed():
    # Regression: insertion order used to let 'indeed' shadow 'smartapply'.
    from backend.app.browser_automation.adapters.hints import _HINTS
    assert get_platform_hints("smartapply.indeed.com") is _HINTS["smartapply"]


# ── Dice external-apply passthrough ─────────────────────────────────────────

def test_dice_has_external_passthrough_plumbing():
    d = DiceAdapter()
    assert hasattr(d, "_inner") and d._inner is None
    assert hasattr(d, "_resolved_url")
    src = inspect.getsource(DiceAdapter)
    assert "_follow_external_apply" in src
    # The old hard-BLOCK for external postings must be gone.
    assert "only drives Dice Easy Apply postings" not in src


def test_dice_delegates_to_inner():
    d = DiceAdapter()

    class _Inner:
        platform_name = "smartrecruiters"

        async def verify_success(self, page):
            return True, "delegated"

        async def submit(self, page):
            return True

    d._inner = _Inner()
    import asyncio
    assert asyncio.run(d.verify_success(page=None)) == (True, "delegated")
    assert asyncio.run(d.submit(page=None)) is True


# ── Gemini client fixes ─────────────────────────────────────────────────────

def test_gemini_call_bounds_thinking_and_uses_json_mime():
    src = inspect.getsource(ClaudeClient._call_gemini)
    assert "thinkingConfig" in src
    assert '"responseMimeType": "application/json"' in src
    assert "thought" in src  # thought-part filter


def test_extract_json_repairs_truncated_object():
    # The exact truncation shape from the 2026-07-10 live logs.
    repaired = ClaudeClient._extract_json_object(
        '{"kind":"fill_field","selector":"#react-aria3510942839-_r_27_","'
    )
    assert repaired is not None
    import json
    obj = json.loads(repaired)
    assert obj["kind"] == "fill_field"

    repaired2 = ClaudeClient._extract_json_object(
        '{"kind":"click","selector":"#submit-btn'
    )
    assert json.loads(repaired2)["selector"] == "#submit-btn"


def test_extract_json_still_prefers_balanced_object():
    text = 'thinking... {"kind":"wait"} trailing'
    assert ClaudeClient._extract_json_object(text) == '{"kind":"wait"}'


# ── LearnedFixes poison guard ───────────────────────────────────────────────

def test_learned_fixes_rejects_apply_anchor_as_submit(monkeypatch):
    monkeypatch.setattr(LearnedFixes, "_persist", lambda self: None)
    lf = LearnedFixes("unit-test-ats")
    lf._loaded = True
    lf._data = {"apply_button": ['a:has-text("Apply Now")']}

    # Anchor with apply-text → refused.
    lf.add("submit", 'a:has-text("Apply Now")')
    assert lf.get("submit") == []
    # Same selector as apply_button → refused even if shaped differently.
    lf.add("submit", 'a:has-text("Apply Now")')
    assert lf.get("submit") == []
    # A real submit button still learns fine, even if it says Apply.
    lf.add("submit", "button:has-text('Apply')")
    assert lf.get("submit") == ["button:has-text('Apply')"]
    # apply_button channel unaffected.
    lf.add("apply_button", 'a:has-text("Apply")')
    assert 'a:has-text("Apply")' in lf.get("apply_button")


# ── resume_enricher regression ──────────────────────────────────────────────

def test_job_title_is_not_a_city():
    # Live 2026-07-10: "CRM Specialist, MS Dynamics" parsed as a Mississippi city.
    assert _scan_location("CRM Specialist, MS Dynamics resume header") is None


def test_real_city_still_wins_after_title_line():
    got = _scan_location("Mark Anderson\nCRM Specialist, MS Dynamics\nAustin, TX")
    assert got is not None
    city, state_name, abbr = got
    assert city == "Austin" and abbr == "TX"


# ── loop.py NameError regression ────────────────────────────────────────────

def test_execute_action_no_self_reference():
    from backend.app.browser_automation.agent.loop import _execute_action
    src = inspect.getsource(_execute_action)
    assert "self.profile" not in src
    assert "profile" in inspect.signature(_execute_action).parameters


# ── Portal memory (self-learning across runs — canonical host-keyed API) ─────

from backend.app.browser_automation.agent import portal_memory as pm  # noqa: E402


def test_portal_memory_host_of_normalises():
    # URL → host, www-stripped, port/user stripped, so www.dice.com and
    # dice.com share one playbook file.
    assert pm.host_of("https://boards.greenhouse.io/vercel/jobs/123") == "boards.greenhouse.io"
    assert pm.host_of("https://www.dice.com/job-detail/abc") == "dice.com"
    assert pm.host_of("jobs.smartrecruiters.com") == "jobs.smartrecruiters.com"
    assert pm.host_of("") == ""


def _fake_action(kind, ok=True, selector=None, label=None):
    from backend.app.browser_automation.agent.loop import AgentAction
    return AgentAction(kind=kind, ok=ok, selector=selector, field_label=label)


def test_portal_memory_records_and_formats(monkeypatch, tmp_path):
    # Point the store at a temp dir so the test never touches the real cache.
    monkeypatch.setattr(pm, "_BASE_DIR", tmp_path)
    monkeypatch.setenv("PORTAL_MEMORY_ENABLED", "true")
    url = "https://jobs.acme.com/apply/9"
    assert pm.format_for_prompt(url) == ""  # cold start → nothing

    actions = [
        _fake_action("click", selector="a:has-text('Apply')"),
        _fake_action("fill_field", selector="#first-name-input", label="First name"),
        _fake_action("fill_field", selector="#email-input", label="Email"),
        _fake_action("upload_file", selector="#resume"),
        _fake_action("submit", selector="button:has-text('Submit application')"),
    ]
    pm.record(url, outcome="SUBMITTED", actions=actions,
              ats="smartrecruiters", success_signal="Application submitted")
    block = pm.format_for_prompt(url)
    assert "SELF-LEARNED PLAYBOOK" in block
    assert "jobs.acme.com" in block
    assert "Submit application" in block            # winning submit selector surfaced
    assert "1 successful" in block or "successful" in block


def test_portal_memory_records_failure_walls(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "_BASE_DIR", tmp_path)
    monkeypatch.setenv("PORTAL_MEMORY_ENABLED", "true")
    url = "https://x.com/j"
    pm.record(url, outcome="BLOCKED", actions=[_fake_action("wait")],
              captcha_types=["turnstile"], note="Cloudflare turnstile wall")
    block = pm.format_for_prompt(url)
    assert "Captcha seen here: turnstile" in block
    assert "Last outcome: BLOCKED" in block


def test_portal_memory_env_gate_and_bad_input(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "_BASE_DIR", tmp_path)
    # Disabled → recall/format are no-ops even after a record.
    monkeypatch.setenv("PORTAL_MEMORY_ENABLED", "false")
    pm.record("https://y.com/j", outcome="SUBMITTED", actions=[])
    assert pm.format_for_prompt("https://y.com/j") == ""
    # Empty/garbage host never raises.
    pm.record("", outcome="SUBMITTED")
    assert pm.format_for_prompt("") == ""


def test_generic_playbook_has_reasoning_and_hard_field_guidance():
    from backend.app.browser_automation.adapters.hints import _HINTS
    quirks = " ".join(_HINTS["generic"]["quirks"]).lower()
    assert "one step ahead" in quirks          # predict-before-click
    assert "never do the exact same thing twice" in quirks  # anti-loop
    assert "consent" in quirks and "checkbox" in quirks     # hidden-required-checkbox
    assert "shadow dom" in quirks              # web-component field finding
    assert "disabled" in quirks                # disabled-submit handling


def test_agentloop_injects_portal_memory_playbook():
    # AgentLoop's system prompt pulls the self-learned playbook via
    # portal_memory.format_for_prompt (teammate's canonical API).
    import inspect
    from backend.app.browser_automation.agent.loop import AgentLoop
    src = inspect.getsource(AgentLoop._build_system_prompt)
    assert "format_for_prompt" in src
    assert "portal_block" in src


# ── Shadow-DOM-piercing DOM snapshot (the SmartRecruiters field-blindness fix) ─

def test_dom_snapshot_pierces_shadow_dom():
    from backend.app.browser_automation.agent.loop import _DOM_SNAPSHOT_JS
    js = _DOM_SNAPSHOT_JS
    # The deep walker must exist and be used for the field-collection passes,
    # not the old light-DOM-only document.querySelectorAll.
    assert "function deepQueryAll" in js
    assert "shadowRoot" in js
    assert "deepQueryAll('input, select, textarea')" in js
    assert "deepQueryAll('[role=\"combobox\"]')" in js
    assert "deepQueryAll('a')" in js
    # Anchor destinations must be surfaced so the AI can reason about where a
    # click leads (and 'interested' CTAs must be included).
    assert "interested" in js
    assert "href" in js


def test_dom_hash_pierces_shadow_dom():
    import inspect
    from backend.app.browser_automation.agent import loop as L
    src = inspect.getsource(L._dom_hash)
    # The stall detector must see shadow-field value changes, else it falsely
    # flags a web-component form (SmartRecruiters) as STUCK mid-fill.
    assert "shadowRoot" in src and "deepAll" in src


# ── Cloudflare managed-challenge solving (himalayas.app "Just a moment") ─────

def test_captcha_service_has_cloudflare_challenge_path():
    import inspect
    from backend.app.browser_automation.captcha.service import CaptchaService
    assert hasattr(CaptchaService, "solve_cloudflare_challenge")
    assert hasattr(CaptchaService, "_is_cloudflare_interstitial")
    # The turnstile dispatch must route an INTERSTITIAL to the CF challenge
    # solver (proxy-based) BEFORE the proxyless widget path — a bare sitekey on
    # an interstitial yields anti-captcha "site key is invalid".
    src = inspect.getsource(CaptchaService._perform_single_solve)
    assert "_is_cloudflare_interstitial" in src
    assert "solve_cloudflare_challenge" in src


def test_cloudflare_solver_uses_correct_anticaptcha_tasks():
    import inspect
    from backend.app.browser_automation.captcha.service import CaptchaService
    src = inspect.getsource(CaptchaService.solve_cloudflare_challenge)
    # Correct anti-captcha task types (NOT the non-existent 'AntiCloudflareTask').
    assert "TurnstileTaskProxyless" in src
    assert "TurnstileTask" in src
    assert "AntiCloudflareTask" not in src
    # CF-page params (cData/chlPageData) are required for a managed challenge.
    assert "cData" in src and "chlPageData" in src
    # The render hook captures those params (they only exist at render time).
    assert "__cfParams" in CaptchaService._CF_TURNSTILE_HOOK_JS
    assert "render" in CaptchaService._CF_TURNSTILE_HOOK_JS


def test_context_manager_exposes_proxy_for_context():
    from backend.app.browser_automation.browser.context_manager import get_proxy_for_context
    # Unknown/None context → None, never raises (used by the CF solver).
    assert get_proxy_for_context(None) is None
    assert get_proxy_for_context(object()) is None


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
