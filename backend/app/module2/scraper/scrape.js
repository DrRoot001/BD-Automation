// module2/scraper/scrape.js
// Mavericks-style scraper, stripped of Express/UI — called as a subprocess
// by the Python backend (see module2/run_scrape.py).
//
// Usage:
//   GEMINI_API_KEY=... node scrape.js "<start_url>"
//
// Prints a single JSON array of extracted items to stdout. All progress/
// logging goes to stderr so stdout stays clean for the caller to parse.

import 'dotenv/config';
import { fox } from 'fetchfox';
import { FIELDS, DEFAULT_LIMIT } from './fields.js';

const AI_MODEL = process.env.AI_MODEL || 'google:gemini-2.5-flash';

async function main() {
  const startUrl = process.argv[2];
  if (!startUrl) {
    console.error('Usage: node scrape.js <start_url>');
    process.exit(1);
  }
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) {
    console.error('GEMINI_API_KEY is not set');
    process.exit(1);
  }

  const items = [];

  try {
    const stream = fox
      .config({
        ai: [AI_MODEL, { apiKey }],
        fetcher: ['playwright', {
          headless: true,
          loadWait: 10000,
          loadTimeout: 60000,
          timeout: 60000,
        }],
      })
      .init(startUrl)
      .extract(FIELDS)
      .limit(DEFAULT_LIMIT)
      .stream();

    for await (const delta of stream) {
      if (!delta?.item) continue;
      const clean = { ...delta.item };
      for (const k of Object.keys(clean)) if (k.startsWith('_')) delete clean[k];
      items.push(clean);
      console.error(`[scrape] got item ${items.length}: ${clean.job_title || ''}`);
    }
  } catch (err) {
    console.error(`[scrape] error for ${startUrl}:`, err.message);
    // Still print whatever we collected so far, then exit non-zero.
    process.stdout.write(JSON.stringify(items));
    process.exit(2);
  }

  process.stdout.write(JSON.stringify(items));
}

main();
