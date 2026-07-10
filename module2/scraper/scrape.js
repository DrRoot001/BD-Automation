// module2/scraper/scrape.js
// Mavericks-style scraper, stripped of Express/UI — called as a subprocess
// by the Python backend (see module2/run_scrape.py).
//
// Usage:
//   GEMINI_API_KEY=... node scrape.js "<start_url>"
//
// Prints a single JSON array of extracted items to stdout. All progress/
// logging goes to stderr so stdout stays clean for the caller to parse.

import dotenv from 'dotenv';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { fox } from 'fetchfox';
import { FIELDS, DEFAULT_LIMIT } from './fields.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: resolve(__dirname, '../../backend/.env') });

const DEFAULT_MODEL = process.env.AI_MODEL || 'google:gemini-3.5-flash';
// Moving alias that always points at the current flash model, so a retired
// pinned model (404 "no longer available") doesn't zero out every scrape.
const FALLBACK_MODEL = 'google:gemini-flash-latest';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function getApiKey(model) {
  if (model.startsWith('google:')) {
    return process.env.GEMINI_API_KEY;
  }
  if (model.startsWith('groq:')) {
    return process.env.GROQ_API_KEY;
  }
  return process.env.GEMINI_API_KEY || process.env.GROQ_API_KEY;
}

async function runWithModel(model, apiKey, startUrl) {
  console.error(`[scrape] using model ${model}`);
  const stream = fox
    .config({
      ai: [model, { apiKey }],
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

  const items = [];
  for await (const delta of stream) {
    if (!delta?.item) continue;
    const clean = { ...delta.item };
    for (const k of Object.keys(clean)) if (k.startsWith('_')) delete clean[k];
    items.push(clean);
    console.error(`[scrape] got item ${items.length}: ${clean.job_title || ''}`);
  }

  return items;
}

function isRetryableGeminiError(err) {
  const text = String(err.message || err).toLowerCase();
  return text.includes('quota') || text.includes('too many requests') || text.includes('failed to parse stream') || text.includes('service unavailable');
}

async function main() {
  const startUrl = process.argv[2];
  if (!startUrl) {
    console.error('Usage: node scrape.js <start_url>');
    process.exit(1);
  }

  const models = [DEFAULT_MODEL];
  if (FALLBACK_MODEL) {
    models.push(FALLBACK_MODEL);
  }

  let items = [];
  let lastError = null;

  for (const model of models) {
    const apiKey = getApiKey(model);
    if (!apiKey) {
      console.error(`[scrape] skipping model ${model} because API key is not set`);
      continue;
    }

    let attempts = 0;
    const maxAttempts = 3;
    let retryDelay = 5000;
    let success = false;

    while (attempts < maxAttempts) {
      try {
        items = await runWithModel(model, apiKey, startUrl);
        lastError = null;
        success = true;
        break;
      } catch (err) {
        lastError = err;
        attempts++;
        console.error(`[scrape] error for model ${model} (Attempt ${attempts}/${maxAttempts}):`, err.message);

        if (isRetryableGeminiError(err) && attempts < maxAttempts) {
          console.error(`[scrape] Rate limit or retryable error hit. Waiting ${retryDelay / 1000}s before retry...`);
          await sleep(retryDelay);
          retryDelay *= 2;
          continue;
        }
        break;
      }
    }

    if (success && items.length > 0) {
      break;
    }
    if (success) {
      // fetchfox swallows extraction errors (e.g. a retired model's 404s)
      // and yields an empty stream with a clean exit, so an empty result may
      // be a hidden failure — give the next model a chance before giving up.
      console.error(`[scrape] model ${model} returned 0 items, trying next model if available`);
    }
  }

  if (lastError) {
    console.error(`[scrape] failed on all models for ${startUrl}`);
    console.error(lastError);
    process.stdout.write(JSON.stringify(items));
    process.exit(2);
  }

  process.stdout.write(JSON.stringify(items));
}

main();
