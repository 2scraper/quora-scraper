"""page_flow.py — what to do with the page Quora just gave us.

Quora answers a request five ways, and four of them want a different
response, which is why this module exists rather than the same triage being
written three times inside three engines and drifting apart (§1):

    content    answers are in the document, or the inline payload describes
               some
    empty      the site's own "no answers yet" copy, in its own language
    shell      served, built out of Quora's own assets, nothing painted yet.
               Wants a WAIT and a SCROLL, not a refetch
    challenge  Cloudflare's interstitial. Worth RETRYING and worth nothing
               to a solver — see the block constants below
    blocked    a refusal with no page to read, or a page that is not Quora's

The policy lives in `STATE_POLICY` as DATA, so an engine cannot quietly
disagree with its twins about whether a page is worth retrying or worth
paying for.

`shell` is the NORMAL first state here, not an edge case
--------------------------------------------------------
Quora server-renders no content DOM whatsoever — measured 0 occurrences of
its own `q-box` class in the raw response body of six captures, against
385-1165 in the hydrated document. So the first response of a perfectly
healthy topic page is a 100 KB shell, and an engine that treated "no cards in
the first response" as a reason to refetch would refetch every page it ever
loaded and still never see an answer (§18). What the shell DOES carry, on a
question or profile page, is the inline GraphQL payload — which is why
`detect_page_state` asks the payload before it gives up on the document.

Everything here is pure or driven through small callables, so each engine
passes its own driver's primitives and keeps its browser plumbing to itself:

    count(selector) -> int              how many elements match
    scroll_to_bottom() -> None          scroll the window to the document end
    page_height() -> Optional[int]      document.body.scrollHeight
    sleep(ms) -> None                   wait

No JavaScript crosses that boundary in either direction (§1): Selenium's
`execute_script` takes a function BODY with an explicit `return` where
Playwright and pyppeteer take `() => expr`, so this module names the
OPERATION and each engine spells it in its own driver's dialect.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from product_parser import (SELECTORS, PAGE_CAP, CONCURRENCY_REASON,
                            PAGE_URL_REASON, answers_from_payloads,
                            detect_block_marker, detect_page_state,
                            is_challenge_page, paginates_by_url,
                            served_by_quora)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
READY_SELECTOR = SELECTORS["item_card"]

# Above 1, per §5: waiting for a single match resolves on the page's own
# first card long before the feed paints. Measured first paints: 20 cards on
# a topic page, 12 on a question page, 18 on a profile.
MIN_CARD_MATCHES = 2

# Generous against a measured first paint of 3-8s. A residential exit and a
# cold cache are both slower than a laptop on a home connection, and the cost
# of waiting too long is latency where the cost of waiting too little is a
# run that reports an empty topic.
CONTENT_TIMEOUT_MS = 25_000


def ready_selector(mode: str = "") -> str:
    """One selector for every mode: all three feeds render the same card."""
    return READY_SELECTOR


def min_matches(mode: str = "", expected: Optional[int] = None) -> int:
    """How many matches mean "painted".

    `expected` is accepted and CLAMPS the floor, for the one case where it
    matters: a question with a single answer can never reach two cards, and
    without this such a page spends the whole timeout before being parsed
    correctly. Quora publishes that number (`answerCount`) on a question page
    and nowhere else, so on a topic or profile feed this is always None.
    """
    if expected is None or expected <= 0:
        return MIN_CARD_MATCHES
    return max(1, min(MIN_CARD_MATCHES, expected))


def content_timeout_ms(mode: str = "") -> int:
    return CONTENT_TIMEOUT_MS


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 250) -> int:
    """Poll `selector` until `minimum` elements match, or the budget runs out.

    Polls a COUNT rather than waiting on an evaluated string. Playwright's
    `wait_for_function` hands the browser a string to evaluate, which a site
    whose CSP lacks `unsafe-eval` refuses outright — it took a sibling
    repo's run down with `EvalError` and exit 1 on that site's most obvious
    URL (§18).

    Quora's own CSP does allow `unsafe-eval`, read from the response headers
    of a question page on 2026-09-15, so a string wait would in fact work
    here today. It is still not used: a CSP is a header the site can change
    without telling anyone, a count poll is a CDP call under any CSP, and it
    spells the same in all three drivers.

    Returns the last count seen, so a caller can tell "painted" from "timed
    out with one of them".
    """
    waited = 0
    seen = count(selector)
    while seen < minimum and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        seen = count(selector)
    if seen < minimum:
        logger.info("readiness wait ended at %d/%d matches for %s after %dms",
                    seen, minimum, selector, waited)
    return seen


# ---------------------------------------------------------------------------
# The scroll, which on this site is the ONLY pagination there is
# ---------------------------------------------------------------------------
# The window scrolls here, unlike a sibling repo whose results lived in an
# inner container and whose body never moved. Measured on Quora:
# `window.scrollTo(0, document.body.scrollHeight)` took a topic feed from 20
# cards to 30 in two rounds, and the document height grew with it.
#
# Scroll to `document.body.scrollHeight` rather than wheeling a fixed
# distance: a fixed wheel stopped three rounds short of the bottom on a
# sibling site's 7,600px grid, so the lazy-load trigger was never reached and
# a run took 30 of 50 cards while looking settled (§8).
SCROLL_STABLE_ROUNDS = 3        # §8: the next batch takes longer than one pause
SCROLL_MAX_ROUNDS = 12
SCROLL_PAUSE_MS = 2_000


def scroll_until_settled(count: Callable[[str], int],
                         scroll_to_bottom: Callable[[], None],
                         page_height: Callable[[], Optional[int]],
                         sleep: Callable[[int], None],
                         selector: str = READY_SELECTOR,
                         target: Optional[int] = None,
                         max_rounds: int = SCROLL_MAX_ROUNDS) -> int:
    """Scroll until the feed stops growing, and return the final card count.

    Requires the card count AND the document height to hold still for
    `SCROLL_STABLE_ROUNDS` consecutive rounds — the count alone is not
    enough, because a batch can be in flight with the count unchanged and the
    height already growing.

    `target` ends the loop early when it is reached. It is only ever
    meaningful in question mode, where the site publishes the question's own
    answer count; not reaching it ends nothing early, because the gap is
    reported rather than chased (§8).
    """
    seen = count(selector)
    height = page_height()
    stable = 0
    for _ in range(max_rounds):
        if target is not None and seen >= target:
            break
        scroll_to_bottom()
        sleep(SCROLL_PAUSE_MS)
        now, now_height = count(selector), page_height()
        if now == seen and now_height == height:
            stable += 1
            if stable >= SCROLL_STABLE_ROUNDS:
                break
        else:
            stable = 0
        seen, height = now, now_height
    return seen


# ---------------------------------------------------------------------------
# Classification and the policy that follows from it
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """Which of the five states this response is.

    `status` is positional and comes SECOND, matching `detect_page_state`.
    Getting that wrong is not a style question: a sibling repo shipped two of
    three engines calling this as `classify(html, url=...)`, both crashed on
    their first fetch, and nothing short of a live run or a signature-binding
    check saw it (§17). This repo's smoke suite binds every shared-module
    call in every engine for that reason.
    """
    if html is None:
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain in three engines (§1).
#
#   parse    is there anything on this page worth writing down?
#   retry    would fetching it again, later or from a different exit,
#            plausibly help?
#   solve    is there something to pay a solver for?
#   blocked  does this count towards exit 3?
STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "content":   {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # The site was asked and answered. Not retried, because a second fetch of
    # a topic with no answers in it returns the same topic with no answers.
    "empty":     {"parse": False, "retry": False, "solve": False, "blocked": False},
    # Served and still painting — the NORMAL first state on this site. Wants
    # the readiness wait and the scroll, not another fetch: refetching a
    # shell buys another shell (§18). Parsed because by the time an engine
    # asks, the wait has already run, and because a question page's shell
    # carries five answers in its inline payload.
    "shell":     {"parse": True,  "retry": False, "solve": False, "blocked": False},
    # Retried, NOT solved, and COUNTED AS BLOCKED — and that last one differs
    # from every sibling repo, on the evidence of this repo's first live run.
    #
    # The siblings mark `challenge` as not-blocked because on those sites a
    # challenge is solvable: pay, and the page becomes content. Here it never
    # is, so a challenge that survives its retries is simply a refusal — and
    # with `blocked: False` the first live run walked straight past it,
    # handed the 6 KB interstitial to the parser, and reported exit 4 ("ran
    # fine, found nothing") on a topic holding hundreds of answers. That is
    # exactly the confusion §8 forbids: blocked is not empty.
    #
    # `retry` is still True and is tried FIRST, so a challenge that clears —
    # the common case, measured — never reaches this.
    "challenge": {"parse": False, "retry": True,  "solve": False, "blocked": True},
    "blocked":   {"parse": False, "retry": True,  "solve": False, "blocked": True},
}


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["parse"]


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["blocked"])["blocked"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is served but has not painted its feed yet."""
    if state != "shell":
        return False
    return served_by_quora(html or "")


# What Quora actually serves when it refuses, measured 2026-09-15 over about
# thirty fetches from one residential address across seventy-five minutes:
#
#   HTTP 403, `cf-mitigated: challenge`, `<title>Just a moment...</title>`,
#   `_cf_chl_opt = {… cType: 'managed' …}`, a 6 KB body.
#
#   first ~20 minutes   14 fetches, 8 served, 6 challenged
#   everything after    ~30 fetches over three hours, 0 served
#
# Three things follow, and each is a policy constant below rather than a
# paragraph an engine might not read (§17: a policy constant nothing consults
# is the same defect as dead code):
#
#   * EARLY ON it is transient. Within the first window the same URL that
#     answered 403 answered 200 with the full feed on the next attempt about
#     a minute later, from the same address and the same browser. So the
#     retry budget is non-zero.
#   * SUSTAINED it is not, and it does not recover quickly. Every attempt
#     after about the thirtieth was challenged, a 45-minute rest did not
#     clear it, and `es.quora.com` refused the same address at the same
#     moment `www` did — the score follows the ADDRESS, not the zone. So the
#     budget is SMALL: a few retries are worth trying and a long loop is not,
#     because past a point nothing on the client side helps and the run
#     should say so rather than keep paying for latency.
#   * It is NOT SOLVABLE. A managed challenge carries no sitekey: measured 0
#     `data-sitekey` attributes and 0 Turnstile iframes on the interstitial
#     Quora served, with `cType: 'managed'` in its own config. There is
#     nothing to hand 2Captcha, so `should_solve` is False for this state and
#     nothing is ever charged for it.
RETRY_ON_BLOCKED = True
BLOCK_RETRIES_WITHOUT_POOL = 2
BLOCK_RETRIES_WITH_POOL = 3
SOLVES_PER_PAGE = 1


def block_advice(html: Optional[str], headless: bool, has_pool: bool) -> str:
    """What a reader should actually DO about this block.

    Exists because the honest first answer on this site is "wait and try
    again", not "buy a proxy" — and a message that says so saves an
    afternoon and a bill.
    """
    marker = detect_block_marker(html or "") or "HTTP 403"
    lead = f"blocked ({marker})"
    hints: List[str] = []
    if is_challenge_page(html or ""):
        lead = "blocked by Cloudflare's managed challenge"
        hints.append("a rested address recovers: the same URL was served in "
                     "full on the next attempt about a minute later, early in "
                     "a measured window, so --retries with a --retry-delay of "
                     "30s or more is the first thing to try. A BUSY address "
                     "does not: twelve consecutive attempts over twenty-five "
                     "minutes were all challenged once one address had made "
                     "about thirty requests")
        hints.append("no solve was attempted and nothing was charged — a "
                     "managed challenge publishes no sitekey, so there is "
                     "nothing for a captcha solver to answer")
    if headless:
        hints.append("a real window helps: --headful")
    if not has_pool:
        hints.append("the rate is what this site limits on, and it follows "
                     "the ADDRESS rather than the language site — one exit "
                     "went from 8-of-14 served to refused on everything "
                     "after, across both www and es, with nothing changed but "
                     "how much it had fetched. A 45-minute rest did not clear "
                     "it. Raise --delay, rest the address for longer than "
                     "feels necessary, or spread the load with --proxy-file")
    else:
        hints.append("with a pool in play, raise --delay before raising the "
                     "request rate: N exits still means N times the traffic")
    return lead + ". " + "; ".join(hints) + "."


# ---------------------------------------------------------------------------
# Pagination — a scroll, not an address and not a button
# ---------------------------------------------------------------------------
# There is deliberately no `page_url()` here and `product_parser.page_url`
# returns None: Quora publishes no per-page address in any mode. `?page=2` on
# a topic or question URL is not an error — it is IGNORED, and the feed comes
# back with its first items again. A run built on it would add no new sku,
# call the listing exhausted and report COMPLETE holding one batch (§18).
#
# There is no next-page control either: 0 `link[rel=next]`, 0 pagination
# elements and 0 "next" buttons across six captures. What exists is the feed
# extending itself when the reader nears the bottom.
#
# So a "page" in this repo is one SETTLED SCROLL BATCH, and the terminating
# condition is the DATA one §7 asks for: a batch that adds no sku this run
# has not already seen ends the listing. That is not a fallback here — it is
# the only signal there is.
NEXT_BATCH_TIMEOUT_MS = 45_000
NEXT_BATCH_POLL_MS = 1_000

ADVANCED = "advanced"            # the feed grew
NO_GROWTH = "no_growth"          # scrolled, and nothing new arrived


def advance_feed(scroll_to_bottom: Callable[[], None],
                 count: Callable[[str], int],
                 page_height: Callable[[], Optional[int]],
                 sleep: Callable[[int], None],
                 timeout_ms: int = NEXT_BATCH_TIMEOUT_MS) -> str:
    """Scroll once more and wait for the feed to actually grow.

    Returns ADVANCED or NO_GROWTH, and the two must not collapse into one
    bool: a sibling repo mapped both to False, the engine mapped False to
    `pagination_exhausted`, and a run that was being rate-limited reported
    status "complete" holding page 1 (§7).

    Readiness is the CARD COUNT growing. The document height is watched too
    but is not sufficient on its own — Quora grows the page by the height of
    an ad slot while a batch is still in flight, so height alone reports an
    arrival that has not happened.
    """
    before = count(READY_SELECTOR)
    before_height = page_height()
    scroll_to_bottom()
    waited = 0
    while waited < timeout_ms:
        sleep(NEXT_BATCH_POLL_MS)
        waited += NEXT_BATCH_POLL_MS
        now = count(READY_SELECTOR)
        if now > before:
            logger.info("feed grew %d -> %d after %dms", before, now, waited)
            return ADVANCED
        # Keep asking: Quora loads the next batch when the viewport is near
        # the end, and one scroll can land short of the trigger once the page
        # has grown underneath it.
        now_height = page_height()
        if now_height != before_height:
            before_height = now_height
            scroll_to_bottom()
    logger.info("feed did not grow within %dms — treating the listing as "
                "exhausted", timeout_ms)
    return NO_GROWTH


def page_cap_reached(page_num: int) -> bool:
    return page_num >= PAGE_CAP


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------
def page_gap(html: Optional[str], parsed: int) -> Optional[int]:
    """None, always, on this site — and that is the honest answer.

    Quora publishes no per-page counter. A question page states how many
    answers the QUESTION has (253 on one measured page, against twelve
    rendered), but that is the size of the catalogue rather than of the page,
    and reading it as a gap would report 241 missing cards on a page that
    rendered everything it was going to.

    None rather than 0, because an unknown gap is not a gap of zero and the
    two must not read the same in a sidecar (§8).
    """
    return None


def answers_expected(html: Optional[str]) -> Optional[int]:
    """How many answers the QUESTION has in total, where the site says so.

    Not a page counter — see `page_gap`. Used to clamp the readiness floor
    for a one-answer question, and to put the catalogue size in the sidecar
    beside what the run actually read, which is what makes the difference
    between the two visible rather than assumed.
    """
    counts = []
    for node in answers_from_payloads(html, "").values():
        question = node.get("question")
        if isinstance(question, dict):
            counts.append(question.get("answerCount"))
    counts = [c for c in counts if isinstance(c, int) and c > 0]
    return max(counts) if counts else None


# ---------------------------------------------------------------------------
# The feed's length is decided at LOAD, not by scrolling
# ---------------------------------------------------------------------------
# §8's third case, measured on this site: "a different page was served to
# this SESSION — no amount of waiting or scrolling helps".
#
# Four loads of the SAME question URL, 2026-09-15, same machine, same
# browser build, minutes apart:
#
#     cards at first paint   5      13      5      12
#     after scrolling to the bottom and waiting 20s
#                            5      13    259      12
#
# In the 5- and 13-card sessions the scroll worked — `scrollY` reached the
# document's end and stayed there — and Quora simply never fetched more. In
# the 259 one it did. So how much of a question you get is decided when the
# page loads, and a short run is not a broken scroll.
#
# What that means for a caller: re-run it. A fresh browser re-rolls the
# variant. What it means for this module: the scroll loop settling early is
# CORRECT behaviour and must not be made more patient to chase it — more
# rounds against a session that is not going to extend just spends time.
#
# So this is reported rather than fought, and the threshold is deliberately
# generous: a question page legitimately renders a fraction of a large
# question, and the warning is for the case where the fraction is tiny.
SHORT_FEED_SHARE = 0.10


def short_feed_warning(rows: int, available: Optional[int]) -> Optional[str]:
    """A warning when this session was served a much shorter feed than most.

    None when there is nothing to say — which includes every topic and
    profile run, because only a question page states a total.
    """
    if not available or available <= 0 or rows <= 0:
        return None
    if rows >= available * SHORT_FEED_SHARE:
        return None
    return (f"This session was served {rows} of the question's {available} "
            f"answers and scrolling did not extend it. That is a property of "
            f"the SESSION rather than of the scroll: four loads of one "
            f"measured question gave 5, 13, 12 and 259 answers, and in the "
            f"short ones the scroll reached the bottom and the site simply "
            f"never fetched more. A fresh browser re-rolls it — re-run, and "
            f"prefer the run that got more.")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str = "") -> Optional[int]:
    """Always 1. See `concurrency_refusal`."""
    return 1


def concurrency_refusal(url: str) -> Optional[str]:
    """Why concurrency above 1 is refused for this URL.

    Refused WITH the reason rather than silently running one worker, which
    would look like the flag did something.
    """
    if not paginates_by_url(url):
        return (f"{PAGE_URL_REASON}, so {CONCURRENCY_REASON}. Batch 5 of an "
                f"infinite scroll exists only inside the browser that "
                f"scrolled through batches 1-4. Run several topics or "
                f"profiles in parallel instead, one process each")
    return None
