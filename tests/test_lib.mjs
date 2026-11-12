/**
 * Tests for the page's search, matching and download logic (web/lib.js).
 *
 *     node --test tests/test_lib.mjs
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  queryTerms, queryScore, termScore, identify, planDownloads, manifestCsv,
  htmlStatementFile, healthSummary, fileNameFor, keyOf, splitKey, baseUrl,
  linksOnly, notDownloadedText, familyKeywords, familiesNamed, findingsFor,
} from "../web/lib.js";

const fund = (id, name, tickers = [], statements = null, family_name = "Example Funds") => ({
  id: `fam::${id}`, family: "fam", family_name, name, tickers,
  statements: statements || [{
    tax_year: 2025, period_end: "2025-12-31", url: `https://m.example/${id}.pdf`,
    bytes: 1000, sha256: "ab",
  }],
});

const FUNDS = [
  fund("xsh", "iShares Core Canadian Short Term Corporate Bond Index ETF", ["XSH"]),
  fund("xsb", "iShares Core Canadian Short Term Bond Index ETF", ["XSB"]),
  fund("vfv", "Vanguard S&P 500 Index ETF", ["VFV"]),
  fund("cash", "Global X High Interest Savings ETF", ["CASH"]),
  fund("mhb", "Mackenzie US High Yield Bond Index ETF (CAD-Hedged)", ["QHY"]),
  fund("rbf", "RBC Global Dividend Growth Fund - Series F", ["RBF1664"]),
  fund("hdiv", "Hamilton Enhanced Canadian Covered Call ETF - Class E", ["HDIV"], [
    { tax_year: 2026, period_end: "2026-04-30", url: "https://h.example/2026/", fmt: "html",
      figures: { "ordinary earnings": "0.01" } },
    { tax_year: 2025, period_end: "2025-04-30", url: "https://h.example/2025/", fmt: "html",
      figures: { "ordinary earnings": "0.02" },
      name: "Hamilton Enhanced Multi Sector Covered Call ETF - Class E" },
  ]),
];
const found = (q) =>
  FUNDS.filter((f) => queryScore(f, queryTerms(q))).map((f) => f.id.split("::")[1]);

test("a list of tickers finds each of them, not nothing", () => {
  // Commas used to mean "one fund matching all of these", which never matched.
  assert.deepEqual(found("XSH, VFV"), ["xsh", "vfv"]);
  assert.deepEqual(found("xsh\nvfv; qhy"), ["xsh", "vfv", "mhb"]);
});

test("words can be partial and in any order", () => {
  assert.deepEqual(found("mackenzie bond hedg"), ["mhb"]);
  assert.deepEqual(found("bond mackenzie"), ["mhb"]);
  assert.deepEqual(found("global divid"), ["rbf"]);
});

test("an empty search shows everything", () => {
  assert.equal(found("").length, FUNDS.length);
  assert.equal(found("  ,  ").length, FUNDS.length);
});

test("an exact ticker outranks a name that merely contains the letters", () => {
  const xsh = FUNDS[0], xsb = FUNDS[1];
  assert.ok(termScore(xsh, "xsh") > termScore(xsh, "short term"));
  assert.equal(termScore(xsb, "xsh"), 0);
});

test("a fund code is found as a ticker", () => {
  assert.deepEqual(found("RBF1664"), ["rbf"]);
});

test("a fund is found by the name it carried in an earlier year", () => {
  assert.deepEqual(found("multi sector covered call"), ["hdiv"]);
});

test("searching the manager finds its funds", () => {
  assert.equal(found("example funds").length, FUNDS.length);
});

test("document matching tells nested fund names apart", () => {
  const hits = identify(
    "Holdings: ISHARES CORE CANADIAN SHORT TERM BOND INDEX ETF 120 units", FUNDS);
  assert.deepEqual([...hits.keys()], ["fam::xsb"]);
});

test("a word-like ticker alone does not match a fund", () => {
  // "CASH" is on every brokerage statement.
  const hits = identify("Account summary. CASH balance 1,204.55. Total 9,000.00", FUNDS);
  assert.equal(hits.size, 0);
});

test("a distinctive ticker alone is offered, and says it is ticker-only", () => {
  const hits = identify("Positions: RBF1664 250.000 units", FUNDS);
  assert.equal(hits.get("fam::rbf").why, "ticker RBF1664 only");
});

test("ticker plus most of the name is the strongest match", () => {
  const hits = identify("VFV VANGUARD S&P 500 INDEX ETF 40 shares; RBF1664", FUNDS);
  assert.ok(hits.get("fam::vfv").score > hits.get("fam::rbf").score);
  assert.match(hits.get("fam::vfv").why, /ticker VFV and name/);
});

test("a short ticker is not found inside a longer word", () => {
  const hits = identify("See the XSHARES prospectus and the QHYX note", FUNDS);
  assert.equal(hits.size, 0);
});

test("a document naming a fund by its earlier name still matches", () => {
  const hits = identify(
    "Hamilton Enhanced Multi Sector Covered Call ETF Class E 300 units", FUNDS);
  assert.match(hits.get("fam::hdiv").why, /earlier name/);
});

test("keys survive a URL containing the separator", () => {
  const f = { id: "fam::x" }, s = { url: "https://m.example/a|b.pdf#page=3" };
  assert.deepEqual(splitKey(keyOf(f, s)), ["fam::x", "https://m.example/a|b.pdf#page=3"]);
  assert.equal(baseUrl(s.url), "https://m.example/a|b.pdf");
});

test("funds from one combined document share a single file in the ZIP", () => {
  const doc = "https://g.example/PFIC-Investor-Statement_2024.pdf";
  const mk = (id, name, page) => {
    const f = fund(id, name, [], [{
      tax_year: 2024, period_end: "2024-12-31", url: `${doc}#page=${page}`, bytes: 9,
      part: { page, pages: 86 },
    }], "Guardian Capital");
    return { fund: f, stmt: f.statements[0] };
  };
  const items = [mk("a", "Guardian Canadian Bond Fund", 22),
                 mk("b", "Guardian US Equity Select Fund", 19),
                 { fund: FUNDS[2], stmt: FUNDS[2].statements[0] }];
  const files = planDownloads(items);
  assert.equal(files.length, 2);
  assert.equal(files[0].url, doc);
  assert.equal(files[0].items.length, 2);
  assert.equal(files[0].name, "Guardian_Capital__combined_statement__FY2024__PFIC_AIS.pdf");
  const csv = manifestCsv(items, files);
  assert.equal(csv.split("\r\n").length, 4);
  assert.match(csv, /"Guardian Canadian Bond Fund".*"PDF \(combined document\)".*"22"/);
});

test("file names in one ZIP never collide", () => {
  const a = fund("a", "Same Name Fund"), b = fund("b", "Same Name Fund");
  const files = planDownloads([{ fund: a, stmt: a.statements[0] },
                               { fund: b, stmt: b.statements[0] }]);
  assert.notEqual(files[0].name, files[1].name);
});

test("file names are safe on every filesystem", () => {
  const f = fund("x", 'BMO Mid/Long: "US" <Treasury> ETF?*', [], null, "BMO Global Asset Management");
  assert.match(fileNameFor(f, f.statements[0]), /^[A-Za-z0-9_]+\.pdf$/);
});

test("the manifest records a failed download against its statement", () => {
  const items = [{ fund: FUNDS[2], stmt: FUNDS[2].statements[0] }];
  const files = planDownloads(items);
  const csv = manifestCsv(items, files, new Map([[files[0].name, "HTTP 502"]]));
  assert.match(csv, /"FAILED: HTTP 502"$/);
});

test("spreadsheet formulas in published text are made inert", () => {
  const f = fund("x", '=HYPERLINK("http://evil.example")');
  const csv = manifestCsv([{ fund: f, stmt: f.statements[0] }]);
  assert.match(csv, /"'=HYPERLINK/);
  // ...but an ordinary negative number is left as a number.
  const g = fund("y", "Fund Y", [], [{
    tax_year: 2025, url: "https://m.example/y/", fmt: "html", figures: { oe: "-0.0012" },
  }]);
  assert.match(manifestCsv([{ fund: g, stmt: g.statements[0] }]), /"oe: -0.0012"/);
});

test("the working-paper page escapes what it prints and carries investee funds", () => {
  const f = fund("x", "Fund <b>X</b>", ["XX"], [{
    tax_year: 2026, period_end: "2026-04-30", url: "https://h.example/2026/?a=1&b=2",
    fmt: "html", figures: { "ordinary earnings (us$) per unit/day": "0.0077" },
    investees: [{ name: "Investee <One>",
                  figures: { "ordinary earnings (us$) per unit/day": "0.0028" } }],
  }]);
  const html = htmlStatementFile(f, f.statements[0], "2026-11-11");
  assert.ok(!html.includes("<b>X</b>"));
  assert.ok(html.includes("Fund &lt;b&gt;X&lt;/b&gt;"));
  assert.ok(html.includes("Investee &lt;One&gt;"));
  assert.ok(html.includes("Direct investee funds of the fund"));
  assert.ok(html.includes("0.0028"));
  assert.ok(html.includes("Retrieved for this file: 2026-11-11"));
  assert.ok(html.includes("https://h.example/2026/?a=1&amp;b=2"));
});

test("health counts only the managers that are collected automatically", () => {
  const h = healthSummary([{ status: "ok" }, { status: "ok" }, { status: "empty" },
                           { status: "manual" }, { status: "none" }, { status: "blocked" },
                           { status: "request" }]);
  assert.deepEqual(h, { ok: 2, total: 4, byHand: 3 });
});

test("a link-only statement is never fetched, and the ZIP says where to get it", () => {
  const f = fund("cg", "Capital Group U.S. Equity Fund (Canada)", [], [{
    tax_year: null, period_end: null, fmt: "link", bytes: 0, sha256: "",
    url: "https://www.capitalgroup.example/content/dam/AIS_CGUSE.pdf",
  }], "Capital Group Canada");
  const items = [{ fund: f, stmt: f.statements[0] },
                 { fund: FUNDS[2], stmt: FUNDS[2].statements[0] }];
  const files = planDownloads(items);
  assert.deepEqual(files.map((x) => x.url), ["https://m.example/vfv.pdf"]);
  assert.equal(linksOnly(items).length, 1);
  const note = notDownloadedText(items);
  assert.match(note, /NOT in this ZIP/);
  assert.match(note, /Capital Group Canada - Capital Group U\.S\. Equity Fund \(Canada\)\r\n {2}https:\/\/www\.capitalgroup\.example\/content\/dam\/AIS_CGUSE\.pdf/);
  const csv = manifestCsv(items, files);
  assert.match(csv, /"link only \(not checked\)".*"not downloaded - open the source URL"/);
  assert.equal(notDownloadedText([items[1]]), "");
});

// --------------------------------------------------------------------------
// naming a manager in a search
// --------------------------------------------------------------------------

const FAMS = [
  { id: "cibc", name: "CIBC Asset Management" },
  { id: "renaissance", name: "Renaissance Investments (CIBC)" },
  { id: "scotia", name: "Scotia Global Asset Management (ScotiaFunds, Scotia ETFs)" },
  { id: "beutel-goodman", name: "Beutel Goodman" },
  { id: "globalx-ca", name: "Global X Canada" },
];

test("a manager is identified by its own words, not by 'Asset Management'", () => {
  assert.deepEqual(familyKeywords(FAMS[0]), ["cibc"]);
  assert.deepEqual(familyKeywords(FAMS[1]), ["renaissance"]);
  assert.deepEqual(familyKeywords(FAMS[2]), ["scotia"]);
  assert.deepEqual(familyKeywords(FAMS[4]), []);          // nothing distinctive: never guessed
});

test("a search names a manager whatever else is typed with it", () => {
  const ids = (q) => familiesNamed(q, FAMS).map((f) => f.id);
  assert.deepEqual(ids("cibc canadian index fund"), ["cibc"]);
  assert.deepEqual(ids("Scotiabank Canadian Dividend"), ["scotia"]);
  assert.deepEqual(ids("XSH, beutel goodman income"), ["beutel-goodman"]);
  assert.deepEqual(ids("goodman"), []);                    // every identifying word is needed
  assert.deepEqual(ids("global dividend"), []);
  assert.deepEqual(ids(""), []);
});

test("a 'no statement' finding is shown only to a search about the funds it covers", () => {
  const NEGS = [
    { family: "cibc", scope: "CIBC mutual funds, Axiom Portfolios", match: ["axiom", "cibc managed portfolio"] },
    { family: "renaissance", scope: "Renaissance HISA" },
  ];
  const shown = (q, found, picked) => findingsFor(q, NEGS, FAMS, found, picked).map((n) => n.family);
  assert.deepEqual(shown("cibc", 20), []);                        // statements listed below: say nothing
  assert.deepEqual(shown("", 20, "cibc"), []);                    // manager picked, nothing typed
  assert.deepEqual(shown("axiom balanced growth", 0), ["cibc"]);  // names the funds outright
  assert.deepEqual(shown("Axiom", 3), ["cibc"]);                  // ...whatever else matched
  assert.deepEqual(shown("cibc managed balanced portfolio", 2), ["cibc"]);
  assert.deepEqual(shown("cibc canadian equity fund", 0), ["cibc"]);   // names the manager, found nothing
  assert.deepEqual(shown("canadian equity fund", 0, "cibc"), ["cibc"]); // manager picked, found nothing
  assert.deepEqual(shown("renaissance", 150), []);
  assert.deepEqual(shown("mackenzie bond", 0), []);
});
