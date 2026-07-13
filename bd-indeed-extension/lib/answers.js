// Answer pipeline for form fields scraped off an Indeed page.
// Resolution order per field:
//   1. Deterministic profile heuristics (name, phone, address, work auth, ...)
//   2. Operator demographic policy (option scan)
//   3. Persisted answer memory (previously used answers for this candidate)
//   4. AI (Gemini/Anthropic) with the candidate's parsed resume as context
// Fields left unresolved return answer=null; the content script leaves
// optional ones blank and flags required ones for the human BD agent.

import { aiAnswerBatch } from './ai.js';

const norm = s => String(s || '').toLowerCase().replace(/\s+/g, ' ').replace(/[*:]+$/g, '').trim();

// ── answer memory (the "field memory" learning layer) ───────────────────────
//
// Every field answered — by AI or by the human when they fill a flagged field —
// is cached here keyed by `${host}::${normalizedLabel}`. Lookups try the exact
// host first, then any host with the same label (cross-site reuse), so once a
// field like "Years of Python experience" is answered on ANY site, Gemini never
// has to answer it again. A separate short "unknown" quality guard skips
// placeholder-y labels so we don't cache junk keys.

async function loadMemory(candidateId) {
  const key = `answers:${candidateId}`;
  const data = await chrome.storage.local.get(key);
  return data[key] || {};
}

const PLACEHOLDER_LABEL_RE = /^(please\s+select|select|choose|--+|—+|pick|n\/?a|)$/i;
const memKey = (host, label) => `${host || '*'}::${norm(label)}`;

/** Look up a remembered answer: exact host first, then any host (cross-site). */
function memLookup(mem, host, label) {
  const l = norm(label);
  if (!l || PLACEHOLDER_LABEL_RE.test(l)) return null;
  const exact = mem[memKey(host, label)];
  if (exact) return exact;
  // cross-site: any key ending with `::<label>` (prefer most recent)
  let best = null;
  for (const [k, v] of Object.entries(mem)) {
    if (k.endsWith(`::${l}`) && (!best || (v.ts || 0) > (best.ts || 0))) best = v;
  }
  return best;
}

export async function rememberAnswers(candidateId, entries, host) {
  if (!entries || !entries.length) return;
  const key = `answers:${candidateId}`;
  const mem = await loadMemory(candidateId);
  for (const { label, answer, source } of entries) {
    const l = norm(label);
    if (!l || PLACEHOLDER_LABEL_RE.test(l) || answer == null || answer === '') continue;
    mem[memKey(host, label)] = { answer, source, ts: Date.now() };
  }
  await chrome.storage.local.set({ [key]: mem });
}

// ── option matching ────────────────────────────────────────────────────────

/** Pick the option that best matches `wanted` (returns the option verbatim, or null). */
export function matchOption(options, wanted) {
  if (!options || !options.length || wanted == null) return null;
  const w = norm(wanted);
  const normed = options.map(o => ({ raw: o, n: norm(o) }));
  let hit = normed.find(o => o.n === w);
  if (hit) return hit.raw;
  hit = normed.find(o => o.n.startsWith(w) || w.startsWith(o.n));
  if (hit) return hit.raw;
  hit = normed.find(o => o.n.includes(w) || w.includes(o.n));
  if (hit) return hit.raw;
  // token overlap fallback
  const wTokens = new Set(w.split(/\W+/).filter(t => t.length > 2));
  let best = null, bestScore = 0;
  for (const o of normed) {
    const score = o.n.split(/\W+/).filter(t => wTokens.has(t)).length;
    if (score > bestScore) { best = o.raw; bestScore = score; }
  }
  return bestScore > 0 ? best : null;
}

function scanOptions(options, ...phraseSets) {
  // phraseSets: arrays tried in priority order; each is a list of substrings.
  if (!options || !options.length) return null;
  const normed = options.map(o => ({ raw: o, n: norm(o) }));
  for (const phrases of phraseSets) {
    for (const p of phrases) {
      const hit = normed.find(o => o.n.includes(p));
      if (hit) return hit.raw;
    }
  }
  return null;
}

function scanOptionsRe(options, ...regexes) {
  // Like scanOptions but with word boundaries — needed where substring
  // matching lies (e.g. "asian" is inside "Caucasian").
  if (!options || !options.length) return null;
  for (const re of regexes) {
    const hit = options.find(o => re.test(norm(o)));
    if (hit) return hit;
  }
  return null;
}

// ── deterministic heuristics ───────────────────────────────────────────────

// Produce a phone value Indeed accepts. Indeed's phone field validates the
// number and rejects a stray "+1 " prefix ("Add a valid phone number"). For
// US-shaped numbers we output the 10-digit national number as XXX-XXX-XXXX
// (the country-code selector, when present, supplies the +1). Non-US numbers
// are returned digit-cleaned but otherwise intact.
function normalizePhone(phone /*, field */) {
  const raw = String(phone || '').trim();
  if (!raw) return '';
  const digits = raw.replace(/\D/g, '');
  // US-candidate product: the national number is the last 10 digits, whatever
  // (wrong) country code the stored value carries (+1, +49, none). Output the
  // clean US format XXX-XXX-XXXX. <10 digits → can't format, return as-is.
  if (digits.length >= 10) {
    const nat = digits.slice(-10);
    return `${nat.slice(0, 3)}-${nat.slice(3, 6)}-${nat.slice(6)}`;
  }
  return digits || raw;
}

// Today's date as YYYY-MM-DD — the value format a native <input type="date">
// requires. Used for "start date" = immediately.
function todayISO() {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${mm}-${dd}`;
}

// Normalize a resume date string to MM/YYYY (Workday work-experience pickers).
// Handles "2020-01", "01/2020", "Jan 2020", "January 2020", "2020". Returns
// null for Present/current/unparseable.
function formatMonthYear(s) {
  if (!s) return null;
  const str = String(s).trim();
  if (/present|current|now|ongoing/i.test(str)) return null;
  let m = str.match(/(\d{4})[-/.](\d{1,2})/);           // YYYY-MM
  if (m) return `${String(m[2]).padStart(2, '0')}/${m[1]}`;
  m = str.match(/\b(\d{1,2})[-/.](\d{4})\b/);            // MM/YYYY
  if (m) return `${String(m[1]).padStart(2, '0')}/${m[2]}`;
  const MON = { jan: '01', feb: '02', mar: '03', apr: '04', may: '05', jun: '06', jul: '07', aug: '08', sep: '09', oct: '10', nov: '11', dec: '12' };
  m = str.match(/([a-z]{3,})\.?\s+(\d{4})/i);            // Month YYYY
  if (m && MON[m[1].slice(0, 3).toLowerCase()]) return `${MON[m[1].slice(0, 3).toLowerCase()]}/${m[2]}`;
  m = str.match(/^(\d{4})$/);                             // bare year
  if (m) return `01/${m[1]}`;
  return null;
}

function splitName(fullName) {
  const parts = String(fullName || '').trim().split(/\s+/);
  return { first: parts[0] || '', last: parts.length > 1 ? parts[parts.length - 1] : '' };
}

// US state name → abbreviation (for parsing "City, State" strings).
const US_STATES = {
  alabama:'AL',alaska:'AK',arizona:'AZ',arkansas:'AR',california:'CA',colorado:'CO',
  connecticut:'CT',delaware:'DE',florida:'FL',georgia:'GA',hawaii:'HI',idaho:'ID',
  illinois:'IL',indiana:'IN',iowa:'IA',kansas:'KS',kentucky:'KY',louisiana:'LA',
  maine:'ME',maryland:'MD',massachusetts:'MA',michigan:'MI',minnesota:'MN',
  mississippi:'MS',missouri:'MO',montana:'MT',nebraska:'NE',nevada:'NV',
  'new hampshire':'NH','new jersey':'NJ','new mexico':'NM','new york':'NY',
  'north carolina':'NC','north dakota':'ND',ohio:'OH',oklahoma:'OK',oregon:'OR',
  pennsylvania:'PA','rhode island':'RI','south carolina':'SC','south dakota':'SD',
  tennessee:'TN',texas:'TX',utah:'UT',vermont:'VT',virginia:'VA',washington:'WA',
  'west virginia':'WV',wisconsin:'WI',wyoming:'WY','district of columbia':'DC',
};
const STATE_ABBR_TO_NAME = Object.fromEntries(
  Object.entries(US_STATES).map(([name, abbr]) => [abbr, name.replace(/\b\w/g, c => c.toUpperCase())]));

// Parse a free-form "City, ST" / "City, State, Country" string into parts.
function parseLocationString(s) {
  const str = String(s || '').trim();
  if (!str || str.length < 3) return { city: null, state: null };
  // Drop a trailing country (", United States"/", USA").
  const parts = str.split(',').map(p => p.trim()).filter(Boolean)
    .filter(p => !/^(usa|us|united states( of america)?)$/i.test(p));
  if (!parts.length) return { city: null, state: null };
  const city = parts[0] || null;
  let state = null;
  if (parts[1]) {
    const p1 = parts[1];
    if (/^[A-Za-z]{2}$/.test(p1)) state = p1.toUpperCase();
    else state = US_STATES[p1.toLowerCase()] || p1;
  }
  return { city, state };
}

function parseLocation(profile) {
  // candidate.location is a free string ("Lahore, Pakistan", "Austin, TX", "US").
  // Parsed resume contact info (module3 parser output) is preferred when present.
  const r = profile.resume || {};
  const contact = r.contact || r.contact_info || r.personal || r.basics || {};
  // Resume parsers store location in wildly different shapes — check many.
  const rawLocStr = contact.location || contact.address || r.location
    || (contact.city && contact.state ? `${contact.city}, ${contact.state}` : '')
    || profile.location || '';
  const fromStr = parseLocationString(rawLocStr);
  const city = contact.city || fromStr.city || null;
  const state = contact.state || contact.region || fromStr.state || null;
  return {
    city,
    state,
    zip: contact.zip || contact.postal_code || contact.zipcode || contact.postalCode || null,
    street: contact.street || (typeof contact.address === 'string' ? contact.address : null) || null,
    cityState: (city && state) ? `${city}, ${state}` : (city || null),
  };
}

const YES = 'Yes';
const NO = 'No';

/**
 * Try to answer one field deterministically. Returns {answer, source} or null.
 */
function heuristicAnswer(field, profile, settings) {
  const q = norm(field.label);
  // Label + machine hint (DOM id / automation-id). Substring matching consults
  // BOTH so fields whose visible label is missing/ambiguous but whose id encodes
  // the meaning (e.g. Workday "phoneNumber--countryPhoneCode") are still
  // recognised. Exact-match checks below stay on `q` (the label) only.
  const qh = norm(`${field.label || ''} ${field.hint || ''}`);
  const opts = field.options || [];
  const { first, last } = splitName(profile.name);
  const loc = parseLocation(profile);

  const has = (...subs) => subs.some(s => qh.includes(s));

  // Account password (create-account, confirm/verify password, and sign-in on
  // job portals). Use the STRONG default portal password — the candidate's gmail
  // password usually fails portal complexity rules (needs upper/lower/digit/
  // special/8+, e.g. Workday/iCIMS). The same value is reused everywhere, so an
  // account created with it can be signed into later. Falls back to the
  // candidate's site password only if no portal default is configured.
  if (has('password', 'passcode') && !has('forgot', 'reset link', 'hint', 'question')) {
    const pw = settings.portalPassword || profile.portalPassword || profile.sitePassword;
    return pw ? { answer: pw, source: 'profile' } : null; // no pw anywhere → human
  }
  // Account username / login id. The label often has a SPACE ("User Name") or an
  // alias ("Login ID", "Account Name"), and ATS pages usually say "do not use
  // spaces, recommend email address" — so ALWAYS use the candidate's login email,
  // never their person-name (which has spaces and breaks the account). Checked
  // before the name rules so "User Name" can't fall through to the full-name rule.
  if (has('username', 'user name', 'user id', 'userid', 'user-id', 'login id', 'loginid',
          'login name', 'login-id', 'account name', 'sign in name', 'sign-in name', 'signin name')
      && field.type !== 'radio' && field.type !== 'select' && field.type !== 'listbox') {
    const u = profile.siteLoginEmail || profile.email || '';
    // Usernames must not contain spaces on these forms.
    return u ? { answer: String(u).replace(/\s+/g, ''), source: 'profile' } : null;
  }

  // Contact / identity
  if (has('first name', 'given name')) return { answer: first, source: 'profile' };
  if (has('last name', 'surname', 'family name')) return { answer: last, source: 'profile' };
  if (q === 'name' || has('full name', 'legal name', 'your name')) return { answer: profile.name, source: 'profile' };
  if (has('email')) {
    // On a login/signup wall the account is the candidate's gmail; on a normal
    // application form the contact email is their primary email.
    if (profile._isLoginForm && (profile.siteLoginEmail)) {
      return { answer: profile.siteLoginEmail, source: 'profile' };
    }
    return { answer: profile.email, source: 'profile' };
  }
  // Country phone / dialing code (Workday) → United States (+1). MUST be
  // before the phone rule and the country rule.
  if (has('country phone code', 'phone country code', 'phone code', 'dialing code', 'dial code') || (has('country code') && has('phone'))) {
    const o = scanOptions(opts, ['united states of america'], ['united states'], ['+1'], ['usa']);
    return { answer: o || 'United States of America (+1)', source: 'policy' };
  }
  if ((has('phone', 'mobile', 'cell')) && !has('device', 'type', 'country code', 'extension')) {
    // Prefer the candidate record; fall back to the parsed resume's contact
    // phone so a missing candidate.phone doesn't leave the field blank.
    const rc = (profile.resume && (profile.resume.contact || profile.resume.contact_info
      || profile.resume.personal || profile.resume.basics)) || {};
    const rawPhone = profile.phone || profile.mobile || profile.phone_number
      || rc.phone || rc.phone_number || rc.mobile || rc.cell || rc.telephone
      || (profile.resume && profile.resume.phone) || '';
    const p = normalizePhone(rawPhone);
    // No phone on file anywhere → return null so we don't clear a prefilled
    // value or submit an empty required field (leave it for the human).
    return p ? { answer: p, source: 'profile' } : null;
  }
  if (has('linkedin')) return { answer: profile.linkedin_url || 'N/A', source: 'profile' };

  // Address — operator policy: represented candidates apply as US-based.
  // Any country field (country, current country, country of residence,
  // preferred work countries) → United States. This is a hard policy rule so
  // sites that geo-default to another country (e.g. Germany) get corrected.
  // BUT skip when "country" is just part of a legal/eligibility question
  // ("authorized to work in the country…", "sponsorship to work in the
  // country…") — those are Yes/No policy answers handled further below, not a
  // country selection.
  if (has('country', 'countries')
      && !has('authoriz', 'sponsor', 'sponsorship', 'eligible', 'legally', 'clearance', 'restriction', 'right to work', 'require')) {
    const o = scanOptions(opts, ['united states of america'], ['united states'], ['usa'], ['us']);
    // Full name so it exact-matches "United States of America" and never the
    // decoy "United States Minor Outlying Islands".
    return { answer: o || 'United States of America', source: 'policy' };
  }
  // Phone device type (Workday) — default Mobile. MUST come before the state
  // rule and is already before phone won't catch it (phone rule excludes it).
  if (has('phone device', 'device type', 'phone type')) {
    const o = scanOptions(opts, ['mobile'], ['cell'], ['personal']);
    return { answer: o || 'Mobile', source: 'policy' };
  }
  // State / province — from the candidate's resume/profile location. Uses word
  // boundaries so "state" does NOT match inside "United States". Returns the
  // FULL state name (Workday/most dropdowns list "New York", not "NY"); text
  // fields accept it equally. Guard against the VERB "state" ("please state your
  // salary", "state whether…") and against non-location questions that merely
  // contain the word — otherwise "Please state your salary expectation" was
  // answered with the candidate's state (New York).
  if (/\b(state|province|region)\b/.test(q)
      && !has('statement', 'united state')
      && !/(please\s+state|state\s+(your|the|whether|if|any|below|above|briefly|clearly|in|how|why|what|which))/.test(q)
      && !has('salary', 'compensation', 'expectation', 'expected pay', 'pay ', 'reason', 'describe', 'explain')) {
    if (loc.state) {
      const full = STATE_ABBR_TO_NAME[String(loc.state).toUpperCase()] || loc.state;
      return { answer: full, source: 'profile' };
    }
    return null; // unknown → AI/human, never guess a state
  }
  // English / language proficiency — represented candidates are fluent.
  if (has('english level', 'english proficiency', 'level of english', 'language proficiency', 'proficiency in english', 'fluency')) {
    const o = scanOptions(opts, ['native'], ['fluent'], ['full professional', 'professional working'], ['advanced'], ['c2'], ['c1']);
    return { answer: o || 'Fluent', source: 'policy' };
  }
  // Preferred display language (e.g. Taleo "Select a language") → English.
  if (has('select a language', 'preferred language', 'display language') || q === 'language') {
    const o = scanOptions(opts, ['english']);
    if (o) return { answer: o, source: 'policy' };
  }
  if (has('postal code', 'postcode', 'zip')) {
    return loc.zip ? { answer: loc.zip, source: 'profile' } : null;
  }
  // Address Line 2 (apartment / suite / unit) — leave BLANK rather than
  // duplicating the street. MUST be checked before the Line-1 rule because
  // "address line 2" also contains the substring "address line".
  if (has('address line 2', 'address line2', 'address 2', 'apartment', 'suite')) {
    return loc.street2 ? { answer: loc.street2, source: 'profile' } : null;
  }
  if (has('street address', 'address line', 'address line 1', 'address line1')) {
    return loc.street ? { answer: loc.street, source: 'profile' } : null;
  }
  if (has('city, state', 'city, province', 'city and state', 'city/state')) {
    return loc.cityState ? { answer: loc.cityState, source: 'profile' } : null;
  }
  if (q === 'city' || has('what city', 'current city', 'city or town', 'town')) {
    // City part of the candidate's Supabase location column, else resume city,
    // else the default's city part.
    const profLoc = String(profile.location || '').trim();
    if (profLoc) return { answer: profLoc.split(',')[0].trim() || profLoc, source: 'profile' };
    if (loc.city) return { answer: loc.city, source: 'profile' };
    const dl = (settings && settings.defaultLocation) || 'United States';
    return { answer: dl.split(',')[0].trim() || dl, source: 'policy' };
  }
  // "Which location/office are you applying for?" is a picker of the JOB's
  // office locations — NOT the candidate's location. Prefer Remote, else first.
  if (has('which location', 'location are you applying', 'office location', 'preferred office', 'work location')) {
    const o = scanOptions(opts, ['remote'], ['any'], ['flexible']);
    if (o) return { answer: o, source: 'policy' };
    if (opts.length) return { answer: opts[0], source: 'policy' };
    // free text → candidate's own location
    return loc.cityState ? { answer: loc.cityState, source: 'profile' } : null;
  }
  // The candidate's own current location — "current location", "location",
  // "current city", "hometown", etc. all resolve the same way: from the resume
  // (parser now extracts it) or the candidate's Supabase location, and — so it
  // NEVER blocks — a configurable default (settings.defaultLocation) as the
  // final fallback.
  // "Are you (currently) based in / located in the US?" as a YES/NO question →
  // Yes (US-based policy). MUST precede the location-TEXT rule below, which also
  // matches "based in" but returns a city/country string that can't fill a
  // Yes/No chip. Detect yes/no by the options or an interrogative label.
  const _isYesNo = field.type === 'buttongroup'
    || (opts.length > 0 && opts.every(o => /^(yes|no|y|n)$/i.test(norm(o))))
    || /^(are|do|does|is|have|has|can|will|would|did)\b/.test(q);
  if (_isYesNo && /\b(based|located|residing|reside|living|live|currently)\b/.test(q)
      && /\b(u\.?s\.?a?|united states|country|america|here|this country)\b/.test(q)) {
    return { answer: matchOption(opts, YES) || YES, source: 'policy' };
  }
  if (has('current location', 'where are you located', 'where do you live', 'city, state', 'location', 'hometown', 'based in')) {
    // 1. The candidate's Supabase `location` column, verbatim (source of truth).
    const profLoc = String(profile.location || '').trim();
    if (profLoc) return { answer: profLoc, source: 'profile' };
    // 2. Location parsed from the resume.
    const realCityState = (loc.city && loc.state) ? `${loc.city}, ${loc.state}`
      : (loc.cityState && /,/.test(loc.cityState)) ? loc.cityState
      : loc.city || null;
    if (realCityState) return { answer: realCityState, source: 'profile' };
    // 3. Configured default — never blocks.
    return { answer: (settings && settings.defaultLocation) || 'United States', source: 'policy' };
  }

  // Work authorization / eligibility
  if (has('authorized to work', 'legally authorized', 'eligible to work', 'legal right to work', 'work authorization')) {
    const yes = (profile.work_auth || 'us_authorized') === 'us_authorized';
    const raw = yes ? YES : NO;
    return { answer: matchOption(opts, raw) || raw, source: 'policy' };
  }
  if (has('sponsorship', 'sponsor')) {
    return { answer: matchOption(opts, NO) || NO, source: 'policy' };
  }
  // Security clearance held (past 2 years or any) — operator policy: No.
  // (NAC / Confidential / Secret / Top Secret / TS-SCI etc.)
  if (has('security clearance', 'clearance granted', 'held a clearance', 'active clearance',
          'government clearance', 'clearance level') || (has('clearance') && !has('obtain', 'willing', 'able to get'))) {
    return { answer: matchOption(opts, NO) || NO, source: 'policy' };
  }
  if (has('18 years', 'at least 18', 'over 18', 'age of 18')) {
    return { answer: matchOption(opts, YES) || YES, source: 'policy' };
  }
  if (has('background check', 'drug test', 'drug screen', 'submit verification', 'e-verify', 'everify')) {
    return { answer: matchOption(opts, YES) || YES, source: 'policy' };
  }
  if (has('commute', 'relocate', 'relocation', 'work on-site', 'work onsite', 'work in person', 'in-person')) {
    return { answer: matchOption(opts, YES) || YES, source: 'policy' };
  }

  // Availability / start date — operator policy: ALWAYS immediately. But NEVER
  // for a WORK-EXPERIENCE / employment start date (that's a past date from the
  // résumé, handled below) — otherwise it wrongly fills today's date.
  if ((has('how soon', 'when can you start', 'when could you start', 'start date', 'available to start',
          'availability to start', 'available start', 'earliest start', 'date available',
          'availability date', 'when are you available', 'preferred start', 'notice period',
          'availability', 'start immediately'))
      && !has('work experience', 'employment', 'prior ', 'previous ', 'job history', 'this role', 'this position', 'this job')) {
    // Dropdown/radio: pick the soonest option (immediately > asap > 1 week > …).
    const o = scanOptions(opts,
      ['immediately', 'immediate'], ['asap', 'as soon as possible', 'right away'],
      ['1 week', 'one week', 'within a week'], ['2 week', 'two week']);
    if (o) return { answer: o, source: 'policy' };
    // Date input: "immediately" = today's date.
    if (field.type === 'date') return { answer: todayISO(), source: 'policy' };
    // Notice-period number field (e.g. "days of notice") → 0 = immediate.
    if (field.type === 'number') return { answer: '0', source: 'policy' };
    return { answer: 'Immediately', source: 'policy' };
  }
  if (has('hours per week', 'hours/week', 'desired hours', 'weekly hours')) {
    return { answer: String(settings.desiredHours || '40'), source: 'policy' };
  }
  if (has('salary', 'compensation', 'desired pay', 'expected pay', 'pay expectation', 'base pay')) {
    const digits = String(settings.desiredSalary || '85000').replace(/[^\d]/g, '');
    return { answer: field.type === 'number' ? digits : digits, source: 'policy' };
  }
  if (has('how did you hear', 'hear about', 'referral source', 'how were you referred')) {
    const o = scanOptions(opts, ['linkedin'], ['job board'], ['other']);
    return { answer: o || 'LinkedIn', source: 'policy' };
  }

  // Skills field → the candidate's resume skills (never "N/A").
  if ((has('skills', 'key skills', 'technical skills', 'core competencies', 'areas of expertise', 'relevant skills', 'skill set'))
      && !has('describe', 'soft skill')) {
    const sk = (Array.isArray(profile.resume && profile.resume.skills) && profile.resume.skills.length)
      ? profile.resume.skills : (Array.isArray(profile.tech_stack) ? profile.tech_stack : []);
    if (sk.length) return { answer: sk.slice(0, 12).join(', '), source: 'profile' };
  }

  // ── Work-experience sub-form (Workday "My Experience", Greenhouse, etc.) ──
  // Filled from the candidate's most-recent parsed resume experience entry.
  // Resume shapes vary (snake_case / camelCase / different array names) — read
  // flexibly so Workday work-experience fields actually fill.
  const _pick = (o, ...keys) => { for (const k of keys) { if (o && o[k] != null && o[k] !== '') return o[k]; } return null; };
  const _expRaw = profile.resume && (profile.resume.experience || profile.resume.work_experience
    || profile.resume.workExperience || profile.resume.employment || profile.resume.jobs || profile.resume.positions);
  const _exp = Array.isArray(_expRaw) ? _expRaw : [];
  const _job = _exp[0] || {};
  const _jobTitle = _pick(_job, 'title', 'job_title', 'jobTitle', 'position', 'role', 'designation');
  const _jobCompany = _pick(_job, 'company', 'company_name', 'companyName', 'employer', 'organization', 'organisation');
  let _jobDesc = _pick(_job, 'description', 'roleDescription', 'role_description', 'summary', 'responsibilities', 'details', 'text');
  if (!_jobDesc) { const b = _job.bullets || _job.highlights || _job.achievements; if (Array.isArray(b) && b.length) _jobDesc = b.join('. '); }
  const _jobStart = _pick(_job, 'start_date', 'startDate', 'from', 'start', 'start_month', 'startMonth', 'from_date');
  const _jobEnd = _pick(_job, 'end_date', 'endDate', 'to', 'end', 'end_month', 'endMonth', 'to_date');

  if (has('job title', 'position title', 'your title', 'role title') || q === 'title') {
    const t = _jobTitle || profile.current_title;
    if (t) return { answer: t, source: 'profile' };
  }
  if (has('company name', 'employer name', 'organization name', 'current company')
      || (has('company', 'employer') && !has('why', 'cover', 'reason'))) {
    const c = _jobCompany || profile.current_company;
    if (c) return { answer: c, source: 'profile' };
  }
  if (has('role description', 'job description', 'responsibilities', 'description of your', 'summary of your role', 'duties', 'description')) {
    if (_jobDesc) return { answer: String(_jobDesc).replace(/\s+/g, ' ').slice(0, 700), source: 'profile' };
    // No parsed description → let AI write one from the resume/title.
    return null;
  }
  if (has('currently work here', 'i currently work', 'current position', 'present role', 'current role')) {
    const cur = !_jobEnd || /present|current/i.test(String(_jobEnd));
    return { answer: cur ? YES : NO, source: 'policy' };
  }
  if (q === 'from' || has('start date', 'from date', 'date from', 'employment start', 'started', 'start month')) {
    const d = formatMonthYear(_jobStart);
    if (d) return { answer: d, source: 'profile' };
  }
  if ((q === 'to' || has('end date', 'to date', 'date to', 'employment end', 'ended', 'end month')) && !has('willing', 'able to', 'authorized')) {
    if (!_jobEnd || /present|current/i.test(String(_jobEnd))) return { answer: 'Present', source: 'policy' };
    const d = formatMonthYear(_jobEnd);
    if (d) return { answer: d, source: 'profile' };
  }

  // ── Education sub-form (Workday "My Experience" → Education, etc.) ──────────
  // Filled from the candidate's most-recent parsed education entry (shapes vary).
  const _eduRaw = profile.resume && (profile.resume.education || profile.resume.educations
    || profile.resume.schools || profile.resume.academics || profile.resume.qualifications);
  const _eduArr = Array.isArray(_eduRaw) ? _eduRaw : [];
  const _edu = _eduArr[0] || {};
  const _school = _pick(_edu, 'school', 'institution', 'university', 'college', 'school_name', 'schoolName', 'name', 'organization');
  const _degree = _pick(_edu, 'degree', 'degree_name', 'degreeName', 'qualification', 'level', 'diploma');
  const _major = _pick(_edu, 'field_of_study', 'fieldOfStudy', 'major', 'field', 'discipline', 'concentration', 'specialization', 'study');
  const _eduEnd = _pick(_edu, 'end_date', 'endDate', 'graduation', 'graduation_date', 'graduationDate', 'year', 'completed', 'to', 'end');
  const _eduStart = _pick(_edu, 'start_date', 'startDate', 'from', 'start');
  // Only answer education fields when they're clearly education (guard against the
  // work "role" fields and generic "name"/"from"/"to" already handled above).
  if (has('school', 'university', 'institution', 'college')) {
    if (_school) return { answer: _school, source: 'profile' };
  }
  if (has('degree') && !has('do you have a degree', 'require a degree', 'have a degree')) {
    if (_degree) return { answer: _degree, source: 'profile' };
  }
  if (has('field of study', 'major', 'discipline', 'concentration', 'area of study')) {
    if (_major) return { answer: _major, source: 'profile' };
  }
  if (has('graduation', 'graduation date', 'graduation year', 'year of graduation', 'completion date', 'degree completion')) {
    const d = formatMonthYear(_eduEnd) || (String(_eduEnd || '').match(/\d{4}/) || [])[0];
    if (d) return { answer: d, source: 'profile' };
  }

  // Disqualifying / negative questions → No (before the favorable-Yes rule).
  if (has('felony', 'convicted', 'criminal record', 'been terminated', 'fired for', 'disciplinary action', 'non-compete', 'noncompete')) {
    return { answer: matchOption(opts, NO) || NO, source: 'policy' };
  }

  // Favorable default for experience / ability / skill / willingness questions
  // ("Do you have experience in X?", "Can you write Python?", "Are you
  // proficient in Y?", "Willing to work on-site?"). Operator policy: answer
  // positively. Applies to Yes/No options and to free-text yes/no fields.
  const EXP_ABILITY_RE = /\b(experience (in|with)|proficien|familiar with|comfortable (with|working)|able to|can you|do you know|knowledge of|skilled in|expertise|worked with|have you (used|worked|built|developed|created|extended|led|managed)|willing to|do you have (experience|knowledge|a degree|a background)|degree|studies|qualification|certification|certified|background in|are you (able|willing|comfortable|proficient|experienced|qualified)|hands[- ]?on)\b/;
  // Only auto-Yes when it's genuinely a yes/no question: either Yes/No options,
  // or the question starts with a yes/no interrogative ("Do/Can/Are/Have you…").
  // Guards against "Describe your experience…" (a free-text prompt).
  const YESNO_START_RE = /^(do|does|can|could|are|is|have|has|had|will|would|did|were)\b/;
  const isYesNoOpts = opts.length > 0 && opts.every(o => /^(yes|no|y|n)$/i.test(norm(o)));
  if (EXP_ABILITY_RE.test(q) && (isYesNoOpts || YESNO_START_RE.test(q))) {
    // Yes/No dropdowns, radios, custom listboxes → Yes.
    if (opts.length || field.type === 'radio' || field.type === 'select' || field.type === 'listbox') {
      return { answer: matchOption(opts, YES) || YES, source: 'policy' };
    }
    // Free-text/textarea: if it asks to explain/describe, let AI write a real
    // answer; otherwise a plain "Yes" is fine.
    if (!/\b(explain|describe|tell us|elaborate|details|why|how|which|what)\b/.test(q)) {
      return { answer: YES, source: 'policy' };
    }
  }

  // Demographics — operator policy (explicit profile fields would need to be
  // added to the candidate record; policy is the default for all candidates).

  // Multi-checkbox race lists surface each option as its OWN Yes/No field
  // whose label IS the race name (no question mark), e.g.
  // "Asian (Not Hispanic or Latino) (United States of America)".
  // Must run BEFORE the hispanic/ethnicity QUESTION branches — those labels
  // contain the word "hispanic" and would otherwise get answered "No".
  const RACE_OPTION_RE = /\b(asian|white|black|african american|american indian|alaska native|native hawaiian|pacific islander|two or more races|hispanic or latino|middle eastern)\b/;
  if (!q.includes('?') && RACE_OPTION_RE.test(q)
      && opts.length && opts.every(o => /^(yes|no)$/i.test(norm(o)))
      && !has('are you', 'do you', 'what is', 'which ')) {
    const isOurs = /\basian\b/.test(q) && !/\bcaucasian\b/.test(q);
    return { answer: isOurs ? YES : NO, source: 'policy' };
  }

  if (has('gender', 'what is your sex')) {
    const g = profile.gender || 'Male';
    return { answer: matchOption(opts, g) || g, source: 'policy' };
  }
  if (has('transgender')) {
    const o = scanOptions(opts, ['no']);
    return { answer: o || NO, source: 'policy' };
  }
  if (has('sexual orientation')) {
    const o = scanOptions(opts, ['heterosexual', 'straight']);
    return { answer: o || 'Heterosexual', source: 'policy' };
  }
  if (has('hispanic', 'latino', 'latinx')) {
    const o = scanOptions(opts, ['no, i am not', 'not hispanic'], ['no']);
    return { answer: o || NO, source: 'policy' };
  }
  // "ethnic" catches ethnicity/ethnicities; "race" is exact. Also fire when the
  // OPTIONS themselves are clearly a race list (Workday's checkbox group can have
  // no legend, only an id hint like "ethnicities").
  if (has('ethnic', 'race')
      || (opts.length >= 2 && opts.filter(o => RACE_OPTION_RE.test(norm(o))).length >= 2)) {
    // HARDCODED policy: always Asian. Word-boundary scan so "asian" never
    // matches "Caucasian". Prefer South Asian; then any Asian option; never
    // White/Hispanic/other. Only if there is truly no Asian option do we fall
    // back to "prefer not to answer" — never a wrong race.
    const o = scanOptionsRe(opts, /\bsouth asian\b/, /\basian\b(?!.*hispanic)/, /\basian\b/);
    if (o) return { answer: o, source: 'policy' };
    if (!opts.length) return { answer: 'Asian', source: 'policy' }; // text / custom dropdown
    const pnta = scanOptions(opts, ['prefer not', 'decline', 'do not wish', 'i don']);
    return { answer: pnta || 'Asian', source: 'policy' };
  }
  if (has('veteran')) {
    const o = scanOptions(opts, ['i am not a protected veteran', 'not a protected veteran'], ['no']);
    return { answer: o || NO, source: 'policy' };
  }
  if (has('disability', 'disabled')) {
    const o = scanOptions(opts, ['no, i do not have a disability', 'do not have a disability', 'no disability'], ['no']);
    return { answer: o || NO, source: 'policy' };
  }

  // Employment history extras
  if (has('gap in employment', 'employment gap')) {
    return field.required ? null : { answer: 'N/A', source: 'policy' };
  }
  if (has('years of experience', 'years experience', 'total experience') && !has('with', 'using', 'in ')) {
    if (profile.years_exp != null) return { answer: String(profile.years_exp), source: 'profile' };
  }

  return null;
}

// ── main entry ─────────────────────────────────────────────────────────────

/**
 * Resolve answers for a batch of fields.
 * @param fields   [{label, type, options, required, currentValue}]
 * @param context  {profile, resume, jobTitle, jobCompany}
 * @param settings extension settings
 * @returns        [{label, answer|null, source}]
 */
export async function answerFields(fields, context, settings) {
  // A visible password field in the batch → this is a login/signup wall, so
  // email fields should use the portal login email (gmail), not contact email.
  const isLoginForm = fields.some(f => f.type === 'password' || /password|passcode/i.test(f.label || ''));
  const profile = { ...context.profile, resume: context.resume, _isLoginForm: isLoginForm };
  const results = new Array(fields.length).fill(null);
  const memory = await loadMemory(context.profile.id);
  const aiQueue = [];

  fields.forEach((field, i) => {
    // If the SITE flagged this field with a validation error, the previous
    // answer (heuristic/memory) was rejected — let the AI re-decide using the
    // error text as context, rather than repeating the same rejected value.
    // (Never AI a credential field, even when errored.)
    const isCredField = /password|passcode|security question/i.test(field.label || '') || field.type === 'password';
    if (field.error && !isCredField) { aiQueue.push(i); return; }
    // 1-2. heuristics + policy
    const h = heuristicAnswer(field, profile, settings);
    if (h && h.answer != null && h.answer !== '') {
      results[i] = { label: field.label, answer: h.answer, source: h.source };
      return;
    }
    // 3. memory (host-aware, cross-site reuse)
    const remembered = memLookup(memory, context.host, field.label);
    if (remembered?.answer) {
      const ans = field.options?.length
        ? matchOption(field.options, remembered.answer)
        : remembered.answer;
      if (ans) {
        results[i] = { label: field.label, answer: ans, source: 'memory' };
        return;
      }
    }
    // 4. queue for AI — but NEVER credential fields: an invented password is
    // worse than pausing for the human.
    if (/password|passcode|security question/i.test(field.label || '') || field.type === 'password') {
      results[i] = { label: field.label, answer: null, source: 'unresolved' };
      return;
    }
    aiQueue.push(i);
  });

  if (aiQueue.length) {
    const aiFields = aiQueue.map(i => fields[i]);
    let aiAnswers = [];
    try {
      // Strip credentials before anything reaches an AI prompt.
      const { sitePassword, ...safeProfile } = context.profile;
      aiAnswers = await aiAnswerBatch(aiFields, {
        profile: safeProfile,
        resume: context.resume,
        jobTitle: context.jobTitle,
        jobCompany: context.jobCompany,
        desiredSalary: settings.desiredSalary,
        desiredHours: settings.desiredHours,
      }, settings);
    } catch (e) {
      console.warn('[answers] AI failed:', e.message);
      context.__aiError = e.message;  // surfaced to the activity log by caller
      aiAnswers = new Array(aiQueue.length).fill(null);
      // Non-required fields degrade to blank; required ones surface to human.
    }
    const toRemember = [];
    aiQueue.forEach((fieldIdx, k) => {
      const field = fields[fieldIdx];
      let ans = aiAnswers[k];
      if (ans != null && field.options?.length) {
        ans = matchOption(field.options, ans) || null;
      }
      if (ans != null && field.type === 'number') {
        const digits = String(ans).replace(/[^\d.]/g, '');
        ans = digits || null;
      }
      if (ans == null && !field.required) {
        // Leave optional unanswered fields BLANK — never inject "N/A" into a
        // structured field (postcode, number, dropdown) where it could fail
        // validation. Genuine explanation fields get N/A via their heuristic.
        results[fieldIdx] = { label: field.label, answer: null, source: 'blank' };
        return;
      }
      results[fieldIdx] = { label: field.label, answer: ans, source: ans != null ? 'ai' : 'unresolved' };
      if (ans != null) toRemember.push({ label: field.label, answer: ans, source: 'ai' });
    });
    await rememberAnswers(context.profile.id, toRemember, context.host).catch(() => {});
  }

  return results.map((r, i) => r || { label: fields[i].label, answer: null, source: 'unresolved' });
}
