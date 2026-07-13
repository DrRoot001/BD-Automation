// BD Auto-Apply — INDEED PORTAL engine (content script, guarded IIFE).
//
// ⚑ PORTAL SPLIT: this file is the INDEED specialist and only acts on
//   indeed.com frames (job pages, SmartApply, and the Indeed-Apply widget
//   iframe embedded on company pages). It is the proven, working Indeed flow —
//   DO NOT add company/ATS-site logic here. All non-Indeed portals are handled
//   by content/generic.js, which is injected on company sites instead. This
//   isolation means company-site changes can never break Indeed.
//
// Engine: a single tick() classifies the page (job-detail vs SmartApply step
// vs blocker vs success), then handles it. Every SmartApply step is handled
// generically: scrape labelled fields → ask background for answers → fill →
// click the primary Continue button. The resume step and review/submit are
// special-cased. CAPTCHAs / login walls flag the human BD agent and the
// script keeps polling so it resumes by itself once the human clears the way.

(() => {
  if (window.__bdIndeedApplyInjected) return;
  window.__bdIndeedApplyInjected = true;

  const TICK_MS = 2500;
  const MAX_ACTIONS = 45;

  // Indeed's "Apply with Indeed" widget renders the application form in an
  // iframe. With all_frames:true this content script also runs INSIDE that
  // iframe — the in-iframe instance drives the form, while the top document
  // defers to it. isIframe tells the two instances apart.
  const isIframe = window.top !== window.self;
  // A visible embedded application iframe (Indeed Apply OR an ATS like
  // Greenhouse/Lever/Workday/Ashby/iCIMS embedded on a company careers page).
  // When present, the in-iframe engine drives the form and the top defers.
  const ATS_IFRAME_RE = /indeed\.com|indeedapply|smartapply|greenhouse\.io|lever\.co|myworkdayjobs|workday|ashbyhq|icims|smartrecruiters|jobvite|breezy|workable|bamboohr|\/apply/i;
  function indeedApplyIframePresent() {
    return Array.from(document.querySelectorAll('iframe')).some(f => {
      if (!f.src) return false;
      const r = f.getBoundingClientRect();
      if (r.width < 120 && r.height < 120) return false; // tracking pixels/badges
      return ATS_IFRAME_RE.test(f.src);
    });
  }

  const S = {
    active: false,
    ctx: null,            // {jobIndex, job, candidateName, autoSubmit}
    busy: false,
    done: false,
    actions: 0,
    metaSent: false,
    processedSigs: new Set(),   // step signatures already filled+continued
    humanSigs: new Set(),       // steps handed to the human — never auto-click
    watchSigs: new Set(),       // steps we watch to learn the human's answers
    reviewedSigs: new Set(),    // steps that have had the AI review pass
    learnPending: {},           // sig -> [{label,type,options}] still unlearned
    refilledSigs: new Set(),
    attentionSent: new Set(),
    applyClickedAt: 0,
    viewjobTicks: 0,
    resumePolls: 0,
    resumeUploaded: false,
    aiSeq: 0,
    aiNavCount: 0,
    lastAiClickId: null,
    aiClickRepeat: 0,
    fieldSeq: 0,
  };

  // ── messaging ────────────────────────────────────────────────────────────

  function send(msg) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (res) => {
          if (chrome.runtime.lastError) resolve({ error: chrome.runtime.lastError.message });
          else resolve(res || {});
        });
      } catch (e) { resolve({ error: e.message }); }
    });
  }

  const report = (state, detail) =>
    send({ type: 'PAGE_STATE', jobIndex: S.ctx?.jobIndex, state, detail });
  const logBg = (msg, level) =>
    send({ type: 'LOG', msg: `content: ${msg}`, level });

  async function finishJob(status, note) {
    if (S.done) return;
    S.done = true;
    await send({ type: 'JOB_RESULT', jobIndex: S.ctx?.jobIndex, status, note });
  }

  async function needsHuman(reason, key) {
    const k = key || reason;
    if (S.attentionSent.has(k)) return;
    S.attentionSent.add(k);
    await send({ type: 'NEEDS_HUMAN', jobIndex: S.ctx?.jobIndex, reason });
  }

  // ── page classification (markers mirrored from module4's IndeedAdapter) ──

  const BLOCKED_URL_MARKERS = ['/account/login', 'secure.indeed.com', '/secure/', 'challenge', 'captcha'];
  const BLOCKED_TEXT_MARKERS = [
    'verify you are human', "verify that you're a human", 'additional verification required',
    'unusual activity from your network', 'checking your browser',
  ];
  const UNAVAILABLE_MARKERS = [
    'this job is no longer available', 'job is no longer accepting applications',
    'this position has been filled', 'job expired', 'this job has expired',
    'the job you were trying to view',
  ];
  // Broken/dead destination pages — e.g. Indeed pagead/clk ad redirects that
  // 404, or a Spring "Whitelabel Error Page". Skip these automatically.
  const DEAD_PAGE_RE = /whitelabel error page|no explicit mapping for|unexpected error \(type=not found|http status 404|error 404|\b404\b.*(not found|error)|page (not found|isn'?t available|could not be found|doesn'?t exist)|this site can'?t be reached|server error|502 bad gateway|503 service/i;
  const SUCCESS_MARKERS = [
    'your application has been submitted', 'application has been submitted',
    "we've sent your application", 'we have sent your application', 'thank you for applying',
    'application submitted', "we've received your application", 'we have received your application',
    'your application has been received', 'application was submitted successfully',
    'thanks for applying', 'successfully applied',
  ];

  const lowerBody = () => (document.body?.innerText || '').toLowerCase();
  const isSmartApply = () => /(^|\.)smartapply\.indeed\.com$/i.test(location.hostname);
  const isIndeedHost = () => /(^|\.)indeed\.com$/i.test(location.hostname);

  // Only an ACTIVE, visible captcha challenge counts as blocked. Invisible
  // reCAPTCHA (the v3 badge that Lever/Greenhouse embed on every form) must
  // NOT flag the page — it runs silently on submit and needs no interaction.
  function captchaPresent() {
    const big = (el, minH = 130, minW = 200) => {
      const r = el.getBoundingClientRect();
      return visible(el) && r.height >= minH && r.width >= minW;
    };
    // reCAPTCHA image-grid challenge popup (the bframe) — large & visible.
    const rcChallenge = Array.from(document.querySelectorAll(
      'iframe[src*="recaptcha"][title*="challenge" i], iframe[title*="recaptcha challenge" i], iframe[src*="/recaptcha/api2/bframe"], iframe[src*="/recaptcha/enterprise/bframe"]'))
      .some(f => big(f));
    if (rcChallenge) return true;
    // reCAPTCHA v2 "I'm not a robot" checkbox (anchor) — real interaction.
    const rcCheckbox = Array.from(document.querySelectorAll('iframe[src*="/recaptcha/api2/anchor"], iframe[src*="/recaptcha/enterprise/anchor"]'))
      .some(f => big(f, 60, 250));
    if (rcCheckbox) return true;
    // hCaptcha / Cloudflare Turnstile visible widget.
    const other = Array.from(document.querySelectorAll(
      'iframe[src*="hcaptcha.com"], .h-captcha iframe, iframe[src*="challenges.cloudflare.com"], .cf-turnstile iframe'))
      .some(f => big(f, 60, 250));
    return other;
  }

  function loginWallPresent() {
    const pw = Array.from(document.querySelectorAll('input[type="password"]')).filter(visible);
    return pw.length > 0;
  }

  // Email-verification (OTP) screen detection — mirrors module4's detector:
  // a "we sent you a code" cue plus either N single-char boxes or one code input.
  const OTP_CUE_RE = /(verification code|security code|enter the code|one[-\s]?time (code|passcode|password)|we (sent|emailed) (you )?a code|check your email for|code (we|that) (sent|emailed)|sent (you )?a code|confirm your email)/i;

  function detectOtpShape() {
    if (!OTP_CUE_RE.test(document.body?.innerText || '')) return null;
    const inputs = Array.from(document.querySelectorAll(
      'input[type="text"], input[type="tel"], input[type="number"], input[inputmode="numeric"], input:not([type])'))
      .filter(visible);
    if (!inputs.length) return null;
    // Split boxes: 4–10 single-char inputs
    const ones = inputs.filter(e => parseInt(e.getAttribute('maxlength') || '0', 10) === 1);
    if (ones.length >= 4 && ones.length <= 10) return { kind: 'split', els: ones };
    // Single input whose label/name smells like a code field
    const single = inputs.find(e => {
      const hay = `${labelForInput(e)} ${e.id || ''} ${e.name || ''} ${e.placeholder || ''}`.toLowerCase();
      return /(verif|confirm|otp|one[-\s]?time|security|access[-\s]?code|\bcode\b|pin)/.test(hay);
    }) || (inputs.length === 1 ? inputs[0] : null);
    return single ? { kind: 'single', els: [single] } : null;
  }

  function headingText() {
    for (const sel of ['h1', '[data-testid="page-title"]', 'main h2', 'h2']) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null && el.innerText.trim()) return el.innerText.trim().toLowerCase();
    }
    return '';
  }

  function classify() {
    const url = location.href.toLowerCase();
    const title = (document.title || '').toLowerCase();
    if (isIndeedHost()) {
      for (const m of BLOCKED_URL_MARKERS) if (url.includes(m)) return { kind: 'BLOCKED', detail: `url marker "${m}"` };
    }
    if (/(^|\s)(blocked|access denied|just a moment)/.test(title) || title.includes('captcha')) {
      return { kind: 'BLOCKED', detail: `title "${document.title}"` };
    }
    const body = lowerBody();
    for (const m of BLOCKED_TEXT_MARKERS) if (body.includes(m)) return { kind: 'BLOCKED', detail: m };
    for (const m of SUCCESS_MARKERS) if (body.includes(m)) return { kind: 'SUCCESS', detail: m };
    for (const m of UNAVAILABLE_MARKERS) if (body.includes(m)) return { kind: 'UNAVAILABLE', detail: m };
    // Broken/error destination (short page + error markers) → skip the job.
    if (DEAD_PAGE_RE.test(body) && body.length < 4000) {
      return { kind: 'UNAVAILABLE', detail: 'broken/error page (404 or app error)' };
    }

    if (isSmartApply()) {
      const h = headingText();
      if (h.includes('preparing review') || body.includes('preparing review')) return { kind: 'WAIT', detail: 'preparing review' };
      if (h.includes('review your application')) return { kind: 'REVIEW', detail: h };
      if (h.includes('cover letter')) return { kind: 'FORM', detail: h };
      // Any heading mentioning resume/CV is the resume step — Indeed has many
      // variants ("Add a resume", "Choose how to share your resume", ...).
      if (h.includes('resume') || /\bcv\b/.test(h)) return { kind: 'RESUME', detail: h };
      return { kind: 'FORM', detail: h || 'smartapply step' };
    }

    // Indeed Apply widget iframe embedded on a company page: this instance is
    // running INSIDE that iframe (indeed.com host). Treat its form as the step.
    if (isIframe && isIndeedHost()) {
      const h = headingText();
      if (h.includes('review your application')) return { kind: 'REVIEW', detail: h };
      if (h.includes('resume') || /\bcv\b/.test(h)) return { kind: 'RESUME', detail: h };
      const ctrls = document.querySelectorAll('input:not([type=hidden]):not([type=submit]), textarea, select, input[type="file"]');
      if (ctrls.length >= 1) return { kind: 'FORM', detail: h || 'indeed apply form' };
      return { kind: 'WAIT', detail: 'indeed apply iframe loading' };
    }

    if (isIndeedHost()) {
      // indeed job-detail / search page
      if (/[?&]jk=|\/viewjob|\/job\/|\/cmp\/.+\/jobs\//.test(url) || document.querySelector('#jobDescriptionText')) {
        return { kind: 'VIEWJOB', detail: '' };
      }
      return { kind: 'UNKNOWN', detail: url.slice(0, 100) };
    }

    // ── external company/ATS site ────────────────────────────────────────
    if (captchaPresent()) return { kind: 'BLOCKED', detail: 'captcha widget on page' };
    if (detectOtpShape()) return { kind: 'OTP', detail: 'email verification code screen' };
    if (loginWallPresent()) {
      // With a candidate site-password provided in the popup, login/signup
      // walls are just another form: fill email + password and continue.
      if (S.ctx?.hasSitePassword) return { kind: 'FORM', detail: 'login / account form' };
      return { kind: 'BLOCKED', detail: 'login form (password field) on page — no candidate site password set' };
    }
    const h = headingText();
    // Count real application fields whether or not they sit inside a <form>
    // tag (Lever/Greenhouse often don't wrap them). Excludes search/checkbox/
    // radio and a lone newsletter email.
    const meaningful = Array.from(document.querySelectorAll(
      'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]):not([type=checkbox]):not([type=radio]):not([type=image]):not([type=reset]), textarea, select'))
      .filter(visible);
    const hasFile = !!document.querySelector('input[type="file"]');
    const topHasForm = hasFile || meaningful.length >= 3;

    // The form lives on THIS page → fill it (don't defer, don't hunt Apply).
    if (topHasForm) return { kind: 'FORM', detail: h || 'external application form' };

    // No form here. If a real apply form is embedded in an iframe, the top
    // document defers to the in-iframe engine. (Only when THIS page has no form
    // of its own — otherwise the Lever direct form would be wrongly skipped.)
    if (!isIframe && indeedApplyIframePresent()) {
      return { kind: 'WAIT', detail: 'apply form open in iframe — deferring to it' };
    }
    // An iframe with no form is a tracking/ad frame (allFrames injects into all
    // of them) — it must NOT hunt for apply buttons.
    if (isIframe) return { kind: 'WAIT', detail: 'non-form iframe' };
    // Otherwise: a job-listing page — look for an Apply button.
    return { kind: 'EXTERNAL_LISTING', detail: h || title.slice(0, 80) };
  }

  // ── DOM utilities ────────────────────────────────────────────────────────

  const visible = (el) => {
    if (!el || el.disabled) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };

  function setNativeValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    el.focus();
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.blur();
  }

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const normText = (s) => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();

  // ── Human pace (Indeed) ────────────────────────────────────────────────────
  // Deliberately slow, field-by-field cadence so the flow reads as unhurried.
  // ~1s between fields; text is entered one character at a time.
  const FIELD_PACE_MS = 1000;  // pause after each field is filled
  const TYPE_CHAR_MS = 90;     // delay per character when typing text

  // Type text one character at a time (focus → per-char keydown/input/keyup),
  // instead of setting the whole value instantly. Used for text/textarea/combo
  // fields so entry is unhurried and matches how the field is normally filled.
  async function humanType(el, text) {
    el.focus();
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    const set = (v) => { if (setter) setter.call(el, v); else el.value = v; };
    set('');
    el.dispatchEvent(new Event('input', { bubbles: true }));
    for (const ch of String(text)) {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
      set((el.value || '') + ch);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
      await sleep(TYPE_CHAR_MS);
    }
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // Full pointer-event chain for React/framework buttons that ignore .click().
  function dispatchRealClick(el) {
    el.scrollIntoView({ block: 'center' });
    const r = el.getBoundingClientRect();
    const opts = { bubbles: true, cancelable: true, view: window, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
    for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      el.dispatchEvent(type.startsWith('pointer') ? new PointerEvent(type, opts) : new MouseEvent(type, opts));
    }
  }

  // Text that is a dropdown placeholder / default option, NOT a real question.
  // Used to reject useless labels like "Please Select" and dig for the real one.
  const PLACEHOLDER_LABEL_RE = /^(please\s+select|select(\s+(an?\s+)?(option|one))?|choose(\s+(an?\s+)?(option|one))?|--+|—+|pick(\s+one)?|n\/?a)\.{0,3}$/i;
  const isPlaceholderLabel = (s) => !s || PLACEHOLDER_LABEL_RE.test(String(s).trim());

  function labelForInput(el) {
    const good = (s) => (s && s.trim() && !isPlaceholderLabel(s.trim())) ? s.trim() : '';
    // aria-label
    let r = good(el.getAttribute('aria-label'));
    if (r) return r;
    // aria-labelledby
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      r = good(labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' '));
      if (r) return r;
    }
    // <label for=...>
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      r = good(lab?.innerText);
      if (r) return r;
    }
    // wrapping label
    r = good(el.closest('label')?.innerText);
    if (r) return r;
    // fieldset legend
    r = good(el.closest('fieldset')?.querySelector('legend')?.innerText);
    if (r) return r;
    // nearest preceding heading/label-ish block within the field container
    let node = el.closest('div');
    for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
      const labs = Array.from(node.querySelectorAll('label, legend, [class*="label" i], [class*="question" i], h1, h2, h3, h4, p, span, div'));
      for (const lab of labs) {
        if (lab.contains(el)) continue;
        const t = (lab.innerText || '').trim();
        if (t && t.length <= 200 && !isPlaceholderLabel(t)) return t;
      }
    }
    // Previous-sibling text (common: <div>Question</div><select>…</select>)
    let sib = el.previousElementSibling;
    for (let i = 0; sib && i < 3; i++, sib = sib.previousElementSibling) {
      const t = (sib.innerText || '').trim();
      if (t && t.length <= 200 && !isPlaceholderLabel(t)) return t;
    }
    return good(el.getAttribute('placeholder')) || el.name || '';
  }

  function groupLabel(input) {
    const fs = input.closest('fieldset');
    const legend = fs?.querySelector('legend');
    if (legend?.innerText.trim()) return legend.innerText.trim();
    const grp = input.closest('[role="group"],[role="radiogroup"]');
    if (grp) {
      const lb = grp.getAttribute('aria-label')
        || (grp.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ');
      if (lb.trim()) return lb.trim();
    }
    // fall back: nearest preceding text block
    let node = (fs || input.closest('div'))?.parentElement;
    for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
      const cand = Array.from(node.children).find(c =>
        !c.contains(input) && c.innerText && c.innerText.trim().length > 5 && c.innerText.trim().length < 400);
      if (cand) return cand.innerText.trim();
    }
    return '';
  }

  function optionLabel(input) {
    if (input.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
      if (lab?.innerText.trim()) return lab.innerText.trim();
    }
    const wrap = input.closest('label');
    if (wrap?.innerText.trim()) return wrap.innerText.trim();
    return input.value || '';
  }

  const cleanLabel = (s) => String(s || '').replace(/\s+/g, ' ').replace(/\s*\*\s*$/, '').trim();
  const isRequired = (el, label) =>
    el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(String(label || ''));

  // ── field scraping ───────────────────────────────────────────────────────

  const CONSENT_RE = /(agree|consent|certify|acknowledge|i understand|terms)/i;
  const OPTOUT_RE = /(save my answers|job alert|subscribe|send me|marketing|updates from indeed|newsletter)/i;

  // In-memory element registry (isolated world). We DON'T write data-bd-* marker
  // attributes onto the page DOM — those are the one thing a site could
  // MutationObserver to fingerprint this extension. IDs and group membership are
  // kept here instead, rebuilt fresh on each scrape pass (field IDs are only
  // ever resolved within the same tick that produced them).
  let _fr = null; // current-pass field registry
  function newFieldReg() {
    _fr = { byId: new Map(), els: new Set(), keyOfEl: new Map(), elsOfKey: new Map() };
  }
  function regField(el, groupKey) {
    const id = String(++S.fieldSeq);
    _fr.byId.set(id, el);
    _fr.els.add(el);
    if (groupKey != null) {
      _fr.keyOfEl.set(el, groupKey);
      let arr = _fr.elsOfKey.get(groupKey);
      if (!arr) { arr = []; _fr.elsOfKey.set(groupKey, arr); }
      arr.push(el);
    }
    return id;
  }
  const fieldRegistered = (el) => !!(_fr && _fr.els.has(el));
  const findByFieldId = (id) => (_fr ? (_fr.byId.get(id) || null) : null);
  const groupKeyOf = (el) => (_fr ? _fr.keyOfEl.get(el) : undefined);
  const groupMembers = (key) => ((_fr && _fr.elsOfKey.get(key)) || []).filter(visible);

  function scrapeFields() {
    newFieldReg();
    const scope = document.querySelector('main form') || document.querySelector('form')
      || document.querySelector('main') || document.body;
    const fields = [];
    const seenGroups = new Set();

    const els = Array.from(scope.querySelectorAll('input, textarea, select'))
      .filter(el => visible(el) && el.type !== 'hidden' && el.type !== 'file' && el.type !== 'submit' && el.type !== 'button');

    for (const el of els) {
      const type = (el.tagName === 'TEXTAREA') ? 'textarea'
        : (el.tagName === 'SELECT') ? 'select'
        : (el.type || 'text').toLowerCase();

      if (type === 'radio' || type === 'checkbox') {
        const groupKey = el.name || el.closest('fieldset')?.outerHTML.slice(0, 60) || labelForInput(el);
        const groupEls = el.name
          ? Array.from(scope.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)).filter(visible)
          : [el];

        if (type === 'checkbox' && groupEls.length === 1) {
          // Single checkbox: consent → tick, marketing → untick, else ask backend.
          const lab = cleanLabel(optionLabel(el) || labelForInput(el));
          if (OPTOUT_RE.test(lab)) { if (el.checked) el.click(); continue; }
          if (CONSENT_RE.test(lab)) { if (!el.checked) el.click(); continue; }
          if (seenGroups.has(groupKey)) continue;
          seenGroups.add(groupKey);
          fields.push({
            id: regField(el), type: 'radio',
            label: cleanLabel(lab), options: ['Yes', 'No'],
            required: isRequired(el, lab), currentValue: el.checked ? 'Yes' : '',
          });
          continue;
        }

        if (seenGroups.has(groupKey)) continue;
        seenGroups.add(groupKey);
        const options = groupEls.map(optionLabel).map(cleanLabel).filter(Boolean);
        const gl = cleanLabel(groupLabel(el));
        const checked = groupEls.find(g => g.checked);
        let firstId = null;
        groupEls.forEach((g, i) => { const gid = regField(g, groupKey); if (i === 0) firstId = gid; });
        fields.push({
          id: firstId, groupName: el.name || null, type: 'radio',
          label: gl || options.join(' / '), options,
          required: isRequired(el, gl), currentValue: checked ? cleanLabel(optionLabel(checked)) : '',
        });
        continue;
      }

      if (type === 'select') {
        const options = Array.from(el.options)
          .filter(o => o.value !== '' && !/^\s*(select|choose|--)/i.test(o.text))
          .map(o => o.text.trim());
        const lab = cleanLabel(labelForInput(el));
        const sel = el.selectedOptions[0];
        fields.push({
          id: regField(el), type: 'select', label: lab, options,
          required: isRequired(el, lab),
          currentValue: sel && sel.value !== '' ? sel.text.trim() : '',
          multiple: el.multiple,
        });
        continue;
      }

      // text-like
      const lab = cleanLabel(labelForInput(el));
      if (!lab && !el.placeholder) continue;
      if (type === 'search') continue;
      // For phone fields, detect an adjacent country-code selector so the
      // answer pipeline can strip a leading +CC from the number.
      let hasCountryCode = false;
      if (type === 'tel') {
        let node = el;
        for (let d = 0; node && d < 4; d++, node = node.parentElement) {
          const cc = node.querySelector('select, [role="combobox"], button[aria-haspopup]');
          if (cc && cc !== el) {
            const t = `${cc.innerText || ''} ${cc.getAttribute('aria-label') || ''}`;
            if (cc.tagName === 'SELECT' || /\+\d|country|dial|code/i.test(t)) { hasCountryCode = true; break; }
          }
        }
      }
      fields.push({
        id: regField(el),
        type: type === 'tel' ? 'phone' : (type === 'number' ? 'number' : (el.tagName === 'TEXTAREA' ? 'textarea' : type)),
        label: lab, options: null,
        required: isRequired(el, lab),
        currentValue: el.value || '',
        hasCountryCode,
        combobox: el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list',
      });
    }

    // Custom (non-native) dropdowns: a button that opens a listbox. Options
    // are unknown until opened — fillField opens it and picks the best match.
    const ddBtns = Array.from(scope.querySelectorAll(
      'button[aria-haspopup="listbox"], button[role="combobox"], div[role="combobox"][tabindex]'))
      .filter(el => visible(el) && !fieldRegistered(el));
    for (const btn of ddBtns) {
      const lab = cleanLabel(labelForInput(btn));
      if (!lab) continue;
      const txt = (btn.innerText || '').trim();
      const placeholderish = !txt || /^(select|choose|--|pick|please)/i.test(txt) || normText(txt) === normText(lab);
      fields.push({
        id: regField(btn), type: 'listbox', label: lab, options: null,
        required: isRequired(btn, lab),
        currentValue: placeholderish ? '' : txt,
      });
    }
    return fields;
  }

  // ── filling ──────────────────────────────────────────────────────────────

  async function fillField(field, answer) {
    const el = findByFieldId(field.id);
    if (!el) return false;

    if (field.type === 'radio') {
      const gk = groupKeyOf(el);
      const groupEls = gk != null ? groupMembers(gk) : [el];
      // Lone checkbox surfaced as a Yes/No question: interpret the answer as
      // checked/unchecked instead of matching option labels.
      if (groupEls.length === 1 && groupEls[0].type === 'checkbox') {
        const box = groupEls[0];
        const want = /^y/i.test(String(answer).trim());
        if (box.checked !== want) box.click();
        return box.checked === want;
      }
      const target = groupEls.find(g => normText(optionLabel(g)) === normText(answer))
        || groupEls.find(g => normText(optionLabel(g)).includes(normText(answer)))
        || groupEls.find(g => normText(answer).includes(normText(optionLabel(g))));
      if (!target) return false;
      if (!target.checked) {
        target.click();
        // React sometimes needs the label click instead
        if (!target.checked) {
          const lab = target.closest('label') || (target.id && document.querySelector(`label[for="${CSS.escape(target.id)}"]`));
          lab?.click();
        }
      }
      return target.checked;
    }

    if (field.type === 'select') {
      const opt = Array.from(el.options).find(o => normText(o.text) === normText(answer))
        || Array.from(el.options).find(o => normText(o.text).includes(normText(answer)))
        || Array.from(el.options).find(o => normText(answer).includes(normText(o.text)));
      if (!opt) return false;
      setNativeValue(el, opt.value);
      return true;
    }

    // custom dropdown (button + listbox): open, read options, pick best match
    if (field.type === 'listbox') {
      el.scrollIntoView({ block: 'center' });
      el.click();
      await sleep(600);
      const list = Array.from(document.querySelectorAll('[role="listbox"]')).find(visible);
      const opts = list ? Array.from(list.querySelectorAll('[role="option"], li')).filter(visible) : [];
      if (opts.length) {
        const want = normText(answer);
        const match = opts.find(o => normText(o.innerText) === want)
          || opts.find(o => normText(o.innerText).includes(want))
          || opts.find(o => want.includes(normText(o.innerText)) && normText(o.innerText).length > 2)
          // race policy: "South Asian"/"Asian" answer against verbose EEO labels
          || (/\basian\b/.test(want) ? opts.find(o => /\basian\b/.test(normText(o.innerText)) && !/\bcaucasian\b/.test(normText(o.innerText))) : null);
        if (match) { match.click(); await sleep(300); return true; }
      }
      // close the dropdown so it doesn't block the Continue button
      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      el.click();
      return false;
    }

    // combobox / autocomplete: type then pick a suggestion
    if (field.combobox) {
      await humanType(el, answer);
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
      await sleep(900);
      const list = document.querySelector('[role="listbox"]');
      if (list) {
        const opts = Array.from(list.querySelectorAll('[role="option"], li')).filter(visible);
        const match = opts.find(o => normText(o.innerText).includes(normText(answer).split(',')[0])) || opts[0];
        if (match) { match.click(); await sleep(300); return true; }
      }
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      return true;
    }

    // A <textarea> or LONG value (cover-letter / description) → set instantly;
    // character-by-character would take ~1 min for a long answer.
    if (el.tagName === 'TEXTAREA' || String(answer).length > 60) {
      setNativeValue(el, answer);
      return (el.value || '').length > 0;
    }
    // Short text input: type it out character-by-character (human pace).
    await humanType(el, answer);
    return el.value === answer || el.value.length > 0;
  }

  function findButton(patterns) {
    // Include anchors — company/ATS "Apply" controls are often <a> links.
    const btns = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]')).filter(visible);
    for (const re of patterns) {
      const hit = btns.find(b => re.test(normText(b.innerText || b.value)));
      if (hit) return hit;
    }
    return null;
  }

  const CONTINUE_PATTERNS = [
    /^continue applying$/, /^continue$/, /^next$/, /^save and continue$/,
    /^review your application$/, /^continue to review$/,
    // external ATS forms are usually single-page with one submit button
    /^submit application$/, /^submit your application$/, /^send application$/,
    /^easy apply$/, /^apply now$/, /^apply$/, /^apply for this (job|position|role)$/, /^submit$/,
    /^i'?m interested$/, /^start application$/, /^complete application$/, /^finish$/,
    /^send$/, /^submit (my )?(info|information|resume|cv)$/,
    // consent / privacy / terms gates (Taleo "I Confirm", cookie/terms accept)
    /^i confirm$/, /^confirm$/, /^i agree$/, /^agree$/, /^i accept$/, /^accept( all| cookies| and continue)?$/,
    /^agree and continue$/, /^i understand$/, /^acknowledge$/, /^proceed$/,
    // account walls on ATS portals
    /^sign in$/, /^sign up$/, /^log ?in$/, /^create account$/, /^register$/, /^get started$/,
  ];
  const SUBMIT_PATTERNS = [/^submit your application$/, /^submit application$/, /^submit$/];

  // Social / SSO login — automation can't drive OAuth popups. Never click these.
  const SOCIAL_LOGIN_RE = /(continue|sign ?in|log ?in|sign ?up|use)\s+with\s+(google|apple|facebook|linkedin|microsoft|github|okta)|single sign|\bsso\b/i;
  // Email/password login path — click this to reveal the password field.
  const EMAIL_LOGIN_RE = /(continue|sign ?in|log ?in|sign ?up|use)\s+with\s+email|email and password|use your email/i;
  // Skip the account entirely and apply as guest. Must NOT match the
  // ubiquitous "Skip to main content" accessibility link → require login/apply
  // context around "skip".
  const SKIP_LOGIN_RE = /no thanks|continue without|without (an )?account|apply without|apply as guest|continue as guest|maybe later|skip (sign[- ]?in|sign[- ]?up|login|account|and (apply|continue)|this step)|i want to apply/i;
  // Create-account path — for fresh applications, prefer this over "Sign in"
  // (our candidates usually don't have an existing account on the portal).
  const CREATE_ACCOUNT_RE = /create (an? )?account|create( your)? profile|new user|register|sign ?up/i;
  const emailInputPresent = () => !!document.querySelector(
    'input[type="email"], input[name*="email" i], input[id*="email" i], input[placeholder*="email" i]');
  // Indeed's apply widget on a company page opens a separate window we can't
  // control — prefer the direct/manual apply path instead.
  const INDEED_APPLY_RE = /apply with indeed|indeed apply/i;
  // "Apply directly on the company site" style options to prefer.
  const MANUAL_APPLY_RE = /apply manually|apply without indeed|apply directly|without indeed|apply on (the )?(company|employer)|continue without indeed/i;

  function hasFillableForm() {
    return document.querySelectorAll(
      'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]), textarea, select, input[type="file"]').length >= 2
      || !!document.querySelector('input[type="file"]');
  }

  function clickContinue() {
    const btn = findButton(CONTINUE_PATTERNS)
      || document.querySelector('main button[type="submit"], form button[type="submit"]');
    if (btn && visible(btn)) {
      btn.scrollIntoView({ block: 'center' });
      btn.click();
      return true;
    }
    return false;
  }

  // ── modal handling ───────────────────────────────────────────────────────

  function handleDialogs() {
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]')).filter(visible);
    for (const d of dialogs) {
      const txt = normText(d.innerText);
      const buttons = Array.from(d.querySelectorAll('button')).filter(visible);
      const clickBtn = (re) => { const b = buttons.find(x => re.test(normText(x.innerText))); if (b) { b.click(); return true; } return false; };
      if (txt.includes('update address')) {
        if (clickBtn(/no thanks|keep|don't update|dont update/) || clickBtn(/close|cancel/)) return true;
      }
      if (txt.includes('exit application') || txt.includes('leave application')) {
        if (clickBtn(/stay|keep applying|continue applying|cancel/)) return true;
      }
    }
    // ATS entry-choice dialog (Workday & co): "Autofill with Resume" /
    // "Apply Manually" / "Use My Last Application". Autofill is best — the
    // file chooser it opens is fed by our resume uploader, and the ATS
    // pre-fills fields from the PDF. Never "use my last application" (could
    // be another candidate's data).
    if (!isIndeedHost()) {
      const now = Date.now();
      if (!S.entryChoiceAt || now - S.entryChoiceAt > 15000) {
        const all = Array.from(document.querySelectorAll('button, a, [role="button"], [role="radio"], label')).filter(visible);
        const choice = all.find(b => /autofill with (my )?resume|apply with (my )?resume/i.test(b.innerText || ''))
          || all.find(b => /^apply manually$/i.test(normText(b.innerText)));
        if (choice) {
          S.entryChoiceAt = now;
          report('ATS entry dialog — choosing', normText(choice.innerText));
          dispatchRealClick(choice);
          return true;
        }
      }
    }
    // cookie banner
    const cookie = document.querySelector('#onetrust-accept-btn-handler');
    if (cookie && visible(cookie)) { cookie.click(); return true; }
    return false;
  }

  // ── validation errors ────────────────────────────────────────────────────

  function validationErrors() {
    const invalid = Array.from(document.querySelectorAll('[aria-invalid="true"]')).filter(visible);
    if (invalid.length) return invalid.length;
    const errTexts = Array.from(document.querySelectorAll('[class*="error" i], [id*="error" i], [role="alert"]'))
      .filter(visible)
      .filter(el => /required|please (enter|select|answer|provide)|(add|enter|provide|select) a valid|is required|invalid|must be|valid (phone|email|number|value)/i.test(el.innerText || ''));
    return errTexts.length;
  }

  // Indeed SmartApply's transient server-side error banner. It is NOT a field
  // validation error — the values are usually fine; the step submission just
  // failed and retrying after a short wait normally works.
  const TRANSIENT_ERR_RE = /(there was an error, please try again|something went wrong|an error occurred|please try again later|unable to (save|submit)|try again)/i;
  function transientErrorPresent() {
    const alerts = Array.from(document.querySelectorAll('[role="alert"], [class*="error" i], [class*="alert" i], [data-testid*="error" i]'))
      .filter(visible);
    return alerts.some(el => TRANSIENT_ERR_RE.test(el.innerText || ''));
  }

  // Sign-in failed because there is no such account (so we should create one).
  const NOT_REGISTERED_RE = /does not correspond to a registered user|no account (found|exists)|not a registered|couldn't find (your )?account|no user (found|with)|account (does not|doesn't) exist/i;
  function accountNotRegistered() {
    return NOT_REGISTERED_RE.test(document.body?.innerText || '');
  }

  // Let filled values commit before advancing: blur the focused field and
  // dismiss any open autocomplete/listbox that would swallow the click.
  async function settleBeforeContinue() {
    const open = Array.from(document.querySelectorAll('[role="listbox"], [aria-expanded="true"]')).filter(visible);
    if (open.length) {
      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await sleep(200);
    }
    if (document.activeElement && document.activeElement.blur) {
      try { document.activeElement.blur(); } catch { /* ignore */ }
    }
    await sleep(500);
  }

  // ── step handlers ────────────────────────────────────────────────────────

  function stepSig(kindDetail) {
    // Signature = step heading + count of interactive elements. Coarse on
    // purpose: it should stay stable across minor rerenders of one step.
    const n = document.querySelectorAll('input, textarea, select').length;
    return `${kindDetail}::${n}::${location.pathname}`;
  }

  async function handleViewJob() {
    handleDialogs();

    if (!S.metaSent) {
      const title = document.querySelector('[data-testid="jobsearch-JobInfoHeader-title"], h1')?.innerText?.trim() || null;
      const company = document.querySelector('[data-testid="inlineHeader-companyName"], [data-company-name], [data-testid*="companyName"]')?.innerText?.trim() || null;
      const description = document.querySelector('#jobDescriptionText')?.innerText?.trim() || null;
      if (title || description) {
        S.metaSent = true;
        await send({ type: 'JOB_META', jobIndex: S.ctx.jobIndex, title, company, description });
        await report('job page loaded', title || '');
      }
    }

    if (S.applyClickedAt && Date.now() - S.applyClickedAt < 12000) return; // wait for nav

    // Easy Apply selectors (mirrored from module4's IndeedAdapter)
    const easySelectors = ['button#indeedApplyButton', 'button[data-testid="indeedApplyButton"]', 'button[buttontype="IA"]'];
    let easyBtn = easySelectors.map(s => document.querySelector(s)).find(b => b && visible(b));
    if (!easyBtn) {
      easyBtn = Array.from(document.querySelectorAll('button, a')).filter(visible)
        .find(b => /apply with indeed|easily apply/i.test(b.innerText || ''));
    }
    if (easyBtn) {
      S.applyClickedAt = Date.now();
      S.actions++;
      await report('clicking Easy Apply');
      easyBtn.scrollIntoView({ block: 'center' });
      easyBtn.click();
      return;
    }

    // No Easy Apply — follow the external "Apply on company site" path. The
    // background worker adopts the new tab (or this tab redirects) and the
    // engine continues on the company site with the human on standby.
    const externalBtn = document.querySelector(
      'a[data-testid="applyButtonLinkContainer"], [data-testid="applyButtonLinkContainer"] a,'
      + ' #applyButtonLinkContainer a, [id*="viewJobButtonLinkContainer"] a, a[data-indeed-apply-joburl],'
      + ' a[href*="/applystart"], a[href*="indeedapply"], a[rel*="nofollow"][target="_blank"][href*="http"]')
      || Array.from(document.querySelectorAll('a, button, [role="button"]')).filter(visible)
        .find(b => {
          const t = normText(b.innerText || b.getAttribute('aria-label') || '');
          return /apply on (the )?(company|employer)('s)?( own)? (site|website)|apply on company|apply externally|apply on (their|the employer'?s) (site|website)|apply on employer|^apply now$|^apply$/.test(t);
        });
    if (externalBtn) {
      S.extApplyAttempts = (S.extApplyAttempts || 0) + 1;
      if (S.extApplyAttempts > 3) {
        await needsHuman('The "Apply on company site" button is not responding to automation. Click it manually — the engine follows automatically.', 'ext-apply-btn');
        return;
      }
      S.applyClickedAt = Date.now();
      S.actions++;
      await report(`following external apply to company site (attempt ${S.extApplyAttempts})`);
      // Synthetic .click() is often ignored on Indeed's anchor wrappers —
      // navigate via the href when there is one, else fire a real event chain.
      const anchor = externalBtn.closest('a[href]') || externalBtn.querySelector('a[href]')
        || (externalBtn.tagName === 'A' && externalBtn.href ? externalBtn : null);
      if (anchor?.href && !/^javascript:/i.test(anchor.href)) {
        location.href = anchor.href;
        return;
      }
      dispatchRealClick(externalBtn);
      return;
    }

    S.viewjobTicks++;
    if (S.viewjobTicks > 8) {
      await finishJob('skipped', 'no apply button found (expired or already applied)');
    }
  }

  // External company job-listing page (no form yet): find and click Apply.
  async function handleExternalListing(detail) {
    handleDialogs();
    await sendExternalMeta();
    if (S.applyClickedAt && Date.now() - S.applyClickedAt < 12000) return;

    // Prefer applying DIRECTLY on the company site over "Apply with Indeed"
    // (which opens an uncontrollable separate window). If the page offers an
    // "apply manually / without Indeed / directly" option, take it.
    const manualBtn = Array.from(document.querySelectorAll('button, a, [role="button"]'))
      .filter(visible).find(b => MANUAL_APPLY_RE.test(normText(b.innerText || '')));
    if (manualBtn) {
      S.applyClickedAt = Date.now();
      S.actions++;
      await report('choosing manual/direct apply over Apply with Indeed', normText(manualBtn.innerText));
      dispatchRealClick(manualBtn);
      return;
    }

    // A prominent "Apply Now" control means this is a LISTING — click it to
    // start the real application. Check this BEFORE treating stray inputs (site
    // search, newsletter/job-alerts signup) as an application form.
    const applyBtn = Array.from(document.querySelectorAll('a, button, [role="button"]')).filter(visible)
      .find(b => {
        const t = normText(b.innerText);
        return t.length < 40 && /^apply( now| today| for this (job|position|role))?$|^apply here$|^start application$|^i'?m interested$/.test(t);
      });
    if (!applyBtn) {
      // No apply button — if there are REAL application fields, fill them.
      // Exclude the ubiquitous newsletter/job-alert email + site search.
      const formControls = Array.from(document.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]):not([type=email]), textarea, select, input[type="file"]'))
        .filter(visible);
      if (formControls.length >= 2 || document.querySelector('input[type="file"]')) {
        await handleFormStep(detail);
        return;
      }
    }
    if (applyBtn) {
      S.listingApplyAttempts = (S.listingApplyAttempts || 0) + 1;
      if (S.listingApplyAttempts > 3) {
        await needsHuman(
          `Clicked "${normText(applyBtn.innerText)}" ${S.listingApplyAttempts - 1}x but the page didn't change `
          + `(${location.hostname}). Advance it manually — automation resumes on the form.`, 'ext-apply-stuck');
        return;
      }
      S.applyClickedAt = Date.now();
      S.actions++;
      // PREFER navigating straight to the apply URL — synthetic clicks often
      // don't trigger framework navigation (Workday/Danaher "Apply Now"), but a
      // direct location change always works. Find the apply anchor's href.
      const anchor = (applyBtn.tagName === 'A' && applyBtn.href ? applyBtn : null)
        || applyBtn.closest('a[href]')
        || applyBtn.querySelector('a[href]')
        || Array.from(document.querySelectorAll('a[href]')).filter(visible)
            .find(a => /apply/i.test(a.href) && /\bapply\b/i.test(normText(a.innerText)));
      if (anchor?.href && !/^javascript:/i.test(anchor.href) && anchor.href !== location.href) {
        await report('external listing — opening apply URL', anchor.href.slice(0, 70));
        location.href = anchor.href;
        return;
      }
      // No usable href — fall back to a real click (button-driven navigation).
      await report('external listing — clicking Apply', normText(applyBtn.innerText));
      dispatchRealClick(applyBtn);
      return;
    }
    // No obvious Apply control — let the AI navigator read the page and pick.
    if (await aiNavigate()) return;

    S.viewjobTicks++;
    if (S.viewjobTicks > 10) {
      await needsHuman(
        `Can't find the application form on this company page (${location.hostname}). `
        + 'Navigate to the form manually — automation resumes when it appears. '
        + 'Or use Skip job in the popup.', 'ext-listing');
    }
  }

  // Title/company/description for external pages (registers CSV jobs in the
  // backend so module3 can tailor the resume against the real description).
  async function sendExternalMeta() {
    if (S.metaSent) return;
    S.metaSent = true;
    const title = (document.querySelector('h1')?.innerText || document.title || '').trim().slice(0, 200) || null;
    const company = location.hostname.replace(/^www\.|^jobs\.|^careers\.|^apply\./, '').split('.')[0] || null;
    const description = (document.body?.innerText || '').trim().slice(0, 15000) || null;
    await send({ type: 'JOB_META', jobIndex: S.ctx.jobIndex, title, company, description });
  }

  // ── AI page navigator ──────────────────────────────────────────────────
  // Fallback for unknown company/ATS pages: send the visible interactive
  // elements to Gemini and let it pick the next control to click (or decide
  // the application is done / needs a human). Deterministic heuristics run
  // first; this only fires when they're stuck.
  let _air = null; // current-pass AI-element registry (id -> el), isolated world
  const aiElById = (id) => (_air ? (_air.get(id) || null) : null);

  function collectAiElements() {
    _air = new Map();
    const els = Array.from(document.querySelectorAll(
      'button, a[href], [role="button"], input:not([type=hidden]), select, textarea, [role="option"]'))
      .filter(visible)
      .filter(el => !['submit-hidden'].includes(el.type));
    const out = [];
    for (const el of els) {
      if (out.length >= 60) break;
      const tag = el.tagName.toLowerCase();
      const isField = tag === 'input' || tag === 'select' || tag === 'textarea';
      const text = isField ? cleanLabel(labelForInput(el)) : normText(el.innerText || el.getAttribute('aria-label') || el.value || '');
      if (!text) continue;
      const aiId = `ai-${S.aiSeq++}`;
      _air.set(aiId, el);
      out.push({
        id: aiId, tag,
        type: isField ? (el.type || tag) : '',
        text,
        value: isField ? (el.value || '') : '',
        required: isField ? isRequired(el, text) : false,
      });
    }
    return out;
  }

  async function aiNavigate() {
    S.aiNavCount++;
    if (S.aiNavCount > 20) {
      await needsHuman('AI navigation exhausted its step budget on this site. Please advance the application manually.', 'ai-budget');
      return true;
    }
    const elements = collectAiElements();
    if (!elements.length) return false;
    const emptyRequired = scrapeFields().filter(f => f.required && !f.currentValue).map(f => f.label);
    const page = {
      url: location.href.slice(0, 200),
      title: (document.title || '').slice(0, 120),
      heading: headingText(),
      elements,
      emptyRequired,
    };
    const res = await send({ type: 'GET_AI_ACTION', jobIndex: S.ctx.jobIndex, page, resumeUploaded: S.resumeUploaded });
    if (!res || res.error) return false;

    if (res.action === 'done') {
      await report('AI: application appears submitted', res.reason || '');
      await finishJob('applied', 'confirmed by AI navigator');
      return true;
    }
    if (res.action === 'human') {
      await needsHuman(`AI navigator needs help: ${res.reason || 'unclear page'}. Advance manually — automation resumes.`, `ai-human:${res.reason || ''}`.slice(0, 60));
      return true;
    }
    if (res.action === 'click' && res.id) {
      const el = aiElById(res.id);
      if (!el || !visible(el)) return false;
      const clickText = normText(el.innerText || el.value || res.id);

      // Guard: never let the AI click social/SSO login — prefer the email
      // path, else hand to the human. (Fixes AI clicking "Continue with
      // Google" instead of using the candidate's email + password.)
      if (SOCIAL_LOGIN_RE.test(clickText)) {
        const emailBtn = findButton([EMAIL_LOGIN_RE]);
        if (emailBtn) {
          await report('AI picked social login — using email path instead');
          dispatchRealClick(emailBtn);
          return true;
        }
        await needsHuman('This login only offers Google/Apple/SSO sign-in, which automation can\'t do. Please log in manually — automation resumes after.', 'social-login');
        return true;
      }
      // Guard: don't click "Apply with Indeed" on a company site — it opens an
      // uncontrollable separate window. Prefer a direct/manual apply option or
      // the on-page form; only ask the human if there's no other path.
      if (INDEED_APPLY_RE.test(clickText) && !isIndeedHost() && !isIframe) {
        const manualBtn = findButton([MANUAL_APPLY_RE]);
        if (manualBtn) {
          await report('AI picked Apply with Indeed — choosing direct apply instead');
          dispatchRealClick(manualBtn);
          return true;
        }
        if (hasFillableForm()) return false; // let the form handler fill it
        await needsHuman('This job only offers "Apply with Indeed" (opens a separate window). Please complete it manually, or use Skip job.', 'indeed-apply-ext');
        return true;
      }

      // Loop guard that survives element-id churn (React re-renders give the
      // same button a new id each tick): dedupe by the button's TEXT and
      // whether the page actually changed since the last AI click.
      const pageSig = stepSig('ai');
      if (clickText === S.lastAiText && pageSig === S.lastAiSig) {
        S.aiClickRepeat = (S.aiClickRepeat || 0) + 1;
        if (S.aiClickRepeat >= 2) {
          await needsHuman(
            `Clicking "${clickText}" isn't changing the page — it may open a form in a frame the automation can't reach. `
            + 'Please advance the application manually; automation resumes on the next screen.', 'ai-noprogress');
          return true;
        }
      } else {
        S.aiClickRepeat = 0;
      }
      S.lastAiText = clickText;
      S.lastAiSig = pageSig;
      await report('AI navigator clicking', clickText);
      S.actions++;
      dispatchRealClick(el);
      const anchor = el.closest('a[href]');
      if (anchor?.href && !/^javascript:/i.test(anchor.href)) {
        await sleep(1200);
      }
      return true;
    }
    return false; // wait
  }

  // While a step is flagged for the human, watch the fields they fill and save
  // those answers to the field memory so future runs answer them automatically.
  async function captureHumanFills(sig) {
    const pending = S.learnPending[sig] || [];
    if (!pending.length) return;
    const current = scrapeFields();
    const entries = [];
    const remaining = [];
    for (const p of pending) {
      const f = current.find(c => normText(c.label) === normText(p.label));
      if (f && f.currentValue) entries.push({ label: p.label, answer: f.currentValue });
      else remaining.push(p);
    }
    if (entries.length) {
      await send({ type: 'LEARN_ANSWERS', jobIndex: S.ctx.jobIndex, entries, pageUrl: location.href });
    }
    S.learnPending[sig] = remaining;
  }

  // Fill a set of fields from their resolved answers. Profile/policy answers
  // overwrite prefilled values (Indeed prefills the BD agent's own data);
  // other sources only fill empties. Never writes an empty answer.
  async function applyAnswers(fields, answers) {
    for (let i = 0; i < fields.length; i++) {
      const field = fields[i];
      const a = answers[i];
      if (!a || a.answer == null || a.answer === '') continue;
      const authoritative = a.source === 'profile' || a.source === 'policy';
      if (field.currentValue && !authoritative) continue;
      if (normText(field.currentValue) === normText(a.answer)) continue;
      await fillField(field, a.answer);
      await sleep(FIELD_PACE_MS); // ~1s between fields — unhurried, one at a time
    }
  }

  async function handleFormStep(detail) {
    handleDialogs();
    if (!isIndeedHost()) await sendExternalMeta();

    // Login / signup / account wall handling — ONLY when this is genuinely an
    // auth screen. A stray newsletter/job-alert email input does NOT count.
    // Auth signal = a password field, OR social/email-login/create-account
    // buttons present.
    const authButtons = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible)
      .filter(b => SOCIAL_LOGIN_RE.test(normText(b.innerText || '')) || EMAIL_LOGIN_RE.test(normText(b.innerText || '')) || CREATE_ACCOUNT_RE.test(normText(b.innerText || '')));
    const isAuthUI = !!document.querySelector('input[type="password"]') || authButtons.length > 0;
    if (isAuthUI) {
      const loginish = !document.querySelector('input[type="password"]') && !emailInputPresent();

      // (1) Skip login — best outcome, apply as guest.
      const skipBtn = Array.from(document.querySelectorAll('button, a, [role="button"]'))
        .filter(visible).find(b => SKIP_LOGIN_RE.test(normText(b.innerText || '')));
      if (skipBtn) {
        await report('login wall — skipping account, applying as guest', normText(skipBtn.innerText));
        S.actions++;
        dispatchRealClick(skipBtn);
        return;
      }

      if (loginish) {
        // (3) reveal email/password
        const emailBtn = findButton([EMAIL_LOGIN_RE]);
        if (emailBtn) {
          await report('login wall — choosing email path');
          S.actions++; dispatchRealClick(emailBtn); return;
        }
        // (4) prefer creating an account over signing in
        const createBtn = findButton([CREATE_ACCOUNT_RE]);
        if (createBtn) {
          await report('login wall — creating an account (candidate has none)');
          S.actions++; dispatchRealClick(createBtn); return;
        }
        // (5) social/SSO only
        await needsHuman('This login only offers Google/Apple/SSO sign-in, which automation can\'t do. Please log in manually — automation resumes after.', 'social-login');
        return;
      }
      // else: email/password fields ARE present → fall through and fill them.
    }

    // Resume handling. On Indeed the resume is its own wizard step; on
    // external ATS forms the file input usually sits inline with the other
    // fields — upload first, then fall through and fill the rest.
    if (!S.resumeUploaded && document.querySelector('input[type="file"]')
        && /resume|curriculum vitae|\bcv\b/i.test(document.body.innerText || '')) {
      if (isSmartApply()) { await handleResumeStep(detail); return; }
      const proceed = await inlineResumeUpload();
      if (!proceed) return; // still waiting on the tailored PDF — next tick retries
    }

    const sig = stepSig(detail);

    if (S.processedSigs.has(sig)) {
      // We already filled + clicked continue on this exact step.
      if (S.humanSigs.has(sig)) return; // handed to the human — don't interfere

      // Learn mode: the human is filling fields we couldn't. Capture their
      // values into the field memory as they type; don't touch the page.
      if (S.watchSigs.has(sig)) { await captureHumanFills(sig); return; }

      // Indeed's transient "there was an error, please try again" banner:
      // the values are fine, the submit just failed. Wait, then re-click —
      // clicking instantly re-triggers the same error. Cap the retries.
      if (transientErrorPresent()) {
        S.transientRetries = (S.transientRetries || {});
        S.transientRetries[sig] = (S.transientRetries[sig] || 0) + 1;
        if (S.transientRetries[sig] > 4) {
          await needsHuman(`Indeed keeps showing "there was an error, please try again" on step "${detail}". Click Continue manually — automation resumes after.`, `transient:${sig}`);
          return;
        }
        await report(`Indeed transient error — waiting and retrying Continue (${S.transientRetries[sig]}/4)`);
        await sleep(2500);
        await settleBeforeContinue();
        clickContinue();
        return;
      }

      // Sign-in failed because the account doesn't exist → switch to creating
      // one (our candidates usually have no account on the portal).
      if (accountNotRegistered() && !S.triedCreate) {
        const createBtn = findButton([CREATE_ACCOUNT_RE]);
        if (createBtn) {
          S.triedCreate = true;
          await report('sign-in failed (no account) — creating an account instead');
          S.actions++;
          S.processedSigs.delete(sig);
          dispatchRealClick(createBtn);
          return;
        }
      }

      // Validation errors ("Errors Found", "X is required") — refill the
      // missing fields. Allow a few rounds (a fix can surface a new error).
      const errs = validationErrors();
      if (errs) {
        S.refillCount = S.refillCount || {};
        S.refillCount[sig] = (S.refillCount[sig] || 0) + 1;
        if (S.refillCount[sig] <= 3) {
          S.processedSigs.delete(sig);
          await report('validation errors — refilling', `${errs} field(s), round ${S.refillCount[sig]}`);
          return; // next tick re-scrapes and fills (incl. new heuristics)
        }
        await needsHuman(`Validation errors on step "${detail}" that auto-fill could not resolve after ${S.refillCount[sig] - 1} tries. Please fix and continue.`, sig);
        return;
      }

      // VERIFY-BEFORE-ADVANCE: never click Continue until EVERY required field
      // is actually filled (committed in the DOM). Refill any still-empty
      // required field one round per tick — this waits out slow radios/dropdowns
      // whose selection commits after a beat, and is what stops Indeed's red
      // "required" error from a premature Continue click.
      const emptyReq = scrapeFields().filter(f => f.required && !f.currentValue);
      if (emptyReq.length) {
        S.fillRounds = S.fillRounds || {};
        S.fillRounds[sig] = (S.fillRounds[sig] || 0) + 1;
        if (S.fillRounds[sig] <= 6) {
          await report(`completing required field(s) before Continue (round ${S.fillRounds[sig]})`, emptyReq.map(f => f.label).slice(0, 3).join(' | '));
          const r = await send({ type: 'GET_ANSWERS', jobIndex: S.ctx.jobIndex, fields: emptyReq, pageUrl: location.href, forceAi: true });
          await applyAnswers(emptyReq, r.answers || []);
          await sleep(300);
          return; // re-verify next tick; do NOT advance yet
        }
        // Exhausted retries — hand the remaining fields to the human.
        await needsHuman(`Couldn't fill required field(s): ${emptyReq.map(f => f.label).slice(0, 4).join(' | ')}. Please fill manually — automation resumes.`, sig);
        S.watchSigs.add(sig);
        S.learnPending[sig] = emptyReq.map(f => ({ label: f.label, type: f.type, options: f.options || null }));
        return;
      }

      // All required fields committed and no errors — NOW it's safe to advance.
      S.actions++;
      await settleBeforeContinue();
      await tryAdvance(sig, detail);
      return;
    }

    const fields = scrapeFields();
    await report(`form step: ${detail || 'unnamed'}`, `${fields.length} field(s)`);

    if (fields.length) {
      const res = await send({ type: 'GET_ANSWERS', jobIndex: S.ctx.jobIndex, fields, pageUrl: location.href });
      if (res.error) { await logBg(`GET_ANSWERS error: ${res.error}`, 'warn'); return; }
      await applyAnswers(fields, res.answers || []);

      // ── REVIEW PASS ──────────────────────────────────────────────────────
      // Re-scan the form: some fields may still be empty (a fill that didn't
      // take, a late-rendering field, or one the first pass couldn't answer).
      // Make a final AI attempt for just those before involving the human.
      if (!S.reviewedSigs.has(sig)) {
        S.reviewedSigs.add(sig);
        await sleep(400);
        const rescanned = scrapeFields();
        const stillEmpty = rescanned.filter(f =>
          !f.currentValue && (f.required || f.type === 'text' || f.type === 'textarea' || f.type === 'select' || f.type === 'listbox'));
        if (stillEmpty.length) {
          await report('review pass — filling remaining fields with AI', `${stillEmpty.length} field(s)`);
          const res2 = await send({ type: 'GET_ANSWERS', jobIndex: S.ctx.jobIndex, fields: stillEmpty, pageUrl: location.href, forceAi: true });
          await applyAnswers(stillEmpty, res2.answers || []);
        }
      }

      // Decide what (if anything) is still unanswered, from a FRESH scrape.
      await sleep(200);
      const finalScan = scrapeFields();
      const unresolvedFields = finalScan
        .filter(f => f.required && !f.currentValue)
        .map(f => ({ label: f.label, type: f.type, options: f.options || null }));
      if (unresolvedFields.length) {
        const labels = unresolvedFields.map(f => f.label).slice(0, 4).join(' | ');
        await needsHuman(`Could not answer required field(s): ${labels}. Please fill manually — automation resumes after you continue.`, sig);
        S.processedSigs.add(sig);   // don't refill over the human
        S.watchSigs.add(sig);       // watch & learn what the human enters
        S.learnPending[sig] = unresolvedFields;
        return;
      }
    }

    // Mark processed WITHOUT clicking Continue yet. The processed branch below
    // then VERIFIES every required field actually committed (refilling any that
    // are still empty, giving slow radios/dropdowns time to commit) and only
    // THEN clicks Continue. This stops the premature click that left required
    // fields empty and tripped Indeed's red "required" error.
    S.processedSigs.add(sig);
  }

  // Advance a step that's already been filled. Waits patiently across ticks —
  // the primary button may still be rendering, or a submit may be settling —
  // and only escalates to the human after the step has genuinely stalled.
  async function tryAdvance(sig, detail) {
    S.stuck = S.stuck || {};
    // Count total ticks spent on this exact step. When we actually advance the
    // step's signature changes, so a fresh counter starts — meaning a rising
    // counter here means "we're stuck", whether the button is missing OR a
    // click is being ignored.
    S.stuck[sig] = (S.stuck[sig] || 0) + 1;
    const clicked = clickContinue();
    // Just clicked a real button — give the page a couple of ticks to navigate
    // before doing anything else.
    if (clicked && S.stuck[sig] <= 2) return;
    // Unknown company page and no progress after a few ticks — let the AI
    // navigator read the page and pick the next control.
    if (!isIndeedHost() && S.stuck[sig] === 3) {
      if (await aiNavigate()) return;
    }
    const limit = isIndeedHost() ? 8 : 14; // ~20s Indeed, ~35s external
    if (S.stuck[sig] > limit) {
      await needsHuman(
        `Step "${detail}" isn't advancing after waiting. `
        + 'Please advance it manually — automation resumes, or use Skip job.', `stuck:${sig}`);
    }
    // else: return silently and let the next tick retry as the page settles.
  }

  // Find (or reveal) the resume file input. Handles Indeed's pre-uploaded-CV
  // card: "Resume options" menu → "Upload a different file" → input appears.
  async function revealFileInput() {
    let fi = document.querySelector('input[type="file"]');
    if (fi) return fi;

    // Indeed's kebab/"Resume options" menu next to an existing resume card
    const optionsBtn = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible)
      .find(b => /resume options/i.test(b.innerText || '') || /resume options/i.test(b.getAttribute('aria-label') || ''));
    if (optionsBtn) {
      dispatchRealClick(optionsBtn);
      await sleep(900);
      const item = Array.from(document.querySelectorAll('[role="menuitem"], [role="option"], li, button, a')).filter(visible)
        .find(b => /upload a different|upload different|replace (resume|file)|upload new/i.test(b.innerText || ''));
      if (item) { dispatchRealClick(item); await sleep(1200); }
      fi = document.querySelector('input[type="file"]');
      if (fi) return fi;
    }

    // Generic "Upload resume" button/option
    const uploadBtn = Array.from(document.querySelectorAll('button, label, [role="button"], [role="radio"], [data-testid*="upload" i]'))
      .filter(visible).find(b => /upload/i.test(b.innerText || '') && /resume|cv|file|different/i.test(b.innerText || ''));
    if (uploadBtn) { uploadBtn.click(); await sleep(1200); }
    return document.querySelector('input[type="file"]');
  }

  // Inline upload for external ATS forms. Returns true when form-filling can
  // proceed (uploaded, or unrecoverable → human flagged), false to wait.
  async function inlineResumeUpload() {
    const res = await send({ type: 'GET_RESUME', jobIndex: S.ctx.jobIndex });
    if (res.status === 'pending') {
      S.resumePolls++;
      if (S.resumePolls === 1) await report('waiting for tailored resume before filling the form...');
      if (S.resumePolls > 70) {
        await needsHuman('Tailored resume is taking too long — attach a resume manually; fields will still auto-fill.', 'inline-resume');
        return true;
      }
      return false;
    }
    if (res.status === 'ready' && res.b64) {
      const fileInput = await revealFileInput();
      if (fileInput) {
        try {
          const fname = res.filename || 'resume.pdf';
          const bytes = Uint8Array.from(atob(res.b64), c => c.charCodeAt(0));
          const dt = new DataTransfer();
          dt.items.add(new File([bytes], fname, { type: 'application/pdf' }));
          fileInput.files = dt.files;
          fileInput.dispatchEvent(new Event('input', { bubbles: true }));
          fileInput.dispatchEvent(new Event('change', { bubbles: true }));
          await sleep(2000);
          S.resumeUploaded = true;
          await report(`resume attached to external form (${res.tailored ? 'tailored' : 'base'})`, fname);
          return true;
        } catch (e) {
          await logBg(`inline resume upload failed: ${e.message}`, 'warn');
        }
      }
    }
    await needsHuman(
      `Could not attach the resume automatically${res.error ? ` (${res.error})` : ''}. `
      + 'Attach it manually — the other fields will still be auto-filled.', 'inline-resume');
    return true;
  }

  async function handleResumeStep(detail) {
    handleDialogs();
    const sig = stepSig(detail);
    if (S.humanSigs.has(sig)) return; // handed to the human — don't interfere
    // The upload rerenders the step (filename card appears), changing the
    // signature — without this flag the next tick would upload again.
    if (S.resumeUploaded || S.processedSigs.has(sig)) { clickContinue(); return; }

    const res = await send({ type: 'GET_RESUME', jobIndex: S.ctx.jobIndex });
    if (res.status === 'pending') {
      S.resumePolls++;
      if (S.resumePolls === 1) await report('resume step: waiting for tailored resume from backend...');
      if (S.resumePolls > 70) { // ~3 min of ticks
        await needsHuman('Tailored resume is taking too long. Select/upload a resume manually and continue.', sig);
      }
      return;
    }

    if (res.status === 'ready' && res.b64) {
      const fileInput = await revealFileInput();
      if (fileInput) {
        try {
          const fname = res.filename || 'resume.pdf';
          const bytes = Uint8Array.from(atob(res.b64), c => c.charCodeAt(0));
          const file = new File([bytes], fname, { type: 'application/pdf' });
          const dt = new DataTransfer();
          dt.items.add(file);
          fileInput.files = dt.files;
          fileInput.dispatchEvent(new Event('input', { bubbles: true }));
          fileInput.dispatchEvent(new Event('change', { bubbles: true }));

          // VERIFY the upload registered: the filename (or a fresh .pdf chip)
          // must appear on the page. Continuing blind risks a resume-less
          // application.
          let verified = false;
          for (let i = 0; i < 8 && !verified; i++) {
            await sleep(1000);
            const bodyTxt = document.body.innerText || '';
            verified = bodyTxt.includes(fname)
              || bodyTxt.includes(fname.replace(/\.pdf$/i, ''))
              || /uploaded|upload complete/i.test(bodyTxt);
          }
          if (verified) {
            await report(`resume uploaded and verified (${res.tailored ? 'tailored' : 'base'})`, fname);
            S.actions++;
            S.resumeUploaded = true;
            S.processedSigs.add(sig);
            await sleep(1500);
            clickContinue();
            return;
          }
          await logBg('resume upload did not register (filename never appeared)', 'warn');
        } catch (e) {
          await logBg(`resume upload failed: ${e.message}`, 'warn');
        }
      } else {
        await logBg('no file input found on resume step', 'warn');
      }
    }

    // NOTE: deliberately NO auto-select of existing resume cards — those
    // belong to the BD agent's own Indeed account, not the candidate. A human
    // must decide. Automation resumes automatically after they continue.
    await needsHuman(
      `Resume step needs help${res.error ? ` (${res.error})` : ''}: automatic upload didn't register. `
      + 'Upload the candidate\'s resume manually and click Continue.', sig);
    S.humanSigs.add(sig);
    S.processedSigs.add(sig);
  }

  // Email-verification screen: fetch the code from the candidate's Gmail via
  // the backend code-catcher, fill it, continue. Human fallback if the
  // candidate has no Gmail connected or the email never arrives.
  async function handleOtpStep() {
    handleDialogs();
    if (S.otpFilled) { clickContinue(); return; }
    const shape = detectOtpShape();
    if (!shape) return;

    const res = await send({ type: 'GET_CODE', jobIndex: S.ctx.jobIndex, senderHint: location.hostname.replace(/^www\./, '').split('.')[0] });
    if (!res.code) {
      S.otpPolls = (S.otpPolls || 0) + 1;
      if (S.otpPolls === 1) await report('verification screen — fetching code from candidate Gmail...');
      if (S.otpPolls > 45) { // ~2 min of ticks
        await needsHuman(
          'Verification code not found in the candidate\'s Gmail (not connected, or email not arriving). '
          + 'Read the code from their inbox and type it manually — automation resumes after.', 'otp');
      }
      return;
    }

    const code = String(res.code).trim();
    await report('filling verification code', `${code.length} chars, ${shape.kind}`);
    if (shape.kind === 'split') {
      const boxes = shape.els;
      for (let i = 0; i < boxes.length && i < code.length; i++) {
        boxes[i].focus();
        setNativeValue(boxes[i], code[i]);
        await sleep(80);
      }
    } else {
      setNativeValue(shape.els[0], code);
    }
    S.otpFilled = true;
    S.actions++;
    await sleep(600);
    // Verify/submit button on OTP screens
    const btn = findButton([/^verify$/, /^confirm$/, /^submit$/, /^continue$/, /^next$/])
      || document.querySelector('form button[type="submit"]');
    if (btn && visible(btn)) dispatchRealClick(btn);
  }

  async function handleReview(detail) {
    handleDialogs();
    if (!S.ctx.autoSubmit) {
      await needsHuman('Application ready for review. Review and click Submit manually.', 'review');
      return;
    }
    const sig = stepSig('review');
    if (S.processedSigs.has(sig)) return; // submit already clicked; wait for success
    const btn = findButton(SUBMIT_PATTERNS)
      || document.querySelector('button[data-testid="indeedApplyButton-submit"]');
    if (btn && visible(btn)) {
      S.processedSigs.add(sig);
      S.actions++;
      await report('review page — submitting application');
      btn.scrollIntoView({ block: 'center' });
      btn.click();
    } else {
      await needsHuman('On review page but no Submit button found. Please submit manually.', 'review-nobtn');
    }
  }

  // ── main loop ────────────────────────────────────────────────────────────

  async function tick() {
    if (S.busy || S.done) return;
    S.busy = true;
    try {
      const gate = await send({ type: 'TICK' });
      if (!gate.act) { S.busy = false; return; }
      S.active = true;
      S.ctx = { jobIndex: gate.jobIndex, job: gate.job, candidateName: gate.candidateName, autoSubmit: gate.autoSubmit, hasSitePassword: gate.hasSitePassword };

      // Readiness gate — never act on a page that's still loading. Prevents
      // the "no Continue button, please do it manually" false alarm that fires
      // before the form has rendered. SUCCESS/BLOCKED still evaluate below via
      // classify(), but they only match on already-painted content anyway.
      if (document.readyState !== 'complete') { S.busy = false; return; }

      if (S.actions > MAX_ACTIONS) {
        await finishJob('failed', `exceeded ${MAX_ACTIONS} actions — likely stuck`);
        return;
      }

      const { kind, detail } = classify();
      // Diagnostic: report page classification when it changes, so the activity
      // log shows what the engine sees on each tab/frame (host, iframe, kind).
      const classSig = `${kind}@${location.hostname}${isIframe ? '(iframe)' : ''}`;
      if (classSig !== S.lastClassSig) {
        S.lastClassSig = classSig;
        await report(`page: ${kind} on ${location.hostname}${isIframe ? ' [iframe]' : ''}`, detail || '');
      }
      switch (kind) {
        case 'BLOCKED':
          await needsHuman(`Blocked page detected (${detail}) — likely CAPTCHA or Indeed login. Solve it in the tab; automation resumes automatically.`, 'blocked');
          break;
        case 'SUCCESS':
          await report('application submitted', detail);
          await finishJob('applied', detail);
          break;
        case 'UNAVAILABLE':
          await finishJob('skipped', `job unavailable (${detail})`);
          break;
        case 'VIEWJOB':
          await handleViewJob();
          break;
        case 'EXTERNAL_LISTING':
          await handleExternalListing(detail);
          break;
        case 'OTP':
          await handleOtpStep();
          break;
        case 'RESUME':
          await handleResumeStep(detail);
          break;
        case 'REVIEW':
          await handleReview(detail);
          break;
        case 'FORM':
          await handleFormStep(detail);
          break;
        case 'WAIT':
        default:
          break; // transient loader / unknown — wait for next tick
      }
    } catch (e) {
      console.warn('[bd-indeed] tick error', e);
    } finally {
      S.busy = false;
    }
  }

  // Kick off: check with background whether this tab is under automation.
  (async () => {
    // PORTAL GATE: this is the INDEED engine — it only acts on indeed.com
    // frames (job pages, SmartApply, and the Indeed-Apply widget iframe that
    // Indeed embeds on company pages). Company/ATS sites are handled by the
    // separate generic.js engine, so changes there never affect Indeed.
    if (!isIndeedHost()) return;
    const gate = await send({ type: 'CONTENT_READY' });
    if (!gate.act) return; // normal browsing tab — do nothing, ever
    S.ctx = { jobIndex: gate.jobIndex, job: gate.job, candidateName: gate.candidateName, autoSubmit: gate.autoSubmit, hasSitePassword: gate.hasSitePassword };
    S.active = true;

    setInterval(tick, TICK_MS);

    // React SPA step changes don't reload the document — nudge the loop on
    // meaningful DOM mutations (debounced).
    let moTimer = null;
    new MutationObserver(() => {
      if (moTimer) return;
      moTimer = setTimeout(() => { moTimer = null; tick(); }, 700);
    }).observe(document.documentElement, { childList: true, subtree: true });

    tick();
  })();
})();
