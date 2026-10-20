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

> **Behaviour changes.** (1) A run through a proxy now makes one pre-check
> request before starting a browser. A proxy that answers **HTTP 407** or
> **402** ends the run with **exit 5** and
> `<out>.latest_attempt.meta.json` (`status: failed`, `stop_reason:
> proxy_auth_failed` / `proxy_balance_exhausted`) instead of timing out on
> every retry. (2) Through a local proxy, images, video/audio and fonts are
> **no longer downloaded**; `--load-media` (or `QUORA_LOAD_MEDIA=1`) restores
> them. Runs without a proxy or over `--cdp-endpoint` are unchanged.

### Added

- `proxy_check.py`: the proxy pre-check (one CONNECT plus `HEAD /robots.txt`
  on the target host, no third-party service) and media blocking for all
  three engines. The 407 message names `QUORA_PROXY` and says the gateway
  refused the credential (or the traffic balance is exhausted); a 402 says
  the proxy traffic/balance is exhausted. Neither prints the credential. A
  proxy that does not answer keeps the existing retry behaviour.
- `--load-media` on all three engines, and `QUORA_LOAD_MEDIA` in
  `.env.example`.
- Offline checks: a stub proxy that answers 407/402/200 or nothing, each
  engine driven to exit 5 without a browser on a 407, the attempt sidecar,
  the Playwright route handler, the pyppeteer Fetch patterns, the Selenium
  blocked-URL list, and when each engine installs blocking.

### Changed

- CI only: the canary now runs weekly (Mondays 06:20 UTC) and on a manual dispatch, with its optional secret `QUORA_PROXY` passed on every run; the daily schedule is removed.

## [0.2.0] — 2026-10-16

> **Behaviour changes in this batch.** (1) A page whose answers were counted
> but parsed to zero rows now ends the run with `stop_reason:
> parser_found_nothing` and **exit 5** (or 6 after earlier batches), not
> exit 4: it is a parser regression, not an empty feed. (2) An address that
> answers **HTTP 404/410** stops with `stop_reason: not_found` and exit 5
> instead of waiting, retrying and reporting an empty result (Playwright and
> pyppeteer; Selenium has no status to read). (3) `--pages` and
> `--concurrency` below 1, and negative `--delay`, `--retry-delay`,
> `--proxy-block-retries` and `--cdp-connect-timeout`, are now refused with
> **exit 2**; so are negative `--retries`/`--retry-delay` in
> `scraper_api_client.py` (0 stays legal there). (4) CSV cells that begin
> with `=`, `+`, `-`, `@`, a tab or a line break are prefixed with an
> apostrophe; JSON is unchanged. (5) The canary now fails when it did not
> parse real rows.

> **`--retries 0` is now refused (exit 2).** The page-load loop is
> `range(1, retries + 1)`, so 0 never meant "no retries": it meant no
> navigation attempt at all. `--retries` counts ATTEMPTS, the minimum is 1,
> and `--help` now says it is not a cap on total requests, because a page the
> site refuses is retried under its own, separate budget.

> **Correction to a claim in earlier READMEs.** They said a managed
> Cloudflare challenge "carries no sitekey … so there is nothing for a
> captcha solver to answer". The measurement was right and the conclusion was
> not. Cloudflare passes `sitekey`, `action`, `cData` and `chlPageData` to
> `turnstile.render()` once and keeps nothing, so the absence is a fact about
> the MARKUP; the arguments can be captured with an init script and 2Captcha
> solves the result as `TurnstileTaskProxyless`. The sentence read as "a
> 2Captcha key would not help you here", which is a claim about the product
> and was wrong.

### Fixed

- **One paid solve per page, at every call site.** Each engine checks for a
  captcha twice per attempt — before classifying the page and after — and
  only the second check was counted, so with the default block retries one
  page could buy three solves against a limit of one. Both call sites now
  claim from one per-page `SolveBudget`, immediately before the paid
  request; detecting nothing costs nothing, a failed purchase still counts,
  and a retry or rotation does not reset it. Verified with the solver
  stubbed: 3 purchases before, 1 after, in all three engines.
- **CSV formula neutralisation.** Answer bodies, author names and credential
  lines are written by strangers, and a cell beginning with a formula
  character is executed by a spreadsheet. Such string cells are escaped in
  the CSV only; numbers are untouched, and the sidecar records
  `csv_cells_escaped` (beneath any value the run itself supplies).
- **A parser failure is no longer reported as an empty feed.** See the note
  above; the canary fails on it by name.
- **The navigation's HTTP status reaches the page classifier.** It was
  discarded in all three engines, so a 401/403/429/5xx with no recognisable
  challenge markup, and a 404, were judged from the document alone.
- **The credential scan reads what it claimed to.** Exclusions were tested
  against the absolute path, so a clone under a directory called `venv`
  scanned 0 files and reported "nothing credential-shaped"; they are now
  relative to the repository, and a scan that reads nothing fails. JSON
  fixtures and every `.env*` file (an untracked `.env.bak` included) are now
  scanned, with a key-field pattern that also matches JSON-escaped quotes.
- **README: captcha solving.** The paid-products table still said a solver
  buys "nothing on this site … there is nothing to solve", which the
  correction above retracts. It now says this repo does not implement the
  Turnstile interception Quora's managed challenge needs; TROUBLESHOOTING
  and SECURITY say the same.
- **README: exit codes and `--allow-empty`.** Exit 5 covers any run that
  never got its content, not only a remote API refusal, and `--allow-empty`
  keeps the exit code that says why there were no rows (3, 4 or 5).
  `--concurrency` above 1 is documented as ignored with a warning, which is
  what it does.
- **Log text from another site.** Messages about "property cards", a
  "search" and "a button press" now describe this site's scroll feed.
- **`.gitignore` covers every dump shape**: `--dump-html X --pages 3`
  writes `X.page3`, which was not ignored, nor were `live/`-style scratch
  directories or a raw `.html` at the root.
- **The canary no longer goes green without testing anything.** From
  2026-10-11 to 2026-10-14 both halves met Cloudflare's challenge from the
  bare runner and the badge stayed green. Any run that did not parse real
  rows is now red, with the reason and the fix (`QUORA_PROXY`) in the log.
- Comments and docs no longer cite private section numbers.
- **A scroll cut off by its round budget is no longer reported as settled.**
  All three engines wrote `"settled": true` whatever ended the loop. The
  sidecar's scroll trace now carries `stopped_by` (`stable`, `target` or
  `max_rounds`) and `settled` is false for `max_rounds`, with a warning in
  the log. The run status is unchanged: `--pages` is the user's budget of
  scrolls, so reaching it is not a failure.
- **Output files are written atomically.** `write_json`, `write_csv` and
  `write_run_meta` wrote in place, so a kill or a full disk halfway through
  left a truncated file where a good one had been, and a sidecar beside it
  describing the old run. They now write to a temporary file in the same
  directory and rename over the destination. The mode follows the umask (a
  new file) or the existing file's mode, rather than the temporary file's
  0600.
- The save message says `rows`, not `products`: this project reads answers.

- **The Scraper API path sent `waitFor` in a form the live API rejects,
  and read the wrong field as the target's status.** Measured 2026-09-23
  against `scraper.2captcha.com/tasks/sync`: `waitFor` sent as a
  JSON-encoded string (what this client built for every `--wait-*` flag)
  is answered HTTP 422 "params.waitFor must be an object" and is still
  billed ($0.0005); the same request with an object gets HTTP 200. It is
  now an object. And the response's `status` is the API's own verdict
  ("success"), not the target site's HTTP code, which is `http_code` —
  so a target 403 or 503 never reached the page classifier. The target
  status is now read from `http_code` (falling back to `status` only if
  that is an integer). Pinned by an offline check that drives the real
  client with `requests.post` stubbed; verified by control (red with the
  old client).

- **Leftovers from the sibling repos this one was bootstrapped from are
  gone.** The worst was user-visible: when a
  run was blocked with pyppeteer's bundled Chromium, `puppeteer_scraper.py`
  logged that this was "the build this site was measured refusing" — true of
  a sibling site, false here, where that Chromium is served and the gate is
  the address's request rate. The log line is removed. Also:
  `requirements.txt` carried a sibling repo's name; the three engine
  requirements files explained a sibling site's HTTP 429 to bundled Chromium and told
  you to `playwright install chrome` (this repo's README says `chromium`);
  `.dockerignore` excluded a sibling's output prefix instead of this repo's default
  output `quora_answers.*`; the site-change issue template asked for a
  `__NEXT_DATA__` dump, a `c-lot-card` and a `bid_kind` label, and claimed
  headless runs are refused; the bug template expected "96 products" from
  `--out grills`; CONTRIBUTING said a datacentre run gets no response at
  all (eight of fourteen were served), asked for price coverage, forbade
  submitting a registration form, and scoped the repo to category, search
  and product pages; a `captcha_solver.py` comment counted "lot links".

- **README no longer says a challenge has nothing for a solver to answer.**
  Every measurement is unchanged. What changed is the reason this scraper
  does not solve one, which is now stated and is specific to this site: what
  gates Quora is the address's recent request RATE, so a token buys one page
  from an address about to be challenged again, while `--delay` and
  `--proxy-file` move the thing actually being measured. The repo does not
  implement the `turnstile.render` interception, and says so.

- **The canary asserted a `title` floor of 100%, which this repo's own README
  says is wrong.** A question page carries answers to other questions, and a
  few of those name their question nowhere on the page; a null there is the
  honest answer and inventing one from the slug would be a guess. Measured
  252 of 260 on a live run, so the floor is 0.90. Caught by the canary's
  second dispatch — a canary failing on its own repo's documented behaviour
  is it working.

### Added

- **`test_captcha_capability_claims_match_the_code`** — fails the build on a
  documented conclusion that a captcha cannot be solved, and equally on a
  README claiming a Turnstile solve with no task type and no interception
  behind it. Verified by control: restoring the old sentence turns it red.

- The canary now asserts the two title regressions a live run found, neither
  of which a coverage number can see: no title carries the "Related" badge
  Quora renders inside the title node, and no row wears the page's own
  question while answering a different one.

- `SECURITY.md` said this project has no releases or version tags; it has
  both. "Supported versions" now names the latest release and `main`.

## [0.1.1] — 2026-09-16

The canary's first dispatch, run without a proxy secret exactly as the family rule says
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
  them the whole thing. All three engines now classify the current page
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

### The paid paths, run rather than assumed

- **Captcha solving**: the key works (balance read live), and there is
  nothing on this site to spend it on — see above.
- **Fingerprint API**: works, and running it found a real defect. The
  Selenium engine read the user agent from a key the API returns in NEITHER
  of its two formats, so `--fingerprint` there set no UA at all and presented
  a Windows fingerprint's screen, locale and timezone over a local Chromium's
  UA. That is the identity MISMATCH the flag exists to avoid, and it is the
  same defect the family rule records as having been live in four sibling repos. Fixed,
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

[Unreleased]: https://github.com/2scraper/quora-scraper/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/2scraper/quora-scraper/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/2scraper/quora-scraper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/2scraper/quora-scraper/releases/tag/v0.1.0
