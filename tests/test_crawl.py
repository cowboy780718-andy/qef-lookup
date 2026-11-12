"""Tests for the crawler's decision logic. No network access.

    python -m unittest discover -s tests -v

Everything here guards a failure that actually happened in production, and the
test names say which. Run them before pushing a change to crawler/crawl.py.
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "crawler"))
import crawl  # noqa: E402

STATEMENT_TEXT = (
    "Example Global Equity Fund PFIC Annual Information Statement "
    "for the year ended December 31, 2025. The Fund is a qualified electing fund "
    "under section 1295. Ordinary earnings per unit 0.01. Net capital gain per unit 0.02. "
    "Form 8621."
)
PDF = b"%PDF-1.7 fake body"
TODAY = "2026-11-11"


def args(**kw):
    base = dict(static_only=True, limit=0, since_year=2021, refresh=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


class Patched(unittest.TestCase):
    """Replace the network and the PDF reader with scripted stand-ins."""

    def setUp(self):
        self._saved = {n: getattr(crawl, n) for n in
                       ("fetch", "curl_fetch", "pdf_text", "SINCE_YEAR")}
        crawl.SINCE_YEAR = 2021
        crawl.pdf_text = lambda blob, pages=3: STATEMENT_TEXT
        self.calls = []

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(crawl, n, v)

    def serve(self, responder):
        def fake(url, *, binary=False, extra_headers=None, json_body=None):
            self.calls.append((url, dict(extra_headers or {})))
            return responder(url, extra_headers or {})
        crawl.fetch = fake
        crawl.curl_fetch = lambda url, *, binary=False: responder(url, {})


class RobotsRules(unittest.TestCase):
    def test_longest_match_beats_file_order(self):
        # iA Clarington: the standard-library parser read this as "allowed".
        r = crawl.Robots("User-agent: *\nAllow: /*\nDisallow: /docs/\n")
        self.assertFalse(r.allowed("/docs/default-source/pfic/x.pdf"))
        self.assertTrue(r.allowed("/applicationsandforms/pficstatements"))

    def test_blanket_disallow_of_a_prefix(self):
        r = crawl.Robots("User-agent: *\nDisallow: /content\n")
        self.assertFalse(r.allowed("/content/dam/tax/pfic.pdf"))
        self.assertTrue(r.allowed("/advisor/ca/en/tax-centre.html"))

    def test_allow_root_with_specific_disallow(self):
        r = crawl.Robots("User-agent: *\nDisallow: /-/media/Dimensional/*\nAllow: /\n")
        self.assertTrue(r.allowed("/chmedia/236385/source/download/pfic.pdf"))
        self.assertFalse(r.allowed("/-/media/Dimensional/x.pdf"))

    def test_wildcard_and_end_anchor(self):
        r = crawl.Robots("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(r.allowed("/a/b/c.pdf"))
        self.assertTrue(r.allowed("/a/b/c.pdf?download=1"))

    def test_rule_naming_this_crawler_can_veto(self):
        r = crawl.Robots("User-agent: QEFLookupBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
        self.assertFalse(r.allowed("/anything"))

    def test_empty_disallow_and_missing_file_allow_everything(self):
        self.assertTrue(crawl.Robots("User-agent: *\nDisallow:\n").allowed("/x"))
        self.assertTrue(crawl.Robots("").allowed("/x"))

    def test_crawl_delay_takes_the_stricter_group(self):
        r = crawl.Robots("User-agent: *\nCrawl-delay: 30\n\nUser-agent: qeflookupbot\nCrawl-delay: 5\n")
        self.assertEqual(r.crawl_delay(), 30)


class RecheckSchedule(unittest.TestCase):
    def test_each_url_falls_due_once_per_window(self):
        from datetime import date, timedelta
        start = date(2026, 11, 1)
        for url in ("https://a.example/x.pdf", "https://a.example/y.pdf"):
            due = [d for d in range(30)
                   if crawl.recheck_due(url, (start + timedelta(days=d - 1)).isoformat(),
                                        30, (start + timedelta(days=d)).isoformat())]
            self.assertEqual(len(due), 1, url)

    def test_load_is_spread_not_bunched(self):
        # The catalogue must not all fall due on the same night.
        urls = [f"https://a.example/doc-{i}.pdf" for i in range(3000)]
        due = sum(crawl.recheck_due(u, "2026-11-10", 30, TODAY) for u in urls)
        self.assertGreater(due, 50)
        self.assertLess(due, 160)       # ~1/30th, not all 3000

    def test_two_missed_windows_forces_a_check(self):
        url = "https://a.example/x.pdf"
        self.assertTrue(crawl.recheck_due(url, "2026-08-01", 30, TODAY))
        self.assertTrue(crawl.recheck_due(url, None, 30, TODAY))


class CacheDecisions(unittest.TestCase):
    URL = "https://a.example/pfic-2024-abc.pdf"

    def rec(self, **kw):
        base = {"verify_version": crawl.VERIFY_VERSION, "verified": True,
                "tax_year": 2024, "checked": "2026-11-10", "last_seen": "2026-11-10"}
        base.update(kw)
        return base

    def quiet_day(self, url, window):
        """A date that is not this URL's recheck night."""
        from datetime import date, timedelta
        d = date(2026, 11, 11)
        while crawl.recheck_due(url, (d - timedelta(days=1)).isoformat(), window, d.isoformat()):
            d += timedelta(days=1)
        return d.isoformat(), (d - timedelta(days=1)).isoformat()

    def test_never_seen_is_always_fetched(self):
        self.assertTrue(crawl.needs_fetch(self.URL, {}, TODAY))

    def test_verified_document_is_left_alone_between_checks(self):
        today, yesterday = self.quiet_day(self.URL, crawl.RECHECK_DAYS_CLOSED)
        self.assertFalse(crawl.needs_fetch(self.URL, self.rec(checked=yesterday), today))

    def test_a_trusted_document_does_come_due_again(self):
        # The earlier fix refreshed the timestamp every night it trusted the
        # cache, so the trust never expired and nothing was ever rechecked.
        self.assertTrue(crawl.needs_fetch(self.URL, self.rec(checked="2026-07-01"), TODAY))

    def test_failed_last_night_is_retried(self):
        today, yesterday = self.quiet_day(self.URL, crawl.RECHECK_DAYS_CLOSED)
        self.assertTrue(crawl.needs_fetch(
            self.URL, self.rec(checked=yesterday, last_error="503"), today))

    def test_old_year_404_is_final_but_recent_404_is_not(self):
        old = "https://a.example/pfic-2022-abc.pdf"
        miss = {"miss": True, "status": "404", "verify_version": crawl.VERIFY_VERSION,
                "probe_year": 2022, "checked": "2026-01-01"}
        self.assertFalse(crawl.needs_fetch(old, miss, TODAY))
        recent = dict(miss, probe_year=2026)
        self.assertTrue(crawl.needs_fetch(old, recent, TODAY))    # long overdue

    def test_a_refusal_is_not_remembered_as_a_miss(self):
        # Manulife: a batch cached while the host returned 403 was never retried.
        miss = {"miss": True, "status": "403", "verify_version": crawl.VERIFY_VERSION,
                "probe_year": 2022, "checked": "2026-11-10"}
        self.assertTrue(crawl.needs_fetch("https://a.example/pfic-2022-x.pdf", miss, TODAY))

    def test_rejection_by_an_older_verifier_is_retried_at_once(self):
        today, yesterday = self.quiet_day(self.URL, crawl.RECHECK_DAYS_CLOSED)
        rej = self.rec(verified=False, checked=yesterday)
        self.assertFalse(crawl.needs_fetch(self.URL, rej, today))
        rej["verify_version"] = crawl.VERIFY_VERSION - 1
        self.assertTrue(crawl.needs_fetch(self.URL, rej, today))


class ProcessPdf(Patched):
    URL = "https://a.example/docs/pfic-2025-abc.pdf"

    def test_new_document_is_verified_and_recorded(self):
        self.serve(lambda u, h: (200, PDF, {"ETag": '"v1"', "Last-Modified": "Mon"}))
        m = {}
        st = crawl.process_pdf(self.URL, "fam", m, TODAY)
        self.assertIsNotNone(st)
        self.assertEqual(st.tax_year, 2025)
        self.assertEqual(st.period_end, "2025-12-31")
        self.assertEqual(m[self.URL]["checked"], TODAY)
        self.assertEqual(m[self.URL]["etag"], '"v1"')

    def test_304_keeps_the_statement(self):
        # A 304 used to fall into the "not a PDF" branch and be recorded as a
        # miss, deleting a statement precisely because it had not changed.
        self.serve(lambda u, h: (200, PDF, {"ETag": '"v1"'}))
        m = {}
        crawl.process_pdf(self.URL, "fam", m, "2026-06-01")
        self.serve(lambda u, h: (304, b"", {}) if h.get("If-None-Match") == '"v1"'
                   else (500, None, {}))
        st = crawl.process_pdf(self.URL, "fam", m, TODAY)
        self.assertIsNotNone(st)
        self.assertTrue(m[self.URL]["verified"])
        self.assertEqual(m[self.URL]["checked"], TODAY)
        self.assertNotIn("miss", m[self.URL])

    def test_one_bad_night_does_not_erase_a_statement(self):
        self.serve(lambda u, h: (200, PDF, {}))
        m = {}
        crawl.process_pdf(self.URL, "fam", m, "2026-06-01")
        for status in (503, 403, "error:Timeout", 429):
            self.serve(lambda u, h, s=status: (s, None, {}))
            st = crawl.process_pdf(self.URL, "fam", m, TODAY)
            self.assertIsNotNone(st, status)
            self.assertTrue(m[self.URL]["verified"])
            self.assertEqual(m[self.URL]["last_error"], str(status))
        self.serve(lambda u, h: (200, PDF, {}))
        crawl.process_pdf(self.URL, "fam", m, TODAY)
        self.assertNotIn("last_error", m[self.URL])

    def test_gone_three_nights_running_is_dropped(self):
        self.serve(lambda u, h: (200, PDF, {}))
        m = {}
        crawl.process_pdf(self.URL, "fam", m, "2026-06-01")
        self.serve(lambda u, h: (404, b"<html>", {}))
        self.assertIsNotNone(crawl.process_pdf(self.URL, "fam", m, "2026-11-09"))
        self.assertIsNotNone(crawl.process_pdf(self.URL, "fam", m, "2026-11-10"))
        self.assertIsNone(crawl.process_pdf(self.URL, "fam", m, TODAY))
        self.assertTrue(m[self.URL]["miss"])

    def test_a_timeout_between_404s_resets_the_count(self):
        self.serve(lambda u, h: (200, PDF, {}))
        m = {}
        crawl.process_pdf(self.URL, "fam", m, "2026-06-01")
        for status in (404, 404, "error:Timeout", 404, 404):
            self.serve(lambda u, h, s=status: (s, None, {}))
            self.assertIsNotNone(crawl.process_pdf(self.URL, "fam", m, TODAY), status)

    def test_unreadable_download_of_a_known_statement_is_not_a_verdict(self):
        self.serve(lambda u, h: (200, PDF, {}))
        m = {}
        crawl.process_pdf(self.URL, "fam", m, "2026-06-01")
        crawl.pdf_text = lambda blob, pages=3: ""
        st = crawl.process_pdf(self.URL, "fam", m, TODAY)
        self.assertIsNotNone(st)
        self.assertTrue(m[self.URL]["verified"])
        self.assertEqual(m[self.URL]["last_error"], "unreadable")

    def test_html_error_page_served_with_200_is_not_a_document(self):
        self.serve(lambda u, h: (200, b"<!doctype html><title>Not found</title>", {}))
        m = {}
        self.assertIsNone(crawl.process_pdf(self.URL, "fam", m, TODAY))
        self.assertEqual(m[self.URL]["status"], "not-a-pdf")

    def test_breaker_stops_asking_a_host_that_keeps_refusing(self):
        self.serve(lambda u, h: (403, None, {}))
        m, breaker = {}, crawl.Breaker(limit=5)
        for i in range(20):
            crawl.process_pdf(f"https://a.example/pfic-2025-{i}.pdf", "fam", m, TODAY,
                              breaker=breaker)
        self.assertTrue(breaker.tripped)
        self.assertEqual(len(self.calls), 5)

    def test_404s_never_trip_the_breaker(self):
        self.serve(lambda u, h: (404, None, {}))
        m, breaker = {}, crawl.Breaker(limit=5)
        for i in range(40):
            crawl.process_pdf(f"https://a.example/pfic-2025-{i}.pdf", "fam", m, TODAY,
                              breaker=breaker)
        self.assertFalse(breaker.tripped)


class FetchDocuments(Patched):
    FAM = {"id": "fam", "derive": {"kind": "ticker_list_pdf"}}

    def test_a_run_of_404_probes_is_not_reported_as_blocked(self):
        # BlackRock: candidates are built from templates, so most never
        # existed. Three 404 probes had it reported "blocked" for weeks.
        good = {f"https://a.example/pfic-stmt-2025-t{i}-en.pdf" for i in range(0, 60, 7)}
        self.serve(lambda u, h: (200, PDF, {}) if u in good else (404, b"", {}))
        links = sorted(f"https://a.example/pfic-stmt-2025-t{i}-en.pdf" for i in range(60))
        health = {}
        out = crawl.fetch_documents(links, self.FAM, {}, TODAY, None, args(), health)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(len(out), len(good))

    def test_nothing_published_yet_is_empty_not_blocked(self):
        self.serve(lambda u, h: (404, b"", {}))
        links = [f"https://a.example/pfic-stmt-2026-t{i}-en.pdf" for i in range(30)]
        health = {}
        crawl.fetch_documents(links, self.FAM, {}, TODAY, None, args(), health)
        self.assertEqual(health["status"], "empty")

    def test_real_refusals_are_reported_and_the_cache_is_kept(self):
        self.serve(lambda u, h: (200, PDF, {}))
        m, links = {}, [f"https://a.example/pfic-2025-{i}.pdf" for i in range(6)]
        crawl.fetch_documents(links, {"id": "fam"}, m, "2026-06-01", None, args(), {})
        self.serve(lambda u, h: (403, None, {}))
        for v in m.values():
            v["checked"] = "2026-01-01"          # everything overdue
        health = {}
        out = crawl.fetch_documents(links, {"id": "fam"}, m, TODAY, None, args(), health)
        self.assertEqual(health["status"], "blocked")
        self.assertTrue(all(v["verified"] for v in m.values()))
        self.assertEqual(out, [])                # index is rebuilt from the cache, which is intact

    def test_a_quiet_night_makes_no_requests_at_all(self):
        self.serve(lambda u, h: (200, PDF, {}))
        m, links = {}, [f"https://a.example/pfic-2023-{i}.pdf" for i in range(40)]
        crawl.fetch_documents(links, {"id": "fam"}, m, "2026-06-01", None, args(), {})
        self.calls.clear()
        health = {}
        out = crawl.fetch_documents(links, {"id": "fam"}, m, "2026-06-02", None, args(), health)
        self.assertEqual(len(out), 40)
        self.assertLessEqual(len(self.calls), 6)     # only those whose night it is
        self.assertEqual(health["status"], "ok")

    def test_probe_result_is_reused_not_fetched_twice(self):
        self.serve(lambda u, h: (200, PDF, {}))
        links = [f"https://a.example/pfic-2025-{i}.pdf" for i in range(5)]
        crawl.fetch_documents(links, {"id": "fam"}, {}, TODAY, None, args(), {})
        fetched = [u for u, _ in self.calls]
        self.assertEqual(len(fetched), len(set(fetched)))


HAMILTON_PAGE = """
<html><body><h1>PFIC Annual Information Statement</h1>
<p>Qualified Electing Fund information under section 1295, for Form 8621.
For the Year Ended April 30, 2026</p>
<table>
 <tr><th>Ticker</th><th>Fund</th><th>Series</th>
     <th>Ordinary Earnings (US$) Per Unit/Day</th>
     <th>Net Capital Gains (US$) Per Unit/Day</th>
     <th>Cash/Property Distributions (US$) Per Unit/Year</th></tr>
 <tr><td>HYLD</td><td>Hamilton Enhanced U.S. Covered Call ETF</td><td>Class E</td>
     <td>&#8211;</td><td>0.0007654617</td><td>1.3092774133</td></tr>
 <tr><td>HYLD</td><td>Hamilton Enhanced U.S. Covered Call ETF</td><td>Class U</td>
     <td>&#8211;</td><td>0.0011213232</td><td>1.8036773312</td></tr>
 <tr><td>SMVP</td><td>HAMILTON CHAMPIONS U.S. Dividend Index ETF</td><td>Class E</td>
     <td>0.0020602801</td><td>&#8211;</td><td>0.2699880120</td></tr>
</table>
<p>For HYLD Series E Shareholders</p>
<table>
 <tr><th>Direct Investee Funds of the Fund</th><th></th>
     <th>Ordinary Earnings (US$) Per Unit/Day</th>
     <th>Net Capital Gains (US$) Per Unit/Day</th>
     <th>Cash/Property Distributions (US$) Per Unit/Year</th></tr>
 <tr><td>HAMILTON CHAMPIONS U.S. Dividend Index ETF</td>
     <td>0.00005982846</td><td>&#8211;</td><td>0.0000208419</td></tr>
</table>
</body></html>
"""


class HtmlStatements(unittest.TestCase):
    def setUp(self):
        self.rows = crawl.parse_html_statements(HAMILTON_PAGE, "https://h.example/pfic-2026/")
        self.by_name = {r["fund_name"]: r for r in self.rows}

    def test_only_the_main_table_makes_statements(self):
        # The investee table's row must not appear as a fund's own statement.
        self.assertEqual(len(self.rows), 3)

    def test_classes_of_one_fund_stay_separate(self):
        e = self.by_name["Hamilton Enhanced U.S. Covered Call ETF - Class E"]
        u = self.by_name["Hamilton Enhanced U.S. Covered Call ETF - Class U"]
        self.assertNotEqual(e["figures"], u["figures"])
        self.assertNotEqual(e["fund_key"], u["fund_key"])

    def test_fund_own_figures_are_not_overwritten_by_an_investee_row(self):
        smvp = self.by_name["HAMILTON CHAMPIONS U.S. Dividend Index ETF - Class E"]
        self.assertEqual(smvp["figures"]["ordinary earnings (us$) per unit/day"], "0.0020602801")
        self.assertNotIn("investees", smvp)

    def test_investee_rows_attach_to_the_parent_with_the_right_headings(self):
        e = self.by_name["Hamilton Enhanced U.S. Covered Call ETF - Class E"]
        self.assertEqual(len(e["investees"]), 1)
        figs = e["investees"][0]["figures"]
        # The table has a stray empty heading cell; figures must not shift
        # one column to the right.
        self.assertEqual(figs["ordinary earnings (us$) per unit/day"], "0.00005982846")
        self.assertEqual(figs["cash/property distributions (us$) per unit/year"], "0.0000208419")
        self.assertNotIn("investees",
                         self.by_name["Hamilton Enhanced U.S. Covered Call ETF - Class U"])

    def test_period_and_published_dashes_are_kept(self):
        e = self.by_name["Hamilton Enhanced U.S. Covered Call ETF - Class E"]
        self.assertEqual(e["period_end"], "2026-04-30")
        self.assertEqual(e["figures"]["ordinary earnings (us$) per unit/day"], "–")

    def test_a_page_that_is_not_a_statement_yields_nothing(self):
        self.assertEqual(crawl.parse_html_statements(
            "<table><tr><th>Fund</th><th>Distribution</th></tr>"
            "<tr><td>Some Fund Name</td><td>1.0</td></tr></table>", "https://x/"), [])


class LinkCollection(unittest.TestCase):
    def test_section_links_ignore_filenames_and_respect_the_section(self):
        html = ('<a href="/a/prospectus.pdf">p</a>'
                '<div class="h">PFIC</div></button>'
                '<a href="/a/26776e52c358.pdf">International Equity Fund</a>'
                '<a href="/a/aa066765b867.pdf">U.S. Equity Fund</a>'
                '<div class="h">Other</div><a href="/a/other.pdf">o</a>')
        cfg = {"start": r">\s*PFIC\s*</div>\s*</button>", "end": r'<div class="h">'}
        got = crawl.section_links(html, "https://m.example/documents", cfg, [])
        self.assertEqual(got, ["https://m.example/a/26776e52c358.pdf",
                               "https://m.example/a/aa066765b867.pdf"])

    def test_json_links_are_found_at_any_depth(self):
        page = ('<script id="__NEXT_DATA__" type="application/json">'
                '{"a":{"b":[{"url":"https://cdn.example/x/pfic-2025.pdf"},'
                '{"url":"https://cdn.example/x/prospectus.pdf"}]}}</script>')
        got = crawl.collect_json_links(page, "https://s.example/", ["(?i)pfic"], [])
        self.assertEqual(got, ["https://cdn.example/x/pfic-2025.pdf"])

    def test_year_floor_only_drops_urls_that_show_an_old_year(self):
        self.assertTrue(crawl.too_old("https://a/legal/2019/pfic/x.pdf", 2021))
        self.assertFalse(crawl.too_old("https://a/legal/2024/pfic/x.pdf", 2021))
        self.assertFalse(crawl.too_old("https://a/docs/26776e52c358.pdf", 2021))


class Parsing(unittest.TestCase):
    def test_verifier_rejects_faq_and_accepts_a_statement(self):
        self.assertTrue(crawl.verify_statement(STATEMENT_TEXT)[0])
        self.assertFalse(crawl.verify_statement(
            "PFIC Frequently Asked Questions. What is a qualified electing fund?")[0])
        self.assertFalse(crawl.verify_statement("")[0])

    def test_period_is_read_from_the_text_not_assumed(self):
        self.assertEqual(crawl.parse_period("for the year ended June 30, 2025"), "2025-06-30")
        self.assertEqual(crawl.parse_period("taxable year ending March 31, 2024"), "2024-03-31")
        self.assertIsNone(crawl.parse_period("no dates here"))

    def test_fund_name_keeps_its_first_letter(self):
        text = ('R ussell Investments Global Equity Pool (the "Fund") is a passive '
                'foreign investment company.')
        self.assertEqual(crawl.parse_fund_name(text), "Russell Investments Global Equity Pool")

    def test_boilerplate_is_never_returned_as_a_fund_name(self):
        text = ('You may elect to treat the mutual fund as a Qualified Electing Fund '
                '(the "Fund") under the Code.')
        self.assertIsNone(crawl.parse_fund_name(text))

    def test_series_is_kept_in_the_name(self):
        text = "RBC Global Equity Fund - Series F PFIC Annual Information Statement 2025"
        self.assertEqual(crawl.parse_fund_name(text), "RBC Global Equity Fund - Series F")

    def test_lone_capital_cleanup_does_not_inject_control_characters(self):
        name = crawl._clean_name("R ussell Investments Canadian Equity Fund")
        self.assertEqual(name, "Russell Investments Canadian Equity Fund")
        self.assertTrue(all(ord(c) >= 32 for c in name))


class IndexAssembly(unittest.TestCase):
    def st(self, **kw):
        base = dict(family="fam", url="https://a/x.pdf", fund_name="Alpha Fund", tickers=[],
                    period_end="2025-12-31", tax_year=2025, sha256="a" * 64, bytes=100,
                    confidence=9, verified=True, first_seen="", last_seen="")
        base.update(kw)
        return crawl.Statement(**base)

    def test_renamed_fund_stays_on_one_row_and_keeps_the_old_name_beside_it(self):
        idx = crawl.build_index([
            self.st(fund_name="Hamilton Enhanced Multi Sector Covered Call ETF - Class E",
                    period_end="2025-04-30", tax_year=2025, fund_key="hdiv-e", fmt="html",
                    bytes=0, sha256="b" * 64, url="https://h/2025/"),
            self.st(fund_name="Hamilton Enhanced Canadian Covered Call ETF - Class E",
                    period_end="2026-04-30", tax_year=2026, fund_key="hdiv-e", fmt="html",
                    bytes=0, sha256="c" * 64, url="https://h/2026/"),
        ], [{"id": "fam", "name": "Fam"}], [], [])
        self.assertEqual(len(idx["funds"]), 1)
        fund = idx["funds"][0]
        self.assertEqual(fund["name"], "Hamilton Enhanced Canadian Covered Call ETF - Class E")
        self.assertNotIn("name", fund["statements"][0])
        self.assertIn("Multi Sector", fund["statements"][1]["name"])

    def test_identical_twin_documents_are_shown_once_but_different_ones_both_kept(self):
        idx = crawl.build_index([
            self.st(url="https://a/1.pdf"), self.st(url="https://a/2.pdf"),
            self.st(url="https://a/3.pdf", bytes=250),
        ], [{"id": "fam", "name": "Fam"}], [], [])
        self.assertEqual(len(idx["funds"][0]["statements"]), 2)
        self.assertEqual(idx["funds"][0]["duplicates_hidden"], 1)

    def test_two_table_rows_for_one_period_are_not_mistaken_for_twins(self):
        idx = crawl.build_index([
            self.st(fmt="html", bytes=0, sha256="d" * 64, url="https://h/p/"),
            self.st(fmt="html", bytes=0, sha256="e" * 64, url="https://h/p/"),
        ], [{"id": "fam", "name": "Fam"}], [], [])
        self.assertEqual(len(idx["funds"][0]["statements"]), 2)


GUARDIAN_PAGE = ('Important Tax Notice to U.S. Shareholders of {name} This statement is '
                 'provided to unitholders who are United States persons. {name} (the "Fund") may '
                 'be a PFIC for the year ending December 31, 2024. PFIC Annual Information '
                 'Statement. Qualified electing fund. Ordinary earnings. Net capital gain.')


class CombinedDocuments(unittest.TestCase):
    def test_a_stapled_bundle_yields_one_entry_per_fund_at_its_own_page(self):
        # Guardian: 86 pages, ~29 funds, and only the first could be found.
        pages = []
        for name in ("Guardian Canadian Equity Select Fund", "Guardian Canadian Bond Fund",
                     "Guardian US Equity Select Fund"):
            pages += [GUARDIAN_PAGE.format(name=name), GUARDIAN_PAGE.format(name=name),
                      "4) The Fund will, upon receipt of a request, permit you to inspect"]
        parts = crawl.split_document(pages, {"by": "pages"})
        self.assertEqual([(p["name"], p["page"]) for p in parts],
                         [("Guardian Canadian Equity Select Fund", 1),
                          ("Guardian Canadian Bond Fund", 4),
                          ("Guardian US Equity Select Fund", 7)])
        self.assertEqual(parts[0]["period_end"], "2024-12-31")

    def test_table_rows_become_funds_and_index_names_are_not_mistaken_for_tickers(self):
        page = ("Ticker Fund Series / Class Ordinary Earnings (US$) Per Unit/Day "
                "CASH Global X High Interest Savings ETF A 0.0022707622 - 0.8141569556 "
                "EACC Global X MSCI EAFE Covered Call ETF** A 0.0022009616 - 1.1827862717 "
                "CNDX Global X S&P/TSX 60 Index ETF A 0.0129643120 - 0.6222156181")
        parts = crawl.split_document([page], {"by": "rows", "name_prefix": "Global X"})
        self.assertEqual([(p["tickers"], p["name"]) for p in parts], [
            (["CASH"], "Global X High Interest Savings ETF"),
            (["EACC"], "Global X MSCI EAFE Covered Call ETF"),
            (["CNDX"], "Global X S&P/TSX 60 Index ETF")])

    def test_a_single_fund_document_is_not_split(self):
        page = GUARDIAN_PAGE.format(name="Guardian Canadian Bond Fund")
        self.assertEqual(crawl.split_document([page, page], {"by": "pages"}), [])

    def test_parts_reach_the_index_as_separate_funds_with_page_links(self):
        manifest = {"https://g.example/PFIC-Investor-Statement_2024.pdf": {
            "family": "fam", "verified": True, "tax_year": 2024, "period_end": "2024-12-31",
            "sha256": "a" * 64, "bytes": 5000, "confidence": 10, "pages": 86,
            "parts": [{"name": "Guardian Canadian Bond Fund", "page": 22,
                       "period_end": "2024-12-31", "tickers": []},
                      {"name": "Guardian US Equity Select Fund", "page": 19,
                       "period_end": "2024-12-31", "tickers": []}]}}
        stmts = crawl.statements_from_manifest(manifest, 2021)
        idx = crawl.build_index(stmts, [{"id": "fam", "name": "Fam"}], [], [])
        self.assertEqual(len(idx["funds"]), 2)
        bond = next(f for f in idx["funds"] if "Bond" in f["name"])
        self.assertTrue(bond["statements"][0]["url"].endswith(".pdf#page=22"))
        self.assertEqual(bond["statements"][0]["part"], {"page": 22, "pages": 86})


class NamesFromRealDocuments(unittest.TestCase):
    """Openings of real statements that were once filed under the wrong name."""

    def test_mackenzie_sentence_about_exchange_rates_is_not_a_fund(self):
        text = ("This statement is provided to unitholders who are United States persons. "
                "PFIC Annual Information Statement For the Year ended December 31, 2025 "
                "1. This Information Statement applies to the taxable year of Mackenzie US "
                "Large Cap Equity Index ETF commencing on January 01, 2025 and ending on "
                "December 31, 2025. 3. Cash and property distributions are converted into "
                "U.S. dollars based on the closing rate published on Thomson One "
                "(Source: Reuters Forex) in effect on the date the distribution is paid.")
        self.assertEqual(crawl.parse_fund_name(text), "Mackenzie US Large Cap Equity Index ETF")

    def test_ninepoint_letter_heading_with_broken_title(self):
        text = ("Attention: U.S. Unitholder of Ninepoint Target Income Fund March 31, 2026 "
                "P FIC Annual Information Statement for the year ended December 31, 2025 "
                "PFI C INFORMATION I MPORTANT TAX NOTICE TO U.S. UNITHOLDERS OF FUND")
        self.assertEqual(crawl.parse_fund_name(text), "Ninepoint Target Income Fund")

    def test_guardian_run_on_heading(self):
        text = ("Important Tax Notice to U.S. Shareholders of Guardian Canadian Equity Select "
                "Fund This statement is provided to shareholders who are United States citizens")
        self.assertEqual(crawl.parse_fund_name(text), "Guardian Canadian Equity Select Fund")

    def test_desjardins_title_is_not_the_internal_revenue_service(self):
        text = ("Internal Revenue Service (IRS) PFIC Annual Information Statement as at "
                "December 31, 2023 DESJARDINS ALT LONG/SHORT EQUITY MARKET NEUTRAL ETF PFIC "
                "Annual Information Statement IMPORTANT TAX NOTICE TO SHAREHOLDERS OF "
                "DESJARDINS ALT LONG/SHORT EQUITY MARKET NEUTRAL ETF (the “Fund”) "
                "WHO HAVE U.S. TAX FILING REQUIREMENTS")
        name = crawl.tidy_name(crawl.parse_fund_name(text))
        self.assertEqual(name, "Desjardins Alt Long/Short Equity Market Neutral ETF")

    def test_ci_covering_letter_names_the_fund_and_series(self):
        text = ("Please find enclosed a PFIC Annual Information Statement for the requested "
                "mutual fund. Fund Name Net Long-Term Capital Cash/Property Gains "
                "Distributions 2) Your prorated shares of the ordinary earnings and net "
                "capital gains for the below listed Fund(s) are as follows: CI Private "
                "Markets Growth Fund (W) 0.50881 0.47847 0.11970 3) The above Fund(s) will")
        self.assertEqual(crawl.parse_fund_name(text), "CI Private Markets Growth Fund - Series W")

    def test_detached_letter_does_not_hide_the_tax_year(self):
        self.assertEqual(crawl.parse_period("For the Y ear Ended April 30, 2025"), "2025-04-30")

    def test_tidy_name_keeps_brackets_and_hyphenated_words(self):
        self.assertEqual(
            crawl.tidy_name("MACKENZIE DEVELOPED EX-NORTH AMERICA BOND INDEX ETF (CAD-HEDGED)"),
            "Mackenzie Developed Ex-North America Bond Index ETF (CAD-Hedged)")
        self.assertEqual(crawl.tidy_name("Vanguard S&P 500 Index ETF (CAD-hedged"),
                         "Vanguard S&P 500 Index ETF (CAD-hedged)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
