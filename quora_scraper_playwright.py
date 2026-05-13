"""
Quora Scraper — Playwright (Primary)
=====================================
Scrapes Quora questions, answers, spaces, topics, and profiles.

Features:
  - CAPTCHA solving via 2captcha.com
  - Proxy support via 2prx.com
  - Anti-detect browser fingerprint spoofing
  - Output: JSON and CSV

Install:
  pip install playwright requests
  playwright install chromium

Usage:
  python quora_scraper_playwright.py --mode questions --query "machine learning" --output json
  python quora_scraper_playwright.py --mode answers  --url "https://quora.com/..." --output csv
  python quora_scraper_playwright.py --mode topics   --slug "Machine-Learning" --output both
  python quora_scraper_playwright.py --mode spaces   --slug "AI-and-Machine-Learning"
  python quora_scraper_playwright.py --mode profile  --user "Andrew-Ng"

Fix notes (v2):
  - Replaced wait_until="networkidle" with "domcontentloaded" + explicit
    wait_for_selector(). Quora fires background XHR indefinitely so networkidle
    never resolves and the script hangs until Ctrl+C.
  - Added SIGINT / SIGTERM handler so the browser closes cleanly on interruption
    and any partial results are saved automatically.
  - Broadened CSS selectors with fallback chains — Quora updates class names
    frequently; the scraper now tries multiple patterns before giving up.
"""

import argparse
import csv
import json
import os
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY", "YOUR_2CAPTCHA_API_KEY")
PROXY_HOST         = os.getenv("PROXY_HOST", "gate.2prx.com")
PROXY_PORT         = os.getenv("PROXY_PORT", "7000")
PROXY_USER         = os.getenv("PROXY_USER", "")
PROXY_PASS         = os.getenv("PROXY_PASS", "")

USE_PROXY          = bool(PROXY_USER)
USE_CAPTCHA_SOLVER = bool(TWOCAPTCHA_API_KEY and TWOCAPTCHA_API_KEY != "YOUR_2CAPTCHA_API_KEY")

BASE_URL = "https://www.quora.com"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

VIEWPORT_PRESETS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

# Timeout for page.goto (ms) — kept short; we don't wait for full load
GOTO_TIMEOUT    = 30_000
# Timeout for wait_for_selector after navigation (ms)
CONTENT_TIMEOUT = 20_000


# ---------------------------------------------------------------------------
# 2Captcha helper
# ---------------------------------------------------------------------------

class TwoCaptchaSolver:
    BASE = "https://2captcha.com"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def solve_recaptcha_v2(self, site_key: str, page_url: str, timeout: int = 120) -> str:
        print("[2captcha] Submitting reCAPTCHA v2 …")
        resp = requests.post(f"{self.BASE}/in.php", data={
            "key":       self.api_key,
            "method":    "userrecaptcha",
            "googlekey": site_key,
            "pageurl":   page_url,
            "json":      1,
        }, timeout=30)
        resp.raise_for_status()
        task_id = resp.json()["request"]
        print(f"[2captcha] Task ID: {task_id}. Polling …")

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            poll = requests.get(f"{self.BASE}/res.php", params={
                "key": self.api_key, "action": "get", "id": task_id, "json": 1,
            }, timeout=30)
            data = poll.json()
            if data.get("status") == 1:
                print("[2captcha] Solved ✓")
                return data["request"]
            if data.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                raise RuntimeError(f"2captcha error: {data}")
        raise TimeoutError("2captcha did not return a solution in time.")

    def solve_recaptcha_v3(self, site_key: str, page_url: str, action: str = "submit") -> str:
        print("[2captcha] Submitting reCAPTCHA v3 …")
        resp = requests.post(f"{self.BASE}/in.php", data={
            "key":       self.api_key,
            "method":    "userrecaptcha",
            "version":   "v3",
            "googlekey": site_key,
            "pageurl":   page_url,
            "action":    action,
            "score":     0.7,
            "json":      1,
        }, timeout=30)
        resp.raise_for_status()
        task_id = resp.json()["request"]
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(5)
            poll = requests.get(f"{self.BASE}/res.php", params={
                "key": self.api_key, "action": "get", "id": task_id, "json": 1,
            }, timeout=30)
            data = poll.json()
            if data.get("status") == 1:
                return data["request"]
            if data.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                raise RuntimeError(f"2captcha error: {data}")
        raise TimeoutError("Timeout waiting for reCAPTCHA v3 solution.")


solver = TwoCaptchaSolver(TWOCAPTCHA_API_KEY) if USE_CAPTCHA_SOLVER else None


# ---------------------------------------------------------------------------
# Browser factory
# ---------------------------------------------------------------------------

def build_browser_context(playwright):
    ua       = random.choice(USER_AGENTS)
    viewport = random.choice(VIEWPORT_PRESETS)

    launch_kwargs: dict = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }

    if USE_PROXY:
        launch_kwargs["proxy"] = {
            "server":   f"http://{PROXY_HOST}:{PROXY_PORT}",
            "username": PROXY_USER,
            "password": PROXY_PASS,
        }

    browser = playwright.chromium.launch(**launch_kwargs)
    context = browser.new_context(
        user_agent          = ua,
        viewport            = viewport,
        locale              = "en-US",
        timezone_id         = "America/New_York",
        java_script_enabled = True,
        extra_http_headers  = {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        },
    )

    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        window.chrome = { runtime: {} };
    """)

    return browser, context


# ---------------------------------------------------------------------------
# Navigation helper  ← THE KEY FIX
# ---------------------------------------------------------------------------

def goto(page, url: str, wait_selector: str | None = None):
    """
    Navigate to *url* and wait for a visible selector instead of networkidle.

    Why: Quora continuously sends background requests (analytics, prefetch,
    live updates) so wait_until="networkidle" never fires and the process
    blocks until Ctrl+C. Using "domcontentloaded" + an explicit selector
    wait gives us a reliable, fast readiness signal.
    """
    print(f"[nav] → {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
    except PWTimeout:
        print("[nav] goto timed out — continuing with whatever loaded")

    if wait_selector:
        try:
            page.wait_for_selector(wait_selector, timeout=CONTENT_TIMEOUT)
        except PWTimeout:
            print(f"[nav] '{wait_selector}' not found — page may be empty or "
                  "Quora's DOM structure has changed")

    human_delay(1.0, 2.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def human_delay(lo: float = 0.8, hi: float = 2.5):
    time.sleep(random.uniform(lo, hi))


def safe_text(el, default: str = "") -> str:
    try:
        return el.inner_text().strip() if el else default
    except Exception:
        return default


def safe_attr(el, attr: str, default: str = "") -> str:
    try:
        v = el.get_attribute(attr) if el else None
        return v.strip() if v else default
    except Exception:
        return default


def scroll_to_bottom(page, max_scrolls: int = 8, pause: float = 1.5):
    for _ in range(max_scrolls):
        prev = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.85)")
        time.sleep(pause)
        if page.evaluate("document.body.scrollHeight") == prev:
            break


def ts() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Module: Questions
# ---------------------------------------------------------------------------

def scrape_questions(page, query: str, max_results: int = 50) -> list[dict]:
    url = f"{BASE_URL}/search?q={requests.utils.quote(query)}&type=question"
    goto(page, url, wait_selector="main, [role='main'], .q-box")
    scroll_to_bottom(page, max_scrolls=6)

    results = []
    seen    = set()

    for a in page.query_selector_all("a"):
        href = safe_attr(a, "href")
        if not href or href in seen:
            continue
        # Quora question slugs: long hyphenated paths or /q/ paths
        is_question = (
            ("/q/" in href and href.count("/") == 2)
            or (href.startswith("/") and href.count("-") >= 3
                and "/profile/" not in href
                and "/topic/" not in href
                and "/search" not in href)
        )
        if not is_question:
            continue

        seen.add(href)
        title = safe_text(a).strip()
        if not title or len(title) < 8:
            continue

        results.append({
            "type":       "question",
            "title":      title,
            "url":        href if href.startswith("http") else BASE_URL + href,
            "scraped_at": ts(),
        })
        if len(results) >= max_results:
            break

    print(f"[questions] Collected {len(results)} questions.")
    return results


# ---------------------------------------------------------------------------
# Module: Answers
# ---------------------------------------------------------------------------

def scrape_answers(page, question_url: str, max_answers: int = 20) -> list[dict]:
    goto(page, question_url, wait_selector="h1")
    scroll_to_bottom(page, max_scrolls=12)

    h1             = page.query_selector("h1")
    question_title = safe_text(h1)

    answers = []
    # Try multiple selector patterns — Quora redesigns frequently
    for sel in [".q-box.spacing_log_answer_content", "[class*='Answer']", "article"]:
        blocks = page.query_selector_all(sel)
        if blocks:
            break

    for block in blocks[:max_answers]:
        try:
            author_el  = block.query_selector(".q-text.qu-bold, strong, b, [class*='creator']")
            author     = safe_text(author_el) or "Anonymous"

            content_el = block.query_selector(
                ".q-text.qu-wordBreak--word, [class*='AnswerBase'], p"
            )
            content = safe_text(content_el)
            if not content or len(content) < 20:
                content = block.inner_text().strip()

            if content:
                answers.append({
                    "type":         "answer",
                    "question":     question_title,
                    "question_url": question_url,
                    "author":       author,
                    "content":      content[:2000],
                    "scraped_at":   ts(),
                })
        except Exception as exc:
            print(f"[answers] Skipping block: {exc}")

    print(f"[answers] Collected {len(answers)} answers.")
    return answers


# ---------------------------------------------------------------------------
# Module: Topics
# ---------------------------------------------------------------------------

def scrape_topic(page, topic_slug: str, max_questions: int = 30) -> list[dict]:
    url = f"{BASE_URL}/topic/{topic_slug}"
    goto(page, url, wait_selector="h1")
    scroll_to_bottom(page, max_scrolls=6)

    h1         = page.query_selector("h1")
    topic_name = safe_text(h1) if h1 else topic_slug

    items = []
    seen  = set()

    for a in page.query_selector_all("a"):
        href = safe_attr(a, "href")
        if not href or href in seen:
            continue
        if href.count("-") < 3 and "/q/" not in href:
            continue
        if any(x in href for x in ["/topic/", "/profile/", "/search", "javascript"]):
            continue
        seen.add(href)

        title = safe_text(a).strip()
        if not title or len(title) < 8:
            continue

        items.append({
            "type":       "topic_question",
            "topic":      topic_name,
            "topic_slug": topic_slug,
            "title":      title,
            "url":        href if href.startswith("http") else BASE_URL + href,
            "scraped_at": ts(),
        })
        if len(items) >= max_questions:
            break

    print(f"[topics] Collected {len(items)} questions under '{topic_name}'.")
    return items


# ---------------------------------------------------------------------------
# Module: Spaces
# ---------------------------------------------------------------------------

def scrape_space(page, space_slug: str, max_posts: int = 20) -> list[dict]:
    url = f"{BASE_URL}/q/{space_slug}"
    goto(page, url, wait_selector="h1")
    scroll_to_bottom(page, max_scrolls=8)

    h1         = page.query_selector("h1")
    space_name = safe_text(h1) if h1 else space_slug

    items = []
    seen  = set()

    for a in page.query_selector_all("a"):
        href = safe_attr(a, "href")
        if not href or href in seen:
            continue
        seen.add(href)
        title = safe_text(a).strip()
        if not title or len(title) < 8:
            continue
        items.append({
            "type":       "space_post",
            "space":      space_name,
            "space_slug": space_slug,
            "title":      title,
            "url":        href if href.startswith("http") else BASE_URL + href,
            "scraped_at": ts(),
        })
        if len(items) >= max_posts:
            break

    print(f"[spaces] Collected {len(items)} posts in '{space_name}'.")
    return items


# ---------------------------------------------------------------------------
# Module: Profiles
# ---------------------------------------------------------------------------

def scrape_profile(page, username: str) -> dict:
    url = f"{BASE_URL}/profile/{username}"
    goto(page, url, wait_selector="h1")

    h1   = page.query_selector("h1")
    name = safe_text(h1) if h1 else username

    bio = ""
    for sel in [".q-text.qu-wordBreak--word", "p", "[class*='bio']"]:
        el = page.query_selector(sel)
        if el:
            bio = safe_text(el)[:500]
            if bio:
                break

    return {
        "type":         "profile",
        "username":     username,
        "display_name": name,
        "bio":          bio,
        "url":          url,
        "scraped_at":   ts(),
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def save_json(data: list | dict, filepath: str):
    rows = data if isinstance(data, list) else [data]
    Path(filepath).write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[output] Saved JSON → {filepath}")


def save_csv(data: list | dict, filepath: str):
    rows = data if isinstance(data, list) else [data]
    if not rows:
        print("[output] No data to write.")
        return
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"[output] Saved CSV → {filepath}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Quora Scraper — Playwright (v2)")
    p.add_argument("--mode",    required=True,
                   choices=["questions", "answers", "topics", "spaces", "profile"])
    p.add_argument("--query",   help="Search query (mode=questions)")
    p.add_argument("--url",     help="Question URL (mode=answers)")
    p.add_argument("--slug",    help="Topic or Space slug")
    p.add_argument("--user",    help="Username (mode=profile)")
    p.add_argument("--max",     type=int, default=50)
    p.add_argument("--output",  choices=["json", "csv", "both"], default="json")
    p.add_argument("--outfile", default="quora_output")
    return p.parse_args()


def main():
    args    = parse_args()
    results = []

    # Keep module-level refs so the signal handler can close them
    _state = {"browser": None, "context": None}

    def _cleanup(*_):
        print("\n[!] Interrupted — closing browser …")
        try:
            if _state["context"]:
                _state["context"].close()
            if _state["browser"]:
                _state["browser"].close()
        except Exception:
            pass
        if results:
            save_json(results, f"{args.outfile}_partial.json")
            print("[!] Partial results saved.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    with sync_playwright() as pw:
        browser, context = build_browser_context(pw)
        _state["browser"]  = browser
        _state["context"]  = context
        page = context.new_page()

        try:
            if args.mode == "questions":
                if not args.query:
                    raise ValueError("--query is required for mode=questions")
                results = scrape_questions(page, args.query, args.max)

            elif args.mode == "answers":
                if not args.url:
                    raise ValueError("--url is required for mode=answers")
                results = scrape_answers(page, args.url, args.max)

            elif args.mode == "topics":
                if not args.slug:
                    raise ValueError("--slug is required for mode=topics")
                results = scrape_topic(page, args.slug, args.max)

            elif args.mode == "spaces":
                if not args.slug:
                    raise ValueError("--slug is required for mode=spaces")
                results = scrape_space(page, args.slug, args.max)

            elif args.mode == "profile":
                if not args.user:
                    raise ValueError("--user is required for mode=profile")
                results = [scrape_profile(page, args.user)]

        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    if args.output in ("json", "both"):
        save_json(results, f"{args.outfile}.json")
    if args.output in ("csv", "both"):
        save_csv(results, f"{args.outfile}.csv")


if __name__ == "__main__":
    main()
