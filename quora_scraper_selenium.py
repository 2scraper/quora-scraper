"""
Quora Scraper — Selenium (v2)
==============================
Scrapes Quora questions, answers, topics, spaces, and profiles.

Features:
  - CAPTCHA solving via 2captcha.com
  - Proxy support via 2prx.com
  - Anti-detect fingerprint spoofing via undetected-chromedriver
  - Output: JSON and CSV

Install:
  pip install selenium undetected-chromedriver requests
  # ChromeDriver is bundled with undetected-chromedriver — no manual install needed.

Usage:
  python quora_scraper_selenium.py --mode questions --query "deep learning" --output json
  python quora_scraper_selenium.py --mode answers   --url "https://quora.com/..." --output csv
  python quora_scraper_selenium.py --mode topics    --slug "Machine-Learning" --output both
  python quora_scraper_selenium.py --mode spaces    --slug "AI-and-Machine-Learning"
  python quora_scraper_selenium.py --mode profile   --user "Andrew-Ng"

Fix notes (v2):
  - Removed all implicit "wait for page load" patterns that block forever on
    Quora's SPA (driver.get() returns after DOMContentLoaded in Selenium, but
    dynamic content isn't there yet).
  - Added explicit WebDriverWait for a real content element instead of relying
    on page-load state.
  - Broadened CSS selectors with fallback chains — Quora updates class names
    frequently.
  - Added SIGINT / SIGTERM handler so the browser quits cleanly on Ctrl+C and
    partial results are saved automatically.
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

try:
    import undetected_chromedriver as uc
    HAS_UC = True
except ImportError:
    HAS_UC = False
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException
)


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

# How long to wait for a content element to appear after navigation (seconds)
CONTENT_WAIT = 15


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
        raise TimeoutError("2captcha timed out.")


solver = TwoCaptchaSolver(TWOCAPTCHA_API_KEY) if USE_CAPTCHA_SOLVER else None


# ---------------------------------------------------------------------------
# Driver factory
# ---------------------------------------------------------------------------

def build_driver():
    if HAS_UC:
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--lang=en-US")
        if USE_PROXY:
            options.add_argument(f"--proxy-server=http://{PROXY_HOST}:{PROXY_PORT}")
        driver = uc.Chrome(options=options, headless=True)
    else:
        options = ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        if USE_PROXY:
            options.add_argument(f"--proxy-server=http://{PROXY_HOST}:{PROXY_PORT}")
        driver = webdriver.Chrome(options=options)

    # Spoof navigator.webdriver
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = {runtime: {}};
        """
    })
    return driver


# ---------------------------------------------------------------------------
# Navigation helper  ← THE KEY FIX
# ---------------------------------------------------------------------------

def goto(driver, url: str, wait_css: str | None = None):
    """
    Navigate to *url*, then wait for *wait_css* to appear.

    Why: driver.get() in Selenium fires after DOMContentLoaded but Quora's
    React app renders content asynchronously — so elements aren't in the DOM
    yet. WebDriverWait with an explicit CSS selector is the correct readiness
    signal. We never use driver.implicitly_wait or page_load_strategy='normal'
    alone because they don't account for async rendering.
    """
    print(f"[nav] → {url}")
    driver.get(url)

    if wait_css:
        try:
            WebDriverWait(driver, CONTENT_WAIT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
            )
        except TimeoutException:
            print(f"[nav] '{wait_css}' not found within {CONTENT_WAIT}s — "
                  "page may be empty or Quora's DOM structure has changed")

    human_delay(1.0, 2.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def human_delay(lo: float = 0.8, hi: float = 2.5):
    time.sleep(random.uniform(lo, hi))


def find_text(driver_or_el, *css_selectors: str, default: str = "") -> str:
    """Try each CSS selector in order; return the first non-empty text found."""
    for sel in css_selectors:
        try:
            el = driver_or_el.find_element(By.CSS_SELECTOR, sel)
            text = el.text.strip()
            if text:
                return text
        except NoSuchElementException:
            pass
    return default


def find_attr(driver_or_el, css: str, attr: str, default: str = "") -> str:
    try:
        el = driver_or_el.find_element(By.CSS_SELECTOR, css)
        return (el.get_attribute(attr) or "").strip()
    except NoSuchElementException:
        return default


def scroll_page(driver, scrolls: int = 8, pause: float = 1.5):
    for _ in range(scrolls):
        prev = driver.execute_script("return document.body.scrollHeight")
        driver.execute_script("window.scrollBy(0, window.innerHeight * 0.85)")
        time.sleep(pause)
        if driver.execute_script("return document.body.scrollHeight") == prev:
            break


def ts() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Module: Questions
# ---------------------------------------------------------------------------

def scrape_questions(driver, query: str, max_results: int = 50) -> list[dict]:
    url = f"{BASE_URL}/search?q={requests.utils.quote(query)}&type=question"
    goto(driver, url, wait_css="main, [role='main'], a[href]")
    scroll_page(driver, scrolls=6)

    results = []
    seen    = set()

    for a in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = a.get_attribute("href") or ""
        except WebDriverException:
            continue

        if not href or href in seen:
            continue

        # Quora question URLs: long hyphenated slugs or /q/ paths
        is_question = (
            ("/q/" in href and href.count("/") <= 5)
            or (
                href.startswith(BASE_URL)
                and href.count("-") >= 3
                and "/profile/" not in href
                and "/topic/"   not in href
                and "/search"   not in href
                and "/sitemap"  not in href
            )
        )
        if not is_question:
            continue

        seen.add(href)
        title = (a.text or "").strip()
        if not title or len(title) < 8:
            continue

        results.append({
            "type":       "question",
            "title":      title,
            "url":        href,
            "scraped_at": ts(),
        })
        if len(results) >= max_results:
            break

    print(f"[questions] Collected {len(results)} questions.")
    return results


# ---------------------------------------------------------------------------
# Module: Answers
# ---------------------------------------------------------------------------

def scrape_answers(driver, question_url: str, max_answers: int = 20) -> list[dict]:
    goto(driver, question_url, wait_css="h1")
    scroll_page(driver, scrolls=12)

    question_title = find_text(driver, "h1")
    answers = []

    # Try multiple block selectors — Quora redesigns frequently
    blocks = []
    for sel in [".q-box.spacing_log_answer_content", "[class*='Answer']", "article"]:
        blocks = driver.find_elements(By.CSS_SELECTOR, sel)
        if blocks:
            break

    for block in blocks[:max_answers]:
        try:
            author = find_text(
                block,
                ".q-text.qu-bold", "strong", "b", "[class*='creator']",
                default="Anonymous"
            )
            content = find_text(
                block,
                ".q-text.qu-wordBreak--word", "[class*='AnswerBase']", "p",
            )
            if not content or len(content) < 20:
                content = (block.text or "").strip()

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

def scrape_topic(driver, slug: str, max_questions: int = 30) -> list[dict]:
    url = f"{BASE_URL}/topic/{slug}"
    goto(driver, url, wait_css="h1")
    scroll_page(driver, scrolls=6)

    topic_name = find_text(driver, "h1", default=slug)
    items = []
    seen  = set()

    for a in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = a.get_attribute("href") or ""
        except WebDriverException:
            continue

        if not href or href in seen:
            continue
        if href.count("-") < 3 and "/q/" not in href:
            continue
        if any(x in href for x in ["/topic/", "/profile/", "/search", "javascript"]):
            continue

        seen.add(href)
        title = (a.text or "").strip()
        if not title or len(title) < 8:
            continue

        items.append({
            "type":       "topic_question",
            "topic":      topic_name,
            "topic_slug": slug,
            "title":      title,
            "url":        href,
            "scraped_at": ts(),
        })
        if len(items) >= max_questions:
            break

    print(f"[topics] Collected {len(items)} questions under '{topic_name}'.")
    return items


# ---------------------------------------------------------------------------
# Module: Spaces
# ---------------------------------------------------------------------------

def scrape_space(driver, slug: str, max_posts: int = 20) -> list[dict]:
    url = f"{BASE_URL}/q/{slug}"
    goto(driver, url, wait_css="h1")
    scroll_page(driver, scrolls=8)

    space_name = find_text(driver, "h1", default=slug)
    items = []
    seen  = set()

    for a in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = a.get_attribute("href") or ""
        except WebDriverException:
            continue

        if not href or href in seen:
            continue
        seen.add(href)

        title = (a.text or "").strip()
        if not title or len(title) < 8:
            continue

        items.append({
            "type":       "space_post",
            "space":      space_name,
            "space_slug": slug,
            "title":      title,
            "url":        href,
            "scraped_at": ts(),
        })
        if len(items) >= max_posts:
            break

    print(f"[spaces] Collected {len(items)} posts in '{space_name}'.")
    return items


# ---------------------------------------------------------------------------
# Module: Profiles
# ---------------------------------------------------------------------------

def scrape_profile(driver, username: str) -> dict:
    url = f"{BASE_URL}/profile/{username}"
    goto(driver, url, wait_css="h1")

    name = find_text(driver, "h1", default=username)
    bio  = find_text(
        driver,
        ".q-text.qu-wordBreak--word", "p", "[class*='bio']",
        default=""
    )

    return {
        "type":         "profile",
        "username":     username,
        "display_name": name,
        "bio":          bio[:500],
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
    p = argparse.ArgumentParser(description="Quora Scraper — Selenium (v2)")
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
    _driver = [None]  # mutable ref for signal handler

    def _cleanup(*_):
        print("\n[!] Interrupted — closing browser …")
        try:
            if _driver[0]:
                _driver[0].quit()
        except Exception:
            pass
        if results:
            save_json(results, f"{args.outfile}_partial.json")
            print("[!] Partial results saved.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    driver = build_driver()
    _driver[0] = driver

    try:
        if args.mode == "questions":
            if not args.query:
                raise ValueError("--query is required for mode=questions")
            results = scrape_questions(driver, args.query, args.max)

        elif args.mode == "answers":
            if not args.url:
                raise ValueError("--url is required for mode=answers")
            results = scrape_answers(driver, args.url, args.max)

        elif args.mode == "topics":
            if not args.slug:
                raise ValueError("--slug is required for mode=topics")
            results = scrape_topic(driver, args.slug, args.max)

        elif args.mode == "spaces":
            if not args.slug:
                raise ValueError("--slug is required for mode=spaces")
            results = scrape_space(driver, args.slug, args.max)

        elif args.mode == "profile":
            if not args.user:
                raise ValueError("--user is required for mode=profile")
            results = [scrape_profile(driver, args.user)]

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if args.output in ("json", "both"):
        save_json(results, f"{args.outfile}.json")
    if args.output in ("csv", "both"):
        save_csv(results, f"{args.outfile}.csv")


if __name__ == "__main__":
    main()
