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

## [0.1.0] — 2026-09-15

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
- An offline suite of 450+ checks that passes with no engine library
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
  clears on retry, it tracks the address's recent request rate (one-in-four
  rising to three-in-three over twenty minutes from one address), and no
  captcha solve is ever attempted on it.
- **No paid product is required to get data**: 8 of 14 fetches from an
  ordinary residential address with no key and no proxy were served in full.

### Known limitations

- `scraper_api_client.py` has **not been run against this site**. It ships
  because the client is family code and the parser it calls is measured, but
  nothing about its behaviour here is claimed.
- Selenium cannot authenticate a proxy, and cannot use an authenticated CDP
  endpoint. Both are reported loudly rather than silently half-working.
- `--fingerprint` and `--locale` exist on Playwright and Selenium and not on
  pyppeteer. The offline suite asserts that difference.

[Unreleased]: https://github.com/2scraper/quora-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/quora-scraper/releases/tag/v0.1.0
