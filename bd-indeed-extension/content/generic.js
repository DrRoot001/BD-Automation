// BD Auto-Apply — GENERIC PORTAL engine (content script, guarded IIFE).
//
// ⚑ PORTAL SPLIT: this file handles EVERY non-Indeed portal — company career
//   sites and ATSes (Lever, Greenhouse, Workday, Ashby, iCIMS, Taleo, …). It
//   tries its best on ANY site. It is injected (by the background worker) only
//   on non-indeed.com pages, so it and the Indeed engine (content/indeed.js)
//   never run on the same page. EDIT THIS FILE for any company/ATS-site
//   behaviour — Indeed stays untouched and safe in indeed.js.
//
// Engine: a single tick() classifies the page (external listing vs form vs
// login wall vs captcha/OTP vs success), then handles it: scrape labelled
// fields → ask background for answers → fill → review-pass AI-fill → submit.
// A DOM-based AI navigator handles unknown layouts. CAPTCHAs / social-only
// logins flag the human; the script keeps polling and resumes automatically.

(() => {
  if (window.__bdGenericApplyInjected) return;
  window.__bdGenericApplyInjected = true;

  const TICK_MS = 2500;
  // DRY-RUN (testing only): when a page sets window.__BD_DRYRUN__ = true before
  // this script runs, the engine still scrapes and FILLS every field (so we can
  // watch it work on a real ATS page) but every OUTWARD action — Create Account,
  // Sign In, Save & Continue, Next, Submit, and resume upload — becomes a no-op
  // that just logs "would …". Nothing is ever sent to the employer and no account
  // is created. Never set in the real extension.
  const DRY_RUN = (typeof window !== 'undefined' && !!window.__BD_DRYRUN__);
  const dryLog = (what) => { try { console.log('%c[BD dry-run] skipped: ' + what, 'color:#e36209'); } catch (e) { /* */ } };
  // Human pace on company sites too (like the Indeed engine): ~1s between fields,
  // typed character-by-character. Calmer, fewer rapid clicks, easier to watch.
  const FIELD_PACE_MS = 1000;
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
  const OTP_CUE_RE = /(access code|verification code|security code|login code|email code|confirmation code|one[-\s]?time (code|passcode|password|pin)|\bpasscode\b|\botp\b|enter (the |your |a )?(\d[-\s]?digit )?(access |verification |security |login |one[-\s]?time )?code|(we'?(ve| have)?|has been) (sent|emailed)[^.]{0,40}code|code[^.]{0,25}(sent|emailed)|sent (you )?(a|an|the) code|(sent|emailed) to your (email|inbox)|check your (email|inbox) for|\d[-\s]?digit code|confirm your email)/i;

  function detectOtpShape() {
    if (!OTP_CUE_RE.test(document.body?.innerText || '')) return null;
    // Include type=password: Taleo's access-code input is a masked password
    // field (id="…accessCodeId" type="password"). Excluding it meant we filled
    // the wrong element and submitted an empty code → "incorrect".
    const inputs = Array.from(document.querySelectorAll(
      'input[type="text"], input[type="tel"], input[type="number"], input[inputmode="numeric"], input[type="password"], input:not([type])'))
      .filter(visible);
    if (!inputs.length) return null;
    const CODE_KEY = /(verif|confirm|otp|one[-\s]?time|security|access[-\s]?code|\bcode\b|passcode|pin|mfa)/;
    // Split boxes FIRST: 4–10 single-char inputs (Greenhouse "security-input-0…"
    // cluster) — checked before the named match so a per-box id doesn't collapse
    // the whole code into box 0.
    const ones = inputs.filter(e => parseInt(e.getAttribute('maxlength') || '0', 10) === 1);
    if (ones.length >= 4 && ones.length <= 10) return { kind: 'split', els: ones };
    // Single input whose id/name/label explicitly names the code — this beats any
    // stray text field (e.g. a username display input) and catches Taleo's masked
    // password access-code field (id="…accessCodeId" type="password").
    const named = inputs.find(e =>
      CODE_KEY.test(`${labelForInput(e)} ${e.id || ''} ${e.name || ''} ${e.placeholder || ''}`.toLowerCase()));
    if (named) return { kind: 'single', els: [named] };
    // Last resort: a lone visible input.
    return inputs.length === 1 ? { kind: 'single', els: [inputs[0]] } : null;
  }

  function headingText() {
    for (const sel of ['h1', '[data-testid="page-title"]', 'main h2', 'h2']) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null && el.innerText.trim()) return el.innerText.trim().toLowerCase();
    }
    return '';
  }

  // Is this a conversational (chatbot) application? Look for a message composer
  // — a contenteditable/textbox or a textarea/input whose placeholder/aria says
  // "message"/"reply"/"chat"/"ask" — together with a chat-like container or a
  // Send control. Conservative so ordinary forms (cover-letter textarea, etc.)
  // are NOT misread as chats.
  function chatComposerPresent() {
    if (isIframe) return false;
    const CHAT_HINT = /\b(type (a|your) message|send a message|write (a )?message|your message|reply|chat|message\b|ask (me|us)?|talk to)\b/i;
    const composers = Array.from(document.querySelectorAll(
      '[contenteditable="true"], [contenteditable=""], [role="textbox"], textarea, input[type="text"], input:not([type])'))
      .filter(visible);
    const composer = composers.find(el => {
      if (el.isContentEditable || el.getAttribute('role') === 'textbox') {
        // A bare contenteditable is a strong chat signal on a non-form page.
        return CHAT_HINT.test(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '') || true;
      }
      const hint = `${el.getAttribute('placeholder') || ''} ${el.getAttribute('aria-label') || ''} ${el.name || ''}`;
      return CHAT_HINT.test(hint);
    });
    if (!composer) return false;
    // Corroborate with a chat container OR a Send control OR conversation-shaped
    // markup, so a lone "message to the hiring team" textarea on a normal form
    // doesn't trip it.
    const container = composer.closest('[class*="chat" i], [class*="messenger" i], [class*="conversation" i], [class*="composer" i], [role="log"]');
    const sendBtn = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible)
      .some(b => /^(send|reply|send message)$/i.test(normText(b.innerText || b.getAttribute('aria-label') || '')));
    const bubbles = document.querySelectorAll('[class*="message" i], [class*="bubble" i], [role="listitem"]').length >= 3;
    return !!(container || (sendBtn && bubbles) || (composer.isContentEditable && (sendBtn || bubbles)));
  }

  // Is this a standalone TERMS / PRIVACY / AGREEMENT gate — a page that's mostly
  // text with an "I Accept / I Agree / Continue" control (often disabled until
  // you scroll to the bottom)? These are NOT ordinary forms: the field-filler
  // treats the lone accept control as a field, "answers" it, clicks Continue,
  // and the ATS reloads the same gate forever. Route them to the AI navigator,
  // which reads the page, scrolls through the terms, and clicks Accept.
  function termsGatePresent() {
    const h = headingText().toLowerCase();
    const body = lowerBody();
    const headingHit = /(privacy (agreement|statement|policy|notice)|terms (and|&) conditions|terms of (use|service)|data (privacy|protection)|e-?signature|consent|disclaimer|agreement)/.test(h);
    const bodyHit = /(i have read and (agree|accept|understand)|please read (the |these )?(terms|agreement|privacy|following)|by (clicking|continuing|submitting).{0,40}(agree|accept|consent)|processing of your personal|you (must )?(agree|consent) to|acknowledge (that|and))/.test(body);
    const ctrls = Array.from(document.querySelectorAll(
      'button, a[href], [role="button"], input[type="submit"], input[type="button"], input[type="checkbox"], label'))
      .filter(visible)
      .map(b => normText(b.innerText || b.value || b.getAttribute('aria-label') || ''));
    // An EXPLICIT accept/agree control is a near-certain gate signal by itself —
    // ordinary application forms don't have "I Accept" / "I Agree" as a button.
    const strongAccept = ctrls.some(t => /\b(i accept|i agree|i consent|accept (and continue|the terms|all|terms|&)|agree (and continue|to (the )?terms|&))\b/.test(t) || /^(accept|agree)$/.test(t));
    if (strongAccept) return true;
    // The weaker heading/body heuristic must NOT run inside sub-frames (ad and
    // tracking iframes injected under all_frames could match a stray word).
    if (isIframe) return false;
    // Otherwise: an agreement heading/body + any advance control + mostly text.
    if (!headingHit && !bodyHit) return false;
    const anyAdvance = ctrls.some(t => /\b(accept|agree|consent|continue|proceed|acknowledge|next|submit)\b/.test(t));
    if (!anyAdvance) return false;
    // A gate is mostly text — very few real input fields. (A normal application
    // form with a consent checkbox has several inputs and must NOT be hijacked.)
    const meaningful = Array.from(document.querySelectorAll(
      'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]):not([type=checkbox]):not([type=radio]):not([type=image]):not([type=reset]), textarea, select'))
      .filter(visible);
    return meaningful.length <= 2;
  }

  // "Start Your Application" entry-choice screen (Workday/iCIMS): a set of apply
  // options (Autofill with Resume / Apply Manually / Use My Last Application /
  // Apply with LinkedIn). We take "Apply Manually" (handled in handleDialogs).
  function hasApplyEntryChoice() {
    if (isIframe) return false;
    return Array.from(document.querySelectorAll('button, a, [role="button"], label')).filter(visible)
      .some(b => /^(apply manually|autofill with (my )?resume|apply with (my )?resume|use my last application|use last application)$/i
        .test(normText(b.innerText || b.getAttribute('aria-label') || '')));
  }

  function classify() {
    const url = location.href.toLowerCase();
    const title = (document.title || '').toLowerCase();
    if (isIndeedHost()) {
      for (const m of BLOCKED_URL_MARKERS) if (url.includes(m)) return { kind: 'BLOCKED', detail: `url marker "${m}"` };
    }
    // Blocked/captcha detection is TOP-DOCUMENT ONLY. With all-frames injection
    // this engine also runs INSIDE the invisible hCaptcha/reCAPTCHA iframe that
    // Lever/Greenhouse embed — whose own page title is literally "hCaptcha".
    // A sub-frame must never declare the whole page blocked from its own title.
    if (!isIframe) {
      if (/(^|\s)(blocked|access denied|just a moment)/.test(title) || title.includes('captcha')) {
        return { kind: 'BLOCKED', detail: `title "${document.title}"` };
      }
    }
    const body = lowerBody();
    if (!isIframe) {
      for (const m of BLOCKED_TEXT_MARKERS) if (body.includes(m)) return { kind: 'BLOCKED', detail: m };
    }
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
      // A login / create-account form is ALWAYS fillable now: the extension has
      // a strong default portal password (config.js portalPassword), so it can
      // create an account or sign in even without a candidate-specific password.
      // (Was BLOCKING when hasSitePassword was false → the Workday "Create
      // Account" page just sat there flagged to the human.)
      return { kind: 'FORM', detail: 'login / account form' };
    }
    // Terms/privacy/agreement GATE (mostly text + an Accept control, often
    // disabled until scrolled). Route to the AI navigator so it reads, scrolls,
    // and accepts — instead of the field-filler looping on the lone control.
    if (termsGatePresent()) return { kind: 'AI_NAV', detail: 'terms / agreement gate' };

    // Conversational (chatbot) application: a recruiter/bot chats in a thread
    // and we reply in a message box. This is NOT an ordinary form — route it to
    // the AI navigator, which reads the transcript and answers the latest
    // question. Checked BEFORE the form decision so a chat with stray fields
    // still goes to the AI, not the field-filler.
    if (chatComposerPresent()) return { kind: 'AI_NAV', detail: 'conversational application (chat)' };

    const h = headingText();
    // Count real application fields whether or not they sit inside a <form>
    // tag (Lever/Greenhouse often don't wrap them). Excludes search/checkbox/
    // radio and a lone newsletter email.
    const meaningful = Array.from(document.querySelectorAll(
      'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]):not([type=checkbox]):not([type=radio]):not([type=image]):not([type=reset]), textarea, select'))
      .filter(visible);
    const hasFile = !!document.querySelector('input[type="file"]');
    // A single-field step counts as a form when it has a real forward control
    // (Next/Continue/Submit/Apply) — e.g. iCIMS "Enter Your Information" (just an
    // Email field + Next). Without this a legit 1-field apply step was treated as
    // "non-form" and the run deadlocked.
    const forwardBtn = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]'))
      .filter(visible)
      .some(b => /^(next|continue|save (and|&) continue|submit( application)?|apply( now| online)?|review|proceed|get started|start( application)?|i'?m interested)$/i.test(normText(b.innerText || b.value || '')));
    // Button-option groups (Ashby Yes/No chips) count as form content too, so a
    // page made ONLY of such questions + a Submit is still treated as a FORM.
    const hasOptBtnGroup = Array.from(document.querySelectorAll('button, [role="button"], [role="radio"]'))
      .filter(visible).some(b => /^(yes|no)$/i.test(normText(b.innerText || b.getAttribute('aria-label') || '')));
    const topHasForm = hasFile || meaningful.length >= 3 || (meaningful.length >= 1 && forwardBtn)
      || (hasOptBtnGroup && forwardBtn);

    // The form lives on THIS page → fill it (don't defer, don't hunt Apply).
    if (topHasForm) return { kind: 'FORM', detail: h || 'external application form' };

    // "Start Your Application" ENTRY CHOICE (Workday/iCIMS: Autofill with
    // Resume / Apply Manually / Use My Last Application) lives on the TOP
    // document. Handle it (→ click Apply Manually) instead of deferring to a
    // tracking / "Apply with LinkedIn" iframe and waiting forever.
    if (!isIframe && hasApplyEntryChoice()) {
      return { kind: 'EXTERNAL_LISTING', detail: 'apply entry choice' };
    }

    // No form here. If a real apply form is embedded in an iframe, the top
    // document defers to the in-iframe engine. (Only when THIS page has no form
    // of its own — otherwise the Lever direct form would be wrongly skipped.)
    if (!isIframe && indeedApplyIframePresent()) {
      return { kind: 'WAIT', detail: 'apply form open in iframe — deferring to it' };
    }
    // Inside an iframe the top document deferred to (iCIMS/Workday/etc. render
    // the apply form in an iframe): ANY real field means this frame IS the form
    // step — drive it. Only a genuinely empty frame (ad/tracking, e.g. youtube /
    // textrecruit) stays a non-form WAIT.
    if (isIframe) {
      if (meaningful.length >= 1 || hasFile) {
        return { kind: 'FORM', detail: h || 'application form (in iframe)' };
      }
      return { kind: 'WAIT', detail: 'non-form iframe' };
    }
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
    const byPlaceholder = good(el.getAttribute('placeholder')) || el.name || '';
    if (byPlaceholder) return byPlaceholder;
    // LAST RESORT: derive a label from a descriptive Workday/ATS id or
    // automation-id, e.g. "workExperience-6--roleDescription" → "work experience
    // role description". Strips indices and uuid-ish hex tokens.
    const wid = el.id || el.getAttribute('data-automation-id') || el.getAttribute('data-uxi-element-id') || '';
    if (wid) {
      const cleaned = wid
        .replace(/[-_]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2')
        .toLowerCase().split(/\s+/)
        .filter(w => w.length > 1 && !/^\d+$/.test(w) && !/^[0-9a-f]{6,}$/.test(w))
        .join(' ').trim();
      if (cleaned && /[a-z]{3,}/.test(cleaned)) return cleaned;
    }
    return '';
  }

  // A machine-readable HINT for a field, built from its DOM id / automation-id /
  // name — de-camelCased and de-dashed. Workday & other ATSes encode the real
  // meaning here even when the visible label is missing/ambiguous (e.g. the
  // Country Phone Code search box has id "phoneNumber--countryPhoneCode" but no
  // usable label). The answer engine matches on label + this hint so such fields
  // are identified reliably instead of falling through to a wrong/"N/A" answer.
  function fieldHint(el) {
    const raw = [
      el.id,
      el.getAttribute && el.getAttribute('data-automation-id'),
      el.getAttribute && el.getAttribute('data-uxi-element-id'),
      el.name,
    ].filter(Boolean).join(' ');
    return raw.replace(/[-_]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2')
      .toLowerCase().replace(/\s+/g, ' ').trim();
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

  // For a Workday-style multiselect SEARCH box (its own value is always empty),
  // the real selection is a set of pills in the same widget. Return their
  // labels so "is this field filled?" reflects the actual selection.
  function pillLabel(p) {
    return (p.getAttribute('title') || p.getAttribute('aria-label') || p.innerText || '')
      .replace(/,?\s*press delete.*/i, '').trim();
  }
  // Climb from the search box to the FIRST ancestor that actually contains
  // selectedItem pills — that's this widget's own selection container. Return
  // the pill labels (the true value; the search box's own value is empty).
  function multiselectPillEls(el) {
    let n = el;
    for (let i = 0; n && i < 8; i++, n = n.parentElement) {
      const pills = n.querySelectorAll ? Array.from(n.querySelectorAll('[data-automation-id="selectedItem"]')).filter(visible) : [];
      if (pills.length) return pills;
    }
    return [];
  }
  function multiselectSelectedValue(el) {
    return multiselectPillEls(el).map(pillLabel).filter(Boolean).join(', ');
  }
  // A Workday-style multiselect search input. Two variants exist: (1) the input
  // itself carries data-automation-id="searchBox" (phone-code, country); (2) a
  // plain <input placeholder="Search"> nested inside a multiSelectContainer /
  // multiselectInputContainer with a selectedItemList of pills (Skills, etc.).
  // Detect BOTH so multi-value fields (skills) route to fillMultiSelect.
  const isMultiselectSearch = (el) =>
    !!(el && el.getAttribute && (
      el.getAttribute('data-automation-id') === 'searchBox'
      || el.getAttribute('data-uxi-multiselect-id')
      || (el.tagName === 'INPUT' && el.closest
          && el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id="multiselectInputContainer"]'))
    ));

  // Is a button-option "chip" currently the selected one? Frameworks mark it
  // various ways (aria-pressed/checked/selected, data-selected, a "selected"/
  // "active" class, or a CSS-module hash like "_selected_ab12"). Best-effort.
  const isOptionButtonSelected = (b) => !!(b && b.getAttribute && (
    b.getAttribute('aria-pressed') === 'true'
    || b.getAttribute('aria-checked') === 'true'
    || b.getAttribute('aria-selected') === 'true'
    || b.getAttribute('data-selected') === 'true'
    || b.getAttribute('data-state') === 'checked' || b.getAttribute('data-state') === 'on'
    || /(^|[\s_-])(selected|active|checked|isselected|is-selected)([\s_-]|$)|_selected_|_active_|_checked_/i.test(b.className || '')
  ));
  // Which chip in a group is selected. Explicit markers first; else RELATIVE —
  // the selected chip carries a class token none of its siblings have (Ashby &
  // co add a hashed "_selected_x"/"_active_x" only to the chosen one). Falls back
  // to the visually-distinct one (darkest / filled background).
  function groupSelectedButton(btns) {
    const explicit = btns.find(isOptionButtonSelected);
    if (explicit) return explicit;
    const tokenSets = btns.map(b => (b.className || '').trim().split(/\s+/).filter(Boolean));
    const counts = {};
    tokenSets.flat().forEach(t => { counts[t] = (counts[t] || 0) + 1; });
    // A token unique to one chip whose name hints selection.
    for (let i = 0; i < btns.length; i++) {
      if (tokenSets[i].some(t => counts[t] === 1 && /sel|activ|check|fill|primary|dark|_on_|solid|current/i.test(t))) return btns[i];
    }
    // Fallback: any token unique to exactly one chip (state class), if only one
    // chip has a unique token (the others share the base classes).
    const withUniq = btns.filter((_, i) => tokenSets[i].some(t => counts[t] === 1));
    if (withUniq.length === 1) return withUniq[0];
    return null;
  }

  const cleanLabel = (s) => String(s || '').replace(/\s+/g, ' ').replace(/\s*\*\s*$/, '').trim();
  const isRequired = (el, label) =>
    el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(String(label || ''));

  // ── field scraping ───────────────────────────────────────────────────────

  const CONSENT_RE = /(agree|consent|certify|acknowledge|i understand|terms)/i;
  const OPTOUT_RE = /(save my answers|job alert|subscribe|send me|marketing|updates from indeed|newsletter)/i;

  // In-memory element registry (isolated world). We DON'T write data-bd-* marker
  // attributes onto the host page's DOM — those are the one thing a site could
  // MutationObserver to fingerprint this extension. IDs and group membership are
  // kept here instead and rebuilt fresh on each scrape/collect pass (field IDs
  // are only ever resolved within the same tick that produced them).
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
      // Workday date pickers expose separate Month/Year spinbutton inputs
      // inside a dateInputWrapper group. Don't scrape them individually — the
      // date-group pass below turns each wrapper into ONE 'date' field.
      if (el.closest('[data-automation-id="dateInputWrapper"]')) continue;

      const type = (el.tagName === 'TEXTAREA') ? 'textarea'
        : (el.tagName === 'SELECT') ? 'select'
        : (el.type || 'text').toLowerCase();

      if (type === 'radio' || type === 'checkbox') {
        // Work out the option group. Radios group by shared `name`. Checkboxes
        // group by shared `name` too, but Workday/EEO render race & ethnicity as
        // an UNNAMED checkbox group — so also group checkboxes by the nearest
        // fieldset / [role=group] container that holds 2+ boxes. Without this each
        // race checkbox became its own Yes/No field and nothing got ticked.
        let groupEls, groupKey, container = null;
        if (type === 'radio') {
          groupKey = el.name || el.closest('fieldset')?.outerHTML.slice(0, 60) || labelForInput(el);
          groupEls = el.name
            ? Array.from(scope.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)).filter(visible)
            : [el];
        } else {
          const named = el.name
            ? Array.from(scope.querySelectorAll(`input[name="${CSS.escape(el.name)}"][type="checkbox"]`)).filter(visible)
            : [];
          if (named.length > 1) { groupEls = named; groupKey = el.name; }
          else {
            container = el.closest('fieldset, [role="group"], [role="radiogroup"], [data-automation-id*="checkboxGroup" i], [data-automation-id*="formField" i]');
            const boxes = container ? Array.from(container.querySelectorAll('input[type="checkbox"]')).filter(visible) : [];
            if (container && boxes.length > 1) { groupEls = boxes; groupKey = container; }
            else { groupEls = [el]; groupKey = labelForInput(el); container = null; }
          }
        }

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
        // Group label: the radio/fieldset legend, else the container's label
        // (the demographic question heading above the checkbox list).
        const gl = cleanLabel(groupLabel(el) || (container ? labelForInput(container) : ''));
        const checked = groupEls.find(g => g.checked);
        let firstId = null;
        groupEls.forEach((g, i) => { const gid = regField(g, groupKey); if (i === 0) firstId = gid; });
        fields.push({
          id: firstId, groupName: el.name || null, type: 'radio',
          label: gl || options.join(' / '), hint: container ? fieldHint(container) : '', options,
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
          id: regField(el), type: 'select', label: lab, hint: fieldHint(el), options,
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
      // A multiselect search box has value="" even when a value is chosen — its
      // selection is a pill. Report the pill as currentValue so the verify loop
      // knows it's filled and doesn't re-type forever.
      const curVal = isMultiselectSearch(el) ? (multiselectSelectedValue(el) || '') : (el.value || '');
      fields.push({
        id: regField(el),
        type: type === 'tel' ? 'phone' : (type === 'number' ? 'number' : (el.tagName === 'TEXTAREA' ? 'textarea' : type)),
        label: lab, hint: fieldHint(el), options: null,
        required: isRequired(el, lab),
        currentValue: curVal,
        hasCountryCode,
        combobox: el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list',
      });
    }

    // Custom (non-native) dropdowns: a button/div that opens a listbox popup.
    // Options are unknown until opened — fillField opens it, optionally types to
    // filter, and picks the best match. Covers react-select, Workday
    // (aria-haspopup="listbox" buttons + data-automation-id), Ashby, etc.
    const ddBtns = Array.from(scope.querySelectorAll(
      'button[aria-haspopup="listbox"], [aria-haspopup="listbox"], button[role="combobox"],'
      + ' [role="combobox"], div[role="button"][aria-haspopup], [data-automation-id*="selectinput" i] button,'
      + ' [data-automation-id*="dropdown" i] button, [data-uxi-widget-type*="selectinput" i]'))
      .filter(el => visible(el) && !fieldRegistered(el)
        && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && el.tagName !== 'SELECT');
    for (const btn of ddBtns) {
      const lab = cleanLabel(labelForInput(btn));
      if (!lab) continue;
      const txt = (btn.innerText || '').trim();
      // Workday shows the current selection or "Select One"; a geo-default like
      // "Pakistan" is a real value (we must override it), while "Select One" is
      // a placeholder (empty). Treat known placeholders as empty.
      const placeholderish = !txt || /^(select( one)?|choose|--|pick|please|search)/i.test(txt) || normText(txt) === normText(lab);
      fields.push({
        id: regField(btn), type: 'listbox', label: lab, hint: fieldHint(btn), options: null,
        required: isRequired(btn, lab),
        currentValue: placeholderish ? '' : txt,
      });
    }

    // BUTTON-OPTION groups (Ashby, Greenhouse, custom ATSes): a question answered
    // by clicking one of several <button> "chips" (e.g. "Yes"/"No"), NOT a radio/
    // select. Detect containers holding 2+ sibling option-buttons whose text is a
    // short, non-navigation option. Grouped as a 'buttongroup' field.
    const NAV_BTN_RE = /^(submit|submit application|continue|save( and continue)?|next|back|previous|cancel|apply( now)?|add( another)?|remove|delete|edit|upload|browse|choose file|attach|sign ?in|log ?in|create( account)?|register|review|close|skip|got it|ok(ay)?|done|clear|search|show more|read more|learn more|\+ add)$/i;
    const optBtnCandidates = Array.from(scope.querySelectorAll('button, [role="button"], [role="radio"]'))
      .filter(b => visible(b) && !fieldRegistered(b) && b.tagName !== 'INPUT');
    const btnGroups = new Map();
    for (const b of optBtnCandidates) {
      const t = cleanLabel(b.innerText || b.getAttribute('aria-label') || '');
      if (!t || t.length > 40 || NAV_BTN_RE.test(t)) continue;
      const parent = b.parentElement;
      if (!parent || btnGroups.has(parent)) continue;
      const sibs = Array.from(parent.children)
        .filter(c => (c.tagName === 'BUTTON' || c.getAttribute('role') === 'button' || c.getAttribute('role') === 'radio') && visible(c));
      if (sibs.length < 2 || sibs.length > 6) continue;
      const texts = sibs.map(s => cleanLabel(s.innerText || s.getAttribute('aria-label') || ''));
      if (!texts.every(x => x && x.length <= 40 && !NAV_BTN_RE.test(x))) continue;
      if (new Set(texts.map(normText)).size < 2) continue; // need distinct options
      btnGroups.set(parent, sibs);
    }
    for (const [container, btns] of btnGroups) {
      if (seenGroups.has(container)) continue;
      seenGroups.add(container);
      let firstId = null;
      btns.forEach((b, i) => { const id = regField(b, container); if (i === 0) firstId = id; });
      const options = btns.map(b => cleanLabel(b.innerText || b.getAttribute('aria-label') || ''));
      const gl = cleanLabel(groupLabel(btns[0]) || labelForInput(container));
      const selected = groupSelectedButton(btns);
      fields.push({
        id: firstId, groupName: null, type: 'buttongroup',
        label: gl || options.join(' / '), options,
        required: isRequired(container, gl) || /\*/.test((container.closest('[class*="field" i], fieldset, [role="group"]') || container).innerText || ''),
        currentValue: selected ? cleanLabel(selected.innerText || selected.getAttribute('aria-label') || '') : '',
      });
    }

    // Workday segmented date pickers: one 'date' field per dateInputWrapper.
    const dateWraps = Array.from(scope.querySelectorAll('[data-automation-id="dateInputWrapper"]'))
      .filter(w => visible(w) && !fieldRegistered(w));
    for (const w of dateWraps) {
      let lab = cleanLabel(labelForInput(w));
      if (!lab || isPlaceholderLabel(lab)) {
        const idl = (w.id || '').toLowerCase();
        lab = /start/.test(idl) ? 'Start Date' : /end/.test(idl) ? 'End Date' : (lab || 'Date');
      }
      // Tag work-experience dates (Workday id "workExperience-N--startDate") so
      // the "availability = Immediately/today" rule doesn't hijack an EMPLOYMENT
      // date — it must come from the résumé instead.
      const idl2 = `${w.id || ''} ${w.getAttribute('data-automation-id') || ''}`.toLowerCase();
      if (/work.?experience|employment|prior|previous/.test(idl2) && !/work experience|employment/.test(lab.toLowerCase())) {
        lab = `Work Experience ${lab}`;
      }
      const mDisp = (w.querySelector('[data-automation-id="dateSectionMonth-display"]') || {}).innerText || '';
      const yDisp = (w.querySelector('[data-automation-id="dateSectionYear-display"]') || {}).innerText || '';
      const mIn = w.querySelector('[data-automation-id="dateSectionMonth-input"]');
      const yIn = w.querySelector('[data-automation-id="dateSectionYear-input"]');
      // "filled" if the section inputs hold digits (displays show MM/YYYY when empty).
      const filled = (/\d/.test((mIn && mIn.value) || '') && /\d/.test((yIn && yIn.value) || ''))
        || (/\d/.test(mDisp) && /\d/.test(yDisp));
      fields.push({
        id: regField(w), type: 'date', label: lab, hint: fieldHint(w), options: null,
        required: isRequired(w, lab) || /\bERROR\b/.test(w.getAttribute('aria-labelledby') || ''),
        currentValue: filled ? `${(mIn && mIn.value) || mDisp}/${(yIn && yIn.value) || yDisp}` : '',
      });
    }
    return fields;
  }

  // ── filling ──────────────────────────────────────────────────────────────

  async function fillField(field, answer) {
    const el = findByFieldId(field.id);
    if (!el) return false;

    if (field.type === 'date') {
      const r = await fillDateField(el, answer, field);
      return r === null ? false : r;
    }

    // Button-option group (Ashby Yes/No chips, etc.): click the button whose text
    // matches the answer. Don't re-click an already-selected chip (many toggle
    // OFF on a second click).
    if (field.type === 'buttongroup') {
      const gk = groupKeyOf(el);
      const btns = gk != null ? groupMembers(gk) : [el];
      const want = normText(answer);
      const btnText = (b) => normText(b.innerText || b.getAttribute('aria-label') || '');
      let target = btns.find(b => btnText(b) === want)
        || btns.find(b => btnText(b).includes(want) && want.length > 1)
        || btns.find(b => want.includes(btnText(b)) && btnText(b).length > 1)
        // Yes/No answers phrased as full sentences → map to the Yes or No chip.
        || (/^(yes|y|true)\b/.test(want) ? btns.find(b => /^(yes|y)$/.test(btnText(b))) : null)
        || (/^(no|n|false)\b/.test(want) ? btns.find(b => /^(no|n)$/.test(btnText(b))) : null);
      if (!target) return false;
      if (groupSelectedButton(btns) === target) return true; // already chosen — don't toggle off
      dispatchRealClick(target);
      await sleep(250);
      return true;
    }

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
      const want = normText(answer);
      let target = groupEls.find(g => normText(optionLabel(g)) === want)
        || groupEls.find(g => normText(optionLabel(g)).includes(want))
        || groupEls.find(g => want.includes(normText(optionLabel(g))) && normText(optionLabel(g)).length > 2);
      // Race/ethnicity safeguard: tick "Asian" (the operator policy), never
      // "Caucasian"/"White", even if the answer text is phrased differently.
      if (!target && /\basian\b/.test(want)) {
        target = groupEls.find(g => /\basian\b/.test(normText(optionLabel(g))) && !/caucasian/.test(normText(optionLabel(g))));
      }
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
      const want = normText(answer);
      const opt = Array.from(el.options).find(o => normText(o.text) === want)
        || Array.from(el.options).find(o => normText(o.text).includes(want))
        || Array.from(el.options).find(o => want.includes(normText(o.text)) && normText(o.text).length > 2)
        // race safeguard: pick any "Asian" option, never Caucasian.
        || (/\basian\b/.test(want) ? Array.from(el.options).find(o => /\basian\b/.test(normText(o.text)) && !/caucasian/.test(normText(o.text))) : null);
      if (!opt) return false;
      // React-controlled selects (Lever EEO): use the native prototype value
      // setter so React's value tracker sees the change, then fire change.
      el.focus();
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
      if (setter) setter.call(el, opt.value); else el.value = opt.value;
      opt.selected = true;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      await sleep(200);
      // Verify it committed (React can revert). If not, we return false so the
      // caller can leave it / a retry can try the open-and-click widget path.
      const committed = normText((el.options[el.selectedIndex] || {}).text || '').includes(want)
        || (/\basian\b/.test(want) && /\basian\b/.test(normText((el.options[el.selectedIndex] || {}).text || '')));
      return committed;
    }

    // custom dropdown (button/div + menu): open, optionally type-to-filter
    // (Workday/searchable lists), read options, pick best match. Commits on
    // mousedown (a plain click can miss). Used by Lever/Greenhouse EEO fields
    // and Workday's Country / How-did-you-hear / Phone-type dropdowns.
    // Button/div-triggered dropdown → unified searchable-select routine
    // (open → type-to-filter → pick option). Handles Workday, react-select, EEO.
    if (field.type === 'listbox') {
      return await fillSearchableSelect(el, answer);
    }

    // A combobox that is NOT a free-text location autocomplete is a SEARCHABLE
    // SELECT (Workday Country / phone-code / how-did-you-hear): type a short
    // filter, then pick the matching option from the fixed list. Different from
    // a Google-Places location field, which has no fixed option list.
    const isLocationLike = /\b(location|city|town|address|where)\b/i.test(field.label || '');
    if (field.combobox && !isLocationLike) {
      return await fillSearchableSelect(el, answer);
    }
    // Only route to the Google-Places-style autocomplete when the element is a
    // REAL autocomplete widget (combobox role / aria-autocomplete). Workday's
    // Address Line 1 / City are PLAIN text inputs whose label merely contains
    // "address"/"city" — sending them through fillAutocomplete made it wait 4s for
    // suggestions that never appear and could leave a stale value; type them
    // normally instead (falls through to the plain-text path below).
    const realAutocomplete = field.combobox
      || el.getAttribute('role') === 'combobox'
      || !!el.getAttribute('aria-autocomplete');
    if (realAutocomplete) {
      return await fillAutocomplete(el, answer);
    }

    // Date field (Workday segmented picker, or an MM/YYYY text field).
    if (/^\d{1,2}[/\-.]\d{4}$/.test(String(answer)) || /^present$/i.test(String(answer)) || field.type === 'date'
        || (/\b(from|to|start date|end date|\bdate\b|month.*year)\b/i.test(field.label || '') && /\b\d{4}\b/.test(String(answer)))) {
      const r = await fillDateField(el, answer, field);
      if (r !== null) return r;
    }

    // Workday MULTISELECT (Skills, preferred locations, languages) with several
    // comma-separated values → add each as its own pill. A single searchable-
    // select would type the whole "Python, SQL, AWS" string and match nothing.
    if (isMultiselectSearch(el) && String(answer).includes(',')) {
      return await fillMultiSelect(el, String(answer).split(',').map(s => s.trim()).filter(Boolean), field.label || '');
    }

    // Known dropdown-type fields sometimes render as a plain text input (a
    // search box) — drive them as a searchable select, never type raw text
    // (which leaves the field unselected → "required" error).
    const KNOWN_SELECT_RE = /\b(country|state|province|phone code|device type|how did you hear|gender|race|ethnicity|veteran|disability|language|nationality|citizenship|pronoun|marital|skill)\b/i;
    if ((isMultiselectSearch(el) || KNOWN_SELECT_RE.test(`${field.label || ''} ${field.hint || ''}`)) && el.tagName === 'INPUT' && (el.type || 'text') === 'text') {
      return await fillSearchableSelect(el, answer, field.label || '');
    }

    // A <textarea> or a LONG value (role description, cover-letter blurb) →
    // set it instantly. Character-by-character would take ~1 minute for a
    // 700-char description and the verify loop would re-fire before it finished.
    if (el.tagName === 'TEXTAREA' || String(answer).length > 60) {
      setNativeValue(el, answer);
      // Some React fields also want a keyup to mark themselves "dirty".
      el.dispatchEvent(new KeyboardEvent('keyup', { key: ' ', bubbles: true }));
      return (el.value || '').length > 0;
    }
    // Short text input → type it character-by-character (human pace), which also
    // commits on validated ATS fields that ignore a programmatic value set.
    await typeInto(el, answer);
    return el.value === answer || el.value.length > 0;
  }

  const SUGGESTION_OPTION_SEL = '.pac-item, [role="option"], li, [class*="option" i], [class*="item" i], [class*="result" i]';

  // Locate a suggestion dropdown that appeared for an autocomplete input.
  function findSuggestionList() {
    const sels = [
      '.pac-container',   // Google Places (Lever/Greenhouse) — appended to <body>
      '[role="listbox"]', '[role="menu"]',
      '[class*="suggestion" i]', '[class*="autocomplete" i]', '[class*="typeahead" i]',
      '[class*="results" i]', '[class*="dropdown" i]', '[class*="menu" i]', '[class*="options" i]',
      'ul li[role="option"]',
    ];
    for (const sel of sels) {
      const els = Array.from(document.querySelectorAll(sel)).filter(visible);
      for (const e of els) {
        // must contain clickable option-like children and be reasonably sized
        const opts = e.querySelectorAll(SUGGESTION_OPTION_SEL);
        if (opts.length && e.getBoundingClientRect().height > 15) return e;
      }
    }
    return null;
  }

  async function fillAutocomplete(el, answer) {
    const proto = HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    const setVal = (v) => { if (setter) setter.call(el, v); else el.value = v; };
    // For "City, ST" type just the city — autocompletes match on the city.
    const typed = String(answer).split(',')[0].trim() || String(answer);
    // Already committed (value contains the city) → don't re-type (re-typing an
    // autocomplete breaks the committed selection). This stops the fill loop.
    if (normText(el.value).includes(normText(typed)) && el.value.trim()) return true;

    el.focus();
    el.click();
    setVal('');
    el.dispatchEvent(new Event('input', { bubbles: true }));
    // Type each character with a full key-event sequence so the widget's
    // debounced suggestion fetch actually fires.
    for (const ch of typed) {
      setVal(el.value + ch);
      el.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
      await sleep(80);
    }

    // Wait (patiently) for suggestions to appear AND settle.
    let list = null;
    for (let i = 0; i < 20 && !list; i++) { await sleep(200); list = findSuggestionList(); }
    if (!list) return (el.value || '').trim().length > 0;
    // let the list finish rendering its items
    await sleep(500);

    const before = el.value;

    // METHOD 1 (most reliable for Google Places / Lever): keyboard select.
    // ArrowDown highlights the first suggestion, Enter commits it. Include the
    // legacy keyCode/which — Google's widget still checks them.
    pressKey(el, 'ArrowDown', 40);
    await sleep(400);
    pressKey(el, 'Enter', 13);
    await sleep(600);
    if ((el.value || '').trim() && normText(el.value) !== normText(before)) return true;
    if ((el.value || '').trim() && !/^[a-z ]+$/.test(normText(el.value))) return true; // got a formatted address

    // METHOD 2: mousedown the matching item (mousedown, before the blur tears
    // the container down).
    const opts = Array.from(list.querySelectorAll(SUGGESTION_OPTION_SEL)).filter(visible);
    const want = normText(answer);
    const match = opts.find(o => normText(o.innerText).includes(want.split(',')[0]))
      || opts.find(o => normText(o.innerText).includes(want)) || opts[0];
    if (match) {
      for (const type of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        match.dispatchEvent(type.startsWith('pointer')
          ? new PointerEvent(type, { bubbles: true, cancelable: true, view: window })
          : new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
        await sleep(40);
      }
      await sleep(500);
    }
    return (el.value || '').trim().length > 0;
  }

  // Fill a date value. Workday uses a SEGMENTED date widget (separate Month /
  // Day / Year spinbutton inputs), not a single "MM/YYYY" text field — typing
  // the whole string fails. Locate the month & year sub-inputs and type into
  // each. Returns true/false, or null if `answer` isn't a date we can place.
  async function fillDateField(el, answer, field) {
    const str = String(answer).trim();
    if (/present|current|now/i.test(str)) return true; // handled by "currently work here"
    const m = str.match(/(\d{1,2})\D+(\d{4})/) || str.match(/^(\d{4})$/) && [null, '01', RegExp.$1];
    if (!m) return null;
    const mm = String(m[1]).padStart(2, '0');
    const yyyy = m[2];

    // el is the dateInputWrapper (or an ancestor). Find the segmented Month/
    // Year spinbutton inputs (Workday: data-automation-id="dateSectionMonth/
    // Year-input", role=spinbutton).
    const wrap = el.matches('[data-automation-id="dateInputWrapper"]') ? el
      : (el.closest('[data-automation-id="dateInputWrapper"]') || el);
    const monthIn = wrap.querySelector('[data-automation-id="dateSectionMonth-input"], input[aria-label*="month" i]');
    const yearIn = wrap.querySelector('[data-automation-id="dateSectionYear-input"], input[aria-label*="year" i]');
    const dayIn = wrap.querySelector('[data-automation-id="dateSectionDay-input"], input[aria-label*="day" i]');
    if (monthIn && yearIn) {
      // Workday date sections are React-controlled role="spinbutton" inputs that
      // IGNORE synthetic key events, and typing char-by-char makes React
      // re-normalise each partial value into garbage ("03" → "7"). Set the FULL
      // value in ONE shot via the React-tracked native setter (clear, then set).
      const setSection = (input, val) => { setNativeValue(input, ''); setNativeValue(input, String(val)); };
      const mmNum = String(parseInt(mm, 10)); // numeric spinbutton wants "3", not "03"
      // Clear ALL sections first — setting the year after an un-cleared month can
      // wipe the month on some Workday renders.
      setSection(monthIn, ''); if (dayIn) setSection(dayIn, ''); setSection(yearIn, '');
      await sleep(150);
      // Set YEAR first, MONTH last — setting the year re-renders and can wipe an
      // already-set month, so month must be written last (and re-checked).
      setSection(yearIn, yyyy); await sleep(200);
      if (dayIn) { setSection(dayIn, '1'); await sleep(150); }
      setSection(monthIn, mmNum); await sleep(200);
      monthIn.blur && monthIn.blur();
      await sleep(200);
      if (!/\d/.test(monthIn.value || '')) { setSection(monthIn, mmNum); await sleep(200); }
      if (!/\d/.test(yearIn.value || '')) { setSection(yearIn, yyyy); setSection(monthIn, mmNum); await sleep(200); }
      return /\d/.test(monthIn.value || '') && /\d/.test(yearIn.value || '');
    }
    // Single-input date field: type the string as-is (MM/YYYY).
    if (el.tagName === 'INPUT') {
      await typeInto(el, `${mm}/${yyyy}`);
      return (el.value || '').length > 0;
    }
    return null;
  }

  // Type digits into a Workday date-section spinbutton. These are driven by
  // real key events (they auto-advance); we fire keydown/keypress/keyup per
  // digit and also set the value for inputs that read it.
  async function typeDigits(input, digits) {
    input.focus();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(input, ''); else input.value = '';
    for (const ch of String(digits)) {
      const code = 48 + Number(ch);
      for (const t of ['keydown', 'keypress']) {
        const ev = new KeyboardEvent(t, { key: ch, code: `Digit${ch}`, bubbles: true, cancelable: true });
        try { Object.defineProperty(ev, 'keyCode', { get: () => code }); Object.defineProperty(ev, 'which', { get: () => code }); } catch { /* */ }
        input.dispatchEvent(ev);
      }
      if (setter) setter.call(input, (input.value || '') + ch); else input.value += ch;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      const up = new KeyboardEvent('keyup', { key: ch, code: `Digit${ch}`, bubbles: true });
      input.dispatchEvent(up);
      await sleep(70);
    }
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // Type a value into an element with real per-character key events.
  async function typeInto(el, text) {
    el.focus();
    // Use the correct prototype's value setter — HTMLInputElement's setter
    // THROWS ("Illegal invocation") on a <textarea>, which silently left role
    // descriptions empty. Textareas use HTMLTextAreaElement.prototype.
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    const set = (v) => { if (setter) setter.call(el, v); else el.value = v; };
    set('');
    el.dispatchEvent(new Event('input', { bubbles: true }));
    for (const ch of String(text)) {
      set((el.value || '') + ch);
      el.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
      await sleep(60);
    }
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // Collect visible option elements from an open dropdown popup anywhere on the
  // page (Workday renders in a portal; options may be role=option, radios, li…).
  // Text of an option element (Workday puts the real label in
  // data-automation-label; others use innerText/aria-label).
  const optText = (e) => normText(e.getAttribute('data-automation-label') || e.innerText || e.getAttribute('aria-label') || '');

  // The scrollable container holding dropdown options (for virtualised lists).
  function findScrollableOptionContainer() {
    // Workday renders options in a portal; the scrollable box is a known
    // container or an ancestor of the promptOption.
    const wd = Array.from(document.querySelectorAll(
      '[data-automation-id="activeListContainer"], [data-automation-id="promptScrollableList"],'
      + ' ul[data-automation-id="promptOptions"], [data-automation-id*="promptList" i]'))
      .filter(visible).find(e => e.scrollHeight > e.clientHeight + 8);
    if (wd) return wd;
    const opt = document.querySelector('[data-automation-id="promptOption"], [role="option"]');
    let n = opt;
    for (let i = 0; n && i < 10; i++, n = n.parentElement) {
      if (n.scrollHeight > n.clientHeight + 10 && n.clientHeight > 30) return n;
    }
    // fallback: any visible role=listbox/menu/ul that scrolls
    return Array.from(document.querySelectorAll('[role="listbox"], [class*="menu" i], [class*="options" i], ul'))
      .filter(visible).find(e => e.scrollHeight > e.clientHeight + 20) || null;
  }

  // The scrollable box holding a long block of text (Terms & Conditions,
  // agreements). Terms are often in their own inner scroll area that must be
  // scrolled to the end before the Accept/Continue button enables.
  function findScrollableTextContainer() {
    const cands = Array.from(document.querySelectorAll(
      '[class*="terms" i], [class*="agreement" i], [class*="scroll" i], [class*="policy" i],'
      + ' [role="document"], [role="article"], .modal-body, [class*="content" i]'))
      .filter(visible)
      .filter(e => e.scrollHeight > e.clientHeight + 40 && e.clientHeight > 120);
    // Prefer the tallest scrollable content area (most likely the T&C body).
    cands.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
    return cands[0] || null;
  }

  // Scroll all the way to the bottom in ONE action (not one viewport per tick).
  // Steps incrementally — firing scroll events so "scroll to the end to enable
  // Accept" widgets unlock — through the terms container (if any) AND the window
  // (a page-level Accept button often sits below the container). Returns true if
  // it reached the bottom of whatever it scrolled.
  async function scrollToBottom(box) {
    const scrollOne = async (el) => {
      const isWin = el === window;
      let last = -1, stall = 0;
      for (let i = 0; i < 60; i++) {
        const pos = isWin ? window.scrollY : el.scrollTop;
        const atBottom = isWin
          ? (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4)
          : (el.scrollTop + el.clientHeight >= el.scrollHeight - 4);
        if (atBottom) return true;
        // No movement for a few tries (content not actually scrollable) → stop.
        if (Math.abs(pos - last) < 2) { if (++stall >= 3) return atBottom; }
        else stall = 0;
        last = pos;
        if (isWin) window.scrollBy(0, Math.round(window.innerHeight * 0.9));
        else el.scrollBy(0, Math.round(el.clientHeight * 0.9));
        await sleep(110);
      }
      return false;
    };
    let reached = false;
    if (box) reached = await scrollOne(box);
    reached = (await scrollOne(window)) || reached;
    return reached;
  }

  function collectOpenOptions() {
    const els = Array.from(document.querySelectorAll(
      '[data-automation-id="promptOption"], .pac-item, [role="option"], [role="radio"],'
      + ' [role="listbox"] li, [class*="menu" i] [class*="option" i], [class*="dropdown" i] li,'
      + ' [class*="results" i] li, li[role="option"]')).filter(visible);
    // De-dupe and keep those with text
    const seen = new Set(); const out = [];
    for (const e of els) {
      const t = optText(e);
      if (!t || seen.has(t)) continue;
      seen.add(t); out.push(e);
    }
    return out;
  }

  // Fill a SEARCHABLE SELECT (type-to-filter dropdown): Workday Country /
  // phone-code / how-did-you-hear, react-select, etc. `el` may be the search
  // input itself (combobox) or the trigger button. Types a short filter term,
  // waits for the list to narrow, then selects the matching option.
  async function fillSearchableSelect(el, answer, label = '') {
    const want = normText(answer);
    // Short, distinctive filter — strip "(+1)"/trailing parens and commas.
    const filterTerm = String(answer).replace(/\(.*?\)/g, '').split(',')[0].trim() || String(answer);
    // PHONE / DIAL CODE dropdowns (Workday "Country Phone Code") must NOT be
    // typed into — typing destabilises the virtualised list and the selection
    // (USA) gets selected then dropped, looping. For these we ONLY open the
    // dropdown and SCROLL through the options to find and click the right one.
    // Element id/automation-id hint: Workday's phone-code box has no usable label
    // (id "phoneNumber--countryPhoneCode"), so key scroll-only off the id too —
    // this guarantees we NEVER type into a phone-code box even if the answer text
    // arrives without a "(+1)" marker.
    const idHint = ((el.id || '') + ' ' + ((el.getAttribute && el.getAttribute('data-automation-id')) || '')).replace(/[-_]/g, '').toLowerCase();
    const scrollOnly = /\bphone code\b|\bdial(ing)? code\b|country code|\bcountry.*code\b|calling code/i.test(label)
      || /\(\+?\d{1,4}\)/.test(String(answer)) // answer like "…(+1)"
      || /countryphonecode|phonecode|phonecountry|dialcode|callingcode/.test(idHint);

    el.scrollIntoView({ block: 'center' });

    // Already selected? (a multiselect pill matching the answer exists) → don't
    // re-open/re-type. This stops the "types United States again and again" loop
    // on Workday's search-box multiselect, whose input value stays empty.
    if (isMultiselectSearch(el)) {
      const cur = normText(multiselectSelectedValue(el));
      if (cur && (cur.includes(want) || cur.includes(normText(filterTerm)))) return true;
    }

    // Open it (works whether el is a button or an input).
    dispatchRealClick(el);
    await sleep(400);

    // Locate the search box (only used for TYPING — skipped for scrollOnly).
    let searchInput = (el.tagName === 'INPUT') ? el : null;
    if (!searchInput && document.activeElement && document.activeElement.tagName === 'INPUT' && document.activeElement !== el) {
      searchInput = document.activeElement;
    }
    if (!searchInput) {
      searchInput = Array.from(document.querySelectorAll(
        'input[type="text"], input[type="search"], input[role="combobox"], input[aria-autocomplete], input[data-automation-id], [data-automation-id*="search" i] input'))
        .filter(visible).find(i => i.getAttribute('aria-autocomplete') || i.getAttribute('role') === 'combobox'
          || /search|filter|type/i.test((i.getAttribute('aria-label') || '') + (i.placeholder || '') + (i.getAttribute('data-automation-id') || '')));
    }
    // Type the filter ONLY for normal searchable selects — never for phone codes.
    if (searchInput && !scrollOnly) { await typeInto(searchInput, filterTerm); await sleep(700); }
    // scrollOnly: clear any pre-existing filter text (a Workday geo-default or a
    // stale value like "N/A") so the option list isn't narrowed to nothing before
    // we scroll to find the right country.
    if (scrollOnly && searchInput && searchInput.value) {
      const st = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      if (st) st.call(searchInput, ''); else searchInput.value = '';
      searchInput.dispatchEvent(new Event('input', { bubbles: true }));
      await sleep(400);
    }

    // Find and select the BEST option (precise, not just "contains").
    let opts = collectOpenOptions();
    let match = bestOption(opts, want, filterTerm);
    for (let i = 0; i < 6 && !match; i++) { await sleep(250); opts = collectOpenOptions(); match = bestOption(opts, want, filterTerm); }

    // Still not found? Workday's lists are VIRTUALISED and may not filter on
    // type (e.g. Country Phone Code) — scroll the option container to render
    // more rows until the target appears. Scroll the container if we can find
    // one; ALSO scroll the last rendered option into view (this forces the
    // virtualiser to mount the next batch even when the scroll box is elusive).
    if (!match) {
      let lastTop = -1, stall = 0;
      for (let i = 0; i < 90 && !match; i++) {
        const host = findScrollableOptionContainer();
        if (host) {
          host.scrollTop += Math.max(160, host.clientHeight * 0.85);
          if (Math.abs(host.scrollTop - lastTop) < 2) stall++; else { stall = 0; lastTop = host.scrollTop; }
        } else {
          stall++;
        }
        // Nudge the virtualiser: bring the LAST rendered option into view.
        const rendered = collectOpenOptions();
        const lastEl = rendered[rendered.length - 1];
        if (lastEl) lastEl.scrollIntoView({ block: 'end' });
        await sleep(120);
        opts = collectOpenOptions();
        match = bestOption(opts, want, filterTerm);
        if (stall >= 4) break; // genuinely at the bottom / not scrolling
      }
    }

    // Last resort for phone code if scrolling never surfaced it: type a SHORT
    // country filter once (some Workday tenants DO filter on type). This is
    // gated so a normal run stays scroll-only as intended.
    if (!match && scrollOnly && searchInput) {
      await typeInto(searchInput, 'United States');
      await sleep(800);
      opts = collectOpenOptions();
      match = bestOption(opts, want, filterTerm);
      for (let i = 0; i < 6 && !match; i++) { await sleep(250); opts = collectOpenOptions(); match = bestOption(opts, want, filterTerm); }
    }

    if (match) {
      // Did it commit? (a matching pill/value appeared).
      const committed = () => {
        if (isMultiselectSearch(el)) { const c = normText(multiselectSelectedValue(el)); return c && (c.includes(want) || c.includes(normText(filterTerm))); }
        return normText(el.value || el.innerText || '').includes(normText(filterTerm));
      };
      // Workday virtualises the list and recycles DOM nodes, so a single
      // synthetic click on the row we found while scrolling frequently no-ops
      // ("USA highlighted but not selected"). Try several commit strategies IN
      // ORDER, RE-QUERYING the live option before each attempt, and stop the
      // instant a pill appears. Native .click() (which fires React's onClick) and
      // keyboard Enter on the highlighted row are the reliable ones.
      const commitAttempts = [
        // 0: native click on the option row — triggers React onClick directly.
        async (t) => { try { t.click(); } catch { /* */ } },
        // 1: click the inner radio/checkbox if the row has one.
        async (t) => { const r = t.querySelector && t.querySelector('input[type="radio"], input[type="checkbox"]'); if (r) { try { r.click(); } catch { /* */ } } },
        // 2: full synthetic pointer+mouse sequence on the row.
        async (t) => {
          try { t.focus && t.focus(); } catch { /* */ }
          for (const type of ['pointerover', 'mouseover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
            t.dispatchEvent(type.startsWith('pointer')
              ? new PointerEvent(type, { bubbles: true, cancelable: true, view: window })
              : new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
            await sleep(20);
          }
        },
        // 3: keyboard Enter — Workday commits the currently highlighted row.
        async () => { const kb = searchInput || el; try { kb.focus && kb.focus(); } catch { /* */ } pressKey(kb, 'Enter', 13); },
        // 4: ArrowDown to move the highlight onto the match, then Enter.
        async () => { const kb = searchInput || el; pressKey(kb, 'ArrowDown', 40); await sleep(150); pressKey(kb, 'Enter', 13); },
      ];
      for (let a = 0; a < commitAttempts.length; a++) {
        // Re-query the live option each attempt — the node from the scroll pass
        // may have been detached/recycled, which is exactly why clicks no-op.
        const live = bestOption(collectOpenOptions(), want, filterTerm) || match;
        const target = (live.closest && live.closest('[data-automation-id="promptOption"], li[data-automation-id="menuItem"], [role="option"], label')) || live;
        try { target.scrollIntoView({ block: 'center' }); } catch { /* */ }
        await sleep(120);
        await commitAttempts[a](target);
        await sleep(400);
        if (committed()) break;
      }
      // AFTER a successful select, clean up stale pills on a MULTI-select
      // (Workday Country Phone Code keeps a removable pill per value) — but
      // ONLY if our correct value is already present, and NEVER remove the
      // matching pill. This avoids the loop where the correct pill got deleted.
      await sleep(200);
      const pills = multiselectPillEls(el);
      const matches = (p) => { const l = normText(pillLabel(p)); return l.includes(want) || l.includes(normText(filterTerm)); };
      if (pills.some(matches) && pills.length > 1) {
        for (const pill of pills) {
          if (matches(pill)) continue; // keep the correct one
          const del = pill.querySelector('[data-automation-id="DELETE_charm"], [aria-label*="delete" i], [aria-label*="clear" i]');
          if (del) { dispatchRealClick(del); await sleep(200); }
        }
      }
      // Clear any leftover text in the search box so it doesn't look "unfilled"
      // or re-trigger a search on the next tick.
      if (searchInput && searchInput.value) {
        const st = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (st) st.call(searchInput, ''); else searchInput.value = '';
        searchInput.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return true;
    }
    // Keyboard commit ONLY when the filter narrowed to a single option — then
    // ArrowDown+Enter is safe. Never blindly pick the first of many (that gave
    // "Career Fair" / "Minor Outlying Islands").
    if (opts.length === 1) {
      const kb = searchInput || el;
      pressKey(kb, 'ArrowDown', 40); await sleep(250);
      pressKey(kb, 'Enter', 13); await sleep(400);
      return true;
    }
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    return false; // leave unselected rather than commit the wrong option
  }

  // Fill a MULTI-VALUE searchable select (Workday Skills, preferred locations,
  // languages…): add EACH value as its own pill. A single fillSearchableSelect
  // would type the whole comma-joined string ("Python, SQL, AWS") and match
  // nothing → loop. For each value: open, type it, pick the best option, commit,
  // clear the box, move to the next. Never removes already-added pills.
  async function fillMultiSelect(el, values, label = '') {
    const pillHas = (want) => multiselectPillEls(el).some(p => normText(pillLabel(p)).includes(want));
    let added = 0;
    // Cap at a sensible number of skills so we don't spend forever on a long list.
    for (const raw of values.slice(0, 12)) {
      const v = String(raw).trim();
      if (!v) continue;
      const want = normText(v);
      if (pillHas(want)) { added++; continue; } // already selected
      dispatchRealClick(el);
      await sleep(300);
      let si = (el.tagName === 'INPUT') ? el
        : (document.activeElement && document.activeElement.tagName === 'INPUT' ? document.activeElement : el);
      await typeInto(si, v);
      await sleep(650);
      let opts = collectOpenOptions();
      let match = bestOption(opts, want, v);
      for (let i = 0; i < 6 && !match; i++) { await sleep(220); opts = collectOpenOptions(); match = bestOption(opts, want, v); }
      if (match) {
        const target = (match.closest && match.closest('[data-automation-id="promptOption"], li[data-automation-id="menuItem"], [role="option"], label')) || match;
        try { target.scrollIntoView({ block: 'center' }); } catch { /* */ }
        try { target.click(); } catch { /* */ }
        await sleep(300);
        if (!pillHas(want)) { try { si.focus(); } catch { /* */ } pressKey(si, 'Enter', 13); await sleep(300); }
        if (pillHas(want)) added++;
      }
      // Clear the search box before the next skill (leftover text filters wrongly).
      if (si && si.value) {
        const st = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (st) st.call(si, ''); else si.value = '';
        si.dispatchEvent(new Event('input', { bubbles: true }));
      }
      await sleep(200);
    }
    // Success if we added at least one, or something was already there — so the
    // verify loop treats the field as filled and does NOT re-run forever.
    return added > 0 || multiselectPillEls(el).length > 0;
  }

  // Choose the best-matching option, avoiding decoys like "United States Minor
  // Outlying Islands" or picking a random first item.
  function bestOption(opts, want, filterTerm) {
    if (!opts.length) return null;
    const N = o => normText(o.getAttribute('data-automation-label') || o.innerText || o.getAttribute('aria-label') || '');
    // 1. exact
    let m = opts.find(o => N(o) === want); if (m) return m;
    // 2. USA — match ONLY the real country. Decoys that also carry "(+1)" or the
    //    word "america" must be rejected: "American Samoa (+1)" (american→america!),
    //    "United States Minor Outlying Islands (+1)", "US Virgin Islands".
    if (/united states|u\.?s\.?a?\b|america/.test(want)) {
      const isUSA = (s) =>
        (/^united states of america\b/.test(s)
          || (/^united states\b/.test(s) && !/minor outlying|virgin islands/.test(s))
          || /^usa\b/.test(s)
          || /^u\.s\.a?\.?$/.test(s));
      m = opts.find(o => isUSA(N(o)));
      if (m) return m;
      // Do NOT loosely match "america"/"united states" contained anywhere — that
      // grabs American Samoa / Minor Outlying Islands. Return null so a virtualised
      // list keeps SCROLLING until the real United States row renders, rather than
      // committing a wrong "+1" decoy.
      return null;
    }
    // 3. LinkedIn / how-did-you-hear synonyms
    if (want.includes('linkedin')) {
      m = opts.find(o => N(o).includes('linkedin'));
      if (m) return m;
      for (const alt of ['social media', 'social', 'internet', 'online', 'job board', 'job site', 'company website', 'website', 'search engine', 'referral', 'other']) {
        m = opts.find(o => N(o).includes(alt)); if (m) return m;
      }
    }
    // 4. race → Asian, never Caucasian
    if (/\basian\b/.test(want)) {
      m = opts.find(o => /\basian\b/.test(N(o)) && !/caucasian/.test(N(o))); if (m) return m;
    }
    // 4b. DEGREE / education level — map résumé abbreviations to the option set
    // (Workday offers "Associates / Bachelors / Masters / Doctorate / High
    // School / GED"). Only when the options actually look like a degree list, so
    // this never mis-fires on other dropdowns.
    if (opts.some(o => /bachelor|master|doctor|associate|\bged\b|high school|diploma/.test(N(o)))) {
      if (/\b(master|m\.?sc?\b|m\.?s\b|mba|m\.?eng|m\.?a\b|meng|graduate)\b/.test(want)) { m = opts.find(o => /master/.test(N(o))); if (m) return m; }
      if (/\b(ph\.?d|doctor|doctorate|d\.?phil|d\.?sc|ed\.?d)\b/.test(want)) { m = opts.find(o => /doctor/.test(N(o))); if (m) return m; }
      if (/\b(bachelor|b\.?sc?\b|b\.?s\b|b\.?eng|b\.?a\b|beng|undergraduate)\b/.test(want)) { m = opts.find(o => /bachelor/.test(N(o))); if (m) return m; }
      if (/\bassociate/.test(want)) { m = opts.find(o => /associate/.test(N(o))); if (m) return m; }
      if (/\bged\b/.test(want)) { m = opts.find(o => /\bged\b/.test(N(o))); if (m) return m; }
      if (/high school|secondary|diploma/.test(want)) { m = opts.find(o => /high school/.test(N(o)) && !/pre/.test(N(o))); if (m) return m; }
    }
    // 4c. DEMOGRAPHIC self-ID dropdowns (Workday Voluntary Disclosures / Self
    // Identify). The engine's answer is a generic "No"/"not…", but the options are
    // verbose ("I am not a protected veteran", "No, I do not have a disability",
    // "I am not Hispanic or Latino"). Match on the OPTION content so the policy
    // answer lands. Each branch is gated by option text → never mis-fires.
    if (opts.some(o => /protected veteran/.test(N(o)))) {
      m = opts.find(o => /\bnot a protected veteran\b|\bnot a veteran\b/.test(N(o)) && !/identify|am one/.test(N(o)));
      if (m) return m;
    }
    if (opts.some(o => /disabilit/.test(N(o)))) {
      m = opts.find(o => /no,?\s*i (do not|don.t) have a disability|do not have a disability|no disability|\bno\b/.test(N(o)) && !/yes|i have|i do have/.test(N(o)));
      if (m) return m;
    }
    if (opts.some(o => /hispanic|latino/.test(N(o))) && /\bno\b|not/.test(want)) {
      m = opts.find(o => /not hispanic|not .*latino|^no\b|^no,/.test(N(o)));
      if (m) return m;
    }
    // 5. starts-with
    m = opts.find(o => N(o).startsWith(want)); if (m) return m;
    // 6. SHORTEST option containing the wanted text (shortest avoids the long
    //    decoy variants)
    const byLen = (a, b) => N(a).length - N(b).length;
    let c = opts.filter(o => N(o).includes(want)).sort(byLen);
    if (c.length) return c[0];
    c = opts.filter(o => N(o).includes(normText(filterTerm))).sort(byLen);
    return c[0] || null;
  }

  // Dispatch a key press with legacy keyCode/which set (needed by some widgets).
  function pressKey(el, key, code) {
    for (const type of ['keydown', 'keypress', 'keyup']) {
      const ev = new KeyboardEvent(type, { key, code: key, bubbles: true, cancelable: true });
      try { Object.defineProperty(ev, 'keyCode', { get: () => code }); } catch { /* ignore */ }
      try { Object.defineProperty(ev, 'which', { get: () => code }); } catch { /* ignore */ }
      el.dispatchEvent(ev);
    }
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

  // Click the account-submit control on a login / registration form. Matches
  // loosely (word-boundary, so "Sign In »" / "Login →" / value="Sign In" all
  // count), excludes guest/social. Returns the clicked button's text, or null.
  function clickLoginSubmit() {
    if (DRY_RUN) { dryLog('submit login / create-account'); return null; }
    const cands = Array.from(document.querySelectorAll(
      'button, a[href], [role="button"], input[type="submit"], input[type="button"]')).filter(visible);
    const txt = b => normText(b.innerText || b.value || b.getAttribute('aria-label') || '');
    const bad = b => { const t = txt(b); return SKIP_LOGIN_RE.test(t) || SOCIAL_LOGIN_RE.test(t) || SKIP_A11Y_RE.test(t); };
    // CREATE-ACCOUNT vs SIGN-IN: a create form has TWO password fields (password
    // + confirm/verify) or says "Create Account"; a sign-in has one. When we
    // KNOW the account exists (forceLogin) we always sign in. Clicking the wrong
    // one is why it looped clicking "Sign In" on a Create-Account form.
    const forcing = !!(S.ctx && S.ctx.forceLogin) || S.forceLogin;
    const pwCount = document.querySelectorAll('input[type="password"]').length;
    const creating = !forcing && (pwCount >= 2
      || /create( an?)? account|create( your)? profile|new user|register|sign ?up/i.test(`${headingText()} ${lowerBody().slice(0, 300)}`));
    const SIGNIN = /\bsign ?in\b|\blog ?in\b/;
    const CREATE = /\bcreate\b|\bregister\b|\bsign ?up\b/;
    const pats = creating
      ? [/\bcreate account\b/, /\bregister\b/, /\bsign ?up\b/, /\bcreate\b/, /\bsubmit\b/, /\bcontinue\b/, /^next$/]
      : [/\bsign ?in\b/, /\blog ?in\b/, /^login$/, /\bsubmit\b/, /\bcontinue\b/, /^next$/];
    for (const re of pats) {
      const b = cands.find(x => {
        const t = txt(x);
        if (!t || !re.test(t) || bad(x)) return false;
        if (creating && SIGNIN.test(t)) return false;   // creating → never a sign-in button
        if (!creating && CREATE.test(t)) return false;   // signing in → never a create button
        return true;
      });
      if (b) { b.scrollIntoView({ block: 'center' }); dispatchRealClick(b); return txt(b) || 'button'; }
    }
    return null;
  }

  // ── Workday blueprint ──────────────────────────────────────────────────────
  // Workday (*.myworkdayjobs.com) is a multi-step wizard with STABLE
  // data-automation-id attributes on every control. Using them is far more
  // reliable than text matching. Flow: Create Account/Sign In → My Information →
  // My Experience (resume/work/education/skills) → Application Questions →
  // Voluntary Disclosures → Self Identify → Review → Submit.
  const isWorkday = () => /myworkdayjobs\.com|\.workday\.com/i.test(location.hostname);

  // Click Workday's forward control (Create Account / Save & Continue / Next /
  // Submit) by its automation-id, falling back to the footer button by text.
  function clickWorkdayForward() {
    if (DRY_RUN) { dryLog('Workday forward (Create Account / Save & Continue / Next / Submit)'); return null; }
    // Ordered by intent; the FINAL "Submit" only exists on the Review step.
    const idSel = [
      '[data-automation-id="createAccountSubmitButton"]',
      '[data-automation-id="signInSubmitButton"]',
      '[data-automation-id="pageFooterNextButton"]',
      '[data-automation-id="bottom-navigation-next-button"]',
      '[data-automation-id="wizardNextButton"]',
      'button[data-automation-id="continueButton"]',
      '[data-automation-id="pageFooterSubmitButton"]',
      '[data-automation-id="submitButton"]',
    ];
    for (const s of idSel) {
      const b = document.querySelector(s);
      if (b && visible(b) && !b.disabled) {
        b.scrollIntoView({ block: 'center' });
        dispatchRealClick(b);
        return normText(b.innerText || b.getAttribute('aria-label') || s);
      }
    }
    // Fallback: the wizard footer holds one or two buttons — take the FORWARD
    // one (Next/Continue/Save and Continue/Submit/Review), never Back/Cancel/Save.
    const footer = document.querySelector('[data-automation-id="pageFooter"], [data-automation-id="wizardFooter"], footer') || document.body;
    const b = Array.from(footer.querySelectorAll('button, [role="button"]')).filter(visible)
      .find(x => /^(next|continue|save and continue|submit|review|create account)$/i.test(normText(x.innerText || x.getAttribute('aria-label') || ''))
        && !/^(back|cancel|save for later|save draft|previous|exit)$/i.test(normText(x.innerText || '')));
    if (b) { b.scrollIntoView({ block: 'center' }); dispatchRealClick(b); return normText(b.innerText); }
    return null;
  }

  function clickContinue() {
    if (DRY_RUN) { dryLog('Continue / Submit'); return false; }
    // On a login / registration form (a password field is present), submit via
    // the ACCOUNT button — never a guest "Continue" in a separate section.
    if (document.querySelector('input[type="password"]')) {
      if (clickLoginSubmit()) return true;
    }
    // Workday: use the stable automation-id forward control first.
    if (isWorkday() && clickWorkdayForward()) return true;
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
    // "Apply Manually" / "Use My Last Application". PREFER "Apply Manually" —
    // it reveals fillable form fields the extension completes from candidate
    // data. "Autofill with Resume" opens a NATIVE OS file picker the extension
    // cannot drive, so it just re-opens every tick → infinite loop. Never "use
    // my last application" (could be another candidate's data). Capped so a
    // persistent dialog hands to the human instead of looping.
    if (!isIndeedHost()) {
      const now = Date.now();
      S.entryChoiceCount = S.entryChoiceCount || 0;
      if ((!S.entryChoiceAt || now - S.entryChoiceAt > 12000) && S.entryChoiceCount < 3) {
        const all = Array.from(document.querySelectorAll('button, a, [role="button"], [role="radio"], label')).filter(visible);
        const choice = all.find(b => /^apply manually$|apply without (uploading|a resume|resume)|enter (my (info|information)|manually)|fill (it |this )?out manually|manual(ly)? (apply|application|entry)/i.test(normText(b.innerText)))
          || all.find(b => /autofill with (my )?resume|apply with (my )?resume/i.test(b.innerText || ''));
        if (choice) {
          S.entryChoiceAt = now;
          S.entryChoiceCount++;
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

  const ERR_TEXT_RE = /required|please (enter|select|answer|provide|choose|complete)|(add|enter|provide|select|choose) a valid|is required|invalid|must (be|select|choose|contain|enter|provide)|valid (phone|email|number|value|option|date|zip)|cannot be (empty|blank)|select an option|this (field|question)|field is|missing|answer this/i;

  function validationErrors() {
    const invalid = Array.from(document.querySelectorAll('[aria-invalid="true"]')).filter(visible);
    if (invalid.length) return invalid.length;
    const errTexts = Array.from(document.querySelectorAll('[class*="error" i], [id*="error" i], [role="alert"]'))
      .filter(visible)
      .filter(el => ERR_TEXT_RE.test(el.innerText || ''));
    return errTexts.length;
  }

  // The validation error message attached to a specific field, if any — read so
  // the answer engine/AI can correct the EXACT problem the site is complaining
  // about (wrong option, missing required select, bad format), not blindly
  // refill everything. Prefers ARIA linkage; falls back to a shallow scan of the
  // field's own wrapper so it doesn't grab a neighbouring field's error.
  function fieldErrorText(el) {
    if (!el) return '';
    const ids = `${el.getAttribute('aria-errormessage') || ''} ${el.getAttribute('aria-describedby') || ''}`
      .split(/\s+/).filter(Boolean);
    for (const id of ids) {
      const e = document.getElementById(id);
      if (e && visible(e)) { const t = normText(e.innerText || ''); if (t && ERR_TEXT_RE.test(t)) return t.slice(0, 140); }
    }
    const invalid = el.getAttribute('aria-invalid') === 'true';
    let node = el.parentElement;
    for (let d = 0; node && d < 3; d++, node = node.parentElement) {
      const errEl = Array.from(node.querySelectorAll('[class*="error" i], [role="alert"], [id*="error" i], [class*="invalid" i]'))
        .find(x => x !== el && visible(x) && (x.innerText || '').trim().length < 200 && ERR_TEXT_RE.test(x.innerText || ''));
      if (errEl) return normText(errEl.innerText).slice(0, 140);
      // Stop at the field's own group so we don't cross into a sibling field.
      if (node.matches && node.matches('[class*="field" i], [class*="form-group" i], [class*="question" i], [data-automation-id], fieldset')) break;
    }
    return invalid ? 'this field needs a valid value' : '';
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

  // Registration failed because the candidate ALREADY has an account (so we
  // should stop trying to register and LOG IN as a returning user instead).
  // STRONG phrasings are unambiguous errors and may appear anywhere in the body.
  const ALREADY_EXISTS_STRONG_RE = /cannot be used to create a new candidate|user ?name (is )?already (in use|taken|exists|registered)|(e-?mail|email)( address)? (is )?already (in use|registered|taken|associated|exists)|account already exists|already registered (with|as an account)|an account (with (this|that)|already) exists|this (e-?mail|email|user ?name) (is )?already (in use|taken|registered)/i;
  // WEAK phrasing "(you) already have an account" is ALSO the text of the
  // ubiquitous "Already have an account? Sign In" LINK on create-account pages,
  // so it must NOT count as an error unless it sits inside a real error banner
  // AND isn't the "…? Sign in" navigation prompt. (This false-positive caused
  // Workday create↔sign-in ping-pong.)
  const ALREADY_EXISTS_WEAK_RE = /(you )?already (have|has) an account/i;
  function accountAlreadyExists() {
    const body = document.body?.innerText || '';
    if (ALREADY_EXISTS_STRONG_RE.test(body)) return true;
    const errs = Array.from(document.querySelectorAll('[role="alert"], [class*="error" i], [id*="error" i], [aria-live="assertive"]'))
      .filter(el => visible(el) && el.tagName !== 'A' && el.tagName !== 'BUTTON');
    return errs.some(el => {
      const t = normText(el.innerText || '');
      return ALREADY_EXISTS_WEAK_RE.test(t) && !/already (have|has) an account\s*\??\s*(sign ?in|log ?in)|already (have|has) an account\?/.test(t);
    });
  }
  // Controls that take us to the sign-in page (returning user).
  const GO_LOGIN_RE = /back to (the )?login( page)?|returning user|sign ?in|log ?in|already (have|registered)|login page/i;
  const SIGN_IN_RE = /\b(sign ?in|log ?in|returning user|existing user|already (have|registered)|member (login|sign)|candidate login)\b/i;

  // Find a control that leads to the returning-user sign-in (link or button),
  // excluding create-account and guest-apply.
  const SKIP_A11Y_RE = /skip to|go to (the )?(main )?content|main content|jump to|skip navigation/i;
  function findSignInControl() {
    const cands = Array.from(document.querySelectorAll('a[href], button, [role="button"]')).filter(visible);
    const textOf = b => normText(b.innerText || b.value || b.getAttribute('aria-label') || '');
    let el = cands.find(b => {
      const t = textOf(b);
      return t && SIGN_IN_RE.test(t) && !CREATE_ACCOUNT_RE.test(t) && !SKIP_LOGIN_RE.test(t)
        && !SOCIAL_LOGIN_RE.test(t) && !SKIP_A11Y_RE.test(t);
    });
    if (el) return el;
    // Fall back to an anchor pointing at a DEDICATED sign-in URL — but NOT the
    // generic word "login" (the page's own login.jsf URL would match it, and a
    // "skip to main content" anchor on that page pointed there → click loop),
    // and never a same-page "#fragment" or accessibility skip-link.
    el = cands.find(b => {
      const t = textOf(b);
      if (SKIP_A11Y_RE.test(t)) return false;
      const a = b.closest('a[href]') || (b.tagName === 'A' && b.href ? b : null);
      if (!a) return false;
      const href = a.getAttribute('href') || '';
      if (!href || href.startsWith('#') || a.href === location.href) return false;
      return /\b(signin|sign-in|logon|returning)\b/i.test(a.href) && !/register|guest|create/i.test(a.href);
    });
    return el || null;
  }

  // When we KNOW the candidate already has an account here (forceLogin), get to
  // the sign-in form and NEVER apply-as-guest or register (which would bounce or
  // duplicate). Returns true if it handled this tick (caller should return).
  async function forceSignInIfNeeded() {
    if (!(S.ctx && S.ctx.forceLogin) && !S.forceLogin) return false;
    S.forceLogin = true;
    // A returning-user form is already here (email + password) → let the normal
    // auth/fill path sign in.
    if (document.querySelector('input[type="password"]')) return false;
    if (S.signInClickedAt && Date.now() - S.signInClickedAt < 8000) return true; // awaiting nav
    const signIn = findSignInControl();
    S.signInTries = (S.signInTries || 0) + 1;
    if (signIn && S.signInTries <= 4) {
      S.signInClickedAt = Date.now();
      S.actions++;
      await report('account exists — going to sign-in (not guest/register)', normText(signIn.innerText || signIn.getAttribute('aria-label') || 'sign in').slice(0, 40));
      const anchor = signIn.closest('a[href]') || (signIn.tagName === 'A' && signIn.href ? signIn : null);
      if (anchor?.href && !/^javascript:/i.test(anchor.href) && anchor.href !== location.href) location.href = anchor.href;
      else dispatchRealClick(signIn);
      return true;
    }
    await needsHuman('This candidate already has an account here, but I can\'t find a sign-in link (only guest/registration is shown). Please sign in as a returning user — automation resumes after.', 'already-registered-nolink');
    return true;
  }

  // A guest-apply page ("Apply as Guest" / "Continue as guest"). On some ATSs
  // (e.g. Taleo) guest-apply just emails you a username/password to finish
  // later — we don't want that. Prefer a real account route instead.
  function guestApplyPresent() {
    const body = lowerBody();
    if (/apply as (a )?guest|continue as (a )?guest|guest (application|apply|checkout)/.test(body)) return true;
    return Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible)
      .some(b => /apply as (a )?guest|continue as (a )?guest/i.test(normText(b.innerText || b.getAttribute('aria-label') || '')));
  }

  // On a guest-apply page, steer to the ACCOUNT route: sign in if the account
  // exists, else create an account. Only hand to the human if neither route is
  // offered — never silently guest-apply. Returns true if it handled this tick.
  async function preferAccountRoute() {
    const forcing = !!(S.ctx && S.ctx.forceLogin) || S.forceLogin;
    if (S.acctRouteAt && Date.now() - S.acctRouteAt < 8000) return true; // awaiting nav
    // Bounded: if we keep landing back here after clicking, stop and ask the
    // operator rather than clicking the same control forever.
    S.acctRouteTries = (S.acctRouteTries || 0) + 1;
    if (S.acctRouteTries > 3) {
      await needsHuman('This page only offers "Apply as Guest" and I can\'t reach a create-account or sign-in option automatically. Please create an account or sign in as a returning user — automation resumes after.', 'guest-only');
      return true;
    }
    const cands = Array.from(document.querySelectorAll('a[href], button, [role="button"]')).filter(visible);
    const signIn = findSignInControl();
    const createBtn = cands.find(b => {
      const t = normText(b.innerText || b.value || b.getAttribute('aria-label') || '');
      return (CREATE_ACCOUNT_RE.test(t) || /new user/i.test(t)) && !SKIP_LOGIN_RE.test(t) && !SOCIAL_LOGIN_RE.test(t);
    });
    const target = forcing ? signIn : (createBtn || signIn);
    if (target) {
      S.acctRouteAt = Date.now(); S.actions++;
      await report(forcing ? 'account exists — signing in (skipping guest apply)' : 'choosing account creation over guest apply',
        normText(target.innerText || target.getAttribute('aria-label') || '').slice(0, 40));
      const anchor = target.closest('a[href]') || (target.tagName === 'A' && target.href ? target : null);
      if (anchor?.href && !/^javascript:/i.test(anchor.href) && anchor.href !== location.href) location.href = anchor.href;
      else dispatchRealClick(target);
      return true;
    }
    await needsHuman('This page only offers "Apply as Guest" (which emails a username/password to finish later). Please create an account or sign in as a returning user — automation resumes after.', 'guest-only');
    return true;
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
    const externalBtn = document.querySelector('a[data-testid="applyButtonLinkContainer"], [data-testid="applyButtonLinkContainer"] a')
      || Array.from(document.querySelectorAll('a, button')).filter(visible)
        .find(b => /apply on (company|employer) site|^apply now$/i.test(normText(b.innerText)));
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
    // Account exists → go sign in; never take "Apply as Guest" / register here.
    if (accountAlreadyExists()) { await send({ type: 'MARK_FORCE_LOGIN', jobIndex: S.ctx.jobIndex }); }
    if (await forceSignInIfNeeded()) return;
    // Guest-apply page → steer to create-account / sign-in instead (guest emails
    // credentials on some ATSs). Only human if no account route is offered.
    if (guestApplyPresent() && (await preferAccountRoute())) return;
    // Workday "My Experience" page (Add buttons, no fields yet) → open the form.
    if (await maybeOpenExperienceForm()) return;
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
      'button, a[href], [role="button"], input:not([type=hidden]), select, textarea,'
      + ' [role="option"], [contenteditable="true"], [contenteditable=""], [role="textbox"]'))
      .filter(visible)
      .filter(el => !['submit-hidden'].includes(el.type));
    const out = [];
    for (const el of els) {
      if (out.length >= 60) break;
      const tag = el.tagName.toLowerCase();
      const editable = el.isContentEditable || el.getAttribute('role') === 'textbox';
      const isField = tag === 'input' || tag === 'select' || tag === 'textarea' || editable;
      let text = isField ? cleanLabel(labelForInput(el)) : normText(el.innerText || el.getAttribute('aria-label') || el.value || '');
      // Contenteditable chat/message boxes rarely have a <label>; fall back to
      // placeholder/aria so the AI can still recognise "type your message here".
      if (editable && !text) text = normText(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || 'message box');
      if (!text) continue;
      const aiId = `ai-${S.aiSeq++}`;
      _air.set(aiId, el);
      const disabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true'
        || (el.classList && (el.className || '').toLowerCase().includes('disabled'));
      out.push({
        id: aiId, tag: editable ? 'textbox' : tag,
        type: isField ? (editable ? 'text' : (el.type || tag)) : '',
        text,
        value: isField ? (editable ? normText(el.innerText || '') : (el.value || '')) : '',
        required: (isField && !editable) ? isRequired(el, text) : false,
        disabled: disabled || undefined,
      });
    }
    return out;
  }

  // Full visible page text the AI reads to understand instructions, terms, and
  // chat questions. Trimmed and de-whitespaced; capped so the prompt stays lean.
  function pageVisibleText() {
    const root = document.querySelector('main, [role="main"]') || document.body;
    if (!root) return '';
    let t = '';
    try { t = root.innerText || ''; } catch { t = document.body?.innerText || ''; }
    return t.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim().slice(0, 4000);
  }

  // Send a chat/conversational reply after the message box has been filled.
  // Prefers a real Send/Submit control near the box; falls back to Enter (many
  // chat widgets submit on Enter, newline on Shift+Enter).
  async function sendChatMessage(box) {
    const SEND_RE = /^(send|reply|submit|send message|►|➤|➔)$/i;
    // Look for a send button within the box's form/container, then page-wide.
    const scopes = [box.closest('form'), box.closest('[class*="chat" i], [class*="message" i], [class*="composer" i], [class*="input" i]'), document];
    let btn = null;
    for (const sc of scopes) {
      if (!sc) continue;
      btn = Array.from(sc.querySelectorAll('button, [role="button"], [type="submit"]'))
        .filter(visible)
        .find(b => {
          const t = normText(b.innerText || b.value || b.getAttribute('aria-label') || '');
          return SEND_RE.test(t) || /\bsend\b/i.test(b.getAttribute('aria-label') || '') || /send/i.test(b.className || '');
        });
      if (btn) break;
    }
    if (btn) {
      await report('AI sending chat message', normText(btn.innerText || btn.getAttribute('aria-label') || 'Send'));
      dispatchRealClick(btn);
    } else {
      // No Send button — press Enter inside the box.
      box.focus();
      for (const type of ['keydown', 'keypress', 'keyup']) {
        box.dispatchEvent(new KeyboardEvent(type, {
          key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true,
        }));
      }
      await report('AI sending chat message (Enter)');
    }
    await sleep(900); // let the bot's next question render before the next tick
  }

  async function aiNavigate() {
    S.aiNavCount++;
    const pageText = pageVisibleText();
    // Progress-aware budget. A conversational (chatbot) application is many
    // turns on ONE page, so a flat step cap would kill it mid-conversation.
    // Instead: reset a stall counter whenever the visible text CHANGES (a new
    // question/answer appeared = real progress); only give up after many ticks
    // with NO change, or a high absolute hard cap as a final backstop.
    const textHash = pageText.length + ':' + pageText.slice(-160);
    // Already handed this exact page to the human — stop re-asking the AI (and
    // stop re-alerting) until the page actually changes (the human advanced it).
    if (S.aiHandedToHuman && S.aiHandedToHuman === textHash) return true;
    if (textHash === S.aiLastTextHash) S.aiStall = (S.aiStall || 0) + 1;
    else { S.aiStall = 0; S.aiLastTextHash = textHash; S.aiHandedToHuman = null; }
    if (S.aiStall >= 8) {
      S.aiHandedToHuman = textHash;
      await needsHuman('The page stopped responding to the AI (no change after several attempts). Please advance the application manually — automation resumes on the next screen.', 'ai-stall');
      return true;
    }
    if (S.aiNavCount > 120) {
      S.aiHandedToHuman = textHash;
      await needsHuman('AI navigation exhausted its step budget on this site. Please advance the application manually.', 'ai-budget');
      return true;
    }
    const elements = collectAiElements();
    if (!elements.length) return false;
    const emptyRequired = scrapeFields().filter(f => f.required && !f.currentValue).map(f => f.label);
    const scrollMax = Math.max(0, (document.documentElement.scrollHeight || 0) - window.innerHeight);
    const page = {
      url: location.href.slice(0, 200),
      title: (document.title || '').slice(0, 120),
      heading: headingText(),
      pageText,
      scrollY: Math.round(window.scrollY || 0),
      scrollMax: Math.round(scrollMax),
      elements,
      emptyRequired,
    };
    const res = await send({ type: 'GET_AI_ACTION', jobIndex: S.ctx.jobIndex, page, resumeUploaded: S.resumeUploaded });
    if (!res || res.error) return false;

    if (res.action === 'scroll') {
      // Scroll ALL the way to the bottom in ONE action (through the terms
      // container AND the window), not one viewport per tick — long agreements
      // need the whole thing scrolled to reveal/enable the Accept button.
      const box = findScrollableTextContainer();
      const winBefore = window.scrollY;
      const boxBefore = box ? box.scrollTop : 0;
      await report('AI scrolling through the terms to the bottom', res.reason || '');
      const reached = await scrollToBottom(box);
      const moved = Math.abs(window.scrollY - winBefore) > 8 || (box && Math.abs(box.scrollTop - boxBefore) > 8);
      if (reached || moved) {
        S.aiScrollStuck = 0;
        if (reached) await report('AI reached the bottom of the terms — looking for the Accept control');
      } else {
        // Couldn't move at all — nothing scrollable, or already at the end with
        // no accept path. Give the AI a couple of tries, then hand off.
        S.aiScrollStuck = (S.aiScrollStuck || 0) + 1;
        if (S.aiScrollStuck >= 3) {
          await needsHuman('AI reached the end of the page but found no Accept/Continue control. Please advance the application manually.', 'ai-scroll-stuck');
          return true;
        }
      }
      return true;
    }

    if (res.action === 'fill' && res.id && res.value != null) {
      const el = aiElById(res.id);
      if (!el || !visible(el)) return false;
      const label = normText(el.getAttribute('aria-label') || el.getAttribute('placeholder') || labelForInput(el) || res.id);
      await report(`AI ${res.submit ? 'replying' : 'filling'}`, `${label}: ${res.value}`.slice(0, 90));
      S.actions++;
      const editable = el.isContentEditable || el.getAttribute('role') === 'textbox';
      if (editable) {
        el.focus();
        // Contenteditable chat boxes: set text and fire input so React state syncs.
        el.textContent = res.value;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: res.value, inputType: 'insertText' }));
      } else if (el.tagName.toLowerCase() === 'select') {
        setNativeValue(el, res.value);
        el.dispatchEvent(new Event('change', { bubbles: true }));
      } else {
        await typeInto(el, res.value);
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
      }
      await sleep(250);
      // Conversational (chatbot) application: after typing the reply, SEND it —
      // click a Send button next to the box, else press Enter. The AI sets
      // submit=true only for chat messages (not for ordinary form fields).
      if (res.submit) await sendChatMessage(el);
      return true;
    }

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

      // Guard: a DISABLED control (e.g. an "I Accept" button locked until the
      // terms are fully scrolled) can't be clicked — scroll down to enable it
      // instead of clicking into the void.
      const isDisabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true'
        || (el.className || '').toLowerCase().includes('disabled');
      if (isDisabled) {
        const box = findScrollableTextContainer();
        if (box) box.scrollBy(0, Math.round(box.clientHeight * 0.9));
        else window.scrollBy(0, Math.round(window.innerHeight * 0.9));
        await report('AI target is disabled — scrolling to enable it', clickText.slice(0, 60));
        await sleep(400);
        return true;
      }

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
  const isAutocompleteField = (f) =>
    f.combobox || /\b(location|city|town|address|where)\b/i.test(f.label || '');

  async function applyAnswers(fields, answers, opts = {}) {
    // opts.force: overwrite even a field that already holds a value — used when
    // the site REJECTED the current value (validation error), so we must replace
    // it with the corrected answer.
    const force = !!opts.force;
    // Fill autocomplete/location fields LAST: while one is mid-selection (its
    // suggestion dropdown open), filling another field would steal focus and
    // Google-Places/Lever would discard the unselected value. Order matters.
    const order = fields.map((_, i) => i)
      .sort((a, b) => (isAutocompleteField(fields[a]) ? 1 : 0) - (isAutocompleteField(fields[b]) ? 1 : 0));
    for (const i of order) {
      const field = fields[i];
      const a = answers[i];
      if (!a || a.answer == null || a.answer === '') continue;
      const authoritative = a.source === 'profile' || a.source === 'policy';
      // Autocomplete/location fields: don't re-type a committed suggestion that
      // ALREADY satisfies our answer (re-typing breaks it — the vanish loop). But
      // DO overwrite a value that doesn't match (e.g. a stale "N/A" or a Workday
      // geo-default) — otherwise wrong prefilled data is never corrected.
      if (!force && isAutocompleteField(field) && field.currentValue
          && normText(field.currentValue).includes(normText(a.answer))) continue;
      // Non-authoritative answers (memory/AI) never clobber an existing value;
      // authoritative ones (profile/policy) may correct a wrong prefill.
      if (!force && field.currentValue && !authoritative) continue;
      if (normText(field.currentValue) === normText(a.answer)) continue;
      await fillField(field, a.answer);
      // Unhurried, one field at a time (human pace, like the Indeed engine).
      // Dropdowns/autocompletes get a bit longer to commit before the next field.
      const slow = isAutocompleteField(field) || field.type === 'select' || field.type === 'listbox';
      await sleep(slow ? 1200 : FIELD_PACE_MS);
    }
  }

  // Workday/Greenhouse "My Experience" pages have "Add" buttons that expand into
  // sub-forms: Work Experience (Job Title / Company / From-To / …) and Education
  // (School / Degree / Field of Study). Click them so those fields render and the
  // normal pipeline fills them from the candidate's parsed resume. Each section
  // is opened at most once.
  async function maybeOpenExperienceForm() {
    const body = lowerBody();
    const wd = /myworkdayjobs|workday/i.test(location.hostname);
    // Does any label/legend/aria-label on the page match this probe? (sub-form open)
    const fieldPresent = (re) => Array.from(document.querySelectorAll('label, legend, input, [aria-label]')).some(e =>
      re.test((e.innerText || e.getAttribute('aria-label') || e.placeholder || '')));
    const specs = [
      {
        flag: 'expAddClicked', name: 'work-experience',
        section: /work experience|my experience|add your (work )?experience/,
        openProbe: /job title|position title/i,
        addRe: /^add( work| your)?( experience)?$/i, headRe: /work experience|experience/i,
      },
      {
        flag: 'eduAddClicked', name: 'education',
        section: /education|degree|school|university/,
        openProbe: /school or university|school name|university|institution|degree|field of study/i,
        addRe: /^add( education)?$/i, headRe: /education/i,
      },
    ];
    for (const s of specs) {
      if (S[s.flag]) continue;
      if (!s.section.test(body)) continue;
      if (fieldPresent(s.openProbe)) continue; // sub-form already open → let it fill
      const addBtns = Array.from(document.querySelectorAll('button, [role="button"], a')).filter(visible)
        .filter(b => s.addRe.test(normText(b.innerText)));
      if (!addBtns.length) continue;
      // Prefer the Add nearest the matching section heading.
      const headingMatched = addBtns.find(b => {
        let n = b;
        for (let i = 0; i < 5 && n; i++, n = n.parentElement) {
          const h = n.querySelector && n.querySelector('h1,h2,h3,h4,legend,label,[class*="title" i]');
          if (h && s.headRe.test(h.innerText || '')) return true;
        }
        return false;
      });
      // On Workday, if only one Add button exists and no heading disambiguates,
      // fall back to it only for work-experience (education Add must be near an
      // education heading to avoid opening the wrong sub-form).
      const target = headingMatched || (wd && s.flag === 'expAddClicked' && addBtns.length === 1 ? addBtns[0] : null);
      if (!target) continue;
      S[s.flag] = true;
      await report(`opening ${s.name} form (Add)`);
      S.actions++;
      dispatchRealClick(target);
      await sleep(900);
      return true;
    }
    return false;
  }

  // The BACKGROUND loop-detector (which survives page reloads — our in-page
  // guards don't) reports we keep landing on this same page+fields. Yield the
  // page: 'ai' → the smart navigator reads it, scrolls terms, clicks Accept /
  // Apply-at-the-bottom; 'human' → we've tried enough, ask the operator. This
  // is the escape hatch for ATS pages (e.g. Taleo privacy agreement) that let
  // us "fill" a field then reload right back, spinning the filler forever.
  async function handleEscalation(res, detail) {
    if (!res || !res.escalate) return false;
    if (res.escalate === 'human') {
      await needsHuman(
        `This page ("${detail || location.hostname}") keeps reloading without accepting the automated entries — it likely needs terms scrolled/accepted or a manual Apply. `
        + 'Please complete this step manually; automation resumes on the next screen.', `loop:${detail || ''}`.slice(0, 60));
      return true;
    }
    await report('page keeps repeating — switching to AI navigator (read / scroll terms / accept)', detail || '');
    await aiNavigate();
    return true;
  }

  async function handleFormStep(detail) {
    handleDialogs();
    if (!isIndeedHost()) await sendExternalMeta();
    if (await maybeOpenExperienceForm()) return;

    // Registration failed because the candidate ALREADY has an account (e.g.
    // Taleo: "This user name cannot be used to create a new candidate record …
    // sign in as a returning user"). STOP re-registering — go to the sign-in
    // page and log in. Do NOT fall through to fill the registration form again.
    if (accountAlreadyExists()) {
      S.forceLogin = true; // never prefer create-account after this
      // Persist across the upcoming navigation (content resets on reload).
      await send({ type: 'MARK_FORCE_LOGIN', jobIndex: S.ctx.jobIndex });
      if (S.loginNavAt && Date.now() - S.loginNavAt < 8000) return; // awaiting nav
      const loginLink = Array.from(document.querySelectorAll('a[href], button, [role="button"]'))
        .filter(visible)
        .find(b => GO_LOGIN_RE.test(normText(b.innerText || b.getAttribute('aria-label') || '')));
      S.loginNavTries = (S.loginNavTries || 0) + 1;
      if (loginLink && S.loginNavTries <= 3) {
        S.loginNavAt = Date.now();
        S.actions++;
        await report('account already exists — going to the sign-in page (returning user)', normText(loginLink.innerText || loginLink.getAttribute('aria-label') || 'login').slice(0, 40));
        const anchor = loginLink.closest('a[href]') || (loginLink.tagName === 'A' && loginLink.href ? loginLink : null);
        if (anchor?.href && !/^javascript:/i.test(anchor.href) && anchor.href !== location.href) location.href = anchor.href;
        else dispatchRealClick(loginLink);
        return;
      }
      await needsHuman('This candidate already has an account on this site. Please sign in as a returning user (or use "Forgot username") — automation resumes after.', 'already-registered');
      return;
    }

    // Already learned (this run) that the account exists — prefer sign-in and
    // never take a guest/skip or create-account path, even after navigation.
    if (await forceSignInIfNeeded()) return;

    // Login / signup / account wall handling — ONLY when this is genuinely an
    // auth screen. A stray newsletter/job-alert email input does NOT count.
    // Auth signal = a password field, OR social/email-login/create-account
    // buttons present.
    const authButtons = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible)
      .filter(b => SOCIAL_LOGIN_RE.test(normText(b.innerText || '')) || EMAIL_LOGIN_RE.test(normText(b.innerText || '')) || CREATE_ACCOUNT_RE.test(normText(b.innerText || '')));
    const isAuthUI = !!document.querySelector('input[type="password"]') || authButtons.length > 0;
    if (isAuthUI) {
      const loginish = !document.querySelector('input[type="password"]') && !emailInputPresent();

      if (loginish) {
        // This is a CHOOSER (buttons only, no fields yet). Preference — an
        // ACCOUNT is best: sign in if the candidate has one, else create one.
        // Guest-apply is LAST resort (on some ATSs it just emails a username/
        // password to finish later, which we don't want).

        // (a) account exists → sign in, never create/guest.
        if (S.forceLogin || (S.ctx && S.ctx.forceLogin)) {
          const signInBtn = findSignInControl();
          if (signInBtn) {
            await report('account exists — choosing sign-in (returning user)');
            S.actions++; dispatchRealClick(signInBtn); return;
          }
          await needsHuman('This candidate already has an account here but only account-creation/social options are shown. Please sign in manually — automation resumes after.', 'already-registered');
          return;
        }
        // (b) create an account (candidate has none) — preferred over guest.
        const createBtn = findButton([CREATE_ACCOUNT_RE]);
        if (createBtn) {
          await report('login wall — creating an account (preferred over guest apply)');
          S.actions++; dispatchRealClick(createBtn); return;
        }
        // (c) email-login path.
        const emailBtn = findButton([EMAIL_LOGIN_RE]);
        if (emailBtn) {
          await report('login wall — choosing email path');
          S.actions++; dispatchRealClick(emailBtn); return;
        }
        // (d) a plain sign-in control.
        const signInBtn = findSignInControl();
        if (signInBtn) {
          await report('login wall — choosing sign-in');
          S.actions++; dispatchRealClick(signInBtn); return;
        }
        // (e) LAST resort — guest apply (no create/login option offered).
        const skipBtn = Array.from(document.querySelectorAll('button, a, [role="button"]'))
          .filter(visible).find(b => SKIP_LOGIN_RE.test(normText(b.innerText || '')));
        if (skipBtn) {
          await report('no account option available — applying as guest (last resort)', normText(skipBtn.innerText));
          S.actions++; dispatchRealClick(skipBtn); return;
        }
        // (f) social/SSO only.
        await needsHuman('This login only offers Google/Apple/SSO sign-in, which automation can\'t do. Please log in manually — automation resumes after.', 'social-login');
        return;
      }
      // else: email/password fields ARE present → fall through and fill them
      // (that IS the create-account or sign-in form — the account route).
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

      // Validation errors ("Errors Found", "X is required", "please select an
      // option") — READ the error attached to each field and re-answer THAT
      // specific field using the error as context, forcing an overwrite of the
      // rejected value. Falls back to a full refill only if no per-field error
      // can be located.
      const errs = validationErrors();
      if (errs) {
        S.refillCount = S.refillCount || {};
        S.refillCount[sig] = (S.refillCount[sig] || 0) + 1;
        if (S.refillCount[sig] <= 4) {
          const all = scrapeFields();
          const errored = [];
          for (const f of all) {
            const err = fieldErrorText(findByFieldId(f.id));
            if (err) errored.push({ ...f, error: err });
          }
          if (errored.length) {
            await report('validation error — fixing the flagged field(s)',
              errored.map(f => `${f.label} → ${f.error}`).slice(0, 2).join(' | ').slice(0, 140));
            const r = await send({ type: 'GET_ANSWERS', jobIndex: S.ctx.jobIndex, fields: errored, pageUrl: location.href });
            if (await handleEscalation(r, detail)) return;
            await applyAnswers(errored, r.answers || [], { force: true });
            await sleep(400);
            return;
          }
          // Couldn't pin the error to a field — refill everything as a fallback.
          S.processedSigs.delete(sig);
          await report('validation errors — refilling all fields', `${errs} error(s), round ${S.refillCount[sig]}`);
          return;
        }
        await needsHuman(`Validation errors on step "${detail}" that auto-fill could not resolve after ${S.refillCount[sig] - 1} tries. Please fix and continue.`, sig);
        return;
      }
      // VERIFY-BEFORE-ADVANCE: never click Next until every required field on
      // the page is actually filled (committed in the DOM). Refill any still-
      // empty required field one round per tick — this handles slow dropdowns
      // whose selection commits after a delay, and uses Gemini as the fallback
      // for anything the deterministic rules can't answer.
      const emptyReq = scrapeFields().filter(f => f.required && !f.currentValue);
      if (emptyReq.length) {
        S.fillRounds = S.fillRounds || {};
        S.fillRounds[sig] = (S.fillRounds[sig] || 0) + 1;
        if (S.fillRounds[sig] <= 6) {
          await report(`completing required field(s) before Next (round ${S.fillRounds[sig]})`, emptyReq.map(f => f.label).slice(0, 3).join(' | '));
          const r = await send({ type: 'GET_ANSWERS', jobIndex: S.ctx.jobIndex, fields: emptyReq, pageUrl: location.href });
          if (await handleEscalation(r, detail)) return;
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
      // All required fields filled and no errors — safe to advance now.
      await settleBeforeContinue();
      await tryAdvance(sig, detail);
      return;
    }

    const fields = scrapeFields();
    await report(`form step: ${detail || 'unnamed'}`, `${fields.length} field(s)`);

    if (fields.length) {
      const res = await send({ type: 'GET_ANSWERS', jobIndex: S.ctx.jobIndex, fields, pageUrl: location.href });
      if (res.error) { await logBg(`GET_ANSWERS error: ${res.error}`, 'warn'); return; }
      if (await handleEscalation(res, detail)) return; // page is looping — AI/human takes over
      await applyAnswers(fields, res.answers || []);
    }
    // Mark processed WITHOUT clicking Next. The processed branch below then
    // verifies every required field actually committed (refilling empties
    // one-by-one, Gemini as fallback) and only THEN clicks Next — so we never
    // advance before the form is truly complete (the cause of Workday's
    // premature-Next dropdown errors).
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
    // Login / registration form: click the account submit explicitly and log
    // which control we clicked (so a mismatch is visible in the activity log).
    let clicked = false;
    if (document.querySelector('input[type="password"]')) {
      const which = clickLoginSubmit();
      if (which) { await report('login form — clicked sign-in control', which.slice(0, 40)); clicked = true; }
      else if (S.stuck[sig] === 1) {
        // Diagnostic: nothing matched — surface the visible buttons so we can
        // see the actual label and tune the matcher.
        const labels = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a[href], [role="button"]'))
          .filter(visible).map(b => normText(b.innerText || b.value || b.getAttribute('aria-label') || '')).filter(Boolean);
        await report('login form — no sign-in button matched; visible buttons', labels.slice(0, 8).join(' | ').slice(0, 140));
      }
    }
    if (!clicked) clicked = clickContinue();
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
    if (DRY_RUN) { dryLog('resume upload'); S.resumeUploaded = true; return true; }
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
    // Package failed on the backend (no resume, fetch error) → say exactly why.
    if (res.status === 'failed' || !res.b64) {
      await needsHuman(
        `Resume unavailable from the backend${res.error ? ` (${res.error})` : ''}. `
        + 'Attach a resume manually — the other fields will still be auto-filled.', 'inline-resume');
      return true;
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
          send({ type: 'MARK_RESUME_DONE', jobIndex: S.ctx.jobIndex }); // survive reloads
          await report(`resume attached to external form (${res.tailored ? 'tailored' : 'base'})`, fname);
          return true;
        } catch (e) {
          await needsHuman(`Resume attach failed (${e.message}) on ${location.hostname}. Attach it manually — other fields still auto-fill.`, 'inline-resume');
          return true;
        }
      }
      // Have the file, but no upload field on this page.
      await needsHuman(
        `Couldn't find the resume upload field on ${location.hostname}. `
        + 'Click the upload/attach control (or attach the resume) — the other fields will still auto-fill.', 'inline-resume');
      return true;
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
            send({ type: 'MARK_RESUME_DONE', jobIndex: S.ctx.jobIndex }); // survive reloads
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
  // "The access code you entered is incorrect / invalid / expired — try again".
  const OTP_REJECT_RE = /(access |verification )?code[^.]{0,30}(is )?(incorrect|invalid|expired|not (valid|correct)|didn'?t match)|incorrect (access |verification )?code|invalid (access |verification )?code|code (is )?(incorrect|invalid|expired)|(that|the) code (is )?(incorrect|invalid|wrong|expired)/i;

  async function handleOtpStep() {
    handleDialogs();
    const shape = detectOtpShape();
    if (!shape) return;

    // A previously-filled code was REJECTED by the site → clear it and fetch a
    // fresh one (the correct email often arrives a little later, or the first
    // extraction grabbed a stale/wrong token). Don't just keep resubmitting it.
    const rejected = OTP_REJECT_RE.test(document.body?.innerText || '');
    if (rejected && S.otpFilled) {
      await report('access code was rejected — clearing and fetching a fresh code');
      S.otpFilled = false;
      for (const el of shape.els) { try { el.focus(); setNativeValue(el, ''); } catch { /* */ } }
      await sleep(400);
    }

    // Already filled and not rejected → WAIT for the submit to navigate. Do NOT
    // re-click submit within a few seconds: these codes are single-use, and a
    // second submit re-sends the same one-time code, which the site then rejects
    // as "already used / incorrect" (the likely cause of a fresh, exact code
    // being rejected). Only nudge submit again after a real stall.
    if (S.otpFilled) {
      if (S.otpSubmitAt && Date.now() - S.otpSubmitAt < 7000) return; // let it navigate
      S.otpResubmits = (S.otpResubmits || 0) + 1;
      if (S.otpResubmits > 2) return; // stop hammering a single-use code
      clickContinue();
      return;
    }

    // Cap the number of DISTINCT codes we try before handing to the operator.
    // Kept low: email OTP codes are short-lived, so chasing more than a couple
    // is usually futile — the operator can type the current one faster.
    S.triedCodes = S.triedCodes || [];
    if (S.triedCodes.length >= 2) {
      await needsHuman(
        'The access codes fetched from the candidate\'s Gmail were rejected. '
        + 'Please read the current code from their inbox and type it manually — automation resumes after.', 'otp-wrong');
      return;
    }

    const res = await send({ type: 'GET_CODE', jobIndex: S.ctx.jobIndex, senderHint: location.hostname.replace(/^www\./, '').split('.')[0] });
    if (!res.code) {
      S.otpPolls = (S.otpPolls || 0) + 1;
      if (S.otpPolls === 1) await report('verification screen — fetching code from candidate Gmail...');
      if (S.otpPolls === 12) await report('still waiting on the code email — you can type it manually in the tab anytime');
      if (S.otpPolls > 22) { // ~1 min of ticks — prompt the operator sooner
        await needsHuman(
          'Verification code not found in the candidate\'s Gmail (not connected, or email not arriving / not in the search window). '
          + 'Type the code from their inbox into the tab — automation resumes after.', 'otp');
      }
      return;
    }

    const code = String(res.code).trim();
    // If Gmail returned a code we ALREADY tried (same stale email, or extraction
    // repeats), don't resubmit it — wait for a newer email to arrive.
    if (S.triedCodes.includes(code)) {
      S.otpWaitPolls = (S.otpWaitPolls || 0) + 1;
      if (S.otpWaitPolls === 1) await report('last code was rejected — waiting for a newer access-code email...');
      if (S.otpWaitPolls > 12) {
        await needsHuman(
          'The access code from Gmail keeps being rejected and no newer code has arrived. '
          + 'Please type the current code from the candidate\'s inbox manually — automation resumes after.', 'otp-wrong');
      }
      return;
    }
    S.triedCodes.push(code);
    S.otpWaitPolls = 0;

    await report('filling verification code', `${code.length} chars, ${shape.kind}, try ${S.triedCodes.length}`);
    if (shape.kind === 'split') {
      const boxes = shape.els;
      for (let i = 0; i < boxes.length && i < code.length; i++) {
        boxes[i].focus();
        setNativeValue(boxes[i], code[i]);
        boxes[i].dispatchEvent(new Event('input', { bubbles: true }));
        await sleep(80);
      }
    } else {
      const el = shape.els[0];
      // ALWAYS type it out character-by-character like a human — validated/masked
      // ATS code fields ignore a programmatic value set, so we never take that
      // shortcut. typeInto focuses, clears, and dispatches key + input events per
      // character.
      await typeInto(el, code);
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.dispatchEvent(new Event('blur', { bubbles: true }));
      // Log what the field ACTUALLY contains now, so a silent fill failure or a
      // case/trim mismatch is visible in the activity log.
      await report('access code field now contains', `"${el.value || ''}" (wanted "${code}")`);
    }
    S.otpFilled = true;
    S.otpSubmitAt = Date.now(); // guard against re-submitting this one-time code
    S.actions++;
    await sleep(600);
    // Verify/submit button on OTP screens — click ONCE.
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
      S.ctx = { jobIndex: gate.jobIndex, job: gate.job, candidateName: gate.candidateName, autoSubmit: gate.autoSubmit, hasSitePassword: gate.hasSitePassword, forceLogin: !!gate.forceLogin };
      // Restore "resume already uploaded" across page reloads so we never
      // re-upload / re-click "Replace resume" (the iCIMS re-parse loop).
      if (gate.resumeDone) S.resumeUploaded = true;

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
        case 'AI_NAV':
          // Conversational application — let the smart navigator read the
          // transcript and answer the latest question.
          await aiNavigate();
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

  // ── TEST HARNESS HOOK ──────────────────────────────────────────────────────
  // When loaded by test/harness.html (which sets window.__BD_HARNESS__ = true
  // BEFORE this script), skip the live bootstrap entirely — no chrome messaging,
  // no tick loop, no MutationObserver — and instead expose the pure DOM helpers
  // so the harness can drive them against fixture markup and let a human WATCH
  // them run (pick a country, add skill pills, tick race, open an Add section).
  // This branch is NEVER reached in the real extension: the flag is only ever
  // set by the harness page, which the extension never loads.
  if (window.__BD_HARNESS__) {
    window.__bd = {
      scrapeFields, fillField, bestOption, fillSearchableSelect, fillMultiSelect,
      labelForInput, fieldHint, collectOpenOptions, multiselectSelectedValue,
      multiselectPillEls, maybeOpenExperienceForm, newFieldReg, normText,
      // exposed for live diagnosis on a real ATS page:
      classify, handleFormStep, applyAnswers, S, isWorkday, clickWorkdayForward,
    };
    return;
  }

  // Kick off: check with background whether this tab is under automation.
  (async () => {
    // PORTAL GATE: this is the GENERIC engine — it acts on every NON-Indeed
    // page. Indeed frames are handled by content/indeed.js, so this engine
    // stands down there to avoid two engines driving the same page.
    if (isIndeedHost()) return;
    const gate = await send({ type: 'CONTENT_READY' });
    if (!gate.act) return; // normal browsing tab — do nothing, ever
    S.ctx = { jobIndex: gate.jobIndex, job: gate.job, candidateName: gate.candidateName, autoSubmit: gate.autoSubmit, hasSitePassword: gate.hasSitePassword, forceLogin: !!gate.forceLogin };
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
