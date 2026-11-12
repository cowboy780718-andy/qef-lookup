#!/usr/bin/env python3
"""
QEF / PFIC statement crawler.

Walks every family in sources.yaml, finds candidate statement PDFs, opens each
one to confirm it really is a PFIC Annual Information Statement, reads the tax
period out of the document text, and writes web/data/index.json.

Design notes worth knowing before you change anything here:

  * Link text lies, filenames lie, and URL patterns drift between years (Mawer
    and TD both moved their paths mid-catalogue). So a document is only admitted
    to the index after its *text* has been checked. See verify_statement().

  * We never assume a December year end. BMO and RBC run to June 30, CI to
    March 31. The period is read from the PDF and only falls back to the
    family's declared `fye` when the document is unreadable.

  * Everything is cached by URL in .cache/manifest.json with the ETag and a
    content hash, so a nightly run re-downloads almost nothing. Each document
    is looked at again on its own night every 10-30 days (recheck_due), and a
    statement whose hash changes is flagged loudly - managers do reissue
    corrected ones.

  * A manager's robots.txt is the last word. Where it forbids the documents
    but not the page that lists them, only the list is read and the links are
    published unfetched and labelled as unchecked (`listing_only`).

  * Nothing already verified is dropped because of one bad night. See
    process_pdf() for the three outcomes that are kept apart.

Usage:
    python crawl.py --since-year 2021          # everything, as the nightly job runs it
    python crawl.py --only mackenzie tdam
    python crawl.py --only mawer --refresh     # re-read a family, ignoring the cache
    python crawl.py --static-only              # skip headless-browser families
    python crawl.py --limit 25                 # sample a few PDFs per family (smoke test)
    python crawl.py --rebuild-only             # rebuild the index from the cache, no network

Tests:  python -m unittest discover -s tests   (from the repo root)
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import dataclasses
import hashlib
import html as htmlmod
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse as urlparse
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
CACHE = ROOT / ".cache"
OUT = REPO / "web" / "data" / "index.json"

# Managers that are listed but deliberately not crawled, and why:
#   manual   statements are posted, but cannot be collected for you
#   request  the manager issues statements on request and posts none
#   none     nothing about PFIC statements could be found on their site
NOT_CRAWLED = ("manual", "request", "none")
HOSTS_OUT = REPO / "web" / "data" / "hosts.json"

# Health states that mean a human should look. "blocked" belongs here: it was
# left out originally, so BlackRock sat blocked for weeks without the nightly
# report ever mentioning it.
NEEDS_ATTENTION = ("down", "error", "empty", "blocked")

# Identification, and why it looks like this.
#
# Several of these hosts run WAFs that 403 any user-agent containing "bot",
# regardless of robots.txt. Mackenzie is one, and its robots.txt explicitly
# allows crawling with no crawl-delay - so the block is a crude heuristic, not
# a stated policy. We therefore send a normal browser UA (these are public
# documents a person can download by hand from the same URL) but we do NOT
# hide: every request carries an X-Crawler header naming the project,
# we obey robots.txt, and we rate-limit per host (honouring Crawl-delay).
# If a manager wants us gone, a robots.txt rule will do it.
# The URL in BOT_ID has to be one a webmaster can actually open. It used to
# read github.com/qef-lookup, which does not exist - an identification header
# pointing nowhere is no identification at all.
BOT_NAME = "QEFLookupBot"
BOT_ID = f"{BOT_NAME}/1.1 (+https://qef-lookup.cowboy780718.workers.dev)"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "X-Crawler": BOT_ID,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Bump when SIGNALS/scoring/parsers change, to invalidate cached verdicts.
VERIFY_VERSION = 5

# Oldest tax year worth indexing. Anything earlier is left on the issuer's own
# site: the UI points you there rather than pretending the year does not exist.
# Raising this is the single biggest lever on crawl time, because the year is
# usually visible in the URL and a document can then be skipped before it is
# downloaded at all.
SINCE_YEAR = 2015

# Default spacing between requests to one host, in seconds. A host's own
# Crawl-delay always wins where it publishes one (see _host_delay). None of
# these publishers currently sets one, and ~3/sec of static PDF fetches is
# negligible for the CDNs actually serving them - but CI Global lists ~15,000
# documents, and at 1/sec that alone is four hours of nightly crawling.
PER_HOST_DELAY = 0.34
MAX_WORKERS = 6
TIMEOUT = 45

# --------------------------------------------------------------------------
# cache policy
# --------------------------------------------------------------------------
#
# A published statement does not change once it is up, so looking at it again
# every night buys almost nothing and costs a request to someone else's server.
# NEW statements are still found every night, because they are discovered by
# re-reading the manager's listing page: a URL not in the cache is always
# fetched. Looking again at a KNOWN document only ever detects a reissue.
#
# So each document is looked at again once per window, on its own night (see
# recheck_due), and with a conditional request where the host supports one, so
# an unchanged document costs a 304 rather than a download.
RECHECK_DAYS_OPEN = 10     # current and prior tax year: reissues do happen
RECHECK_DAYS_CLOSED = 30   # older years, and documents we rejected
RECHECK_DAYS_MISS = 7      # a URL that was not there, for a recent year

# A verified statement is only forgotten once its URL has positively gone
# (404/410) on this many consecutive nights. One bad night - a timeout, a 403
# from a rate limiter, a CDN hiccup - must never erase a statement we hold.
GONE_STRIKES = 3

# If a host turns away this many requests in a row, stop asking for the rest of
# the run and serve that family from the cache. Hammering a server that is
# already refusing us is how a temporary block becomes a permanent one.
BREAKER_LIMIT = 12

# A run that overruns the job's time limit is cancelled outright: nothing is
# published and the night is lost. So the crawler keeps its own, earlier
# deadline (--max-minutes). Past it, no new request is made - whatever is left
# is answered from the cache and picked up tomorrow - and the run still
# finishes, saves its cache and publishes what it has.
_deadline: float | None = None
_deferred = 0


def out_of_time() -> bool:
    return _deadline is not None and time.monotonic() > _deadline


_host_lock = defaultdict(threading.Lock)
_host_last = defaultdict(float)
_robots: dict[str, "Robots"] = {}
_host_delays: dict[str, float] = {}
_robots_lock = threading.Lock()
_manifest_lock = threading.RLock()


def utc_today():
    return datetime.now(timezone.utc).date()


def recheck_due(url: str, last_checked: str | None, window: int, today: str) -> bool:
    """Is tonight this URL's night to be looked at again?

    Every URL gets a fixed night in a `window`-day cycle, derived from the URL
    itself. That spreads the work evenly: with a 30-day window roughly 1/30th
    of the catalogue is rechecked each night, instead of the whole catalogue
    falling due on the same night a month after it was first crawled.

    The age test is the safety net for a missed run: anything not looked at
    for two full windows is due regardless of whose night it is.
    """
    day = date.fromisoformat(today)
    slot = int(hashlib.sha1(url.encode("utf-8")).hexdigest()[:8], 16) % window
    if (day.toordinal() + slot) % window == 0:
        return True
    if not last_checked:
        return True
    try:
        age = (day - date.fromisoformat(last_checked[:10])).days
    except ValueError:
        return True
    return age >= 2 * window


class Robots:
    """robots.txt, evaluated the way RFC 9309 says to: the longest match wins.

    Python's own urllib.robotparser applies the FIRST matching rule in file
    order instead. iA Clarington's file opens with "Allow: /*" and then lists
    "Disallow: /docs/", so the standard-library parser reported their documents
    as open to crawling when they are not - and the crawler duly collected a
    catalogue it had been asked to leave alone. Longest-match reads that file
    correctly: "/docs/" is more specific than "/*", so it wins.
    """

    def __init__(self, text: str = "") -> None:
        # Each group: (agent tokens, [(allow?, path pattern)], crawl delay)
        self.groups: list[tuple[list[str], list[tuple[bool, str]], float | None]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        delay: float | None = None
        in_rules = False
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:                     # previous group is complete
                    self.groups.append((agents, rules, delay))
                    agents, rules, delay, in_rules = [], [], None, False
                agents.append(val.lower())
            elif key in ("allow", "disallow"):
                in_rules = True
                if val:                          # an empty Disallow forbids nothing
                    rules.append((key == "allow", val))
            elif key == "crawl-delay":
                in_rules = True
                try:
                    delay = float(val)
                except ValueError:
                    pass
        if agents:
            self.groups.append((agents, rules, delay))

    def _groups(self, token: str) -> list[list[tuple]]:
        """The rule sets that bind us: any group naming this crawler, and "*".

        Strictly, a named group replaces "*". We apply both and let either
        veto, because the requests also carry a browser User-Agent that "*"
        rules are written for. Erring towards staying out is the point.
        """
        token = token.lower()
        named = [g for g in self.groups if any(a != "*" and a in token for a in g[0])]
        star = [g for g in self.groups if "*" in g[0]]
        return [gs for gs in (named, star) if gs]

    @staticmethod
    def _match_len(pattern: str, path: str) -> int:
        """Length of the pattern if it matches the path, else -1."""
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        rx = ".*".join(re.escape(piece) for piece in body.split("*"))
        if re.match(rx + ("$" if anchored else ""), path):
            return len(pattern)
        return -1

    def allowed(self, path: str, token: str = BOT_NAME) -> bool:
        for group_set in self._groups(token):
            best, verdict = -1, True
            for _agents, rules, _delay in group_set:
                for allow, pattern in rules:
                    n = self._match_len(pattern, path)
                    # Longest match wins; on a tie the Allow does.
                    if n > best or (n == best and n >= 0 and allow):
                        best, verdict = n, allow
            if not verdict:
                return False
        return True

    def crawl_delay(self, token: str = BOT_NAME) -> float | None:
        delays = [d for gs in self._groups(token) for _a, _r, d in gs if d is not None]
        return max(delays) if delays else None


# --------------------------------------------------------------------------
# polite fetching
# --------------------------------------------------------------------------

def _host_delay(host: str) -> float:
    """Spacing for this host: its published Crawl-delay, else the default."""
    return _host_delays.get(host, PER_HOST_DELAY)


def _throttle(url: str) -> None:
    host = urlparse.urlsplit(url).netloc
    with _host_lock[host]:
        wait = _host_delay(host) - (time.monotonic() - _host_last[host])
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.monotonic()


def robots_allows(url: str) -> bool:
    """Honour robots.txt. A host that won't serve robots.txt is treated as open."""
    parts = urlparse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    with _robots_lock:
        rp = _robots.get(base)
        if rp is None:
            text = ""
            try:
                resp = requests.get(base + "/robots.txt", headers=HEADERS, timeout=15)
                # Only a real robots.txt counts. Several of these sites answer
                # a missing file with their HTML error page and a 200.
                if resp.ok and "<html" not in resp.text[:500].lower():
                    text = resp.text
            except Exception:
                pass
            rp = _robots[base] = Robots(text)
            # Respect a published Crawl-delay, taking the stricter of the rule
            # for this crawler by name and the catch-all.
            delay = rp.crawl_delay()
            if delay is not None:
                _host_delays[parts.netloc] = max(delay, PER_HOST_DELAY)
                print(f"    robots.txt Crawl-delay for {parts.netloc}: "
                      f"{_host_delays[parts.netloc]}s")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return rp.allowed(path)


def fetch(url: str, *, binary: bool = False, extra_headers: dict | None = None,
          json_body: str | None = None):
    """Return (status, content, headers); status is a string on failure.

    `json_body` turns the request into a JSON POST, for the handful of sites
    whose document list comes from an API call rather than from the page.
    """
    if not robots_allows(url):
        return "robots-denied", None, {}
    _throttle(url)
    h = dict(HEADERS)
    if extra_headers:
        h.update(extra_headers)
    try:
        if json_body is not None:
            h.update({"Content-Type": "application/json", "Accept": "application/json"})
            r = requests.post(url, data=json_body.encode("utf-8"), headers=h,
                              timeout=TIMEOUT, allow_redirects=True)
        else:
            r = requests.get(url, headers=h, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 429:
            _slow_down(url)
        return r.status_code, (r.content if binary else r.text), dict(r.headers)
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}", None, {}


def _slow_down(url: str) -> None:
    """A 429 is the host asking us to ease off. Do so for the rest of the run."""
    host = urlparse.urlsplit(url).netloc
    with _robots_lock:
        slower = min(max(_host_delay(host) * 2, 1.0), 3.0)
        if slower > _host_delay(host):
            _host_delays[host] = slower
            print(f"    - {host} answered 429 (too many requests); "
                  f"now waiting {slower:g}s between requests to it")


# Client-hint and connection headers some WAFs insist on. Kept separate from
# HEADERS because they are only meaningful on a real HTTP/1.1 request.
_CURL_EXTRA_HEADERS = {
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}


def curl_fetch(url: str, *, binary: bool = False):
    """Third fetch path, for hosts that reject Python's TLS but serve curl.

    Manulife is the case for this: with a byte-identical header set, curl gets
    200 and 600KB while requests gets 403, and headless Chromium gets a
    359-byte block notice. Nothing here is disguised - same headers, same
    X-Crawler identification, same robots.txt check and rate limit. It is
    simply a different HTTP client.

    Deliberately NOT used to get past a real bot-management product. PIMCO
    serves Akamai Bot Manager (_abck / bm_sz cookies) and withholds content
    from automation on purpose; that family is marked manual instead.
    """
    if not robots_allows(url):
        return "robots-denied", None, {}
    _throttle(url)
    cmd = ["curl", "-sS", "-L", "--max-time", str(TIMEOUT), "--compressed",
           "-o", "-", "-w", "\n%{http_code}"]
    for k, v in {**HEADERS, **_CURL_EXTRA_HEADERS}.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 15)
        raw = p.stdout
        nl = raw.rfind(b"\n")
        if nl < 0:
            return "error:CurlNoStatus", None, {}
        body, status = raw[:nl], raw[nl + 1:].decode(errors="ignore").strip()
        return (int(status) if status.isdigit() else status,
                body if binary else body.decode("utf-8", "replace"), {})
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}", None, {}


class BrowserSession:
    """A headless Chromium kept alive for the duration of one family.

    Two distinct problems need this:

      1. Some hubs build their document list with JavaScript (RBC's cascading
         menus, Global X, PIMCO), so there is no list in the raw HTML.

      2. Some hosts reject Python's TLS fingerprint no matter what headers are
         sent, while serving the same bytes to a browser. Mackenzie does this on
         its hub; iA Clarington does it on the PDFs themselves, returning a
         404 HTML page to requests/curl and a 277KB PDF to Chromium.

    Playwright's sync API is not thread-safe, so a family using browser fetching
    processes its documents sequentially. That is slower but it is a nightly
    job, and correctness beats throughput here.
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ctx = None

    def _ensure(self) -> bool:
        if self._ctx is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                args=["--disable-blink-features=AutomationControlled"])
            self._ctx = self._browser.new_context(
                user_agent=UA, locale="en-CA",
                viewport={"width": 1440, "height": 2200},
                extra_http_headers={"X-Crawler": BOT_ID},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"    ! browser launch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return False

    def html(self, url: str) -> str | None:
        if not self._ensure():
            return None
        try:
            page = self._ctx.new_page()
            # Never block on networkidle alone: pages with analytics or chat
            # widgets poll forever and never reach it (Capital Group's tax
            # centre is one). Get the DOM, then give the network a bounded
            # chance to settle for the JS-built document lists.
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            # Consent banners: decline non-essential rather than accept. An
            # earlier version clicked "Accept", which opts into tracking on
            # every publisher's site on every nightly run - the opposite of
            # what an unattended crawler should do. Rejecting still dismisses
            # the overlay, which is all we need to read the page.
            for sel in ("#onetrust-reject-all-handler",
                        "button:has-text('Reject all')", "button:has-text('Reject All')",
                        "button:has-text('Decline')", "button:has-text('Necessary only')",
                        "button:has-text('Only essential')"):
                try:
                    page.click(sel, timeout=1200)
                    break
                except Exception:
                    pass

            # Document lists commonly hide behind accordions or "load more".
            for sel in ("button:has-text('Load more')", "button:has-text('Show all')",
                        "[aria-expanded='false']"):
                try:
                    for el in page.query_selector_all(sel)[:25]:
                        el.click(timeout=1500)
                        page.wait_for_timeout(250)
                except Exception:
                    pass
            page.wait_for_timeout(1500)
            out = page.content()
            page.close()
            return out
        except Exception as exc:  # noqa: BLE001
            print(f"    ! browser render failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None

    def get(self, url: str) -> tuple[object, bytes | None, dict]:
        """Same contract as fetch(), but issued from the browser context."""
        if not self._ensure():
            return "no-browser", None, {}
        if not robots_allows(url):
            return "robots-denied", None, {}
        _throttle(url)
        try:
            r = self._ctx.request.get(url, timeout=60_000)
            return r.status, r.body(), dict(r.headers)
        except Exception as exc:  # noqa: BLE001
            return f"error:{type(exc).__name__}", None, {}

    def close(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass
        self._pw = self._browser = self._ctx = None


# --------------------------------------------------------------------------
# PDF inspection - the part that keeps the index honest
# --------------------------------------------------------------------------

MONTHS = ("january february march april may june july august september "
          "october november december").split()

# A real PFIC Annual Information Statement carries these markers. We require a
# score of >=3 so that FAQs, overviews and covering letters do not slip in.
SIGNALS = [
    (re.compile(r"pfic\s+annual\s+information\s+statement", re.I), 3),
    (re.compile(r"annual\s+information\s+statement", re.I), 2),
    (re.compile(r"qualified\s+electing\s+fund", re.I), 2),
    (re.compile(r"1\.1295-1|section\s+1295|§\s*1295", re.I), 2),
    (re.compile(r"ordinary\s+earnings", re.I), 2),
    (re.compile(r"net\s+capital\s+gain", re.I), 2),
    (re.compile(r"form\s+8621", re.I), 1),
    (re.compile(r"per\s+(unit|share)", re.I), 1),
]
ANTI_SIGNALS = [
    (re.compile(r"frequently\s+asked\s+questions", re.I), 3),
    (re.compile(r"\bsample\b|\bspecimen\b|\bexample\s+only\b", re.I), 3),
]

PERIOD_PATTERNS = [
    re.compile(r"(?:year|period|taxable\s+year)\s+end(?:ed|ing)\s+"
               r"(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})", re.I),
    re.compile(r"end(?:ed|ing)\s+on\s+(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})", re.I),
    re.compile(r"as\s+at\s+(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})", re.I),
]

# Fund-name extraction.
#
# The naive approach - a non-greedy run of characters ending at (the "Fund") -
# fails in two ways that both corrupt the index. It starts matching as late as
# possible, so "iA Clarington ..." arrives as "A Clarington ..." and "Russell
# Investments ..." as "ussell Investments ...". And when the anchor phrase
# appears in boilerplate ("treat the mutual fund as a Qualified Electing Fund")
# it happily returns "Electing Fund" - which collapsed all 12,589 CI documents
# into one nonexistent fund.
#
# So: find the anchor, read *backwards* to a real delimiter, then clean up.

# Also the two-name form Mackenzie used in 2021:
#   (THE "FUND", or "MDVD/MDVD.U")
_FUND_ANCHOR = re.compile(
    r"""\(\s*(?:the\s+)?["“”']?\s*Fund\s*["“”']?\s*(?:,?\s*or\s+[^()]{1,40})?\)""", re.I)

# Headline forms that name the fund outright; tried first.
# Only the explicit addressee line. A looser "Annual Information Statement
# of/for ..." rule was tried and had to be removed: it matched the document's
# own title block and returned "Year Ending December 31, 2024 1) This
# Information Statement ..." for five separate families that the anchor rule
# had been handling correctly.
_NAME_LEAD = [
    re.compile(r"(?:SECURITY\s*HOLDERS|SECURITYHOLDERS|UNITHOLDERS|SHAREHOLDERS|HOLDERS)"
               r"\s+OF\s*:?\s*(?:the\s+)?(?:Fund\s+)?(.{4,110}?)"
               # Guardian runs straight on: "...Shareholders of Guardian
               # Canadian Equity Select Fund This statement is provided to..."
               r"\s*(?:\(|who\s+have|This\s+statement|$)", re.I),
]

# "This Information Statement applies to the taxable year of <fund> commencing
# on ..." - Mackenzie's and Mawer's statements name the fund nowhere else on
# the page that can be read reliably.
_NAME_SUBJECT = re.compile(
    r"applies\s+to\s+the\s+taxable\s+year\s+of\s+(?:the\s+)?(.{4,110}?)"
    r"\s+(?:commencing|beginning|for\s+the\s+(?:year|period)|from\s|ending\s|\()", re.I)

# CI's covering-letter layout puts the fund in a table, after a run of repeated
# column headers that the text layer duplicates, with the series in
# parentheses immediately after the name. The series is not decoration: CI
# issues roughly 30 documents per fund per year, one per series, identical
# except for the series code and the per-unit figures. Dropping it merged them
# into ~30 indistinguishable rows and made picking the right one impossible.
#
# Only the lead-in is case-insensitive. The name itself must START with a
# capital: with the whole pattern case-insensitive, "distributions are
# converted into U.S. dollars ... (Source: Reuters Forex)" matched, and eight
# Mackenzie statements were filed under that sentence as if it were a fund.
_NAME_TABLE = [
    re.compile(r"(?i:(?:Distributions\s+){1,4})([A-Z][^()\n]{3,90}?)\s*\(([^()\n]{1,28})\)"),
    # Same table when the text layer emits the headers first and the row after
    # the numbered paragraphs: "...are as follows: CI Private Markets Growth
    # Fund (W) 0.50881 ..."
    re.compile(r"(?i:are\s+as\s+follows\s*:)\s*([A-Z][^()\n]{3,90}?)\s*"
               r"\(([^()\n]{1,28})\)\s+[\d.-]"),
]

# RBC and TD lead with the fund name, then the document title. RBC's includes
# the series ("... Fund - Series F"), which matters: different series carry
# different per-unit figures, so collapsing them would be wrong.
_NAME_TITLE_LEAD = re.compile(
    r"^(.{6,140}?)\s*PFIC\s+(?:Annual\s+)?Information\s+Statement", re.I)

# Anything matching these is boilerplate, not a fund.
_NAME_JUNK = re.compile(
    r"^(?:the\s+)?(?:qualified\s+)?"
    r"(?:electing|mutual|requested|below\s+listed|following|underlying|"
    r"mentioned|who\b|fund\b)"
    r"|^(?:fund|the\s+fund|statement|table|information)$"
    r"|^internal\s+revenue\s+service"
    r"|\b(?:tax\s+filing\s+requirement|ordinary\s+earnings|"
    r"information\s+statement|year\s+end(?:ing|ed)|"
    r"qualified\s+electing\s+fund|\bmay\s+elect\b|\belect\s+to\s+treat\b|"
    r"please\s+find|\bis\s+provided\b|\benclosed\b|united\s+states\s+persons?|"
    r"passive\s+foreign\s+investment\s+company|u\.s\.\s+persons?\b)",
    re.I)

# Stripped when they lead a captured name.
_NAME_PREFIX_JUNK = re.compile(
    r"^(?:.*?(?:EXCHANGE\s+TRADED\s+FUNDS?|IMPORTANT\s+TAX\s+NOTICE[^:]*:?|"
    r"TD\s+Asset\s+Management(?=\s+TD)|"
    r"Attention\s*:\s*U\.?S\.?\s+(?:Unit|Share|Security)\s*holders?\s+of|"
    r"TO\s+U\.?S\.?\s+(?:PERSONS|INVESTORS|TAXPAYERS)[^:]*:?)\s*)", re.I)
# A letter date that trails the name: "... Senior Debt Fund March 31, 2026".
_NAME_TRAILING_DATE = re.compile(
    r"\s+(?:" + "|".join(MONTHS) + r")\s+\d{1,2},?\s+\d{4}$", re.I)
# ...or leads it: Purpose dates its covering letter on the line above the fund,
# and all eighty of its funds were listed as "March 31, 2026 Purpose ...".
_NAME_LEADING_DATE = re.compile(
    r"^(?:" + "|".join(MONTHS) + r")\s+\d{1,2},?\s+\d{4}\s+", re.I)
TICKER_PATTERN = re.compile(r"\(([A-Z]{2,5}(?:\.[A-Z]{1,2})?)\)")


_pdf_errors: dict[str, int] = defaultdict(int)

# Tickers learned from WHERE a document was found rather than from the
# document: Evolve's statements name no ticker and neither do their file
# names, but each is linked from /product/{ticker}/.
_doc_tickers: dict[str, str] = {}

_GLYPH_NAME = re.compile(r"/uni([0-9A-Fa-f]{4})")


def _repair_text(text: str) -> str:
    """Undo a text-layer fault where each letter is written as its glyph name.

    Some of Starlight's statements extract as
    "/uni0050/uni0046/uni0049/uni0043/uni0020/uni0041..." - which is "PFIC A..."
    spelled out one Unicode code point at a time. Unrepaired, a perfectly good
    statement scores zero and is rejected as not being one.
    """
    if "/uni" not in text:
        return text
    return _GLYPH_NAME.sub(lambda m: chr(int(m.group(1), 16)), text)



def pdf_text(blob: bytes, pages: int = 3) -> str:
    """Extract text from the first few pages.

    Failures are counted and reported at the end of the run rather than
    swallowed. A silent empty string here once cost a whole afternoon: several
    publishers (Fidelity, BlackRock, iA Clarington) ship permissions-encrypted
    PDFs that pypdf cannot open without `cryptography` installed, and the only
    symptom was every document scoring zero.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        _pdf_errors["pypdf not installed"] += 1
        return ""
    try:
        reader = PdfReader(io.BytesIO(blob))
        return _repair_text("\n".join((reader.pages[i].extract_text() or "")
                                      for i in range(min(pages, len(reader.pages)))))
    except Exception as exc:  # noqa: BLE001
        _pdf_errors[f"{type(exc).__name__}: {str(exc)[:80]}"] += 1
        return ""


def pdf_pages(blob: bytes, limit: int = 400) -> list[str]:
    """Text of every page, for documents that bundle many funds together."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(blob))
        return [_repair_text(reader.pages[i].extract_text() or "")
                for i in range(min(limit, len(reader.pages)))]
    except Exception as exc:  # noqa: BLE001
        _pdf_errors[f"{type(exc).__name__}: {str(exc)[:80]}"] += 1
        return []


def split_document(pages: list[str], how: dict) -> list[dict]:
    """Find the individual funds inside one document that covers many.

    Two publishers do not issue one document per fund:

      * Guardian staples every fund's three-page statement into a single PDF
        per year - 86 pages, 29 funds. Indexed as one document it surfaced as
        "Guardian Canadian Equity Select Fund", the first fund in it, and the
        other 28 could not be found at all.  -> by: pages

      * Global X publishes one table covering every ETF, a row each.
                                              -> by: rows

    Each fund found becomes its own entry pointing at the page it is on, so a
    search for the fund lands on the right page of the right document.

    Opt-in per family (sources.yaml `split:`), never guessed: a fund-of-funds'
    own statement also runs to many pages naming other funds, and those are the
    parent's indirect amounts - not statements for the funds named.
    """
    parts: list[dict] = []
    seen: set[str] = set()
    if how.get("by") == "pages":
        for i, text in enumerate(pages, 1):
            ok, _score = verify_statement(text)
            name = parse_fund_name(text) if ok else None
            if not name or slug(name) in seen:
                continue
            seen.add(slug(name))
            parts.append({"name": name, "page": i, "period_end": parse_period(text),
                          "tickers": parse_tickers(text, "")})
    elif how.get("by") == "rows":
        # A row is "<TICKER> <fund name>". Anchoring the name on the family's
        # brand keeps this honest: without it "MSCI EAFE Covered Call ETF"
        # reads as a fund called "EAFE Covered Call ETF" with the ticker MSCI.
        prefix = re.escape(how["name_prefix"])
        row = re.compile(
            r"(?<![A-Za-z0-9.&/])([A-Z]{2,5}(?:\.[A-Z]{1,2})?)\s+"
            r"(" + prefix + r"(?:(?!\d+\.\d)[^\n]){3,110}?\b(?:ETF|Fund|Portfolio|Pool))"
            r"(?:\*+)?(?=\s|$)")
        for i, text in enumerate(pages, 1):
            for m in row.finditer(" ".join(text.split())):
                ticker, name = m.group(1), " ".join(m.group(2).split())
                if ticker in _CODE_STOPWORDS or ticker in seen:
                    continue
                seen.add(ticker)
                parts.append({"name": name, "page": i, "period_end": None,
                              "tickers": [ticker]})
    return parts if len(parts) >= 2 else []


def _suspect_name(name: str | None) -> bool:
    """A cached fund name that the current parser would not have produced."""
    return (not name or len(name) > 100 or bool(_NAME_JUNK.search(name))
            or name.count("(") != name.count(")")
            or bool(_NAME_LEADING_DATE.match(name))
            or bool(re.search(r"^investors\s+in\b|^text\d|this\s+statement|^attention\b|converted\s+into|"
                              r"\b(?:19|20)\d\d$", name, re.I)))


def verify_statement(text: str) -> tuple[bool, int]:
    if not text.strip():
        return False, 0
    score = sum(w for rx, w in SIGNALS if rx.search(text))
    score -= sum(w for rx, w in ANTI_SIGNALS if rx.search(text))
    return score >= 3, score


def parse_period(text: str) -> str | None:
    # Text layers sometimes detach a word's first letter: Global X's 2025
    # statement reads "For the Y ear Ended April 30, 2025", and unrepaired the
    # whole document came out with no tax year at all. "A" and "I" are left
    # alone because they are words in their own right.
    text = re.sub(r"(?<![A-Za-z&.])([B-HJ-Z])\s(?=[a-z]{2})", r"\1", text or "")
    for rx in PERIOD_PATTERNS:
        m = rx.search(text)
        if m:
            month = MONTHS.index(m.group(1).lower()) + 1
            day, year = int(m.group(2)), int(m.group(3))
            if 1990 <= year <= 2100 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _clean_name(raw: str) -> str | None:
    name = re.sub(r"\s+", " ", raw or "").strip()
    name = re.sub(r"^Page\s+\d+\s+of\s+\d+\s*", "", name, flags=re.I)
    # A PDF form-field label that leaks into the text layer ("Text75 MACKENZIE...").
    name = re.sub(r"^.*?\bText\d+\s+", "", name)
    # The sentence the (the "Fund") marker sits in: "Investors in <fund> (the "Fund")".
    name = re.sub(r"^Investors\s+in\s+(?:the\s+)?", "", name, flags=re.I)
    # The (the "Fund") marker ends the name wherever it appears, not only at
    # the end - Fidelity continues straight into boilerplate after it.
    name = _FUND_ANCHOR.split(name)[0].strip()
    name = re.sub(r"\(\s*(?:the\s+)?[\"“”']?\s*Fund\s*[\"“”']?\s*\)\s*$",
                  "", name, flags=re.I).strip()
    name = re.sub(r"\s*\(\s*formerly.*$", "", name, flags=re.I)
    # PDF text layers sometimes detach the first glyph: "R ussell Investments",
    # "M ulti-Asset", "I A Clarington". Rejoin a lone leading capital.
    name = re.sub(r"^([A-Z])\s+(?=[a-z]{2})", r"\1", name)
    name = _NAME_PREFIX_JUNK.sub("", name)
    name = _NAME_TRAILING_DATE.sub("", name)
    name = _NAME_LEADING_DATE.sub("", name)
    # RBC writes "Fund - Series F" some years and "Fund – Series F" others.
    name = re.sub(r"\s[–—]\s(?=(?:Series|Class)\b)", " - ", name)
    name = re.sub(r"^(?:the\s+)", "", name, flags=re.I)
    name = re.sub(r"^Fund\s+(?=[A-Za-z])", "", name)
    # Drop a trailing exchange ticker - "(ETHX.B)", "(XSH)" - but keep
    # descriptive parentheticals such as "(CAD-Hedged)".
    name = re.sub(r"\s*\([A-Z]{2,5}(?:\.[A-Z]{1,2})?\)\s*$", "", name)
    name = name.strip(" .,:;-–—\"'“”(")
    # Trim a stray closing bracket, but not the one that closes "(CAD-Hedged)".
    while name.endswith(")") and name.count(")") > name.count("("):
        name = name[:-1].rstrip(" .,:;-–—")
    if not (6 <= len(name) <= 120):
        return None
    if _NAME_JUNK.search(name):
        return None
    if not re.search(r"[A-Za-z]{3}", name):
        return None
    return name


def parse_fund_name(text: str, extra_pattern: str | None = None) -> str | None:
    """Best-effort fund name, tried in order of reliability."""
    flat = " ".join((text or "").split())
    # Some text layers detach the opening glyph of a heading, yielding
    # "R ussell Investments", "M ulti-Asset", "T D One-Click", "I A Clarington".
    # Rejoin before any splitting, or the stray letter ends up in a different
    # chunk and the name silently loses its first character.
    flat = re.sub(r"(?<![A-Za-z&.])([A-Z])\s(?=[a-z]{2})", r"\1", flat)
    # Join two *standalone* capitals ("T D One-Click" -> "TD One-Click",
    # "I A Clarington" -> "IA Clarington"). Both letters must be lone tokens,
    # or this eats real spaces: "PH&N Absolute" became "PH&NAbsolute" and
    # "T D One-Click" became "T DOne-Click" on the looser version.
    flat = re.sub(r"(?<![A-Za-z&.])\b([A-Z])\s([A-Z])\b(?![A-Za-z])", r"\1\2", flat)
    # The same fault inside capitalised headings: "P FIC Annual Information
    # Statement", "I MPORTANT TAX NOTICE". Unrepaired, the title that marks
    # where the fund name ends is not recognised, and Ninepoint's statements
    # were named "Investors in Ninepoint ..." from a sentence further down.
    flat = re.sub(r"(?<![A-Za-z&.])([A-Z])\s(?=[A-Z]{3,}\b)", r"\1", flat)
    flat = re.sub(r"\bPFI\sC\b", "PFIC", flat)

    # 0. A family-specific override from sources.yaml wins outright.
    if extra_pattern:
        m = re.search(extra_pattern, flat, re.I)
        if m and (n := _clean_name(m.group(1))):
            return n

    # 1. Name preceding the document title (RBC, TD). Checked before the
    #    addressee line because it preserves the series suffix.
    m = _NAME_TITLE_LEAD.search(flat[:400])
    if m and (n := _clean_name(m.group(1))):
        return n

    # 2. Explicit "...HOLDERS OF: <name>" headline.
    for rx in _NAME_LEAD:
        m = rx.search(flat)
        if m and (n := _clean_name(m.group(1))):
            return n

    # 3. Read backwards from the (the "Fund") anchor to a real delimiter, so the
    #    first character of the name survives.
    for m in _FUND_ANCHOR.finditer(flat):
        before = flat[max(0, m.start() - 160):m.start()]
        # Split on strong boundaries: a colon, a sentence end, or 2+ spaces.
        chunk = re.split(r":\s+|(?<=[a-z])\.\s+|\s{2,}|•", before)[-1]
        if (n := _clean_name(chunk)):
            return n

    # 4. "...applies to the taxable year of <fund> commencing on..."
    m = _NAME_SUBJECT.search(flat)
    if m and (n := _clean_name(m.group(1))):
        return n

    # 5. CI-style table layout. Carry the series through, formatted the way RBC
    #    writes it so both families read alike in the UI.
    for rx in _NAME_TABLE:
        m = rx.search(flat)
        if not (m and (n := _clean_name(m.group(1)))):
            continue
        series = re.sub(r"\s+", " ", m.group(2)).strip()
        if not series or series.lower() in ("the", "fund", "usd", "cad"):
            return n
        # CI writes series codes ("AT5") and descriptive labels
        # ("ETF C$ Series") in the same slot. Keep the label verbatim; format
        # a bare code the way RBC writes it so both read alike.
        if "series" in series.lower() or " " in series:
            return f"{n} ({series})"
        return f"{n} - Series {series.upper()}"
    return None


# Fund codes / tickers hiding in URLs. Each publisher encodes them differently,
# and for several (Mackenzie, BlackRock, Vanguard) the URL is the *only* place
# the ticker appears - it is not in the document text at all.
URL_CODE_PATTERNS = [
    re.compile(r"pfic[-_](?:stmt[-_])?\d{4}[-_]([a-z]{2,6})[-_.]", re.I),   # mackenzie, blackrock
    re.compile(r"/([A-Z]{2,5})_\d+_PFIC", re.I),                            # vanguard
    re.compile(r"/pfic[-_]([a-z]{2,6})\.pdf", re.I),                        # vanguard legacy
    re.compile(r"/([A-Z0-9]{4})_[A-Za-z]", ),                               # russell fund codes
    # National Bank names its files by FundSERV code: MFIN4001-pfic-2025.pdf.
    # That is the only family so far where the code the client statement shows
    # is recoverable straight from the document URL.
    re.compile(r"/([A-Z]{2,4}\d{3,5})-pfic-", re.I),
    # RBC names its statements by fund code too: /pfic/2025/rbf1664_e.pdf.
    # Between them RBC and National Bank cover ~325 funds whose codes are
    # recoverable with no extra requests at all.
    re.compile(r"/pfic/\d{4}/([a-z]{2,4}\d{2,5}[a-z]?)_", re.I),
]
_CODE_STOPWORDS = {"THE", "FUND", "ETF", "USD", "CAD", "PFIC", "PFICS", "QEF", "IRS",
                   "IRC", "CRA", "AIS", "US", "EN", "FR", "STMT", "ENV1", "SE", "PDFUA",
                   "TSX", "NAV", "INC", "LTD", "LLC", "LP", "CDN", "FAQ"}


def parse_tickers(text: str, url: str) -> list[str]:
    found = set(TICKER_PATTERN.findall(text[:1200]))
    for rx in URL_CODE_PATTERNS:
        m = rx.search(url)
        if m:
            found.add(m.group(1).upper())
    return sorted(c for c in found if c not in _CODE_STOPWORDS and not c.isdigit())


def tidy_name(name: str) -> str:
    """Publishers shout. Title-case all-caps names but keep real acronyms."""
    if name and name.count("(") > name.count(")"):
        name += ")"                     # "(CAD-Hedged" - the bracket was trimmed off
    if not name or not name.isupper():
        return name
    keep = {"ETF", "ETFS", "US", "USA", "UK", "EAFE", "ESG", "REIT", "TSX", "GIC",
            "MSCI", "S&P", "EM", "HISA", "ADR", "II", "III", "IV", "CAD", "USD",
            "U.S.", "U.S", "NASDAQ", "AI", "TD", "RBC", "BMO", "CI", "NBI", "IA"}

    def piece(p: str) -> str:
        if not p or p in keep or "." in p:      # "U.UN", "U.S." stay as written
            return p
        return p[0] + p[1:].lower()

    def word(w: str) -> str:
        # Keep the brackets and punctuation; title-case what is inside them,
        # treating "-" and "/" as word breaks so "EX-NORTH" becomes "Ex-North"
        # and "LONG/SHORT" becomes "Long/Short".
        lead = w[:len(w) - len(w.lstrip("(\"'"))]
        tail = w[len(w.rstrip(").,;:\"'")):]
        core = w[len(lead):len(w) - len(tail)]
        if core in keep:
            return w
        return lead + re.sub(r"[^-/]+", lambda m: piece(m.group(0)), core) + tail

    return " ".join(word(w) for w in name.split())


# --------------------------------------------------------------------------
# crawl
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Statement:
    family: str
    url: str
    fund_name: str | None
    tickers: list[str]
    period_end: str | None
    tax_year: int | None
    sha256: str
    bytes: int
    confidence: int
    verified: bool
    first_seen: str
    last_seen: str
    # Not every manager publishes a file. Hamilton puts its statements in an
    # HTML table on the page, so there is nothing to download - the table row
    # IS the statement. Those are recorded with fmt="html" and the published
    # figures carried verbatim, so the site can hand over a working paper
    # rather than a dead link.
    fmt: str = "pdf"
    figures: dict | None = None
    # Lower-tier PFIC amounts a fund-of-funds publishes alongside its own
    # figures ("Direct Investee Funds of the Fund"). They belong to the parent
    # fund's statement and are meaningless on their own.
    investees: list | None = None
    # Identity that survives a rename. Fund names drift between years ("Yield
    # Maximizer" / "YIELD MAXIMIZER(TM)", "U.S" / "U.S.") and funds are renamed
    # outright, so where a ticker and class are published they define the
    # fund, and every year's statement lands on one row.
    fund_key: str | None = None
    # Set when this is one fund out of a document covering many:
    # {"page": 37, "pages": 86}. The URL then carries "#page=37".
    part: dict | None = None


def load_manifest() -> dict:
    CACHE.mkdir(exist_ok=True)
    p = CACHE / "manifest.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001
            # Say so. An unreadable cache means re-downloading the whole
            # catalogue, and that should never happen without a trace.
            print(f"! cache unreadable ({type(exc).__name__}); starting empty",
                  file=sys.stderr)
    return {}


def save_manifest(m: dict) -> None:
    """Write the cache atomically: to a temporary file, then swap it in.

    Writing in place meant a run cancelled mid-write left half a JSON file,
    which load_manifest() then discarded - silently turning the next night
    into a full re-download of every document.
    """
    CACHE.mkdir(exist_ok=True)
    with _manifest_lock:
        payload = json.dumps(m, separators=(",", ":"))
    tmp = CACHE / "manifest.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, CACHE / "manifest.json")


def derive_candidates(fam: dict, browser: "BrowserSession | None" = None,
                      since: int = SINCE_YEAR) -> tuple[list[str], dict]:
    """Build candidate URLs for publishers that don't link their statements.

    BlackRock is the case this exists for: its tax centre links only a "Fund
    List - PFIC Statements" roster, not the statements themselves, which live at
    a predictable path keyed by ticker and year. So we read the roster, harvest
    the fund-name/ticker pairs, and probe the known URL shapes.

    Returns (urls, {ticker: fund_name}) - the name map is worth keeping because
    the roster names these funds better than the statements do.
    """
    d = fam.get("derive")
    if not d:
        return [], {}

    st, blob, _ = fetch(d["list_url"], binary=True)
    if not (st == 200 and _is_pdf(blob)):
        st, blob, _ = curl_fetch(d["list_url"], binary=True)
    if not (st == 200 and _is_pdf(blob)) and browser:
        st, blob, _ = browser.get(d["list_url"])
    if not (st == 200 and _is_pdf(blob)):
        print(f"    ! derive list unavailable ({st})")
        return [], {}

    text = " ".join(pdf_text(blob, pages=12).split())
    pairs = re.findall(d["pair_pattern"], text)
    names = {}
    for name, ticker in pairs:
        names.setdefault(ticker.upper(), re.sub(r"\s+", " ", name).strip(" .,-"))
    if not names:
        print("    ! derive pattern matched nothing in the list document")
        return [], {}

    # The current calendar year plus `years_back` before it. The current year
    # is included even though a December-year-end fund cannot have published
    # it yet: its misses are cached and re-probed weekly, which is exactly how
    # next season's statements get noticed.
    this_year = utc_today().year
    oldest = max(since, this_year - int(d.get("years_back", 5)))
    years = list(range(this_year, oldest - 1, -1))
    urls = []
    for ticker in names:
        for year in years:
            for tmpl in d["url_templates"]:
                urls.append(tmpl.format(year=year, ticker=ticker,
                                        ticker_lower=ticker.lower()))
    print(f"    derived {len(names)} tickers x {len(years)} years -> {len(urls)} candidates")
    return urls, names


# Years appear in these URLs as a path segment (/2024/pfic/), a filename prefix
# (2022_PFIC_...) or an embedded token (pfic-2021-mft-en.pdf, ...-2024.pdf).
_URL_YEAR = re.compile(r"(?:^|[^0-9])(19[89]\d|20[0-4]\d)(?:[^0-9]|$)")


def url_year(url: str) -> int | None:
    """Best-effort tax year from a URL. None when it cannot be told."""
    held = archive_parts(url)
    if held:
        url = held[1]          # the year of the document, not of the capture
    years = [int(y) for y in _URL_YEAR.findall(urlparse.urlsplit(url).path)]
    return max(years) if years else None


# --------------------------------------------------------------------------
# earlier years
# --------------------------------------------------------------------------
#
# Many managers keep only the latest statement on their site: CIBC and NEI
# overwrite one file per fund every year, Renaissance and Harvest move to a new
# folder and stop linking the old one. The earlier years are still wanted - a
# late or amended Form 8621 needs them - so they are looked for in two places:
#
#   1. at the manager, at the addresses it used before. Often the old file is
#      still there and merely unlinked; that is the best copy there is.
#   2. in the Internet Archive (web.archive.org), which keeps dated copies of
#      public web files. An archived copy is the manager's own file as it was
#      served on that date, byte for byte. It goes through exactly the same
#      verification as any other document, and the index marks it as an
#      archived copy wherever it is shown.
#
# The archive's list of what it holds (its "CDX" index) is asked for once a
# month per manager; a copy, once read, is never fetched again - it cannot
# change.

ARCHIVE_HOST = "web.archive.org"
ARCHIVE_CDX = "https://web.archive.org/cdx/search/cdx"
ARCHIVE_LIST_DAYS = 30
ARCHIVE_DELAY = 3.0            # seconds between requests to the archive; it throttles hard
_ARCHIVED = re.compile(r"^https://web\.archive\.org/web/(\d{14})(?:id_)?/(https?://.+)$")


def archive_parts(url: str) -> tuple[str, str] | None:
    """(capture time, the manager's own address) of an archived copy, else None."""
    m = _ARCHIVED.match(url or "")
    return (m.group(1), m.group(2)) if m else None


def archive_url(timestamp: str, original: str, raw: bool = True) -> str:
    """Address of a capture. `raw` is the file exactly as the manager served it."""
    return f"https://{ARCHIVE_HOST}/web/{timestamp}{'id_' if raw else ''}/{original}"


def _https(url: str) -> str:
    return re.sub(r"^http://([^/:]+)(?::80)?/", r"https://\1/", url)


def archive_captures(fam: dict, manifest: dict, today: str, since: int) -> list[list[str]]:
    """What the archive holds under this manager's document folder.

    Returns [capture time, original address, content digest] rows, one per
    distinct version of each file. Kept in the cache and refreshed monthly.
    """
    cfg = fam["archive"]
    key = f"archive-list::{fam['id']}"
    prev = manifest.get(key) or {}
    held = prev.get("captures") or []
    age = ((date.fromisoformat(today) - date.fromisoformat(prev["checked"])).days
           if prev.get("checked") else None)
    asked = prev.get("prefix") == cfg["prefix"]
    if asked and age is not None and age < ARCHIVE_LIST_DAYS:
        return held
    if out_of_time():
        return held
    query = urlparse.urlencode([
        ("url", cfg["prefix"]), ("matchType", "prefix"),
        ("filter", "statuscode:200"), ("filter", "mimetype:application/pdf"),
        ("from", str(since)), ("collapse", "digest"),
        ("fl", "timestamp,original,digest"), ("limit", "20000")])
    status, text, _ = fetch(f"{ARCHIVE_CDX}?{query}")
    if status != 200 or text is None:
        print(f"    ! the archive's list could not be read ({status}); "
              f"using the {len(held)} capture(s) already known")
        return held
    seen: dict[tuple[str, str], list[str]] = {}
    for line in text.splitlines():
        cols = line.split(" ")
        if len(cols) != 3 or not re.fullmatch(r"\d{14}", cols[0]):
            continue                      # an error page, not a listing
        seen.setdefault((cols[1], cols[2]), cols)      # the first capture of each version
    rows = sorted(seen.values())
    if not rows and held:
        return held                       # an empty answer does not un-know what we knew
    _put(manifest, key, {"family": fam["id"], "archive_list": True, "prefix": cfg["prefix"],
                         "checked": today, "captures": rows})
    print(f"    the archive holds {len(rows)} capture(s) under this manager's folder")
    return rows


def archive_candidates(fam: dict, manifest: dict, today: str, since: int,
                       listed: list[str], include: list[str],
                       exclude: list[str]) -> tuple[list[str], list[str]]:
    """Earlier documents worth looking for: (at the manager, in the archive).

    `listed` is what the manager's page links tonight.
    """
    cfg = fam["archive"]
    inc = [re.compile(p) for p in (cfg.get("include") or include)]
    exc = [re.compile(p) for p in (cfg.get("exclude") or exclude)]
    now = set(listed)
    at_manager: list[str] = []
    copies: list[str] = []
    for stamp, original, _digest in archive_captures(fam, manifest, today, since):
        original = _https(original)
        if not any(p.search(original) for p in inc) or any(p.search(original) for p in exc):
            continue
        if too_old(original, since):
            continue
        dated = url_year(original) is not None
        if original in now:
            if dated:
                # Still posted, at an address with its year in it: the copy
                # and tonight's document are the same statement.
                continue
        else:
            if cfg.get("only_listed"):
                # A folder shared with every other kind of document
                # (Manulife): only files the PFIC page links are statements.
                continue
            if original not in at_manager:
                at_manager.append(original)
        copies.append(archive_url(stamp, original))
    return at_manager, copies


def earlier_year_urls(links: list[str], since: int) -> list[str]:
    """The same addresses with an earlier year in them.

    Dynamic and Harvest file statements under /pfic/2024/ and /tax/2025/PFIC/
    and link only the latest folder; last year's file is often still at last
    year's address. Asking costs one request per address, once: "not there" is
    remembered.
    """
    out: list[str] = []
    for u in links:
        parts = urlparse.urlsplit(u)
        years = set(_URL_YEAR.findall(parts.path))
        if len(years) != 1:
            continue
        year = years.pop()
        for y in range(int(year) - 1, since - 1, -1):
            path = re.sub(rf"(?<![0-9]){year}(?![0-9])", str(y), parts.path)
            out.append(urlparse.urlunsplit(parts._replace(path=path)))
    return out


def too_old(url: str, since: int) -> bool:
    """True only when the URL positively shows a year older than the floor.

    Deliberately one-sided: a URL with no readable year is kept and decided on
    the period parsed out of the document. Guessing 'old' from an unreadable URL
    would silently drop current statements.
    """
    y = url_year(url)
    return y is not None and y < since


def fetch_any(url: str, browser: "BrowserSession | None" = None,
              allow_browser: bool = True) -> str | None:
    """Fetch a page through whichever transport this host will actually serve.

    Publishers block in every combination: Mackenzie rejects Python but serves
    Chromium, Manulife rejects Python and Chromium but serves curl, iA
    Clarington needs the browser for its documents. Trying all three in turn
    removes the guesswork.
    """
    st, body, _ = fetch(url)
    if isinstance(st, int) and st == 200 and body:
        return body
    st, body, _ = curl_fetch(url)
    if isinstance(st, int) and st == 200 and body:
        return body
    if allow_browser and browser is not None:
        return browser.html(url)
    return None


def follow_pages(hub_html: str, hub_url: str, fam: dict, browser, args) -> list[str]:
    """Collect document links from second-level pages linked off the hub.

    Some managers publish one landing page per fund instead of linking
    documents directly - Manulife has 61 of them, each holding a single
    statement PDF. Without following them the family looks empty when it is
    actually fully populated.
    """
    cfg = fam.get("follow")
    if not cfg:
        return []
    page_rx = re.compile(cfg["page_pattern"])
    # "/etf/hdif" and "/etf/hdif/" are one page; without the rstrip Harvest's
    # fund pages were each read twice.
    by_page: dict[str, str] = {}
    for h in re.findall(r'href=["\']([^"\']+)["\']', hub_html):
        if page_rx.search(h):
            absolute = urlparse.urljoin(hub_url, h)
            # Prefer the trailing-slash form where both are linked: asking for
            # the other one costs a redirect on every page, every night.
            if absolute.endswith("/") or absolute.rstrip("/") not in by_page:
                by_page[absolute.rstrip("/")] = absolute
    pages = sorted(by_page.values())
    if args.limit and len(pages) > args.limit:
        step = len(pages) / args.limit
        pages = [pages[int(i * step)] for i in range(args.limit)]
    if not pages:
        return []
    print(f"    following {len(pages)} per-fund page(s)")

    inc = [re.compile(x) for x in (fam.get("link_include") or [])]
    ticker_rx = re.compile(cfg["ticker_pattern"]) if cfg.get("ticker_pattern") else None
    found: set[str] = set()
    for i, page in enumerate(pages, 1):
        if out_of_time():
            print(f"      time budget reached after {i - 1}/{len(pages)} pages")
            break
        body = fetch_any(page, browser, allow_browser=not args.static_only)
        if not body:
            continue
        page_ticker = ticker_rx.search(page) if ticker_rx else None
        # Scan href attributes AND any quoted string that looks like a document
        # URL. Purpose renders its fund pages from embedded JSON, so the
        # statement link never appears as an href and an href-only scan
        # followed all 85 pages and found nothing.
        for pattern in (r'href=["\']([^"\']+\.pdf[^"\']*)["\']',
                        r'["\'](https?://[^"\'\s]+\.pdf[^"\'\s]*)["\']'):
            for h in re.findall(pattern, body, re.I):
                absolute = urlparse.urljoin(page, htmlmod.unescape(h)).split("#")[0]
                if inc and not any(r.search(absolute) for r in inc):
                    continue
                found.add(absolute)
                if page_ticker:
                    _doc_tickers[absolute] = page_ticker.group(1).upper()
        if i % 20 == 0:
            print(f"      {i}/{len(pages)} pages, {len(found)} documents so far")
    print(f"    {len(found)} document(s) from per-fund pages")
    return sorted(found)


def _cells(row_html: str) -> list[str]:
    return [re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)]


_FIGURE_COLUMN = re.compile(r"ordinary|capital\s+gain|distribution", re.I)
_INVESTEE_PARENT = re.compile(
    r"For\s+([A-Z]{2,6})(?:\.[A-Z]{1,2})?\s+(?:Series|Class)\s+([A-Z0-9]{1,3})\s+"
    r"(?:Share|Unit|Security)\s*holders", re.I)


def _class_code(cell: str | None) -> str:
    """'Class E', 'Series E' and 'E' are the same thing written three ways."""
    return re.sub(r"^(?:class|series)\s+", "", (cell or "").strip(), flags=re.I).strip()


def parse_html_statements(page_html: str, url: str) -> list[dict]:
    """Read statements published as an HTML table rather than a PDF.

    Hamilton is the case this exists for: its PFIC page carries a table of
    ticker / fund / class / ordinary earnings / net capital gains /
    distributions, with the period in a heading above it. There is no document
    to fetch, so the row itself has to be captured or the fund is invisible.

    The page holds TWO kinds of table, and telling them apart matters:

      * the main table - one row per fund and class. Each row is that fund's
        own statement.

      * "Direct Investee Funds of the Fund" tables, one per fund-of-funds,
        each introduced by "For HDIV Series E Shareholders". Their rows are
        NOT statements for the funds they name: they are the lower-tier PFIC
        amounts attributable to one unit of the PARENT. An earlier version
        read them as ordinary rows, so HDIV's indirect share of a fund was
        published as that fund's own statement - a different number entirely.
        They are now attached to the parent's statement as `investees`.

    Returns one dict per fund-and-class row. Figures are copied exactly as
    published, dashes included - nothing is recomputed, converted or rounded.
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page_html, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", text)))

    ok, score = verify_statement(text)
    if not ok:
        return []
    period = parse_period(text)

    def figures_of(header: list[str], cells: list[str], skip: set[int]) -> dict | None:
        """Pair each figure with its column heading, or refuse.

        Some of these tables carry a stray empty heading cell, so the header
        has one more column than the rows beneath it. Pairing by position
        would then file every figure under its neighbour's heading - ordinary
        earnings labelled as capital gains. Realign on the empty cell, and if
        the columns still do not line up, drop the row rather than guess.
        """
        if len(header) != len(cells):
            squeezed = [h for h in header if h]
            if len(squeezed) != len(cells):
                return None
            # Positions in `skip` were worked out on the original header.
            skip = {squeezed.index(header[i]) for i in skip if header[i] in squeezed}
            header = squeezed
        out = {header[i]: cells[i] for i in range(len(cells))
               if i not in skip and _FIGURE_COLUMN.search(header[i]) and cells[i] != ""}
        return out or None

    statements: dict[tuple[str, str], dict] = {}
    ordered: list[dict] = []
    investee_tables: list[tuple[str, str, list[dict]]] = []
    unparsed = 0

    for tm in re.finditer(r"<table[^>]*>.*?</table>", page_html, re.S | re.I):
        rows = [_cells(r) for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tm.group(0), re.S | re.I)]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2:
            continue
        header = [h.lower() for h in rows[0]]
        if not any(_FIGURE_COLUMN.search(h) for h in header):
            continue

        if "investee" in header[0]:
            lead = re.sub(r"\s+", " ", htmlmod.unescape(
                re.sub(r"<[^>]+>", " ", page_html[max(0, tm.start() - 1500):tm.start()])))
            parents = _INVESTEE_PARENT.findall(lead)
            if not parents:
                unparsed += len(rows) - 1
                continue
            ticker, cls = parents[-1]
            items, seen = [], set()
            for c in rows[1:]:
                figs = figures_of(header, c, {0})
                if not c[0] or figs is None:
                    unparsed += 1
                    continue
                sig = (c[0], tuple(sorted(figs.items())))
                if sig in seen:          # the page repeats some tables verbatim
                    continue
                seen.add(sig)
                items.append({"name": c[0], "figures": figs})
            if items:
                investee_tables.append((ticker.upper(), cls.upper(), items))
            continue

        i_tick = next((i for i, h in enumerate(header) if "ticker" in h), None)
        i_name = next((i for i, h in enumerate(header) if "fund" in h), None)
        i_class = next((i for i, h in enumerate(header) if "class" in h or "series" in h), None)
        if i_tick is None and i_name is None:
            continue
        for c in rows[1:]:
            if len(c) != len(header):
                unparsed += 1
                continue
            name = c[i_name] if i_name is not None else None
            ticker = (c[i_tick] if i_tick is not None else "").upper()
            if not name and not ticker:
                continue
            if name and len(name) < 4:
                continue
            figs = figures_of(header, c, {i for i in (i_tick, i_name, i_class) if i is not None})
            if figs is None:
                unparsed += 1
                continue
            cls = _class_code(c[i_class]) if i_class is not None else ""
            # The class is part of the fund's identity, not decoration: HYLD
            # Class E and HYLD Class U carry different per-unit amounts. With
            # the class left off, both rows had the same name and the second
            # silently overwrote the first.
            full = f"{name or ticker} - Class {cls}" if cls and len(cls) <= 6 else (name or ticker)
            row = {
                "fund_name": full,
                "tickers": [ticker] if re.fullmatch(r"[A-Z]{2,6}(?:\.[A-Z]{1,2})?", ticker) else [],
                "period_end": period,
                "figures": figs,
                "confidence": score,
                "url": url,
            }
            if row["tickers"]:
                row["fund_key"] = slug(f"{ticker.split('.')[0]} {cls}")
            key = (ticker.split(".")[0], cls.upper())
            if key in statements:
                unparsed += 1            # two rows claiming one fund and class: keep the first
                continue
            statements[key] = row
            ordered.append(row)

    for ticker, cls, items in investee_tables:
        parent = statements.get((ticker, cls))
        if parent is None:
            unparsed += len(items)
            continue
        parent.setdefault("investees", []).extend(items)

    if unparsed:
        print(f"    ! {unparsed} table row(s) on {url} could not be read reliably and were left out")
    return ordered


def collect_json_links(page_html: str, base: str,
                       include: list[str], exclude: list[str]) -> list[str]:
    """Pull document URLs out of JSON embedded in the page.

    Modern framework sites ship their content as a JSON blob and render the
    links in the browser, so there is nothing in the markup for a link scraper
    to find. BMO is the case for this: its PFIC page is Next.js, and all 220
    statement URLs sit in __NEXT_DATA__ pointing at bmo.bynder.com, while the
    HTML itself contains not one document link.
    """
    inc = [re.compile(p) for p in include]
    exc = [re.compile(p) for p in exclude]
    urls: set[str] = set()

    blocks = re.findall(
        r'<script[^>]*(?:id=["\']__NEXT_DATA__["\']|type=["\']application/json["\'])'
        r'[^>]*>(.*?)</script>', page_html, re.S | re.I)
    for block in blocks:
        try:
            data = json.loads(block)
        except Exception:
            continue

        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, str) and ".pdf" in node.lower():
                absolute = urlparse.urljoin(base, node.strip())
                if not absolute.lower().startswith("http"):
                    continue
                if not any(r.search(absolute) for r in inc):
                    continue
                if any(r.search(absolute) for r in exc):
                    continue
                urls.add(absolute.split("#")[0])
    return sorted(urls)


def collect_links(html: str, base: str, include: list[str], exclude: list[str]) -> list[str]:
    inc = [re.compile(p) for p in include]
    exc = [re.compile(p) for p in exclude]
    urls: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        href = m.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urlparse.urljoin(base, href)
        if ".pdf" not in absolute.lower():
            continue
        if not any(r.search(absolute) for r in inc):
            continue
        if any(r.search(absolute) for r in exc):
            continue
        urls.add(absolute.split("#")[0])
    return sorted(urls)


def scan_urls(page: str, include: list[str], exclude: list[str]) -> list[str]:
    """Document addresses written anywhere in a page, not only in links.

    Starlight is the case for this. Its documents page builds its table in the
    browser from data tucked into an HTML attribute, so the statement addresses
    are in the page source but never as an href - and they do not end in
    ".pdf" either (/files/191/download/38415/2024-PFIC-Annual-Information-
    Statement). The family was listed as "statements not posted" for three
    months on the strength of a link scan that could not see them.

    Opt-in per family (`url_scan: true`), and the family's own link_include
    has to match, because this looks at every address on the page.
    """
    inc = [re.compile(p) for p in include]
    exc = [re.compile(p) for p in exclude]
    # The data is HTML-escaped JSON: &quot; for quotes, \/ for slashes.
    text = htmlmod.unescape(page).replace("\\/", "/")
    urls = set()
    for u in re.findall(r"https?://[^\s\"'<>\\?#]+", text):
        u = u.rstrip(".,;)")
        if any(r.search(u) for r in inc) and not any(r.search(u) for r in exc):
            urls.add(u)
    if urls:
        print(f"    {len(urls)} document address(es) found in the page's data")
    return sorted(urls)


def section_links(html: str, base: str, cfg: dict | None, exclude: list[str]) -> list[str]:
    """Every PDF linked inside one named section of a page, whatever it is called.

    Mawer is the case for this. Its documents page has a "PFIC" accordion
    whose files are named by content hash -
    26776e52c358f0ddf878f907772cbf5bcebedc71.pdf - so no filename filter can
    ever pick them out, and the family sat at "empty" while fourteen current
    statements were on the page. What identifies them is where they sit: under
    the PFIC heading. Content verification still decides what is admitted.

    cfg:  start  regex marking the section's heading
          end    regex marking the next section (optional; else end of page)
    """
    if not cfg:
        return []
    m = re.search(cfg["start"], html)
    if not m:
        print("    ! link_section start marker not found on the hub")
        return []
    tail = html[m.end():]
    if cfg.get("end"):
        e = re.search(cfg["end"], tail)
        if e:
            tail = tail[:e.start()]
    exc = [re.compile(p) for p in exclude]
    urls: set[str] = set()
    for h in re.findall(r'href=["\']([^"\']+)["\']', tail, re.I):
        absolute = urlparse.urljoin(base, htmlmod.unescape(h.strip())).split("#")[0]
        if ".pdf" not in absolute.lower():
            continue
        if any(r.search(absolute) for r in exc):
            continue
        urls.add(absolute)
    print(f"    {len(urls)} document(s) in the page's PFIC section")
    return sorted(urls)


def api_links(hub_html: str, fam: dict, include: list[str], exclude: list[str],
              health: dict | None = None) -> list[str]:
    """Document URLs from the JSON endpoint a page loads its list from.

    Dimensional is the case for this. Its document centre is an empty shell
    that asks /investment-api/v2/document-grid for the list, so the page had to
    be rendered in a browser - and from GitHub's US-based runners that render
    comes back with a country-selection overlay and no documents, which is why
    the family worked at home and read "empty" every night in production.
    Asking the endpoint directly needs no browser and gives the same list.

    cfg:  url            the endpoint
          body           JSON request body; "$TOKEN" is replaced (optional)
          token_pattern  regex whose group 1, read from the hub page, is the
                         token - so an id the site rotates is picked up fresh
    """
    cfg = fam.get("json_api")
    if not cfg:
        return []

    def note(why: str) -> None:
        # The run's log cannot be read without signing in to GitHub, but the
        # index it publishes can - so the reason goes there too.
        print(f"    ! {why}")
        if health is not None:
            health["detail"] = why

    body = cfg.get("body")
    if body and "$TOKEN" in body:
        m = re.search(cfg["token_pattern"], hub_html)
        token = m.group(1) if m else cfg.get("token_default")
        if not token:
            note("document API: its id was not on the page the server sent us")
            return []
        if not m:
            # Seen from GitHub's US-based runners: the page arrives without
            # the id it carries for a visitor in Canada. Fall back on the last
            # id known to work rather than list nothing.
            note("document API: id not on the page; used the stored one")
        body = body.replace("$TOKEN", token)
    status, text, _ = fetch(cfg["url"], json_body=body,
                            extra_headers={"Referer": fam["hub"]})
    if status != 200 or not text:
        note(f"document API answered {status}")
        return []
    # Reuse the embedded-JSON walker by presenting the payload the way a page
    # would carry it.
    found = collect_json_links(
        f'<script type="application/json">{text}</script>', cfg["url"], include, exclude)
    print(f"    {len(found)} document(s) from the site's document API")
    if not found:
        note(f"document API answered with {len(text)} bytes but no statement links")
    return found


_GENERIC_LABEL = re.compile(
    r"^(?:pdf|download|view|open|here|click\s+here|english|french|en|fr|"
    r"statement|document|(?:19|20)\d\d)?$", re.I)


def _link_label(text: str) -> str:
    """The words of a link, without the furniture: "(PDF, 126 KB)", icons, "PDF"."""
    t = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", text or "")))
    t = re.sub(r"\(\s*PDF\b[^)]*\)", " ", t, flags=re.I)
    t = re.sub(r"\b(?:save_alt|open_in_new|opens?\s+in\s+a?\s*new\s+(?:tab|window))\b", " ", t,
               flags=re.I)
    t = re.sub(r"(?:^|\s)PDF(?=\s|$)", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -–—|:,.")


def link_rows(fragment: str, base: str) -> list[tuple[str, str, str]]:
    """Every link in a piece of HTML as (absolute url, what it is called, its own text).

    A link that says only "PDF" or "2025" takes its name from the table row or
    list item it sits in - Franklin Templeton's list is a table of fund name in
    one cell and a bare "PDF" link in the next.
    """
    out = []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         fragment, re.S | re.I):
        href = htmlmod.unescape(m.group(1)).strip()
        if not href or href.startswith(("javascript:", "mailto:", "#", "tel:")):
            continue
        own = _link_label(m.group(2))
        label = own
        if _GENERIC_LABEL.match(label):
            start = max(fragment.rfind("<tr", 0, m.start()), fragment.rfind("<li", 0, m.start()))
            ends = [e for e in (fragment.find("</tr>", m.end()), fragment.find("</li>", m.end()))
                    if e > 0]
            if start >= 0 and ends and min(ends) - start < 3000:
                row = _link_label(fragment[start:min(ends)])
                if not _GENERIC_LABEL.match(row):
                    label = row
        out.append((urlparse.urljoin(base, href).split("#")[0], label, own))
    return out


def listed_year(url: str, own_text: str) -> int | None:
    """The year a listed document is for, from how the manager labels it."""
    m = re.fullmatch(r"\s*((?:19|20)\d\d)\s*", own_text or "")
    if m:
        return int(m.group(1))
    path = urlparse.urlsplit(url).path
    in_name = re.findall(r"(?<!\d)(20[0-4]\d)(?!\d)", path.rsplit("/", 1)[-1])
    if in_name:
        return int(in_name[-1])       # "Statement_2025.pdf" under /uploads/2026/04/
    in_path = re.findall(r"/(20[0-4]\d)(?=/)", path)
    return int(in_path[-1]) if in_path else None


def listing_fragments(fam: dict, hub_html: str, browser, args) -> list[tuple[str, str]]:
    """The pieces of HTML a manager's list of statements is made of."""
    fragments = [(hub_html, fam["hub"])]
    for extra in (fam.get("extra_hubs") or []):
        extra_html = fetch_any(extra, browser, allow_browser=not args.static_only)
        if extra_html:
            fragments.append((extra_html, extra))
    api = fam.get("json_api")
    if api:
        status, text, _ = fetch(api["url"], json_body=api.get("body"),
                                extra_headers={"Referer": fam["hub"]})
        try:
            data = json.loads(text) if status == 200 and text else None
        except ValueError:
            data = None
        if data is None:
            print(f"    ! json_api returned {status}")
        # The list arrives as HTML held inside JSON. `within` names the key
        # the list sits under, so links elsewhere on the page are left out.
        within = api.get("within")
        stack = [(data, not within)]
        while stack:
            node, inside = stack.pop()
            if isinstance(node, dict):
                stack.extend((v, inside or k == within) for k, v in node.items())
            elif isinstance(node, list):
                stack.extend((v, inside) for v in node)
            elif isinstance(node, str) and inside and "<a " in node:
                fragments.append((node, fam["hub"]))
    return fragments


def crawl_listing_only(fam: dict, hub_html: str, defaults: dict, manifest: dict,
                       today: str, health: dict, browser, args) -> list["Statement"]:
    """Record a manager's own links to its statements, without fetching them.

    For managers whose robots.txt forbids fetching the documents but not
    reading the page that lists them: Capital Group ("Disallow: /content"),
    iA Clarington ("Disallow: /docs/"), Lysander ("Disallow: /*.pdf$") and
    Franklin Templeton, whose files sit on a host that disallows everything.

    We do what the page invites any visitor to do - read the list - and stop
    there. No document is requested, so nothing is verified: these entries
    say "the manager lists a statement for this fund, here", and the site
    labels them as unchecked. They are never offered to the ZIP proxy.
    """
    fid, cfg = fam["id"], fam["listing_only"]
    inc = [re.compile(p) for p in (fam.get("link_include") or defaults.get("link_include", []))]
    exc = [re.compile(p) for p in fam.get("link_exclude", defaults.get("link_exclude", []))]

    listed: dict[str, tuple[str, int | None]] = {}
    for fragment, base in listing_fragments(fam, hub_html, browser, args):
        for url, label, own in link_rows(fragment, base):
            if not any(r.search(url) for r in inc) or any(r.search(url) for r in exc):
                continue
            year = listed_year(url, own)
            if year is not None and year < args.since_year:
                continue
            name = cfg.get("name") or label
            if not name or _GENERIC_LABEL.match(name):
                stem = Path(urlparse.urlsplit(url).path).stem
                name = re.sub(r"[-_]+", " ", stem).strip()
            if cfg.get("name_strip"):
                name = re.sub(cfg["name_strip"], "", name).strip()
            listed.setdefault(url, (name, year))

    statements = []
    for url, (name, year) in sorted(listed.items()):
        first_seen = (manifest.get(url) or {}).get("first_seen") or today
        _put(manifest, url, {
            "verify_version": VERIFY_VERSION, "family": fid, "fmt": "link",
            "verified": True, "confidence": 0, "sha256": "", "bytes": 0,
            "fund_name": name, "tickers": [], "period_end": None, "tax_year": year,
            "first_seen": first_seen, "last_seen": today, "checked": today,
        })
        statements.append(Statement(
            family=fid, url=url, fund_name=name, tickers=[], period_end=None,
            tax_year=year, sha256="", bytes=0, confidence=0, verified=True,
            first_seen=first_seen, last_seen=today, fmt="link"))

    # A link the manager has taken off its page is no longer a listing. Only
    # when the page did yield links, so a page that failed to load cannot
    # empty the family.
    if listed:
        with _manifest_lock:
            stale = [u for u, v in manifest.items()
                     if v.get("family") == fid and v.get("fmt") == "link" and u not in listed]
            for u in stale:
                del manifest[u]
        if stale:
            print(f"    - {len(stale)} link(s) no longer listed, removed")

    health["candidates"] = len(listed)
    health["statements"] = len(statements)
    health["status"] = "ok" if statements else "empty"
    health["message"] = ("links only - the documents are not fetched: " + cfg["why"]
                         if statements else "the page listed no statement links")
    print(f"    {len(statements)} statement link(s) listed (not fetched)")
    return statements


class Breaker:
    """Counts a family's requests, and stops them once the host is refusing us.

    A 404 is an answer about one document, not a refusal, so it never counts
    towards the limit - a derived family like BlackRock gets hundreds of them
    by design.
    """

    def __init__(self, limit: int = BREAKER_LIMIT) -> None:
        self.limit = limit
        self.streak = 0
        self.tripped = False
        self.last_status = ""
        self.fetched = 0        # requests actually sent
        self.unchanged = 0      # rechecks the host answered with 304
        self._lock = threading.Lock()

    def record(self, status, good: bool) -> None:
        with self._lock:
            self.fetched += 1
            if status == 304:
                self.unchanged += 1
            if good or status in (404, 410):
                self.streak = 0
                return
            self.streak += 1
            self.last_status = str(status)
            if self.streak >= self.limit:
                self.tripped = True


def _is_pdf(blob) -> bool:
    # Not startswith(): a few publishers ship a byte-order mark or a blank
    # line ahead of the header, and those are perfectly good PDFs.
    return bool(blob) and b"%PDF" in blob[:1024]


# Stricter than url_year(): a cached miss for an old year is never retried, so
# a year misread out of a hash-like path segment would bury a live document.
_MISS_YEAR = re.compile(r"(?:^|[-_/.])(20[0-4]\d)(?=[-_/.]|$)")


def _miss_year(url: str) -> int | None:
    years = [int(y) for y in _MISS_YEAR.findall(urlparse.urlsplit(url).path)]
    return max(years) if years else None


def _put(manifest: dict, url: str, record: dict) -> None:
    """Every cache write goes through here, and always as a fresh dict.

    Documents are processed on worker threads while the main thread
    checkpoints the cache to disk. Serialising a dict that another thread is
    adding keys to raises "dictionary changed size during iteration", which
    used to surface as a family randomly failing with status "error".
    """
    with _manifest_lock:
        manifest[url] = record


def cached_statement(prev: dict, url: str, family_id: str, today: str) -> "Statement | None":
    year = prev.get("tax_year")
    if year is not None and year < SINCE_YEAR:
        return None
    return Statement(
        family=family_id, url=url, fund_name=prev.get("fund_name"),
        tickers=prev.get("tickers", []), period_end=prev.get("period_end"),
        tax_year=year, sha256=prev.get("sha256", ""), bytes=prev.get("bytes", 0),
        confidence=prev.get("confidence", 0), verified=True,
        first_seen=prev.get("first_seen", today), last_seen=today)


def needs_fetch(url: str, prev: dict, today: str) -> bool:
    """Decide from the cache alone whether this URL needs a request tonight."""
    if not prev:
        return True                       # never seen: always fetch
    this_year = date.fromisoformat(today).year
    current = prev.get("verify_version") == VERIFY_VERSION
    checked = prev.get("checked") or prev.get("last_seen")

    # An archived copy cannot change, so once it has been read - accepted or
    # rejected - there is nothing to ask again until the verifier changes.
    if (archive_parts(url) and current and not prev.get("miss")
            and prev.get("verified") is not None and not prev.get("last_error")):
        return False

    if prev.get("miss"):
        if not current:
            return True
        # A refusal or a timeout says nothing about the document: try again.
        if prev.get("status") not in ("404", "410", "not-a-pdf"):
            return True
        # Positively not there. For an old year that is final. For a recent
        # one it is exactly where next season's statement will appear, so
        # keep looking - weekly, not nightly.
        year = prev.get("probe_year")
        if year is not None and year < this_year - 1:
            return False
        return recheck_due(url, checked, RECHECK_DAYS_MISS, today)

    if prev.get("verified") is None:
        return True
    if prev.get("last_error"):
        return True                       # failed last time: retry until it answers
    if not prev["verified"]:
        # Rejected as not-a-statement. Under an older verifier that may have
        # been our own bug, so look again at once; under the current one,
        # only occasionally.
        return (not current) or recheck_due(url, checked, RECHECK_DAYS_CLOSED, today)
    # A parser fix should reach the records it was written for straight away,
    # not whenever their turn comes round. (Only for verdicts from an older
    # verifier, or a genuinely nameless document would be re-read every night.)
    if not current and _suspect_name(prev.get("fund_name")):
        return True
    year = prev.get("tax_year")
    window = (RECHECK_DAYS_CLOSED if year and year <= this_year - 2
              else RECHECK_DAYS_OPEN)
    return recheck_due(url, checked, window, today)


def process_pdf(url: str, family_id: str, manifest: dict, today: str,
                getter=None, breaker: "Breaker | None" = None,
                prefetched: dict | None = None, via: str | None = None,
                name_pattern: str | None = None, split: dict | None = None,
                force: bool = False, name_fix: list | None = None) -> "Statement | None":
    """Return the verified statement at this URL, or None.

    The cache answers whenever it can (see needs_fetch). When a request is
    made, three outcomes are kept strictly apart, because confusing them is
    how statements used to vanish from the index:

      * the document came back      -> verify it and record the verdict
      * the host says it is gone    -> a strike; forgotten after GONE_STRIKES
      * the request simply failed   -> keep what we have and try again
    """
    prev = manifest.get(url) or {}
    current = prev.get("verify_version") == VERIFY_VERSION
    known_good = bool(prev.get("verified")) and not prev.get("miss")

    def from_cache() -> "Statement | None":
        if prev:
            _put(manifest, url, {**prev, "last_seen": today})
        return cached_statement(prev, url, family_id, today) if known_good else None

    if not force and not needs_fetch(url, prev, today):
        return from_cache()
    if breaker is not None and breaker.tripped:
        return from_cache()
    if out_of_time():
        global _deferred
        with _manifest_lock:
            _deferred += 1
        return from_cache()

    # Conditional request, so an unchanged document costs a 304 instead of a
    # download. Only over plain HTTP, and only when the cached verdict came
    # from the current verifier - after a parser change we want the bytes back
    # so the document is read again.
    conditional = {}
    if (getter is None and current and not force
            and prev.get("verified") is not None and not prev.get("miss")):
        if prev.get("etag"):
            conditional["If-None-Match"] = prev["etag"]
        if prev.get("last_modified"):
            conditional["If-Modified-Since"] = prev["last_modified"]

    if prefetched and url in prefetched:
        status, blob, resp_headers = prefetched.pop(url)   # already fetched by the transport probe
    elif getter is None:
        status, blob, resp_headers = fetch(url, binary=True, extra_headers=conditional)
    else:
        status, blob, resp_headers = getter(url)
    resp_headers = {str(k).lower(): v for k, v in (resp_headers or {}).items()}

    settled = {k: v for k, v in prev.items() if k not in ("last_error", "gone_strikes")}

    if status == 304 and conditional:
        if breaker is not None:
            breaker.record(status, True)
        _put(manifest, url, {**settled, "checked": today, "last_seen": today})
        return cached_statement(prev, url, family_id, today) if known_good else None

    good = status == 200 and _is_pdf(blob)
    if breaker is not None:
        breaker.record(status, good)

    text = pdf_text(blob) if good else ""
    # A document we already hold that suddenly cannot be read at all is a bad
    # download (truncated, or a transient encryption hiccup), not a verdict.
    unreadable = good and known_good and not text.strip()

    if not good or unreadable:
        label = ("unreadable" if unreadable
                 else "not-a-pdf" if status == 200 else str(status))
        if known_good:
            # 404/410 and "200 but an HTML page" both mean the document has
            # been taken down. Anything else is a failed request.
            gone = label in ("404", "410", "not-a-pdf")
            strikes = prev.get("gone_strikes", 0) + 1 if gone else 0
            if strikes < GONE_STRIKES:
                _put(manifest, url, {**prev, "last_seen": today,
                                     "last_error": label, "gone_strikes": strikes})
                return cached_statement(prev, url, family_id, today)
            print(f"    * gone on {GONE_STRIKES} consecutive checks, dropping: {url}")
        _put(manifest, url, {"miss": True, "status": label, "checked": today,
                             "last_seen": today, "verify_version": VERIFY_VERSION,
                             "family": family_id, "probe_year": _miss_year(url)})
        return None

    digest = hashlib.sha256(blob).hexdigest()
    if known_good and prev.get("sha256") and prev["sha256"] != digest:
        print(f"    * CONTENT CHANGED (reissue?): {url}")

    ok, score = verify_statement(text)
    period = parse_period(text)
    name = parse_fund_name(text, name_pattern)
    for pattern, replacement in (name_fix or []):
        if name:
            name = re.sub(pattern, replacement, name)
    tickers = parse_tickers(text, url)
    if url in _doc_tickers and _doc_tickers[url] not in tickers:
        tickers = sorted([*tickers, _doc_tickers[url]])
    year = int(period[:4]) if period else None

    record = {
        "verify_version": VERIFY_VERSION,
        "family": family_id,
        "etag": resp_headers.get("etag"),
        "last_modified": resp_headers.get("last-modified"),
        "sha256": digest,
        "bytes": len(blob),
        "verified": ok,
        "confidence": score,
        "fund_name": name,
        "tickers": tickers,
        "period_end": period,
        "tax_year": year,
        "first_seen": prev.get("first_seen") or today,
        "last_seen": today,
        "checked": today,
    }
    if via and via != "plain HTTP":
        record["via"] = via               # which client this host serves, for next time
    if ok and split:
        pages = pdf_pages(blob)
        record["pages"] = len(pages)
        record["parts"] = split_document(pages, split)
    _put(manifest, url, record)

    if not ok:
        return None
    if year is not None and year < SINCE_YEAR:
        return None
    return Statement(
        family=family_id, url=url, fund_name=name, tickers=tickers,
        period_end=period, tax_year=year, sha256=digest, bytes=len(blob),
        confidence=score, verified=True,
        first_seen=record["first_seen"], last_seen=today,
    )


def _spread(items: list, n: int) -> list:
    """Up to n items sampled evenly across the list, never just its head.

    Links sort alphabetically, which usually means by year, so a head slice
    would test only the oldest catalogue - the entries most likely to be dead.
    """
    if n <= 0 or len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def fetch_documents(links: list[str], fam: dict, manifest: dict, today: str,
                    browser: "BrowserSession", args, health: dict) -> list["Statement"]:
    """Turn candidate URLs into verified statements, with as few requests as possible.

    Most nights most documents are answered from the cache and nothing is
    fetched at all. Only when something does need fetching do we work out which
    HTTP client this host will serve documents to - and that is remembered from
    one night to the next rather than rediscovered.
    """
    fid = fam["id"]
    breaker = Breaker()
    statements: list[Statement] = []
    pending: list[str] = []

    split = fam.get("split")
    forced: set[str] = set()
    for u in links:
        prev = manifest.get(u) or {}
        # A document cached before this family was marked as bundled has
        # never been split; that cannot wait a month for its recheck night.
        if (split and prev.get("verified") and "parts" not in prev) or args.refresh:
            forced.add(u)
        if u in forced or needs_fetch(u, prev, today):
            pending.append(u)
        else:
            st = process_pdf(u, fid, manifest, today)
            if st:
                statements.append(st)
    health["from_cache"] = len(links) - len(pending)

    def finish(status: str | None = None, message: str = "") -> list[Statement]:
        health["requests"] = breaker.fetched
        health["unchanged"] = breaker.unchanged
        health["statements"] = len(statements)
        health["status"] = status or ("ok" if statements else "empty")
        if message:
            health["message"] = message
        elif not statements:
            health["message"] = "candidates found but none verified as PFIC statements"
        print(f"    {len(statements)} verified statement(s)  "
              f"[{health['from_cache']} from cache, {breaker.fetched} request(s), "
              f"{breaker.unchanged} unchanged]")
        return statements

    if not pending:
        return finish()

    def known_good(u: str) -> bool:
        p = manifest.get(u) or {}
        return bool(p.get("verified")) and not p.get("miss")

    transports = [("plain HTTP", lambda u: fetch(u, binary=True)),
                  ("curl", lambda u: curl_fetch(u, binary=True))]
    if not args.static_only:
        transports.append(("browser", browser.get))

    # Which client served this family's documents last time? Every publisher
    # blocks a different combination (Manulife 403s plain HTTP and hands every
    # PDF to curl), so the answer is recorded per document and reused.
    with _manifest_lock:
        history = [v.get("via", "plain HTTP") for v in manifest.values()
                   if v.get("family") == fid and v.get("verified") and not v.get("miss")]
    preferred = max(set(history), key=history.count) if history else None
    if preferred:
        transports.sort(key=lambda t: t[0] != preferred)

    # Probe a few of tonight's documents to confirm the transport. Start with
    # ones we know exist; a derived family's candidates are mostly URLs that
    # were never there, and three 404s in a row used to get BlackRock reported
    # as "blocked" for weeks when nothing was wrong at all.
    sample = [u for u in pending if known_good(u)][:2]
    sample += [u for u in _spread(pending, 12 if fam.get("derive") and not preferred else 4)
               if u not in sample]

    chosen = None
    fetched: dict[str, tuple] = {}
    refusals: list[str] = []
    for u in sample:
        for name, fn in transports:
            status, blob, hdrs = fn(u)
            if status == 200 and _is_pdf(blob):
                chosen = (name, fn)
                fetched = {u: (status, blob, hdrs)}
                break
            if status in (404, 410):
                # An answer, not a refusal. With a transport on record there
                # is nothing to learn by asking two more clients the same
                # question; without one, a 404 may be a disguised block (iA
                # Clarington served a 404 page to everything but a browser).
                if preferred:
                    if name == preferred:
                        fetched[u] = (status, blob, hdrs)
                    break
                continue
            refusals.append(f"{name}={status}")
        if chosen:
            break

    if chosen is None:
        if refusals:
            print("    ! documents unreachable by any transport")
            return finish("blocked", "documents unreachable "
                          f"({'; '.join(refusals[:6])}); cached statements kept")
        # Every probe was answered "not there". Nothing is blocked - tonight's
        # candidates simply do not exist (yet).
        chosen = transports[0]
    name, fn = chosen
    getter = None if name == "plain HTTP" else fn
    if name != "plain HTTP":
        health["pdf_transport"] = name
        print(f"    - documents fetched via {name}")
    # `fetched` holds the probe's own results, so those documents are not
    # requested a second time below.

    def one(u: str) -> "Statement | None":
        return process_pdf(u, fid, manifest, today, getter=getter, breaker=breaker,
                           prefetched=fetched, via=name,
                           name_pattern=fam.get("name_pattern"),
                           name_fix=fam.get("name_fix"),
                           split=split, force=u in forced)

    if name == "browser":
        # Playwright's sync API is bound to the thread that created it.
        for i, u in enumerate(pending, 1):
            st = one(u)
            if st:
                statements.append(st)
            if i % 25 == 0:
                print(f"      {i}/{len(pending)} fetched")
            if i % 200 == 0:
                save_manifest(manifest)
    else:
        with futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            # Checkpoint periodically. CI Global alone lists ~15,000 documents;
            # losing hours of downloads because the run was interrupted between
            # family boundaries is not an acceptable failure mode.
            for n, st in enumerate(pool.map(one, pending), 1):
                if st:
                    statements.append(st)
                if n % 250 == 0:
                    save_manifest(manifest)
                    print(f"      {n}/{len(pending)} processed")

    if breaker.tripped:
        print(f"    ! host refused {breaker.limit} requests in a row "
              f"(last: {breaker.last_status}); stopped asking")
        return finish("blocked", f"host refused {breaker.limit} requests in a row "
                      f"(last: {breaker.last_status}); cached statements kept")
    return finish()


def fetch_archived(copies: list[str], fam: dict, manifest: dict, today: str,
                   browser: "BrowserSession", args, health: dict) -> list["Statement"]:
    """Read the archived copies of statements the manager has taken down.

    Skipped for any document just found at the manager under a dated address:
    that is the same file, and the manager's own copy is the one to show.
    """
    wanted = []
    for u in copies:
        original = archive_parts(u)[1]
        rec = manifest.get(original) or {}
        if rec.get("verified") and not rec.get("miss") and url_year(original) is not None:
            continue
        wanted.append(u)
    if not wanted:
        return []
    _host_delays[ARCHIVE_HOST] = max(_host_delay(ARCHIVE_HOST), ARCHIVE_DELAY)
    print(f"    {len(wanted)} archived cop{'y' if len(wanted) == 1 else 'ies'} of earlier statements")
    # Its own tally and its own circuit breaker: the archive being slow or
    # refusing tonight says nothing about the manager, and must not turn a
    # healthy source "blocked".
    tally: dict = {}
    plain = argparse.Namespace(**{**vars(args), "static_only": True})
    found = fetch_documents(wanted, {**fam, "derive": None}, manifest, today, browser, plain, tally)
    health["archived"] = len(found)
    health["from_cache"] = health.get("from_cache", 0) + tally.get("from_cache", 0)
    health["requests"] = health.get("requests", 0) + tally.get("requests", 0)
    health["statements"] = health.get("statements", 0) + len(found)
    if found and health.get("status") == "empty":
        health["status"] = "ok"
    if tally.get("status") == "blocked":
        health["detail"] = "the Internet Archive refused requests tonight; earlier years will be retried"
    return found


def crawl_family(fam: dict, defaults: dict, manifest: dict, args, today: str):
    fid, mode = fam["id"], fam.get("mode", "static")
    health = {"id": fid, "name": fam["name"], "mode": mode, "hub": fam["hub"],
              "checked": today, "status": "unknown", "statements": 0, "candidates": 0,
              "message": ""}
    print(f"\n[{fid}] {fam['name']}  ({mode})")

    # A manager whose server is known to rate-limit can be paced from the
    # start (sources.yaml `delay:`, seconds between requests to its host).
    if fam.get("delay"):
        host = urlparse.urlsplit(fam["hub"]).netloc
        _host_delays[host] = max(_host_delay(host), float(fam["delay"]))

    browser = BrowserSession()
    began = time.monotonic()
    try:
        return _crawl_family_inner(fam, defaults, manifest, args, today, health, browser)
    finally:
        browser.close()
        # Published with the index, so a slow night can be explained without
        # the run's log.
        health["seconds"] = round(time.monotonic() - began)
        print(f"    took {health['seconds']}s")


def _crawl_family_inner(fam: dict, defaults: dict, manifest: dict, args, today: str,
                        health: dict, browser: "BrowserSession"):
    fid, mode = fam["id"], fam.get("mode", "static")

    # Some managers publish statements but forbid automated retrieval. We take
    # that at face value rather than routing around it: the family stays in the
    # index as a deep link, flagged for manual fetch.
    access = fam.get("access")
    if access in NOT_CRAWLED:
        health["status"] = access
        health["message"] = (fam.get("guidance") or "").strip() or {
            "none": "no statement could be located on the manager's website",
            "request": "issued by the manager on request; none are posted",
        }.get(access, "statements exist but cannot be fetched automatically")
        print(f"    - not crawled ({access})")
        return [], health

    # Getting the hub at all is the hardest part of several families, and every
    # publisher blocks a different combination. Try all three transports in the
    # order this family is likely to need, and accept the first that returns a
    # page of plausible size. A 359-byte "page" is a block notice, not a
    # document list - Manulife serves exactly that to Chromium while serving
    # 600KB to curl.
    MIN_HUB_BYTES = 2000

    def _plain_hub():
        st, body, _ = fetch(fam["hub"])
        return body if isinstance(st, int) and st == 200 else None

    def _curl_hub():
        st, body, _ = curl_fetch(fam["hub"])
        return body if isinstance(st, int) and st == 200 else None

    order = ([("browser", lambda: browser.html(fam["hub"])), ("curl", _curl_hub),
              ("plain HTTP", _plain_hub)] if mode == "browser" else
             [("plain HTTP", _plain_hub), ("curl", _curl_hub),
              ("browser", lambda: browser.html(fam["hub"]))])
    if args.static_only:
        order = [o for o in order if o[0] != "browser"]

    html = None
    for label, fn in order:
        try:
            body = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"    - {label} failed: {type(exc).__name__}")
            continue
        if body and len(body) >= MIN_HUB_BYTES:
            if label != order[0][0]:
                health["message"] = f"hub needed {label}"
                print(f"    - hub retrieved via {label}")
            html = body
            break
        if body:
            print(f"    - {label} returned only {len(body)} bytes (looks blocked)")

    if not html:
        health["status"] = "down"
        health["message"] = "hub unreachable by plain HTTP, curl or browser"
        print("    ! hub unreachable by every transport")
        return [], health


    if fam.get("listing_only"):
        return crawl_listing_only(fam, html, defaults, manifest, today, health,
                                  browser, args), health

    # Families that publish statements as HTML tables instead of documents.
    if fam.get("doc_type") == "html_table":
        pages = list(fam.get("statement_pages") or [])
        if not pages:
            pages = [fam["hub"]]
        # Managers add a new page each year, at a URL nobody can predict:
        # Hamilton's 2024 statements live at /pfic/, 2025 at
        # /pfic-annual-information-statements-2025/. A hand-kept list of pages
        # therefore goes stale every spring - the 2026 page was up for months
        # and never read. So also pick up any page the hub (or a page already
        # known) links that matches the family's pattern.
        page_rx = (re.compile(fam["statement_page_pattern"])
                   if fam.get("statement_page_pattern") else None)

        def discover(source_html: str, source_url: str) -> None:
            if not page_rx:
                return
            for h in re.findall(r'href=["\']([^"\'#]+)', source_html):
                absolute = urlparse.urljoin(source_url, htmlmod.unescape(h))
                if page_rx.search(absolute) and absolute not in pages:
                    print(f"    + discovered statement page {absolute}")
                    pages.append(absolute)

        discover(html, fam["hub"])
        statements: list[Statement] = []
        pages_read = 0
        for page_url in pages:              # grows while iterating, by design
            if pages_read >= 25:
                break
            page = html if page_url == fam["hub"] else None
            if page is None:
                page = fetch_any(page_url, browser, allow_browser=not args.static_only)
            if not page:
                print(f"    ! could not read {page_url}")
                continue
            pages_read += 1
            discover(page, page_url)
            rows = parse_html_statements(page, page_url)
            print(f"    {len(rows)} row(s) from {page_url.rstrip('/').rsplit('/', 1)[-1]}")
            seen_keys: set[str] = set()
            for r in rows:
                year = int(r["period_end"][:4]) if r["period_end"] else None
                if year is not None and year < args.since_year:
                    continue
                digest = hashlib.sha256(
                    json.dumps([r["fund_name"], r["period_end"], r["figures"],
                                r.get("investees")], sort_keys=True).encode()).hexdigest()
                # Record in the manifest like any other statement, so the index
                # rebuild sees it. Many rows share one page URL, so the key is
                # synthesised and the real link kept in "url".
                # The ticker goes into the key as well as the name: Hamilton's
                # 2024 page lists AMAX and HBA under the same fund name, and
                # keyed on the name alone one row overwrote the other.
                key = (f"{page_url}#{r.get('fund_key') or ''}:"
                       f"{slug(r['fund_name'])}|{r['period_end']}")
                seen_keys.add(key)
                first_seen = (manifest.get(key) or {}).get("first_seen") or today
                _put(manifest, key, {
                    "verify_version": VERIFY_VERSION, "family": fid, "url": page_url,
                    "verified": True, "confidence": r["confidence"], "sha256": digest,
                    "bytes": 0, "fund_name": r["fund_name"], "tickers": r["tickers"],
                    "period_end": r["period_end"], "tax_year": year,
                    "fmt": "html", "figures": r["figures"],
                    "investees": r.get("investees"), "fund_key": r.get("fund_key"),
                    "first_seen": first_seen, "last_seen": today, "checked": today,
                })
                statements.append(Statement(
                    family=fid, url=page_url, fund_name=r["fund_name"],
                    tickers=r["tickers"], period_end=r["period_end"], tax_year=year,
                    sha256=digest, bytes=0, confidence=r["confidence"], verified=True,
                    first_seen=first_seen, last_seen=today, fmt="html",
                    figures=r["figures"], investees=r.get("investees"),
                    fund_key=r.get("fund_key")))
            # A row the manager has since taken off a page we just read in full
            # is no longer published; keeping it would show a statement that
            # the source page does not carry. Only when the page yielded rows,
            # so a half-loaded page cannot wipe its own history.
            if rows:
                with _manifest_lock:
                    stale = [k for k, v in manifest.items()
                             if v.get("fmt") == "html" and v.get("url") == page_url
                             and k not in seen_keys]
                    for k in stale:
                        del manifest[k]
                if stale:
                    print(f"    - {len(stale)} row(s) no longer on that page, removed")
        health["candidates"] = len(pages)
        health["statements"] = len(statements)
        health["status"] = "ok" if statements else "empty"
        if not statements:
            health["message"] = "no statement tables found on the listed pages"
        print(f"    {len(statements)} verified statement(s)")
        return statements, health

    include = fam.get("link_include", defaults["link_include"])
    exclude = fam.get("link_exclude", defaults["link_exclude"])
    # Some managers publish one document per year at a fixed path with nothing
    # linking to it from a browsable page - Desjardins is the case. A template
    # expanded over the year range reaches them.
    year_urls: list[str] = []
    for tmpl in (fam.get("year_urls") or []):
        this_year = utc_today().year
        for yr in range(this_year, max(args.since_year, this_year - 8) - 1, -1):
            year_urls.append(tmpl.format(year=yr))
    if year_urls:
        print(f"    {len(year_urls)} templated URL(s) to probe")

    links = collect_links(html, fam["hub"], include, exclude) + year_urls
    links += section_links(html, fam["hub"], fam.get("link_section"), exclude)
    if fam.get("url_scan"):
        links += scan_urls(html, include, exclude)
    links += api_links(html, fam, include, exclude, health)

    # Bilingual managers sometimes split their catalogue across language sites:
    # National Bank's French page lists 15 ETF tickers its English page does
    # not. Any additional hub is scanned with the same filters.
    for extra in (fam.get("extra_hubs") or []):
        extra_html = fetch_any(extra, browser, allow_browser=not args.static_only)
        if not extra_html:
            print(f"    ! extra hub unreachable: {extra[:70]}")
            continue
        found = collect_links(extra_html, extra, include, exclude)
        found += collect_json_links(extra_html, extra, include, exclude)
        new = [u for u in found if u not in links]
        print(f"    +{len(new)} document(s) from {extra.rsplit('/', 1)[-1]}")
        links += new
    links += collect_json_links(html, fam["hub"], include, exclude)
    links += follow_pages(html, fam["hub"], fam, browser, args)
    derived, derived_names = derive_candidates(fam, browser, args.since_year)
    links = sorted(set(links) | set(derived))
    health["derived_names"] = len(derived_names)

    # Earlier years the page no longer links (see "earlier years", above).
    archived_copies: list[str] = []
    earlier: list[str] = []
    if fam.get("earlier_years"):
        earlier += earlier_year_urls(links, args.since_year)
    if fam.get("archive"):
        at_manager, archived_copies = archive_candidates(
            fam, manifest, today, args.since_year, links, include, exclude)
        earlier += at_manager
    earlier = sorted(set(earlier) - set(links))
    if earlier:
        print(f"    {len(earlier)} earlier address(es) to look for at the manager")

    # Drop pre-floor documents before spending a request on them. This is where
    # the crawl time actually goes: CI alone lists ~15,000 documents, and the
    # year is right there in the filename.
    before = len(links)
    links = [u for u in links if not too_old(u, args.since_year)]
    if before != len(links):
        health["skipped_old"] = before - len(links)
        print(f"    {before - len(links)} candidate(s) older than "
              f"{args.since_year} skipped by URL")
    listed = len(links)
    links = sorted(set(links) | {u for u in earlier if not too_old(u, args.since_year)})
    if args.limit:
        links = _spread(links, args.limit)     # a smoke test: sample, and nothing else
        archived_copies = _spread(archived_copies, args.limit)
    else:
        # Statements we verified earlier that tonight's listing no longer
        # links. Managers prune their pages (Mawer shows only the latest year)
        # while the older documents stay downloadable - those are worth
        # keeping. But a delisted document whose URL has also died must not
        # sit in the index as a dead link forever, and nothing would ever look
        # at it again unless it is put back in the queue here. It then follows
        # the normal schedule: rechecked occasionally, dropped once it is gone.
        listed_set = set(links)
        with _manifest_lock:
            orphans = sorted(
                u for u, v in manifest.items()
                if v.get("family") == fid and v.get("verified") and not v.get("miss")
                and v.get("fmt", "pdf") == "pdf" and u not in listed_set
                and not archive_parts(u)       # those have their own pass, below
                and not too_old(u, args.since_year))
        if orphans:
            health["delisted"] = len(orphans)
            print(f"    {len(orphans)} held statement(s) no longer listed; kept on the recheck schedule")
            links += orphans
    health["candidates"] = len(links)
    print(f"    {listed} candidate PDF link(s) listed")

    if not links:
        health["status"] = "empty"
        health["message"] = "hub reachable but no candidate PDFs matched"
        return [], health

    statements = fetch_documents(links, fam, manifest, today, browser, args, health)
    statements += fetch_archived(archived_copies, fam, manifest, today, browser, args, health)
    # A hub that lists nothing while we still hold its statements has not
    # gone quiet - its page has changed shape under us (or failed to render),
    # and new statements will be missed until someone looks. Say so, rather
    # than reporting "ok" on the strength of the cache.
    if not listed and health["status"] == "ok":
        health["status"] = "empty"
        health["message"] = ("hub reachable but it listed no documents tonight; "
                             f"{len(statements)} cached statement(s) kept"
                             + (f" ({health['detail']})" if health.get("detail") else ""))
    return statements, health


# --------------------------------------------------------------------------
# index assembly
# --------------------------------------------------------------------------

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def statements_from_manifest(manifest: dict, since: int) -> list[Statement]:
    """Rebuild the full statement set from the cache.

    The index is assembled from everything ever verified, not just what this
    run happened to touch. Without this, `--only mackenzie` would publish an
    index containing Mackenzie and nothing else, quietly deleting twenty other
    families - and an interrupted run would do the same.
    """
    out = []
    for url, v in manifest.items():
        if not v.get("verified") or v.get("miss"):
            continue
        year = v.get("tax_year")
        if v.get("parts"):
            # One document, many funds: one entry per fund, each opening the
            # document at that fund's page.
            for p in v["parts"]:
                p_end = p.get("period_end") or v.get("period_end")
                p_year = int(p_end[:4]) if p_end else year
                if p_year is not None and p_year < since:
                    continue
                out.append(Statement(
                    family=v.get("family", "unknown"), url=f"{url}#page={p['page']}",
                    fund_name=p["name"], tickers=p.get("tickers", []),
                    period_end=p_end, tax_year=p_year, sha256=v.get("sha256", ""),
                    bytes=v.get("bytes", 0), confidence=v.get("confidence", 0),
                    verified=True, first_seen=v.get("first_seen", ""),
                    last_seen=v.get("last_seen", ""),
                    part={"page": p["page"], "pages": v.get("pages")}))
            continue
        if year is not None and year < since:
            continue
        out.append(Statement(
            # HTML rows share a page URL, so their manifest key is synthetic
            # and the real link lives in "url".
            family=v.get("family", "unknown"), url=v.get("url") or url,
            fund_name=v.get("fund_name"), tickers=v.get("tickers", []),
            period_end=v.get("period_end"), tax_year=year,
            sha256=v.get("sha256", ""), bytes=v.get("bytes", 0),
            confidence=v.get("confidence", 0), verified=True,
            first_seen=v.get("first_seen", ""), last_seen=v.get("last_seen", ""),
            fmt=v.get("fmt", "pdf"), figures=v.get("figures"),
            investees=v.get("investees"), fund_key=v.get("fund_key"),
        ))
    return out


def build_index(all_statements: list[Statement], families: list[dict],
                health: list[dict], negatives: list[dict]) -> dict:
    fam_by_id = {f["id"]: f for f in families}
    funds: dict[str, dict] = {}

    for st in all_statements:
        fam = fam_by_id.get(st.family, {})
        if st.fund_name:
            name = tidy_name(st.fund_name)
        elif fam.get("split"):
            # A combined document whose funds could not be told apart.
            name = f"{fam.get('name', st.family)} - combined statement (all funds)"
        else:
            # No readable name: fall back on the filename, made presentable.
            stem = Path(urlparse.unquote(urlparse.urlsplit(st.url).path)).stem
            name = re.sub(r"[-_]+", " ", stem).strip()
        key = f"{st.family}::{st.fund_key or slug(name)}"
        f = funds.setdefault(key, {
            "id": key,
            "family": st.family,
            "family_name": fam_by_id.get(st.family, {}).get("name", st.family),
            "country": fam_by_id.get(st.family, {}).get("country", "CA"),
            "name": name,
            "tickers": [],
            "statements": [],
        })
        for t in st.tickers:
            if t not in f["tickers"]:
                f["tickers"].append(t)
        held = archive_parts(st.url)
        entry = {
            "name": name,
            "period_end": st.period_end,
            "tax_year": st.tax_year,
            # For a reader, the capture's ordinary address; "id_" is for us.
            "url": archive_url(held[0], held[1], raw=False) if held else st.url,
            "bytes": st.bytes,
            "sha256": st.sha256[:16],
            "confidence": st.confidence,
            "fmt": st.fmt,
        }
        if st.figures:
            entry["figures"] = st.figures
        if st.investees:
            entry["investees"] = st.investees
        if st.part:
            entry["part"] = st.part
        if held:
            # The manager no longer posts this one: say whose copy it is and
            # when it was taken.
            entry["archived"] = f"{held[0][:4]}-{held[0][4:6]}-{held[0][6:8]}"
        f["statements"].append(entry)

    # An archived copy stands in only where the manager's own document is
    # missing. Where both exist for a period the manager's wins; of several
    # captures of one period, the latest is the manager's last word on it.
    for f in funds.values():
        own = {s["period_end"] for s in f["statements"] if not s.get("archived")}
        latest: dict = {}
        for s in f["statements"]:
            if s.get("archived") and s["period_end"] not in own:
                key = s["period_end"] or s["url"]
                if key not in latest or s["archived"] > latest[key]["archived"]:
                    latest[key] = s
        keep = {id(s) for s in latest.values()}
        f["statements"] = [s for s in f["statements"]
                           if not s.get("archived") or id(s) in keep]

    # CI republishes the same statement under consecutive document ids - byte
    # for byte the same length, identical extracted text, different binary
    # (regeneration metadata). Showing both is pure noise in the picker. Dedupe
    # only on an exact (period, size) twin, which is a strong identity signal;
    # where two documents for one period genuinely differ in size, both are
    # kept, because dropping a real statement is worse than showing two.
    for f in funds.values():
        seen: set[tuple] = set()
        deduped = []
        for s in f["statements"]:
            # Table rows have no size, so their content hash stands in for it;
            # otherwise two different rows for one period look like twins.
            # ...and a link we never fetched has neither, so its address does.
            sig = (s["period_end"], s["bytes"] or s["sha256"] or s["url"])
            if sig in seen:
                continue
            seen.add(sig)
            deduped.append(s)
        f["duplicates_hidden"] = len(f["statements"]) - len(deduped)
        f["statements"] = deduped

    for f in funds.values():
        f["statements"].sort(
            key=lambda s: (s["period_end"] or str(s["tax_year"] or ""), s["url"]), reverse=True)
        # The fund is shown under its most recent published name. A statement
        # the manager published under a different name keeps that name beside
        # it, so nothing is silently relabelled.
        f["name"] = f["statements"][0]["name"]
        for s in f["statements"]:
            if slug(s["name"]) == slug(f["name"]):
                del s["name"]
        f["years"] = sorted({s["tax_year"] for s in f["statements"] if s["tax_year"]},
                            reverse=True)

    fund_list = sorted(funds.values(), key=lambda f: (f["family_name"], f["name"]))
    all_years = sorted({y for f in fund_list for y in f["years"]}, reverse=True)

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": {
            "families": len(families),
            "families_ok": sum(1 for h in health if h["status"] == "ok"),
            "funds": len(fund_list),
            "statements": sum(len(f["statements"]) for f in fund_list),
            "years": all_years,
        },
        "families": [
            {**{k: fam.get(k) for k in
                ("id", "name", "country", "mode", "fye", "hub", "page", "notes",
                 "access", "guidance", "action", "caveat", "covers")},
             "links_only": bool(fam.get("listing_only")),
             # so the UI can explain a family with no statements rather than
             # just showing an empty result
             "statement_count": sum(1 for f in fund_list if f["family"] == fam["id"]
                                    for _ in f["statements"]),
             "fund_count": sum(1 for f in fund_list if f["family"] == fam["id"])}
            for fam in families
        ],
        "funds": fund_list,
        "negatives": negatives,
        "health": health,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="family ids to crawl")
    ap.add_argument("--static-only", action="store_true")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="permit an index that loses over half its statements")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="rebuild the index from the cache without any network access")
    ap.add_argument("--limit", type=int, default=0, help="max PDFs per family")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="stop making requests after this long and finish from the "
                         "cache, so the run always completes (0 = no limit)")
    ap.add_argument("--refresh", action="store_true",
                    help="download and re-read every listed document, ignoring the "
                         "cache; use with --only after changing a family's settings")
    ap.add_argument("--since-year", type=int, default=SINCE_YEAR,
                    help=f"oldest tax year to index (default {SINCE_YEAR}); "
                         "raising this is the biggest lever on crawl time")
    args = ap.parse_args()
    globals()["SINCE_YEAR"] = args.since_year

    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    negs = yaml.safe_load((ROOT / "negatives.yaml").read_text(encoding="utf-8"))
    codes_path = ROOT / "fundcodes.yaml"
    fund_codes = (yaml.safe_load(codes_path.read_text(encoding="utf-8")).get("prefixes", [])
                  if codes_path.exists() else [])
    defaults = cfg["defaults"]
    all_families = cfg["families"]
    families = ([f for f in all_families if f["id"] in args.only]
                if args.only else all_families)

    if args.max_minutes > 0:
        globals()["_deadline"] = time.monotonic() + args.max_minutes * 60

    manifest = load_manifest()
    today = utc_today().isoformat()

    all_statements: list[Statement] = []
    health: list[dict] = []
    for fam in (() if args.rebuild_only else families):
        try:
            sts, h = crawl_family(fam, defaults, manifest, args, today)
        except Exception as exc:  # noqa: BLE001
            sts, h = [], {"id": fam["id"], "name": fam["name"], "mode": fam.get("mode"),
                          "hub": fam["hub"], "checked": today, "status": "error",
                          "statements": 0, "candidates": 0,
                          "message": f"{type(exc).__name__}: {exc}"}
            print(f"    ! {type(exc).__name__}: {exc}", file=sys.stderr)
        all_statements.extend(sts)
        health.append(h)
        # Save after every family, not just at the end. A crawl that dies on
        # family 15 of 21 should not throw away the 14 families of downloads it
        # already paid for.
        save_manifest(manifest)

    save_manifest(manifest)

    # Assemble from the whole cache so a partial run updates its families
    # without erasing the others, and carry forward the last known health of
    # anything not crawled this time.
    prior_health, prior_funds, prior_total = {}, [], 0
    if OUT.exists():
        try:
            prior = json.loads(OUT.read_text(encoding="utf-8"))
            prior_health = {h["id"]: h for h in prior.get("health", [])}
            prior_funds = prior.get("funds", [])
            prior_total = prior.get("stats", {}).get("statements", 0)
        except Exception:
            pass
    crawled = {h["id"] for h in health}
    merged_health = health + [h for fid, h in prior_health.items()
                              if fid not in crawled
                              and fid in {f["id"] for f in all_families}]

    # Every family gets a health row, and a declared access level always wins
    # over a stale crawl result.
    #
    # Without this the nightly alert cried wolf: Sun Life and PIMCO were marked
    # manual-fetch, but their health still read "empty" from before that flag
    # existed, so the job would have opened an issue about them every night
    # forever - for two families behaving exactly as intended. Six other
    # families had no health row at all and were simply invisible.
    have = {h["id"] for h in merged_health}
    for fam in all_families:
        if fam["id"] not in have:
            merged_health.append({"id": fam["id"], "name": fam["name"],
                                  "mode": fam.get("mode"), "hub": fam.get("hub"),
                                  "checked": today, "status": "unknown",
                                  "statements": 0, "candidates": 0,
                                  "message": "not crawled yet"})
    by_id = {f["id"]: f for f in all_families}
    for h in merged_health:
        access = by_id.get(h["id"], {}).get("access")
        if access in NOT_CRAWLED:
            h["status"] = access
            h["message"] = (by_id[h["id"]].get("guidance") or h.get("message") or "").strip()

    # Carry forward families this run did not successfully crawl.
    #
    # The cache is not guaranteed to exist. On a fresh CI runner it starts
    # empty, so a `--only mackenzie` run rebuilt the index from a cache holding
    # nothing but Mackenzie, committed it, and published an index of 203
    # statements over one of 13,572. Rebuilding from the cache alone is only
    # safe when the cache is complete, and it never is on CI.
    #
    # A family is replaced by this run's results only when it actually crawled
    # OK. If it was skipped, blocked, timed out or errored, its previous
    # statements are kept - a fund company having a bad night must not delete
    # everything we already knew about them.
    stmts = statements_from_manifest(manifest, args.since_year)
    healthy_now = {h["id"] for h in health if h["status"] == "ok"}
    have_families = {s.family for s in stmts}

    carried = 0
    for f in prior_funds:
        fid = f.get("family")
        if fid in healthy_now or fid in have_families:
            continue
        for s in f.get("statements", []):
            if s.get("tax_year") and s["tax_year"] < args.since_year:
                continue
            stmts.append(Statement(
                family=fid, url=s["url"], fund_name=f.get("name"),
                tickers=f.get("tickers", []), period_end=s.get("period_end"),
                tax_year=s.get("tax_year"), sha256=s.get("sha256", ""),
                bytes=s.get("bytes", 0), confidence=s.get("confidence", 0),
                verified=True, first_seen="", last_seen="",
                fmt=s.get("fmt", "pdf"), figures=s.get("figures"),
                investees=s.get("investees"),
                fund_key=(f.get("id") or "").split("::", 1)[-1] or None,
                part=s.get("part")))
            carried += 1
    if carried:
        print(f"\ncarried forward {carried} statement(s) from families not "
              f"crawled or not healthy this run")
    merged_health.sort(key=lambda h: [f["id"] for f in all_families].index(h["id"])
                       if h["id"] in [f["id"] for f in all_families] else 99)

    index = build_index(stmts, all_families, merged_health, negs.get("negatives", []))
    # Client statements name funds by FundSERV code; PFIC statements never
    # print the code. This lets the site at least recognise a code and say
    # whose it is, instead of returning a blank "nothing found".
    index["fund_code_prefixes"] = fund_codes

    # Seatbelt. Publishing is automatic, so a bad index reaches the live site
    # within a minute. Any large collapse is treated as a bug until a human
    # says otherwise.
    new_total = index["stats"]["statements"]
    if prior_total > 100 and new_total < prior_total * 0.5 and not args.allow_shrink:
        print(f"\nREFUSING TO WRITE: statements would fall {prior_total} -> "
              f"{new_total} ({100 - round(100 * new_total / prior_total)}% drop).",
              file=sys.stderr)
        print("The index was NOT modified. If this shrink is genuine, re-run "
              "with --allow-shrink.", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # PyYAML turns bare `verified: 2026-08-18` into a date object; default=str
    # keeps the negatives register serializable without quoting every date.
    OUT.write_text(json.dumps(index, indent=1, default=str), encoding="utf-8")

    # The hosts the download proxy may fetch from: exactly the ones the index
    # links to, regenerated with it. This list used to be typed by hand into
    # the proxy and redeployed by hand, so every manager added after the first
    # deploy - BMO, National Bank, Ninepoint, Sprott, Desjardins, Guardian,
    # 731 statements between them - listed fine and then failed inside every
    # ZIP with "host not allowlisted".
    pdf_urls = [urlparse.urlsplit(s["url"]) for f in index["funds"]
                for s in f["statements"] if s.get("fmt", "pdf") == "pdf"]
    hosts = sorted({u.netloc.lower() for u in pdf_urls})
    # The proxy insists on a ".pdf" address, except for hosts whose statements
    # genuinely have none (Starlight serves /files/.../2025-PFIC-Annual-
    # Information-Statement). Either way the reply must be a real PDF.
    any_path = sorted({u.netloc.lower() for u in pdf_urls
                       if not u.path.lower().endswith(".pdf")})
    HOSTS_OUT.write_text(json.dumps(
        {"generated": index["generated"], "hosts": hosts, "any_path_hosts": any_path},
        indent=1), encoding="utf-8")

    s = index["stats"]
    print(f"\n{'='*62}")
    print(f"families ok : {s['families_ok']}/{s['families']}")
    print(f"funds       : {s['funds']}")
    print(f"statements  : {s['statements']}")
    print(f"years       : {', '.join(map(str, s['years'])) or '-'}")
    print(f"written     : {OUT.relative_to(REPO)}")

    if _deferred:
        print(f"\ntime budget of {args.max_minutes:g} minutes reached: {_deferred} "
              f"document(s) left for the next run")

    if _pdf_errors:
        print(f"\nPDF read failures ({sum(_pdf_errors.values())} documents):")
        for msg, n in sorted(_pdf_errors.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>5}x  {msg}")

    broken = [h for h in merged_health if h["status"] in NEEDS_ATTENTION]
    if broken:
        print(f"\nneeds attention ({len(broken)}):")
        for h in broken:
            print(f"  - {h['id']:<24} {h['status']:<9} {h['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
