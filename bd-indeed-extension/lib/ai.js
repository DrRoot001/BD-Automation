// AI question answerer. Used ONLY for screening questions that the profile
// heuristics and the persisted answer memory could not resolve. Mirrors the
// project's LLM order: Gemini primary, Anthropic fallback.

const OPERATOR_POLICY = `
Operator demographic/answering policy (fixed for all represented candidates,
explicit profile fields override these defaults):
- Gender: infer from the candidate's first name; if ambiguous answer "Male".
- Race/ethnicity: "South Asian" if offered, otherwise the closest "Asian" option.
- Hispanic or Latino: No.
- Sexual orientation: Heterosexual. Transgender: No.
- Veteran status: No / "I am not a protected veteran".
- Disability: "No, I do not have a disability".
- Country of residence: United States.
- "How did you hear about this job/us": LinkedIn.
- Work authorization: legally authorized to work in the job's country WITHOUT
  restrictions = Yes. Visa sponsorship needed now or in the future = No.
- Security clearance held/granted by the US Government within the past 2 years
  (NAC, Confidential, Secret, Top Secret, TS/SCI, etc.) = No.
- Willing to commute / relocate / work on-site if asked: Yes.
- Never invent employment history that is not in the resume; for company-specific
  history questions the candidate has no relation to, answer "N/A" or "No".
`;

function buildPrompt(questions, context) {
  const q = questions.map((f, i) => ({
    idx: i,
    question: f.label,
    field_type: f.type,
    options: f.options && f.options.length ? f.options : undefined,
    required: !!f.required,
    // Present only when the site rejected this field — the validation message.
    error: f.error || undefined,
    current_value: f.currentValue || undefined,
  }));
  return `You are filling a job application form on Indeed on behalf of a candidate.
Answer each screening question AS THE CANDIDATE, based on the candidate profile and resume below.

CANDIDATE PROFILE:
${JSON.stringify(context.profile, null, 1).slice(0, 3000)}

RESUME (parsed):
${JSON.stringify(context.resume || {}, null, 1).slice(0, 9000)}

JOB: ${context.jobTitle || 'unknown'} at ${context.jobCompany || 'unknown'}

${OPERATOR_POLICY}

RULES:
1. For questions with "options", the answer MUST be EXACTLY one of the options, verbatim.
1b. NEVER leave a field blank or on a placeholder. If a required field has options,
   ALWAYS choose a real option — never "Not Specified", "Please Select", "Select One",
   "Choose…", "--", or an empty value. If genuinely unsure, pick the most reasonable/
   most favorable valid option (for a Yes/No, prefer the answer that keeps the
   candidate eligible). Every required question MUST get a concrete answer.
2. For field_type "number", answer with digits only (no commas, "$", or words).
3. For dates, use MM/DD/YYYY.
4. Keep free-text answers short (1-3 sentences), first person, professional, never mention AI.
5. Salary questions: answer ${context.desiredSalary || '85000'} unless the question demands a range.
6. Hours-per-week questions: ${context.desiredHours || '40'}. Start date / notice: "Immediately" (or the closest option).
7. If a question is optional and truly not applicable, answer "N/A".
8. Answer positively and favorably for the candidate whenever truthful.
9. if the question is about current city and then ai should intelligently anyalse the state and zipcode based on the candidate state/city
10. ERROR CORRECTION: if a question includes an "error" field, the site REJECTED the previous answer ("current_value"). Read the error and return a DIFFERENT, corrected answer that satisfies it — e.g. error "please select an option" → choose a valid option (never blank/placeholder); "enter a valid phone/email/date" → fix the format; "required" → provide the value. Do NOT return the same current_value that was rejected.
11. if the question asked for example answer intelligently based on the candidates data
QUESTIONS (JSON):
${JSON.stringify(q, null, 1)}

Respond with ONLY a JSON array: [{"idx": <number>, "answer": "<string>"}] — one entry per question, no markdown fences.`;
}

function parseAnswers(text, count) {
  const cleaned = text.replace(/```(json)?/gi, '').trim();
  const start = cleaned.indexOf('[');
  const end = cleaned.lastIndexOf(']');
  if (start === -1 || end === -1) throw new Error('AI returned no JSON array');
  const arr = JSON.parse(cleaned.slice(start, end + 1));
  const out = new Array(count).fill(null);
  for (const item of arr) {
    if (typeof item?.idx === 'number' && item.answer != null) {
      out[item.idx] = String(item.answer);
    }
  }
  return out;
}

async function askGemini(prompt, settings) {
  const model = settings.geminiModel || 'gemini-3.5-flash';
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${settings.geminiKey}`;
  const body = {
    contents: [{ parts: [{ text: prompt }] }],
    generationConfig: {
      temperature: 0.2,
      // Generous budget: gemini-2.5-* spend "thinking" tokens from this same
      // pool, so a small limit can leave zero room for the actual answer.
      maxOutputTokens: 8192,
      // Disable thinking for 2.5 models — we want the JSON answer, not
      // reasoning tokens eating the budget (was causing empty responses).
      thinkingConfig: { thinkingBudget: 0 },
    },
  };
  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new Error(`Gemini network error: ${e.message}`);
  }
  if (!res.ok) {
    // thinkingConfig is rejected by older/other models — retry once without it.
    const errTxt = (await res.text()).slice(0, 300);
    if (res.status === 400 && /thinking/i.test(errTxt)) {
      delete body.generationConfig.thinkingConfig;
      const res2 = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      if (!res2.ok) throw new Error(`Gemini ${res2.status}: ${(await res2.text()).slice(0, 200)}`);
      const d2 = await res2.json();
      const t2 = d2?.candidates?.[0]?.content?.parts?.map(p => p.text).join('') || '';
      if (!t2) throw new Error('Gemini returned empty response (retry)');
      return t2;
    }
    throw new Error(`Gemini ${res.status}: ${errTxt}`);
  }
  const data = await res.json();
  const cand = data?.candidates?.[0];
  const text = cand?.content?.parts?.map(p => p.text).join('') || '';
  if (!text) {
    const reason = cand?.finishReason || data?.promptFeedback?.blockReason || 'no text';
    throw new Error(`Gemini returned empty response (${reason})`);
  }
  return text;
}

async function askAnthropic(prompt, settings) {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': settings.anthropicKey,
      'anthropic-version': '2023-06-01',
      'anthropic-dangerous-direct-browser-access': 'true',
    },
    body: JSON.stringify({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 2048,
      messages: [{ role: 'user', content: prompt }],
    }),
  });
  if (!res.ok) throw new Error(`Anthropic ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const data = await res.json();
  return (data.content || []).map(b => b.text || '').join('');
}

// ── AI page navigator (vision-lite: reads the DOM, decides the next click) ──

function buildNavPrompt(page, context) {
  const cand = context.candidate || {};
  const resume = context.resume || {};
  const candSummary = [
    cand.name && `Name: ${cand.name}`,
    cand.email && `Email: ${cand.email}`,
    cand.phone && `Phone: ${cand.phone}`,
    cand.location && `Location: ${cand.location}`,
    cand.current_title && `Current title: ${cand.current_title}`,
    cand.current_company && `Current company: ${cand.current_company}`,
    cand.years_exp != null && `Years of experience: ${cand.years_exp}`,
    (cand.work_auth || resume.work_auth) && `Work authorization: ${cand.work_auth || resume.work_auth}`,
    cand.linkedin_url && `LinkedIn: ${cand.linkedin_url}`,
    (resume.skills && resume.skills.length) && `Skills: ${resume.skills.slice(0, 14).join(', ')}`,
    (cand.tech_stack && cand.tech_stack.length) && `Tech: ${[].concat(cand.tech_stack).slice(0, 14).join(', ')}`,
    resume.summary && `Summary: ${String(resume.summary).slice(0, 400)}`,
  ].filter(Boolean).join('\n');

  const moreBelow = (page.scrollMax || 0) - (page.scrollY || 0) > 40;

  return `You are an EXPERT autonomous agent whose one job is to COMPLETE AND SUBMIT a job application for a candidate on ANY website — including sites and layouts you have never seen. You get one page-observation at a time and choose the SINGLE next action that best moves toward a submitted application. Be decisive: read the whole page, infer what it wants, and act. Never stall when a reasonable action exists.

MISSION: get this application to a SUBMITTED / confirmation state, filling everything truthfully and favourably as the candidate. Every screen is a step toward that — figure out what THIS screen needs and do the one thing that advances it.

CANDIDATE (fill every field / answer every question AS this person; first person; professional; truthful but favourable):
${candSummary || context.candidateName || 'the candidate'}
Resume already uploaded this run: ${context.resumeUploaded ? 'yes' : 'no'}
${context.forceLogin ? 'NOTE: this candidate ALREADY HAS AN ACCOUNT here — sign in, do not register or apply as guest.' : ''}

PAGE URL: ${page.url}
HEADING: ${page.heading || page.title || ''}
SCROLL: at ${page.scrollY || 0}px of ${page.scrollMax || 0}px — ${moreBelow ? 'MORE CONTENT BELOW (you can scroll to reveal it)' : 'at the bottom'}
REQUIRED FIELDS STILL EMPTY: ${page.emptyRequired && page.emptyRequired.length ? page.emptyRequired.join(' | ') : 'none detected'}

VISIBLE PAGE TEXT (read it fully — it tells you what the page is: instructions, a form, terms to accept, a chat transcript, an error, a confirmation, a login/consent wall):
"""
${(page.pageText || '').slice(0, 3500)}
"""

INTERACTIVE ELEMENTS you may act on (refer to them by id). [required]=must be filled, [DISABLED]=not clickable yet:
${page.elements.map(e => `${e.id}: <${e.tag}${e.type ? ' ' + e.type : ''}> "${(e.text || '').slice(0, 80)}"${e.value ? ` (value: "${String(e.value).slice(0, 40)}")` : ''}${e.required ? ' [required]' : ''}${e.disabled ? ' [DISABLED]' : ''}`).join('\n').slice(0, 5000)}

HOW TO DECIDE (apply in order):
1. Is the application already SUBMITTED / a thank-you or confirmation page? → "done".
2. Is something blocking that only a human can pass — a CAPTCHA / "I'm not a robot", an SMS or email verification code, a social/SSO-ONLY login, an identity/document upload, or a payment? → "human".
3. Is a cookie/consent/privacy popup or modal covering the page? → dismiss it: click "Accept"/"Accept all"/"OK"/"Got it" (or "Reject non-essential" if that also closes it) so the real content is usable.
4. Is there an OPEN dialog/modal that IS the application step (a form or message inside it)? → work INSIDE it; never click a background "Apply" behind an open modal.
5. Does the page need reading/scrolling before the action appears (long terms, or the forward button is below the fold / [DISABLED])? → "scroll".
6. Are there empty [required] fields? → "fill" them one at a time with the candidate's data (or a sensible favourable answer). Fill before advancing.
7. Otherwise advance: click the primary FORWARD control (Apply / Continue / Next / Save & Continue / Review / Submit / Send / Accept / Agree).
8. Nothing actionable yet (spinner/transition)? → "wait".

PAGE-TYPE PLAYBOOK (recognise the pattern, take the move):
- LANDING / job description with an Apply button → click Apply/Apply Now/Start Application (the direct one; NOT "Apply with Indeed", NOT a social login).
- LOGIN / ACCOUNT WALL → prefer, in order: an existing-account SIGN IN (if this candidate has an account / you see "already registered"), else CREATE ACCOUNT / New user, else an email+password path. AVOID "Apply as Guest" (it often just emails credentials to finish later). Social/SSO-only (Google/Apple/LinkedIn/Microsoft) → "human". Fill email/username with the candidate's email; the password is filled by the extension, not you.
- MULTI-STEP FORM (progress bar / "Step 2 of 5") → fill this step's required fields, then Continue/Next. Don't jump around.
- TERMS / PRIVACY / E-SIGNATURE GATE → SCROLL to the very bottom first (repeat "scroll" until SCROLL says you're at the bottom); a [DISABLED] Accept/Continue unlocks after scrolling — only click it once it's enabled. Never type into the accept control.
- CHAT / CONVERSATIONAL APPLY (a bot/recruiter asks questions in a thread with a message box) → find the LATEST unanswered question in the page text, "fill" the message box with a good answer from the candidate data, "submit": true to send. Don't repeat an answer already given; if there's no new question yet, "wait".
- RESUME / FILE UPLOAD step → if a resume is already uploaded this run, just Continue; the extension handles the actual file — click the upload/attach control to reveal the file input, then advance.
- DROPDOWN / SEARCHABLE SELECT (a combobox, "Search"/type-to-filter box, or "Select One" button — country, phone code, state, gender, race/ethnicity, "how did you hear") → "click" the control to OPEN it, then on the next tick "click" the correct OPTION ROW from the list (do NOT type long text into it and do NOT re-open a dropdown you already opened). For a country/phone-code list pick the real "United States of America" — NOT a look-alike like "American Samoa" or "United States Minor Outlying Islands". For race/ethnicity tick "Asian" (never Caucasian). Once the option is chosen and shows selected, MOVE ON — never keep re-selecting the same dropdown.
- SKILLS multiselect (a box that holds several "pills"/tags, e.g. Workday Skills) → add a FEW relevant skills from the candidate's Skills list ONE AT A TIME: "fill" the box with a single skill, then "click" the matching suggestion to add its pill; repeat for 3-6 skills, then advance. Do not paste all skills as one string; do not loop once several pills exist.
- REPEATABLE SECTION with an "Add" button (Work Experience, Education, Certifications) → if the sub-form fields aren't visible yet, "click" the "Add" button under that heading FIRST, then fill the revealed fields (School/Degree/Field of Study for Education; Title/Company/Dates for Experience) from the candidate's resume. Don't skip a required section just because it starts collapsed.
- SCREENING / KNOCKOUT QUESTIONS → answer to keep the candidate ELIGIBLE and favourable (see fixed answers below). Never leave one blank.
- VALIDATION ERROR shown ("please select an option", "enter a valid …", "required") → fix exactly that field (choose a valid option / correct the format), then re-submit.
- ERROR / DEAD-END page (404, "job no longer available", "session expired") → "human".

ACTIONS (return exactly one):
- "click" (id): press a button/link/checkbox/radio — Apply, Continue, Next, Submit, Accept, a radio option, a dropdown opener, a cookie-accept, etc.
- "fill" (id, value): type into an input/textarea/contenteditable/chat box. Compose the value from the candidate data. Add "submit": true ONLY when it's a CHAT message to send immediately; for an ordinary form field omit/false.
- "scroll": reveal more content / read a long section / expose or enable a button that's further down.
- "done": application is clearly submitted/received/confirmed.
- "human": captcha, verification code, social/SSO-only login, ID/payment, or a genuine dead-end.
- "wait": a loader/transition — nothing to do this tick.

HARD RULES:
- NEVER click ads, cookie "manage/settings" rabbit-holes, nav menus, Back, Cancel, "Save draft", social/SSO login, unrelated job links, or "Apply with Indeed" on a company site.
- NEVER apply-as-guest when a sign-in or create-account option exists; if the candidate already has an account, SIGN IN (never re-register).
- NEVER leave a required field or dropdown on a placeholder ("Please Select" / "Select One" / "Not Specified" / "--" / blank) — always choose a real valid option; if unsure, the most reasonable one, and for legal/eligibility questions the one that keeps the candidate eligible.
- FIXED ANSWERS (apply whenever asked): authorized to work in the job's country without restrictions = Yes; visa sponsorship needed now or in future = No; held a US Government security clearance in the last 2 years = No; willing to relocate/commute/work on-site = Yes; available to start = immediately.
- NEVER invent employment history not in the candidate data; for a company-specific history question they have no relation to, answer "N/A" or "No".
- Don't repeat an action that already failed to change the page — if your last click/scroll did nothing, choose a DIFFERENT control or "human". NEVER re-open or re-fill the SAME dropdown/field twice in a row: if it already shows a selected value or a pill, treat it as done and move to the next empty control.
- When two controls could advance, pick the one that goes FORWARD toward submit.

Respond with ONLY this JSON (no markdown, no prose, no code fence):
{"action":"click|fill|scroll|done|human|wait","id":"<element id or null>","value":"<text if action is fill, else null>","submit":<true only for a chat message to send, else false>,"reason":"<short why>"}`;
}

function parseNavDecision(text) {
  const cleaned = text.replace(/```(json)?/gi, '').trim();
  const s = cleaned.indexOf('{'), e = cleaned.lastIndexOf('}');
  if (s === -1 || e === -1) throw new Error('AI nav returned no JSON');
  const obj = JSON.parse(cleaned.slice(s, e + 1));
  return {
    action: String(obj.action || 'wait').toLowerCase(),
    id: obj.id || null,
    value: obj.value != null ? String(obj.value) : null,
    submit: obj.submit === true || obj.submit === 'true',
    reason: obj.reason || '',
  };
}

/**
 * Ask the AI which single element to click next on an unknown page.
 * @returns {action, id, reason}
 */
export async function aiDecideAction(page, context, settings) {
  const prompt = buildNavPrompt(page, context);
  let lastErr = null;
  if (settings.geminiKey) {
    try { return parseNavDecision(await askGemini(prompt, settings)); }
    catch (e) { lastErr = e; }
  }
  if (settings.anthropicKey) {
    try { return parseNavDecision(await askAnthropic(prompt, settings)); }
    catch (e) { lastErr = e; }
  }
  throw lastErr || new Error('No AI key configured for navigation');
}

/**
 * Answer a batch of screening questions with AI.
 * @returns array (same length as questions) of answer strings or null.
 */
export async function aiAnswerBatch(questions, context, settings) {
  if (!questions.length) return [];
  const prompt = buildPrompt(questions, context);
  let lastErr = null;
  if (settings.geminiKey) {
    try {
      return parseAnswers(await askGemini(prompt, settings), questions.length);
    } catch (e) { lastErr = e; }
  }
  if (settings.anthropicKey) {
    try {
      return parseAnswers(await askAnthropic(prompt, settings), questions.length);
    } catch (e) { lastErr = e; }
  }
  if (lastErr) throw lastErr;
  throw new Error('No AI API key configured (set a Gemini key in the popup settings)');
}
