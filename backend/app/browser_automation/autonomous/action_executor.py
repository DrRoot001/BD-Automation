"""ActionExecutor — turns a reasoned :class:`NextAction` into a Playwright op.

It executes ONE action against the live page/frame and reports whether it ran
(:class:`ActionResult`). It makes no decisions and observes nothing about
success beyond "did the operation raise" — judging the *effect* is the job of
the next OBSERVE turn (that is the whole point of the observe→reason→act→observe
cycle: we never assume an action worked).

Verification / captcha actions are intentionally NOT executed here — the agent
routes those to injected handlers (or halts), because they require external
services (Gmail, a captcha solver), not a DOM operation.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from ..reasoning.models import ActionType, NextAction
from .models import ActionResult

logger = logging.getLogger(__name__)

_NEXT_SELECTORS = (
    "button:has-text('Next')", "button:has-text('Continue')", "button:has-text('Review')",
    "[role='button']:has-text('Next')", "input[type='button'][value='Next']",
)
_SUBMIT_SELECTORS = (
    "button[type='submit']", "input[type='submit']",
    "button:has-text('Submit application')", "button:has-text('Submit Application')",
    "button:has-text('Send application')", "button:has-text('Submit')",
)


class ActionExecutor:
    def __init__(self, click_timeout_ms: int = 6_000, fill_timeout_ms: int = 6_000):
        self.click_timeout_ms = click_timeout_ms
        self.fill_timeout_ms = fill_timeout_ms

    async def execute(
        self,
        page: Any,
        frame: Optional[Any],
        action: NextAction,
        context: Optional[Dict[str, Any]] = None,
    ) -> ActionResult:
        ctx = frame or page
        context = context or {}
        try:
            handler = getattr(self, f"_do_{action.type.name.lower()}", None)
            if handler is None:
                return ActionResult(ok=False, note=f"no executor for {action.type.value}")
            return await handler(page, ctx, action, context)
        except Exception as exc:
            logger.debug(f"[ActionExecutor] {action.type.value} raised: {exc}")
            return ActionResult(ok=False, error=str(exc), note=f"{action.type.value} raised")

    # ── no-op / observational ────────────────────────────────────────────────
    async def _do_observe(self, page, ctx, a, c) -> ActionResult:
        return ActionResult(ok=True, note="observe")

    async def _do_wait(self, page, ctx, a, c) -> ActionResult:
        await asyncio.sleep(float(c.get("wait_s", 1.5)))
        return ActionResult(ok=True, note="waited")

    # ── page interaction ─────────────────────────────────────────────────────
    async def _do_scroll(self, page, ctx, a, c) -> ActionResult:
        dy = -700 if (a.direction == "up") else 700
        try:
            await page.evaluate("(y) => window.scrollBy(0, y)", dy)
        except Exception:
            pass
        return ActionResult(ok=True, note=f"scroll {a.direction or 'down'}")

    async def _click_selector(self, ctx, selector: str, text: Optional[str]) -> ActionResult:
        # HARD POLICY GUARD: manual email+password login ONLY — never a social/
        # SSO sign-in control (Continue with Google/Apple/…). Refuse by the
        # selector/text up front, and again on the resolved element below.
        try:
            from ..agent.loop import _is_sso_text, _is_sso_href
        except Exception:  # pragma: no cover - defensive
            _is_sso_text = _is_sso_href = lambda _s: False
        if _is_sso_text(f"{selector or ''} {text or ''}"):
            return ActionResult(
                ok=False,
                note="SSO/social login disabled by policy — use the email+password form",
            )
        loc = None
        if selector:
            loc = ctx.locator(selector).first
        elif text:
            loc = ctx.get_by_text(text, exact=False).first
        if loc is None:
            return ActionResult(ok=False, note="no selector/text to click")
        # Re-check the RESOLVED element — a generic selector may resolve to a
        # Google/Apple button or an OAuth link.
        try:
            _el_text = await loc.inner_text(timeout=1_500)
        except Exception:
            _el_text = ""
        try:
            _el_href = await loc.get_attribute("href")
        except Exception:
            _el_href = ""
        if _is_sso_text(_el_text) or _is_sso_href(_el_href):
            return ActionResult(
                ok=False,
                note="SSO/social login disabled by policy — use the email+password form",
            )
        # Resolving the locator can RAISE on a malformed selector the reasoner
        # emitted (bad CSS/xpath) — that propagated as a hard "CLICK raised" and
        # stalled the run. Guard it so a bad selector is a clean ok=False the
        # reasoner can re-plan around, not a crash.
        try:
            if await loc.count() == 0:
                return ActionResult(ok=False, note=f"selector not found: {selector or text!r}")
        except Exception as exc:
            return ActionResult(ok=False, error=str(exc), note=f"invalid selector: {selector or text!r}")
        try:
            await loc.scroll_into_view_if_needed(timeout=2_000)
        except Exception:
            pass
        # Plain click first; on an actionability failure (Ashby's styled/opacity:0
        # Yes/No buttons where the visible element isn't the real event target, or
        # a transient overlay), fall back to a FORCE click, then a JS .click().
        # This is what stops the "CLICK raised" spiral on Ashby's custom widgets.
        try:
            await loc.click(timeout=self.click_timeout_ms)
            return ActionResult(ok=True, note=f"clicked {selector or text!r}")
        except Exception:
            pass
        try:
            await loc.click(timeout=self.click_timeout_ms, force=True)
            return ActionResult(ok=True, note=f"clicked {selector or text!r} (force)")
        except Exception:
            pass
        try:
            await loc.evaluate("el => el.click()")
            return ActionResult(ok=True, note=f"clicked {selector or text!r} (js)")
        except Exception as exc:
            return ActionResult(ok=False, error=str(exc), note=f"click raised: {selector or text!r}")

    async def _do_click(self, page, ctx, a, c) -> ActionResult:
        return await self._click_selector(ctx, a.selector, a.value or a.click_text)

    async def _do_click_apply(self, page, ctx, a, c) -> ActionResult:
        if a.selector or a.value:
            return await self._click_selector(ctx, a.selector, a.value)
        # discover an Apply control
        for sel in ("a:has-text('Apply')", "button:has-text('Apply')",
                    "a#apply_button", "[data-qa='btn-apply']"):
            r = await self._click_selector(ctx, sel, None)
            if r.ok:
                return r
        return ActionResult(ok=False, note="no apply control found")

    async def _do_fill(self, page, ctx, a, c) -> ActionResult:
        if not a.selector:
            return ActionResult(ok=False, note="fill missing selector")
        value = a.value or ""
        # password secret substitution (never let a placeholder reach the field)
        secrets = c.get("secrets") or {}
        if a.selector and secrets:
            lbl = (a.field_label or "").lower()
            if "password" in lbl and secrets.get("password"):
                value = secrets["password"]
        loc = ctx.locator(a.selector).first
        if await loc.count() == 0:
            return ActionResult(ok=False, note=f"fill target not found: {a.selector!r}")

        # Combobox / autocomplete (Ashby Location, react-select typed input): typing
        # opens a suggestion menu that MUST be committed AND closed, or its overlay
        # intercepts every later click — the observed STUCK death-spiral. Route
        # these through a commit that picks the option (or Enter) and closes the menu.
        try:
            is_combo = await loc.evaluate(
                """el => {
                    const r = (el.getAttribute('role') || '').toLowerCase();
                    if (r === 'combobox') return true;
                    if (el.getAttribute('aria-autocomplete')) return true;
                    if (el.getAttribute('aria-expanded') !== null) return true;
                    return !!el.closest(
                        '.select__control, .react-select__control,'
                        + " [class*='autocomplete'], [class*='Autocomplete'], [class*='combobox']"
                    );
                }"""
            )
        except Exception:
            is_combo = False
        if is_combo:
            return await self._commit_combobox_fill(page, ctx, loc, value, a)

        try:
            await loc.fill(value, timeout=self.fill_timeout_ms)
        except Exception:
            # controlled inputs / contenteditable: fall back to type
            await loc.click(timeout=self.click_timeout_ms)
            await loc.type(value, delay=15)
        return ActionResult(ok=True, note=f"filled {a.field_label or a.selector!r}")

    async def _commit_combobox_fill(self, page, ctx, loc, value: str, a) -> ActionResult:
        """Type `value` into a combobox/autocomplete, then COMMIT the suggestion
        (click the best match, else Enter) and ensure the menu is CLOSED so it
        can't block later clicks. This is the fix for the Ashby-location STUCK
        spiral where a fill left the suggestion list hanging open."""
        try:
            await loc.click(timeout=self.click_timeout_ms)
        except Exception:
            pass
        try:
            await loc.fill("")  # clear any prior text so the filter is clean
        except Exception:
            pass
        try:
            await loc.type(str(value)[:60], delay=15)
        except Exception:
            try:
                await loc.fill(value, timeout=self.fill_timeout_ms)
            except Exception:
                return ActionResult(ok=False, note=f"combobox type failed for {value!r}")
        await asyncio.sleep(0.5)
        picked = await self._pick_open_option(ctx, value)
        if not picked:
            # commit the highlighted top suggestion
            try:
                await page.keyboard.press("Enter")
                await asyncio.sleep(0.2)
            except Exception:
                pass
        # ensure the suggestion menu is closed (it must not linger and block clicks)
        try:
            if await self._menu_is_open(ctx):
                await page.keyboard.press("Escape")
        except Exception:
            pass
        return ActionResult(
            ok=True,
            note=f"combobox {a.field_label or a.selector!r} = {value!r}"
            + ("" if picked else " (Enter)"),
        )

    async def _menu_is_open(self, ctx) -> bool:
        """True if a react-select/autocomplete option menu is currently visible."""
        for sel in (".select__menu", "[role='listbox']", "[role='option']", ".select__option"):
            try:
                first = ctx.locator(sel).first
                if await first.count() > 0 and await first.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _do_select_option(self, page, ctx, a, c) -> ActionResult:
        if not a.selector:
            return ActionResult(ok=False, note="select missing selector")
        return await self._commit_dropdown(page, ctx, a.selector, a.value or "")

    async def _commit_dropdown(self, page, ctx, selector: str, value: str) -> ActionResult:
        """Commit a value into EITHER a native <select> OR a custom react-select /
        combobox widget (Greenhouse/Ashby/Lever/Workday). Native selects use
        select_option; custom widgets are opened, filtered, and the matching
        option is clicked (with a keyboard fallback)."""
        loc = ctx.locator(selector).first
        if await loc.count() == 0:
            return ActionResult(ok=False, note=f"dropdown not found: {selector!r}")

        try:
            tag = (await loc.evaluate("el => el.tagName.toLowerCase()")) or ""
        except Exception:
            tag = ""
        try:
            el_type = (await loc.evaluate("el => (el.getAttribute('type') || '').toLowerCase()")) or ""
        except Exception:
            el_type = ""

        # 0. Radio GROUP (Ashby/React "labeled-radio": visually-hidden opacity:0
        # inputs that ALL share value="on", with the option text in a
        # <label for=id>). A native select_option won't work and the value read
        # back is always "on"; commit by matching the LABEL text and clicking the
        # label so React onChange fires. Falls through to the widget path if the
        # group can't be resolved.
        if el_type == "radio":
            r = await self._commit_radio_group(ctx, loc, value)
            if r is not None:
                return r

        # 1. Native <select>
        if tag == "select":
            for kwargs in ({"label": value}, {"value": value}):
                try:
                    await loc.select_option(**kwargs)
                    return ActionResult(ok=True, note=f"native select {value!r}")
                except Exception:
                    continue
            return ActionResult(ok=False, note=f"native select_option rejected {value!r}")

        # 2. Custom react-select / combobox: open → filter → click the option.
        try:
            await loc.scroll_into_view_if_needed(timeout=2_000)
        except Exception:
            pass
        opened = False
        for opener in (loc, ctx.locator(selector).first.locator(
                "xpath=ancestor-or-self::*[contains(@class,'select__control') or "
                "@role='combobox' or contains(@class,'combobox')][1]")):
            try:
                await opener.click(timeout=self.click_timeout_ms)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            return ActionResult(ok=False, note="could not open custom dropdown")
        await asyncio.sleep(0.35)

        # type to filter (react-select's input narrows the option list)
        try:
            await loc.type(str(value)[:40], delay=15)
            await asyncio.sleep(0.4)
        except Exception:
            pass

        if await self._pick_open_option(ctx, value):
            return ActionResult(ok=True, note=f"picked option {value!r}")

        # keyboard fallback — react-select commits the highlighted option on Enter
        try:
            await page.keyboard.press("Enter")
            return ActionResult(ok=True, note=f"committed {value!r} via Enter")
        except Exception as exc:
            return ActionResult(ok=False, note=f"could not pick option {value!r}: {exc}")

    async def _pick_open_option(self, ctx, value: str) -> bool:
        """Click the option matching `value` in an ALREADY-OPEN menu (react-select
        menu / autocomplete listbox). Match priority: exact > value-in-option >
        option-in-value. Returns True if an option was clicked."""
        val = str(value).strip().lower()
        for opt_sel in ("[role='option']", ".select__option", "li[role='option']",
                        "[class*='option']"):
            opts = ctx.locator(opt_sel)
            try:
                count = min(await opts.count(), 40)
            except Exception:
                count = 0
            exact = contains = contained = None
            for i in range(count):
                o = opts.nth(i)
                try:
                    if not await o.is_visible():
                        continue
                    t = (await o.inner_text()).strip().lower()
                except Exception:
                    continue
                if not t:
                    continue
                if t == val:
                    exact = o
                    break
                if contains is None and val in t:
                    contains = o
                elif contained is None and len(t) >= 2 and t in val:
                    contained = o
            best = exact or contains or contained
            if best is not None:
                try:
                    await best.scroll_into_view_if_needed(timeout=1_500)
                    await best.click(timeout=3_000)
                    return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _pick_closest_option(value: str, options: list) -> str:
        """Match `value` to the best option: exact (ci) > value-in-option >
        option-in-value > first. Mirrors the legacy loop's option matcher without
        importing from it (keeps the autonomous framework self-contained)."""
        v = (value or "").strip().lower()
        if not options:
            return value
        for o in options:
            if o.strip().lower() == v:
                return o
        for o in options:
            if v and v in o.strip().lower():
                return o
        for o in options:
            ol = o.strip().lower()
            if len(ol) >= 2 and ol in v:
                return o
        return options[0]

    async def _commit_radio_group(self, ctx, loc, value: str):
        """Select the option matching `value` inside the radio GROUP `loc` belongs
        to, by matching each option's LABEL text, then clicking that label so a
        React/SPA change handler fires. Returns an ActionResult, or None when the
        group can't be resolved (caller falls through to the widget path)."""
        try:
            name = await loc.get_attribute("name")
        except Exception:
            name = None
        if not name:
            return None
        try:
            options = await ctx.evaluate(
                """(name) => {
                    const labels = Array.from(document.querySelectorAll('label[for]'));
                    const forText = (id) => {
                        const l = labels.find(x => x.getAttribute('for') === id);
                        return l ? (l.textContent || '').trim() : '';
                    };
                    return Array.from(document.querySelectorAll('input[type=radio]'))
                        .filter(i => i.name === name)
                        .map(inp => {
                            let t = inp.id ? forText(inp.id) : '';
                            if (!t) { const w = inp.closest('label'); if (w) t = (w.textContent || '').trim(); }
                            if (!t) t = (inp.getAttribute('aria-label') || '').trim();
                            return { id: inp.id, text: t.slice(0, 80) };
                        });
                }""",
                name,
            )
        except Exception:
            options = []
        texts = [o["text"] for o in (options or []) if o.get("text")]
        if not texts:
            return None
        target = self._pick_closest_option(value, texts)
        chosen = next((o for o in options if o.get("text") == target), None)
        if not chosen or not chosen.get("id"):
            return None
        try:
            clicked = await ctx.evaluate(
                """(id) => {
                    const inp = document.getElementById(id);
                    if (!inp) return false;
                    const lbl = Array.from(document.querySelectorAll('label[for]'))
                        .find(x => x.getAttribute('for') === id) || inp.closest('label');
                    (lbl || inp).click();
                    return true;
                }""",
                chosen["id"],
            )
        except Exception:
            clicked = False
        if not clicked:
            return None
        return ActionResult(
            ok=True,
            note=f"radio {target!r}" + ("" if target == value else " (closest)"),
        )

    async def _do_upload(self, page, ctx, a, c) -> ActionResult:
        if not a.selector:
            return ActionResult(ok=False, note="upload missing selector")
        which = (a.value or "resume").lower()
        path = c.get("resume_path") if which == "resume" else c.get("cover_letter_path")
        if not path:
            return ActionResult(ok=False, note=f"no {which} path in context")
        loc = ctx.locator(a.selector).first
        if await loc.count() == 0:
            return ActionResult(ok=False, note=f"upload input not found: {a.selector!r}")
        await loc.set_input_files(path)
        return ActionResult(ok=True, note=f"uploaded {which}")

    async def _do_next_step(self, page, ctx, a, c) -> ActionResult:
        for sel in _NEXT_SELECTORS:
            r = await self._click_selector(ctx, sel, None)
            if r.ok:
                return r
        return ActionResult(ok=False, note="no Next/Continue found")

    async def _do_navigate(self, page, ctx, a, c) -> ActionResult:
        if not a.url:
            return ActionResult(ok=False, note="navigate missing url")
        try:
            await page.goto(a.url, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:
            return ActionResult(ok=False, error=str(exc), note="navigation failed")
        return ActionResult(ok=True, note=f"navigated {a.url}")

    async def _do_submit(self, page, ctx, a, c) -> ActionResult:
        if a.selector:
            r = await self._click_selector(ctx, a.selector, None)
            if r.ok:
                return r
        for sel in _SUBMIT_SELECTORS:
            r = await self._click_selector(ctx, sel, None)
            if r.ok:
                return r
        return ActionResult(ok=False, note="no submit control found")
