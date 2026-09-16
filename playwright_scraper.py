#!/usr/bin/env python3
"""quora-scraper — Playwright edition (primary engine)

Scrapes Quora answers out of one of three feeds:

    --mode topic     (default)  /topic/{Slug} — a topic's answer feed
    --mode question             /{Question-Slug} — one question's answers
    --mode profile              /profile/{Slug} — one author's answers

All three yield the same row, because all three are feeds of the same thing;
see `output_writer.Answer`. The mode is inferred from the URL, so passing it
is only ever a way to be explicit or to be told you are wrong.

There is deliberately no `--language` flag. The language IS the hostname —
www.quora.com, es.quora.com, jp.quora.com and twenty-one more — so a flag
could only disagree with the URL it was given.

WHAT IS DIFFERENT ABOUT THIS SITE
---------------------------------
* **Nothing is server-rendered.** 0 occurrences of Quora's own `q-box` class
  in the raw response body of six captures, against 385-1165 in the hydrated
  document. Every page starts as a shell, so `shell` is the normal first
  state here rather than a fault, and an engine that refetched on "no cards
  in the response" would refetch every page it ever loaded.

* **Its structured data is its own GraphQL, inlined.** 0
  `application/ld+json` blocks anywhere. What the server does send is
  `window.ansFrontendGlobals.data.inlineQueryResults.…push("{…}")`, and that
  payload carries what the DOM does not: numeric ids, the FULL answer text,
  `numUpvotes`, `numViews`, an epoch-microsecond `creationTime` and Quora's
  own `isMachineAnswer` flag. It covers a question page and a profile and
  does NOT cover a topic feed, so `data_source` on every row says which of
  the two views built it.

* **Pagination is a SCROLL, and there is no address and no button.** No
  `link[rel=next]`, no pagination control, and `?page=2` does not fail — it
  is ignored and the feed returns its first items again, which is how a run
  built on it would report COMPLETE holding one batch. So a "page" here is
  one settled scroll batch, the listing ends when a batch adds no new sku,
  and `--concurrency` above 1 is refused with that reason.

* **The block is Cloudflare's managed challenge, and the address's recent
  RATE is what decides.** HTTP 403, `cf-mitigated: challenge`, "Just a
  moment...", `cType: 'managed'`, and no sitekey — so there is nothing to pay
  a solver for. Measured 2026-09-15 from one datacentre exit: the first
  fourteen fetches were 8 served and 6 challenged, each of those clearing on
  the next attempt about a minute later; everything after that — about thirty
  attempts over three hours, a 45-minute rest included, and `es.quora.com`
  tried too — was challenged, with nothing changed but how much that address
  had fetched. So the retry budget is small on purpose, and `--delay` is the
  lever.

* **A bundled Chromium is enough.** Unlike a sibling site that refuses
  anything but real Chrome, Playwright's own Chromium was served HTTP 200
  and the full feed on every un-challenged fetch. So no browser channel is
  forced, and `--browser-channel chrome` is available rather than assumed.

Examples
--------
    python3 playwright_scraper.py \\
        --url "https://www.quora.com/topic/Machine-Learning" --pages 3

    python3 playwright_scraper.py \\
        --url "https://www.quora.com/What-is-machine-learning-4" --pages 2

    python3 playwright_scraper.py \\
        --url "https://es.quora.com/profile/Gonzalo-Perez-9"
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from product_parser import (parse_answers, SELECTORS, PAGE_CAP,
                            detect_bot_challenge, listing_kind, normalize_url,
                            served_by_quora, site_host, is_supported_host,
                            source_of, unsupported_reason)
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# No browser channel is forced here, and that is a measurement rather than an
# omission. A sibling site reads the CLIENT before the address and refuses a
# bundled Chromium outright; Quora does not. Playwright's own Chromium was
# served HTTP 200 and the full feed on every fetch that was not challenged —
# eight of the first fourteen, from one datacentre exit on 2026-09-15 — and
# the refusals were Cloudflare's managed challenge, which tracks the ADDRESS's
# recent request rate rather than the browser build. Named here rather than at
# the call site so the smoke suite can assert the three engines agree on it;
# `--browser-channel chrome` is still available for a reader who wants it.
DEFAULT_BROWSER_CHANNEL = None

# How long to wait for a remote browser to accept the CDP connection.
#
# 150s, not the 30s this family shipped. What is MEASURED is narrow and
# arithmetic: against a live Scraping Browser endpoint the WebSocket upgrade
# hung for **121 seconds** before the SERVER hung up, so a 30s client timeout
# gives up while the server is still working. Sitting above the server's own
# give-up point means the client is never the one that walks away first.
#
# What is NOT established, and was claimed here for one commit before the
# evidence contradicted it: that giving up early is what leaves a profile
# stuck at `profile_locked`. Two profiles were observed locked and not
# clearing (one for over forty minutes, one after four minutes of complete
# silence), and the second locked INSTANTLY on its first WebSocket attempt —
# with no timed-out connect anywhere in its history. So the lock has some
# other cause, and this timeout is not a fix for it. Raising it is still
# right; expecting it to unwedge anything is not.
CDP_CONNECT_TIMEOUT_MS = 150_000


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chrome
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes: dedupe that mutates a running set inside the loop
    makes the OUTPUT depend on the order pages happened to arrive in. Pages
    are strictly sequential on this site, which is exactly why keeping the
    merge order-independent costs nothing and keeps the family's contract.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # How many answers the QUESTION has in total, where the site states one.
    # NOT a per-page counter and never used as one: a question page renders
    # twelve answers and reports 253, so reading it as a gap would claim 241
    # missing cards on a page that rendered everything it was going to. It
    # goes in the sidecar beside what the run actually read, which is what
    # makes the difference between the two visible rather than assumed.
    answers_available: Optional[int] = None
    # None, always, on this site: Quora publishes no per-page counter, and an
    # unknown gap must not read the same as a gap of zero (§8).
    gap: Optional[int] = None
    # What the scroll did: how many cards it reached and whether it SETTLED.
    # A batch whose feed was still growing when the round budget ran out is
    # a floor rather than the whole feed, and a run that reported it as
    # complete would read as a shrinking catalogue.
    scroll: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# The lowest share of rows that must carry an author and an answer body
# before the read is suspect. Measured across four captures in two
# languages: 30/30, 30/30, 18/18 and 13/13 — every card that rendered
# carried both, on every one. So the floor sits high; 90% leaves room for a
# deleted author without hiding a broken read.
#
# There is deliberately NO credential floor beside it. 28/30, 27/30, 14/18
# and 10/13 carried one, and the gap is authors who have not written a
# credential rather than a parse that missed it — a threshold there would
# fire on a correct read of a topic full of new writers.
FIELD_FLOOR = 90



# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a page has turned over — lives in page_flow.py so all three
# engines make it identically. What lives here is only HOW to ask this
# particular driver.
#
# The primitives are NAMED OPERATIONS rather than JavaScript (§1). Selenium's
# execute_script takes a function BODY with an explicit `return` while
# Playwright and pyppeteer take `() => expr`, so a shared module handing JS
# across this boundary would quietly acquire one driver's dialect.
def _count(page, selector: str) -> int:
    """How many elements match, or 0 if the page moved under us.

    GUARDED, like its twins in the other two engines, and the crash that
    taught us why came from the canary's first dispatch: a scroll batch was
    polling the card count when the page navigated — Cloudflare's challenge
    can arrive at any moment on this site — and Playwright raised
    `Execution context was destroyed, most likely because of a navigation`.
    That left the run with exit 1, a CRASH, where the correct answer was
    "blocked".

    0 is the safe reading rather than a lie: every caller treats it as "no
    cards seen this poll", which makes a readiness wait keep waiting and a
    scroll batch report no growth — both of which are what actually happened.
    The alternative, letting it propagate, turns a routine mid-poll
    navigation into a traceback.
    """
    try:
        return len(page.query_selector_all(selector))
    except (PWError, PWTimeout) as e:
        logger.debug("count(%s) failed: %s", selector, e)
        return 0


def _scroll_to_bottom(page) -> None:
    """Scroll the WINDOW to the end of the document.

    The opposite of a sibling repo, where the body never scrolled and the
    results lived in an inner container. Measured on Quora: this took a topic
    feed from 20 cards to 30 in two rounds and grew the document with it.

    To `document.body.scrollHeight` rather than by a fixed wheel distance: a
    fixed wheel stopped three rounds short of the bottom on a sibling site's
    7,600px grid, so the lazy-load trigger was never reached and a run took
    30 of 50 cards while looking settled (§8).

    Guarded for the same reason `_count` is: the page can navigate mid-loop
    on this site, and a scroll that raises turns a routine challenge into a
    traceback. A failed scroll needs no report of its own — the next poll
    sees the card count unchanged and the loop draws the right conclusion.
    """
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    except (PWError, PWTimeout) as e:
        logger.debug("scroll failed: %s", e)


def _page_height(page) -> Optional[int]:
    """The document's scroll height, or None if the page is mid-navigation."""
    try:
        return page.evaluate("() => document.body.scrollHeight")
    except (PWError, PWTimeout):
        return None


# Each scroll batch after the first arrives over POST /graphql, and that
# endpoint can be refused while the HTML keeps answering 200. Counting the
# refusals is what separates a feed that ran out (COMPLETE) from one whose
# next batch was refused (PARTIAL) — without it the two are the same
# observation and a throttled run reports "complete" holding its first batch
# (§7).
#
# Any status at or above 400, rather than one specific code, and the SAME
# threshold in all three engines. A threshold that differed between them
# would mean one engine reporting `complete` where its twins report `partial`
# on the identical run, which is precisely the drift the shared modules exist
# to prevent (§6) — and this file carried a sibling repo's single-code check
# for several commits before the smoke suite was taught to compare the three.
_GRAPHQL_PATH = "/graphql/"


def _watch_graphql(session) -> None:
    """Start counting refused GraphQL responses on this session's page."""
    session._graphql_refused = 0

    def _on_response(response):
        try:
            if _GRAPHQL_PATH in response.url and response.status >= 400:
                session._graphql_refused += 1
        except Exception:  # noqa: BLE001 — a listener must never break a run
            pass

    session.page.on("response", _on_response)


def _graphql_refused_count(session) -> int:
    return getattr(session, "_graphql_refused", 0)


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args, html: str = "") -> int:
    """The readiness threshold, lowered to what THIS page actually holds.

    Passing the counter's own range is what keeps a short last page from
    spending the whole timeout and then reporting itself unpainted.
    """
    return page_flow.min_matches(args.mode, page_flow.answers_expected(html))


def _classify(page, html: str, status=None) -> str:
    return page_flow.classify(html, status, page.url)


def _same_url(a: str, b: str) -> bool:
    from product_parser import strip_tracking
    return strip_tracking(a or "") == strip_tracking(b or "")


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different one — retrying it unchanged just
    spends the budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch a browser on `pool`'s current exit; return (browser, context, page).

    Uses Playwright's own Chromium by default, and on this site that is a
    measurement rather than a shrug: it was served HTTP 200 and the full feed
    on every fetch that was not challenged, and the challenges it did meet
    were Cloudflare's managed one, which tracks the ADDRESS's recent request
    rate rather than the browser build. `--browser-channel chrome` is offered
    for a reader who wants it and is not needed.

    Factored out so a proxy rotation can tear the whole browser down and call
    it again. Swapping the proxy under a live session would be cheaper and
    wrong: cookies a bot manager issued against one exit, replayed from
    another, are a stronger signal than either address alone.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    channel = args.browser_channel
    if channel:
        try:
            browser = pw.chromium.launch(channel=channel, **launch_kwargs)
        except (PWError, PWTimeout) as e:
            # A fallback, not a downgrade. Unlike a sibling site, this one
            # was measured serving the bundled Chromium the full feed, so
            # losing the requested channel costs nothing that is known.
            logger.info(
                "Could not launch the %r channel (%s) — using Playwright's "
                "own Chromium instead, which this site was measured serving "
                "normally. Install the channel with `playwright install %s` "
                "if you want it.", channel, str(e)[:160], channel)
            browser = pw.chromium.launch(**launch_kwargs)
    else:
        browser = pw.chromium.launch(**launch_kwargs)

    ctx_kwargs = {"user_agent": _chrome_ua(browser.version),
                  "locale": args.locale,
                  "viewport": {"width": 1440, "height": 900}}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)",
                    fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        _watch_graphql(self)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    try:
        browser = pw.chromium.connect_over_cdp(
            args.cdp_endpoint, timeout=args.cdp_connect_timeout * 1000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # that endpoint is a URL with a password in it — repeated five times,
        # in the message plus a four-line call log. Unmasked it lands in the
        # terminal, in CI output and in any log the run is piped to, which is
        # the one thing this project promises does not happen. The host and
        # port are KEPT: which endpoint failed is the useful half and is not
        # the secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so `profile_locked` means something holds this `pid`.\n"
            f"Worth knowing before you go looking for it on your side: two "
            f"profiles were observed here entering that state and NOT leaving "
            f"it — one for over forty minutes, one still locked after four "
            f"minutes of no requests at all, having locked on its very first "
            f"connection attempt. Waiting did not clear either. If that is "
            f"what you are seeing, it is not another run of this tool holding "
            f"it, and nothing on this side will free it: use a different pid, "
            f"or reset the profile from the 2Captcha dashboard."
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser. Tried first when --cdp-endpoint is set;
    # this script's own detect+solve logic still runs as a fallback.
    #
    # Worth knowing what it can and cannot do here: the refusal this site
    # actually serves is Cloudflare's MANAGED challenge, which publishes no
    # sitekey at all — 0 `data-sitekey` attributes and 0 Turnstile iframes on
    # the one measured. An auto-solver has nothing to answer either. What it
    # WOULD cover is a rendered reCAPTCHA or Turnstile widget, which Quora
    # has wired into every page (an empty `cf-turnstile-response` input in a
    # 0x0 div, and Turnstile's own loader in the head) and has never been
    # observed rendering to an anonymous reader.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve",
                         {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info(
            "[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning(
            "[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001
        logger.info("Captcha.setAutoSolve not available on this "
                    "--cdp-endpoint (%s) — relying on this script's own "
                    "detect+solve logic instead.", e)
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY rather than once is the point: a Playwright connection
# error repeats the endpoint five times, so a masker that handled only the
# first occurrence would print the password four times and look like it was
# working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright RAISES rather than returning empty while a navigation is in
    flight ("Unable to retrieve content because the page is navigating"), and
    this site's challenge handler resolves by navigating — so the one moment
    this is called is the one moment it can fail. Returns None if the page
    will not hold still, so a caller can skip a check instead of failing the
    run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating — retrying content() in %dms "
                        "(%d/%d).", pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page. The static-HTML and runtime
    reCAPTCHA detectors are run and RECONCILED against each other rather than
    short-circuited, because they can disagree about the variant and the
    parameters for one are rejected for the other.

    NOTE what this cannot help with, because on this site it is the normal
    case. Quora's refusal is Cloudflare's MANAGED challenge (`cType:
    'managed'`), and a managed challenge has no widget and no sitekey: 0
    `data-sitekey` attributes and 0 Turnstile iframes on the one measured.
    There is nothing to hand a solver, so `page_flow.STATE_POLICY` marks
    `challenge` as retry-but-do-not-solve and nothing is ever charged for it.

    The detector is still run on every navigation, and that asymmetry is
    deliberate (§8). Quora HAS a captcha configured — Turnstile's loader is
    in the head of every page it serves and an empty
    `cf-turnstile-response` input sits in a 0x0 div — it simply does not
    render one to an anonymous reader. Which challenge a visitor meets
    depends on the exit country and on what the address has been doing, and a
    narrow detector is how a rendered challenge gets reported as an empty
    topic months later.
    """
    html = _content_when_settled(page)
    if html is None:
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # answers are already rendered guards nothing, and counting the anchors is
    # instant — which is why this check sits here rather than after the
    # readiness wait. The other way round would cost 25 wasted seconds on a
    # page the challenge genuinely gates, where solving FIRST is what makes
    # the content appear.
    already_rendered = _count(page, _ready_selector(args))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > page_flow.MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d cards are already on the page "
                    "— not solving it. Pass --solve-captcha always to solve "
                    "it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting "
                   "to solve.", challenge.kind, challenge.source,
                   challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int = 1) -> List:
    """Rows for this mode, always as a list even when the mode yields one.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it a row from
    page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output.
    """
    return parse_answers(html, url, page=page_num, mode=args.mode)


def _scroll_the_feed(session, args, html: str, page_num: int) -> dict:
    """Scroll until the feed stops growing, and return the sidecar trace.

    On this site this is where every row after the first batch comes from:
    a first paint of 12-20 cards becomes 30 after two rounds of scrolling the
    window, and the feed has no other way to be extended.

    `target` is the QUESTION's own answer count where the site states one. It
    ends the loop early if it is reached and ends nothing early if it is not,
    because the gap is reported rather than chased (§8) — and on this site
    the gap is usually enormous and legitimate: a question with 253 answers
    renders twelve.
    """
    target = page_flow.answers_expected(html) if args.mode == "question" else None
    before = _count(session.page, page_flow.READY_SELECTOR)
    reached = page_flow.scroll_until_settled(
        lambda sel: _count(session.page, sel),
        lambda: _scroll_to_bottom(session.page),
        lambda: _page_height(session.page),
        session.page.wait_for_timeout,
        selector=page_flow.READY_SELECTOR,
        target=target)
    logger.info("Scrolled batch %d: %d card(s) at first paint, %d after "
                "scrolling%s.", page_num, before, reached,
                f" (the question has {target} answers in total)" if target else "")
    return {"first_paint": before, "reached": reached, "target": target,
            "settled": True}


def _fetch_one_page(session, args, pool, page_num: int, url: Optional[str]) -> PageOutcome:
    """Fetch (or turn to) one page and parse it.

    `url` is the address to navigate to for page 1, and None for every page
    after it: pages 2..N on this site are not addresses at all, they are the
    result of pressing the site's own button. Passing None is what makes that
    explicit rather than leaving a stale URL to look like it was fetched.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a refusal, a challenge page, a dead exit are all recorded on
    the outcome instead.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url or session.page.url)

    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not merely documented — a policy
    # constant nothing reads is the same defect as dead code (§17). It is
    # True on this site because a RESTED address recovers: the same URL that
    # was challenged was served in full on the next attempt about a minute
    # later. The budget is deliberately small, because a BUSY address does
    # not: every attempt after about the thirtieth was challenged, and a
    # 45-minute rest did not clear it.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        load_failed, exit_failed = False, None

        if url is not None:
            logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
            for attempt in range(1, args.retries + 1):
                try:
                    session.page.goto(url, wait_until="domcontentloaded",
                                      timeout=60000)
                    load_failed = False
                    break
                except (PWTimeout, PWError) as e:
                    # A dead or misconfigured proxy raises PWError
                    # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                    # catching only the latter lets it escape as a traceback,
                    # which is the likeliest failure the first time anyone
                    # points --proxy-file at a real list.
                    reason = _proxy_failure(e)
                    if reason:
                        exit_failed, load_failed = reason, True
                        break  # a different exit is the only thing that helps
                    load_failed = True
                    if attempt < args.retries:
                        pause = args.retry_delay * (2 ** (attempt - 1))
                        logger.warning("Timeout loading %s (attempt %d/%d) — "
                                       "retrying in %.1fs.", url, attempt,
                                       args.retries, pause)
                        time.sleep(pause)
        else:
            logger.info("Extending the feed to batch %d/%d by scrolling "
                        "(this listing has no batch-%d address).",
                        page_num, args.pages, page_num)
            refused_before = _graphql_refused_count(session)
            turn = page_flow.advance_feed(
                lambda: _scroll_to_bottom(session.page),
                lambda sel: _count(session.page, sel),
                lambda: _page_height(session.page),
                session.page.wait_for_timeout)
            if turn == page_flow.NO_GROWTH:
                refused = _graphql_refused_count(session) - refused_before
                if refused:
                    # The feed stopped growing AND the endpoint behind it was
                    # refusing. Those are two different endings and must not
                    # collapse into one: reporting this as the end of the
                    # listing would make a throttled run say "complete" while
                    # holding its first batch (§7).
                    outcome.state = "no_turnover"
                    outcome.load_failed = True
                    outcome.final_url = session.page.url
                    logger.error(
                        "The feed did not grow within %.0fs and %d GraphQL "
                        "response(s) were refused while waiting. This is NOT "
                        "the end of the listing — the run is reported as "
                        "PARTIAL (exit 6) rather than complete. Each batch "
                        "after the first is fetched over POST /graphql, "
                        "which is limited separately from the HTML: the page "
                        "itself still answers 200 while the batch is "
                        "refused. Raise --delay (it is %.1fs now), or spread "
                        "the load with --proxy-file.",
                        page_flow.NEXT_BATCH_TIMEOUT_MS / 1000, refused,
                        args.delay)
                    return outcome
                # Nothing refused and nothing new. Before calling that the
                # end of the listing, ASK WHAT PAGE WE ARE ON: Cloudflare's
                # challenge can arrive mid-scroll — it is what crashed this
                # repo's first canary dispatch — and a feed that stopped
                # growing because the page was replaced has not run out. The
                # engines used to report that as `exhausted`, which is a
                # COMPLETE stop reason, so a run that was blocked halfway
                # would have claimed the feed ended (§7).
                current = _content_when_settled(session.page) or ""
                state_now = _classify(session.page, current)
                if page_flow.counts_as_blocked(state_now):
                    outcome.state = "blocked_mid_scroll"
                    outcome.blocked_by = (detect_bot_challenge(current)
                                          or "bot-challenge")
                    outcome.final_url = session.page.url
                    logger.error(
                        "The feed stopped growing because the page was "
                        "replaced: it is now %s. This is NOT the end of the "
                        "listing — the batches already gathered are kept and "
                        "the run is reported as partial rather than "
                        "complete.", state_now)
                    return outcome
                # Ours, still served, and no more answers came. On an
                # infinite scroll that is the only ending there is.
                outcome.state = "exhausted"
                outcome.final_url = session.page.url
                return outcome

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session.page, args):
            session.page.wait_for_timeout(1000)

        html = _content_when_settled(session.page) or ""
        state = _classify(session.page, html)

        # "Not painted yet" is not a fault, and telling it apart from one is
        # the distinction §8 is about. A search page's first response is a
        # shell — the grid arrives over client-side GraphQL — so classified
        # naively it reads as something to retry, and retrying a shell buys
        # another shell. Wait for the anchor and re-classify BEFORE the retry
        # decision.
        if page_flow.is_unpainted(state, html):
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is a shell the site served but has not "
                        "filled in (%d bytes, no cards) — waiting up to "
                        "%.0fs for the grid rather than spending a retry.",
                        page_num, len(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: _count(session.page, sel),
                session.page.wait_for_timeout,
                _ready_selector(args), _min_matches(args, html), wait_timeout)
            if found < _min_matches(args, html):
                logger.info("The grid still had not painted after %.0fs "
                            "(%d match(es)).", wait_timeout / 1000, found)
            html = _content_when_settled(session.page) or html
            state = _classify(session.page, html)

        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _content_when_settled(session.page) or html
                state = _classify(session.page, html)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works. This
                # line is what says whether the money bought anything.
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty search
            # is a CORRECT one, so retrying it would spend the budget
            # re-confirming the same right answer and rotating the exit would
            # blame an address for the URL it was given.
            break

        if block_attempt < block_retries:
            pause = args.retry_delay * (block_attempt + 1)
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit in %.1fs (%d/%d).",
                               page_num, state, mask(pool.current), pause,
                               block_attempt + 1, block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
                time.sleep(pause)
            else:
                # No pool, so nowhere else to go — but on this site a plain
                # wait is often what clears it, because the challenge is a
                # rate response rather than a verdict on the address. The
                # browser
                # is NOT relaunched over --cdp-endpoint: a profile allows one
                # live connection, so reconnecting risks `profile_locked` and
                # would lose the cookies the retry is meant to build on.
                logger.warning("Page %d came back as %s — waiting %.1fs and "
                               "re-fetching through the same access path "
                               "(%d/%d). The refusal here is a RATE response "
                               "and it does clear.", page_num, state, pause,
                               block_attempt + 1, block_retries)
                time.sleep(pause)
            # A scroll cannot be replayed: batch N exists only inside a
            # browser that has already scrolled through batches 1..N-1, and
            # re-navigating to the feed's URL would silently restart it at
            # batch 1. So a blocked batch ends the run instead.
            if url is None:
                logger.warning("Batch %d was reached by scrolling, so there "
                               "is no address to re-fetch — stopping here "
                               "rather than silently restarting the feed at "
                               "its first batch.", page_num)
                break

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if page_flow.counts_as_blocked(state):
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png")
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        served = served_by_quora(html or "")
        vendor = detect_bot_challenge(html or "", url=session.page.url)
        logger.error(
            "The site did not serve this request — %d bytes, %s the site's "
            "own asset hosts, saved to %s. This is exit 3, distinct from a "
            "genuinely empty result (exit 4).%s",
            len(html or ""),
            "which references" if served else "with no reference to",
            debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        logger.error("%s", page_flow.block_advice(
            html, headless=bool(getattr(args, "headless", False)),
            has_pool=has_pool))
        outcome.blocked_by = vendor or ("no-response" if not html else "bot-or-not")
        outcome.final_url = session.page.url
        return outcome

    if page_flow.should_parse(state):
        selector, threshold = _ready_selector(args), _min_matches(args, html)
        content_timeout = page_flow.content_timeout_ms(args.mode)
        # A POLL, not wait_for_function. wait_for_function hands the browser a
        # STRING to evaluate, and a site whose CSP lacks `unsafe-eval`
        # refuses that outright — it took a sibling repo's run down with
        # EvalError and exit 1. A count poll is a CDP call under any CSP.
        found = page_flow.wait_for_count(
            lambda sel: _count(session.page, sel),
            session.page.wait_for_timeout, selector, threshold, content_timeout)
        session.page.wait_for_timeout(500)
        if found < threshold:
            logger.info("No answer cards appeared within %.0fs. If this feed "
                        "genuinely holds nothing, that is the expected "
                        "answer and the run will report 0 rows (exit 4).",
                        content_timeout / 1000)

        outcome.scroll = _scroll_the_feed(session, args, html, page_num)

        html = _content_when_settled(session.page) or html

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is the exact bytes.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html or ""))

    products = _parse_for_mode(html or "", session.page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    # What the site says the QUESTION holds, recorded beside what this run
    # read. Not a gap and never used as one — see PageOutcome.answers_available.
    outcome.answers_available = page_flow.answers_expected(html or "")
    outcome.gap = page_flow.page_gap(html or "", len(products))
    if outcome.answers_available:
        logger.info("The question states it has %d answer(s) in total; this "
                    "batch parsed %d. Quora renders a fraction of a large "
                    "question and extends it on scroll, so a large "
                    "difference here is the site's behaviour rather than a "
                    "missing read.", outcome.answers_available, len(products))

    if products:
        with_author = sum(1 for p in products if p.author)
        with_text = sum(1 for p in products if p.text)
        worst = min(with_author, with_text)
        share = 100.0 * worst / len(products)
        # Reported every time, not only when it looks wrong, so a consumer
        # gets the number rather than a threshold someone guessed.
        logger.info("Author/body coverage on batch %d: %d and %d of %d "
                    "(%.0f%% at worst); the measured floor is %d%%.",
                    page_num, with_author, with_text, len(products), share,
                    FIELD_FLOOR)
        if share < FIELD_FLOOR:
            logger.warning(
                "Only %.0f%% of batch %d carries both an author and a body, "
                "against a measured floor of %d%%. Every card that rendered "
                "carried both on every capture, so this is the read breaking "
                "rather than the feed being unusual — re-run with "
                "--dump-html. The likeliest cause is the card scope: the "
                "FIRST /profile/ link in a card is the avatar and has no "
                "text, so a scope that lost the named link gives an empty "
                "author on every row while looking like it worked.",
                share, page_num, FIELD_FLOOR)

        # The inline payload's reach, which is the one number that says
        # whether upvotes, views and full answer text are in this run at all.
        # Low is CORRECT in topic mode — a topic page's payload carries no
        # answers whatsoever — so the warning is scoped to the modes where
        # the payload was measured to be there.
        enriched = sum(1 for p in products if p.data_source != "dom")
        payload_share = 100.0 * enriched / len(products)
        logger.info("Inline-payload coverage on batch %d: %d/%d (%.0f%%). "
                    "Rows without it have a null upvote count, view count "
                    "and creation time, and a body truncated to what the "
                    "card rendered.", page_num, enriched, len(products),
                    payload_share)

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        try:
            session.page.screenshot(path=debug_png)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw "
                       "to %s and %s. Open the .png to see it.",
                       debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    dedupe_key = "sku"
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    # Refused, not silently honoured. A listing here has no per-page address,
    # so there is nothing to hand a second worker — and saying so is the
    # point: running one worker quietly would look like the flag did
    # something (§18).
    if args.concurrency > 1:
        refusal = page_flow.concurrency_refusal(args.url)
        logger.warning("--concurrency %d is refused: %s.",
                       args.concurrency, refusal)

    session = None
    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None
            else:
                seen_keys.update(p.sku for p in first.products if p.sku is not None)
                for page_num in range(2, args.pages + 1):
                    if page_flow.page_cap_reached(page_num):
                        logger.warning(
                            "Stopping at the %d-batch cap. Every batch past "
                            "the first costs a sequential scroll and a fresh "
                            "GraphQL round trip on this site, and Quora "
                            "starts repeating a topic's items well before "
                            "this point — pick a narrower topic, or a "
                            "specific question, instead.", PAGE_CAP)
                        stop_reason = "page_cap"
                        break
                    # A new exit per page is what actually spreads a run's
                    # volume — but it cannot be done here, and saying so is
                    # better than doing it wrong: the next page exists only
                    # inside THIS browser's session, so relaunching on
                    # another exit would lose the listing and silently
                    # restart it at page 1.
                    if pool and pool.rotates_per_page() and page_num == 2:
                        logger.warning(
                            "--proxy-rotate per-page cannot be honoured on a "
                            "Quora feed: batch %d exists only inside this "
                            "browser's session (it is reached by scrolling, "
                            "not by an address), so relaunching on another "
                            "exit would restart the feed at its first batch. "
                            "Holding the current exit for the whole run.",
                            page_num)

                    time.sleep(args.delay)
                    outcome = _fetch_one_page(session, args, pool, page_num, None)
                    outcomes.append(outcome)

                    if outcome.state == "exhausted":
                        logger.info("The feed stopped growing after batch %d, "
                                    "with nothing refused behind it — "
                                    "treating that as the end of the "
                                    "listing.", page_num - 1)
                        stop_reason = "pagination_exhausted"
                        outcomes.pop()  # nothing was fetched; do not count it
                        break
                    if outcome.state == "no_turnover":
                        # Deliberately NOT "pagination_exhausted", which is a
                        # COMPLETE stop reason: a throttled turn is a partial
                        # run, not a finished one.
                        stop_reason = "next_batch_refused"
                        outcomes.pop()
                        break
                    if not outcome.ok:
                        stop_reason = ("page_load_timeout" if outcome.load_failed
                                       else f"blocked_{outcome.blocked_by}")
                        blocked = outcome.blocked_by is not None
                        break

                    # Whether this page contributed anything not already
                    # seen. The authoritative dedupe happens once, after the
                    # loop, in page order; this running check exists because
                    # the termination condition is inherently sequential.
                    fresh_count = sum(1 for p in outcome.products
                                      if p.sku is None or p.sku not in seen_keys)
                    seen_keys.update(p.sku for p in outcome.products
                                     if p.sku is not None)
                    if not fresh_count:
                        # A property of the DATA, not of a CSS selector that
                        # may have been renamed (§7). On this site it is the
                        # ONLY termination signal there is: an infinite
                        # scroll has no last page to recognise.
                        logger.info("Batch %d added no rows not already seen "
                                    "— treating that as the end of the "
                                    "listing.", page_num)
                        stop_reason = "no_new_products"
                        break
        finally:
            if session is not None:
                session.close()

    # Merge once, in PAGE order — not in the order pages happened to finish.
    all_rows = []
    merged_seen = set()
    fresh_by_batch = {}
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        fresh_by_batch[oc.page_num] = len(fresh)
        if len(fresh) < len(oc.products):
            # EXPECTED here, unlike in every sibling repo, and the number is
            # logged at info rather than warned about for that reason: a
            # scroll batch re-parses the WHOLE feed, so batch 2 arrives
            # holding batch 1's rows again by construction. The interesting
            # figure is how many were new, which is the next line.
            logger.info("Batch %d: %d row(s) new, %d already seen.",
                        oc.page_num, len(fresh),
                        len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # There is deliberately NO thin-page check here, and its absence is a
    # measurement rather than an oversight. A sibling repo warns when a page
    # comes back much smaller than the fullest one, because its pages are a
    # steady 50. Quora's are not comparable: the first batch is the initial
    # paint (12-30 rows) and every batch after it adds about ten, so every
    # healthy run would trip such a check on every batch after the first.
    # What IS reported is the new-row count per batch, above.

    answers_available = next((o.answers_available for o in outcomes
                              if o.answers_available is not None), None)

    if all_rows and answers_available:
        logger.info("The question states it has %d answer(s); this run took "
                    "%d (%.1f%%).", answers_available, len(all_rows),
                    100.0 * len(all_rows) / answers_available)
        short = page_flow.short_feed_warning(len(all_rows), answers_available)
        if short:
            logger.warning("%s", short)

    if all_rows:
        enriched = sum(1 for r in all_rows if r.data_source != "dom")
        logger.info("Inline-payload coverage over the merged run: %d/%d "
                    "(%.0f%%).", enriched, len(all_rows),
                    100.0 * enriched / len(all_rows))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    extra = {
        "scroll": {o.page_num: o.scroll for o in outcomes if o.scroll},
        "rows_new_per_batch": fresh_by_batch,
        # What the QUESTION holds, where the site states it. Beside
        # `rows` in the sidecar rather than subtracted from it: the
        # difference is Quora rendering a fraction of a large question, not
        # a gap this run failed to close (§8 — an unknown gap is not a gap).
        "answers_available": answers_available,
        "inline_payload_rows": sum(1 for r in all_rows
                                   if r.data_source != "dom"),
        # Recorded because a reader comparing two runs needs to know the
        # pagination model before comparing anything: a "page" here is a
        # scroll batch, not an address.
        "pagination": "infinite-scroll",
    }

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=source_of(final_url),
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Quora answer scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="A Quora URL: a topic (/topic/{Slug}), a question "
                        "(/{Question-Slug}) or a profile "
                        "(/profile/{Slug}). An answer permalink "
                        "(/{Q}/answer/{Author}) is accepted and read as its "
                        "question. The LANGUAGE is the hostname — "
                        "www.quora.com plus twenty-three language sites, all "
                        "serving identical markup. Required, unless QUORA_URL "
                        "is set in the environment or in .env.")
    p.add_argument("--mode", choices=["topic", "question", "profile"],
                   default=None,
                   help="Which feed the URL is. Inferred from the URL by "
                        "default, and passing one that disagrees with the "
                        "URL is an error rather than an override: the mode is "
                        "a property of the path. All three yield the same "
                        "row. What differs is how much of it Quora sends — a "
                        "question or profile page carries its answers in the "
                        "page, a topic page does not, so upvotes, views, "
                        "creation time and the full answer text are null on "
                        "topic rows. The data_source column says which.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Filled from the URL "
                        "by default — the topic, profile or question slug — "
                        "so it is rarely empty.")
    p.add_argument("--pages", type=int, default=1,
                   help=f"Number of scroll BATCHES to walk (default 1, cap "
                        f"{PAGE_CAP}). Quora has no per-page address in any "
                        f"mode: `?page=2` does not fail, it is ignored and "
                        f"the feed returns its first items again. So a batch "
                        f"is one settled scroll, batches are strictly "
                        f"sequential, and --concurrency cannot help.")
    p.add_argument("--delay", type=float, default=3.0,
                   help="Delay between batches, seconds (default 3.0). The "
                        "rate matters more than the address on this site: "
                        "fourteen fetches in twenty minutes from one exit "
                        "took the Cloudflare challenge rate from one-in-four "
                        "to three-in-three. This is the cheapest lever.")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for family compatibility and REFUSED above "
                        "1, with the reason: a Quora feed has no per-page "
                        "address, so batch 5 exists only inside the browser "
                        "that scrolled through batches 1-4 and there is "
                        "nothing to hand a second worker. Run several topics "
                        "or profiles in parallel instead, one process each.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried — see "
                        "page_flow.STATE_POLICY — because an empty search is "
                        "a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="quora_answers", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It does NOT decide "
                        "the site language: the HOSTNAME does. This only "
                        "affects what the browser claims about itself.")
    p.add_argument("--browser-channel", default=DEFAULT_BROWSER_CHANNEL,
                   metavar="CHANNEL",
                   help="Which installed browser to drive. Unset by default, "
                        "which means Playwright's own Chromium — and on this "
                        "site that is enough: it was served HTTP 200 and the "
                        "full feed on every fetch that was not challenged, "
                        "and the refusals were Cloudflare's managed "
                        "challenge, which real Chrome met at the same rate. "
                        "Pass `chrome` to drive an installed Chrome anyway "
                        "(`playwright install chrome`).")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and "
                        "blank lines skipped) to rotate across. Wins over "
                        "--proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES),
                   default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page is accepted but cannot be honoured mid-feed "
                        "here — the next batch lives inside the current "
                        "browser session, so rotating would restart the feed "
                        "at its first batch. The run says so when it "
                        "happens.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do "
                        "not all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused, retry it from this "
                        "many OTHER exits before giving up (default 2). Needs "
                        "a pool of more than one; ignored otherwise. Worth "
                        "knowing on this site: the refusal is Cloudflare's "
                        "managed challenge and it clears on its own — the "
                        "same URL was served in full about a minute later "
                        "from the same address — so --delay and "
                        "--retry-delay are usually the better levers.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off "
                        "by default so a failed run can't overwrite a good "
                        "result with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's "
                        "Fingerprint API and apply it to the launched "
                        "browser. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint, where the Scraping Browser supplies "
                        "its own.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" across
    # this family, which the API rejects with HTTP 400, so --fingerprint
    # failed on every invocation in four repos at once (§17).
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. Use --fp-country to narrow further. "
                        "(default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Note what "
                        "neither setting reaches, and it is the normal case "
                        "here: Quora's refusal is Cloudflare's MANAGED "
                        "challenge, which publishes no sitekey — 0 "
                        "data-sitekey attributes and 0 Turnstile iframes on "
                        "the one it served — so there is nothing for a solver "
                        "to answer. Those are reported as a challenge to be "
                        "retried, and nothing is charged.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or "
                        "0.9 — the API only accepts these three). Ignored for "
                        "v2 widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching one locally, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy, --browser-channel and --headless/--headful "
                        "are ignored when this is set.")
    p.add_argument("--cdp-connect-timeout", type=float,
                   default=CDP_CONNECT_TIMEOUT_MS / 1000, metavar="SECONDS",
                   help=f"How long to wait for --cdp-endpoint to accept the "
                        f"connection (default {CDP_CONNECT_TIMEOUT_MS // 1000}). "
                        f"Deliberately high: a Scraping Browser provisions a "
                        f"browser when the WebSocket upgrade arrives, and one "
                        f"was measured taking 121s before the SERVER gave up. "
                        f"Giving up earlier than the server does leaves the "
                        f"profile held by a half-open session — measured "
                        f"`profile_locked` on every later attempt, for over "
                        f"twenty minutes.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure. Useful when the row count is "
                        "right but a column comes back empty — see "
                        "TROUBLESHOOTING.md.")
    # HEADFUL by default. Every measurement in this repo was taken with a
    # real window, and headless was not separately measured against Quora's
    # challenge rate — so the default is the configuration the numbers in the
    # README describe, and --headless is offered without a claim attached.
    p.add_argument("--headful", dest="headless", action="store_false",
                   default=False,
                   help="Run with a real browser window. THE DEFAULT here.")
    p.add_argument("--headless", dest="headless", action="store_true",
                   help="Run headless. Not separately measured against this "
                        "site's challenge rate; every figure in the README "
                        "was taken headful.")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and QUORA_URL is not set in the environment "
                "or in .env.")
    why = unsupported_reason(args.url)
    if why:
        # Refused rather than attempted. The parser's card hooks, its path
        # patterns and its scroll pagination are all this site's, so pointing
        # it at another Q&A site would not fail loudly — it would return zero
        # rows and look like an empty topic.
        p.error(why)

    # An answer permalink is normalised to its question rather than refused.
    # `/{Q}/answer/{Author}` holds one answer; `/{Q}` holds all of them and
    # is what a reader pasting an answer's link almost certainly wants. Said
    # out loud, because silently fetching a different URL than the one given
    # is how a run's rows stop matching its command line.
    normalized = normalize_url(args.url)
    if normalized != args.url:
        logger.info("Fetching %s instead of %s — an answer permalink holds "
                    "one answer, and its question page holds all of them.",
                    normalized, args.url)
        args.url = normalized

    kind = listing_kind(args.url)
    if args.mode is None:
        # Inferred from the URL, which is the only thing that can be right:
        # the mode is a property of the path, not a preference.
        args.mode = kind
        logger.info("Reading %s as a %s feed.", args.url, args.mode)
    elif args.mode != kind:
        p.error(f"--mode {args.mode} does not match {args.url!r}, which is a "
                f"{kind} page. The mode follows the URL on this site; leave "
                f"it off and it is inferred.")
    if args.pages > PAGE_CAP:
        logger.warning("--pages %d is above this scraper's %d-batch cap; it "
                       "will stop there.", args.pages, PAGE_CAP)
    if args.mode == "topic" and args.pages > 1:
        # Said out loud because it changes what the rows CONTAIN, not just
        # how many there are. A topic page's inline payload carries no
        # answers at all, so every row of a topic run is built from the card
        # alone: no upvote count, no view count, no creation time, and a body
        # truncated to the three lines the card renders.
        logger.info(
            "In topic mode every row comes from the rendered card: Quora "
            "sends a topic's answers over a later XHR rather than in the "
            "page, so upvotes, views, creation time and the full answer text "
            "are null on these rows. A question or profile URL carries them. "
            "The data_source column says which view built each row.")
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint "
                     "API uses the same key, though it's a separate "
                     "subscription from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch "
                       "rather than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). `profile_locked` means another run still holds this
        # `pid`, and a harness that sees exit 1 goes looking for a bug in the
        # scraper instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
