"""Cut the offline suite's fixtures out of real captures, and PROVE they
parse the same.

Its output is `fixtures_generated.json`, which `smoke_test.py` loads. This
script is shipped because two files point at it — `smoke_test.py`'s own
docstring and TROUBLESHOOTING.md — and an instruction pointing at a file that
does not exist is worse than no instruction.

WHAT YOU NEED TO RUN IT
-----------------------
Your own captures, in `../captures/` relative to the repo, named as `SOURCES`
below expects. They are deliberately NOT in the repository: a single capture
of this site is 300 KB to 1 MB.

Take them with a real browser — `--dump-html` on any engine writes exactly
the bytes the parser was given. Take a QUESTION page as well as a topic one:
a question page carries Quora's inline GraphQL payload and a topic page does
not, and those are the repo's two read paths.

WHAT IT ENFORCES, and why each rule is here
-------------------------------------------
  * every fixture is CUT from a real capture, never hand-written. The one
    thing in a sibling repo that WAS hand-written — a guess at the site's
    "nothing matched" copy — matched none of the real strings, and an empty
    search came back as `shell` and spent a 25-second readiness wait on an
    answer the site had already given;
  * each one is verified to parse IDENTICALLY to the untrimmed original for
    the answers it keeps — every column, not just a count;
  * the trimmed fixture must still CLASSIFY the same way, which is what
    catches a trim that dropped the site's own asset references and turned a
    good page into a `blocked` one.

WHAT IS NOT VERBATIM, and why
-----------------------------
Three things are rewritten before anything is written to disk, and every one
of them is a PERSON rather than the site:

    the author's display name          -> "Fixture Author N"
    the author's profile slug          -> "/profile/Fixture-Author-N"
    the answer's body text             -> filler of the SAME LENGTH
    the author's credential line       -> filler, span structure preserved

Quora answers are bylined public writing, but republishing a named person's
prose in a scraper's test corpus is a separate act from the site showing it
on its own page (§10, where a sibling repo committed a real customer's
review, name and photo ids). The STRUCTURE is what the checks need — the
card's markup, the class hooks, the URL shapes, the payload's keys, the
counts, the timestamps — and all of that survives untouched.

Question titles, ids, upvote and view counts, timestamps and every piece of
markup Quora generates are kept verbatim: they are the site's, they are what
the parser is being tested against, and `text_chars` assertions depend on the
filler matching the original's length exactly.
"""
import json
import os
import re
import sys
import pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import product_parser as P
from bs4 import BeautifulSoup
from dataclasses import asdict

HERE = pathlib.Path(__file__).parent
OUT = HERE / "fixtures_generated.json"


def _find_captures() -> pathlib.Path:
    """The captures directory, wherever the checkout happens to sit.

    Walks up rather than hardcoding `../../captures`: the same repo is worked
    on from a plain clone, from a git worktree (which nests it two levels
    deeper) and from CI, and a fixed relative path is right in exactly one of
    those. `QUORA_CAPTURES` overrides it outright.
    """
    override = os.environ.get("QUORA_CAPTURES")
    if override:
        return pathlib.Path(override)
    for parent in [HERE] + list(HERE.parents):
        candidate = parent / "captures"
        if candidate.is_dir():
            return candidate
    return HERE.parent.parent / "captures"


CAPTURES = _find_captures()

# name -> (capture file, url it was fetched from, how many answers to keep)
SOURCES = {
    # The DOM-only path, which is what a topic feed is.
    "TOPIC_EN":    ("topic_ml_scroll.html",
                    "https://www.quora.com/topic/Machine-Learning", 4),
    # A second language, per §15. Spanish brings a non-ASCII question title,
    # a percent-encoded slug and a localised relative date ("3 años") that
    # must NOT be parsed into a date.
    "TOPIC_ES":    ("topic_es.html",
                    "https://es.quora.com/topic/Aprendizaje-autom%C3%A1tico", 4),
    # The inline-payload path: a question page carries its first answers in
    # the served HTML, with the counts and the full text the DOM never shows.
    "QUESTION_EN": ("question_ml.html",
                    "https://www.quora.com/What-is-machine-learning-4", 4),
    # A profile, which is where the Quora Session subdomain shows up: four of
    # eighteen permalinks here are on `{session}.quora.com` with no `/answer/`
    # segment at all, which no URL pattern can tell from a question page.
    # The URL is rewritten to the placeholder slug the anonymiser produces
    # for this capture's own subject, so the fixture does not carry a named
    # person in `_URLS` either.
    "PROFILE_EN":  ("profile_lecun.html",
                    "https://www.quora.com/profile/Fixture-Author-1", 4),
}
BLOCK_SOURCE = ("blocked_cf_challenge.html",
                "https://www.quora.com/topic/Machine-Learning")

# A card whose permalink is on a Session subdomain must survive into
# PROFILE_EN: it is the second URL shape, only a profile feed carries one,
# and a fixture without it cannot catch `permalink_key` regressing on the
# ordinary `/answer/` shape alone.
SESSION_PERMA_RE = re.compile(r'href="https://(?!www\.)[a-z0-9-]+\.quora\.com/')


# ---------------------------------------------------------------------------
# Anonymisation
# ---------------------------------------------------------------------------
# Applied to the FULL capture before anything is trimmed, so the
# identical-parse proof below compares like with like.
_AUTHOR_FILLER = "Fixture Author"
_SLUG_FILLER = "Fixture-Author"
# Deterministic filler. Not lorem ipsum: a reader opening the fixture should
# see immediately that it is not a real answer.
_FILLER_WORDS = ("fixture answer body text placeholder not written by a "
                 "real person ").split()


def _filler(length: int) -> str:
    """Filler of exactly `length` characters, so text_chars is unchanged."""
    if length <= 0:
        return ""
    out = []
    size = 0
    index = 0
    while size < length:
        word = _FILLER_WORDS[index % len(_FILLER_WORDS)]
        out.append(word)
        size += len(word) + 1
        index += 1
    return " ".join(out)[:length]


def _profile_numbers(html: str):
    """Every distinct `/profile/{slug}` in the capture, in first-seen order."""
    seen = []
    for match in re.finditer(r"/profile/([A-Za-z0-9%\-.]+)", html):
        slug = match.group(1)
        if slug not in seen:
            seen.append(slug)
    return seen


def anonymise(html: str) -> str:
    """Replace the people in this capture, keeping everything Quora wrote.

    Order matters: the profile slugs are rewritten first, so the display
    names derived from them below line up with the URLs a reader would follow.
    """
    slugs = _profile_numbers(html)
    replacement = {}
    for index, slug in enumerate(slugs, start=1):
        replacement[slug] = f"{_SLUG_FILLER}-{index}"
    # Longest first, so a `Name-2` slug is not half-replaced by `Name`.
    for slug in sorted(slugs, key=len, reverse=True):
        html = html.replace(f"/profile/{slug}", f"/profile/{replacement[slug]}")
        # The SAME slug appears again as the last segment of every answer
        # permalink — `/{Question-Slug}/answer/{Author-Slug}` — and replacing
        # only the profile link leaves the person named in every sku.
        html = html.replace(f"/answer/{slug}", f"/answer/{replacement[slug]}")
        # The name as Quora spells it in a card, which is the slug with
        # dashes turned into spaces and any disambiguating suffix dropped.
        spelled = re.sub(r"-\d+$", "", slug).replace("-", " ")
        if len(spelled) > 3:
            number = replacement[slug].rsplit("-", 1)[-1]
            html = html.replace(spelled, f"{_AUTHOR_FILLER} {number}")

    # The SAME slugs again, percent-encoded, inside the share-intent URLs
    # Quora builds for Twitter and friends. Those carry the person's name
    # twice over — once in the slug and once in the tweet text — and a
    # replacement that only handled the readable form leaves both.
    for slug in sorted(slugs, key=len, reverse=True):
        html = html.replace(f"%2Fprofile%2F{slug}",
                            f"%2Fprofile%2F{replacement[slug]}")
        html = html.replace(slug.replace("-", "+"),
                            replacement[slug].replace("-", "+"))

    # A Quora Session or Space lives on its own subdomain, and a Session's
    # is built out of its subject's name
    # (`quorasessionwith{firstlast}.quora.com`). The SHAPE is what the
    # fixture is for — a permalink on a non-www host with no `/answer/`
    # segment — so the host is renamed rather than dropped.
    def _rename_host(match):
        host = match.group(1) + ".quora.com"
        # es.quora.com and its twenty-two siblings are LANGUAGE hosts, not
        # people. Renaming one would destroy the very thing the Spanish
        # fixture exists to prove.
        if host in P.LANGUAGE_HOSTS or host in P.NON_CONTENT_HOSTS:
            return match.group(0)
        return "https://fixture-session.quora.com"

    html = re.sub(r"https://(?!www\.)([a-z0-9-]+)\.quora\.com",
                  _rename_host, html)

    return html


# The payload is a JSON document inside a JSON string literal inside a
# <script>, which means three levels of backslash escaping and a value that
# can contain escaped quotes of its own. Regex surgery on that LEAKS: a
# pattern whose value class stops at the first backslash replaces the head of
# an answer and leaves its tail, which is worse than not scrubbing at all
# because it looks scrubbed. Measured, on the first attempt: four names and a
# vendor link survived into the fixtures.
#
# So the payload is decoded, edited as an object, and re-encoded. The cost is
# that its escaping is then Python's rather than Quora's; the structure, every
# key, and every value that is not a person's words are the site's, and that
# is what the parser is being tested on. Said out loud because it is the one
# place a fixture is not byte-for-byte what the site sent.
_NAME_KEYS = {"givenName": "Fixture", "familyName": "Author"}


# Everything a credential object can say about a person, including the two
# it says through a Topic entity: Quora models "Data Scientist at
# Multinational Corporations" as a `position` string plus a `company` TOPIC,
# so replacing only `translatedString` leaves the credential reconstructable
# from its parts.
_CREDENTIAL_STRINGS = ("translatedString", "position", "companyName",
                       "schoolName", "concentration", "text")
_ENTITY_KEYS = ("company", "school")


def _is_credential(node) -> bool:
    typename = node.get("__typename") or ""
    return "__isCredential" in node or typename.endswith("Credential")


def _looks_like_rich_text(value) -> bool:
    """A string that is really one of Quora's rich-text documents.

    Checked by SHAPE rather than by key name, because the same document
    appears under `content`, `title`, `profileDescriptionJson` and others —
    and a list of keys is how a profile bio ("I am the Director of …")
    survived the first attempt at this.
    """
    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return False
    try:
        parsed = json.loads(value)
    except ValueError:
        return False
    return isinstance(parsed, dict) and isinstance(parsed.get("sections"), list)


def _rewrite_payload_object(node, in_rich_text=False):
    """Replace prose, names, credentials and cited links in a payload."""
    if isinstance(node, dict):
        if _is_credential(node):
            for key in _CREDENTIAL_STRINGS:
                if isinstance(node.get(key), str):
                    node[key] = "Fixture credential"
            for key in _ENTITY_KEYS:
                entity = node.get(key)
                if isinstance(entity, dict) and isinstance(entity.get("name"), str):
                    entity["name"] = "Fixture Organisation"
        for key, value in list(node.items()):
            if key == "text" and isinstance(value, str) and value.strip():
                node[key] = _filler(len(value))
            elif key in _NAME_KEYS and isinstance(value, str):
                node[key] = _NAME_KEYS[key]
            elif key == "url" and in_rich_text and isinstance(value, str):
                # A link the author put IN their answer. Their citation, not
                # the site's, and it names third parties.
                node[key] = "https://example.invalid/fixture"
            elif key == "title" and _looks_like_rich_text(value):
                # A QUESTION, which is the site's own content and is what the
                # parser's title assertions are about. Kept verbatim, and
                # deliberately not descended into: the generic branch below
                # would replace its spans with filler and every fixture row
                # would come out titled "fixture answer body text".
                continue
            elif _looks_like_rich_text(value):
                inner = json.loads(value)
                _rewrite_payload_object(inner, in_rich_text=True)
                node[key] = json.dumps(inner, ensure_ascii=False)
            else:
                _rewrite_payload_object(value, in_rich_text)
    elif isinstance(node, list):
        for value in node:
            _rewrite_payload_object(value, in_rich_text)
    return node


_PUSH_LITERAL_RE = re.compile(
    r'inlineQueryResults\.results\[\s*"[0-9a-f]+"\s*\]\.push\(\s*(".*?")\s*\)',
    re.S)


def rewrite_payloads(html: str) -> str:
    """Anonymise the prose inside every inline payload, keeping its shape."""
    def replace(match):
        literal = match.group(1)
        try:
            payload = json.loads(json.loads(literal))
        except (ValueError, TypeError):
            return match.group(0)
        _rewrite_payload_object(payload)
        rewritten = json.dumps(json.dumps(payload, ensure_ascii=False),
                               ensure_ascii=False)
        return match.group(0).replace(literal, rewritten)

    return _PUSH_LITERAL_RE.sub(replace, html)


def _scrub_rendered(soup) -> None:
    """Rewrite the prose inside a parsed document, in place.

    The answer body and the credential are each read by the parser out of a
    node whose STRUCTURE matters — the credential in particular is
    reassembled from several spans, and the artefact that reassembly has to
    normalise (a space before a comma) only exists because Quora splits it
    that way. So the spans are kept and only their strings are replaced.
    """
    for body in soup.select(P.SELECTORS["answer_body"]):
        for node in body.find_all(string=True):
            text = str(node)
            if text.strip():
                node.replace_with(_filler(len(text)))
        # The links the author cited, which are in ATTRIBUTES rather than in
        # text and so survive the pass above untouched. Quora also repeats
        # the host in a `title` attribute.
        for anchor in body.find_all("a", href=True):
            anchor["href"] = "https://example.invalid/fixture"
            if anchor.has_attr("title"):
                anchor["title"] = "example.invalid"

    # The credential, which is a person's own statement about themselves and
    # is rendered in the card HEADER rather than in the body. Every string in
    # the header except the author link and the timestamp is credential —
    # that is exactly how `product_parser._card_credential` reads it — so the
    # same subtraction is used here, and the spans survive untouched. Keeping
    # them matters: the artefact the parser has to normalise (a space before
    # a comma) only exists because Quora splits the line across spans.
    for header in soup.select(P.SELECTORS["answer_header"]):
        skip = set()
        for anchor in header.select(P.SELECTORS["author_link"]):
            skip.update(id(n) for n in anchor.find_all(string=True))
        for anchor in header.select(P.SELECTORS["item_card"]):
            skip.update(id(n) for n in anchor.find_all(string=True))
        for node in header.find_all(string=True):
            if id(node) in skip:
                continue
            text = str(node)
            if text.strip() and not re.fullmatch(r"[\s·,|\u00a0]+", text):
                node.replace_with(_filler(len(text)))



# Quora's own long hex: image ids (`main-qimg-<32 hex>`), GraphQL query
# hashes, build shas. None of it is a credential, and all of it looks exactly
# like one to a scanner — including this repo's own CI grep, which fails on a
# bare 32-hex string anywhere in a scanned file (§10). So every 24+ character
# hex run is replaced before a fixture is written.
#
# With ONE exception, and it has to be an exception: the key of an inline
# payload push (`inlineQueryResults.results["<64 hex>"]`) is matched as hex by
# `product_parser._INLINE_PUSH_RE`, so redacting it would make the fixture's
# payload invisible to the parser and quietly turn the QUESTION fixture into
# a DOM-only one. Those keys get a short hex id instead — still hex, too
# short to trip either check.
_RESULTS_KEY_RE = re.compile(r'(inlineQueryResults\.results\[\s*")([0-9a-f]{8,})(")')
_ANY_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{24,}\b")


def dehex(html: str) -> str:
    keys = {}

    def key_replacement(match):
        original = match.group(2)
        if original not in keys:
            keys[original] = "%08x" % (len(keys) + 1)
        return match.group(1) + keys[original] + match.group(3)

    html = _RESULTS_KEY_RE.sub(key_replacement, html)
    return _ANY_LONG_HEX.sub("REDACTED-HEX", html)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
_ASSET_HEAD = (
    '<link rel="preconnect" href="https://qsbr.cf2.quoracdn.net">'
    '<link rel="preconnect" href="https://qph.cf2.quoracdn.net">'
    '<script>window.ansFrontendGlobals = window.ansFrontendGlobals || {};</script>'
)


def _head(soup, title=None) -> str:
    """The bits of the page a fixture needs besides its cards.

    Two things, and each is load-bearing for at least one check:
      * `<html lang>`, because a reader of the fixture should be able to tell
        the Spanish one from the English one at a glance;
      * enough references to Quora's own asset hosts to clear
        `served_by_quora`'s threshold of three — otherwise every trimmed
        fixture classifies as `blocked`, which is the trap this comment
        exists to stop someone rediscovering.
    """
    lang = (soup.html.get("lang") if soup.html else "") or "en"
    heading = f"<h1>{title}</h1>" if title else ""
    return f'<html lang="{lang}"><head>{_ASSET_HEAD}</head><body>{heading}'


_PUSH_RE = re.compile(
    r'<script[^>]*>[^<]*?inlineQueryResults\.results\[[^<]*?</script>', re.S)


def _payload_scripts(html: str, keep_skus, base_url: str) -> str:
    """The inline-payload <script> blocks that describe the kept answers.

    Whole script statements, verbatim (after anonymisation), rather than a
    re-emitted subset: the exact escaping of the double-encoded JSON literal
    is the thing `inline_payloads` has to survive, and rebuilding it by hand
    would test this script's idea of the format rather than Quora's.
    """
    kept = []
    for match in _PUSH_RE.finditer(html):
        block = match.group(0)
        payload_skus = set(P.answers_from_payloads(block, base_url))
        if payload_skus & set(keep_skus):
            kept.append(block)
    return "".join(kept)


def build_listing(name, filename, url, keep):
    path = CAPTURES / filename
    full = anonymise(path.read_text(encoding="utf-8"))
    full = rewrite_payloads(full)
    full = dehex(full)
    soup = BeautifulSoup(full, "html.parser")
    _scrub_rendered(soup)
    full = str(soup)
    soup = BeautifulSoup(full, "html.parser")

    anchors = soup.select(P.SELECTORS["item_card"])
    if not anchors:
        raise SystemExit(f"{filename}: no answer cards found — is this a real "
                         f"capture?")

    chosen = [P._card_of(a) for a in anchors[:keep]]
    # Make sure at least one Session-subdomain permalink is in the profile
    # fixture. See SESSION_PERMA_RE.
    if name == "PROFILE_EN" and not any(
            SESSION_PERMA_RE.search(str(c)) for c in chosen):
        for anchor in anchors:
            if SESSION_PERMA_RE.search(str(anchor)):
                chosen[-1] = P._card_of(anchor)
                break

    # A question page prints its question once, as the page's own heading,
    # and the cards do not repeat it. Without this the fixture's rows would
    # all have a null title and the page-title fallback would go untested.
    heading = None
    if P.listing_kind(url) == "question":
        node = soup.select_one(P.SELECTORS["page_question_title"])
        heading = node.get_text(" ", strip=True) if node is not None else None

    body = "".join(str(c) for c in chosen)
    kept_skus = [P.permalink_key(P._absolute(url, a.get("href")))
                 for c in chosen
                 for a in c.select(P.SELECTORS["item_card"])[:1]]
    payload = _payload_scripts(full, kept_skus, url)

    trimmed = (_head(soup, heading)
               + f'<div class="q-box fixture-feed">{body}</div>'
               + payload
               + "</body></html>")

    # THE PROOF. Parse both and compare every column of every kept answer.
    want = {r.sku: asdict(r) for r in P.parse_answers(full, url, page=1)}
    got = {r.sku: asdict(r) for r in P.parse_answers(trimmed, url, page=1)}
    invented = set(got) - set(want)
    if invented:
        raise SystemExit(f"{name}: trimmed fixture invented skus {invented}")
    for sku, row in got.items():
        for column, value in row.items():
            if column in ("scraped_at", "position"):
                continue  # per-run, and position renumbers within the trim
            if want[sku][column] != value:
                raise SystemExit(
                    f"{name}: trimming changed {column!r} on {sku}: "
                    f"{want[sku][column]!r} -> {value!r}")
    state = P.detect_page_state(trimmed, 200, url)
    if state != "content":
        raise SystemExit(f"{name}: trimmed fixture classifies as {state!r}, "
                         f"not content")
    enriched = sum(1 for r in got.values() if r["data_source"] != "dom")
    print(f"  {name}: {len(full)} -> {len(trimmed)} bytes, {len(got)} "
          f"answer(s) verified identical, {enriched} payload-backed")
    return trimmed


def build_block(filename, url):
    """Cloudflare's managed challenge, trimmed to what identifies it.

    No site keys to scrub here, and that is the finding rather than an
    oversight: a MANAGED challenge carries none — 0 `data-sitekey`
    attributes and 0 Turnstile iframes on the measured page. What it does
    carry is a rotating `cf_chl` token per request, which is not a secret but
    is long hex, so it goes through the same sweep the rest of this family
    uses.
    """
    full = (CAPTURES / filename).read_text(encoding="utf-8")
    title = re.search(r"<title>[^<]*</title>", full)
    marker = full.find("_cf_chl_opt")
    config = full[max(0, marker - 100): marker + 600] if marker > 0 else ""
    config = re.sub(r"\b[0-9a-zA-Z_.\-]{24,}\b", "REDACTED-TOKEN", config)
    config = dehex(config)
    trimmed = ("<html><head>" + (title.group(0) if title else "")
               + '<script src="https://challenges.cloudflare.com/turnstile/'
                 'v0/api.js"></script></head><body>'
               + '<div id="cf-please-wait">Enable JavaScript and cookies to '
                 'continue</div>'
               + f"<script>{config}</script>"
               + "</body></html>")

    state = P.detect_page_state(trimmed, 403, url)
    if state != "challenge":
        raise SystemExit(f"BLOCK_CF: classifies as {state!r}, not challenge")
    if P.detect_bot_challenge(trimmed) != "cloudflare":
        raise SystemExit("BLOCK_CF: lost the vendor — naming it is the point")
    if P.served_by_quora(trimmed):
        raise SystemExit("BLOCK_CF: trimmed page reads as served by Quora, "
                         "which would defeat the positive-asset check")
    leftovers = re.findall(r"\b[0-9a-fA-F]{24,}\b", trimmed)
    if leftovers:
        raise SystemExit(f"BLOCK_CF: unscrubbed long hex left in: "
                         f"{leftovers[:3]}")
    print(f"  BLOCK_CF: {len(full)} -> {len(trimmed)} bytes, vendor "
          f"{P.detect_bot_challenge(trimmed)!r}, scrubbed")
    return trimmed


def main():
    if not CAPTURES.is_dir():
        raise SystemExit(
            f"No captures directory at {CAPTURES}. Take your own with "
            f"`--dump-html` and put them there; see this file's docstring.")
    fixtures = {"_README": (
        "Generated by make_fixtures.py from real captures. Every feed fixture "
        "is verified to parse IDENTICALLY to its untrimmed original, column "
        "for column. Author names, profile slugs, credentials and answer "
        "bodies are REPLACED with placeholders — see make_fixtures.py's "
        "docstring for why and for what is kept verbatim. Do not hand-edit — "
        "regenerate.")}
    urls = {}
    print("Cutting fixtures:")
    for name, (filename, url, keep) in SOURCES.items():
        fixtures[name] = build_listing(name, filename, url, keep)
        urls[name] = url
    fixtures["BLOCK_CF"] = build_block(*BLOCK_SOURCE)
    urls["BLOCK_CF"] = BLOCK_SOURCE[1]
    # Chromium's own answer when a proxy is dead: an empty document, because
    # Playwright does not expose the interstitial. It must classify as
    # blocked, which is the inverted-detection case §18 describes — a
    # sibling repo measured 187,799 bytes of Chromium's network-error page
    # carrying the SITE'S OWN hostname in its <title>, which every text
    # marker reads as a real page and only the positive-asset check catches.
    fixtures["BROWSER_ERROR"] = "<html><head></head><body></body></html>"
    urls["BROWSER_ERROR"] = "https://www.quora.com/topic/Machine-Learning"
    fixtures["_URLS"] = urls

    OUT.write_text(json.dumps(fixtures, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    total = sum(len(v) for k, v in fixtures.items()
                if not k.startswith("_") and isinstance(v, str))
    print(f"\nWrote {OUT} ({total} bytes of fixture HTML across "
          f"{len(urls)} fixtures).")


if __name__ == "__main__":
    main()
