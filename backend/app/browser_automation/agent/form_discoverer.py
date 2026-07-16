"""Universal application-form discovery — find the form on ANY portal.

WHY THIS EXISTS
---------------
The fill engine is already portal-agnostic: ``agent/loop.py`` fills whatever
fields it can see, and the per-ATS adapters are thin (Greenhouse's is ~145 lines
and its own docstring says the Apply click, dropdowns, EEO block, uploads,
submit and verification are "handled by the shared perception + reasoning loop").

So the thing that actually blocks a never-seen portal is not *filling* — it is
**finding the form in the first place**:

  * ``adapters/generic.py`` sets no ``iframe_selector``, and
    ``AutonomousAdapter._resolve_frame`` early-returns when it is unset. An
    unknown portal that renders its form in an iframe (the Greenhouse / Workday
    / iCIMS pattern) is therefore invisible to the loop — it scopes to the page,
    sees no fields, and fails. No amount of model reasoning fixes that, because
    the form was never in the model's context.
  * The form is often revealed only after an "Apply" CTA is clicked.

This module closes that gap deterministically. It answers one question — *where
is the application form?* — using evidence from the live DOM, and returns a
frame the loop can be scoped to.

DESIGN
------
Cheapest-first, and **no LLM on the common paths**:

  1. Score the main frame. A real application form has a recognisable shape
     (text inputs + an email field + a file input + a submit control), so we
     score rather than pattern-match a selector.
  2. Score every child iframe, cross-origin ones included (Playwright can
     evaluate inside them), and take the best scorer.
  3. If nothing scores, look for an Apply CTA, click it, and re-scan — the form
     is frequently injected only on that click.
  4. Pierce open shadow roots (SmartRecruiters-style web components).

The LLM is a LAST resort, not the strategy — see ``describe_for_llm``, which
hands the caller a compact page description to ask a model "where is the form?"
only when every deterministic probe has come up empty.

Scoring, not matching, is the point: it degrades gracefully on markup nobody has
seen before, which is exactly the case adapters cannot cover.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: A frame must score at least this much to be called an application form.
#: Tuned so a lone search box or newsletter signup (score ~1-2) never wins but a
#: real form (email + file + several inputs + submit ≈ 8+) always does.
MIN_FORM_SCORE = 5

#: Apply CTAs, ordered most- to least- specific. Text matched case-insensitively.
_APPLY_TEXTS = (
    "apply for this job",
    "apply now",
    "apply to this job",
    "submit application",
    "start application",
    "apply",
)

# Scores the *shape* of a form in whatever frame it runs in. Deliberately
# generic: no vendor selectors, only what an application form inherently has.
_FORM_SCORE_JS = r"""
() => {
  const vis = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    } catch (e) { return false; }
  };
  const all = Array.from(document.querySelectorAll('input, select, textarea'));
  const fields = all.filter(el => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'hidden') return false;
    if (['submit', 'button', 'reset', 'image'].includes(t)) return false;
    return vis(el);
  });

  const txt = (document.body ? (document.body.innerText || '') : '').toLowerCase();
  const hasEmail = fields.some(el =>
    (el.getAttribute('type') || '').toLowerCase() === 'email' ||
    /e-?mail/i.test((el.getAttribute('name') || '') + (el.getAttribute('id') || '') +
                    (el.getAttribute('aria-label') || '') + (el.getAttribute('placeholder') || '')));
  const hasFile = all.some(el => (el.getAttribute('type') || '').toLowerCase() === 'file');
  const hasName = fields.some(el =>
    /first.?name|last.?name|full.?name|\bname\b/i.test(
      (el.getAttribute('name') || '') + (el.getAttribute('id') || '') +
      (el.getAttribute('aria-label') || '') + (el.getAttribute('placeholder') || '')));
  const submits = Array.from(document.querySelectorAll(
    'button, input[type=submit], [role=button]')).filter(vis).filter(el => {
      const s = ((el.innerText || '') + (el.value || '')).toLowerCase();
      return /submit|apply|send application|continue|next/.test(s);
    });
  const resumeish = /resum|cv\b|cover letter|upload/i.test(txt);

  // Evidence -> score. An application form is the conjunction of these, so no
  // single weak signal can carry a frame over the threshold on its own.
  let score = 0;
  if (fields.length >= 3) score += 2;
  if (fields.length >= 6) score += 1;
  if (hasEmail) score += 2;
  if (hasName) score += 2;
  if (hasFile) score += 3;
  if (submits.length > 0) score += 2;
  if (resumeish) score += 1;
  if (document.querySelector('form')) score += 1;

  return {
    score,
    fields: fields.length,
    hasEmail, hasFile, hasName,
    submits: submits.length,
    resumeish,
    url: location.href,
  };
}
"""

# Finds a clickable Apply CTA and reports where it is (no click here).
_APPLY_PROBE_JS = r"""
(texts) => {
  const vis = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    } catch (e) { return false; }
  };
  const cands = Array.from(document.querySelectorAll('a, button, [role=button], input[type=submit]'))
    .filter(vis);
  for (const want of texts) {
    for (const el of cands) {
      const s = ((el.innerText || '') + ' ' + (el.value || '') + ' ' +
                 (el.getAttribute('aria-label') || '')).trim().toLowerCase();
      if (!s) continue;
      if (s === want || s.includes(want)) {
        return {
          found: true, text: (el.innerText || el.value || '').trim().slice(0, 60),
          id: el.id || null, tag: el.tagName.toLowerCase(),
        };
      }
    }
  }
  return { found: false };
}
"""


@dataclass
class FormLocation:
    """Where the application form is, and the evidence that says so."""

    found: bool = False
    #: "page" | "iframe" | "shadow" | "none"
    where: str = "none"
    #: Playwright selector for the iframe when ``where == "iframe"``.
    iframe_selector: Optional[str] = None
    score: int = 0
    fields: int = 0
    has_file: bool = False
    has_email: bool = False
    apply_clicked: bool = False
    url: Optional[str] = None
    evidence: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.found:
            return f"no application form found (best score={self.score}, need >={MIN_FORM_SCORE})"
        loc = self.iframe_selector or self.where
        return (
            f"form in {loc} score={self.score} fields={self.fields} "
            f"file={self.has_file} email={self.has_email} "
            f"apply_clicked={self.apply_clicked}"
        )


class FormDiscoverer:
    """Locate the application form on an arbitrary page. Never raises."""

    @classmethod
    async def discover(cls, page: Any, *, allow_apply_click: bool = True) -> FormLocation:
        """Find the form, clicking an Apply CTA if that's what it takes.

        Returns a :class:`FormLocation`. ``found=False`` is a legitimate answer
        (login wall, expired posting, external redirect) — the caller decides
        what to do about it rather than this guessing.
        """
        best = await cls._scan_all_frames(page)
        if best.found:
            return best

        if allow_apply_click:
            clicked = await cls._click_apply(page)
            if clicked:
                await cls._settle(page)
                after = await cls._scan_all_frames(page)
                after.apply_clicked = True
                if after.found:
                    after.evidence.insert(0, "revealed by Apply click")
                    return after
                best = after if after.score > best.score else best
                best.apply_clicked = True

        shadow = await cls._scan_shadow(page)
        if shadow.found:
            return shadow

        return best

    # ── frame scanning ───────────────────────────────────────────────────────

    @classmethod
    async def _scan_all_frames(cls, page: Any) -> FormLocation:
        """Score the main frame and every child iframe; return the best."""
        best = FormLocation()

        main = await cls._score_frame(page, "page", None)
        if main and main.score > best.score:
            best = main

        try:
            frames = list(page.frames)
        except Exception:
            frames = []

        for fr in frames:
            try:
                if fr is page.main_frame:
                    continue
            except Exception:
                pass
            sel = await cls._selector_for_frame(fr)
            got = await cls._score_frame(fr, "iframe", sel)
            if got and got.score > best.score:
                best = got

        best.found = best.score >= MIN_FORM_SCORE
        return best

    @classmethod
    async def _score_frame(
        cls, ctx: Any, where: str, iframe_selector: Optional[str]
    ) -> Optional[FormLocation]:
        try:
            r = await ctx.evaluate(_FORM_SCORE_JS)
        except Exception as exc:
            logger.debug(f"[FormDiscoverer] score failed for {where} {iframe_selector}: {exc}")
            return None
        if not isinstance(r, dict):
            return None

        ev: List[str] = []
        if r.get("hasEmail"):
            ev.append("email field")
        if r.get("hasName"):
            ev.append("name field")
        if r.get("hasFile"):
            ev.append("file input")
        if r.get("submits"):
            ev.append(f"{r['submits']} submit control(s)")
        if r.get("resumeish"):
            ev.append("resume/cover wording")
        ev.append(f"{r.get('fields', 0)} visible field(s)")

        return FormLocation(
            found=int(r.get("score", 0)) >= MIN_FORM_SCORE,
            where=where,
            iframe_selector=iframe_selector,
            score=int(r.get("score", 0)),
            fields=int(r.get("fields", 0)),
            has_file=bool(r.get("hasFile")),
            has_email=bool(r.get("hasEmail")),
            url=r.get("url"),
            evidence=ev,
        )

    @staticmethod
    async def _selector_for_frame(frame: Any) -> Optional[str]:
        """Build a Playwright selector that resolves back to this iframe.

        Prefers a stable id/name; falls back to a src fragment. Returns None if
        the frame can't be addressed — the caller then can't scope to it.
        """
        try:
            el = await frame.frame_element()
        except Exception:
            el = None
        if el is not None:
            for attr, tmpl in (("id", "iframe#{}"), ("name", 'iframe[name="{}"]')):
                try:
                    v = await el.get_attribute(attr)
                except Exception:
                    v = None
                if v:
                    return tmpl.format(v)
        try:
            src = frame.url or ""
        except Exception:
            src = ""
        if src and src not in ("about:blank",):
            frag = src.split("?")[0][:120]
            if frag:
                return f'iframe[src*="{frag}"]'
        return None

    # ── Apply CTA ────────────────────────────────────────────────────────────

    @classmethod
    async def _click_apply(cls, page: Any) -> bool:
        """Click an Apply CTA if one is present. True if something was clicked."""
        try:
            probe = await page.evaluate(_APPLY_PROBE_JS, list(_APPLY_TEXTS))
        except Exception as exc:
            logger.debug(f"[FormDiscoverer] apply probe failed: {exc}")
            return False
        if not isinstance(probe, dict) or not probe.get("found"):
            return False

        if probe.get("id"):
            try:
                loc = page.locator(f"#{probe['id']}").first
                await loc.scroll_into_view_if_needed(timeout=3_000)
                await loc.click(timeout=6_000)
                logger.info(f"[FormDiscoverer] clicked Apply via #{probe['id']}")
                return True
            except Exception as exc:
                logger.debug(f"[FormDiscoverer] id click failed: {exc}")

        text = (probe.get("text") or "").strip()
        if text:
            try:
                loc = page.get_by_text(text, exact=False).first
                await loc.scroll_into_view_if_needed(timeout=3_000)
                await loc.click(timeout=6_000)
                logger.info(f"[FormDiscoverer] clicked Apply via text {text!r}")
                return True
            except Exception as exc:
                logger.debug(f"[FormDiscoverer] text click failed: {exc}")
        return False

    @staticmethod
    async def _settle(page: Any) -> None:
        """Let an injected iframe / SPA route render before re-scanning."""
        try:
            await page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            pass
        try:
            await page.wait_for_timeout(900)
        except Exception:
            pass

    # ── shadow DOM ───────────────────────────────────────────────────────────

    @classmethod
    async def _scan_shadow(cls, page: Any) -> FormLocation:
        """Look for form fields inside open shadow roots (web-component ATSes)."""
        js = r"""
        () => {
          let n = 0, file = false, email = false;
          const walk = (root, depth) => {
            if (depth > 4) return;
            for (const el of root.querySelectorAll('*')) {
              if (el.shadowRoot) {
                for (const f of el.shadowRoot.querySelectorAll('input, select, textarea')) {
                  const t = (f.getAttribute('type') || '').toLowerCase();
                  if (t === 'hidden') continue;
                  n++;
                  if (t === 'file') file = true;
                  if (t === 'email') email = true;
                }
                walk(el.shadowRoot, depth + 1);
              }
            }
          };
          try { walk(document, 0); } catch (e) {}
          return { n, file, email };
        }
        """
        try:
            r = await page.evaluate(js)
        except Exception:
            return FormLocation()
        if not isinstance(r, dict) or int(r.get("n", 0)) < 3:
            return FormLocation()
        score = 2 + (3 if r.get("file") else 0) + (2 if r.get("email") else 0)
        return FormLocation(
            found=score >= MIN_FORM_SCORE,
            where="shadow",
            score=score,
            fields=int(r.get("n", 0)),
            has_file=bool(r.get("file")),
            has_email=bool(r.get("email")),
            evidence=[f"{r.get('n')} field(s) in shadow DOM"],
        )

    # ── LLM last resort ──────────────────────────────────────────────────────

    @classmethod
    async def describe_for_llm(cls, page: Any) -> str:
        """Compact page description for a 'where is the form?' LLM question.

        Only for when every deterministic probe returned nothing. Kept small on
        purpose — this runs on the expensive tier.
        """
        js = r"""
        () => {
          const frames = Array.from(document.querySelectorAll('iframe')).map(f => ({
            id: f.id || null, name: f.name || null, src: (f.src || '').slice(0, 120),
          }));
          const btns = Array.from(document.querySelectorAll('a, button'))
            .map(b => (b.innerText || '').trim()).filter(Boolean).slice(0, 25);
          return {
            url: location.href,
            title: document.title,
            iframes: frames,
            buttons: btns,
            bodyStart: (document.body ? document.body.innerText : '').slice(0, 600),
          };
        }
        """
        try:
            r = await page.evaluate(js)
        except Exception as exc:
            return f"(page description unavailable: {exc})"
        lines = [f"URL: {r.get('url')}", f"TITLE: {r.get('title')}"]
        if r.get("iframes"):
            lines.append("IFRAMES:")
            for f in r["iframes"]:
                lines.append(f"  id={f.get('id')} name={f.get('name')} src={f.get('src')}")
        if r.get("buttons"):
            lines.append("CLICKABLE: " + " | ".join(r["buttons"]))
        lines.append("TEXT: " + (r.get("bodyStart") or "").replace("\n", " ")[:600])
        return "\n".join(lines)
