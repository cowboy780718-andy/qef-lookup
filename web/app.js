/**
 * QEF Statement Lookup - client-side application.
 *
 * Everything here runs in the reader's browser. Client documents dropped onto
 * the page are parsed locally with PDF.js and never leave the machine; the only
 * network calls are for the static index and, when bundling a ZIP, the PDF
 * proxy. There is no server-side session and nothing about a lookup is logged.
 */

import * as pdfjsLib from "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.6.82/pdf.min.mjs";
pdfjsLib.GlobalWorkerOptions.workerSrc =
  "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.6.82/pdf.worker.min.mjs";

const CFG = window.QEF_CONFIG;
const $ = (id) => document.getElementById(id);

const state = {
  index: null,
  query: "",
  years: new Set(),
  family: "",
  from: "",
  to: "",
  selected: new Map(),   // key -> {fund, stmt}
  expanded: new Set(),
  matches: null,         // Map fundId -> {score, why} from a dropped PDF
  // Paging. Four thousand funds in one list is unusable and slow to read;
  // ten at a time is a page you can actually scan.
  perPage: 10,
  page: 1,
};

// Only true function words and legal suffixes. Content words stay, even the
// ones that feel generic: stripping "corporate" once made
// "...Short Term Corporate Bond Index ETF" and "...Short Term Bond Index ETF"
// reduce to identical token sets, and every search for one matched the other.
const STOP = new Set("the a an of and for to in on inc ltd ltee limited".split(" "));

const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
const tokens = (s) => norm(s).split(" ").filter((t) => t && !STOP.has(t) && t.length > 1);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const kb = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.round(n / 1024) + " KB");
const keyOf = (f, s) => `${f.id}|${s.url}`;

// --------------------------------------------------------------------------
// boot
// --------------------------------------------------------------------------

async function boot() {
  try {
    const r = await fetch(CFG.INDEX_URL, { cache: "no-cache" });
    if (!r.ok) throw new Error(`index returned ${r.status}`);
    state.index = await r.json();
  } catch (e) {
    $("stats").innerHTML =
      `<span style="color:var(--bad)">Could not load the index (${esc(e.message)}). ` +
      `If this is a fresh checkout, run the crawler first.</span>`;
    return;
  }
  const s = state.index.stats;
  $("stats").innerHTML = [
    `<span><b>${s.funds.toLocaleString()}</b> funds</span>`,
    `<span><b>${s.statements.toLocaleString()}</b> statements</span>`,
    `<span><b>${s.families_ok}/${s.families}</b> sources healthy</span>`,
    `<span class="muted">updated ${new Date(state.index.generated).toLocaleDateString()}</span>`,
  ].join("");
  $("built").textContent = `Index generated ${new Date(state.index.generated).toUTCString()}.`;

  buildYearChips();
  buildFamilySelect();
  renderNegatives();
  renderHealth();
  render();
  wire();
}

function buildYearChips() {
  $("years").innerHTML = state.index.stats.years
    .map((y) => `<button class="chip" data-year="${y}" aria-pressed="false">${y}</button>`)
    .join("") || `<span class="muted">No years indexed yet.</span>`;
}

function buildFamilySelect() {
  // Every family we have ever checked appears here, including those with
  // nothing available. A manager missing from the list looks like a manager
  // nobody thought of; a manager listed as "none available" is a question
  // already answered.
  const fams = [...(state.index.families || [])]
    .sort((a, b) => a.name.localeCompare(b.name));
  const withDocs = fams.filter((f) => (f.fund_count || 0) > 0);
  const without = fams.filter((f) => !(f.fund_count > 0));

  const opt = (f) => {
    const tag = f.access === "none" ? " — none available"
      : f.access === "manual" ? " — fetch by hand"
      : (f.fund_count ? "" : " — nothing found yet");
    return `<option value="${esc(f.id)}">${esc(f.name)}${tag}</option>`;
  };

  $("famsel").innerHTML =
    `<option value="">All families</option>` +
    `<optgroup label="Statements in the index">${withDocs.map(opt).join("")}</optgroup>` +
    (without.length
      ? `<optgroup label="Checked — nothing to download">${without.map(opt).join("")}</optgroup>`
      : "");
}

/**
 * Recognise a FundSERV code and say whose it is.
 *
 * Client statements identify Canadian mutual funds by code - RBF556, TDB900 -
 * while PFIC statements only ever print the legal fund name, so the index has
 * no codes for mutual funds to match against. Returning a blank "nothing
 * found" for a code the user copied straight off a client statement is the
 * worst possible answer: it reads as "no statement exists".
 */
function fundCodeHelp(query) {
  const term = (query || "").trim().toUpperCase();
  const m = /^([A-Z]{3})(\d{2,5})$/.exec(term);
  if (!m) return "";
  const entry = (state.index.fund_code_prefixes || [])
    .find((p) => p.prefix === m[1]);
  const fam = entry && entry.family
    ? (state.index.families || []).find((f) => f.id === entry.family) : null;
  const resolve = entry && entry.resolve
    ? entry.resolve.replace("{code}", term) : null;

  return `<div class="guide code">
    <h3>${esc(term)} looks like a FundSERV fund code</h3>
    <p class="who">${entry ? esc(entry.manager) : "Manager not recognised"}</p>
    <p>Fund companies print these codes on client statements, but a PFIC
       statement only ever carries the fund&rsquo;s legal name &mdash; so the
       index has no code to match. Resolve the code to a fund name first, then
       search that name here.</p>
    ${resolve ? `<p><a class="gobtn" href="${esc(resolve)}" target="_blank"
        rel="noopener">Look up ${esc(term)} on the manager&rsquo;s site &rarr;</a></p>`
      : entry && fam ? `<p><a class="gobtn" href="${esc(fam.hub)}" target="_blank"
        rel="noopener">Open ${esc(entry.manager)} &rarr;</a></p>` : ""}
    ${entry && entry.verified === false ? `<p class="caution">The direct link for
       this prefix is unconfirmed &mdash; if it 404s, search the manager&rsquo;s
       site for the code instead.</p>` : ""}
    ${!entry ? `<p class="caution">This prefix is not in the table yet. Search
       the code on the web to identify the manager, then tell me and I will add
       it.</p>` : ""}
  </div>`;
}

/**
 * Panel shown when a family has no statements to list. Empty results are the
 * moment the tool is most likely to mislead: "nothing found" reads as "no
 * statement exists". So say which of the two it is, why, and where to go.
 */
function familyGuidance(fam) {
  if (!fam) return "";
  // Wording is deliberate. We report what was found on a manager's public
  // website on a date - never that a manager refuses to issue a statement.
  // One may well be available on request.
  const kind = fam.access === "none"
    ? { cls: "none", head: "No statement available from this manager's website" }
    : fam.access === "manual"
      ? { cls: "manual", head: "Available, but must be fetched by hand" }
      : { cls: "unknown", head: "Nothing indexed for this manager yet" };
  const link = fam.hub;
  const linkLabel = fam.access === "none"
    ? "Open the manager's website"
    : "Open the manager's PFIC page";
  return `<div class="guide ${kind.cls}">
    <h3>${esc(kind.head)}</h3>
    <p class="who">${esc(fam.name)}</p>
    <p>${esc((fam.guidance || fam.notes || "").trim()) ||
       "No further detail recorded yet."}</p>
    ${link ? `<p><a class="gobtn" href="${esc(link)}" target="_blank"
        rel="noopener">${linkLabel} &rarr;</a></p>` : ""}
    <p class="caution">This records what was found on the manager&rsquo;s public
      website, not whether they will issue a statement. One may be available on
      request &mdash; contact the manager before concluding otherwise.</p>
  </div>`;
}

// --------------------------------------------------------------------------
// filtering
// --------------------------------------------------------------------------

function matchesQuery(fund, terms) {
  if (!terms.length) return true;
  const hay = norm(`${fund.name} ${fund.family_name} ${fund.tickers.join(" ")}`);
  const ticks = fund.tickers.map((t) => t.toLowerCase());
  // Every term must hit somewhere: substring of the haystack, or a ticker.
  return terms.every((t) => hay.includes(t) || ticks.some((k) => k === t || k.startsWith(t)));
}

function visibleFunds() {
  const terms = state.query.split(/[\n,]+/).map(norm).filter(Boolean);
  const out = [];
  for (const fund of state.index.funds) {
    if (state.family && fund.family !== state.family) continue;
    if (state.matches && !state.matches.has(fund.id)) continue;
    if (!matchesQuery(fund, terms)) continue;
    const stmts = fund.statements.filter((s) => {
      if (state.years.size && !state.years.has(s.tax_year)) return false;
      if (state.from && (!s.period_end || s.period_end < state.from)) return false;
      if (state.to && (!s.period_end || s.period_end > state.to)) return false;
      return true;
    });
    if (!stmts.length) continue;
    out.push({ ...fund, statements: stmts });
  }
  if (state.matches) {
    out.sort((a, b) => (state.matches.get(b.id).score - state.matches.get(a.id).score));
  }
  return out;
}

// --------------------------------------------------------------------------
// render
// --------------------------------------------------------------------------

function render() {
  const funds = visibleFunds();
  const total = funds.reduce((n, f) => n + f.statements.length, 0);

  const per = state.perPage || funds.length || 1;
  const pages = Math.max(1, Math.ceil(funds.length / per));
  if (state.page > pages) state.page = pages;
  const from = (state.page - 1) * per;
  const shown = state.perPage ? funds.slice(from, from + per) : funds;

  $("count").textContent = funds.length
    ? `${funds.length} fund${funds.length === 1 ? "" : "s"}, ${total} statement${total === 1 ? "" : "s"}`
      + (state.perPage && funds.length > per
         ? ` — showing ${from + 1}–${from + shown.length}` : "")
    : "";

  const pager = $("pager");
  pager.hidden = pages <= 1;
  if (pages > 1) {
    $("pageinfo").textContent = `Page ${state.page} of ${pages}`;
    $("prevpage").disabled = state.page === 1;
    $("nextpage").disabled = state.page === pages;
  }

  if (!funds.length) {
    const fam = state.family
      ? (state.index.families || []).find((f) => f.id === state.family) : null;
    const code = fundCodeHelp(state.query);
    $("results").innerHTML = code
      ? code
      : fam
      ? familyGuidance(fam)
      : `<div class="empty">
          <p><strong>Nothing matched.</strong></p>
          <p>If you expected a statement here, pick the manager from the
          <em>Fund family</em> list above &mdash; every company checked is listed
          there, including those with nothing available, with what was found and
          a link to their site.</p>
          <p>The <em>Not available</em> tab records the same findings in detail.</p>
        </div>`;
    updateTray();
    return;
  }

  $("results").innerHTML = shown.map((f) => {
    const open = state.expanded.has(f.id);
    const m = state.matches?.get(f.id);
    const fam = state.index.families.find((x) => x.id === f.family);
    const oddFye = fam && fam.fye && fam.fye !== "12-31";
    return `
    <div class="fund ${m ? "match" : ""}">
      <div class="fundhead" data-fund="${esc(f.id)}">
        <input type="checkbox" data-fundck="${esc(f.id)}"
               ${f.statements.every((s) => state.selected.has(keyOf(f, s))) ? "checked" : ""}>
        <div class="fundmain">
          <div class="fundname">${esc(f.name)}</div>
          <div class="fundmeta">
            <span>${esc(f.family_name)}</span>
            ${f.tickers.map((t) => `<span class="tick">${esc(t)}</span>`).join("")}
            <span class="badge">${f.statements.length} yr</span>
            ${oddFye ? `<span class="badge fye">FYE ${esc(fam.fye)}</span>` : ""}
            ${m ? `<span class="matchnote">matched: ${esc(m.why)}</span>` : ""}
          </div>
        </div>
        <span class="caret">${open ? "&#9662;" : "&#9656;"}</span>
      </div>
      ${open ? `<div class="stmts">${f.statements.map((s) => `
        <div class="stmt">
          <input type="checkbox" data-stmt="${esc(keyOf(f, s))}"
                 ${state.selected.has(keyOf(f, s)) ? "checked" : ""}>
          <span class="yr">${s.tax_year ?? "&mdash;"}</span>
          <span class="pe">${s.period_end ? "ended " + esc(s.period_end) : "period unread"}</span>
          <a href="${esc(s.url)}" target="_blank" rel="noopener">${
            s.fmt === "html" ? "open page" : "open PDF"}</a>
          <span class="sz">${s.fmt === "html"
            ? `<span class="fmt-html" title="Published as a table on the manager's page, not as a downloadable document">table</span>`
            : kb(s.bytes)}</span>
        </div>
        ${s.fmt === "html" && s.figures ? `<div class="figs">${
          Object.entries(s.figures).map(([k, v]) =>
            `<span><b>${esc(v)}</b> ${esc(k)}</span>`).join("")
        }<em>as published by the manager &mdash; verify against the source page</em></div>` : ""}`).join("")}</div>` : ""}
    </div>`;
  }).join("");

  updateTray();
}

function renderNegatives() {
  const negs = state.index.negatives || [];
  $("negatives").innerHTML = negs.length ? negs.map((n) => `
    <div class="neg">
      <h3>${esc(n.scope)}</h3>
      ${n.note ? `<p class="why">${esc(n.note)}</p>` : ""}
      <div class="tagline">
        <span class="tag ${esc(n.finding)}">${esc(String(n.finding).replace(/_/g, " "))}</span>
        <span>verified ${esc(n.verified)} (${esc(n.verified_by)})</span>
        ${n.evidence ? `<a href="${esc(n.evidence)}" target="_blank" rel="noopener">evidence</a>` : ""}
      </div>
    </div>`).join("") : `<div class="empty">No negative findings recorded yet.</div>`;
}

function renderHealth() {
  const h = state.index.health || [];
  $("health").innerHTML = h.map((x) => `
    <div class="hrow">
      <span class="dot ${esc(x.status)}"></span>
      <span class="hname">${esc(x.name)}</span>
      <span class="hnum">${x.statements}/${x.candidates}</span>
      <span class="hmsg">${esc(x.message || x.status)}</span>
      <a href="${esc(x.hub)}" target="_blank" rel="noopener">source</a>
    </div>`).join("");
}

function updateTray() {
  const n = state.selected.size;
  $("tray").hidden = n === 0;
  $("traycount").textContent = `${n} statement${n === 1 ? "" : "s"} selected`;
  // A selection of only HTML statements needs no proxy - those files are
  // generated in the browser rather than fetched.
  const needsProxy = [...state.selected.values()].some((x) => x.stmt.fmt !== "html");
  $("btnzip").disabled = needsProxy && !CFG.PDF_PROXY;
  $("btnzip").title = $("btnzip").disabled
    ? "ZIP bundling of PDFs needs the proxy Worker deployed - see config.js."
    : "";
}

// --------------------------------------------------------------------------
// client document identification (local only)
// --------------------------------------------------------------------------

async function readPdf(file) {
  const buf = await file.arrayBuffer();
  const doc = await pdfjsLib.getDocument({ data: buf }).promise;
  let text = "";
  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i);
    const content = await page.getTextContent();
    text += content.items.map((it) => it.str).join(" ") + "\n";
  }
  return text;
}

/**
 * Match a client document against the index.
 *
 * Deliberately conservative. A fund is only proposed when either an exact
 * ticker appears as a standalone token, or most of the fund's distinctive name
 * tokens are present. Over-matching here would be worse than under-matching:
 * a wrong fund quietly filed is a bigger problem than one you had to look up.
 */
export function identify(text) {
  // Match on contiguous token runs, not bag-of-words overlap. Fund families
  // name products in nested series, so one fund's name is routinely a subset of
  // another's ("Short Term Bond" inside "Short Term Corporate Bond"). Set
  // overlap treats those as the same fund; requiring the tokens to appear in
  // order, adjacent, does not.
  const docStr = ` ${tokens(text).join(" ")} `;
  const upper = text.toUpperCase();
  const hits = new Map();

  for (const fund of state.index.funds) {
    let score = 0;
    const why = [];

    for (const t of fund.tickers) {
      if (t.length >= 2 && new RegExp(`(^|[^A-Z0-9])${t}([^A-Z0-9]|$)`).test(upper)) {
        score += 10; why.push(`ticker ${t}`); break;
      }
    }

    const toks = tokens(fund.name);
    if (toks.length >= 2 && docStr.includes(` ${toks.join(" ")} `)) {
      score += 6 + Math.min(toks.length, 6);
      why.push("full name");
    }

    if (score > 0) hits.set(fund.id, { score, why: why.join(", ") });
  }
  return hits;
}

async function handleFiles(files) {
  const box = $("dropstatus");
  box.hidden = false;
  box.innerHTML = `Reading ${files.length} file(s) locally&hellip;`;
  try {
    let text = "";
    for (const f of files) {
      if (f.type !== "application/pdf") continue;
      text += await readPdf(f) + "\n";
    }
    if (!text.trim()) {
      box.innerHTML = `<strong>No text found.</strong> This looks like a scanned PDF.
        The page cannot OCR it without uploading it somewhere, which it will not do &mdash;
        type the fund names into the search box instead.`;
      return;
    }
    const hits = identify(text);
    state.matches = hits.size ? hits : null;
    state.page = 1;
    state.expanded = new Set([...hits.keys()].slice(0, 8));
    box.innerHTML = hits.size
      ? `<strong>${hits.size} candidate fund${hits.size === 1 ? "" : "s"} identified.</strong>
         Results below are filtered to these matches.
         <button id="clrmatch" class="ghost" style="padding:3px 9px">show everything</button>
         <div class="hint">A match is a lead, not a determination. Confirm the fund, series
         and period against the client&rsquo;s holding before using it.</div>`
      : `<strong>No funds in the index matched this document.</strong>
         That is not the same as &ldquo;not a PFIC&rdquo; &mdash; the fund may simply not be
         indexed yet, or no statement may be available. Check the <em>Not available</em> tab.`;
    $("clrmatch")?.addEventListener("click", () => {
      state.matches = null; state.page = 1; box.hidden = true; render();
    });
    render();
  } catch (e) {
    box.innerHTML = `<strong style="color:var(--bad)">Could not read that file.</strong> ${esc(e.message)}`;
  }
}

// --------------------------------------------------------------------------
// downloads
// --------------------------------------------------------------------------

const safe = (s) => String(s).replace(/[^A-Za-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 70);

function fileNameFor(fund, stmt) {
  const yr = stmt.tax_year || (stmt.period_end || "").slice(0, 4) || "unknown";
  const ext = stmt.fmt === "html" ? "html" : "pdf";
  return `${safe(fund.family_name)}__${safe(fund.name)}__FY${yr}__PFIC_AIS.${ext}`;
}

/**
 * Build a working-paper page for a statement the manager published as a table
 * rather than a document. There is nothing to download in those cases, so the
 * ZIP would otherwise contain a gap exactly where a fund actually does have a
 * statement. The figures are reproduced as published, with the source URL and
 * retrieval date on the page so the file stands on its own in a file.
 */
export function htmlStatementFile(fund, stmt) {
  const rows = Object.entries(stmt.figures || {})
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("");
  return `<!doctype html><meta charset="utf-8">
<title>${esc(fund.name)} — PFIC AIS ${stmt.tax_year ?? ""}</title>
<style>
 body{font:14px/1.55 system-ui,sans-serif;margin:36px;max-width:760px;color:#14181d}
 h1{font-size:1.2rem;margin:0 0 4px}
 .meta{color:#5a636e;margin-bottom:18px}
 table{border-collapse:collapse;width:100%;margin:14px 0}
 th,td{border:1px solid #dfe3e8;padding:8px 10px;text-align:left;vertical-align:top}
 th{background:#f6f7f9;width:58%;font-weight:600}
 td{font-variant-numeric:tabular-nums}
 .note{background:#fff8e6;border:1px solid #f0d999;padding:11px 13px;border-radius:7px;
       font-size:.9rem;margin-top:20px}
 a{color:#1d4ed8}
</style>
<h1>${esc(fund.name)}</h1>
<div class="meta">${esc(fund.family_name)}
  ${fund.tickers.length ? " &middot; " + esc(fund.tickers.join(", ")) : ""}<br>
  PFIC Annual Information Statement &mdash; fund tax year
  ${esc(String(stmt.tax_year ?? "unknown"))}${
    stmt.period_end ? `, period ended ${esc(stmt.period_end)}` : ""}</div>
<table><tbody>${rows}</tbody></table>
<div class="note">
  <strong>This is a transcription, not the manager's own document.</strong>
  ${esc(fund.family_name)} publishes this statement as a table on its website
  rather than as a downloadable file, so the figures above were captured from
  that page and are reproduced exactly as published &mdash; nothing has been
  recomputed or converted.
  <br><br>
  Source: <a href="${esc(stmt.url)}">${esc(stmt.url)}</a><br>
  Retrieved for this file: ${new Date().toISOString().slice(0, 10)}<br>
  <strong>Verify against the source page before using it.</strong>
</div>`;
}

async function downloadZip() {
  const items = [...state.selected.values()];
  if (!items.length) return;
  const prog = $("progress"), bar = $("bar"), ptext = $("ptext");
  prog.hidden = false;
  let done = 0, failed = [];
  const zip = new JSZip();

  const queue = items.slice();
  async function worker() {
    while (queue.length) {
      const { fund, stmt } = queue.shift();
      try {
        if (stmt.fmt === "html") {
          // Nothing to fetch - the manager published a table, not a file.
          zip.file(fileNameFor(fund, stmt), htmlStatementFile(fund, stmt));
        } else {
          const url = `${CFG.PDF_PROXY}/?u=${encodeURIComponent(stmt.url)}`;
          const r = await fetch(url);
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          zip.file(fileNameFor(fund, stmt), await r.arrayBuffer());
        }
      } catch (e) {
        failed.push(`${fund.name} FY${stmt.tax_year}: ${e.message}`);
      }
      done++;
      bar.style.width = `${(done / items.length) * 100}%`;
      ptext.textContent = `${done} / ${items.length}`;
    }
  }
  await Promise.all(Array.from({ length: Math.min(CFG.MAX_PARALLEL_DOWNLOADS, items.length) }, worker));

  zip.file("manifest.csv", manifestCsv(items));
  if (failed.length) zip.file("FAILED.txt", failed.join("\n"));

  const blob = await zip.generateAsync({ type: "blob" });
  triggerDownload(blob, `QEF_statements_${new Date().toISOString().slice(0, 10)}.zip`);
  ptext.textContent = failed.length
    ? `${done - failed.length} bundled, ${failed.length} failed (see FAILED.txt in the ZIP)`
    : `${done} bundled.`;
  setTimeout(() => { prog.hidden = true; bar.style.width = "0"; }, 6000);
}

function manifestCsv(items) {
  const rows = [["Fund family", "Fund", "Tickers", "Fund tax year", "Period end",
                 "Format", "File name", "Source URL", "Bytes", "SHA256 (first 16)",
                 "Published figures"]];
  for (const { fund, stmt } of items) {
    const figs = stmt.figures
      ? Object.entries(stmt.figures).map(([k, v]) => `${k}: ${v}`).join(" | ") : "";
    rows.push([fund.family_name, fund.name, fund.tickers.join(" "), stmt.tax_year ?? "",
               stmt.period_end ?? "", stmt.fmt === "html" ? "web table" : "PDF",
               fileNameFor(fund, stmt), stmt.url, stmt.bytes, stmt.sha256, figs]);
  }
  return rows.map((r) => r.map((c) => `"${String(c ?? "").replace(/"/g, '""')}"`).join(",")).join("\r\n");
}

function triggerDownload(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

// --------------------------------------------------------------------------
// events
// --------------------------------------------------------------------------

function wire() {
  let t;
  $("q").addEventListener("input", (e) => {
    clearTimeout(t);
    t = setTimeout(() => { state.query = e.target.value; state.page = 1; render(); }, 140);
  });

  $("years").addEventListener("click", (e) => {
    const b = e.target.closest("[data-year]"); if (!b) return;
    const y = Number(b.dataset.year);
    state.years.has(y) ? state.years.delete(y) : state.years.add(y);
    b.setAttribute("aria-pressed", state.years.has(y));
    state.page = 1;
    render();
  });

  $("famsel").addEventListener("change", (e) => {
    state.family = e.target.value; state.page = 1; render();
  });
  $("periodfrom").addEventListener("change", (e) => {
    state.from = e.target.value; state.page = 1; render();
  });
  $("periodto").addEventListener("change", (e) => {
    state.to = e.target.value; state.page = 1; render();
  });

  $("perpage").addEventListener("change", (e) => {
    state.perPage = Number(e.target.value);   // 0 = show everything
    state.page = 1;
    render();
  });

  const turn = (delta) => {
    state.page += delta;
    render();
    // Put the top of the list back in view rather than leaving the reader
    // stranded halfway down the previous page.
    document.querySelector(".listhead")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $("prevpage").addEventListener("click", () => turn(-1));
  $("nextpage").addEventListener("click", () => turn(1));

  $("results").addEventListener("click", (e) => {
    const ck = e.target.closest("[data-stmt]");
    if (ck) {
      const [fundId, url] = ck.dataset.stmt.split("|");
      const fund = state.index.funds.find((f) => f.id === fundId);
      const stmt = fund.statements.find((s) => s.url === url);
      ck.checked ? state.selected.set(ck.dataset.stmt, { fund, stmt })
                 : state.selected.delete(ck.dataset.stmt);
      updateTray();
      return;
    }
    const fck = e.target.closest("[data-fundck]");
    if (fck) {
      e.stopPropagation();
      const fund = visibleFunds().find((f) => f.id === fck.dataset.fundck);
      for (const s of fund.statements) {
        fck.checked ? state.selected.set(keyOf(fund, s), { fund, stmt: s })
                    : state.selected.delete(keyOf(fund, s));
      }
      render();
      return;
    }
    const head = e.target.closest("[data-fund]");
    if (head) {
      const id = head.dataset.fund;
      state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
      render();
    }
  });

  $("selall").addEventListener("change", (e) => {
    for (const fund of visibleFunds()) {
      for (const s of fund.statements) {
        e.target.checked ? state.selected.set(keyOf(fund, s), { fund, stmt: s })
                         : state.selected.delete(keyOf(fund, s));
      }
    }
    render();
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      tab.classList.add("active");
      for (const p of ["results", "negatives", "health"]) {
        $("tab-" + p).hidden = p !== tab.dataset.tab;
      }
    });
  });

  const drop = $("drop"), file = $("file");
  drop.addEventListener("click", () => file.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") file.click(); });
  file.addEventListener("change", (e) => handleFiles([...e.target.files]));
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => handleFiles([...e.dataTransfer.files]));

  $("btnzip").addEventListener("click", downloadZip);
  $("btnclear").addEventListener("click", () => { state.selected.clear(); render(); });
  $("btncsv").addEventListener("click", () => {
    triggerDownload(new Blob([manifestCsv([...state.selected.values()])],
      { type: "text/csv" }), "qef_manifest.csv");
  });
  $("btnopen").addEventListener("click", () => {
    const items = [...state.selected.values()];
    if (items.length > 12 &&
        !confirm(`Open ${items.length} tabs? Your browser may block some.`)) return;
    for (const { stmt } of items) window.open(stmt.url, "_blank", "noopener");
  });
}

boot();
