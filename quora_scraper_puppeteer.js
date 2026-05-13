/**
 * Quora Scraper — Puppeteer (Node.js, v2)
 * =========================================
 * Scrapes Quora questions, answers, spaces, topics, and profiles.
 *
 * Features:
 *   - CAPTCHA solving via 2captcha.com
 *   - Proxy support via 2prx.com
 *   - Anti-detect fingerprint spoofing via puppeteer-extra-plugin-stealth
 *   - Output: JSON and CSV
 *
 * Install:
 *   npm install puppeteer puppeteer-extra puppeteer-extra-plugin-stealth axios
 *
 * Usage:
 *   node quora_scraper_puppeteer.js --mode questions --query "machine learning" --output json
 *   node quora_scraper_puppeteer.js --mode answers   --url "https://quora.com/..."
 *   node quora_scraper_puppeteer.js --mode topics    --slug "Machine-Learning"
 *   node quora_scraper_puppeteer.js --mode spaces    --slug "AI-and-Machine-Learning"
 *   node quora_scraper_puppeteer.js --mode profile   --user "Andrew-Ng"
 *
 * Fix notes (v2):
 *   - Replaced waitUntil: "networkidle2" with "domcontentloaded" everywhere.
 *     Quora fires background XHR indefinitely so networkidle2 (≤2 active
 *     connections for 500ms) almost never fires and the script hangs.
 *   - Added page.waitForSelector() after each goto as the real readiness
 *     signal — mirrors the Playwright fix.
 *   - Broadened in-page selectors with fallback chains.
 *   - Added SIGINT / SIGTERM handlers for clean browser shutdown + partial
 *     result saving on Ctrl+C.
 */

"use strict";

const puppeteer     = require("puppeteer-extra");
const StealthPlugin = require("puppeteer-extra-plugin-stealth");
const axios         = require("axios");
const fs            = require("fs");

puppeteer.use(StealthPlugin());

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const TWOCAPTCHA_API_KEY = process.env.TWOCAPTCHA_API_KEY || "YOUR_2CAPTCHA_API_KEY";
const PROXY_HOST         = process.env.PROXY_HOST         || "gate.2prx.com";
const PROXY_PORT         = process.env.PROXY_PORT         || "7000";
const PROXY_USER         = process.env.PROXY_USER         || "";
const PROXY_PASS         = process.env.PROXY_PASS         || "";

const USE_PROXY          = !!PROXY_USER;
const USE_CAPTCHA_SOLVER = TWOCAPTCHA_API_KEY !== "YOUR_2CAPTCHA_API_KEY";

const BASE_URL = "https://www.quora.com";

const USER_AGENTS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
];

// Timeout for waitForSelector after navigation (ms)
const CONTENT_TIMEOUT = 20_000;
// Timeout for page.goto itself (ms)
const GOTO_TIMEOUT    = 30_000;


// ---------------------------------------------------------------------------
// 2Captcha helper
// ---------------------------------------------------------------------------

async function solveCaptcha(siteKey, pageUrl) {
  if (!USE_CAPTCHA_SOLVER) throw new Error("2Captcha API key not configured.");

  console.log("[2captcha] Submitting reCAPTCHA …");
  const submitRes = await axios.post("https://2captcha.com/in.php", null, {
    params: {
      key:       TWOCAPTCHA_API_KEY,
      method:    "userrecaptcha",
      googlekey: siteKey,
      pageurl:   pageUrl,
      json:      1,
    },
  });
  const taskId = submitRes.data.request;
  console.log(`[2captcha] Task ID: ${taskId}`);

  for (let i = 0; i < 24; i++) {
    await sleep(5000);
    const pollRes = await axios.get("https://2captcha.com/res.php", {
      params: { key: TWOCAPTCHA_API_KEY, action: "get", id: taskId, json: 1 },
    });
    if (pollRes.data.status === 1) {
      console.log("[2captcha] Solved ✓");
      return pollRes.data.request;
    }
    if (!["CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"].includes(pollRes.data.request)) {
      throw new Error(`2captcha error: ${JSON.stringify(pollRes.data)}`);
    }
  }
  throw new Error("2captcha timed out.");
}


// ---------------------------------------------------------------------------
// Browser factory
// ---------------------------------------------------------------------------

async function buildBrowser() {
  const ua = USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)];

  const launchArgs = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
  ];

  if (USE_PROXY) {
    launchArgs.push(`--proxy-server=http://${PROXY_HOST}:${PROXY_PORT}`);
  }

  const browser = await puppeteer.launch({
    headless: "new",
    args:     launchArgs,
    defaultViewport: { width: 1440, height: 900 },
  });

  const page = await browser.newPage();
  await page.setUserAgent(ua);
  await page.setExtraHTTPHeaders({ "Accept-Language": "en-US,en;q=0.9" });

  if (USE_PROXY && PROXY_USER) {
    await page.authenticate({ username: PROXY_USER, password: PROXY_PASS });
  }

  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    Object.defineProperty(navigator, "plugins",   { get: () => [1, 2, 3] });
    Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
    window.chrome = { runtime: {} };
  });

  return { browser, page };
}


// ---------------------------------------------------------------------------
// Navigation helper  ← THE KEY FIX
// ---------------------------------------------------------------------------

/**
 * Navigate to url using domcontentloaded (never hangs on Quora),
 * then wait for a real content selector to confirm the page is usable.
 *
 * Why: networkidle2 means ≤2 in-flight requests for 500ms — Quora's
 * analytics/live-update XHR streams ensure this condition is never met,
 * so the await never resolves and the process stalls.
 */
async function goto(page, url, waitSelector = null) {
  console.log(`[nav] → ${url}`);
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: GOTO_TIMEOUT });
  } catch (err) {
    console.log(`[nav] goto timeout (${err.message}) — continuing with whatever loaded`);
  }

  if (waitSelector) {
    try {
      await page.waitForSelector(waitSelector, { timeout: CONTENT_TIMEOUT });
    } catch {
      console.log(`[nav] '${waitSelector}' not found within timeout — ` +
                  "page may be empty or Quora's DOM structure has changed");
    }
  }

  await humanDelay();
}


// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const sleep      = (ms) => new Promise(r => setTimeout(r, ms));
const rand       = (lo, hi) => Math.random() * (hi - lo) + lo;
const humanDelay = ()  => sleep(rand(800, 2500));
const now        = ()  => new Date().toISOString();

async function scrollToBottom(page, scrolls = 8, pause = 1500) {
  for (let i = 0; i < scrolls; i++) {
    const prevH = await page.evaluate(() => document.body.scrollHeight);
    await page.evaluate(() => window.scrollBy(0, window.innerHeight * 0.85));
    await sleep(pause);
    const newH = await page.evaluate(() => document.body.scrollHeight);
    if (newH === prevH) break;
  }
}

/** Try each selector in order; return the first non-empty innerText. */
async function safeText(page, ...selectors) {
  for (const sel of selectors) {
    try {
      const text = await page.$eval(sel, el => el.innerText.trim());
      if (text) return text;
    } catch { /* not found */ }
  }
  return "";
}


// ---------------------------------------------------------------------------
// Module: Questions
// ---------------------------------------------------------------------------

async function scrapeQuestions(page, query, maxResults = 50) {
  const url = `${BASE_URL}/search?q=${encodeURIComponent(query)}&type=question`;
  await goto(page, url, "main, [role='main'], a[href]");
  await scrollToBottom(page, 6);

  const results = await page.evaluate((base, max) => {
    const seen  = new Set();
    const items = [];

    for (const a of document.querySelectorAll("a")) {
      const href = a.href || "";
      if (!href || seen.has(href)) continue;

      const isQuestion = (
        (href.includes("/q/") && href.split("/").length <= 6)
        || (
          href.startsWith(base)
          && (href.match(/-/g) || []).length >= 3
          && !href.includes("/profile/")
          && !href.includes("/topic/")
          && !href.includes("/search")
          && !href.includes("/sitemap")
        )
      );
      if (!isQuestion) continue;

      seen.add(href);
      const title = a.innerText.trim();
      if (!title || title.length < 8) continue;

      items.push({
        type:       "question",
        title,
        url:        href,
        scraped_at: new Date().toISOString(),
      });
      if (items.length >= max) break;
    }
    return items;
  }, BASE_URL, maxResults);

  console.log(`[questions] Found ${results.length} questions.`);
  return results;
}


// ---------------------------------------------------------------------------
// Module: Answers
// ---------------------------------------------------------------------------

async function scrapeAnswers(page, questionUrl, maxAnswers = 20) {
  await goto(page, questionUrl, "h1");
  await scrollToBottom(page, 12);

  const questionTitle = await safeText(page, "h1");

  const results = await page.evaluate((question, url, max) => {
    // Fallback selector chain for answer blocks
    const blockSels = [
      ".q-box.spacing_log_answer_content",
      "[class*='Answer']",
      "article",
    ];
    let blocks = [];
    for (const sel of blockSels) {
      blocks = [...document.querySelectorAll(sel)];
      if (blocks.length) break;
    }

    return blocks.slice(0, max).map(block => {
      // Author
      const authorEl = block.querySelector(".q-text.qu-bold, strong, b, [class*='creator']");
      const author   = authorEl?.innerText?.trim() || "Anonymous";

      // Content — try specific selectors, fall back to full block text
      const contentEl = block.querySelector(
        ".q-text.qu-wordBreak--word, [class*='AnswerBase'], p"
      );
      const content = (contentEl?.innerText?.trim() || block.innerText?.trim() || "").slice(0, 2000);

      if (!content || content.length < 20) return null;
      return {
        type:         "answer",
        question,
        question_url: url,
        author,
        content,
        scraped_at:   new Date().toISOString(),
      };
    }).filter(Boolean);
  }, questionTitle, questionUrl, maxAnswers);

  console.log(`[answers] Found ${results.length} answers.`);
  return results;
}


// ---------------------------------------------------------------------------
// Module: Topics
// ---------------------------------------------------------------------------

async function scrapeTopic(page, slug, maxQuestions = 30) {
  const url = `${BASE_URL}/topic/${slug}`;
  await goto(page, url, "h1");
  await scrollToBottom(page, 6);

  const topicName = await safeText(page, "h1");

  const results = await page.evaluate((topic, topicSlug, base, max) => {
    const seen  = new Set();
    const items = [];

    for (const a of document.querySelectorAll("a")) {
      const href = a.href || "";
      if (!href || seen.has(href)) continue;
      if ((href.match(/-/g) || []).length < 3 && !href.includes("/q/")) continue;
      if (href.includes("/topic/") || href.includes("/profile/") ||
          href.includes("/search") || href.includes("javascript")) continue;

      seen.add(href);
      const title = a.innerText.trim();
      if (!title || title.length < 8) continue;

      items.push({
        type:       "topic_question",
        topic,
        topic_slug: topicSlug,
        title,
        url:        href,
        scraped_at: new Date().toISOString(),
      });
      if (items.length >= max) break;
    }
    return items;
  }, topicName, slug, BASE_URL, maxQuestions);

  console.log(`[topics] Found ${results.length} questions in "${topicName}".`);
  return results;
}


// ---------------------------------------------------------------------------
// Module: Spaces
// ---------------------------------------------------------------------------

async function scrapeSpace(page, slug, maxPosts = 20) {
  const url = `${BASE_URL}/q/${slug}`;
  await goto(page, url, "h1");
  await scrollToBottom(page, 8);

  const spaceName = await safeText(page, "h1");

  const results = await page.evaluate((space, spaceSlug, max) => {
    const seen  = new Set();
    const items = [];

    for (const a of document.querySelectorAll("a")) {
      const href = a.href || "";
      if (!href || seen.has(href)) continue;
      seen.add(href);

      const title = a.innerText.trim();
      if (!title || title.length < 8) continue;

      items.push({
        type:       "space_post",
        space,
        space_slug: spaceSlug,
        title,
        url:        href,
        scraped_at: new Date().toISOString(),
      });
      if (items.length >= max) break;
    }
    return items;
  }, spaceName, slug, maxPosts);

  console.log(`[spaces] Found ${results.length} posts in "${spaceName}".`);
  return results;
}


// ---------------------------------------------------------------------------
// Module: Profiles
// ---------------------------------------------------------------------------

async function scrapeProfile(page, username) {
  const url = `${BASE_URL}/profile/${username}`;
  await goto(page, url, "h1");

  const name = await safeText(page, "h1");
  const bio  = await safeText(
    page,
    ".q-text.qu-wordBreak--word",
    "[class*='bio']",
    "p"
  );

  return {
    type:         "profile",
    username,
    display_name: name,
    bio:          bio.slice(0, 500),
    url,
    scraped_at:   now(),
  };
}


// ---------------------------------------------------------------------------
// Output helpers
// ---------------------------------------------------------------------------

function saveJson(data, filepath) {
  const rows = Array.isArray(data) ? data : [data];
  fs.writeFileSync(filepath, JSON.stringify(rows, null, 2), "utf8");
  console.log(`[output] JSON → ${filepath}`);
}

function saveCsv(data, filepath) {
  const rows = Array.isArray(data) ? data : [data];
  if (!rows.length) return;
  const keys    = Object.keys(rows[0]);
  const header  = keys.join(",");
  const csvRows = rows.map(r =>
    keys.map(k => `"${String(r[k] || "").replace(/"/g, '""')}"`).join(",")
  );
  fs.writeFileSync(filepath, [header, ...csvRows].join("\n"), "utf8");
  console.log(`[output] CSV → ${filepath}`);
}


// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

function parseArgs() {
  const result = {};
  const argv   = process.argv.slice(2);
  for (let i = 0; i < argv.length; i += 2) {
    result[argv[i].replace(/^--/, "")] = argv[i + 1];
  }
  return result;
}


// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

(async () => {
  const args    = parseArgs();
  const mode    = args.mode;
  const output  = args.output  || "json";
  const outfile = args.outfile || "quora_output";
  const max     = parseInt(args.max || "50", 10);

  if (!mode) {
    console.error("Usage: node quora_scraper_puppeteer.js --mode <mode> [options]");
    console.error("Modes: questions | answers | topics | spaces | profile");
    process.exit(1);
  }

  let browser = null;
  let results = [];

  // Clean shutdown on Ctrl+C or kill
  async function cleanup(signal) {
    console.log(`\n[!] ${signal} received — closing browser …`);
    try { if (browser) await browser.close(); } catch { /* ignore */ }
    if (results.length) {
      saveJson(results, `${outfile}_partial.json`);
      console.log("[!] Partial results saved.");
    }
    process.exit(0);
  }

  process.on("SIGINT",  () => cleanup("SIGINT"));
  process.on("SIGTERM", () => cleanup("SIGTERM"));

  try {
    const built = await buildBrowser();
    browser     = built.browser;
    const page  = built.page;

    if (mode === "questions") {
      if (!args.query) throw new Error("--query is required for mode=questions");
      results = await scrapeQuestions(page, args.query, max);

    } else if (mode === "answers") {
      if (!args.url) throw new Error("--url is required for mode=answers");
      results = await scrapeAnswers(page, args.url, max);

    } else if (mode === "topics") {
      if (!args.slug) throw new Error("--slug is required for mode=topics");
      results = await scrapeTopic(page, args.slug, max);

    } else if (mode === "spaces") {
      if (!args.slug) throw new Error("--slug is required for mode=spaces");
      results = await scrapeSpace(page, args.slug, max);

    } else if (mode === "profile") {
      if (!args.user) throw new Error("--user is required for mode=profile");
      results = [await scrapeProfile(page, args.user)];

    } else {
      console.error(`Unknown mode: ${mode}`);
      process.exit(1);
    }

  } finally {
    try { if (browser) await browser.close(); } catch { /* ignore */ }
  }

  if (output === "json" || output === "both") saveJson(results, `${outfile}.json`);
  if (output === "csv"  || output === "both") saveCsv(results,  `${outfile}.csv`);
})();
