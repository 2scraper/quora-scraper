# quora-scraper

[![release](https://img.shields.io/github/v/release/2scraper/quora-scraper?sort=semver)](https://github.com/2scraper/quora-scraper/releases)
[![tests](https://github.com/2scraper/quora-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/quora-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/quora-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/quora-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.12-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer-lightgrey)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-brightgreen)](#do-you-need-any-of-the-paid-products)

Scrapes **Quora answers** — off a topic feed, a question page or a profile —
into JSON or CSV with a stable column schema, a run-metadata sidecar, and
exit codes that tell "blocked" from "empty" from "partial".

Works on all twenty-four of Quora's language sites, which are twenty-four
different catalogues: `www.quora.com` plus `es.`, `fr.`, `de.`, `it.`, `jp.`,
`id.`, `pt.`, `hi.`, `nl.`, `da.`, `fi.`, `no.`, `sv.`, `mr.`, `bn.`, `ta.`,
`ar.`, `he.`, `gu.`, `kn.`, `ml.`, `te.` and `pl.quora.com`.

> **Read this first if you only read one thing.**
> **Which URL you point it at decides what you get back**, and the difference
> is not cosmetic. Quora renders none of its content on the server; what it
> *does* send is its own GraphQL results, inlined into the page — and only
> some page kinds carry the answers.
>
> | you ask for | rows | upvotes, views, created_at, full text |
> |---|---|---|
> | a **question** page | its answers | **yes** — from the inlined payload |
> | a **profile** | that author's answers | **yes**, for the ones the payload covers |
> | a **topic** feed | the topic's answers | **no** — card only, body truncated to 3 lines |
>
> A topic page's answers arrive over a later XHR, so its inlined payload holds
> **0 answer objects** (measured, two topic captures in two languages). The
> `data_source` column on every row says which view built it, and the run
> says so at startup.

---

## Install and run

```bash
git clone https://github.com/2scraper/quora-scraper && cd quora-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py \
  --url "https://www.quora.com/What-is-machine-learning-4" \
  --pages 3 --out ml
```

That writes `ml.json`, `ml.csv` and `ml.meta.json`.

`chromium`, not `chrome` — and unlike a sibling repo in this family that is
not a compromise. See [below](#do-you-need-any-of-the-paid-products).

**Install exactly one engine.** The three engines' pins are mutually
unsatisfiable (`playwright` and `pyppeteer` disagree on `pyee`; `pyppeteer`
and `selenium` on `urllib3`). Use a virtualenv per engine if you need more
than one.

---

## The six things that will surprise you

### 1. `--pages` counts scroll batches, not addresses

Quora has **no per-page URL in any mode**. There is no `link[rel=next]`, no
pagination control, no numbered anchors — and `?page=2` does not fail. It is
ignored, and the feed comes back with its first items again.

That is the dangerous part. A scraper built on the obvious convention would
fetch `?page=2`, find no answer it had not already seen, conclude the listing
was exhausted and report a **complete** run holding one batch. So:

- a "page" here is **one settled scroll batch**;
- the listing ends when a batch adds no `sku` the run has not already seen;
- **`--concurrency` above 1 is refused, with that reason.** Batch 5 exists
  only inside the browser that scrolled through batches 1-4; there is nothing
  to hand a second worker. Run several topics in parallel instead, one
  process each.

Each batch re-parses the whole feed, so `rows_new_per_batch` in the sidecar is
the number worth reading rather than the raw per-batch count.

### 2. A topic run has no upvotes, views or creation times

Not a bug and not a threshold — it is what the site sends. On a topic page:

```
inline GraphQL payloads in the served HTML   3
Answer objects in them                       0
```

against a question page's 16 payloads carrying 5 answers with their numeric
ids, full text, `numUpvotes`, `numViews`, `numShares` and an
epoch-microsecond `creationTime`.

So `upvotes`, `views`, `shares`, `comments`, `created_at`, `answer_id` and
`question_id` are **null on every topic row**, and `text` there is what the
card rendered — which Quora truncates to three lines. `text_chars` makes that
visible at a glance and `data_source` says which view built the row.

**If you want the counts, point it at a question or a profile.**

### 3. The block is Cloudflare, and the address's recent rate is what decides

What a refusal looks like: HTTP 403, `cf-mitigated: challenge`,
`<title>Just a moment...</title>`, about 6 KB of body, and `cType: 'managed'`
in the challenge's own config.

Measured 2026-09-15, one residential address, about forty-five fetches over
three hours:

| window | fetches | served | challenged |
|---|---|---|---|
| first ~20 minutes | 14 | 8 | 6 |
| everything after | ~30 | **0** | **all of them** |

Three things follow:

- **A rested address recovers.** Early in that window the same URL that
  answered 403 answered 200 with the full feed on the next attempt about a
  minute later, from the same address and the same browser. So the first
  thing to try is `--retries 4 --retry-delay 40`.
- **A busy address does not, and it does not recover quickly.** After about
  thirty fetches every subsequent attempt was challenged, and a **45-minute
  rest did not clear it** — nine further attempts across the next twenty
  minutes were all refused. Nothing about that address changed except how
  much it had been fetching.
- **The score follows the ADDRESS, not the language site.** `es.quora.com`
  refused the same address at the same moment `www.quora.com` did, with three
  attempts each. Switching hosts is not a way around it.

So `--delay` is the cheapest lever, `--proxy-file` is the one that scales, and
"it worked an hour ago" is not evidence that it will work now.

And one thing that follows for your wallet: a **managed** challenge carries no
sitekey — 0 `data-sitekey` attributes and 0 Turnstile iframes on the one
measured — so there is nothing for a captcha solver to answer. This scraper
never attempts a solve on it and never charges you for one.

If a run exits 3, the interstitial is saved beside your output as
`<out>_page<N>_debug.html` with a screenshot next to it.

### 4. How many answers you get is decided at LOAD, not by scrolling

Four loads of the same question URL, minutes apart, same machine, same
browser build:

| | | | |
|---|---|---|---|
| cards at first paint | 5 | 13 | 5 |
| after scrolling to the bottom and waiting 20s | 5 | 13 | **259** |

In the short sessions the scroll worked — `scrollY` reached the document's end
and stayed there — and Quora simply never fetched more. So a run that comes
back with six rows out of a question's stated 253 is **not** a broken scroll,
and no amount of extra patience will fix it.

**Re-run it.** A fresh browser re-rolls the variant. The run warns when it
sees this, rather than quietly reporting a short result as a complete one.

### 5. A Space or a Quora Session answer lives on its own subdomain

Four of one profile's eighteen answer permalinks were on
`{session}.quora.com`, with **no `/answer/` segment at all** — which is the
same URL shape as a question page on `www`. That is why the card anchor here
is Quora's own `a.answer_timestamp` class rather than a URL pattern: no
pattern can tell those two apart.

On such a host the permalink is the question's path plus a per-answer suffix,
and Quora's own payload reports a *third* value again:

```
permaUrl       …quora.com/How-important-is-…-in-Machine-Learning-1
question.url   …quora.com/How-important-is-…-in-Machine-Learning
question.slug  How-important-is-…-in-Machine-Learning-5
```

So `question_url` is exact when the payload reached the row and is the
permalink itself otherwise — a stated approximation rather than a guess
dressed up as a reading.

### 6. Traps that look like bugs

- **`answer_count` is much larger than the rows you got.** One question page
  renders twelve answers and reports 253. That is Quora rendering a fraction
  and extending it on scroll, not a gap this run failed to close — which is
  why it sits beside the row count in the sidecar rather than being
  subtracted from it.
- **`author_credential` is null on some rows.** 28 of 30, 27 of 30, 14 of 18
  and 10 of 13 across four captures. The gap is authors who have not written
  one.
- **`date_text` is a display string, never a date.** "Aug 14", "2y",
  "3 años" — in the page's own language, and "2y" cannot be resolved to a
  day. `created_at` is the real timestamp and comes only from the payload.
- **`language` is null on a Space or Session row.** Those hosts are not
  language sites, and their language is whatever their writers use.
- **A run that finds nothing writes nothing**, so a bad run cannot overwrite
  last night's good file. `--allow-empty` is the opt-out; the exit code is 4
  either way.

---

## Output

One row per answer. Same columns in JSON and CSV, same order.

```json
{
  "source": "www.quora.com",
  "scraped_at": "2026-09-15T09:14:02.881293+00:00",
  "url": "https://www.quora.com/What-is-machine-learning-4/answer/Someone-1",
  "sku": "www.quora.com/What-is-machine-learning-4/answer/Someone-1",
  "title": "What is machine learning?",
  "question_url": "https://www.quora.com/What-is-machine-learning-4",
  "question_id": "4256391",
  "answer_id": "1477743744691014",
  "answer_count": 253,
  "author": "Someone",
  "author_url": "https://www.quora.com/profile/Someone-1",
  "author_credential": "former Data Scientist at a large company",
  "author_is_verified": false,
  "text": "Hey, future ML engineers!! …",
  "text_chars": 2150,
  "upvotes": 125,
  "views": 1733,
  "shares": 0,
  "comments": 2,
  "created_at": "2024-03-07T08:31:46.062197Z",
  "updated_at": null,
  "date_text": "2y",
  "is_machine_answer": false,
  "is_translated": false,
  "language": "en",
  "category": "What-is-machine-learning-4",
  "mode": "question",
  "data_source": "dom+inline",
  "page": 1,
  "position": 3
}
```

`sample_output.json` and `sample_output.csv` are cut from a real run.

**There is no `price`, `currency`, `rating` or `in_stock` column**, unlike the
rest of this scraper family. Quora has none of those things, and six columns
null on every row of every run of every mode is worse than six missing ones.
`upvotes` is a count rather than a score and is not written under `rating`.

### Exit codes

| code | meaning |
|---|---|
| 0 | rows written |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked — Cloudflare's challenge survived the retries |
| 4 | ran fine, found nothing |
| 5 | a remote API refused the connection |
| 6 | partial — see `stop_reason` in the sidecar |

`<out>.meta.json` records `status`, `stop_reason`, which batches failed *by
number*, the scroll trace, `rows_new_per_batch`, `answers_available` and
`inline_payload_rows`. A **failed** run writes no sidecar, so a `"failed"`
file can never sit beside good data.

### Diffing two runs

```bash
python3 diff_runs.py --old ml.2026-09-01.json --new ml.2026-09-08.json
```

Keyed on `sku`. The bucket to know about here is **`source_changed`**: a topic
row has null counts and the same answer read off its question page has real
ones, so diffing the two would report every counter as having appeared from
nowhere. That goes in its own bucket and `--fail-on-change` ignores it.

`--price-tolerance-pct` keeps the family's name and here means a COUNT
tolerance. Unlike in most of this family it has a real use: Quora's view and
upvote counts are live, so a monitor watching for an answer taking off wants
a threshold — while one watching for an edit wants `text_chars`, which the
tolerance never absorbs.

---

## Engines

| script | driver | notes |
|---|---|---|
| `playwright_scraper.py` | Playwright | **primary.** The only one with `--fingerprint` and `--locale`. |
| `selenium_scraper.py` | Selenium + chromedriver | Cannot authenticate a proxy, and cannot use an authenticated CDP endpoint. |
| `puppeteer_scraper.py` | pyppeteer | Parity engine. pyppeteer is effectively unmaintained and its own README points at Playwright. |
| `scraper_api_client.py` | 2Captcha Scraper API | One HTTP request, no local browser. **The richest path per row on a question URL** and useless on a topic one — see below. |

All three browser engines share `product_parser.py`, `page_flow.py`,
`output_writer.py`, `proxy_pool.py` and `captcha_solver.py`, so they agree on
rows, exit codes, run status and whether a run spends money. The offline suite
binds every shared-module call in every engine against the callee's real
signature, because two engines in a sibling repo once called one with a
keyword where the parameter was positional and both crashed on their first
fetch.

Known limits, stated rather than left to be discovered:

- **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password.
- **Selenium cannot authenticate a proxy at all.** Credentials are stripped
  with a warning rather than silently half-working.
- `--fingerprint` and `--locale` exist on Playwright and Selenium and not on
  pyppeteer. The offline suite asserts that difference, so closing it needs a
  README edit rather than a quiet patch.

---

## The four paths, measured against each other

All four read the same rows into the same columns. What differs is how many
rows and how much of each, and on this site the difference is larger than
usual — measured 2026-09-15 on the same question URL:

| path | rows | payload-backed | cost |
|---|---|---|---|
| a browser engine, 1 scroll batch | 260 | 6 | free |
| Scraper API, one request | 6 | **6 of 6** | $0.0005 |

The API returns the SERVED response, so every row it produces comes out of
Quora's inlined payload with its upvote count, view count, creation time,
numeric ids and full text. A browser engine reaches forty times as many rows
and reads almost all of them off cards, which carry none of those columns.

On a TOPIC url the API returns **nothing at all** — 99,833 bytes, 3 inline
payloads, 0 answer objects, 0 cards — because a topic's answers arrive over a
later XHR. `--wait-element 'a.answer_timestamp'` does not help: the same URL
took sixteen seconds instead of three and came back with the same shell.

So: the API for a question read in depth-of-columns, a browser engine for a
topic feed or for breadth of rows.

## Do you need any of the paid products?

**Not to get started.** Playwright's own bundled Chromium, from an ordinary
residential address, with no key and no proxy, was served HTTP 200 and the
full feed on **eight of the first fourteen** fetches on 2026-09-15, and the
six refusals cleared on the next attempt.

**To keep going, yes.** Everything after the first fourteen fetches from that
same address was challenged — about thirty attempts over three hours, a
45-minute rest included, and a second language host made no difference.
Nothing about the address changed except how much it had been fetching.

So what the paid products buy here is **rate**: many addresses is how you
fetch a lot of Quora, and one address plus a generous `--delay` is how you
fetch a little of it for free.

| product | what it buys here |
|---|---|
| [Proxies](https://2captcha.com/proxy) | spreads the request rate across addresses, which is the real limit |
| [Scraping Browser API](https://2captcha.com) | a remote browser over CDP, with persistent profiles — no browser infrastructure of your own |
| [Fingerprints](https://2captcha.com) | a consistent device identity across runs |
| [Scraper API](https://2captcha.com) | a question's answers in one request, with every column populated, for $0.0005 — measured. Not a topic feed: see the table below. |
| [Captcha solving](https://2captcha.com) | **nothing on this site.** Quora's refusal is a *managed* Cloudflare challenge with no sitekey; there is nothing to solve. The detector is kept because a rendered challenge is possible and a scraper that cannot name what stopped it is much harder to fix. |

All four sit behind one key. Put it in `.env` (see `.env.example`) rather than
on a command line — a secret in `argv` is readable by anything that can run
`ps` and lands in shell history.

```bash
python3 env_config.py     # prints what was picked up, WITHOUT secrets
```

---

## What Quora publishes, and where this reads it from

No JSON-LD anywhere: **0** `application/ld+json` blocks on topic, question,
profile, answer-permalink and language pages alike. And no server-rendered
content: **0** occurrences of Quora's own `q-box` class in the raw response
body of six captures, against 385-1165 in the hydrated document.

What the server does send is its own query results:

```
window.ansFrontendGlobals.data.inlineQueryResults.results["<hash>"]
    .push("{\"data\":{…}}")
```

That is this site's structured data, and where it covers a row it is richer
than anything rendered. Where it does not, the rendered card is read through
Quora's own test and logging hooks — `a.answer_timestamp`,
`.puppeteer_test_question_title`, `.spacing_log_answer_content`,
`.spacing_log_answer_header` — which are semantic names rather than build
hashes and were stable across every capture and every language host.

Measured coverage on four captures, two languages:

| capture | rows | author | body | credential | payload-backed |
|---|---|---|---|---|---|
| topic (en) | 30 | 30 | 30 | 28 | 0 |
| topic (es) | 30 | 30 | 30 | 27 | 0 |
| question (en) | 13 | 13 | 13 | 10 | 5 |
| profile (en) | 18 | 18 | 18 | 14 | 3 |

---

## Testing

```bash
python3 smoke_test.py        # offline, no network, no engine library needed
pytest                       # the same checks, wrapped as one pytest test
```

The suite passes with **no engine installed at all** — every engine import is
guarded and the skip is reported, because "skipped, engine absent" reads
exactly like a real import error. CI's `engine-smoke` job installs each engine
in its own virtualenv and fails if any group reports a skip.

Fixtures are cut from real captures by `make_fixtures.py`, which proves each
one parses **identically** to its untrimmed original, column for column.
Author names, profile slugs, credentials, answer bodies and cited links are
replaced with placeholders first; question titles, ids, counts, timestamps and
Quora's own markup stay verbatim.

`canary.yml` runs two real runs daily — a question URL for the inline-payload
path and a topic URL for the DOM-only one — and asserts that each behaves the
way this README says it does.

---

## Scope and licence

This scrapes **public pages**: topic feeds, question pages and profiles, as an
anonymous reader sees them. It does not log in, does not touch anything behind
Quora's sign-in wall, and `/search` is refused outright with that reason —
Quora answers an anonymous search request with the language home page rather
than with results.

Respect the site's terms and robots policy, and keep your request rate low —
the measurements above are as much about being a good citizen as about
getting data.

MIT. See [LICENSE](LICENSE), [CONTRIBUTING.md](CONTRIBUTING.md),
[SECURITY.md](SECURITY.md) and [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
