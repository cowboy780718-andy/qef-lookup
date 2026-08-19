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
    content hash, so a nightly run re-downloads almost nothing. A statement
    whose hash changes is flagged loudly - managers do reissue corrected ones.

Usage:
    python crawl.py                  # everything
    python crawl.py --only mackenzie tdam
    python crawl.py --static-only    # skip headless-browser families
    python crawl.py --limit 25       # cap PDFs per family (for smoke tests)
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import dataclasses
import hashlib
import io
import json
import re
import sys
import threading
import time
import urllib.parse as urlparse
import urllib.robotparser as robotparser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
CACHE = ROOT / ".cache"
OUT = REPO / "web" / "data" / "index.json"

# Identification, and why it looks like this.
#
# Several of these hosts run WAFs that 403 any user-agent containing "bot",
# regardless of robots.txt. Mackenzie is one, and its robots.txt explicitly
# allows crawling with no crawl-delay - so the block is a crude heuristic, not
# a stated policy. We therefore send a normal browser UA (these are public
# documents a person can download by hand from the same URL) but we do NOT
# hide: every request carries X-Crawler and From headers naming the project,
# we obey robots.txt, and we rate-limit per host (honouring Crawl-delay).
# If a manager wants us gone, a robots.txt rule will do it.
BOT_ID = "QEFLookupBot/1.0 (+https://github.com/qef-lookup)"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "X-Crawler": BOT_ID,
    "From": "qef-lookup (see X-Crawler)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Bump when SIGNALS/scoring/parsers change, to invalidate cached verdicts.
VERIFY_VERSION = 3

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

_host_lock = defaultdict(threading.Lock)
_host_last = defaultdict(float)
_robots: dict[str, robotparser.RobotFileParser] = {}
_host_delays: dict[str, float] = {}
_robots_lock = threading.Lock()


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
            rp = robotparser.RobotFileParser()
            rp.set_url(base + "/robots.txt")
            try:
                resp = requests.get(base + "/robots.txt", headers=HEADERS, timeout=15)
                rp.parse(resp.text.splitlines() if resp.ok else [])
            except Exception:
                rp.parse([])
            _robots[base] = rp
            # Respect a published Crawl-delay, taking the stricter of the rule
            # for this crawler by name and the catch-all.
            delays = [d for d in (rp.crawl_delay(BOT_ID), rp.crawl_delay("*"))
                      if d is not None]
            if delays:
                _host_delays[parts.netloc] = max(float(max(delays)), PER_HOST_DELAY)
                print(f"    robots.txt Crawl-delay for {parts.netloc}: "
                      f"{_host_delays[parts.netloc]}s")
    try:
        # Honour both a rule naming this crawler specifically and the "*" rules
        # that our browser-shaped UA would fall under. Either one can veto.
        return rp.can_fetch(BOT_ID, url) and rp.can_fetch(UA, url)
    except Exception:
        return True


def fetch(url: str, *, binary: bool = False, extra_headers: dict | None = None):
    """Return (status, content, headers) or (None, None, {}) on failure."""
    if not robots_allows(url):
        return "robots-denied", None, {}
    _throttle(url)
    h = dict(HEADERS)
    if extra_headers:
        h.update(extra_headers)
    try:
        r = requests.get(url, headers=h, timeout=TIMEOUT, allow_redirects=True)
        return r.status_code, (r.content if binary else r.text), dict(r.headers)
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
            # Document lists commonly hide behind accordions or "load more".
            for sel in ("button:has-text('Accept')", "button:has-text('I agree')",
                        "button:has-text('Load more')", "button:has-text('Show all')",
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

_FUND_ANCHOR = re.compile(r"""\(\s*(?:the\s+)?["“”']?\s*Fund\s*["“”']?\s*\)""", re.I)

# Headline forms that name the fund outright; tried first.
# Only the explicit addressee line. A looser "Annual Information Statement
# of/for ..." rule was tried and had to be removed: it matched the document's
# own title block and returned "Year Ending December 31, 2024 1) This
# Information Statement ..." for five separate families that the anchor rule
# had been handling correctly.
_NAME_LEAD = [
    re.compile(r"(?:SECURITY\s*HOLDERS|SECURITYHOLDERS|UNITHOLDERS|SHAREHOLDERS|HOLDERS)"
               r"\s+OF\s*:?\s*(?:the\s+)?(?:Fund\s+)?(.{4,110}?)"
               r"\s*(?:\(|who\s+have|$)", re.I),
]

# CI Global's covering-letter layout puts the name in a table, after a run of
# repeated column headers that the text layer duplicates.
_NAME_TABLE = re.compile(r"(?:Distributions\s+){1,4}([A-Z][^()\n]{3,90}?)\s*\(", re.I)

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
    r"|\b(?:tax\s+filing\s+requirement|ordinary\s+earnings|"
    r"information\s+statement|year\s+end(?:ing|ed)|"
    r"passive\s+foreign\s+investment\s+company|u\.s\.\s+persons?\b)",
    re.I)

# Stripped when they lead a captured name.
_NAME_PREFIX_JUNK = re.compile(
    r"^(?:.*?(?:EXCHANGE\s+TRADED\s+FUNDS?|IMPORTANT\s+TAX\s+NOTICE[^:]*:?|"
    r"TD\s+Asset\s+Management(?=\s+TD)|"
    r"TO\s+U\.?S\.?\s+(?:PERSONS|INVESTORS|TAXPAYERS)[^:]*:?)\s*)", re.I)
TICKER_PATTERN = re.compile(r"\(([A-Z]{2,5}(?:\.[A-Z]{1,2})?)\)")


_pdf_errors: dict[str, int] = defaultdict(int)


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
        return "\n".join((reader.pages[i].extract_text() or "")
                         for i in range(min(pages, len(reader.pages))))
    except Exception as exc:  # noqa: BLE001
        _pdf_errors[f"{type(exc).__name__}: {str(exc)[:80]}"] += 1
        return ""


def verify_statement(text: str) -> tuple[bool, int]:
    if not text.strip():
        return False, 0
    score = sum(w for rx, w in SIGNALS if rx.search(text))
    score -= sum(w for rx, w in ANTI_SIGNALS if rx.search(text))
    return score >= 3, score


def parse_period(text: str) -> str | None:
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
    # The (the "Fund") marker ends the name wherever it appears, not only at
    # the end - Fidelity continues straight into boilerplate after it.
    name = _FUND_ANCHOR.split(name)[0].strip()
    name = re.sub(r"\(\s*(?:the\s+)?[\"“”']?\s*Fund\s*[\"“”']?\s*\)\s*$",
                  "", name, flags=re.I).strip()
    name = re.sub(r"\s*\(\s*formerly.*$", "", name, flags=re.I)
    # PDF text layers sometimes detach the first glyph: "R ussell Investments",
    # "M ulti-Asset", "I A Clarington". Rejoin a lone leading capital.
    name = re.sub(r"^([A-Z])\s+(?=[A-Za-z])", r"", name)
    name = _NAME_PREFIX_JUNK.sub("", name)
    name = re.sub(r"^(?:the\s+)", "", name, flags=re.I)
    name = re.sub(r"^Fund\s+(?=[A-Za-z])", "", name)
    # Drop a trailing exchange ticker - "(ETHX.B)", "(XSH)" - but keep
    # descriptive parentheticals such as "(CAD-Hedged)".
    name = re.sub(r"\s*\([A-Z]{2,5}(?:\.[A-Z]{1,2})?\)\s*$", "", name)
    name = name.strip(" .,:;-–—\"'“”()")
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

    # 2. Read backwards from the (the "Fund") anchor to a real delimiter, so the
    #    first character of the name survives.
    for m in _FUND_ANCHOR.finditer(flat):
        before = flat[max(0, m.start() - 160):m.start()]
        # Split on strong boundaries: a colon, a sentence end, or 2+ spaces.
        chunk = re.split(r":\s+|(?<=[a-z])\.\s+|\s{2,}|•", before)[-1]
        if (n := _clean_name(chunk)):
            return n

    # 3. CI-style table layout.
    m = _NAME_TABLE.search(flat)
    if m and (n := _clean_name(m.group(1))):
        return n
    return None


# Fund codes / tickers hiding in URLs. Each publisher encodes them differently,
# and for several (Mackenzie, BlackRock, Vanguard) the URL is the *only* place
# the ticker appears - it is not in the document text at all.
URL_CODE_PATTERNS = [
    re.compile(r"pfic[-_](?:stmt[-_])?\d{4}[-_]([a-z]{2,6})[-_.]", re.I),   # mackenzie, blackrock
    re.compile(r"/([A-Z]{2,5})_\d+_PFIC", re.I),                            # vanguard
    re.compile(r"/pfic[-_]([a-z]{2,6})\.pdf", re.I),                        # vanguard legacy
    re.compile(r"/([A-Z0-9]{4})_[A-Za-z]", ),                               # russell fund codes
]
_CODE_STOPWORDS = {"THE", "FUND", "ETF", "USD", "CAD", "PFIC", "QEF", "IRS",
                   "AIS", "US", "EN", "FR", "STMT", "ENV1", "SE", "PDFUA"}


def parse_tickers(text: str, url: str) -> list[str]:
    found = set(TICKER_PATTERN.findall(text[:1200]))
    for rx in URL_CODE_PATTERNS:
        m = rx.search(url)
        if m:
            found.add(m.group(1).upper())
    return sorted(c for c in found if c not in _CODE_STOPWORDS and not c.isdigit())


def tidy_name(name: str) -> str:
    """Publishers shout. Title-case all-caps names but keep real acronyms."""
    if not name or not name.isupper():
        return name
    keep = {"ETF", "US", "USA", "UK", "EAFE", "ESG", "REIT", "TSX", "GIC",
            "MSCI", "S&P", "EM", "HISA", "ADR", "II", "III", "IV"}
    out = []
    for w in name.split():
        core = w.strip("().,")
        out.append(w if core in keep else w.capitalize())
    return " ".join(out)


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


def load_manifest() -> dict:
    CACHE.mkdir(exist_ok=True)
    p = CACHE / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_manifest(m: dict) -> None:
    CACHE.mkdir(exist_ok=True)
    (CACHE / "manifest.json").write_text(json.dumps(m, indent=1), encoding="utf-8")


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
    if not (isinstance(st, int) and st == 200 and blob and blob.startswith(b"%PDF")):
        if browser:
            st, blob, _ = browser.get(d["list_url"])
        if not (blob and blob.startswith(b"%PDF")):
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

    this_year = datetime.now(timezone.utc).year
    oldest = max(since, this_year - int(d.get("years_back", 5)) + 1)
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
    years = [int(y) for y in _URL_YEAR.findall(urlparse.urlsplit(url).path)]
    return max(years) if years else None


def too_old(url: str, since: int) -> bool:
    """True only when the URL positively shows a year older than the floor.

    Deliberately one-sided: a URL with no readable year is kept and decided on
    the period parsed out of the document. Guessing 'old' from an unreadable URL
    would silently drop current statements.
    """
    y = url_year(url)
    return y is not None and y < since


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


def process_pdf(url: str, family_id: str, manifest: dict, today: str,
                getter=None) -> Statement | None:
    prev = manifest.get(url, {})
    headers = {}
    # Only trust a cached result if it was produced by the current verifier.
    # Otherwise a document rejected because of a crawler bug stays rejected
    # forever, since its ETag never changes. Bump VERIFY_VERSION whenever the
    # signal list, scoring or parsers change.
    if prev.get("etag") and prev.get("verify_version") == VERIFY_VERSION:
        headers["If-None-Match"] = prev["etag"]

    # Derived sources probe URL templates, so most candidates are misses by
    # design. Remember them: without this, BlackRock alone re-probes ~900 dead
    # URLs every single night. Misses for the two most recent tax years are
    # always re-checked, because that is exactly when a statement appears.
    if prev.get("miss") and prev.get("verify_version") == VERIFY_VERSION:
        yr = prev.get("probe_year") or 0
        if yr < datetime.now(timezone.utc).year - 1:
            return None

    # A published statement for a closed tax year never changes. Re-validating
    # every one of them nightly costs a request each and makes routine runs as
    # slow as the first build, for no information. So trust a recent verdict for
    # settled years, and always re-check the current and prior tax year, which
    # is where reissues and late publications actually happen.
    if (prev.get("verified") and prev.get("verify_version") == VERIFY_VERSION
            and prev.get("last_seen")):
        yr = prev.get("tax_year") or 0
        age_days = (datetime.now(timezone.utc).date()
                    - datetime.fromisoformat(prev["last_seen"]).date()).days
        if yr and yr <= datetime.now(timezone.utc).year - 2 and age_days < 30:
            prev["last_seen"] = today
            manifest[url] = prev
            return Statement(
                family=family_id, url=url, fund_name=prev.get("fund_name"),
                tickers=prev.get("tickers", []), period_end=prev.get("period_end"),
                tax_year=yr, sha256=prev.get("sha256", ""), bytes=prev.get("bytes", 0),
                confidence=prev.get("confidence", 0), verified=True,
                first_seen=prev.get("first_seen", today), last_seen=today,
            )

    if getter is None:
        status, blob, resp_headers = fetch(url, binary=True, extra_headers=headers)
    else:
        status, blob, resp_headers = getter(url)

    if not isinstance(status, int) or status != 200 or not blob or not blob.startswith(b"%PDF"):
        m = re.search(r"[-_/](20\d{2})[-_/]", url)
        manifest[url] = {"miss": True, "status": str(status), "last_seen": today,
                         "verify_version": VERIFY_VERSION, "family": family_id,
                         "probe_year": int(m.group(1)) if m else None}
        return None

    if status == 304 and prev.get("verified") is not None:
        prev["last_seen"] = today
        manifest[url] = prev
        if not prev.get("verified"):
            return None
        return Statement(
            family=family_id, url=url, fund_name=prev.get("fund_name"),
            tickers=prev.get("tickers", []), period_end=prev.get("period_end"),
            tax_year=prev.get("tax_year"), sha256=prev.get("sha256", ""),
            bytes=prev.get("bytes", 0), confidence=prev.get("confidence", 0),
            verified=True, first_seen=prev.get("first_seen", today), last_seen=today,
        )

    digest = hashlib.sha256(blob).hexdigest()
    if prev.get("sha256") and prev["sha256"] != digest:
        print(f"    * CONTENT CHANGED (reissue?): {url}")

    text = pdf_text(blob)
    ok, score = verify_statement(text)
    period = parse_period(text)
    name = parse_fund_name(text)
    tickers = parse_tickers(text, url)
    year = int(period[:4]) if period else None

    manifest[url] = {
        "verify_version": VERIFY_VERSION,
        "family": family_id,
        "etag": resp_headers.get("ETag"),
        "sha256": digest,
        "bytes": len(blob),
        "verified": ok,
        "confidence": score,
        "fund_name": name,
        "tickers": tickers,
        "period_end": period,
        "tax_year": year,
        "first_seen": prev.get("first_seen", today),
        "last_seen": today,
    }
    if not ok:
        return None
    if year is not None and year < SINCE_YEAR:
        return None
    return Statement(
        family=family_id, url=url, fund_name=name, tickers=tickers,
        period_end=period, tax_year=year, sha256=digest, bytes=len(blob),
        confidence=score, verified=True,
        first_seen=prev.get("first_seen", today), last_seen=today,
    )


def crawl_family(fam: dict, defaults: dict, manifest: dict, args, today: str):
    fid, mode = fam["id"], fam.get("mode", "static")
    health = {"id": fid, "name": fam["name"], "mode": mode, "hub": fam["hub"],
              "checked": today, "status": "unknown", "statements": 0, "candidates": 0,
              "message": ""}
    print(f"\n[{fid}] {fam['name']}  ({mode})")

    browser = BrowserSession()
    try:
        return _crawl_family_inner(fam, defaults, manifest, args, today, health, browser)
    finally:
        browser.close()


def _crawl_family_inner(fam: dict, defaults: dict, manifest: dict, args, today: str,
                        health: dict, browser: "BrowserSession"):
    fid, mode = fam["id"], fam.get("mode", "static")

    # Some managers publish statements but forbid automated retrieval. We take
    # that at face value rather than routing around it: the family stays in the
    # index as a deep link, flagged for manual fetch.
    if fam.get("access") == "manual":
        health["status"] = "manual"
        health["message"] = "publisher disallows crawling; fetch by hand from the hub"
        print("    - manual-fetch family (publisher disallows crawling)")
        return [], health

    html = None
    if mode == "browser":
        if args.static_only:
            health["status"] = "skipped"
            health["message"] = "browser mode skipped (--static-only)"
            print("    - skipped (static-only run)")
            return [], health
        html = browser.html(fam["hub"])
        if html is None:
            health["status"] = "degraded"
            health["message"] = "Playwright unavailable; fell back to plain fetch"
            print("    ! Playwright unavailable, falling back to plain fetch")

    if html is None:
        status, body, _ = fetch(fam["hub"])
        # Several hosts (Mackenzie, Global X, Sun Life) sit behind a WAF that
        # rejects Python's TLS fingerprint no matter what headers we send, while
        # serving the identical page to a browser and serving their PDFs to
        # anyone. Rather than spoof a TLS fingerprint, escalate to a real
        # browser for the hub page only. It is one request per family per run.
        if status in (403, 429) and not args.static_only:
            print(f"    - hub returned {status} over plain HTTP; escalating to browser")
            body = browser.html(fam["hub"])
            if body:
                health["message"] = f"hub requires browser (plain HTTP returned {status})"
                health["escalated"] = True
                status = 200
        if not isinstance(status, int) or status != 200 or not body:
            health["status"] = "down"
            health["message"] = f"hub fetch returned {status}"
            print(f"    ! hub fetch failed: {status}")
            return [], health
        html = body

    include = fam.get("link_include", defaults["link_include"])
    exclude = fam.get("link_exclude", defaults["link_exclude"])
    links = collect_links(html, fam["hub"], include, exclude)
    derived, derived_names = derive_candidates(fam, browser, args.since_year)
    links = sorted(set(links) | set(derived))
    health["derived_names"] = len(derived_names)

    # Drop pre-floor documents before spending a request on them. This is where
    # the crawl time actually goes: CI alone lists ~15,000 documents, and the
    # year is right there in the filename.
    before = len(links)
    links = [u for u in links if not too_old(u, args.since_year)]
    if before != len(links):
        health["skipped_old"] = before - len(links)
        print(f"    {before - len(links)} candidate(s) older than "
              f"{args.since_year} skipped by URL")
    if args.limit and len(links) > args.limit:
        # Sample evenly rather than taking the head. Links sort alphabetically,
        # which usually means by year, so a head slice would test only the
        # oldest catalogue - exactly the entries most likely to be dead.
        step = len(links) / args.limit
        links = [links[int(i * step)] for i in range(args.limit)]
    health["candidates"] = len(links)
    print(f"    {len(links)} candidate PDF link(s)")

    if not links:
        health["status"] = "empty"
        health["message"] = "hub reachable but no candidate PDFs matched"
        return [], health

    # Probe one document over plain HTTP. If the host answers with something
    # that is not a PDF (iA Clarington serves a 404 HTML page to non-browsers
    # while serving the real file to Chromium), switch the whole family to
    # browser fetching rather than discarding a catalogue that is really there.
    # Sample a spread of documents, not just the first. Publishers leave dead
    # links on their pages for years (iA Clarington still lists 2021 files that
    # 404), so a single failed probe proves nothing about the host.
    probe_idx = sorted({0, len(links) // 2, len(links) - 1})
    probe_results = []
    plain_works = False
    for i in probe_idx:
        st, blob, _ = fetch(links[i], binary=True)
        probe_results.append(st)
        if isinstance(st, int) and st == 200 and blob and blob.startswith(b"%PDF"):
            plain_works = True
            break

    use_browser_pdfs = False
    if not plain_works:
        if args.static_only:
            health["status"] = "blocked"
            health["message"] = (f"PDFs need a browser (probes: {probe_results}); "
                                 f"skipped on --static-only")
            print(f"    - PDFs need browser (probes {probe_results}); skipped")
            return [], health
        use_browser_pdfs = True
        health["pdf_mode"] = "browser"
        print(f"    - plain HTTP probes {probe_results}; using browser for documents")

    statements: list[Statement] = []
    if use_browser_pdfs:
        # Playwright's sync API is single-threaded; go sequentially.
        for i, u in enumerate(links, 1):
            st = process_pdf(u, fid, manifest, today, getter=browser.get)
            if st:
                statements.append(st)
            if i % 25 == 0:
                print(f"      {i}/{len(links)} fetched")
            if i % 200 == 0:
                save_manifest(manifest)
    else:
        with futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            # Checkpoint periodically. CI Global alone lists ~15,000 documents;
            # losing hours of downloads because the run was interrupted between
            # family boundaries is not an acceptable failure mode.
            for n, st in enumerate(
                    pool.map(lambda u: process_pdf(u, fid, manifest, today), links), 1):
                if st:
                    statements.append(st)
                if n % 250 == 0:
                    save_manifest(manifest)
                    print(f"      {n}/{len(links)} processed")

    health["statements"] = len(statements)
    health["status"] = "ok" if statements else "empty"
    if not statements:
        health["message"] = "candidates found but none verified as PFIC statements"
    print(f"    {len(statements)} verified statement(s)")
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
        if year is not None and year < since:
            continue
        out.append(Statement(
            family=v.get("family", "unknown"), url=url,
            fund_name=v.get("fund_name"), tickers=v.get("tickers", []),
            period_end=v.get("period_end"), tax_year=year,
            sha256=v.get("sha256", ""), bytes=v.get("bytes", 0),
            confidence=v.get("confidence", 0), verified=True,
            first_seen=v.get("first_seen", ""), last_seen=v.get("last_seen", ""),
        ))
    return out


def build_index(all_statements: list[Statement], families: list[dict],
                health: list[dict], negatives: list[dict]) -> dict:
    fam_by_id = {f["id"]: f for f in families}
    funds: dict[str, dict] = {}

    for st in all_statements:
        name = tidy_name(st.fund_name or
                         Path(urlparse.urlsplit(st.url).path).stem.replace("_", " "))
        key = f"{st.family}::{slug(name)}"
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
        f["statements"].append({
            "period_end": st.period_end,
            "tax_year": st.tax_year,
            "url": st.url,
            "bytes": st.bytes,
            "sha256": st.sha256[:16],
            "confidence": st.confidence,
        })

    for f in funds.values():
        f["statements"].sort(key=lambda s: (s["period_end"] or ""), reverse=True)
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
            {k: fam.get(k) for k in ("id", "name", "country", "mode", "fye", "hub", "notes")}
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
    ap.add_argument("--limit", type=int, default=0, help="max PDFs per family")
    ap.add_argument("--since-year", type=int, default=SINCE_YEAR,
                    help=f"oldest tax year to index (default {SINCE_YEAR}); "
                         "raising this is the biggest lever on crawl time")
    args = ap.parse_args()
    globals()["SINCE_YEAR"] = args.since_year

    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    negs = yaml.safe_load((ROOT / "negatives.yaml").read_text(encoding="utf-8"))
    defaults = cfg["defaults"]
    all_families = cfg["families"]
    families = ([f for f in all_families if f["id"] in args.only]
                if args.only else all_families)

    manifest = load_manifest()
    today = datetime.now(timezone.utc).date().isoformat()

    all_statements: list[Statement] = []
    health: list[dict] = []
    for fam in families:
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
    prior_health = {}
    if OUT.exists():
        try:
            prior_health = {h["id"]: h for h in
                            json.loads(OUT.read_text(encoding="utf-8")).get("health", [])}
        except Exception:
            pass
    crawled = {h["id"] for h in health}
    merged_health = health + [h for fid, h in prior_health.items()
                              if fid not in crawled
                              and fid in {f["id"] for f in all_families}]
    merged_health.sort(key=lambda h: [f["id"] for f in all_families].index(h["id"])
                       if h["id"] in [f["id"] for f in all_families] else 99)

    index = build_index(statements_from_manifest(manifest, args.since_year),
                        all_families, merged_health, negs.get("negatives", []))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # PyYAML turns bare `verified: 2026-08-18` into a date object; default=str
    # keeps the negatives register serializable without quoting every date.
    OUT.write_text(json.dumps(index, indent=1, default=str), encoding="utf-8")

    s = index["stats"]
    print(f"\n{'='*62}")
    print(f"families ok : {s['families_ok']}/{s['families']}")
    print(f"funds       : {s['funds']}")
    print(f"statements  : {s['statements']}")
    print(f"years       : {', '.join(map(str, s['years'])) or '-'}")
    print(f"written     : {OUT.relative_to(REPO)}")

    if _pdf_errors:
        print(f"\nPDF read failures ({sum(_pdf_errors.values())} documents):")
        for msg, n in sorted(_pdf_errors.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>5}x  {msg}")

    broken = [h for h in merged_health if h["status"] in ("down", "error", "empty")]
    if broken:
        print(f"\nneeds attention ({len(broken)}):")
        for h in broken:
            print(f"  - {h['id']:<24} {h['status']:<9} {h['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
