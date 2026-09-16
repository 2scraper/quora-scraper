# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. **A patch release means "fixes", not "no flag moved"**:
a default that was measured to be wrong can change in a patch, and when one
does the release notes lead with it in a blockquote. That is the honest
reading — pretending every flag is frozen would mean either never fixing a
bad default or violating the promise quietly.

## [Unreleased]

### Fixed

- **The canary asserted a `title` floor of 100%, which this repo's own README
  says is wrong.** A question page carries answers to other questions, and a
  few of those name their question nowhere on the page; a null there is the
  honest answer and inventing one from the slug would be a guess. Measured
  252 of 260 on a live run, so the floor is 0.90. Caught by the canary's
  second dispatch — a canary failing on its own repo's documented behaviour
  is it working.

### Added

- The canary now asserts the two title regressions a live run found, neither
  of which a coverage number can see: no title carries the "Related" badge
  Quora renders inside the title node, and no row wears the page's own
  question while answering a different one.

## [0.1.1] — 2026-09-16

The canary's first dispatch, run without a proxy secret exactly as §15 says
to, failed — and it was right to. Three fixes, all in the same area and all
found by that one run.

### Fixed

> **A run can no longer crash on a page that navigates under it.** If you
> scripted around exit 1 from this scraper, that path is gone: the same event
> now reports exit 3 or 6 with a reason.

- **`_count` crashed the Playwright engine.** A scroll batch was polling the
  card count when the page navigated — Cloudflare's challenge can arrive at
  any moment here — and Playwright raised `Execution context was destroyed,
  most likely because of a navigation`. Exit 1, a crash, where the honest
  answer was "blocked". Its two twins had guarded that call from the start;
  this is the same shape as the refused-GraphQL threshold, where two engines
  agreed and one did not.
- **`_scroll_to_bottom` was unguarded in the same engine**, with the same
  exposure. Found immediately by the check written for the first one.
- **A feed that stopped growing because the page was REPLACED was reported as
  `exhausted`** — a COMPLETE stop reason — so a run blocked halfway would
  have claimed the listing ended, keeping its earlier batches and calling
  them the whole thing (§7). All three engines now classify the current page
  before drawing that conclusion, and report `blocked_mid_scroll` instead.

### Added

- A check that every driver primitive the scroll loop drives (`_count`,
  `_page_height`, `_scroll_to_bottom`) catches its driver's error in all
  three engines, asserted structurally from the AST so it needs no engine
  library. Verified by reverting both fixes: it names the engine and the
  primitive.

## [0.1.0] — 2026-09-16

First release on this scraper family's architecture. The repository
previously held three standalone scripts with no shared schema, no run
metadata, no tests and no exit-code contract; none of that code survives.

### Added

- **Three modes, one row.** `--mode topic` (a topic's answer feed),
  `--mode question` (one question's answers) and `--mode profile` (one
  author's answers), all yielding the same `Answer` row. The mode is inferred
  from the URL and passing one that disagrees is an error rather than an
  override.
- **All twenty-four Quora language sites**, taken from Quora's own switcher at
  `/about/languages` rather than guessed. Space and Quora Session subdomains
  are supported too; `help.`, `contests.` and the blogs are refused with their
  own reason rather than with "not a Quora site".
- **Two read paths, with provenance in a column.** Quora's inlined GraphQL
  payloads are the primary source and the rendered cards the fallback;
  `data_source` on every row says which built it. There is no JSON-LD on this
  site at all — 0 blocks across five page kinds.
- **A stable 30-column schema** in JSON and CSV, a `<out>.meta.json` sidecar
  per run, and the family's exit-code contract (`0` ok, `1` crash, `2` usage,
  `3` blocked, `4` empty, `5` remote API, `6` partial).
- **Four engines**: Playwright (primary), Selenium, pyppeteer, and a
  2Captcha Scraper API client. The three browser engines share the parser,
  the page policy, the writers, the proxy pool and the solver.
- `diff_runs.py`, keyed on `sku`, with a `source_changed` bucket for the case
  this site actually produces: a topic row's null counts against the same
  answer's real ones read off its question page.
- Proxy pool with rotation, credential masking and argv safety;
  `.env` loading via `env_config.py`; 2Captcha fingerprint support on the
  Playwright and Selenium engines.
- An offline suite of 500+ checks that passes with no engine library
  installed, fixtures cut from real captures and proven to parse identically
  to them, a daily two-run canary, and a Docker image built and exercised in
  CI.

### Notes on what this site does, all measured 2026-09-15

- **Nothing is server-rendered**: 0 occurrences of Quora's own `q-box` class
  in the raw response body of six captures, against 385-1165 in the hydrated
  document. `shell` is therefore the normal first page state, not a fault.
- **There is no per-page URL in any mode.** `?page=2` does not fail — it is
  ignored and the feed returns its first items again. A "page" here is one
  settled scroll batch, and `--concurrency` above 1 is refused with that
  reason.
- **A topic page's inlined payload carries 0 answer objects**, so `upvotes`,
  `views`, `shares`, `comments`, `created_at`, `answer_id` and `question_id`
  are null on every topic row.
- **The block is Cloudflare's managed challenge** — HTTP 403,
  `cf-mitigated: challenge`, `cType: 'managed'`, no sitekey anywhere. It
  clears on retry while the address is rested and stops clearing once it is
  busy (8 of the first 14 fetches served, then 0 of the next ~30 across three
  hours from one address, a 45-minute rest included), and no captcha solve is
  ever attempted on it.
- **No paid product is required to get data**: 8 of the first 14 fetches from
  an ordinary DATACENTRE address with no key and no proxy were served in
  full — a stronger result than a residential one would have been, since
  datacentre ranges are what a bot manager scores first. Keeping it up is what costs — see the rate note above.
- **A question page carries OTHER questions' answers**: related ones behind a
  badge rendered inside the title node, and merged duplicates behind an
  "Originally Answered:" banner. Both are read structurally, because both
  labels are localised across twenty-four language sites.

### The paid paths, run rather than assumed (§16)

- **Captcha solving**: the key works (balance read live), and there is
  nothing on this site to spend it on — see above.
- **Fingerprint API**: works, and running it found a real defect. The
  Selenium engine read the user agent from a key the API returns in NEITHER
  of its two formats, so `--fingerprint` there set no UA at all and presented
  a Windows fingerprint's screen, locale and timezone over a local Chromium's
  UA. That is the identity MISMATCH the flag exists to avoid, and it is the
  same defect §16 records as having been live in four sibling repos. Fixed,
  routed through the shared helper, and asserted for all three engines.
- **Scraper API**: works, and is the richest path per row this repo has on a
  QUESTION url — 6 answers, 6 of 6 payload-backed, every column populated,
  $0.0005. On a TOPIC url it returns nothing at all, with or without
  `--wait-element`, because a topic's answers arrive over a later XHR and the
  API returns the served response rather than a rendered DOM.
- **Proxies**: the PATH is verified, the upstream is not. Two working
  credentials were tried from this machine and both were refused at the
  source — the SOCKS5 endpoint returns auth-failure for the given string, for
  every variant of it, for the bare login AND for deliberately wrong
  credentials, which means it is refusing the source rather than reading the
  password; the HTTP endpoint accepts TCP and never answers. The same API key
  authenticates fine from the same machine, so the account and the network
  are not the issue. What that DID exercise, for the first time, is this
  repo's own proxy handling: credentials masked in every log line, an
  authenticated SOCKS5 URL refused up front with the reason rather than
  silently stripped, and an unreachable exit reported as a failed run rather
  than as a crash — which is where the exit-code fix below came from.
- **Scraping Browser**: NOT verified. 401 on every zone tried, which is a
  separate subscription rather than anything in this code.

### Fixed

- **Zero rows no longer always means exit 4.** An unreachable proxy produced
  exit 4 — "ran fine, found nothing" — on a question with 253 answers, while
  the sidecar beside it correctly said `status: failed`, `pages_completed:
  0`. Zero rows has three causes and they are different things: blocked
  (exit 3), never reached the site (exit 6), and the site's answer was
  nothing (exit 4). A pipeline branching on the exit code, which is what
  this family says exit codes are for, would have recorded an empty
  catalogue. This is shared family code and the same mapping is in every
  sibling repo.

### Known limitations

- **How many answers a question page gives you is decided at LOAD.** Four
  loads of one URL gave 5, 13, 12 and 259, and in the short ones the scroll
  reached the document's end and Quora never fetched more. A short run is a
  short SESSION, not a broken scroll; re-running re-rolls it, and the run
  warns rather than reporting a short result as a complete one.
- Selenium cannot authenticate a proxy, and cannot use an authenticated CDP
  endpoint. Both are reported loudly rather than silently half-working.
- `--fingerprint` and `--locale` exist on Playwright and Selenium and not on
  pyppeteer. The offline suite asserts that difference.

[Unreleased]: https://github.com/2scraper/quora-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/quora-scraper/releases/tag/v0.1.0
