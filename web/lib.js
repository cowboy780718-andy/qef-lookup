/**
 * QEF Statement Lookup - the logic, with no page attached.
 *
 * Everything in this file is a plain function of its arguments: it never
 * touches the page, the network or the clock. That is what lets it be tested
 * (tests/test_lib.mjs) - searching, matching a client document, naming files
 * and laying out a ZIP are exactly the parts where a quiet mistake would put
 * the wrong statement in someone's working papers.
 */

// Only true function words and legal suffixes. Content words stay, even the
// ones that feel generic: stripping "corporate" once made
// "...Short Term Corporate Bond Index ETF" and "...Short Term Bond Index ETF"
// reduce to identical token sets, and every search for one matched the other.
export const STOP = new Set("the a an of and for to in on inc ltd ltee limited".split(" "));

export const norm = (s) =>
  (s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
export const tokens = (s) =>
  norm(s).split(" ").filter((t) => t && !STOP.has(t) && t.length > 1);
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const kb = (n) =>
  (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");

export const keyOf = (fund, stmt) => `${fund.id}|${stmt.url}`;
/** Undo keyOf. Fund ids never contain "|"; URLs occasionally do. */
export function splitKey(key) {
  const i = key.indexOf("|");
  return [key.slice(0, i), key.slice(i + 1)];
}

/** A statement URL without the "#page=37" that opens a combined document. */
export const baseUrl = (url) => String(url).split("#")[0];

// --------------------------------------------------------------------------
// search
// --------------------------------------------------------------------------

/**
 * Split what was typed into separate searches.
 *
 * Commas and line breaks separate ALTERNATIVES: "XSH, VFV" means either fund.
 * (It used to mean a single fund matching both, which no fund ever does - so
 * pasting a list of tickers always returned nothing.)
 */
export function queryTerms(query) {
  return String(query || "").split(/[\n,;]+/).map(norm).filter(Boolean);
}

// Words in a manager's name that say what it is rather than who it is.
const GENERIC_NAME = new Set(("asset management managers investments investment funds fund " +
  "etfs etf canada global capital group partners financial advisors mutual").split(" "));

/** The words that identify a manager: "CIBC Asset Management" -> ["cibc"]. */
export function familyKeywords(fam) {
  // "(CIBC)" after Renaissance says whose it is, not what it is called.
  const name = String(fam?.name || "").replace(/\([^)]*\)/g, " ");
  return tokens(name).filter((w) => w.length >= 3 && !GENERIC_NAME.has(w));
}

/**
 * The managers a search names, whatever else was typed with them:
 * "cibc canadian index fund" names CIBC. Used to say what is known about a
 * manager - what it has no statement for - next to whatever the search found.
 */
export function familiesNamed(query, families) {
  const terms = queryTerms(query).map((t) => t.split(" "));
  if (!terms.length) return [];
  return (families || []).filter((f) => {
    const keys = familyKeywords(f);
    return keys.length > 0 &&
      terms.some((words) => keys.every((k) => words.some((w) => w.startsWith(k))));
  });
}

/**
 * The "no statement" findings a search should be told about.
 *
 * Only when the search is about the funds a finding covers:
 *   - it names them outright - a finding's `match` phrases, e.g. "axiom"; or
 *   - it names the manager (or the manager is picked) and found nothing.
 * Never merely because a manager was named: "No PFIC statement" above a list
 * of that manager's statements reads as a contradiction, and nobody searching
 * "Fidelity" knows which series of which fund it is talking about.
 */
export function findingsFor(query, negatives, families, found, picked = "") {
  const terms = queryTerms(query).map((t) => t.split(" "));
  if (!terms.length) return [];
  const says = (phrase) => {
    const keys = norm(phrase).split(" ");
    return terms.some((words) => keys.every((k) => words.some((w) => w.startsWith(k))));
  };
  const named = new Set(familiesNamed(query, families).map((f) => f.id));
  if (picked) named.add(picked);
  return (negatives || []).filter((n) =>
    (n.match || []).some(says) || (!found && named.has(n.family)));
}

/** Everything about a fund that a search may hit, normalised once and kept. */
export function haystack(fund) {
  if (fund._hay === undefined) {
    // Names a fund was published under in earlier years are searchable too:
    // someone holding it since 2024 knows it by the old name.
    const earlier = (fund.statements || []).map((s) => s.name || "").join(" ");
    Object.defineProperty(fund, "_hay", {
      value: ` ${norm(`${fund.name} ${earlier} ${fund.family_name} ${(fund.tickers || []).join(" ")}`)} `,
      enumerable: false,
    });
  }
  return fund._hay;
}

/**
 * How well one search matches one fund: 0 is no match, higher is better.
 *
 * Within a search every word must be found, in any order, and part of a word
 * is enough - "mackenzie bond hedg" finds Mackenzie's hedged bond ETFs.
 */
export function termScore(fund, term) {
  const hay = haystack(fund);
  const words = term.split(" ");
  const ticks = (fund.tickers || []).map((t) => norm(t));
  if (ticks.includes(term)) return 100;                      // exact ticker or fund code
  if (words.length === 1 && ticks.some((k) => k.startsWith(term))) return 60;
  if (!words.every((w) => hay.includes(w))) return 0;
  const name = norm(fund.name);
  if (name === term) return 90;
  if (name.startsWith(term)) return 50;
  if (hay.includes(` ${term} `)) return 40;                  // the phrase, whole words
  if (hay.includes(term)) return 30;                         // the phrase
  return 20;                                                 // the words, scattered
}

export function queryScore(fund, terms) {
  if (!terms.length) return 1;
  let best = 0;
  for (const t of terms) best = Math.max(best, termScore(fund, t));
  return best;
}

// --------------------------------------------------------------------------
// identifying funds in a client document
// --------------------------------------------------------------------------

// Tickers that are also ordinary words on any brokerage statement. Seeing
// "CASH" in a client document says nothing about the Global X High Interest
// Savings ETF.
const WORDLIKE = new Set(("CASH GOLD BOND BANK HERO CARS FOOD CALL FUND PLUS CORE REIT TECH " +
  "COP HAL HAD CAN USA DLR FLOT RING SAFE GAS OIL ONE TWO SIX TEN NET FEE TAX DIV INT PAY " +
  "BUY SELL OPEN DATE TYPE UNIT COST BOOK YEAR ACCT PAGE NOTE RATE").split(" "));

/**
 * Match the text of a client document against the index.
 *
 * Deliberately conservative. Over-matching here would be worse than
 * under-matching: a wrong fund quietly used is a bigger problem than one you
 * had to look up. A fund is proposed when
 *
 *   - its full name appears, words in order and adjacent; or
 *   - its ticker appears as a standalone token AND either the ticker is
 *     distinctive on its own, or most of the fund's name is on the page too.
 *
 * Contiguous name matching rather than word overlap, because fund families
 * name products in nested series: "Short Term Bond" sits inside "Short Term
 * Corporate Bond", and overlap treats those as the same fund.
 *
 * Returns Map(fundId -> {score, why}).
 */
export function identify(text, funds) {
  const docTokens = tokens(text);
  const docStr = ` ${docTokens.join(" ")} `;
  const docSet = new Set(docTokens);
  // Standalone upper-case tokens, so each ticker is a set lookup rather than
  // a fresh regular expression per fund.
  const upperTokens = new Set(String(text).toUpperCase().match(/[A-Z][A-Z0-9]*(?:\.[A-Z]{1,2})?/g) || []);
  const hits = new Map();

  for (const fund of funds) {
    let score = 0;
    const why = [];
    const toks = tokens(fund.name);
    const present = toks.filter((t) => docSet.has(t)).length;
    const mostOfName = toks.length >= 2 && present / toks.length >= 0.6;

    for (const t of fund.tickers || []) {
      if (t.length < 2 || !upperTokens.has(t.toUpperCase())) continue;
      const distinctive = (t.length >= 4 || /[.0-9]/.test(t)) && !WORDLIKE.has(t.toUpperCase());
      if (mostOfName) { score += 10; why.push(`ticker ${t} and name`); break; }
      if (distinctive) { score += 6; why.push(`ticker ${t} only`); break; }
    }

    const names = [fund.name, ...(fund.statements || []).map((s) => s.name).filter(Boolean)];
    for (const n of names) {
      const nt = tokens(n);
      if (nt.length >= 2 && docStr.includes(` ${nt.join(" ")} `)) {
        score += 6 + Math.min(nt.length, 6);
        why.push(n === fund.name ? "full name" : "earlier name");
        break;
      }
    }

    if (score > 0) hits.set(fund.id, { score, why: why.join(", ") });
  }
  return hits;
}

// --------------------------------------------------------------------------
// downloads
// --------------------------------------------------------------------------

export const safe = (s) =>
  String(s).replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 70);

const yearOf = (stmt) => stmt.tax_year || (stmt.period_end || "").slice(0, 4) || "unknown";

export function fileNameFor(fund, stmt) {
  const ext = stmt.fmt === "html" ? "html" : "pdf";
  return `${safe(fund.family_name)}__${safe(fund.name)}__FY${yearOf(stmt)}__PFIC_AIS.${ext}`;
}

/**
 * Decide what goes in the ZIP.
 *
 * One file per selected statement - except that several selected funds can
 * live in the same combined document (Guardian staples ~30 funds into one PDF
 * a year). That document is fetched and stored once, under a name that says
 * what it is, and every fund selected from it points at the same file.
 *
 * Returns [{name, kind: "pdf"|"html", url, items: [{fund, stmt}]}], with file
 * names guaranteed distinct.
 */
export function planDownloads(items) {
  const byUrl = new Map();
  const files = [];
  for (const item of items) {
    const { fund, stmt } = item;
    // A link the manager's site does not let us fetch is not a file; it goes
    // in the manifest and in NOT_DOWNLOADED.txt instead (see linksOnly).
    if (stmt.fmt === "link") continue;
    if (stmt.fmt === "html") {
      files.push({ kind: "html", url: stmt.url, name: fileNameFor(fund, stmt), items: [item] });
      continue;
    }
    const url = baseUrl(stmt.url);
    let file = byUrl.get(url);
    if (!file) {
      file = { kind: "pdf", url, name: fileNameFor(fund, stmt), items: [] };
      byUrl.set(url, file);
      files.push(file);
    }
    file.items.push(item);
  }
  for (const f of files) {
    if (f.kind === "pdf" && (f.items.length > 1 || f.items[0].stmt.part)) {
      const { fund, stmt } = f.items[0];
      f.name = `${safe(fund.family_name)}__combined_statement__FY${yearOf(stmt)}__PFIC_AIS.pdf`;
    }
  }
  const used = new Map();
  for (const f of files) {
    const n = (used.get(f.name) || 0) + 1;
    used.set(f.name, n);
    if (n > 1) f.name = f.name.replace(/(\.[a-z]+)$/, `_${n}$1`);
  }
  return files;
}

/** The selected statements that are links only and so cannot go in a ZIP. */
export const linksOnly = (items) => items.filter((it) => it.stmt.fmt === "link");

/** The note that goes in the ZIP in place of documents we may not fetch. */
export function notDownloadedText(items) {
  const links = linksOnly(items);
  if (!links.length) return "";
  return "These statements are NOT in this ZIP.\r\n\r\n" +
    "The managers below ask automated tools not to fetch their documents, so this\r\n" +
    "tool lists the manager's own link and stops there. Open each address in your\r\n" +
    "browser to download the statement, and check the fund and year when you do -\r\n" +
    "these links have not been opened or verified.\r\n\r\n" +
    links.map(({ fund, stmt }) =>
      `${fund.family_name} - ${fund.name}${stmt.tax_year ? ` (${stmt.tax_year})` : ""}\r\n  ${stmt.url}`)
      .join("\r\n\r\n") + "\r\n";
}

const csvCell = (c) => {
  let v = String(c ?? "");
  // A cell starting with = + - or @ is run as a formula by Excel. These
  // values come from other people's documents, so make them inert.
  if (/^[=+\-@]/.test(v) && !/^-?[\d.,]+$/.test(v)) v = "'" + v;
  return `"${v.replace(/"/g, '""')}"`;
};

const figureText = (figures) =>
  Object.entries(figures || {}).map(([k, v]) => `${k}: ${v}`).join(" | ");

/** The manifest that goes in the ZIP: one row per selected statement. */
export function manifestCsv(items, files = planDownloads(items), failed = new Map()) {
  const fileOf = new Map();
  for (const f of files) for (const it of f.items) fileOf.set(keyOf(it.fund, it.stmt), f);
  const rows = [["Fund family", "Fund", "Published as (if different)", "Tickers",
                 "Fund tax year", "Period end", "Format", "File name", "Page in file",
                 "Source URL", "Bytes", "SHA256 (first 16)", "Published figures",
                 "Indirect PFICs listed (direct investee funds)", "Download"]];
  for (const { fund, stmt } of items) {
    const f = fileOf.get(keyOf(fund, stmt));
    const why = f && failed.get(f.name);
    rows.push([
      fund.family_name, fund.name, stmt.name || "", (fund.tickers || []).join(" "),
      stmt.tax_year ?? "", stmt.period_end ?? "",
      stmt.fmt === "html" ? "web table"
        : stmt.fmt === "link" ? "link only (not checked)"
        : stmt.part ? "PDF (combined document)" : "PDF",
      f ? f.name : "", stmt.part ? stmt.part.page : "",
      stmt.url, stmt.bytes || "", stmt.sha256 || "", figureText(stmt.figures),
      (stmt.investees || []).map((i) => i.name).join(" | "),
      stmt.fmt === "link" ? "not downloaded - open the source URL"
        : why ? `FAILED: ${why}` : "ok",
    ]);
  }
  return rows.map((r) => r.map(csvCell).join(",")).join("\r\n");
}

const figureRows = (figures) => Object.entries(figures || {})
  .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("");

/**
 * Build a working-paper page for a statement the manager published as a table
 * rather than a document. There is nothing to download in those cases, so the
 * ZIP would otherwise contain a gap exactly where a fund does have a
 * statement. The figures are reproduced as published, with the source URL and
 * retrieval date on the page so the file stands on its own.
 */
export function htmlStatementFile(fund, stmt, today = new Date().toISOString().slice(0, 10)) {
  const investees = (stmt.investees || []).map((i) => `
<h3>${esc(i.name)}</h3>
<table><tbody>${figureRows(i.figures)}</tbody></table>`).join("");
  return `<!doctype html><meta charset="utf-8">
<title>${esc(fund.name)} — PFIC AIS ${esc(String(stmt.tax_year ?? ""))}</title>
<style>
 body{font:14px/1.55 system-ui,sans-serif;margin:36px;max-width:760px;color:#14181d}
 h1{font-size:1.2rem;margin:0 0 4px}
 h2{font-size:1rem;margin:26px 0 2px}
 h3{font-size:.92rem;margin:16px 0 0}
 .meta{color:#5a636e;margin-bottom:18px}
 table{border-collapse:collapse;width:100%;margin:8px 0 14px}
 th,td{border:1px solid #dfe3e8;padding:8px 10px;text-align:left;vertical-align:top}
 th{background:#f6f7f9;width:58%;font-weight:600}
 td{font-variant-numeric:tabular-nums}
 .note{background:#fff8e6;border:1px solid #f0d999;padding:11px 13px;border-radius:7px;
       font-size:.9rem;margin-top:20px}
 a{color:#1d4ed8}
</style>
<h1>${esc(fund.name)}</h1>
<div class="meta">${esc(fund.family_name)}
  ${(fund.tickers || []).length ? " &middot; " + esc(fund.tickers.join(", ")) : ""}<br>
  PFIC Annual Information Statement &mdash; fund tax year
  ${esc(String(stmt.tax_year ?? "unknown"))}${
    stmt.period_end ? `, period ended ${esc(stmt.period_end)}` : ""}${
    stmt.name ? `<br>Published by the manager under the name: ${esc(stmt.name)}` : ""}</div>
<table><tbody>${figureRows(stmt.figures)}</tbody></table>
${investees ? `<h2>Direct investee funds of the fund</h2>
<div class="meta">Listed by the manager for holders of this fund: amounts attributable
to one unit of <em>this</em> fund through its holdings of the funds named below.
They are not those funds&rsquo; own statements.</div>${investees}` : ""}
<div class="note">
  <strong>This is a transcription, not the manager's own document.</strong>
  ${esc(fund.family_name)} publishes this statement as a table on its website
  rather than as a downloadable file, so the figures above were captured from
  that page and are reproduced exactly as published &mdash; nothing has been
  recomputed or converted.
  <br><br>
  Source: <a href="${esc(stmt.url)}">${esc(stmt.url)}</a><br>
  Retrieved for this file: ${esc(today)}<br>
  <strong>Verify against the source page before using it.</strong>
</div>`;
}

// --------------------------------------------------------------------------
// source health
// --------------------------------------------------------------------------

/**
 * "Sources healthy" as a reader would understand it.
 *
 * Managers we deliberately do not collect from (their robots.txt forbids it,
 * or they post nothing) are not sources that can be healthy or unhealthy. The
 * header used to count them in the denominator, so a perfect night read
 * "19/33" and looked like fourteen things were broken.
 */
export const NOT_CRAWLED = new Set(["manual", "request", "none"]);

export function healthSummary(health) {
  const rows = health || [];
  const crawled = rows.filter((h) => !NOT_CRAWLED.has(h.status));
  return {
    ok: crawled.filter((h) => h.status === "ok").length,
    total: crawled.length,
    byHand: rows.length - crawled.length,
  };
}
