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
import { randomUUID } from 'crypto';
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

/**
 * Build a Playwright-compatible proxy config from the PROXY_URL env variable.
 *
 * PROXY_URL format (iProyal residential):
 *   http://<user>:<pass>_country-us@geo.iproyal.com:12321
 *
 * A random sticky-session token (_session-<uuid>) is appended to the password
 * on every call so each scrape run gets its own held residential IP address
 * (preventing job portals from rate-limiting the same IP across pages).
 *
 * Returns null when PROXY_URL is not set so scraping works without a proxy
 * in local dev environments.
 */
function buildProxyConfig() {
  const rawProxy = process.env.PROXY_URL;
  if (!rawProxy) {
    return null;
  }

  try {
    const url = new URL(rawProxy);
    const sessionToken = randomUUID().replace(/-/g, '').slice(0, 16);
    // Append sticky session suffix to the password so every run gets a
    // unique residential IP that is held for the life of this process.
    const password = `${url.password}_session-${sessionToken}`;

    const proxyConfig = {
      server: `${url.protocol}//${url.host}`,
      username: url.username,
      password,
    };

    console.error(
      `[scrape] proxy enabled: ${proxyConfig.server} (user=${proxyConfig.username}, session=${sessionToken})`
    );
    return proxyConfig;
  } catch (err) {
    console.error(`[scrape] PROXY_URL is set but could not be parsed — scraping without proxy: ${err.message}`);
    return null;
  }
}

async function runWithModel(model, apiKey, startUrls) {
  console.error(`[scrape] using model ${model} for ${startUrls.length} pages`);

  const proxy = buildProxyConfig();
  const playwrightOptions = {
    headless: true,
    loadWait: 10000,
    loadTimeout: 60000,
    timeout: 60000,
    ...(proxy ? { proxy } : {}),
  };

  const f = fox.config({
    ai: [model, { apiKey }],
    fetcher: ['playwright', playwrightOptions],
  });

  const cooldownStr = process.env.SCRAPE_PAGE_COOLDOWN_SECONDS || '0';
  const cooldown = parseFloat(cooldownStr) * 1000;
  
  const allItems = [];

  for (let i = 0; i < startUrls.length; i++) {
    const url = startUrls[i];
    console.error(`[scrape] scraping page ${i + 1} -> ${url}`);
    
    let urlItems = [];
    let attempts = 0;
    const maxAttempts = 3;
    let retryDelay = 5000;
    let success = false;
    let lastErr = null;

    while (attempts < maxAttempts) {
      try {
        const stream = f.init(url).extract(FIELDS).limit(DEFAULT_LIMIT).stream();
        for await (const delta of stream) {
          if (!delta?.item) continue;
          const clean = { ...delta.item };
          for (const k of Object.keys(clean)) if (k.startsWith('_')) delete clean[k];
          urlItems.push(clean);
        }
        success = true;
        lastErr = null;
        break;
      } catch (err) {
        lastErr = err;
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

    if (!success) {
      throw lastErr || new Error(`Failed to scrape ${url}`);
    }

    allItems.push(...urlItems);
    console.error(`[scrape] page ${i + 1}: ${urlItems.length} scraped, total so far: ${allItems.length}`);

    if (urlItems.length === 0) {
      console.error(`[scrape] no items from page ${i + 1}, stopping pagination for this link`);
      break;
    }

    if (i < startUrls.length - 1 && cooldown > 0) {
      console.error(`[scrape] Sleeping for ${cooldownStr}s (SCRAPE_PAGE_COOLDOWN_SECONDS)...`);
      await sleep(cooldown);
    }
  }

  return allItems;
}

function isRetryableGeminiError(err) {
  const text = String(err.message || err).toLowerCase();
  return text.includes('quota') || text.includes('too many requests') || text.includes('failed to parse stream') || text.includes('service unavailable');
}

async function main() {
  const startUrls = process.argv.slice(2);
  if (startUrls.length === 0) {
    console.error('Usage: node scrape.js <url1> [url2] ...');
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

    try {
      items = await runWithModel(model, apiKey, startUrls);
      lastError = null;
      if (items.length > 0) {
        break;
      } else {
        console.error(`[scrape] model ${model} returned 0 items, trying next model if available`);
      }
    } catch (err) {
      lastError = err;
      console.error(`[scrape] model ${model} failed entirely:`, err.message);
    }
  }

  if (lastError) {
    console.error(`[scrape] failed on all models for ${startUrls[0]}`);
    console.error(lastError);
    process.stdout.write(JSON.stringify(items));
    process.exit(2);
  }

  process.stdout.write(JSON.stringify(items));
}

// Catch any rejection that escapes the main() async chain (e.g. errors thrown
// inside fetchfox's PlaywrightFetcher async generator that bypass the inner
// try/catch). Exit with 2 so Python still treats any stdout as partial results.
process.on('unhandledRejection', (reason) => {
  console.error('[scrape] unhandledRejection:', reason);
  process.stdout.write(JSON.stringify([]));
  process.exit(2);
});

main().catch((err) => {
  console.error('[scrape] fatal error in main():', err);
  process.stdout.write(JSON.stringify([]));
  process.exit(2);
});
