"""In-page JavaScript used by :class:`.collector.BrowserStateCollector`.

Two scripts:

* :data:`EXTRACT_STATE_JS` — one DOM sweep that returns a JSON-serialisable
  object describing forms, inputs, buttons, messages, modals, visible text and
  navigation state. It pierces open shadow roots, resolves human labels, and
  builds a stable selector for every element.
* :data:`WAIT_STABLE_JS` — a MutationObserver-based quiescence probe that
  resolves once the DOM has stopped mutating for a quiet window (or a hard
  timeout elapses).

Both are pure, side-effect-free (no clicks / no writes) and never throw out of
the browser — the collector still degrades gracefully if evaluate() fails.
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Full page-state extraction.  Signature: (opts) => ({...})
# opts = {maxFields, maxButtons, maxMessages, maxModals, htmlMaxLen, textMaxLen}
# ─────────────────────────────────────────────────────────────────────────────
EXTRACT_STATE_JS = r"""
(opts) => {
  opts = opts || {};
  const MAX_FIELDS   = opts.maxFields   || 300;
  const MAX_BUTTONS  = opts.maxButtons  || 120;
  const MAX_MESSAGES = opts.maxMessages || 50;
  const MAX_MODALS   = opts.maxModals   || 12;
  const HTML_MAX     = opts.htmlMaxLen  || 500000;
  const TEXT_MAX     = opts.textMaxLen  || 20000;

  // ── helpers ───────────────────────────────────────────────────────────────
  const esc = (s) => {
    try {
      if (window.CSS && CSS.escape) return CSS.escape(s);
      // Fallback: backslash-escape anything that isn't a safe identifier char.
      return String(s).replace(/([^a-zA-Z0-9_-])/g, '\\$1');
    } catch (e) { return String(s); }
  };

  // Walk open shadow roots so web-component form fields are reachable.
  function deepQueryAll(selector, root, acc, depth) {
    root = root || document; acc = acc || []; depth = depth || 0;
    if (depth > 12) return acc;
    try { root.querySelectorAll(selector).forEach(m => acc.push(m)); } catch (e) {}
    let hosts;
    try { hosts = root.querySelectorAll('*'); } catch (e) { hosts = []; }
    for (const el of hosts) { if (el.shadowRoot) deepQueryAll(selector, el.shadowRoot, acc, depth + 1); }
    return acc;
  }

  function isVisible(el) {
    if (!el || el.nodeType !== 1) return false;
    if (typeof el.checkVisibility === 'function') {
      try { return el.checkVisibility({ checkVisibilityCSS: true, checkOpacity: true }); } catch (e) {}
    }
    let s; try { s = getComputedStyle(el); } catch (e) { return false; }
    if (!s) return false;
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    if (parseFloat(s.opacity || '1') === 0) return false;
    return el.offsetParent !== null || s.position === 'fixed';
  }

  // A stable-ish selector: #id > [data-testid] > tag[name] > nth-of-type path.
  function selectorFor(el) {
    if (!el || el.nodeType !== 1) return '';
    if (el.id) return '#' + esc(el.id);
    for (const a of ['data-testid', 'data-qa', 'data-test', 'data-cy']) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v) return el.tagName.toLowerCase() + '[' + a + '="' + v + '"]';
    }
    const tag = el.tagName.toLowerCase();
    if (el.name && /^(input|select|textarea|button)$/.test(tag)) {
      return tag + '[name="' + el.name + '"]';
    }
    // Build a short structural path (bounded depth).
    const parts = [];
    let node = el, depth = 0;
    while (node && node.nodeType === 1 && depth < 6) {
      if (node.id) { parts.unshift('#' + esc(node.id)); break; }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = node.parentElement; depth++;
    }
    return parts.join(' > ');
  }

  function labelFor(el) {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria.trim().slice(0, 120);
    const ariaBy = el.getAttribute && el.getAttribute('aria-labelledby');
    if (ariaBy) {
      const parts = ariaBy.split(/\s+/).map(id => {
        const n = document.getElementById(id); return n ? (n.textContent || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ').trim().slice(0, 120);
    }
    if (el.id) {
      const scope = el.getRootNode ? el.getRootNode() : document;
      let lbl = null;
      try { lbl = (scope.querySelector ? scope.querySelector('label[for="' + esc(el.id) + '"]') : null); } catch (e) {}
      if (!lbl) { try { lbl = document.querySelector('label[for="' + esc(el.id) + '"]'); } catch (e) {} }
      if (lbl) return (lbl.textContent || '').trim().slice(0, 120);
    }
    // wrapping <label>
    let wrap = el.closest ? el.closest('label') : null;
    if (wrap) {
      const clone = wrap.cloneNode(true);
      clone.querySelectorAll('input, select, textarea').forEach(c => c.remove());
      const t = (clone.textContent || '').trim();
      if (t) return t.slice(0, 120);
    }
    // nearby ancestor label / legend
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++) {
      const l = p.querySelector(':scope > label, :scope > legend, :scope > .label');
      if (l && !l.contains(el)) { const t = (l.textContent || '').trim(); if (t) return t.slice(0, 120); }
      p = p.parentElement;
    }
    return (el.placeholder || el.name || el.id || '').slice(0, 120);
  }

  function requiredFor(el, label) {
    if (el.required) return true;
    if (el.getAttribute && el.getAttribute('aria-required') === 'true') return true;
    if (/\*/.test(label || '')) return true;
    // Ashby-style: required class on the question container (not the input).
    const c = el.closest ? el.closest('[data-field-path]') : null;
    if (c && c.querySelector('[class*="required" i]')) return true;
    return false;
  }

  // ── forms ───────────────────────────────────────────────────────────────
  const forms = [];
  deepQueryAll('form').forEach(f => {
    if (forms.length >= 100) return;
    const fields = f.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea');
    forms.push({
      selector: selectorFor(f),
      action: (f.getAttribute && f.getAttribute('action')) || null,
      method: (f.getAttribute && f.getAttribute('method')) || null,
      fieldCount: fields.length,
      fieldSelectors: Array.from(fields).slice(0, 40).map(selectorFor),
      visible: isVisible(f),
    });
  });

  // ── inputs / selects / textareas ──────────────────────────────────────────
  const SKIP = new Set(['hidden', 'submit', 'button', 'image', 'reset']);
  const inputs = [];
  const seenSel = new Set();
  deepQueryAll('input, select, textarea, [role="combobox"]').forEach(el => {
    if (inputs.length >= MAX_FIELDS) return;
    const tag = el.tagName.toLowerCase();
    let type = (el.type || (tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : 'text')).toLowerCase();
    // <select>.type is 'select-one' / 'select-multiple' — normalise to 'select'.
    if (tag === 'select') type = 'select';
    if (el.getAttribute && el.getAttribute('role') === 'combobox') type = 'combobox';
    if (SKIP.has(type)) return;
    const sel = selectorFor(el);
    if (!sel || seenSel.has(sel)) return;
    seenSel.add(sel);
    const label = labelFor(el);
    let options = null;
    if (tag === 'select') {
      options = Array.from(el.options || []).map(o => (o.textContent || '').trim()).filter(Boolean).slice(0, 60);
    } else if (el.list) {
      options = Array.from(el.list.options || []).map(o => o.value).filter(Boolean).slice(0, 60);
    }
    let checked = null;
    if (type === 'checkbox' || type === 'radio') checked = !!el.checked;
    inputs.push({
      selector: sel,
      fieldType: type,
      label: label,
      name: el.name || null,
      elementId: el.id || null,
      value: (type === 'checkbox' || type === 'radio') ? '' : (el.value || ''),
      placeholder: el.placeholder || null,
      required: requiredFor(el, label),
      disabled: !!el.disabled,
      readonly: !!el.readOnly,
      checked: checked,
      options: options,
      visible: isVisible(el),
    });
  });

  // ── buttons ──────────────────────────────────────────────────────────────
  const SUBMIT_RE = /\b(submit|apply now|apply for this|send application|finish|complete application)\b/i;
  const buttons = [];
  deepQueryAll('button, input[type=submit], input[type=button], [role="button"]').forEach(el => {
    if (buttons.length >= MAX_BUTTONS) return;
    const tag = el.tagName.toLowerCase();
    let text = '';
    if (tag === 'input') text = el.value || '';
    else text = (el.textContent || el.getAttribute('aria-label') || '').trim();
    text = text.replace(/\s+/g, ' ').slice(0, 100);
    const btype = (el.getAttribute && el.getAttribute('type')) || (tag === 'button' ? 'submit' : null);
    const isSubmit = btype === 'submit' || SUBMIT_RE.test(text);
    buttons.push({
      selector: selectorFor(el),
      text: text,
      buttonType: btype,
      disabled: !!el.disabled || (el.getAttribute && el.getAttribute('aria-disabled') === 'true'),
      visible: isVisible(el),
      isSubmit: !!isSubmit,
    });
  });

  // ── messages: validation / error / success / toast / alert ────────────────
  const messages = [];
  const seenMsg = new Set();
  const SUCCESS_RE = /(thank you for applying|thanks for applying|application (has been )?(received|submitted|complete)|we(’|'| ha)?ve received your application|successfully submitted|your application has been)/i;
  const ERROR_WORD_RE = /(error|required|invalid|failed|must |cannot |please (enter|select|provide|fill)|is not valid|already applied)/i;

  function pushMsg(el, forcedKind) {
    if (messages.length >= MAX_MESSAGES) return;
    if (!isVisible(el)) return;
    const txt = (el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (!txt || txt.length < 2 || txt.length > 300) return;
    const key = txt.slice(0, 120);
    if (seenMsg.has(key)) return;
    seenMsg.add(key);
    // classify
    const cls = ((el.className && el.className.toString) ? el.className.toString() : '') + ' ' + (el.id || '');
    const role = el.getAttribute && el.getAttribute('role');
    let kind = forcedKind || null;
    if (!kind) {
      if (SUCCESS_RE.test(txt) || /success/i.test(cls)) kind = 'success';
      else if (/toast|snackbar|notification|snack/i.test(cls) || role === 'status') kind = 'toast';
      else if (el.getAttribute && el.getAttribute('aria-invalid') === 'true') kind = 'validation';
      else if (/error|invalid|danger|field-error|help-block/i.test(cls) || ERROR_WORD_RE.test(txt)) kind = 'error';
      else if (role === 'alert' || /\balert\b/i.test(cls)) kind = 'alert';
      else kind = 'alert';
    }
    // associate a validation error with the nearest field label
    let assoc = null;
    if (kind === 'error' || kind === 'validation') {
      // Start from the PARENT so a self-classing error node (e.g. a
      // `.field-error` span, which itself matches [class*="field"]) resolves to
      // its enclosing question group rather than matching itself.
      const startNode = el.parentElement || el;
      const grp = startNode.closest('.form-group, .field, .input-group, [class*="field"], [data-field-path]');
      if (grp) {
        const lbl = grp.querySelector('label, legend');
        if (lbl) assoc = (lbl.textContent || '').trim().slice(0, 80);
        const invalidInput = grp.querySelector('[aria-invalid="true"], input, select, textarea');
        if (assoc) kind = 'validation';
        else if (invalidInput) kind = 'validation';
      }
    }
    messages.push({ kind: kind, text: txt.slice(0, 260), selector: selectorFor(el), associatedField: assoc });
  }

  // explicit error/validation surfaces
  const ERR_SELECTORS = [
    '.field-error', '.error-message', '.error', '[role="alert"]', '[aria-invalid="true"]',
    '[class*="invalid"]', '[class*="errorMessage"]', '.help-block.error', '.has-error',
    '.form-error', '.input-error', '.invalid-feedback', '[class*="field-error"]',
  ];
  ERR_SELECTORS.forEach(s => { try { document.querySelectorAll(s).forEach(e => pushMsg(e)); } catch (e) {} });
  // success / toast / status surfaces
  const OK_SELECTORS = [
    '[class*="success"]', '.alert-success', '[role="status"]', '[class*="toast"]',
    '[class*="snackbar"]', '[class*="notification"]', '.confirmation', '[class*="confirmation"]',
  ];
  OK_SELECTORS.forEach(s => { try { document.querySelectorAll(s).forEach(e => pushMsg(e)); } catch (e) {} });
  // page-body success text (bare confirmation with no obvious wrapper class)
  try {
    const h = document.querySelector('h1, h2, .headline, [class*="headline"]');
    if (h && SUCCESS_RE.test(h.textContent || '')) pushMsg(h, 'success');
  } catch (e) {}

  // ── modals / dialogs ──────────────────────────────────────────────────────
  const modals = [];
  const APP_RE = /(first name|last name|email|resume|cover letter|phone|apply)/i;
  const modalNodes = deepQueryAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"], .modal, [class*="modal"], [class*="Dialog"]');
  const seenModal = new Set();
  modalNodes.forEach(m => {
    if (modals.length >= MAX_MODALS) return;
    if (!isVisible(m)) return;
    const sel = selectorFor(m);
    if (seenModal.has(sel)) return;
    seenModal.add(sel);
    const txt = (m.textContent || '').replace(/\s+/g, ' ').trim();
    let closeSel = null;
    const closeBtn = m.querySelector(
      '[aria-label*="close" i], [aria-label*="dismiss" i], button.close, .close-button, ' +
      '[class*="close-btn"], button[title*="close" i]'
    ) || Array.from(m.querySelectorAll('button')).find(b => /^[×✕✖xX]$|^close$|^dismiss$/i.test((b.textContent || '').trim()));
    if (closeBtn) closeSel = selectorFor(closeBtn);
    modals.push({
      selector: sel,
      role: (m.getAttribute && m.getAttribute('role')) || null,
      textPreview: txt.slice(0, 200),
      hasCloseButton: !!closeBtn,
      closeSelector: closeSel,
      looksLikeApplication: APP_RE.test(txt) && txt.length > 200,
    });
  });

  // ── captcha (a VISIBLE widget only — never a mere string in markup) ─────────
  // Detecting captcha from a rendered, visible widget avoids the false positives
  // that plague substring matching (bot-walled sites load captcha scripts on
  // every page; "captcha"/"2fa" appear in tokens/markup site-wide).
  let captcha = { present: false, kind: '' };
  // A widget counts only when it is a REAL, interactable challenge — not the
  // invisible reCAPTCHA v3 badge / hidden g-recaptcha-response textarea that
  // Greenhouse/Lever put on every form (that is not a wall, it resolves at
  // submit). We therefore require a sized, visible widget and exclude the badge.
  function bigVisible(el, minW) {
    if (!el || !isVisible(el)) return false;
    try { const r = el.getBoundingClientRect(); return (r.width >= minW && r.height >= 20); }
    catch (e) { return true; }
  }
  const CAPTCHA_CHECKS = [
    ['turnstile', "iframe[src*='challenges.cloudflare.com'], .cf-turnstile", 60],
    ['hcaptcha',  "iframe[src*='hcaptcha.com'], iframe[src*='hcaptcha.'], .h-captcha", 60],
    // recaptcha: the v2 checkbox anchor or the image-grid challenge (bframe) —
    // NOT the floating .grecaptcha-badge (invisible reCAPTCHA).
    ['recaptcha', "iframe[src*='recaptcha/api2/anchor'], iframe[src*='recaptcha/api2/bframe'], "
                + ".g-recaptcha:not(.grecaptcha-badge)", 100],
  ];
  for (const [kind, sel, minW] of CAPTCHA_CHECKS) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) {}
    if (nodes.some(n => bigVisible(n, minW))) { captcha = { present: true, kind: kind }; break; }
  }

  // ── navigation ─────────────────────────────────────────────────────────────
  const nav = {
    url: location.href,
    title: document.title || '',
    readyState: document.readyState || '',
    isLoading: document.readyState !== 'complete',
    frameCount: (window.frames ? window.frames.length : 0),
    referrer: document.referrer || '',
    visibilityState: document.visibilityState || '',
  };

  // ── raw surfaces ───────────────────────────────────────────────────────────
  let html = '';
  try { html = document.documentElement.outerHTML || ''; } catch (e) {}
  if (html.length > HTML_MAX) html = html.slice(0, HTML_MAX);
  let visibleText = '';
  try { visibleText = (document.body && document.body.innerText) || ''; } catch (e) {}
  visibleText = visibleText.replace(/\n{3,}/g, '\n\n');
  if (visibleText.length > TEXT_MAX) visibleText = visibleText.slice(0, TEXT_MAX);

  return { nav, html, visibleText, forms, inputs, buttons, messages, modals, captcha };
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# DOM quiescence probe.  Signature: (opts) => Promise<{stable, reason, mutations}>
# opts = {quietMs, timeoutMs}
# Resolves when no mutations for quietMs AND readyState==complete, or timeoutMs.
# ─────────────────────────────────────────────────────────────────────────────
WAIT_STABLE_JS = r"""
(opts) => new Promise((resolve) => {
  opts = opts || {};
  const QUIET = opts.quietMs || 500;
  const TIMEOUT = opts.timeoutMs || 6000;
  let mutations = 0;
  let settleTimer = null;
  let observer = null;
  const start = Date.now();

  const done = (stable, reason) => {
    try { if (observer) observer.disconnect(); } catch (e) {}
    if (settleTimer) clearTimeout(settleTimer);
    resolve({ stable: stable, reason: reason, mutations: mutations, elapsedMs: Date.now() - start });
  };

  const arm = () => {
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      // Quiet window elapsed. Require the document to be fully loaded too.
      if (document.readyState === 'complete') done(true, 'quiescent');
      else arm();  // keep waiting for load to finish
    }, QUIET);
  };

  const hardStop = setTimeout(() => done(false, 'timeout'), TIMEOUT);

  try {
    observer = new MutationObserver((recs) => { mutations += recs.length; arm(); });
    const target = document.body || document.documentElement;
    observer.observe(target, { childList: true, subtree: true, attributes: true, characterData: true });
  } catch (e) {
    clearTimeout(hardStop);
    return done(true, 'observer_unavailable');
  }

  arm();
  // Clear the hard-stop when we resolve normally (settleTimer path calls done()
  // which disconnects; hardStop is harmless if it fires after — resolve is idempotent).
})
"""
