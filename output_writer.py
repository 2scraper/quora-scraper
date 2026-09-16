"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Three modes, one row shape
--------------------------
    --mode topic      /topic/{Slug}      a topic's answer feed
    --mode question   /{Question-Slug}   one question's answers
    --mode profile    /profile/{Slug}    one author's answers

All three yield the SAME class, because they are three feeds OF the same
thing. An answer is the only leaf Quora publishes: a question is a container
for answers, and a topic and a profile are both ways of selecting them. So
there is one dataclass here (a sibling repo needs a second for reviews; this
one does not), and `diff_runs.py` can compare a topic run against a profile
run on the columns both populate.

The row is `Answer` and not `Product`
-------------------------------------
Every other repo in this family names its row `Product` and keeps the
commerce columns even where they are null, because on a shop they are null
for a reason worth recording. Quora is not a shop in any sense: it has no
price, no currency, no discount, no stock and no brand, and there is no
measurement to write down beside those names because there is nothing on the
page to measure. Six columns null on every row of every run of every mode is
exactly what §9 says must not exist, so they are not here.

What IS kept, byte-identical and in order, is the family prefix — `source`,
`scraped_at`, `url`, `sku`, `title` — so one column name works across the
family and a consumer reading six of these repos reads the same first five
columns in the same order (§9).

Two family columns are absent for measured reasons rather than definitional
ones, and those measurements belong here:

    rating          Quora publishes no rating on an answer. It publishes
                    upvotes, which have their own column and are a count
                    rather than a score; writing them under `rating` would
                    put an 82 in a column the rest of the family fills with
                    a number out of five.
    review_count    Its nearest equivalent is `comments`, which is named
                    for what it is.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The host a row came from. It genuinely varies: Quora runs twenty-four
# language sites (www plus twenty-three), each with its own catalogue of
# questions — a Spanish question lives on es.quora.com and nowhere else — and
# answers also live on Space and Quora Session subdomains. This column is the
# only thing that says which catalogue a row is from.
# `product_parser.source_of` fills it from the URL; this is the fallback for a
# row built without one.
SOURCE_DEFAULT = "www.quora.com"


@dataclass
class Answer:
    # --- the family prefix, byte-identical and in order across the family ---
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # The answer's permalink.
    url: str = ""
    # The permalink with the scheme removed —
    # `www.quora.com/What-is-machine-learning-4/answer/Naushin-Ara-6`.
    #
    # NOT the numeric `aid`, which the site does publish and which would be
    # the tidier key: a topic feed — a whole mode — carries no numeric id
    # anywhere, measured 0 occurrences across two topic captures in two
    # languages. A key null for a third of the modes cannot be the column
    # `diff_runs.py` joins on. `answer_id` below carries the number where the
    # inline payload reached the row.
    sku: Optional[str] = None
    # The QUESTION being answered, which is this row's headline. On a topic
    # or profile feed the card prints it; on a question page the cards do not
    # repeat it and it comes from the page's own heading.
    title: Optional[str] = None

    # --- the question -----------------------------------------------------
    question_url: Optional[str] = None
    # Quora's numeric question id. From the inline payload only.
    question_id: Optional[str] = None
    # The answer's numeric id. From the inline payload only — see `sku`.
    answer_id: Optional[str] = None
    # How many answers the question has IN TOTAL, which is not how many this
    # run read: a question page renders twelve and reports 253. The gap is
    # the site's, not the scraper's, and having both numbers is what makes it
    # visible.
    answer_count: Optional[int] = None

    # --- the author -------------------------------------------------------
    author: Optional[str] = None
    author_url: Optional[str] = None
    # The line Quora prints under the name — "former Data Scientist at
    # Multinational Corporations". Per-ANSWER rather than per-author: a
    # writer picks a different credential for different answers, so this is
    # read from the answer's own field before the author's default.
    author_credential: Optional[str] = None
    author_is_verified: Optional[bool] = None

    # --- the answer -------------------------------------------------------
    # The answer body as plain text. Where the inline payload reached this
    # row this is the FULL answer; where only the card was read it is what
    # the card rendered, which Quora truncates to three lines and ends with
    # "(more)". `data_source` is the column that says which, and
    # `text_chars` makes a truncated row obvious at a glance.
    text: Optional[str] = None
    text_chars: Optional[int] = None

    # --- what the site counts --------------------------------------------
    # All four are from the inline payload only: an anonymous reader is shown
    # none of them in the DOM, measured 0 rendered upvote or view nodes
    # across five captures. They are null on a topic-feed row and populated
    # on a question or profile row, which is a property of what Quora
    # server-renders rather than of this parser.
    upvotes: Optional[int] = None
    views: Optional[int] = None
    shares: Optional[int] = None
    comments: Optional[int] = None

    # --- time -------------------------------------------------------------
    # ISO 8601 UTC, from Quora's epoch-MICROSECOND timestamps.
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # What the card printed where a date goes: "Aug 14", "2y", "3 años".
    # Kept verbatim and never parsed into a date: Quora prints a relative age
    # in the page's own language for anything older than the current year,
    # and "2y" cannot be resolved to a day. A parsed value here would be this
    # machine's calendar presented as the site's fact.
    date_text: Optional[str] = None

    # --- what Quora says about the answer ---------------------------------
    # Quora's OWN flag for an answer its assistant wrote, not an inference.
    # Measured: one of five payload-covered answers on one question page.
    is_machine_answer: Optional[bool] = None
    is_translated: Optional[bool] = None

    # --- provenance -------------------------------------------------------
    # The site language, from the host. Null on a Space subdomain, which is
    # not a language site and whose language is whatever its writers use.
    language: Optional[str] = None
    # The topic, profile or question slug the run was pointed at.
    category: Optional[str] = None
    # Which feed this row came off. Recorded because the repo reads more than
    # one kind and the mode is no longer implied by the source (§9).
    mode: Optional[str] = None
    # Which of the two views built this row: `dom+inline`, `dom`, or
    # `inline`. The columns above marked "inline payload only" are null on a
    # `dom` row, so this is the column that tells a consumer whether a null
    # upvote count means zero upvotes or means nobody looked (§8: never
    # present a guess as a fact). `diff_runs.py` reports a difference that
    # comes with a `data_source` difference as `source_changed`.
    data_source: Optional[str] = None
    # The scroll batch this row appeared in, and its position within it.
    # Unique as a pair across a run; `smoke_test.py` asserts it.
    page: Optional[int] = None
    position: Optional[int] = None


# Every mode yields the same class; see the module docstring.
ROW_CLASS_BY_MODE = {"topic": Answer, "question": Answer, "profile": Answer}

# Kept under the family's name so that code shared with the siblings — and
# anything a user wrote against one of them — keeps importing successfully.
# This repo has exactly one row class, so the alias is the same object rather
# than a second definition that could drift.
Product = Answer

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. All three of this repo's modes qualify: `sku` is
# the answer's permalink, and a feed names each answer once.
UNIQUE_BY_SKU_MODES = ("topic", "question", "profile")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. This site needs that more
    than its siblings do: a scroll batch re-parses the WHOLE feed, cards
    already read included, so every batch after the first arrives mostly
    duplicate by design. A batch that drops all of its rows is the signal
    that the feed is exhausted, which is §7's data-based terminating
    condition and the only one available here.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    All three of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On this site this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no grid; a search whose query
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: this site refuses a
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "topic", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are both recorded, and on this site BOTH of them
    genuinely vary. `mode`, because a topic row and a question row populate
    different columns: upvotes, views and creation time come from an inline
    payload a topic page does not carry, so diffing one against the other
    would report every counter as having appeared from nowhere. `source`,
    because Quora runs twenty-four language hosts and they are different
    CATALOGUES — a Spanish question lives on es.quora.com with its own slug,
    so diffing two hosts would report every row as both added and removed.
    diff_runs.py refuses a pair whose modes or language sites differ.

    `extra` carries facts about the run that are not about any single row.
    This repo puts the scroll trace there, plus `rows_new_per_batch` and
    `answers_available`, so a consumer can see how the feed grew and how far
    short of the question's own answer count the run stopped, WITHOUT
    re-reading the HTML. Note `answers_available` is NOT a gap: Quora renders
    a fraction of a large question and extends it on scroll, so a run that
    took 13 of 253 is normal rather than short — that pair is the measured
    example.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" beside it, and on
# this site the ordering between them is not a preference — it is the only
# thing that works.
#
# Quora publishes no `link[rel=next]`, no pagination control and no
# numbered anchors anywhere, in any mode. It extends the feed when the reader
# nears the bottom, and the URL convention that would let a run address batch
# 2 does not fail when you try it — `?page=2` is IGNORED and the feed comes
# back with its first items again. So a run that trusted a built URL would
# find no new sku, call the listing exhausted, and report COMPLETE holding
# one batch (§18).
#
# "no_new_products" is therefore the data-side termination condition, and on
# this site it is the ONLY one: an infinite scroll has no last page to
# recognise. "pagination_exhausted" is kept for the family's shape and is set
# when a scroll produced nothing new AND nothing behind it was refused — see
# `page_flow.advance_feed`, which is careful to keep those two apart, because
# a refused batch reported as an exhausted listing is how a throttled run
# says "complete".
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted",
                         "no_new_products")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "topic", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # answers are genuinely different things and a pipeline branches on
        # them (§8: blocked is not empty is not partial):
        #
        #   blocked          something stood between the run and the content
        #   did not complete we never reached the site — a dead proxy, a
        #                    load timeout, a refused batch
        #   completed        we asked, and the site's answer was nothing
        #
        # The middle one used to fall through to EXIT_NO_PRODUCTS, and that
        # was measured rather than reasoned about: an unreachable proxy
        # produced exit 4 — "ran fine, found nothing" — on a question with
        # 253 answers, while the sidecar beside it correctly said
        # `status: failed`, `pages_completed: 0`. A consumer branching on the
        # exit code, which is what this family says exit codes are for, would
        # have recorded an empty catalogue.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Failed run: 0 of {pages_requested} page(s) were "
                  f"fetched ({stop_reason}). This is NOT an empty result — "
                  f"nothing was read from the site at all.")
            return EXIT_PARTIAL
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
