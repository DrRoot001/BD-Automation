// Optional pre-baked defaults so BD agents don't have to configure anything
// on a fresh install. Values entered in the popup ⚙ settings ALWAYS override
// these — this file only seeds the initial defaults.
//
// ⚠ Anything you paste here is readable by anyone who gets a copy of this
// folder or of the git repo. Fine for an internal tool on trusted machines;
// do NOT push a real key to a public remote.

export const DEFAULT_CONFIG = {
  // Gemini API key — THE key the extension uses. When set here it is
  // AUTHORITATIVE: it overrides anything saved in the popup, so a stale key can
  // never shadow the code. Leave EMPTY in git — set it in the popup ⚙ settings,
  // or copy config.example.js locally and fill it in (config.js is gitignored).
  geminiKey: '',

  // Gemini model. AUTHORITATIVE when set here (overrides the popup). Verified
  // available: gemini-3.5-flash (newest stable Flash), also
  // gemini-3-flash-preview / gemini-2.5-flash.
  geminiModel: 'gemini-3.5-flash',

  // Optional Anthropic fallback key, e.g. 'sk-ant-...'. Leave empty in git.
  anthropicKey: '',

  // Backend API base — change if the backend isn't on localhost.
  apiBase: 'https://bd-autoamation.maverickslabs.io/api',

  // Strong default password used when the extension CREATES an account on a job
  // portal (Workday/iCIMS/Taleo/etc.). The candidate's gmail password usually
  // fails portal complexity rules, so we use this instead. It satisfies the
  // common requirements: uppercase, lowercase, digit, special char, 8+ length.
  // The SAME value is reused for later sign-ins, so accounts stay accessible.
  // Set the real value locally / in the popup — not in git.
  portalPassword: '',
};
