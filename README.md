# quora-scraper

> Open-source Quora scraper · Playwright · Selenium · Puppeteer · 2captcha · 2prx.com

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![2captcha](https://img.shields.io/badge/CAPTCHA-2captcha.com-orange)](https://2captcha.com)
[![Proxies](https://img.shields.io/badge/Proxies-2prx.com-blue)](https://2prx.com)

---

## What it scrapes

| Content type | Fields collected |
|---|---|
| **Questions** | title, URL, answer count |
| **Answers** | question, author, content, upvotes |
| **Topics** | topic name, question list |
| **Spaces** | space name, post titles, authors |
| **Profiles** | display name, bio, followers, following |

All scrapers export data to **JSON** and/or **CSV**.

---

## Scrapers

| File | Runtime | Notes |
|---|---|---|
| `quora_scraper_playwright.py` | Python 3.10+ | **Primary** — fastest & most reliable |
| `quora_scraper_selenium.py` | Python 3.10+ | Uses `undetected-chromedriver` |
| `quora_scraper_puppeteer.js` | Node.js 18+ | Uses `puppeteer-extra-plugin-stealth` |

---

## Quick start

### Playwright (Python — recommended)

```bash
pip install playwright requests
playwright install chromium

# Scrape questions
python quora_scraper_playwright.py --mode questions --query "machine learning" --output json

# Scrape answers from a specific question
python quora_scraper_playwright.py --mode answers --url "https://www.quora.com/What-is-machine-learning" --output csv

# Scrape a topic
python quora_scraper_playwright.py --mode topics --slug "Machine-Learning" --output both

# Scrape a Space
python quora_scraper_playwright.py --mode spaces --slug "AI-and-Machine-Learning" --output json

# Scrape a profile
python quora_scraper_playwright.py --mode profile --user "Andrew-Ng" --output json
```

### Selenium (Python)

```bash
pip install selenium undetected-chromedriver requests

python quora_scraper_selenium.py --mode questions --query "python programming" --max 30 --output csv
```

### Puppeteer (Node.js)

```bash
npm install puppeteer puppeteer-extra puppeteer-extra-plugin-stealth axios

node quora_scraper_puppeteer.js --mode questions --query "deep learning" --output json
```

---

## CLI flags (all scrapers)

| Flag | Description | Default |
|---|---|---|
| `--mode` | `questions` / `answers` / `topics` / `spaces` / `profile` | required |
| `--query` | Search keyword (mode=questions) | — |
| `--url` | Question URL (mode=answers) | — |
| `--slug` | Topic or Space slug (mode=topics / spaces) | — |
| `--user` | Username (mode=profile) | — |
| `--max` | Max items to collect | 50 |
| `--output` | `json` / `csv` / `both` | json |
| `--outfile` | Output filename without extension | quora_output |

---

## CAPTCHA solving — 2captcha.com

Quora sometimes shows reCAPTCHA v2/v3. All three scrapers integrate with **[2captcha.com](https://2captcha.com)** to solve them automatically.

```bash
# Set your API key as an environment variable
export TWOCAPTCHA_API_KEY="your_key_here"
```

Get an API key at **https://2captcha.com** — plans start at $0.001/captcha.

---

## Proxies — 2prx.com

To avoid IP blocks, route traffic through **[2prx.com](https://2prx.com)** residential or datacenter proxies.

```bash
export PROXY_HOST="gate.2prx.com"
export PROXY_PORT="7000"
export PROXY_USER="your_proxy_user"
export PROXY_PASS="your_proxy_pass"
```

Then run any scraper — proxy support is automatic when `PROXY_USER` is set.

---

## Anti-detect browser

For large-scale or high-stealth scraping, we offer a proprietary **anti-detect browser** that spoofs hardware fingerprints, canvas, WebGL, fonts, and audio APIs at the browser level — going beyond what Playwright/Selenium stealth plugins can achieve.

👉 Available as a paid add-on at **[2captcha.com](https://2captcha.com)**

---

## Environment variables reference

| Variable | Description |
|---|---|
| `TWOCAPTCHA_API_KEY` | Your 2captcha.com API key |
| `PROXY_HOST` | Proxy hostname (default: `gate.2prx.com`) |
| `PROXY_PORT` | Proxy port (default: `7000`) |
| `PROXY_USER` | Proxy username |
| `PROXY_PASS` | Proxy password |

---

## Output format

### JSON
```json
[
  {
    "type": "question",
    "title": "What is machine learning?",
    "url": "https://www.quora.com/What-is-machine-learning",
    "answer_count": "1.2k answers",
    "scraped_at": "2024-06-01T12:00:00"
  }
]
```

### CSV
```
type,title,url,answer_count,scraped_at
question,What is machine learning?,https://...,1.2k answers,2024-06-01T12:00:00
```

---

## Notes

- Quora is a JavaScript-heavy SPA; always wait for `networkidle` before scraping.
- Quora may update its class names — if selectors break, inspect the page and update accordingly.
- Respect Quora's Terms of Service; use reasonable delays and rate limits.
- `--max` limits items per run to avoid overloading the target server.

---

## License

MIT © [2scraper](https://github.com/2scraper)
