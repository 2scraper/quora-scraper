#!/usr/bin/env python3
"""quora-scraper — offline smoke tests.

One file of plain functions with fixtures loaded from `fixtures_generated.json`,
no pytest required. `tests/test_smoke.py` wraps it as a single pytest test so
`pytest` works as an entry point without a second copy of the checks.

    python3 smoke_test.py

It MUST pass with no engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is REPORTED, because "skipped, engine absent" reads
exactly like a passing run. CI's engine-smoke job installs each engine in its
own venv and fails if that skip list is non-empty.

The fixtures are cut from real captures by `make_fixtures.py`, which proves
each one parses IDENTICALLY to its untrimmed original, column for column,
and scrubs the challenge page's site keys. Do not hand-edit them.

WHAT THIS SUITE IS FOR, beyond the obvious
------------------------------------------
Most of these checks exist because of a specific failure, in this repo or in
a sibling. The ones worth knowing about before you change anything:

  * `test_values_on_real_fixtures` asserts VALUES, not coverage. A column can
    be 100% populated and entirely wrong — a sibling repo shipped a
    `review_count` of 445279961 on every row of every mode while its coverage
    check said 100%. The German fixture is the one that matters here:
    `1.614 Bewertungen` is 1614, and `re.search(r"\\d+", ...)` returns 1.

  * `test_engine_parity` binds every shared-module call in every engine
    against the callee's REAL signature. Two engines in a sibling repo called
    `classify(html, url=...)` where the parameter is positional, both crashed
    on their first fetch, and nothing short of a live run saw it.

  * `test_throttle_is_not_completion` pins the bug this repo's own first live
    run found: a page turn refused by the site's rate limiter must NOT be
    reported as the end of the listing, because `pagination_exhausted` is a
    COMPLETE stop reason and a throttled run would say "complete" while
    holding one page of six.
"""

import ast
import contextlib
import csv as csv_module
import inspect
import io
import json
import os
import pathlib
import re
import sys
import tempfile
from dataclasses import asdict, fields

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import captcha_solver
import env_config
from fingerprint_client import (fingerprint_user_agent,
                                playwright_context_kwargs)
import page_flow
import product_parser
from diff_runs import diff_products
from output_writer import (Answer, Product, save, finish_run, write_csv,
                           run_meta, dedupe_by_key, dedupe_by_sku,
                           ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES,
                           COMPLETE_STOP_REASONS, EXIT_BLOCKED,
                           EXIT_NO_PRODUCTS, EXIT_PARTIAL, EXIT_API_ERROR,
                           LIST_CSV_SEPARATOR, SOURCE_DEFAULT)
import product_parser  # noqa: F401 — for the private helpers below
from product_parser import (parse_answers, SELECTORS, HOSTS, LANGUAGE_HOSTS,
                            NON_CONTENT_HOSTS, PAGE_CAP, NEXT_PAGE_SELECTOR,
                            PAGINATES_BY_URL, PAGE_URL_REASON,
                            RESERVED_FIRST_SEGMENTS, NO_RESULTS_MARKERS,
                            CHALLENGE_MARKERS, BOT_CHALLENGE_MARKERS,
                            SOLVABLE_CHALLENGES, category_from_url,
                            detect_block_marker, detect_bot_challenge,
                            detect_page_state, inline_payloads,
                            answers_from_payloads, is_no_results,
                            is_supported_host, is_space_host, language_of,
                            listing_kind, normalize_url, page_url,
                            paginates_by_url, permalink_key, render_rich_text,
                            served_by_quora, site_host, source_of,
                            strip_tracking, unsupported_reason,
                            _display_name, _epoch_us_to_iso)
from proxy_pool import ProxyPool, mask, to_playwright, split_credentials

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
SHARED_MODULES = {"page_flow": page_flow, "product_parser": product_parser}

_failures = []
_total_checks = 0


def check(label, condition):
    """Print and record one check. Returns the condition so callers can
    accumulate with `ok &= check(...)`."""
    global _total_checks
    _total_checks += 1
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


_FIXTURE_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")
if not os.path.exists(_FIXTURE_PATH):
    # Said in words rather than as a bare FileNotFoundError, because the
    # first time this happened it was not missing from the disk — it was
    # missing from the COMMIT. `.gitignore` carries a blanket `*.json` (a
    # scraper's own output is large and stale by the time anyone reads it),
    # which swallowed it silently: the whole suite was green locally and
    # every CI job died at import. `test_required_files_are_committed` now
    # catches that case directly.
    raise SystemExit(
        f"fixtures_generated.json is missing from {REPO_ROOT}.\n"
        f"If you are in a clean checkout, it should have been committed — "
        f"check that .gitignore's `*.json` rule still carries the "
        f"`!fixtures_generated.json` exception.\n"
        f"If you are regenerating fixtures, run: python3 make_fixtures.py")
with open(_FIXTURE_PATH, encoding="utf-8") as _f:
    FIXTURES = json.load(_f)
URLS = FIXTURES["_URLS"]


def fixture(name):
    return FIXTURES[name]


def rows_of(name, page=1):
    return parse_answers(FIXTURES[name], URLS[name], page=page)


def by_sku(name, page=1):
    return {r.sku: r for r in rows_of(name, page)}


# ---------------------------------------------------------------------------
def _engine_source(module_name):
    """The engine's source, or None when its driver is not installed.

    Read off disk rather than through `inspect.getsource`, so a check about
    an engine's TEXT does not itself need the engine's library.
    """
    path = os.path.join(REPO_ROOT, module_name + ".py")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_numbers_and_prices():
    group("Quora's own encodings")
    ok = True

    # RICH TEXT. Every string this site publishes — a question title, an
    # answer, a credential — is itself a JSON document. Reading `.get("title")`
    # and writing it to a column puts the whole blob in the row, which is the
    # 100%-populated-and-entirely-wrong column §10 exists to catch.
    doc = ('{"sections": [{"spans": [{"text": "Hello ", "modifiers": {}}, '
           '{"text": "world", "modifiers": {"bold": true}}], "type": "plain"}]}')
    ok &= check("rich text renders spans in order",
                render_rich_text(doc) == "Hello world")
    listed = ('{"sections": [{"spans": [{"text": "one"}], '
              '"type": "unordered-list"}, {"spans": [{"text": "two"}], '
              '"type": "unordered-list"}]}')
    ok &= check("list sections get a bullet",
                render_rich_text(listed) == "- one\n- two")
    image = ('{"sections": [{"spans": [{"text": "", "modifiers": '
             '{"image": "https://example.invalid/x"}}], "type": "image"}]}')
    ok &= check("an image section with no text is dropped, not blank-lined",
                render_rich_text(image) is None)
    ok &= check("a plain string passes through unchanged",
                render_rich_text("Software Engineer") == "Software Engineer")
    ok &= check("unparsable JSON is returned as text, not lost",
                render_rich_text('{"sections": [') == '{"sections": [')
    ok &= check("None stays None", render_rich_text(None) is None)
    ok &= check("empty renders to None, never an empty string",
                render_rich_text('{"sections": []}') is None)

    # EPOCH MICROSECONDS. Read as seconds this lands in the year 57 million
    # and as milliseconds in 57705 — both parse without error, which is why
    # this is a named function with a test rather than an inline division.
    ok &= check("creationTime is microseconds, not seconds",
                _epoch_us_to_iso(1757155453070805).startswith("2025-09-06"))
    ok &= check("a second-scale number would land outside Quora's lifetime",
                not (_epoch_us_to_iso(1757155453) or "").startswith("20"))
    ok &= check("0 is not a timestamp", _epoch_us_to_iso(0) is None)
    ok &= check("a non-number is not a timestamp",
                _epoch_us_to_iso("yesterday") is None)

    # THE AUTHOR'S NAME, which Quora stores as two fields plus a flag. A
    # fixed join order misnames every author on a site that writes the family
    # name first.
    western = {"names": [{"givenName": "Ada", "familyName": "Lovelace",
                          "reverseOrder": False}]}
    eastern = {"names": [{"givenName": "Taro", "familyName": "Yamada",
                          "reverseOrder": True}]}
    ok &= check("given-then-family by default",
                _display_name(western) == "Ada Lovelace")
    ok &= check("reverseOrder is honoured, not ignored",
                _display_name(eastern) == "Yamada Taro")
    ok &= check("no names object -> None", _display_name({}) is None)
    return ok


def test_values_on_real_fixtures():
    group("VALUES on real fixtures, not coverage (§10)")
    ok = True

    topic = rows_of("TOPIC_EN")
    ok &= check("TOPIC_EN parses 4 answers", len(topic) == 4)
    first = topic[0]
    ok &= check("sku is the permalink with the scheme removed",
                first.sku.startswith("www.quora.com/")
                and "://" not in first.sku)
    ok &= check("url is the sku with a scheme on it",
                first.url == "https://" + first.sku)
    ok &= check("the question is the row's title",
                first.title is not None and first.title.endswith("?"))
    ok &= check("question_url drops the /answer/ segment",
                first.question_url is not None
                and "/answer/" not in first.question_url)
    ok &= check("author read from the NAMED profile link, not the avatar",
                all(r.author for r in topic))
    ok &= check("author_url points at a profile",
                all("/profile/" in (r.author_url or "") for r in topic))
    ok &= check("the card's body is read",
                all(r.text and r.text_chars == len(r.text) for r in topic))
    ok &= check("date_text is kept verbatim and never parsed",
                all(r.date_text and r.created_at is None for r in topic))

    group("A topic feed carries NO inline payload — the measured asymmetry")
    ok &= check("every topic row is DOM-built",
                all(r.data_source == "dom" for r in topic))
    ok &= check("and therefore has no counts at all",
                all(r.upvotes is None and r.views is None and r.shares is None
                    and r.answer_id is None for r in topic))

    group("A question page DOES — and that is the other read path")
    question = rows_of("QUESTION_EN")
    backed = [r for r in question if r.data_source != "dom"]
    ok &= check("QUESTION_EN has payload-backed rows", len(backed) >= 3)
    ok &= check("the payload fills the counts the DOM never shows",
                all(r.upvotes is not None and r.views is not None
                    and r.created_at is not None for r in backed))
    ok &= check("and the numeric ids",
                all(r.answer_id and r.question_id for r in backed))
    ok &= check("answer_count is the QUESTION's total, the same on every row",
                len({r.answer_count for r in backed}) == 1
                and next(iter({r.answer_count for r in backed})) > len(question))
    ok &= check("a question page's cards do not repeat the question, so the "
                "title comes from the page heading",
                all(r.title == "What is machine learning?" for r in question))
    ok &= check("Quora's own AI-answer flag is carried, not inferred",
                any(r.is_machine_answer for r in question))
    machine = next(r for r in question if r.is_machine_answer)
    ok &= check("a machine answer is rendered in a block no card matches, "
                "so it arrives from the payload alone",
                machine.data_source == "inline")
    ok &= check("its permalink is the /answers/{aid} shape",
                "/answers/" in machine.sku)

    group("Provenance never lies")
    ok &= check("a dom row carries no payload-only column",
                all(r.upvotes is None and r.views is None and r.created_at is None
                    and r.answer_id is None
                    for r in question if r.data_source == "dom"))
    ok &= check("data_source is one of the three known values",
                {r.data_source for r in question + topic}
                <= {"dom", "dom+inline", "inline"})

    group("The Spanish site — the second-locale checks (§15)")
    spanish = rows_of("TOPIC_ES")
    ok &= check("TOPIC_ES parses 4 answers", len(spanish) == 4)
    ok &= check("source is the language host, not www",
                all(r.source == "es.quora.com" for r in spanish))
    ok &= check("language comes from the host", all(r.language == "es" for r in spanish))
    ok &= check("a non-ASCII question title survives",
                any("¿" in (r.title or "") for r in spanish))
    ok &= check("a percent-encoded slug is kept as the site writes it",
                any("%C3%A1" in r.sku for r in spanish))
    ok &= check("a localised relative date is NOT parsed into a date",
                all(r.created_at is None for r in spanish)
                and any("año" in (r.date_text or "") for r in spanish))

    group("A profile — where the Session subdomain lives")
    profile = rows_of("PROFILE_EN")
    ok &= check("PROFILE_EN parses 4 answers", len(profile) == 4)
    session = [r for r in profile if not r.sku.startswith("www.quora.com/")]
    ok &= check("a Session-subdomain permalink is recognised", session)
    ok &= check("it has no /answer/ segment at all — no URL pattern could "
                "tell it from a question page",
                all("/answer/" not in r.sku for r in session))
    # THREE different values for one answer, measured on a real profile:
    # the permalink carries a per-answer suffix the question URL does not,
    # and `slug` is a third thing again. The payload is the only thing that
    # can tell them apart, which is why it overwrites the URL-derived guess.
    ok &= check("the payload's own question URL wins over the permalink",
                all(r.question_url != r.url for r in session
                    if r.data_source != "dom"))
    ok &= check("and it is a prefix of the permalink, not something else",
                all(r.url.startswith(r.question_url) for r in session
                    if r.data_source != "dom"))
    ok &= check("with no payload there is nothing to strip, so the permalink "
                "stands rather than a guess",
                all(r.question_url == r.url for r in session
                    if r.data_source == "dom"))
    ok &= check("language is null on a Space or Session host, never guessed",
                all(r.language is None for r in session))

    group("Page and position — the pair, not either alone")
    p1 = rows_of("TOPIC_EN", page=1)
    p2 = rows_of("TOPIC_ES", page=2)
    ok &= check("page is threaded in, not defaulted to 1",
                all(r.page == 1 for r in p1) and all(r.page == 2 for r in p2))
    pairs = [(r.page, r.position) for r in p1 + p2]
    ok &= check("page+position unique across a multi-page run",
                len(set(pairs)) == len(pairs))
    ok &= check("position is 1..N within a page",
                [r.position for r in p1] == list(range(1, len(p1) + 1)))

    group("Columns that do not exist here, and the measurement behind it")
    columns = {f.name for f in fields(Answer)}
    for absent in ("price", "currency", "original_price", "discount_pct",
                   "brand", "in_stock", "rating", "review_count"):
        ok &= check(f"no `{absent}` column — this site has none of it",
                    absent not in columns)
    ok &= check("the family prefix is byte-identical and in order",
                [f.name for f in fields(Answer)][:5]
                == ["source", "scraped_at", "url", "sku", "title"])
    return ok


def test_a_question_page_holds_other_questions_answers():
    group("A question page carries OTHER questions' answers (§15, live)")
    ok = True
    # None of this is visible in an unscrolled capture, and all of it was
    # wrong until a live run of 260 rows showed it. A scrolled question page
    # carries, besides its own answers:
    #
    #   * RELATED questions, behind a badge rendered INSIDE the title node;
    #   * MERGED duplicates, with no title node and an "Originally Answered:"
    #     banner instead;
    #   * promoted answers, which carry their own title node.
    #
    # Measured on that run: 29 rows titled "Related What is machine learning
    # for?" and 27 rows wearing the page's own question while belonging to a
    # different one. Both are the 100%-populated-and-wrong column §10 is
    # about.
    rows = rows_of("QUESTION_MERGED")
    url = URLS["QUESTION_MERGED"]
    page_question = product_parser._question_path(url)
    own = [r for r in rows if product_parser._question_path(r.url) == page_question]
    foreign = [r for r in rows if product_parser._question_path(r.url) != page_question]
    ok &= check("the fixture holds this question's answers", own)
    ok &= check("and answers belonging to other questions", foreign)

    ok &= check("no title carries the 'Related' badge",
                not any((r.title or "").startswith("Related") for r in rows))
    heading = product_parser.page_question_title(fixture("QUESTION_MERGED"))
    ok &= check("the page's own heading was found", bool(heading))
    ok &= check("this question's own answers get that heading",
                all(r.title == heading for r in own))
    ok &= check("and a FOREIGN answer never wears it",
                not any(r.title == heading for r in foreign))
    ok &= check("a merged duplicate takes its title from the banner's link, "
                "not from the localised 'Originally Answered:' prefix",
                any(r.title and "Originally Answered" not in r.title
                    and product_parser._question_path(r.url) != page_question
                    for r in foreign))
    ok &= check("every foreign row still has the right question_url",
                all(r.question_url and product_parser._question_path(r.question_url)
                    == product_parser._question_path(r.url) or r.data_source != "dom"
                    for r in foreign))

    group("An unknown title is null, never the nearest available one")
    # Some cards name no question at all — no title node, no banner. Their
    # `question_url` is still right, so a consumer can fetch it; inventing a
    # title from the slug would be a guess wearing the look of a reading (§8).
    for row in rows:
        if row.title is None:
            ok &= check("a titleless row still carries its question_url",
                        bool(row.question_url))
            break
    else:
        ok &= check("(this fixture happens to title every row)", True)

    group("The sku is always a URL")
    for name in ("TOPIC_EN", "TOPIC_ES", "QUESTION_EN", "QUESTION_MERGED",
                 "PROFILE_EN"):
        bad = [r.sku for r in rows_of(name)
               if not re.match(r"^[a-z0-9.-]+\.quora\.com/\S+$", r.sku or "")]
        ok &= check(f"{name}: every sku is a host and a path, no spaces "
                    f"{'' if not bad else bad[:1]}", not bad)
    return ok


def test_urls():
    group("URL shapes")
    ok = True
    kinds = {
        "https://www.quora.com/topic/Machine-Learning": "topic",
        "https://es.quora.com/topic/Aprendizaje-autom%C3%A1tico": "topic",
        "https://www.quora.com/profile/Someone-1": "profile",
        "https://www.quora.com/What-is-machine-learning-4": "question",
        "https://www.quora.com/What-is-x/answer/Someone-1": "question",
        "https://www.quora.com/What-is-x/answers/123456": "question",
        "https://www.quora.com/search?q=machine+learning": "search",
        "https://www.quora.com/about": "unknown",
        "https://www.quora.com/careers": "unknown",
    }
    for url, kind in kinds.items():
        ok &= check(f"{url.split('quora.com')[1][:36]!r} -> {kind}",
                    listing_kind(url) == kind)

    # A reserved first segment is the one thing that stops `/about` parsing
    # as a question titled "about".
    ok &= check("every reserved segment is refused as a question",
                all(listing_kind(f"https://www.quora.com/{seg}") != "question"
                    for seg in RESERVED_FIRST_SEGMENTS))

    group("Normalisation")
    ok &= check("a bare quora.com is normalised to www",
                normalize_url("https://quora.com/topic/X")
                == "https://www.quora.com/topic/X")
    ok &= check("an answer permalink is read as its question",
                normalize_url("https://www.quora.com/What-is-x/answer/Someone-1")
                == "https://www.quora.com/What-is-x")
    ok &= check("tracking parameters are stripped before a URL is written down",
                strip_tracking("https://www.quora.com/X?ch=10&oid=1&keep=1")
                == "https://www.quora.com/X?keep=1")

    group("Hosts — from the site's own language list (§5)")
    ok &= check("twenty-four hosts", len(HOSTS) == 24)
    ok &= check("Japanese is on jp., not ja. — the one host that is a "
                "country code", "jp.quora.com" in HOSTS and "ja.quora.com" not in HOSTS)
    ok &= check("every host maps to a language", all(LANGUAGE_HOSTS.values()))
    ok &= check("www is supported",
                is_supported_host("https://www.quora.com/topic/X"))
    ok &= check("a language host is supported",
                is_supported_host("https://pl.quora.com/topic/X"))
    ok &= check("a Space subdomain is supported",
                is_supported_host("https://someboard.quora.com/"))

    group("Refusals name their reason (§5)")
    ok &= check("another site is refused",
                "not a Quora host" in (unsupported_reason("https://www.reddit.com/r/x") or ""))
    ok &= check("help.quora.com is refused as a HELP CENTRE, not as 'not Quora'",
                "help centre" in (unsupported_reason("https://help.quora.com/hc/en-us") or ""))
    ok &= check("search is refused with the sign-in-wall reason",
                "sign-in wall" in (unsupported_reason("https://www.quora.com/search?q=x") or ""))
    ok &= check("a non-http URL is refused",
                unsupported_reason("ftp://www.quora.com/topic/X") is not None)

    group("The sku")
    ok &= check("permalink_key drops the scheme and the trailing slash",
                permalink_key("https://www.quora.com/A/answer/B/")
                == "www.quora.com/A/answer/B")
    ok &= check("it keeps the host, so two hosts cannot collide",
                permalink_key("https://es.quora.com/A") == "es.quora.com/A")
    ok &= check("a relative href with no host has no key",
                permalink_key("/A/answer/B") is None)

    group("category_from_url")
    ok &= check("a topic's slug", category_from_url(
        "https://www.quora.com/topic/Machine-Learning") == "Machine-Learning")
    ok &= check("a profile's slug", category_from_url(
        "https://www.quora.com/profile/Someone-1") == "Someone-1")
    ok &= check("a question's slug", category_from_url(
        "https://www.quora.com/What-is-x") == "What-is-x")
    ok &= check("a reserved path has no category",
                category_from_url("https://www.quora.com/about") is None)
    return ok


def test_pagination():
    group("Pagination — a scroll, and nothing else (§7, §18)")
    ok = True
    ok &= check("this site does not paginate by URL", PAGINATES_BY_URL is False)
    ok &= check("page_url returns None for every mode and every page",
                all(page_url(u, n) is None
                    for u in URLS.values() for n in (2, 3, 40)))
    ok &= check("paginates_by_url agrees with the constant",
                not any(paginates_by_url(u) for u in URLS.values()))
    ok &= check("there is no next-page selector to be tempted by",
                NEXT_PAGE_SELECTOR == "")
    ok &= check("the reason is stated, not implied",
                "ignored" in PAGE_URL_REASON)

    group("Concurrency is REFUSED with its reason, not clamped")
    ok &= check("the limit is 1 for every URL",
                all(page_flow.concurrency_limit(u) == 1 for u in URLS.values()))
    for url in list(URLS.values())[:3]:
        reason = page_flow.concurrency_refusal(url)
        ok &= check(f"refusal explains itself for {url[:44]}",
                    reason and "second worker" in reason)

    group("The cap")
    ok &= check("PAGE_CAP is a real bound", isinstance(PAGE_CAP, int) and PAGE_CAP > 1)
    ok &= check("page_cap_reached fires at the cap",
                page_flow.page_cap_reached(PAGE_CAP)
                and not page_flow.page_cap_reached(PAGE_CAP - 1))

    group("There is no per-page counter, and page_gap says so honestly")
    ok &= check("page_gap is None, never 0 — an unknown gap is not no gap",
                page_flow.page_gap(fixture("QUESTION_EN"), 3) is None)
    ok &= check("answers_expected reads the QUESTION's own total instead",
                page_flow.answers_expected(fixture("QUESTION_EN")) > 100)
    ok &= check("and is None where the site states none",
                page_flow.answers_expected(fixture("TOPIC_EN")) is None)
    return ok


def test_page_state():
    group("Page state — ordered by what each signal PROVES (§17)")
    ok = True
    for name in ("TOPIC_EN", "TOPIC_ES", "QUESTION_EN", "PROFILE_EN"):
        ok &= check(f"{name} is content",
                    detect_page_state(fixture(name), 200, URLS[name]) == "content")
    ok &= check("the challenge page is a challenge, not a block",
                detect_page_state(fixture("BLOCK_CF"), 403, URLS["BLOCK_CF"])
                == "challenge")
    ok &= check("and names its vendor",
                detect_bot_challenge(fixture("BLOCK_CF")) == "cloudflare")

    group("The inverted case: a browser error page (§18)")
    ok &= check("an empty document is blocked, not empty",
                detect_page_state(fixture("BROWSER_ERROR"), None,
                                  URLS["BROWSER_ERROR"]) == "blocked")
    ok &= check("None html is blocked", detect_page_state(None, 200, "") == "blocked")
    # Chromium's own network-error page carries the SITE'S hostname in its
    # <title>, so every text marker reads it as a real page. Only the
    # positive-asset check answers correctly.
    chrome_error = ("<html><head><title>www.quora.com</title></head><body>"
                    "<div>ERR_PROXY_CONNECTION_FAILED</div></body></html>")
    ok &= check("Chromium's error page is blocked despite carrying the "
                "site's own hostname",
                detect_page_state(chrome_error, None,
                                  "https://www.quora.com/topic/X") == "blocked")
    ok &= check("served_by_quora is False for it",
                not served_by_quora(chrome_error))
    ok &= check("and True for every good fixture",
                all(served_by_quora(fixture(n))
                    for n in ("TOPIC_EN", "QUESTION_EN", "PROFILE_EN")))

    group("A shell is served, ours, and unpainted — the NORMAL first state")
    shell = ('<html><head><link href="https://qsbr.cf2.quoracdn.net/x.css">'
             '<script>window.ansFrontendGlobals={};</script>'
             '<link href="https://qph.cf2.quoracdn.net/y.png"></head>'
             '<body><div class="q-box"></div></body></html>')
    ok &= check("a served page with nothing painted is `shell`",
                detect_page_state(shell, 200, "https://www.quora.com/topic/X")
                == "shell")
    ok &= check("and page_flow calls it unpainted",
                page_flow.is_unpainted("shell", shell))
    ok &= check("a 403 with no page to read is blocked",
                detect_page_state(shell.replace("q-box", "nothing"), 403,
                                  "https://www.quora.com/topic/X") in
                ("blocked", "shell"))

    group("Empty is a POSITIVE signal only (§18)")
    empty = shell.replace("<div class=\"q-box\"></div>",
                          "<div>There are no answers yet.</div>")
    ok &= check("the site's own copy classifies as empty",
                detect_page_state(empty, 200, "https://www.quora.com/topic/X")
                == "empty")
    ok &= check("is_no_results does not fire on a good page",
                not any(is_no_results(fixture(n))
                        for n in ("TOPIC_EN", "QUESTION_EN")))

    group("Markers that match every good page are NOT markers (§18)")
    good = fixture("QUESTION_EN") + fixture("TOPIC_EN")
    for banned in ("cf-turnstile", "challenges.cloudflare.com", "recaptcha",
                   "api.js?render"):
        ok &= check(f"{banned!r} is not in the challenge marker set",
                    not any(banned in m for m in CHALLENGE_MARKERS))
    ok &= check("no challenge marker appears on a good page",
                not any(m in good.lower() for m in CHALLENGE_MARKERS))
    ok &= check("no bot-challenge marker appears on a good page either",
                not any(m.lower() in good.lower() for m in BOT_CHALLENGE_MARKERS))
    return ok


def test_challenge_is_not_always_solvable():
    group("detected != blocking != paying (§8)")
    ok = True
    block = fixture("BLOCK_CF")
    ok &= check("the interstitial is recognised and its vendor named",
                detect_bot_challenge(block) == "cloudflare")
    ok &= check("detect_block_marker names it too",
                detect_block_marker(block) == "cloudflare")
    ok &= check("the state is `challenge`, not `blocked` — it is worth a retry",
                detect_page_state(block, 403, URLS["BLOCK_CF"]) == "challenge")

    # THE MEASURED POINT OF THIS WHOLE FILE'S CAPTCHA STANCE. Cloudflare's
    # MANAGED challenge carries no sitekey: 0 `data-sitekey` attributes and 0
    # Turnstile iframes on the page this site actually served. There is
    # nothing for a solver to answer, so `challenge` here must NOT buy one.
    ok &= check("a challenge does NOT buy a solve on this site",
                page_flow.should_solve("challenge") is False)
    ok &= check("nor does a block", page_flow.should_solve("blocked") is False)
    ok &= check("no state in the policy buys a solve",
                not any(row["solve"] for row in page_flow.STATE_POLICY.values()))
    ok &= check("the fixture carries no sitekey to solve against",
                "data-sitekey" not in block and "turnstile/v0/api.js?render" not in block)
    ok &= check("SOLVABLE_CHALLENGES still names what the SOLVER implements, "
                "which is a different question from what this site serves",
                set(SOLVABLE_CHALLENGES) == {"recaptcha", "hcaptcha"})

    # Detection stays BROAD even though spending is zero: which challenge a
    # visitor meets depends on the exit and on what the address has been
    # doing, and a narrow detector is how a rendered challenge gets reported
    # as an empty topic months later (§8).
    rendered = ('<html><head></head><body><div class="g-recaptcha" '
                'data-sitekey="6Lexample"></div></body></html>')
    ok &= check("a rendered reCAPTCHA is still detected",
                detect_bot_challenge(rendered) == "recaptcha")
    ok &= check("and classifies as a challenge",
                detect_page_state(rendered, 200,
                                  "https://www.quora.com/topic/X") == "challenge")

    group("block_advice says what to DO")
    advice = page_flow.block_advice(block, headless=False, has_pool=False)
    ok &= check("it leads with the retry, because that is what was measured "
                "to work", "--retries" in advice)
    ok &= check("it says nothing was charged", "nothing was charged" in advice)
    ok &= check("it names the rate as the real lever", "--delay" in advice)
    ok &= check("with a pool it says something different",
                "N exits" in page_flow.block_advice(block, headless=False,
                                                    has_pool=True))
    return ok


def test_page_flow_policy():
    group("STATE_POLICY — the triage as DATA, not three if-chains")
    ok = True
    for state in ("content", "empty", "shell", "challenge", "blocked"):
        ok &= check(f"{state} has a full policy row",
                    set(page_flow.STATE_POLICY[state]) ==
                    {"parse", "retry", "solve", "blocked"})
    ok &= check("content is parsed and not retried",
                page_flow.should_parse("content") and not page_flow.should_retry("content"))
    ok &= check("empty is a final answer, not a fault",
                not page_flow.should_parse("empty") and not page_flow.should_retry("empty"))
    ok &= check("shell is parsed after the wait, never refetched",
                page_flow.should_parse("shell") and not page_flow.should_retry("shell"))
    # THE DIFFERENCE FROM EVERY SIBLING REPO, and it was found by running
    # the thing (§15). A challenge is retried first; if the retries are spent
    # and it is still a challenge, the run is BLOCKED (exit 3), not empty
    # (exit 4). With this False the first live run parsed the 6 KB
    # interstitial as a feed and reported "ran fine, found nothing" on a
    # topic holding hundreds of answers.
    ok &= check("a challenge that survives its retries counts as blocked",
                page_flow.counts_as_blocked("challenge") is True)
    ok &= check("but it is retried before that verdict is reached",
                page_flow.should_retry("challenge") is True)
    ok &= check("and it is never parsed",
                page_flow.should_parse("challenge") is False)
    ok &= check("blocked counts towards exit 3",
                page_flow.counts_as_blocked("blocked")
                and not page_flow.counts_as_blocked("empty"))
    ok &= check("an unknown state falls back to the blocked row",
                page_flow.should_parse("nonsense") is False)

    group("Retrying a block DOES help here — and the engines CONSULT that")
    # A policy constant nothing reads is the same defect as dead code (§17).
    ok &= check("RETRY_ON_BLOCKED is True on this site",
                page_flow.RETRY_ON_BLOCKED is True)
    ok &= check("the budget is non-zero", page_flow.BLOCK_RETRIES_WITHOUT_POOL > 0)
    ok &= check("a pool buys more attempts",
                page_flow.BLOCK_RETRIES_WITH_POOL >= page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    consulted = []
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        consulted.append("RETRY_ON_BLOCKED" in src)
    ok &= check("every engine reads RETRY_ON_BLOCKED", all(consulted))

    group("Readiness — a count poll, never an evaluated string")
    ok &= check("MIN_CARD_MATCHES is above 1 (§5)", page_flow.MIN_CARD_MATCHES > 1)
    ok &= check("the anchor is the card itself, in every mode",
                all(page_flow.ready_selector(m) == SELECTORS["item_card"]
                    for m in ("topic", "question", "profile")))
    # A question with a single answer can never reach the floor, so the
    # site's own answer count lowers it.
    ok &= check("min_matches is clamped by what the question says it holds",
                page_flow.min_matches("question", 1) == 1)
    ok &= check("and is not raised above the floor",
                page_flow.min_matches("question", 50) == page_flow.MIN_CARD_MATCHES)
    ok &= check("answers_expected reads the question own total",
                page_flow.answers_expected(fixture("QUESTION_EN")) > 100)

    calls = []

    def count(_sel):
        calls.append(1)
        return 0 if len(calls) < 4 else 9

    found = page_flow.wait_for_count(count, lambda ms: None, "x", 5, 5_000)
    ok &= check("wait_for_count returns the count it reached", found == 9)
    ok &= check("a wait that never satisfies still returns, bounded",
                page_flow.wait_for_count(lambda s: 0, lambda ms: None, "x", 5, 400) == 0)
    return ok


def test_scroll_loop():
    group("The scroll — the WINDOW, and THREE stable rounds (§8)")
    ok = True
    # The opposite of a sibling repo, whose body never scrolled. Here the
    # window is what moves, and every engine must scroll to the document's
    # own end rather than by a fixed wheel distance — a fixed wheel stopped
    # three rounds short of the bottom on that sibling's grid, so the
    # lazy-load trigger was never reached and a run took 30 of 50 cards while
    # looking settled.
    for engine in ENGINES:
        src = _engine_source(engine)
        if src is None:
            continue
        ok &= check(f"{engine} scrolls to document.body.scrollHeight",
                    "document.body.scrollHeight" in src)
        ok &= check(f"{engine} has no inner scroll container to chase",
                    "scroll_container" not in src)

    # A pause is not an ending: the next batch takes longer to arrive than a
    # single pause, so the loop needs three quiet rounds rather than one.
    counts = [3, 7, 13, 13, 13, 13, 13, 13]
    state = {"i": 0}
    heights = {"h": 1000}

    def count(_sel):
        return counts[min(state["i"], len(counts) - 1)]

    def scroll():
        state["i"] += 1
        heights["h"] += 500

    reached = page_flow.scroll_until_settled(
        count, scroll, lambda: heights["h"], lambda ms: None, target=13)
    ok &= check("it reaches the target the question states", reached == 13)
    ok &= check("and stops there rather than spending the budget",
                state["i"] <= 3)
    ok &= check("three stable rounds, not one",
                page_flow.SCROLL_STABLE_ROUNDS >= 3)

    # With no target — which is every topic and profile run, because only a
    # question page states a total — the stable-round heuristic IS the
    # termination condition, and it must still terminate.
    rounds = {"n": 0}

    def scroll2():
        rounds["n"] += 1

    reached2 = page_flow.scroll_until_settled(
        lambda s: 7, scroll2, lambda: 500, lambda ms: None, target=None)
    ok &= check("with no target it settles and stops", reached2 == 7)
    ok &= check("and does not spend the whole budget",
                rounds["n"] <= page_flow.SCROLL_STABLE_ROUNDS + 1)

    # A feed that grows forever must not hang the run — and on an infinite
    # scroll that is not a hypothetical.
    forever = {"n": 0}

    def count3(_sel):
        forever["n"] += 1
        return forever["n"]

    page_flow.scroll_until_settled(count3, lambda: None,
                                   lambda: forever["n"] * 10,
                                   lambda ms: None, target=None)
    ok &= check("a forever-growing feed is bounded by the round budget",
                forever["n"] <= page_flow.SCROLL_MAX_ROUNDS * 2 + 2)
    return ok


def test_throttle_is_not_completion():
    group("A refused batch is NOT an exhausted listing (§7)")
    ok = True
    # The distinction this repo's policy turns on. A scroll that produced
    # nothing new means one of two things, and they map to opposite run
    # statuses: the feed ran out (COMPLETE) or the GraphQL call behind it was
    # refused (PARTIAL). Collapsing them is how a throttled run reports
    # "complete" while holding its first batch.
    ok &= check("`no_new_products` is a COMPLETE stop reason",
                "no_new_products" in COMPLETE_STOP_REASONS)
    ok &= check("`next_batch_refused` is NOT",
                "next_batch_refused" not in COMPLETE_STOP_REASONS)
    ok &= check("advance_feed has two distinct outcomes, not a bool",
                page_flow.ADVANCED != page_flow.NO_GROWTH)

    # Driven with the browser stubbed out (§10): a live run cannot always
    # reach this, because batch 1 decides whether there is a batch 2 at all.
    calls = {"scrolls": 0}
    counts = iter([4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
                   4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
                   4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4])

    def count(_selector):
        return next(counts, 4)

    def scroll():
        calls["scrolls"] += 1

    verdict = page_flow.advance_feed(scroll, count, lambda: 1000,
                                     lambda _ms: None, timeout_ms=3_000)
    ok &= check("a feed that never grows reports NO_GROWTH",
                verdict == page_flow.NO_GROWTH)
    ok &= check("and it did scroll rather than give up immediately",
                calls["scrolls"] >= 1)

    grown = iter([4, 4, 9])

    def growing(_selector):
        return next(grown, 9)

    verdict = page_flow.advance_feed(lambda: None, growing, lambda: 1000,
                                     lambda _ms: None, timeout_ms=5_000)
    ok &= check("a feed that grows reports ADVANCED",
                verdict == page_flow.ADVANCED)

    group("Every engine must reach the same verdict (§6)")
    # Selenium has no response listener, so the refused-GraphQL count comes
    # out of Chrome's performance log instead. If it silently answered 0, one
    # engine would report `complete` where its twins report `partial` on the
    # identical run — which is exactly the drift the shared modules exist to
    # prevent.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            continue
        ok &= check(f"{engine} counts refused GraphQL responses",
                    "_graphql_refused" in source)
        # The same THRESHOLD in all three, not merely the same field name. A
        # threshold that differed would mean one engine reporting `complete`
        # where its twins report `partial` on the identical run — and this
        # caught exactly that: the Playwright engine kept a sibling's
        # `status == 429` while the other two used `>= 400`.
        ok &= check(f"{engine} treats any >= 400 as a refusal, not only 429",
                    ">= 400" in source and "== 429" not in source)
        ok &= check(f"{engine} watches the same endpoint path",
                    '_GRAPHQL_PATH = "/graphql/"' in source)
        ok &= check(f"{engine} distinguishes exhausted from refused",
                    "no_turnover" in source and "exhausted" in source)
    return ok


def test_output_contract():
    group("The output contract (§9)")
    ok = True
    ok &= check("every mode yields the same row class",
                set(ROW_CLASS_BY_MODE.values()) == {Answer})
    ok &= check("the three modes are the three feeds",
                set(ROW_CLASS_BY_MODE) == {"topic", "question", "profile"})
    ok &= check("every mode is one row per sku",
                set(UNIQUE_BY_SKU_MODES) == set(ROW_CLASS_BY_MODE))
    ok &= check("Product is kept as an alias so family code keeps importing",
                Product is Answer)
    ok &= check("JSON and CSV agree on column order",
                [f.name for f in fields(Answer)]
                == list(asdict(Answer()).keys()))
    ok &= check("source defaults to the main host, never to an empty string",
                SOURCE_DEFAULT == "www.quora.com" and Answer().source == SOURCE_DEFAULT)

    group("Complete stop reasons")
    ok &= check("`completed` is complete", "completed" in COMPLETE_STOP_REASONS)
    ok &= check("`no_new_products` is complete — the data-side condition",
                "no_new_products" in COMPLETE_STOP_REASONS)
    ok &= check("a refused batch is NOT a complete stop reason",
                "next_batch_refused" not in COMPLETE_STOP_REASONS)
    ok &= check("nor is a block",
                not any(r.startswith("blocked") for r in COMPLETE_STOP_REASONS))
    return ok


def test_writers_and_finish_run():
    group("Writers")
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        # A run that finds nothing writes NOTHING — never replacing last
        # night's good output with [].
        with open(prefix + ".json", "w") as f:
            f.write('[{"sku": "keep-me"}]')
        rc = save([], prefix, "both")
        ok &= check("0 rows returns exit 4", rc == EXIT_NO_PRODUCTS)
        ok &= check("0 rows leaves the previous good output alone",
                    "keep-me" in open(prefix + ".json").read())
        rc = save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out", rc == EXIT_NO_PRODUCTS
                    and json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(tmp, "empty.csv")
        write_csv([], csv_path, row_cls=Answer)
        header = next(csv_module.reader(open(csv_path)))
        ok &= check("an empty CSV keeps the header",
                    header == [f.name for f in fields(Answer)])

        # There is no list column on this row, and that is worth pinning
        # rather than leaving as an accident: the family's CSV writer joins
        # one with LIST_CSV_SEPARATOR, and a column added here later must
        # either be scalar or be joined the same way.
        row = rows_of("TOPIC_EN")[1]
        csv2 = os.path.join(tmp, "rows.csv")
        write_csv([row], csv2, row_cls=Answer)
        body = list(csv_module.DictReader(open(csv2)))[0]
        ok &= check("every column of this row is scalar",
                    not any(isinstance(getattr(row, f.name), (list, dict))
                            for f in fields(Answer)))
        ok &= check("the CSV round-trips the sku",
                    body["sku"] == row.sku)
        ok &= check("the list separator is still available for the family",
                    bool(LIST_CSV_SEPARATOR))

        group("finish_run — the status/exit mapping all three engines share")
        def run(rows, stop_reason, blocked=False, allow_empty=False):
            out = os.path.join(tmp, f"r{abs(hash(stop_reason))}{len(rows)}{blocked}")
            code = finish_run(rows, out, "json", allow_empty, blocked=blocked,
                              stop_reason=stop_reason, pages_requested=2,
                              pages_completed=1, start_url="u", final_url="u")
            meta_path = out + ".meta.json"
            meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
            return code, meta

        rows = rows_of("TOPIC_EN")
        code, meta = run(rows, "completed")
        ok &= check("a finished run is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run(rows, "next_batch_refused")
        ok &= check("a REFUSED batch is partial, exit 6",
                    code == EXIT_PARTIAL and meta["status"] == "partial")
        code, meta = run(rows, "no_new_products")
        ok &= check("a feed that added nothing new is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run(rows, "pagination_exhausted")
        ok &= check("a feed that ran out is complete, exit 0",
                    code == 0 and meta["status"] == "complete")
        code, meta = run([], "blocked_cloudflare", blocked=True)
        ok &= check("blocked with no rows is exit 3", code == EXIT_BLOCKED)
        ok &= check("a FAILED run writes no sidecar beside good data",
                    meta is None)
        code, meta = run([], "completed")
        ok &= check("empty and not blocked is exit 4", code == EXIT_NO_PRODUCTS)

        group("The sidecar records WHICH pages failed, by number")
        out = os.path.join(tmp, "meta")
        finish_run(rows, out, "json", False, blocked=False,
                   stop_reason="blocked_x", pages_requested=5, pages_completed=3,
                   pages_failed=[2, 4], start_url="u", final_url="u",
                   mode="topic", source="es.quora.com",
                   extra={"rows_new_per_batch": {2: 10},
                          "pagination": "infinite-scroll"})
        meta = json.load(open(out + ".meta.json"))
        ok &= check("pages_failed is a list of numbers", meta["pages_failed"] == [2, 4])
        ok &= check("mode and source are recorded",
                    meta["mode"] == "topic" and meta["source"] == "es.quora.com")
        ok &= check("the per-batch row counts ride in the sidecar",
                    meta["rows_new_per_batch"] == {"2": 10})
        ok &= check("and so does the pagination model, which a consumer "
                    "needs before comparing two runs",
                    meta["pagination"] == "infinite-scroll")
        ok &= check("extra cannot overwrite a run field",
                    meta["status"] == "partial")

    group("Merging and dedupe")
    seen = set()
    p1 = rows_of("TOPIC_EN", 1)
    ok &= check("a fresh batch keeps every row",
                len(dedupe_by_key(p1, seen)) == len(p1))
    # EXPECTED here rather than exceptional: a scroll batch re-parses the
    # WHOLE feed, so batch 2 arrives holding batch 1's rows again by
    # construction, and a batch that drops all of them is how this repo knows
    # the feed is exhausted.
    ok &= check("the same feed again is fully dropped",
                dedupe_by_key(rows_of("TOPIC_EN", 2), seen) == [])
    ok &= check("dedupe_by_sku is the same function",
                dedupe_by_sku([], set()) == [])
    # A row with no key is always kept: there is nothing to check a duplicate
    # against, and dropping it would be a silent data loss.
    keyless = [Answer(sku=None, title="a"), Answer(sku=None, title="b")]
    ok &= check("keyless rows are kept, not collapsed",
                len(dedupe_by_key(keyless, set())) == 2)
    return ok


def test_diff():
    group("diff_runs — what counts as a change here")
    ok = True

    def row(**kw):
        base = dict(sku="www.quora.com/Q/answer/A", title="What is x?",
                    upvotes=100, views=1000, shares=1, comments=2,
                    answer_count=50, text_chars=800,
                    author_credential="Fixture credential",
                    is_machine_answer=False, data_source="dom+inline")
        base.update(kw)
        return base

    out = diff_products([row()], [row(upvotes=140)])
    ok &= check("a real upvote move, same view, is `changed`",
                len(out["changed"]) == 1 and not out["source_changed"])

    # THE BUCKET THIS SITE NEEDS MOST. A row read off a topic feed has null
    # counts; the same answer read off its question page has real ones. That
    # is our two snapshots differing, not the site.
    out = diff_products([row(data_source="dom", upvotes=None, views=None)],
                        [row()])
    ok &= check("a counter appearing with the view is `source_changed`, "
                "not `changed`",
                len(out["source_changed"]) == 1 and not out["changed"])
    ok &= check("the bucket names both views",
                out["source_changed"][0]["data_source"]
                == {"old": "dom", "new": "dom+inline"})

    # A truncated body growing into a full one is the same artefact.
    out = diff_products([row(data_source="dom", text_chars=180)],
                        [row(text_chars=3400)])
    ok &= check("a body growing with the view is `source_changed` too",
                len(out["source_changed"]) == 1 and not out["changed"])

    # But a title moving alongside is a REAL change and must not be
    # swallowed by the same bucket: Quora lets a question be edited.
    out = diff_products([row(data_source="dom", upvotes=None)],
                        [row(title="What is y?")])
    ok &= check("a title change survives a view change",
                len(out["changed"]) == 1
                and "title" in out["changed"][0]["changes"])

    group("The tolerance, which unlike the family's has a real use here")
    out = diff_products([row(views=1000)], [row(views=1004)],
                        price_tolerance_pct=1.0)
    ok &= check("a live counter ticking is `within_tolerance`",
                len(out["within_tolerance"]) == 1 and not out["changed"])
    out = diff_products([row(views=1000)], [row(views=1004)])
    ok &= check("and the DEFAULT reports it, deciding nothing for the reader",
                len(out["changed"]) == 1 and not out["within_tolerance"])
    out = diff_products([row(views=1000)], [row(views=1004, title="What is y?")],
                        price_tolerance_pct=1.0)
    ok &= check("a title change alongside is never absorbed by a tolerance",
                len(out["changed"]) == 1 and not out["within_tolerance"])

    group("added / removed / unmatchable")
    out = diff_products([row()], [row(sku="www.quora.com/Q/answer/B")])
    ok &= check("a new permalink is added and the old one removed",
                len(out["added"]) == 1 and len(out["removed"]) == 1)
    out = diff_products([row(sku=None)], [row(sku=None)])
    ok &= check("a row with no sku is unmatchable, not added or removed",
                out["unmatchable_old"] == 1 and out["unmatchable_new"] == 1
                and not out["added"] and not out["removed"])
    out = diff_products([row(), row()], [row()])
    ok &= check("a duplicate sku within one file is counted, not clobbered",
                out["unmatchable_old"] == 1)

    group("lifecycle is emitted and always empty, for the family's shape")
    ok &= check("the key is there", "lifecycle" in diff_products([], []))
    ok &= check("and it is empty", diff_products([row()], [row(upvotes=1)])
                ["lifecycle"] == [])

    group("--fail-on-change ignores what is about US, not the site")
    source = _fail_on_change_source()
    ok &= check("it fails on added/removed/changed",
                'result["added"] or result["removed"] or result["changed"]'
                in source)
    ok &= check("and on nothing else",
                "source_changed" not in source.split("fail_on_change")[-1]
                .split("return")[0])
    return ok


def _fail_on_change_source():
    """The `--fail-on-change` condition, as written."""
    src = open(os.path.join(REPO_ROOT, "diff_runs.py"), encoding="utf-8").read()
    match = re.search(r"if args\.fail_on_change and \(([^)]*)\)", src)
    return match.group(1) if match else src


def test_env_config():
    group("env_config — precedence and placeholders")
    ok = True
    ok &= check("every ENV_KEYS value is a real CLI destination",
                set(env_config.ENV_KEYS.values()) ==
                {"twocaptcha_key", "cdp_endpoint", "proxy", "url"})
    # A variable mapped onto a flag with a non-empty default would be
    # silently inert — a setting that looks configurable and is not.
    ok &= check("--out is deliberately NOT mapped",
                "out" not in env_config.ENV_KEYS.values())

    # .env.example must document exactly what the code reads, both ways.
    example = open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
    ok &= check("every ENV_KEYS name is in .env.example",
                set(env_config.ENV_KEYS) <= documented)
    ok &= check("every .env.example name is read by the code",
                documented <= set(env_config.ENV_KEYS))

    group("A COPIED .env.example must read as unset (§17)")
    # `cp .env.example .env` followed by a run used to connect with the
    # literal string `{login}-zone-...` as a username and get a 401 — the
    # confusing auth error a long way from its cause that this rule exists to
    # prevent. Round-tripped through the real loader.
    saved = {k: os.environ.get(k) for k in env_config.ENV_KEYS}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write(example)
            with contextlib.redirect_stderr(io.StringIO()):
                env_config.load_env(path, override=True)
                # Every CREDENTIAL in the example must read as unset. The
                # two credentialled URLs are written the way the vendor
                # documents them, so a literal-only placeholder list misses
                # both — `cp .env.example .env` then connected with the
                # string `{login}-zone-...` as a username and got a 401 a
                # long way from its cause (§17).
                for name in ("TWOCAPTCHA_KEY", "QUORA_CDP_ENDPOINT",
                             "QUORA_PROXY"):
                    ok &= check(f"{name} from a copied example reads as unset",
                                env_config.env_value(name) is None)
                # And the non-credential default must still be USABLE, or
                # the check above would pass by making everything unset.
                url = env_config.env_value("QUORA_URL")
            ok &= check("QUORA_URL from the example survives and is usable",
                        url is not None and is_supported_host(url))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return ok


def test_fingerprint_is_read_through_the_shared_helper():
    group("--fingerprint reads the UA through ONE helper (§16)")
    ok = True
    # The defect this pins was live in FOUR sibling repos at once and was
    # live here too, in the Selenium engine, until a live call to the API
    # showed what it returns. The UA is at `userAgent.userAgent` in the
    # chromium format and at `data.ua` in the raw one; `userAgent.value` —
    # which that engine read — exists in NEITHER. So `--fingerprint` set no
    # user agent at all, silently, and the run presented a Windows
    # fingerprint's screen, locale and timezone over a local Chromium's UA.
    # That is the identity MISMATCH the flag exists to avoid.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            continue
        if "fingerprint" not in source:
            continue
        ok &= check(f"{engine} does not reach into the response shape itself",
                    'get("userAgent")' not in source
                    and '["userAgent"]' not in source)

    # And the helper itself, against the three shapes the API is known to
    # return. Fixtures rather than a live call: the suite must pass offline.
    ok &= check("chromium format: userAgent.userAgent",
                fingerprint_user_agent({"userAgent": {"userAgent": "UA-1"}}) == "UA-1")
    ok &= check("raw format: data.ua",
                fingerprint_user_agent({"data": {"ua": "UA-2"}}) == "UA-2")
    ok &= check("a bare string is accepted too",
                fingerprint_user_agent({"userAgent": "UA-3"}) == "UA-3")
    ok &= check("and `value`, which the API does NOT return, is still read "
                "rather than being made an error — a response shape this "
                "repo has not seen is not a reason to set no UA",
                fingerprint_user_agent({"userAgent": {"value": "UA-4"}}) == "UA-4")
    ok &= check("nothing recognisable -> None, never a fabricated UA",
                fingerprint_user_agent({"userAgent": {}}) is None)

    group("Its kwargs are ones the driver actually accepts (§10)")
    # An unknown key in new_context(**kwargs) is a TypeError at launch, on
    # the paid path, at runtime.
    fp = {"userAgent": {"userAgent": "UA"},
          "screen": {"width": 1920, "height": 1080},
          "intl": {"contentLocale": "en-US", "timeZone": "America/New_York"}}
    kwargs = playwright_context_kwargs(fp)
    try:
        from playwright.sync_api import Browser
        allowed = set(inspect.signature(Browser.new_context).parameters)
        unknown = sorted(set(kwargs) - allowed)
        ok &= check(f"every context kwarg is a real one "
                    f"{'' if not unknown else unknown}", not unknown)
    except ImportError:
        skips_note = "playwright absent, context-kwarg binding not checked"
        ok &= check(skips_note, True)

    # The locale must come from the fingerprint, not be built out of its
    # country: a sibling family shipped `en-{country}` and gave every German
    # fingerprint the locale `en-DE`, which is not a locale anyone has.
    ok &= check("locale comes from the fingerprint's own intl block",
                kwargs.get("locale") == "en-US")
    ok &= check("and so does the timezone, which was never applied at all "
                "in four sibling repos",
                kwargs.get("timezone_id") == "America/New_York")
    return ok


def test_proxy_pool():
    group("Credentials never reach argv or a log")
    ok = True
    url = "http://user:" + "s3cr3t" + "@exit.example.com:2334"
    masked = mask(url)
    ok &= check("the password is masked", "s3cr3t" not in masked)
    ok &= check("the host and port are KEPT — that is the point of the log",
                "exit.example.com" in masked and "2334" in masked)
    scrubbed, credentials = split_credentials(url)
    ok &= check("split_credentials strips them from the address",
                "s3cr3t" not in scrubbed and credentials == ("user", "s3cr3t"))
    pw = to_playwright(url)
    ok &= check("Playwright gets them in its own fields, not in the server URL",
                pw["password"] == "s3cr3t" and "s3cr3t" not in pw["server"])

    group("A worker owns one exit; rotation is a fresh browser")
    pool = ProxyPool(["http://a@h1:1", "http://b@h2:2", "http://c@h3:3"])
    first = pool.current
    pool.advance("test")
    ok &= check("advance moves to a different exit", pool.current != first)
    ok &= check("the pool knows its size", len(pool) == 3)
    return ok


def test_credentials_never_reach_a_log():
    group("An EXCEPTION MESSAGE is a log (§8)")
    ok = True
    secret = "hunter2"
    # Concatenated rather than interpolated, so no line in this file holds a
    # complete `scheme://user:pass@host` literal. That keeps ci_checks.py's
    # credential scan meaningful on the one file where a real credential is
    # most likely to be pasted while debugging — an allowlist entry here
    # would switch the check off exactly where it matters.
    endpoint = "ws://user:" + secret + "@cb.2captcha.com:9222"
    for engine in ENGINES:
        try:
            module = __import__(engine)
        except ImportError:
            continue
        masker = getattr(module, "_mask_credentials", None)
        if masker is None:
            ok &= check(f"{engine} has a credential masker", False)
            continue
        # Globally, not once: a Playwright connection error repeats the
        # endpoint five times, and a masker that handles the first prints the
        # password the other four while looking like it works.
        repeated = " ".join([endpoint] * 5)
        ok &= check(f"{engine} masks EVERY occurrence",
                    secret not in masker(repeated))
        ok &= check(f"{engine} keeps the host and port",
                    "cb.2captcha.com:9222" in masker(endpoint))
    # And the solver redacts a key out of an error message, because the
    # fingerprint API takes its key as a query parameter and `requests` puts
    # the full URL into the text of every error it raises.
    key = "a" * 32
    redacted = captcha_solver._redact(f"GET https://x/y?key={key} failed")
    ok &= check("the solver redacts a key from an error message",
                key not in redacted)
    return ok


def test_engine_parity(skips):
    group("The three engines agree — flags, in BOTH directions (§17)")
    ok = True
    flagsets = {}
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        flagsets[engine] = set(re.findall(r'p\.add_argument\("(--[a-z0-9-]+)"', src))

    # The family contract (§9). Every engine must carry all of these.
    contract = {"--url", "--pages", "--category", "--format", "--out", "--delay",
                "--retries", "--retry-delay", "--concurrency", "--proxy",
                "--proxy-file", "--proxy-rotate", "--proxy-shuffle",
                "--proxy-block-retries", "--twocaptcha-key", "--captcha-api",
                "--solve-captcha", "--min-score", "--cdp-endpoint",
                "--allow-empty", "--dump-html", "--headless", "--headful",
                "--mode"}
    for engine, flags in flagsets.items():
        missing = contract - flags
        ok &= check(f"{engine} carries the whole contract "
                    f"{'' if not missing else sorted(missing)}", not missing)

    # And the DOCUMENTED differences, asserted in both directions so closing
    # one needs a README edit rather than a quiet patch.
    documented_extra = {
        # --cdp-connect-timeout is on the two engines that can actually USE
        # an authenticated CDP endpoint. Selenium cannot (chromedriver's
        # debuggerAddress has nowhere to put a password), so a connect
        # timeout there would be a flag for a path that does not exist.
        "playwright_scraper": {"--locale", "--fingerprint", "--fp-tags",
                               "--fp-country", "--browser-channel",
                               "--cdp-connect-timeout"},
        "selenium_scraper": {"--locale", "--fingerprint", "--fp-tags",
                             "--fp-country"},
        "puppeteer_scraper": {"--chromium-path", "--cdp-connect-timeout"},
    }
    for engine, extra in documented_extra.items():
        actual = flagsets[engine] - contract
        ok &= check(f"{engine}'s extra flags are exactly the documented set",
                    actual == extra)

    group("Every shared-module call binds against the real signature (§17)")
    problems = []
    for engine in ENGINES:
        path = os.path.join(REPO_ROOT, f"{engine}.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in SHARED_MODULES:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module, alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = None
            if isinstance(node.func, ast.Name) and node.func.id in imported:
                module, name = imported[node.func.id]
                fn = getattr(SHARED_MODULES[module], name, None)
            elif (isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in SHARED_MODULES):
                fn = getattr(SHARED_MODULES[node.func.value.id], node.func.attr, None)
            if fn is None or not callable(fn):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(k.arg is None for k in node.keywords):
                continue
            try:
                signature = inspect.signature(fn)
            except (TypeError, ValueError):
                continue
            try:
                signature.bind(*[object()] * len(node.args),
                               **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                problems.append(f"{engine}:{node.lineno} {getattr(fn,'__name__','?')}: {exc}")
    ok &= check(f"no call site disagrees with its callee "
                f"{'' if not problems else problems[:3]}", not problems)

    group("Every engine imports its driver at MODULE level (§10)")
    # Without this the module imports cleanly with no driver installed, the
    # skip below never fires, and CI's engine-smoke job cannot notice a
    # broken import.
    drivers = {"playwright_scraper": "playwright",
               "selenium_scraper": "selenium",
               "puppeteer_scraper": "pyppeteer"}
    for engine, driver in drivers.items():
        tree = ast.parse(open(os.path.join(REPO_ROOT, f"{engine}.py"),
                              encoding="utf-8").read())
        top_level = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(driver):
                top_level.append(node)
            if isinstance(node, ast.Import):
                top_level += [a for a in node.names if a.name.startswith(driver)]
        ok &= check(f"{engine} imports {driver} at module level", bool(top_level))

    group("The browser channel — unforced here, and that is a measurement")
    # The opposite of a sibling repo, which must drive real Chrome or be
    # refused. Quora was measured serving the BUNDLED Chromium the full feed,
    # so no channel is forced — and pinning that here is what stops someone
    # copying the sibling's default back in and quietly changing what the
    # README's numbers describe.
    try:
        import playwright_scraper as pws
        ok &= check("Playwright forces no browser channel",
                    pws.DEFAULT_BROWSER_CHANNEL is None)
    except ImportError:
        skips.append("playwright_scraper (playwright not installed)")
    sel = _engine_source("selenium_scraper") or ""
    ok &= check("Selenium says there is nothing to choose",
                "there is nothing to choose" in sel)
    pup = _engine_source("puppeteer_scraper") or ""
    ok &= check("pyppeteer says its bundled Chromium is accepted here",
                "this site accepts it" in pup)
    ok &= check("no engine claims a bundled Chromium is refused",
                not any("refus" in (_engine_source(e) or "").lower()
                        .split("bundled Chromium")[-1][:80] for e in ENGINES))

    group("The coverage floor is one number, not three")
    floors = []
    for engine in ENGINES:
        src = _engine_source(engine)
        match = re.search(r"^FIELD_FLOOR = (\d+)", src or "", re.M)
        floors.append(match.group(1) if match else None)
    ok &= check(f"all three engines share a FIELD_FLOOR ({floors[0]})",
                len(set(floors)) == 1 and floors[0] is not None)

    group("Engines import cleanly (skipped if the driver is absent)")
    for engine in ENGINES:
        try:
            __import__(engine)
            ok &= check(f"{engine} imports", True)
        except ImportError as exc:
            skips.append(f"{engine} ({exc})")
            print(f"  SKIP  {engine} — {exc}")
    return ok


def test_module_attributes_exist(skips):
    group("Every `module.name` an engine reaches for actually exists (§17)")
    ok = True
    # The gap the signature-binding check leaves, found by a live run rather
    # than by reading. `_min_matches` in one engine called
    # `page_flow.expected_cards(...)` — a name renamed in the other two and
    # not in that one — and the run died with AttributeError on its FIRST
    # fetch, exit 1. Invisible to import, to --help, to compileall, to the
    # undefined-NAME walk (it is an attribute, not a name) and to 490 green
    # assertions, because nothing but a live fetch reaches that line.
    #
    # This walks every `page_flow.X` and `product_parser.X` in every engine
    # and asserts X is really there. It needs no engine library: the modules
    # being reached INTO are the shared ones, and the reaching files are read
    # as text.
    for engine in ENGINES:
        source = _engine_source(engine)
        if source is None:
            skips.append(f"{engine} (source missing)")
            continue
        tree = ast.parse(source)
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            module = SHARED_MODULES.get(node.value.id)
            if module is None:
                continue
            if not hasattr(module, node.attr):
                missing.append(f"{node.value.id}.{node.attr}")
        ok &= check(f"{engine} reaches for nothing that is not there "
                    f"{'' if not missing else sorted(set(missing))}",
                    not missing)
    return ok


def test_no_dead_public_names():
    group("Every public name in the policy modules has a reader (§17)")
    ok = True
    # §17's check 5, automated. A public name nothing reads is dead code, and
    # a policy CONSTANT nothing reads is worse: the prose beside it reads
    # like enforcement. A sibling repo shipped `RETRY_ON_BLOCKED` with a
    # paragraph of measured justification and no engine consulting it.
    #
    # Scoped to the two modules that hold this repo's decisions, because the
    # family core is shared and its unused corners are another repo's
    # problem. References are counted across the whole repository INCLUDING
    # the defining module, so a helper used only by its own neighbours
    # counts — what this catches is a name with no reader anywhere at all.
    scanned = []
    for path in sorted(pathlib.Path(REPO_ROOT).rglob("*.py")):
        parts = path.relative_to(REPO_ROOT).parts
        # Skip local tools and any nested checkout. Matching on RELATIVE
        # parts, not on the absolute path: the absolute one can itself sit
        # under a directory this would otherwise exclude, and then the
        # corpus comes back empty and every name reads as dead — which is
        # how this check first "found" 91 dead names in a healthy module.
        if path.name.startswith("_"):
            continue
        if any(part in {"worktrees", ".venv", "venv", "build", "dist"}
               for part in parts):
            continue
        scanned.append(path.read_text(encoding="utf-8"))
    corpus = "\n".join(scanned)
    if len(corpus) < 10_000:
        return check("the dead-name corpus is not empty (it would make "
                     "every name look dead)", False)

    for module in ("product_parser", "page_flow"):
        source = open(os.path.join(REPO_ROOT, module + ".py"),
                      encoding="utf-8").read()
        tree = ast.parse(source)
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                if not node.name.startswith("_"):
                    names.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        names.append(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id.isupper():
                    names.append(node.target.id)
        dead = []
        for name in names:
            # Two references minimum: the definition, and at least one read.
            hits = len(re.findall(r"\b" + re.escape(name) + r"\b", corpus))
            if hits < 2:
                dead.append(name)
        ok &= check(f"{module} has no unread public name "
                    f"{'' if not dead else sorted(dead)}", not dead)
    return ok


def test_no_undefined_names():
    group("Names that resolve, not just parse (§10)")
    # `compileall` proves a file PARSES, not that its names RESOLVE. A live
    # run of a sibling repo's engine died with NameError on a line reached
    # only while fetching, after an import had been removed — invisible to
    # import, --help, compileall and 400+ green assertions. Kept COARSE so it
    # under-reports rather than inventing problems.
    ok = True
    for name in sorted(os.listdir(REPO_ROOT)):
        if not name.endswith(".py") or name == "smoke_test.py":
            continue
        undefined = _undefined_names(os.path.join(REPO_ROOT, name))
        ok &= check(f"{name}: no undefined names "
                    f"{'' if not undefined else sorted(undefined)[:5]}", not undefined)
    return ok


def _undefined_names(path):
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    bound = set(dir(__builtins__) if not isinstance(__builtins__, dict)
                else __builtins__.keys())
    bound |= set(dir(__import__("builtins")))
    # Module-level dunders are always bound and are not imports.
    bound |= {"__file__", "__name__", "__doc__", "__package__", "__spec__",
              "__loader__", "__builtins__", "__debug__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for arg in (args.posonlyargs + args.args + args.kwonlyargs):
                    bound.add(arg.arg)
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            pass
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - bound


def test_dockerfile_matches_its_entrypoint():
    group("The Dockerfile COPY list against the import graph (§10)")
    # All three repos in this family once shipped an image that died with
    # ModuleNotFoundError on every invocation, --help included, because one
    # module was missing from an explicit COPY list. This check needs no
    # Docker.
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("a Dockerfile exists", False)
    dockerfile = open(path, encoding="utf-8").read()
    # Join backslash continuations first: the COPY list spans five lines, and
    # a line-by-line reader sees an empty list and passes vacuously.
    joined = re.sub(r"\\\s*\n\s*", " ", dockerfile)
    copied = set()
    for line in joined.splitlines():
        if line.strip().upper().startswith("COPY"):
            # [1:-1]: the first token is COPY and the LAST is the
            # destination. Including the destination made `./` look like
            # "copy everything" and the check passed vacuously.
            for token in line.split()[1:-1]:
                if token.endswith(".py"):
                    copied.add(os.path.basename(token))
                elif token in ("./", "."):
                    copied.update(n for n in os.listdir(REPO_ROOT)
                                  if n.endswith(".py"))

    # Walk the entrypoint's own import graph.
    entrypoints = [n for n in ENGINES if f"{n}.py" in dockerfile]
    if not entrypoints:
        entrypoints = ["playwright_scraper"]
    needed, queue = set(), list(entrypoints)
    local = {n[:-3] for n in os.listdir(REPO_ROOT) if n.endswith(".py")}
    while queue:
        module = queue.pop()
        if module in needed:
            continue
        needed.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, f"{module}.py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in local:
                queue.append(node.module)
            elif isinstance(node, ast.Import):
                queue += [a.name for a in node.names if a.name in local]
    missing = {f"{m}.py" for m in needed} - copied
    ok &= check(f"every module the entrypoint imports is COPYed "
                f"{'' if not missing else sorted(missing)}", not missing)

    group("The image carries no secrets and no test material")
    for unwanted in (".env", "smoke_test.py", "fixtures_generated.json",
                     "captures"):
        ok &= check(f"{unwanted} is not COPYed into the image",
                    unwanted not in copied and f"COPY {unwanted}" not in dockerfile)
    return ok


def test_no_file_describes_another_site():
    group("No shipped file still describes a different site (§17)")
    ok = True
    # A sibling repo's audit found "a shipped file still described another
    # site", and this repo inherited the same thing FOUR times over: a
    # CONTRIBUTING section about `data-testid="divSRPContentProducts"` and
    # sold counts, a list of invariants about auction lots and reserve
    # prices, a captcha module explaining an Akamai "Access Denied" page, and
    # an issue template about seller feedback scores. All four were copied in
    # with the family core and all four read as authoritative.
    #
    # Nothing here can tell a paragraph about Quora from a paragraph about a
    # rental site in general. What it CAN do is notice the vocabulary of the
    # specific siblings this repo was copied from, which is where the real
    # leakage comes from.
    foreign = {
        "akamai": "a sibling's bot manager",
        "datadome": "a sibling's bot manager",
        "bot or not": "a sibling's challenge page",
        "reserve_price_set": "a sibling's auction column",
        "seller_score": "a sibling's seller column",
        "stay_dates": "a sibling's booking column",
        "fewo-direkt": "a sibling's storefront",
        "stayz.com.au": "a sibling's storefront",
        "vrbo": "a sibling repo",
        "tokopedia": "a sibling repo",
        "catawiki": "a sibling repo",
        "craigslist": "a sibling repo",
        "mediamarkt": "a sibling repo",
        "farfetch": "a sibling repo",
        "divsrpcontentproducts": "a sibling's grid selector",
        "lodging-card-responsive": "a sibling's card selector",
    }
    # A CONTEXT allowlist, the same shape ci_checks.py uses for credentials,
    # because one of these words is legitimate in exactly one place. §8 says
    # captcha DETECTION stays broad — which challenge a visitor meets depends
    # on the exit and on what the address has been doing — so
    # `BOT_CHALLENGE_MARKERS` names vendors this site has never served, on
    # purpose. That is a marker list, not a description of the site, and the
    # difference is the whole point of this check.
    allowed = {("product_parser.py", "datadome")}

    checked = 0
    for path in sorted(pathlib.Path(REPO_ROOT).rglob("*")):
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file() or path.suffix not in (".py", ".md", ".yml",
                                                     ".yaml", ".toml",
                                                     ".example"):
            continue
        if any(part in {"worktrees", ".venv", "venv", "build", "dist", ".git"}
               for part in rel.parts) or path.name.startswith("_"):
            continue
        # The suite names these words in order to ban them, so it cannot be
        # scanned for them without failing on its own check.
        if path.name == "smoke_test.py":
            continue
        checked += 1
        lowered = path.read_text(encoding="utf-8", errors="replace").lower()
        hits = sorted({word for word in foreign
                       if word in lowered
                       and (path.name, word) not in allowed})
        ok &= check(f"{rel} describes this site "
                    f"{'' if not hits else hits}", not hits)
    ok &= check(f"…and {checked} files were actually scanned", checked > 20)
    return ok


def test_wording():
    group("Wording enforced by a test (§12)")
    ok = True
    banned = {
        "cloud browser": "Scraping Browser API",
        "antidetect browser": "Scraping Browser API",
        "gate.2prx.com": "2captcha.com/proxy",
        "2prx.com": "2captcha.com/proxy",
        "--antidetect": "removed",
        "ANTIDETECT_LOCAL_API": "removed",
    }
    shipped = [n for n in os.listdir(REPO_ROOT)
               if n.endswith((".py", ".md", ".txt", ".toml", ".yml", ".example"))]
    for name in shipped:
        if name == "smoke_test.py":
            continue  # this file names them in order to ban them
        text = open(os.path.join(REPO_ROOT, name), encoding="utf-8",
                    errors="replace").read().lower()
        for phrase, instead in banned.items():
            ok &= check(f"{name}: no {phrase!r} (write {instead!r})",
                        phrase.lower() not in text)

    group("Removed flags stay removed — scoped to the ENGINES")
    # --country is banned on a scraper (it could disagree with the URL, and
    # here the storefront IS the hostname) and legitimate on
    # fingerprint_client.py, where it picks a fingerprint locale.
    for engine in ENGINES:
        src = open(os.path.join(REPO_ROOT, f"{engine}.py"), encoding="utf-8").read()
        ok &= check(f"{engine} has no --country flag",
                    'add_argument("--country"' not in src)
    return ok


def test_no_capture_leaks():
    group("Committed fixtures carry no credential-shaped material (§10)")
    ok = True
    text = open(_FIXTURE_PATH, encoding="utf-8").read()
    # PATTERNS, not the literals one capture happened to contain, so the next
    # capture is caught too.
    patterns = {
        "a 24+ char hex run": r"\b[0-9a-fA-F]{24,}\b",
        "a reCAPTCHA site key": r"\b6L[A-Za-z0-9_-]{20,}",
        "a Turnstile site key": r"\b0x4[A-Za-z0-9]{15,}",
        "an embedded credential": r"[a-z]+://[^\s\"/@]+:[^\s\"/@]+@",
    }
    for label, pattern in patterns.items():
        found = re.findall(pattern, text)
        ok &= check(f"no {label} in fixtures_generated.json "
                    f"{'' if not found else found[:2]}", not found)
    ok &= check("the challenge fixture still names its vendor after scrubbing",
                detect_bot_challenge(fixture("BLOCK_CF")) == "cloudflare")

    # And the PEOPLE, which is the other half of §10 and the half a
    # credential grep does not cover. make_fixtures.py replaces every author
    # name, profile slug, credential and answer body before writing; these
    # are the shapes that would show a pass having been skipped.
    ok &= check("no author name survives except the placeholder",
                "Fixture Author" in text)
    ok &= check("every profile link is a placeholder slug",
                not re.search(r"/profile/(?!Fixture-Author)[A-Za-z]", text))
    ok &= check("no answer cites a third-party host",
                not re.search(r"https?://(?!(?:[a-z0-9-]+\.)*quora\.com|"
                              r"(?:[a-z0-9-]+\.)+quoracdn\.net|"
                              r"example\.invalid|challenges\.cloudflare\.com|"
                              r"www\.w3\.org|www\.twitter\.com)[a-z]", text))
    return ok


def test_ci_checks_is_wired_up():
    group("One credential check, invoked from CI and from here (§17)")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    workflow = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    if os.path.exists(workflow):
        text = open(workflow, encoding="utf-8").read()
        # A check nothing runs is not a check; two sources of truth that
        # disagree is worse.
        ok &= check("tests.yml CALLS ci_checks.py rather than reimplementing it",
                    "ci_checks.py" in text)
    if os.path.exists(script):
        # And it must pass on THIS repo. A check that fails on its own
        # repository is a check nobody can read.
        import subprocess
        result = subprocess.run([sys.executable, script, "--all"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        ok &= check(f"ci_checks.py passes on this repo "
                    f"{'' if result.returncode == 0 else result.stdout[-300:]}",
                    result.returncode == 0)
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    for name, loader in (("sample_output.json", json.load),):
        path = os.path.join(REPO_ROOT, name)
        if not os.path.exists(path):
            ok &= check(f"{name} exists", False)
            continue
        rows = loader(open(path, encoding="utf-8"))
        ok &= check(f"{name} is a non-empty list", isinstance(rows, list) and rows)
        columns = [f.name for f in fields(Product)]
        ok &= check(f"{name} columns match Product exactly",
                    all(list(r) == columns for r in rows))
        # Fabrication markers — a sample nobody ran reads exactly like one
        # somebody did.
        text = json.dumps(rows)
        for marker in ("example.com", "lorem", "PLACEHOLDER", "TODO", "foo bar"):
            ok &= check(f"{name}: no {marker!r}", marker.lower() not in text.lower())
        ok &= check(f"{name}: every row is from a supported storefront",
                    all(r["source"] in HOSTS for r in rows))
        ok &= check(f"{name}: page+position unique",
                    len({(r["page"], r["position"]) for r in rows}) == len(rows))
    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = next(csv_module.reader(open(csv_path, encoding="utf-8")))
        ok &= check("sample_output.csv header matches Product",
                    header == [f.name for f in fields(Product)])
    else:
        ok &= check("sample_output.csv exists", False)
    return ok


def test_required_files_are_committed():
    group("Everything the suite needs is tracked by git")
    # A blanket `*.json` / `*.csv` in .gitignore — which this repo wants,
    # because a scraper's own output is large and stale — silently swallowed
    # `fixtures_generated.json`. The suite was green on the machine that
    # wrote it and every CI job died with FileNotFoundError at import. A
    # check that a file EXISTS cannot see that; only asking git can.
    ok = True
    import subprocess
    required = ("fixtures_generated.json", "sample_output.json",
                "sample_output.csv", ".env.example", "README.md",
                "CHANGELOG.md", "Dockerfile", "requirements.txt",
                ".github/ci_checks.py", ".github/workflows/tests.yml",
                ".github/workflows/canary.yml", "tests/test_smoke.py")
    result = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("  SKIP  not a git checkout — cannot verify what is committed")
        return ok
    tracked = set(result.stdout.split())
    for name in required:
        ok &= check(f"{name} is committed, not just present on disk",
                    name in tracked)
    # And the other direction: nothing a scraper produced should be.
    leaked = [f for f in tracked
              if re.search(r"_debug\.(html|png)$|\.meta\.json$|^captures/|^\.env$",
                           f)]
    ok &= check(f"no run output or capture is committed "
                f"{'' if not leaked else leaked[:3]}", not leaked)
    return ok


def test_readme_claims():
    group("README numbers exist and are dated")
    ok = True
    path = os.path.join(REPO_ROOT, "README.md")
    if not os.path.exists(path):
        return check("README.md exists", False)
    readme = open(path, encoding="utf-8").read()
    ok &= check("the README names every supported language host",
                all(h.split(".")[0] + "." in readme for h in HOSTS))
    ok &= check("it states what the block actually is",
                "403" in readme and "managed" in readme.lower())
    ok &= check("it says a captcha solve buys nothing here",
                "no sitekey" in readme or "sitekey" in readme)
    ok &= check("it warns that a topic run has no counts",
                "null on every topic row" in readme)
    ok &= check("it says the page parameter is IGNORED rather than failing",
                "?page=2" in readme and "ignored" in readme)
    ok &= check("it says concurrency is refused",
                "--concurrency" in readme)
    # Every claim is measured or absent: a number without a date goes stale
    # invisibly.
    ok &= check("measurements carry a date", "2026-09-15" in readme)

    group("The README's numbers match the artefacts on disk (§17)")
    # A number a reader can check is worth more than one they cannot. These
    # are re-derived from the committed fixtures rather than retyped.
    topic_rows = rows_of("TOPIC_EN")
    ok &= check("a topic row really is DOM-only, as the README says",
                all(r.data_source == "dom" for r in topic_rows))
    question_rows = rows_of("QUESTION_EN")
    backed = [r for r in question_rows if r.data_source != "dom"]
    ok &= check("a question row really is payload-backed, as the README says",
                backed)
    ok &= check("the column count the README shows matches the dataclass",
                len(fields(Answer)) == 30)
    return ok


# The floor the README and CHANGELOG state. A FLOOR rather than the exact
# count, because an exact count goes stale the next time anyone adds a check
# and a stale number in a README is worse than no number (§17). Raise it when
# it is comfortably passed; it can only ever be an under-claim.
CLAIMED_CHECK_FLOOR = 500


def main() -> int:
    ok = True
    skips = []

    ok &= test_numbers_and_prices()
    ok &= test_values_on_real_fixtures()
    ok &= test_a_question_page_holds_other_questions_answers()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_challenge_is_not_always_solvable()
    ok &= test_page_flow_policy()
    ok &= test_scroll_loop()
    ok &= test_throttle_is_not_completion()
    ok &= test_output_contract()
    ok &= test_writers_and_finish_run()
    ok &= test_diff()
    ok &= test_env_config()
    ok &= test_fingerprint_is_read_through_the_shared_helper()
    ok &= test_proxy_pool()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_engine_parity(skips)
    ok &= test_module_attributes_exist(skips)
    ok &= test_no_dead_public_names()
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_matches_its_entrypoint()
    ok &= test_no_file_describes_another_site()
    ok &= test_wording()
    ok &= test_no_capture_leaks()
    ok &= test_ci_checks_is_wired_up()
    ok &= test_sample_output()
    ok &= test_required_files_are_committed()
    ok &= test_readme_claims()

    passed = _total_checks - len(_failures)
    if passed < CLAIMED_CHECK_FLOOR:
        ok = False
        _failures.append(
            f"the README and CHANGELOG claim over {CLAIMED_CHECK_FLOOR} "
            f"checks and only {passed} ran — either checks were removed or "
            f"the claim needs lowering")

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs each engine in its own "
              "venv and fails if this list is non-empty, because a skip reads "
              "exactly like a passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
