"""
product_parser.py
-----------------
Everything this repo knows about Quora. The engines, the writers, the proxy
pool and the solver are family code and carry no site knowledge; this file is
the site.

Family naming, kept on purpose
------------------------------
Every repo in this family calls this module `product_parser.py` and its row
`Product`. Quora sells nothing, so the row here is an `Answer` (§9's
precedent: a sibling added a second dataclass for reviews rather than
pretending a review was a product). The MODULE name is kept anyway, because
`page_flow.py` and `scraper_api_client.py` import it by that name in six
repos and a fix landed in one of them should apply here verbatim (§16).

What a row is
-------------
One answer. Quora publishes questions, answers, topics, spaces and profiles,
but only the answer is a leaf: a question is a container for answers, a topic
and a profile are both feeds OF answers, and a space is a feed of answers on
a subdomain. So three modes read three different feeds and all three yield
the same kind of row:

    --mode topic      /topic/{Slug}           a topic's answer feed
    --mode question   /{Question-Slug}        one question's answers
    --mode profile    /profile/{Slug}         one author's answers

Two sources, in this order
--------------------------
1. **The inline GraphQL payloads (primary).** Quora server-renders NO
   content DOM at all — measured 0 occurrences of its own `q-box` class in
   the raw body of six captures, against 385-1165 in the hydrated document.
   What the server DOES send is its own query results, pushed into

       window.ansFrontendGlobals.data.inlineQueryResults.results["<hash>"]
           .push("{\\"data\\":{…}}")

   as JSON string literals. That payload is this site's structured data, and
   it is richer than anything rendered: the numeric answer and question ids,
   the FULL answer text (the DOM truncates a feed card to three lines),
   `numUpvotes`, `numViews`, `numShares`, an epoch-microsecond
   `creationTime`, and Quora's own `isMachineAnswer` flag. None of those six
   is in the DOM for an anonymous reader.

   There is no JSON-LD anywhere: measured 0 `application/ld+json` blocks on
   topic, question, profile, answer-permalink and language pages alike. §4's
   primary path therefore falls to the site's own data, exactly as it did on
   a sibling site with no structured data.

2. **The rendered cards (fallback, and the only universal one).** The inline
   payload covers a question page (5 answers of 12 rendered) and a profile
   (3 of 18), and covers a TOPIC FEED NOT AT ALL — a topic's answers arrive
   over a later `TopicReadMultifeedLoggedOut_Query` XHR, so 0 Answer objects
   are in that page's payloads. The DOM is what every mode has, so it is the
   skeleton and the payload is the overlay.

`data_source` on every row records which of the two built it (§4: provenance
of a value goes in a column, never in a comment), and `parse_answers` logs
the overlay's coverage so a run whose payload share collapses is visible
rather than silently thinner.

Anchoring on a class, which §4 normally forbids
-----------------------------------------------
§4 says anchor on a URL pattern and never on a CSS class, because classes are
build-generated hashes. Quora is the case that rule does not fit, in both
directions:

* Its content classes are NOT hashes. `puppeteer_test_question_title`,
  `spacing_log_answer_content` and `answer_timestamp` are the site's own test
  and logging hooks, stable across every capture and every language host.
  (Its LAYOUT classes — `c1nud10e`, `b2c1r2a` — are hashes, and nothing here
  touches one.)
* Its URLs genuinely cannot identify an answer. Measured over 81 rendered
  answer links: 77 are `/{Question-Slug}/answer/{Author-Slug}`, and 4 are a
  bare `https://{space}.quora.com/{Question-Slug}` — which is the same shape
  as a QUESTION url on www. A URL-pattern anchor would either miss every
  answer published in a Space, or treat every question link as an answer.

So the anchor is `a.answer_timestamp` — the permalink the site itself prints
under each card — and the URL pattern is kept as a validator and as the
source of the question URL.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

from bs4 import BeautifulSoup

from output_writer import Answer

log = logging.getLogger(__name__)


# ===========================================================================
# Hosts
# ===========================================================================
# Taken from Quora's own language switcher at /about/languages (§5: the
# site's own set, not a guess), captured 2026-09-15. Twenty-three language
# hosts plus www.
#
# Two things about this list are traps:
#
#   * Japanese is on `jp.quora.com`, not `ja.` — the host is the COUNTRY
#     code where every other entry is the language code. Deriving the host
#     from an ISO 639-1 code gets Japanese wrong and nothing else.
#   * `quora.com` without `www.` redirects to `www.`, and `m.quora.com` is
#     not a host Quora publishes. Both are accepted on input and normalised;
#     see `normalize_url`.
LANGUAGE_HOSTS: Dict[str, str] = {
    "www.quora.com": "en",
    "es.quora.com": "es",
    "fr.quora.com": "fr",
    "de.quora.com": "de",
    "it.quora.com": "it",
    "jp.quora.com": "ja",      # not ja.quora.com — see above
    "id.quora.com": "id",
    "pt.quora.com": "pt",
    "hi.quora.com": "hi",
    "nl.quora.com": "nl",
    "da.quora.com": "da",
    "fi.quora.com": "fi",
    "no.quora.com": "no",
    "sv.quora.com": "sv",
    "mr.quora.com": "mr",
    "bn.quora.com": "bn",
    "ta.quora.com": "ta",
    "ar.quora.com": "ar",
    "he.quora.com": "he",
    "gu.quora.com": "gu",
    "kn.quora.com": "kn",
    "ml.quora.com": "ml",
    "te.quora.com": "te",
    "pl.quora.com": "pl",
}

HOSTS: Tuple[str, ...] = tuple(LANGUAGE_HOSTS)

# Hosts that are on quora.com and are NOT a language site. Each is refused
# WITH ITS REASON (§5: "is not a Quora site" is false and sends the reader
# hunting for a typo).
NON_CONTENT_HOSTS: Dict[str, str] = {
    "help.quora.com": "is Quora's help centre, which publishes articles "
                      "rather than answers",
    "log.quora.com": "is Quora's logging endpoint and serves no pages",
    "contests.quora.com": "is Quora's programming-contest site",
    "quorablog.quora.com": "is Quora's own blog",
    "productupdates.quora.com": "is Quora's product-update blog",
    "poe.com": "is Poe, a separate Quora product with its own API",
}

SOURCE_DEFAULT = "www.quora.com"


def site_host(url: str) -> Optional[str]:
    """The lowercased host, or None if the URL is not on quora.com."""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return None
    if host == "quora.com":
        return "www.quora.com"
    if host.endswith(".quora.com") or host == "poe.com":
        return host
    return None


def is_space_host(host: Optional[str]) -> bool:
    """A Space or a Quora Session lives on its own `{name}.quora.com`.

    Measured: four of a profile's eighteen answer permalinks were on
    `quorasessionwithyannlecun.quora.com`, a Session subdomain that is not in
    the language list and is not one of the known non-content hosts. There
    are thousands of these and no published list, so they are recognised by
    exclusion rather than by an allowlist.
    """
    if not host or not host.endswith(".quora.com"):
        return False
    return host not in LANGUAGE_HOSTS and host not in NON_CONTENT_HOSTS


def unsupported_reason(url: str) -> Optional[str]:
    """Why this URL cannot be scraped, in a sentence, or None if it can."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return f"{url!r} is not an http(s) URL"
    host = site_host(url)
    if host is None:
        return (f"{parsed.hostname or url!r} is not a Quora host — this "
                f"scraper reads {', '.join(sorted(HOSTS)[:3])} and the other "
                f"{len(HOSTS) - 3} language sites")
    if host in NON_CONTENT_HOSTS:
        return f"{host} {NON_CONTENT_HOSTS[host]}"
    if not is_space_host(host) and host not in LANGUAGE_HOSTS:
        return f"{host} is not one of Quora's {len(HOSTS)} language sites"
    kind = listing_kind(url)
    if kind == "search":
        return ("Quora's search results are behind its sign-in wall — an "
                "anonymous /search request is answered with the language "
                "home page, not results. Pass a /topic/, /profile/ or "
                "question URL instead")
    if kind == "unknown":
        return (f"{parsed.path or '/'} is not a topic, question or profile "
                f"path")
    return None


def is_supported_host(url: str) -> bool:
    return unsupported_reason(url) is None


def source_of(url: str) -> str:
    """The host a row came from, for the `source` column."""
    return site_host(url) or SOURCE_DEFAULT


def language_of(url: str) -> Optional[str]:
    """The site language, from the host. None for a Space subdomain.

    A Space is not a language site: it inherits the language of whoever
    writes in it, and guessing `en` from the absence of a prefix would put a
    guess in a data column (§8).
    """
    return LANGUAGE_HOSTS.get(site_host(url) or "")


# ===========================================================================
# URL shapes
# ===========================================================================
# Quora's first path segment is either a reserved route or a question slug.
# There is no marker distinguishing the two, so the reserved set has to be
# written down: `/about`, `/careers` and friends would otherwise parse as
# questions titled "about" and "careers".
RESERVED_FIRST_SEGMENTS = frozenset("""
about answer ask business careers contact contests developers followers
graphql help log login logout messages notifications partners press profile
q search settings sitemap spaces stats topic unanswered
""".split())

_TOPIC_RE = re.compile(r"^/topic/(?P<slug>[^/?#]+)(?:/(?P<tab>[a-z_]+))?/?$")
_PROFILE_RE = re.compile(r"^/profile/(?P<slug>[^/?#]+)/?$")
_ANSWER_RE = re.compile(r"^/(?P<question>[^/?#]+)/answers?/(?P<answer>[^/?#]+)/?$")
_SEARCH_RE = re.compile(r"^/search/?$")
_QUESTION_RE = re.compile(r"^/(?P<slug>[^/?#]+)/?$")

# Query parameters Quora appends for its own analytics. Stripped before a URL
# is written to a row, so the same answer does not get a different `url` on
# every run and every diff report it as changed.
TRACKING_PARAMS = frozenset("""
ch oid share srid target_type ti __nsrc__ __sncid__ comment_id comment_type
""".split())


def strip_tracking(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k not in TRACKING_PARAMS]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def normalize_url(url: str) -> str:
    """The URL this scraper will actually fetch.

    Three normalisations, each of which a user will otherwise hit:

    * `quora.com/…` -> `www.quora.com/…`. The bare host 301s, and a rebuilt
      URL that never matches the page's own breaks the join between a row and
      its source (§5, where two of a sibling site's ten hosts answered
      without `www.` and quietly emptied the DOM-only columns).
    * An ANSWER permalink -> its question page. `/Q/answer/Someone` holds one
      answer; `/Q` holds all of them and is what the user almost certainly
      wants. Reported by the engines rather than done silently.
    * Tracking parameters removed.
    """
    parsed = urlparse(url or "")
    host = site_host(url)
    if host and parsed.hostname and parsed.hostname.lower() != host:
        parsed = parsed._replace(netloc=host)
    url = strip_tracking(urlunparse(parsed))
    m = _ANSWER_RE.match(urlparse(url).path)
    if m:
        parsed = urlparse(url)
        url = urlunparse(parsed._replace(path="/" + m.group("question"),
                                         query=""))
    return url


def listing_kind(url: str) -> str:
    """Which feed this URL is: topic, question, profile, search or unknown.

    A Space subdomain's root (`https://{space}.quora.com/`) is a `topic`:
    the feed markup is identical and the only difference is which answers it
    holds.
    """
    parsed = urlparse(url or "")
    path = parsed.path or "/"
    host = site_host(url)
    if _SEARCH_RE.match(path):
        return "search"
    if _TOPIC_RE.match(path):
        return "topic"
    if _PROFILE_RE.match(path):
        return "profile"
    if _ANSWER_RE.match(path):
        return "question"          # normalized to its question before fetch
    if path in ("", "/"):
        return "topic" if is_space_host(host) else "unknown"
    m = _QUESTION_RE.match(path)
    if m and m.group("slug").split("?")[0] not in RESERVED_FIRST_SEGMENTS:
        return "question"
    return "unknown"


def category_from_url(url: str) -> Optional[str]:
    """The `category` column: the topic, the profile or the question slug.

    Whatever the run was pointed at, spelled the way the site spells it.
    """
    parsed = urlparse(url or "")
    path = parsed.path or "/"
    for pattern, group in ((_TOPIC_RE, "slug"), (_PROFILE_RE, "slug")):
        m = pattern.match(path)
        if m:
            return m.group(group)
    if path in ("", "/") and is_space_host(site_host(url)):
        return (site_host(url) or "").split(".")[0]
    m = _ANSWER_RE.match(path) or _QUESTION_RE.match(path)
    if m:
        slug = m.groupdict().get("question") or m.groupdict().get("slug")
        if slug and slug not in RESERVED_FIRST_SEGMENTS:
            return slug
    return None


def _absolute(base_url: str, href: str) -> str:
    if not href:
        return ""
    return urljoin(base_url or "https://www.quora.com/", href)


def permalink_key(url: str) -> Optional[str]:
    """The `sku`: an answer's permalink with the scheme removed.

    `www.quora.com/What-is-machine-learning-4/answer/Naushin-Ara-6`.

    NOT the numeric `aid`, even though the inline payload carries one and a
    number would be the tidier key. The topic feed — a whole mode — publishes
    no numeric id anywhere: 0 occurrences of `aid`, `/answers/{n}` or any
    logging id across two topic captures in two languages, because a topic's
    answers arrive as rendered DOM only. A key that is null for a third of
    the modes cannot be the family's join column, and `diff_runs.py` joins on
    `sku`.

    The host is part of the key rather than dropped, because four of a
    profile's eighteen permalinks were on a Session subdomain whose paths
    live in their own namespace. It makes `sku` a prefix of `url`, which is
    redundancy accepted on purpose: an unambiguous diff key is worth more
    than a shorter column.
    """
    parsed = urlparse(strip_tracking(url or ""))
    if not parsed.netloc:
        return None
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.netloc.lower()}{path}"


def _question_path(url: str) -> Optional[str]:
    """The question part of a URL's path, for comparing two of them."""
    question = question_url_of(url)
    if not question:
        return None
    parsed = urlparse(question)
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def question_url_of(answer_url: str) -> Optional[str]:
    """The question an answer belongs to, from its permalink.

    Exact for the ordinary `/{Question}/answer/{Author}` shape, and an
    APPROXIMATION for the Space/Session one, which is why the payload's own
    `question.url` overwrites it whenever the payload reached the row.

    The measurement, from one real profile capture:

        permaUrl      …quora.com/How-important-is-…-in-Machine-Learning-1
        question.url  …quora.com/How-important-is-…-in-Machine-Learning
        question.slug How-important-is-…-in-Machine-Learning-5

    Three values, all different. A Session answer's permalink is the
    question's path with a per-answer suffix, so stripping nothing leaves a
    URL that is one character off the question's — and `slug` is a third
    thing again and is not the path. Without the payload there is nothing in
    the URL that says how much to strip, and guessing would be worse than
    returning the permalink: a wrong question URL reads as a real one.
    """
    if not answer_url:
        return None
    parsed = urlparse(answer_url)
    m = _ANSWER_RE.match(parsed.path)
    if m:
        return urlunparse(parsed._replace(path="/" + m.group("question"),
                                          query="", fragment=""))
    return urlunparse(parsed._replace(query="", fragment=""))


# ===========================================================================
# Pagination
# ===========================================================================
# Quora has NO addressable pages, in any mode. Every feed here is an infinite
# scroll: a topic, a profile and a question page all extend themselves with a
# GraphQL fetch when the reader nears the bottom, and none of them accepts a
# page parameter. `?page=2` on a question URL is not an error — it is
# ignored, and the page returns its first answers again, which is the failure
# mode §18 warns about: a constructed URL that "works" and silently re-reads
# page one would let a run report `complete` while holding a third of the
# data.
#
# So `page_url` returns None with a reason, a "page" is one settled scroll
# batch, and the terminating condition is the DATA one §7 asks for: a batch
# that adds no new sku ends the listing.
PAGINATES_BY_URL = False

PAGE_URL_REASON = ("Quora paginates by infinite scroll and publishes no "
                   "per-page URL: a `?page=N` parameter on a topic, profile "
                   "or question URL is ignored and the feed restarts at its "
                   "first items")

# A feed that never runs out would otherwise run until the process is killed.
# 40 scroll batches at the ~10 answers a batch adds is ~400 answers, which is
# past the point where Quora starts repeating a topic's items.
PAGE_CAP = 40


def paginates_by_url(url: str = "") -> bool:
    return PAGINATES_BY_URL


def page_url(url: str, page: int) -> Optional[str]:
    """None, always. See PAGINATES_BY_URL."""
    return None


def concurrency_limit(url: str = "") -> Optional[int]:
    """1, always — and the engines must REFUSE a higher value, not clamp it.

    §7's concurrency rests on page N's address being knowable without
    fetching page N-1. Here it is not knowable at all: batch 5 of an infinite
    scroll exists only inside the browser that scrolled through batches 1-4,
    so there is nothing to hand a second worker.
    """
    return 1


CONCURRENCY_REASON = ("each feed here is one browser scrolling one page, so "
                      "there is no second page for a second worker to fetch")


# ===========================================================================
# Selectors
# ===========================================================================
# Quora's own test and logging hooks. Every one of these was counted on six
# captures across two languages and four page kinds before it was written
# down; see the module docstring for why a class is the anchor here.
SELECTORS: Dict[str, str] = {
    # The permalink the site prints under every answer. THE anchor.
    "item_card": "a.answer_timestamp",
    # The question an answer answers, as rendered on a feed card. Absent on a
    # question page, where the question is the page's own heading.
    "question_title": ".puppeteer_test_question_title",
    # The answer body. Two hooks, because the feed card and the question page
    # use different ones; both are stable.
    "answer_body": ".puppeteer_test_answer_content, .spacing_log_answer_content",
    # The card's author link. The FIRST profile link in a card is the avatar
    # and has no text — reading that one gives an author of "" on every row
    # while looking like it worked (§8).
    "author_link": 'a[href*="/profile/"]',
    # The block holding the author's name, their credential for THIS answer
    # and the date, in that order. Present on every card of every mode
    # (measured 30/30, 30/30, 18/18, 12/12 on four captures).
    "answer_header": ".spacing_log_answer_header",
    # The question text INSIDE a title node, without the badge Quora puts in
    # front of it. See `_card_question_title`.
    "question_title_text": ".qu-userSelect--text",
    # Quora MERGES duplicate questions, and an answer written for the
    # merged-away one is shown on the surviving question's page under this
    # banner: "Originally Answered: {the original question}". The banner
    # carries a LINK whose text is that question and whose href is its URL.
    "merged_question_banner": ".spacing_log_originally_answered_banner",
    # The question page's own heading.
    "page_question_title": "h1",
    # Quora's answer-count line on a question page.
    "answer_count": ".answer_count, .q-text",
}

# How many rendered answer links mean "this feed has painted". Must be > 1:
# waiting for one match resolves on the page's own first card long before the
# feed is there (§5).
MIN_CARD_MATCHES = 2

NEXT_PAGE_SELECTOR = ""      # there is no next-page control anywhere on Quora


def count_cards(html: Optional[str]) -> int:
    """How many rendered answers this document holds."""
    if not html:
        return 0
    return len(BeautifulSoup(html, "html.parser").select(SELECTORS["item_card"]))


# ===========================================================================
# The inline GraphQL payloads
# ===========================================================================
# Quora pushes each query result into the page as a JSON *string literal*:
#
#   window.ansFrontendGlobals.data.inlineQueryResults
#       .results["c310f5…"].push("{\"data\":{\"question\":{…}}}");
#
# so the literal is decoded once to get the JSON text and once more to get
# the object. A payload that fails either decode is skipped rather than
# raised on: a truncated document should cost its own rows, not the run.
_INLINE_PUSH_RE = re.compile(
    r'inlineQueryResults\.results\[\s*"[0-9a-f]+"\s*\]\.push\(\s*(".*?")\s*\)',
    re.S)


def inline_payloads(html: Optional[str]) -> List[dict]:
    """Every decoded inline query result in this document, in page order."""
    out: List[dict] = []
    for match in _INLINE_PUSH_RE.finditer(html or ""):
        try:
            literal = json.loads(match.group(1))
            payload = json.loads(literal)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _nodes_of_type(payloads: Sequence[dict], typename: str) -> List[dict]:
    """Every object in these payloads whose `__typename` is `typename`.

    A walk rather than a path, because the same Answer object appears under
    `question.answers.edges[].node.answer` on a question page and under
    `user.profileFeed.edges[].node.answer` on a profile, and pinning either
    path would make one of the two modes silently empty.
    """
    found: List[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("__typename") == typename:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for payload in payloads:
        walk(payload)
    return found


# ---------------------------------------------------------------------------
# Quora's rich text
# ---------------------------------------------------------------------------
# Every string Quora publishes — a question title, an answer body, a
# credential — is itself a JSON document:
#
#   {"sections": [{"spans": [{"text": "…", "modifiers": {…}}],
#                  "type": "plain"|"unordered-list"|"image"|…,
#                  "indent": 0, "quoted": false}]}
#
# Reading `.get("title")` and writing it to a column puts that whole JSON
# blob in the row, which is the kind of 100%-populated, entirely-wrong column
# §10 exists to catch.
_LIST_TYPES = ("unordered-list", "ordered-list")


def render_rich_text(value) -> Optional[str]:
    """Quora's rich text as plain text, or None.

    A string that is not Quora's JSON shape is returned as-is: a few fields
    (a Space name, some credentials) are plain strings already, and a
    renderer that dropped them would trade one wrong column for another.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if not stripped.startswith("{"):
            return stripped
        try:
            value = json.loads(stripped)
        except ValueError:
            return stripped
    if not isinstance(value, dict):
        return None

    lines: List[str] = []
    for section in value.get("sections") or []:
        if not isinstance(section, dict):
            continue
        spans = section.get("spans") or []
        text = "".join(span.get("text") or "" for span in spans
                       if isinstance(span, dict))
        kind = section.get("type")
        if kind == "image" and not text.strip():
            # An image section carries its URL in a span modifier and no
            # text. Dropped rather than rendered as an empty line, so an
            # answer built of screenshots does not come out as blank lines.
            continue
        if kind in _LIST_TYPES:
            text = "- " + text
        if section.get("quoted"):
            text = "> " + text
        lines.append(text)
    rendered = "\n".join(lines).strip()
    return rendered or None


def _display_name(user: Optional[dict]) -> Optional[str]:
    """A User object's display name.

    Quora stores given and family name separately with a `reverseOrder` flag,
    because the sites it serves include ones that write the family name
    first. Joining them in a fixed order would misname every Japanese author
    on jp.quora.com.
    """
    if not isinstance(user, dict):
        return None
    for name in user.get("names") or []:
        if not isinstance(name, dict):
            continue
        given = (name.get("givenName") or "").strip()
        family = (name.get("familyName") or "").strip()
        ordered = f"{family} {given}" if name.get("reverseOrder") else \
                  f"{given} {family}"
        ordered = " ".join(ordered.split())
        if ordered:
            return ordered
    return None


def _credential(answer: dict) -> Optional[str]:
    """The line under the author's name: "former Data Scientist at …".

    Quora has five credential types (work, school, location, life-experience
    and free-form) and they all carry the rendered line in the same field, so
    this needs no type switch — but it does need the answer's OWN credential
    rather than the author's best one, because an author picks a different
    credential per answer.
    """
    for key in ("authorCredential", "businessCredential"):
        node = answer.get(key)
        if isinstance(node, dict):
            rendered = render_rich_text(node.get("translatedString"))
            if rendered:
                return rendered
    author = answer.get("author")
    if isinstance(author, dict):
        best = author.get("bestCredential")
        if isinstance(best, dict):
            return render_rich_text(best.get("translatedString"))
    return None


def _epoch_us_to_iso(value) -> Optional[str]:
    """Quora's timestamps are epoch MICROSECONDS, not seconds or millis.

    `creationTime: 1757155453070805` is 2025-09-06. Read as seconds it is the
    year 57 million; read as milliseconds, the year 57705. Both parse without
    error, which is why this is a named function with a test rather than an
    inline division.
    """
    try:
        micros = int(value)
    except (TypeError, ValueError):
        return None
    if micros <= 0:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(micros / 1_000_000, timezone.utc) \
            .isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def answers_from_payloads(html: Optional[str], base_url: str) -> Dict[str, dict]:
    """Every answer the inline payloads describe, keyed by `sku`.

    Returns a dict rather than a list because this is an OVERLAY: the DOM
    decides which rows exist and in what order, and this is looked up per row
    (§4's structured-vs-displayed reconciliation, in this site's shape).
    Answers the payload carries and the DOM does not render are returned too;
    `parse_answers` appends them, because a machine answer at
    `/{Q}/answers/{aid}` is real content that the feed renders in a block the
    card selectors do not match.
    """
    payloads = inline_payloads(html)
    out: Dict[str, dict] = {}
    for node in _nodes_of_type(payloads, "Answer"):
        if node.get("isDeleted"):
            continue
        perma = node.get("permaUrl") or node.get("url")
        question = node.get("question") if isinstance(node.get("question"), dict) else {}
        if not perma and question.get("url"):
            # An answer permalink page's own payload omits permaUrl; the page
            # URL is the permalink in that case.
            perma = urlparse(base_url).path or question.get("url")
        if not perma:
            continue
        key = permalink_key(_absolute(base_url, perma))
        if not key:
            continue
        # A payload can describe the same answer twice (a feed item and its
        # own node). Last write wins, and they are identical.
        out[key] = node
    return out


def _row_from_payload(node: dict, base_url: str) -> dict:
    """The columns an Answer object contributes, as a plain dict."""
    question = node.get("question") if isinstance(node.get("question"), dict) else {}
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    perma = node.get("permaUrl") or node.get("url") or ""
    absolute = _absolute(base_url, perma) if perma else ""
    q_url = _absolute(base_url, question.get("url") or "") if question.get("url") else None

    profile_url = author.get("profileUrl")
    return {
        "url": strip_tracking(absolute) or None,
        "answer_id": str(node["aid"]) if node.get("aid") else None,
        "title": render_rich_text(question.get("title")),
        "question_url": strip_tracking(q_url) if q_url else None,
        "question_id": str(question["qid"]) if question.get("qid") else None,
        "answer_count": question.get("answerCount"),
        "author": _display_name(author),
        "author_url": strip_tracking(_absolute(base_url, profile_url))
                      if profile_url else None,
        "author_credential": _credential(node),
        "author_is_verified": author.get("isVerified"),
        "text": render_rich_text(node.get("content")),
        "upvotes": node.get("numUpvotes"),
        "views": node.get("numViews"),
        "shares": node.get("numShares"),
        "comments": node.get("numDisplayComments"),
        "created_at": _epoch_us_to_iso(node.get("creationTime")),
        "updated_at": _epoch_us_to_iso(node.get("updatedTime")),
        "is_machine_answer": node.get("isMachineAnswer"),
        "is_translated": node.get("isTranslated"),
    }


# ===========================================================================
# The rendered cards
# ===========================================================================
# Widening a permalink to its card, by the §4 rule and one addition.
#
# §4 says stop at the outermost ancestor still covering exactly one item,
# counting distinct ids. On Quora that rule alone never fires: the feed wraps
# every card in a chain of single-child divs, and the ancestor at sixteen
# levels up STILL holds one permalink while having absorbed the neighbouring
# ad slot and the page footer (measured: a scope of 161,882 bytes on an
# answer permalink page, which is the whole document).
#
# So the stop condition is a POSITIVE one — the first ancestor that contains
# the answer's own body — with the distinct-permalink count kept as the guard
# that stops it one level before a neighbour's card, and the level cap kept
# as the backstop §4 asks for.
_WIDEN_CAP = 16


def _card_of(anchor) -> object:
    scope = anchor
    node = anchor
    for _ in range(_WIDEN_CAP):
        if node.parent is None:
            break
        node = node.parent
        permalinks = {a.get("href") for a in node.find_all("a", href=True)
                      if "answer_timestamp" in (a.get("class") or [])}
        if len(permalinks) > 1:
            break
        scope = node
        if node.select_one(SELECTORS["answer_body"]) is not None:
            break
    return scope


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""


def _author_of(card) -> Tuple[Optional[str], Optional[str]]:
    """The card's author name and profile URL.

    The first `/profile/` anchor in a card is the AVATAR — an image link with
    no text — so taking it gives `author=""` on every row while every
    coverage check reads 100%. The name is the first profile link that has
    text; the URL falls back to the avatar's, which is the same profile.
    """
    anchors = card.select(SELECTORS["author_link"])
    named = next((a for a in anchors if _text(a)), None)
    if named is not None:
        return _text(named), named.get("href")
    if anchors:
        return None, anchors[0].get("href")
    return None, None


# The header reads "{Author} {Credential} · {Date}" with no element that
# holds the credential alone — its own wrapper is a build-hashed class
# (`c1h7helg`) carrying a `--separator` CSS variable, which is exactly the
# kind of anchor §4 forbids. So the credential is read the way §4 reads a
# price out of a node that also holds a strike: from a COPY of the header
# with the author and timestamp subtrees removed, leaving what is by
# definition the credential.
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.)\]])")
_CREDENTIAL_TRIM = " ·•|—-·•"


def _card_credential(card) -> Optional[str]:
    header = card.select_one(SELECTORS["answer_header"])
    if header is None:
        return None
    clone = BeautifulSoup(str(header), "html.parser")
    for node in clone.select(SELECTORS["item_card"]):
        node.decompose()
    for node in clone.select(SELECTORS["author_link"]):
        node.decompose()
    text = clone.get_text(" ", strip=True)
    # get_text(" ") puts a space before the punctuation Quora's own spans
    # split on — "Uppsala University , (Graduated 1991)". Normalised because
    # a credential is a human-readable line and the artefact is this
    # parser's, not the site's.
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text).strip(_CREDENTIAL_TRIM).strip()
    return text or None


def _card_question_title(card) -> Optional[str]:
    """The question a card names, or None if it names none.

    Two things sit in front of the question text and neither is the question,
    both found by a live run rather than by a capture — a capture of an
    unscrolled question page has neither:

    * a **"Related" badge**, which Quora renders INSIDE the title node as a
      `<div>` pill. Reading the node's text gives "Related What is machine
      learning for?" on 29 of 260 rows of one measured run: a column 100%
      populated and wrong, which is this family's most expensive bug class.
      The question itself is in the node's own `qu-userSelect--text` span, so
      the badge is skipped structurally rather than by matching the word
      "Related" — which is localised, and would need 24 translations.

    * a **merged-question banner**. Quora merges duplicate questions, and an
      answer written for the merged-away one appears on the surviving
      question's page under "Originally Answered: {question}". Those cards
      have no title node at all — 27 of 260 on the same run — and the page's
      own heading is NOT their question. The banner carries a link whose text
      IS the question, so it is read from there; again structurally, because
      the "Originally Answered:" prefix is localised and the link is not.
    """
    node = card.select_one(SELECTORS["question_title"])
    if node is not None:
        inner = node.select_one(SELECTORS["question_title_text"])
        if inner is not None:
            text = _text(inner)
            if text:
                return text
        # No inner span: read the node minus any badge, rather than reading
        # the node whole.
        clone = BeautifulSoup(str(node), "html.parser")
        for badge in clone.find_all("div"):
            badge.decompose()
        text = _text(clone)
        if text:
            return text
    banner = card.select_one(SELECTORS["merged_question_banner"])
    if banner is not None:
        link = banner.find("a", href=True)
        if link is not None and _text(link):
            return _text(link)
    return None


def page_question_title(html: Optional[str]) -> Optional[str]:
    """A question page's own question, for the cards that do not repeat it.

    On a question page the answer cards carry no question title — the
    question is the page's `h1`, printed once. Without this fallback
    `--mode question` writes a null `title` on every row while topic and
    profile mode fill it, which is the sort of per-mode hole §10's
    value-assertions exist to catch.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(SELECTORS["page_question_title"])
    if heading is not None and _text(heading):
        return _text(heading)
    meta = soup.select_one('meta[property="og:title"]')
    if meta is not None and meta.get("content"):
        return meta["content"].strip() or None
    return None


def answers_from_dom(html: Optional[str], base_url: str) -> List[dict]:
    """Every rendered answer card, in the site's own order."""
    soup = BeautifulSoup(html or "", "html.parser")
    fallback_title = None
    rows: List[dict] = []

    page_question = _question_path(base_url)

    for anchor in soup.select(SELECTORS["item_card"]):
        href = anchor.get("href") or ""
        absolute = _absolute(base_url, href)
        key = permalink_key(absolute)
        if not key or site_host(absolute) is None:
            continue
        card = _card_of(anchor)
        title = _card_question_title(card)
        if title is None:
            # The page's own heading, and ONLY for a card that really answers
            # the page's own question. A question page carries answers to
            # OTHER questions too — merged duplicates, related questions and
            # promoted answers — and handing those the page's heading is a
            # confidently wrong value rather than a missing one (§8). Measured
            # on one live run: 27 of 260 rows would have claimed the wrong
            # question.
            same_question = (page_question is not None
                             and _question_path(absolute) == page_question)
            if same_question:
                if fallback_title is None:
                    fallback_title = page_question_title(html) or ""
                title = fallback_title or None
        author, author_url = _author_of(card)
        body = card.select_one(SELECTORS["answer_body"])
        rows.append({
            "sku": key,
            "url": strip_tracking(absolute),
            "title": title,
            "question_url": question_url_of(strip_tracking(absolute)),
            "author": author,
            "author_url": strip_tracking(_absolute(base_url, author_url))
                          if author_url else None,
            "author_credential": _card_credential(card),
            "text": _text(body) or None,
            # What the card prints where a date goes: "Aug 14", "2y",
            # "3 años". A display string in the page's own language and
            # NEVER a date — Quora prints a relative age for anything older
            # than the current year, and "2y" cannot be resolved to a day.
            # The real timestamp is `created_at`, which only the inline
            # payload carries.
            "date_text": _text(anchor) or None,
        })
    return rows


# ===========================================================================
# Challenge, block and page-state detection
# ===========================================================================
# Every marker below was COUNTED on six pages known to be good before it was
# written down (§18: a marker that matches every page is worse than no
# marker). Two obvious candidates failed that test and are deliberately
# absent:
#
#   cf-turnstile               1 occurrence on every good page, 1 on the
#                              challenge page. Quora ships an empty,
#                              zero-size Turnstile mount
#                              (`input name="cf-turnstile-response"` inside a
#                              0x0 fixed div) on every page it serves.
#   challenges.cloudflare.com  1 on every good page, 6 on the challenge.
#                              Quora loads Turnstile's own
#                              `api.js?render=explicit` in the head of every
#                              page for its sign-in flow.
#   recaptcha / api.js?render  1 on every good page and ZERO on the
#                              challenge page — the hits are feature-flag
#                              names (`baseline_recaptcha_rate`) in Quora's
#                              page config. A marker that fires only on good
#                              pages is the failure inverted.
#
# So Quora is §18's "configured but not rendered" case exactly: a captcha is
# wired into every page and an anonymous reader is never shown one.
CHALLENGE_MARKERS: Tuple[str, ...] = (
    "just a moment...",
    "_cf_chl_opt",
    "/cdn-cgi/challenge-platform",
    "cf-mitigated",
    "checking if the site connection is secure",
    "enable javascript and cookies to continue",
)

# What a solver could actually be pointed at, if Quora ever rendered one.
# Kept deliberately broader than what has been observed (§8: detection stays
# broad, paying stays narrow).
BOT_CHALLENGE_MARKERS: Tuple[str, ...] = (
    "g-recaptcha",
    'class="h-captcha"',
    "hcaptcha.com/1/api.js",
    "datadome",
    "px-captcha",
    "_Incapsula_Resource",
    "www.google.com/recaptcha/api2/anchor",
    "www.google.com/recaptcha/api2/bframe",
)

# A Cloudflare MANAGED challenge carries no sitekey — measured on the one
# this site served: `cType: 'managed'`, zero `data-sitekey` attributes, zero
# Turnstile iframes, nothing a task-based solver can be given. The response
# to it is a retry or a different exit, not a purchase, and
# `detect_page_state` reports it as `challenge` so the engines retry rather
# than as a solvable one so they pay.
SOLVABLE_CHALLENGES: Tuple[str, ...] = ("recaptcha", "hcaptcha")

# The positive "this page was built out of Quora's own assets" signal (§8).
# Counted: 265-354 occurrences on every good page, 0 on the challenge page
# and 0 on a browser network-error page, which no marker list would recognise
# because it carries the site's own hostname in its <title>.
_ASSET_MARKER = re.compile(r"quoracdn\.net|puppeteer_test_|ansFrontendGlobals")
_ASSET_MIN_MATCHES = 3


def served_by_quora(html: Optional[str]) -> bool:
    if not html:
        return False
    return len(_ASSET_MARKER.findall(html)) >= _ASSET_MIN_MATCHES


def is_challenge_page(html: Optional[str]) -> bool:
    lowered = (html or "").lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def detect_bot_challenge(html: Optional[str], url: str = "") -> Optional[str]:
    """Which vendor's challenge is rendered here, or None.

    Only ever REFINES a verdict the page-state policy has already reached —
    it is not consulted for a page that classified as `content` or `empty`,
    because a detection on a page whose answers are already rendered guards
    nothing and a detection on a correct empty answer is simply wrong (§18).
    """
    lowered = (html or "").lower()
    for marker in BOT_CHALLENGE_MARKERS:
        if marker.lower() in lowered:
            if "recaptcha" in marker:
                return "recaptcha"
            if "hcaptcha" in marker:
                return "hcaptcha"
            if "datadome" in marker:
                return "datadome"
            if "px-captcha" in marker:
                return "perimeterx"
            if "incapsula" in marker.lower():
                return "incapsula"
    if is_challenge_page(html):
        return "cloudflare"
    return None


def detect_block_marker(html: Optional[str]) -> Optional[str]:
    if is_challenge_page(html):
        return "cloudflare"
    if html and not served_by_quora(html):
        return "not-served-by-quora"
    return None


# Quora's own empty answers, in the languages this repo has captured. A
# POSITIVE signal only: a feed with no cards and none of this copy is a page
# still painting, and the two want opposite responses (§18).
NO_RESULTS_MARKERS: Tuple[str, ...] = (
    "there are no answers yet",
    "no answers yet",
    "this topic doesn't have any",
    "no se han encontrado resultados",
    "aún no hay respuestas",
    "page not found",
    "the page you were looking for",
)


def is_no_results(html: Optional[str]) -> bool:
    lowered = (html or "").lower()
    return any(marker in lowered for marker in NO_RESULTS_MARKERS)


def detect_page_state(html: Optional[str], status: Optional[int] = None,
                      url: str = "") -> str:
    """Which of five states this response is.

    Ordered by how much each signal PROVES, not by what is cheap to check
    (§17). `status` is positional and second, matching `page_flow.classify`;
    two engines in a sibling repo passed it as a keyword and both crashed on
    their first fetch, invisibly to every offline check.

    The five:

        content    answers are in the document
        empty      the site's own "no answers" copy, in its own language
        challenge  an interstitial, named by vendor where it names itself
        blocked    a refusal with nothing to read, or a page that is not
                   Quora's at all
        shell      Quora's, served, and the feed has not painted yet — the
                   state that wants the readiness wait and the scroll rather
                   than a refetch. EVERY Quora page starts here: the server
                   renders no content DOM at all, so the first response of a
                   perfectly good topic page is a 100 KB shell.
    """
    if html is None:
        return "blocked"

    # 1. Unambiguous positive: rendered answers.
    if count_cards(html) > 0:
        return "content"

    # 1b. The inline payload describes answers even where none have painted.
    #     A question page's server response carries five of them, so a run
    #     that never scrolled still has content rather than a shell.
    if answers_from_payloads(html, url):
        return "content"

    # 2. Its own empty answer, in its own language.
    if is_no_results(html):
        return "empty"

    # 3. The interstitial. Before the status check because it says WHICH
    #    vendor, and before the asset threshold because a challenge page is
    #    not built out of Quora's assets and would otherwise read as a
    #    generic block.
    if is_challenge_page(html):
        return "challenge"

    # 4. A refusal with no page to read.
    if status is not None and (status in (401, 403, 429) or status >= 500):
        return "blocked"

    # 5. A vendor challenge rendered inside a page.
    if detect_bot_challenge(html):
        return "challenge"

    # 6. Not built out of Quora's assets: a browser error page, or somebody
    #    else's interstitial. This is the one that catches Chromium's own
    #    network-error page, which carries the site's hostname in its title
    #    and would pass any text marker.
    if not served_by_quora(html):
        return "blocked"

    # 7. Ours, served, not painted.
    return "shell"


# ===========================================================================
# Rows
# ===========================================================================
def _merge(dom_row: dict, payload_node: Optional[dict],
           base_url: str) -> Tuple[dict, str]:
    """One row out of the two views, and where each value came from.

    `payload_node` is the raw GraphQL object, and it is mapped to this repo's
    column names BEFORE anything is copied. Copying its own keys across
    directly is a bug that reads as working: the merged dict then holds
    `numUpvotes` and `aid`, `_build` asks for `upvotes` and `answer_id`, and
    every row comes out with a `dom+inline` provenance and null counts.
    """
    merged = dict(dom_row)
    if not payload_node:
        return merged, "dom"
    for key, value in _row_from_payload(payload_node, base_url).items():
        if value is None or value == "":
            continue
        # The payload wins on every column it publishes: it is the site's own
        # data, where the DOM is the site's own data after truncation. The
        # one exception is `text`, and it is the reason this is not a blanket
        # overwrite in the other direction — a feed card's body is cut to
        # three lines and ends in "(more)", so the payload's full text is
        # strictly better and must not be left behind.
        merged[key] = value
    return merged, "dom+inline"


def parse_answers(html: str, url: str, page: int = 1,
                  mode: Optional[str] = None) -> List[Answer]:
    """Every answer on this page, in the site's own order.

    `page` is threaded in rather than defaulted, because `position` restarts
    at 1 on every page and a `page` column stuck at 1 makes the pair
    worthless — 60 of 119 rows of a sibling repo's two-page run silently
    claimed a position another row already held (§18). `smoke_test.py`
    asserts the pair is unique across a multi-page run.
    """
    mode = mode or listing_kind(url)
    source = source_of(url)
    language = language_of(url)
    category = category_from_url(url)

    payload_rows = answers_from_payloads(html, url)
    dom_rows = answers_from_dom(html, url)

    rows: List[Answer] = []
    used: set = set()
    position = 0

    for dom_row in dom_rows:
        sku = dom_row["sku"]
        if sku in used:
            # Quora repeats a card when a feed reloads around an ad slot.
            # Dropped here rather than in the writer so `position` stays a
            # description of the rendered order.
            continue
        used.add(sku)
        merged, provenance = _merge(dom_row, payload_rows.get(sku), url)
        position += 1
        rows.append(_build(merged, sku, provenance, source, language,
                           category, mode, page, position))

    # Answers the payload describes that no card matched. A question page's
    # machine answer is rendered in a block the card selectors do not match,
    # and dropping it would lose the one answer Quora itself wrote.
    for sku, node in payload_rows.items():
        if sku in used:
            continue
        used.add(sku)
        position += 1
        rows.append(_build(_row_from_payload(node, url), sku, "inline",
                           source, language, category, mode, page, position))

    _log_coverage(rows, url)
    return rows


def _build(values: dict, sku: str, provenance: str, source: str,
           language: Optional[str], category: Optional[str], mode: str,
           page: int, position: int) -> Answer:
    url = values.get("url") or f"https://{sku}"
    # `source` and `language` describe the ROW, not the run. A feed on
    # www.quora.com links to answers published in Spaces and Quora Sessions,
    # which live on their own subdomains — measured, four of a profile's
    # eighteen — and those really are a different catalogue. Taking them from
    # the run's URL instead would label a Session answer `www.quora.com` and
    # give it the run's language, which is a guess wearing the look of a
    # fact (§8). `source`/`language` arguments remain the fallback for a row
    # whose own URL is unusable.
    row_source = source_of(url) or source
    row_language = language_of(url) if site_host(url) else language
    return Answer(
        source=row_source,
        url=url,
        sku=sku,
        title=values.get("title"),
        question_url=values.get("question_url") or question_url_of(url),
        question_id=values.get("question_id"),
        answer_id=values.get("answer_id"),
        answer_count=_as_int(values.get("answer_count")),
        author=values.get("author"),
        author_url=values.get("author_url"),
        author_credential=values.get("author_credential"),
        author_is_verified=values.get("author_is_verified"),
        text=values.get("text"),
        text_chars=len(values["text"]) if values.get("text") else None,
        upvotes=_as_int(values.get("upvotes")),
        views=_as_int(values.get("views")),
        shares=_as_int(values.get("shares")),
        comments=_as_int(values.get("comments")),
        created_at=values.get("created_at"),
        updated_at=values.get("updated_at"),
        date_text=values.get("date_text"),
        is_machine_answer=values.get("is_machine_answer"),
        is_translated=values.get("is_translated"),
        language=row_language,
        category=category,
        mode=mode,
        data_source=provenance,
        page=page,
        position=position,
    )


def _as_int(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# The share of rows the inline payload reached. Logged rather than asserted,
# because it legitimately varies by mode: a topic feed's payload carries no
# answers at all, so `dom` on every row there is correct and a warning would
# be noise. The warning fires only where the payload was expected.
PAYLOAD_EXPECTED_MODES = ("question", "profile")
PAYLOAD_COVERAGE_WARN = 0.10


def _log_coverage(rows: Sequence[Answer], url: str) -> None:
    if not rows:
        return
    enriched = sum(1 for row in rows if row.data_source != "dom")
    share = enriched / len(rows)
    mode = rows[0].mode
    log.info("inline-payload coverage: %d/%d rows (%.0f%%) on %s",
             enriched, len(rows), share * 100, mode)
    if mode in PAYLOAD_EXPECTED_MODES and share < PAYLOAD_COVERAGE_WARN:
        log.warning("inline-payload coverage %.0f%% on a %s page — upvotes, "
                    "views and full answer text come from that payload and "
                    "will be null on most rows", share * 100, mode)
