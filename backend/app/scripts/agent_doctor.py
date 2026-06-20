"""Agent doctor — verifies the autonomous-agent wiring is healthy.

Checks (in order):
  1. All new modules import cleanly.
  2. GEMINI_API_KEY is set and a tiny round-trip call succeeds.
  3. LLM JSON-mode returns valid JSON.
  4. learned_fixes write/read cycle works.
  5. Celery task registry includes task:dynamic_apply.

Run with::

    cd backend
    python -m app.scripts.agent_doctor
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
ROOT = BACKEND.parent
for p in (str(BACKEND), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")


def section(name: str) -> None:
    print(f"\n=== {name} ===")


async def check_imports() -> bool:
    section("1. Imports")
    mods = [
        "app.browser_automation.llm.claude_client",
        "app.browser_automation.forms.llm_filler",
        "app.browser_automation.agent.page_agent",
        "app.browser_automation.agent.learned_fixes",
        "app.browser_automation.agent.failure_diagnoser",
        "app.browser_automation.services.executor",
        "app.tasks.dynamic_apply",
    ]
    import importlib
    ok = True
    for m in mods:
        try:
            importlib.import_module(m)
            print(f"  OK   {m}")
        except Exception as exc:
            print(f"  FAIL {m}: {exc}")
            traceback.print_exc()
            ok = False
    return ok


async def check_gemini_key() -> bool:
    section("2. Claude API key")
    key = (
        os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("CLAUDE_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or ""
    ).strip()
    if not key:
        print("  FAIL  No Anthropic/Claude key found (ANTHROPIC_API_KEY / GEMINI_API_KEY)")
        return False
    print(f"  OK    Claude API key set ({len(key)} chars, prefix={key[:7]!r})")
    return True


async def check_gemini_roundtrip() -> bool:
    section("3. Claude round-trip (text)")
    from app.browser_automation.llm import get_llm, LLMUnavailable
    try:
        client = get_llm()
        reply = await client.generate_text(
            "Reply with one word: pong", temperature=0.0, timeout_s=20.0
        )
        print(f"  OK    Reply: {reply!r}")
        return True
    except LLMUnavailable as exc:
        print(f"  FAIL  {exc}")
        return False
    except Exception as exc:
        print(f"  FAIL  {exc}")
        traceback.print_exc()
        return False


async def check_gemini_json() -> bool:
    section("4. Claude JSON mode")
    from app.browser_automation.llm import get_llm, LLMUnavailable
    try:
        client = get_llm()
        data = await client.generate_json(
            'Return exactly: {"ok": true, "name": "agent"}',
            temperature=0.0, timeout_s=20.0,
        )
        print(f"  OK    Parsed JSON: {json.dumps(data)[:120]}")
        return isinstance(data, dict)
    except LLMUnavailable as exc:
        print(f"  FAIL  {exc}")
        return False
    except Exception as exc:
        print(f"  FAIL  {exc}")
        return False


async def check_learned_fixes_io() -> bool:
    section("5. learned_fixes read/write")
    from app.browser_automation.agent import get_learned_fixes
    try:
        cache = get_learned_fixes("__doctor_test__")
        cache.add("apply_button", "button.doctor-test")
        got = cache.get("apply_button")
        assert "button.doctor-test" in got, f"selector not persisted: {got}"
        print(f"  OK    selectors round-trip: {got}")
        # Cleanup
        from app.browser_automation.agent.learned_fixes import _path_for
        p = _path_for("__doctor_test__")
        if p.exists():
            p.unlink()
        return True
    except Exception as exc:
        print(f"  FAIL  {exc}")
        return False


async def check_celery_registry() -> bool:
    section("6. Celery task registry")
    try:
        from app.celery_app import celery_app
        # Force-import task modules so the registry is populated
        import app.tasks.browser_automation  # noqa: F401
        import app.tasks.dynamic_apply  # noqa: F401
        names = set(celery_app.tasks.keys())
        required = {
            "task:execute_application",
            "task:dynamic_apply",
        }
        missing = required - names
        if missing:
            print(f"  FAIL  missing tasks: {missing}")
            return False
        print(f"  OK    registered tasks include: {sorted(required)}")
        return True
    except Exception as exc:
        print(f"  FAIL  {exc}")
        return False


async def main():
    results = []
    results.append(("imports", await check_imports()))
    if results[-1][1]:
        results.append(("gemini_key", await check_gemini_key()))
        if results[-1][1]:
            results.append(("gemini_text", await check_gemini_roundtrip()))
            results.append(("gemini_json", await check_gemini_json()))
        results.append(("learned_fixes", await check_learned_fixes_io()))
        results.append(("celery_registry", await check_celery_registry()))

    section("SUMMARY")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = [n for n, ok in results if not ok]
    if bad:
        print(f"\n{len(bad)} check(s) failed: {bad}")
        sys.exit(1)
    print("\nAll checks passed. Agent wiring is healthy.")


if __name__ == "__main__":
    asyncio.run(main())
