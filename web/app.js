/**
 * QEF Statement Lookup - the page.
 *
 * Everything here runs in the reader's browser. Client documents dropped onto
 * the page are parsed locally with PDF.js and never leave the machine; the only
 * network calls are for the static index and, when bundling a ZIP, the
 * statements themselves. There is no server-side session and nothing about a
 * lookup is logged.
 *
 * The logic that decides WHAT matches and WHAT gets downloaded lives in
 * lib.js, where it can be tested. This file is the wiring to the page.
 */

import {
  esc, kb, keyOf, splitKey, queryTerms, queryScore, identify, planDownloads,
  manifestCsv, htmlStatementFile, healthSummary, linksOnly, notDownloadedText,
  NOT_CRAWLED,
} from "./lib.js";

const CFG = window.QEF_CONFIG;
const $ = (id) => document.getElementById(id);

// Loaded only when first needed. They used to be loaded up front, which meant
// that if the CDN was slow or blocked, the whole page - search included -
// never started, for the sake of two features most visits do not use.
const PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.6.82/pdf.min.mjs";
const PDFJS_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.6.82/pdf.worker.min.mjs";
const JSZIP = "https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js";

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
  visible: [],           // what the current filters show; set by render()
};

const familyById = (id) => (state.index.families || []).find((f) => f.id === id);

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
      `Check your connection and reload the page.</span>`;
    return;
  }
  const s = state.index.stats;
  const h = healthSummary(state.index.health);
  const generated = new Date(state.index.generated);
  const ageDays = (Date.now() - generated.getTime()) / 86400000;
  $("stats").innerHTML = [
    `<span><b>${s.funds.toLocaleString()}</b> funds</span>`,
    `<span><b>${s.statements.toLocaleString()}</b> statements</span>`,
    `<span title="Managers collected automatically. ${h.byHand} more are listed with a link because their statements have to be fetched by hand."><b>${h.ok}/${h.total}</b> sources healthy</span>`,
    `<span class="muted">updated ${generated.toLocaleDateString()}</span>`,
    // The index is rebuilt nightly. If it has not been, say so rather than
    // let week-old data pass as current.
    ageDays > 4 ? `<span style="color:var(--warn)">not refreshed for ${Math.floor(ageDays)} days</span>` : "",
  ].join("");
  $("built").textContent = `Index generated ${generated.toUTCString()}.`;

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
      : f.access === "request" ? " — on request"
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
 * no codes for most mutual funds to match against. Returning a blank "nothing
 * found" for a code the user copied straight off a client statement is the
 * worst possible answer: it reads as "no statement exists".
 */
function fundCodeHelp(query) {
  const term = (query || "").trim().toUpperCase();
  const m = /^([A-Z]{3})(\d{2,5})$/.exec(term);
  if (!m) return "";
  const entry = (state.index.fund_code_prefixes || [])
    .find((p) => p.prefix === m[1]);
  const fam = entry && entry.family ? familyById(entry.family) : null;
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
       the code on the web to identify the manager.</p>` : ""}
  </div>`;
}

/**
 * Guidance for managers matched by NAME.
 *
 * Typing "pimco" used to produce a bare "Nothing matched", because PIMCO has
 * no funds in the index - which reads as "no statement exists" when the truth
 * is "PIMCO publishes these, we just cannot fetch them for you". So the search
 * is matched against manager names too.
 */
function familyMatchPanels(query) {
  const terms = queryTerms(query);
  if (!terms.length) return "";
  const hits = (state.index.families || []).filter((f) => {
    // Families with funds would already have appeared in the results.
    if ((f.fund_count || 0) > 0) return false;
    // `covers` is the list of funds a manager says it has statements for but
    // does not post (Sun Life). Searching one of those funds by name should
    // say how to get it, not "nothing matched".
    const hay = ` ${queryTerms(`${f.name} ${f.id}`).join(" ")} `;
    const covers = (f.covers || []).map((c) => ` ${queryTerms(c).join(" ")} `);
    return terms.some((t) => {
      const words = t.split(" ");
      return words.every((w) => hay.includes(w)) ||
        (t.length >= 4 && covers.some((c) => words.every((w) => c.includes(w))));
    });
  });
  return hits.map(familyGuidance).join("");
}

/**
 * Panel shown when a family has no statements to list. Empty results are the
 * moment the tool is most likely to mislead: "nothing found" reads as "no
 * statement exists". So say which of the two it is - in one sentence - and
 * what to do about it, in one more. Anything longer does not get read.
 */
function familyGuidance(fam) {
  if (!fam) return "";
  // Wording is deliberate. We report what was found on a manager's public
  // website on a date - never that a manager refuses to issue a statement.
  // One may well be available on request, which is what the action says.
  const kind = fam.access === "none"
    ? { cls: "none", head: "No PFIC statement available" }
    : fam.access === "request"
      ? { cls: "manual", head: "On request only" }
    : fam.access === "manual"
      ? { cls: "manual", head: "Fetch by hand" }
      : { cls: "unknown", head: "Nothing indexed yet" };
  const linkLabel = fam.access === "none"
    ? "Open the manager's website"
    : "Open the manager's PFIC page";
  const covers = fam.covers || [];
  return `<div class="guide ${kind.cls}">
    <h3>${esc(kind.head)} <span class="who">${esc(fam.name)}</span></h3>
    <p>${esc((fam.guidance || "").trim()) || "No further detail recorded yet."}</p>
    ${fam.action ? `<p class="todo"><strong>What to do:</strong> ${esc(fam.action.trim())}</p>` : ""}
    ${covers.length ? `<details class="covers"><summary>The ${covers.length} funds it names</summary>
        <ul>${covers.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></details>` : ""}
    ${fam.hub ? `<p class="go"><a class="gobtn" href="${esc(fam.hub)}" target="_blank"
        rel="noopener">${linkLabel} &rarr;</a></p>` : ""}
  </div>`;
}

// --------------------------------------------------------------------------
// filtering
// --------------------------------------------------------------------------

function visibleFunds() {
  const terms = queryTerms(state.query);
  const out = [];
  for (const fund of state.index.funds) {
    if (state.family && fund.family !== state.family) continue;
    if (state.matches && !state.matches.has(fund.id)) continue;
    const score = queryScore(fund, terms);
    if (!score) continue;
    const stmts = fund.statements.filter((s) => {
      if (state.years.size && !state.years.has(s.tax_year)) return false;
      if (state.from && (!s.period_end || s.period_end < state.from)) return false;
      if (state.to && (!s.period_end || s.period_end > state.to)) return false;
      return true;
    });
    if (!stmts.length) continue;
    out.push({ ...fund, statements: stmts, _score: score });
  }
  if (state.matches) {
    out.sort((a, b) => (state.matches.get(b.id).score - state.matches.get(a.id).score));
  } else if (terms.length) {
    // Best match first; the index order (family, then name) breaks ties, and
    // Array.prototype.sort keeps that order for equal scores.
    out.sort((a, b) => b._score - a._score);
  }
  return out;
}

// --------------------------------------------------------------------------
// render
// --------------------------------------------------------------------------

/**
 * What a reader needs to know about a manager before relying on its rows:
 * that the links were never opened, that only the latest year exists, that
 * the amounts are per day. Shown at the top of every opened fund.
 */
function familyNote(fam) {
  if (!fam) return "";
  const parts = [];
  if (fam.links_only) {
    parts.push(`These are ${esc(fam.name)}&rsquo;s own links, as listed on its PFIC page.
      They have <strong>not been opened or checked</strong> here &mdash; its site asks
      automated tools not to fetch its documents &mdash; so confirm the fund and year when
      you open one. They cannot be added to a ZIP; use the link.`);
  }
  if (fam.caveat) parts.push(esc(fam.caveat.trim()));
  return parts.length ? `<div class="famnote">${parts.join(" ")}</div>` : "";
}

function statementRow(f, s) {
  const key = keyOf(f, s);
  const link = s.fmt === "html" ? "open page"
    : s.fmt === "link" ? "open on the manager&rsquo;s site"
    : s.part ? `open PDF at page ${s.part.page}` : "open PDF";
  const size = s.fmt === "html"
    ? `<span class="fmt-html" title="Published as a table on the manager's page, not as a downloadable document">table</span>`
    : s.fmt === "link"
    ? `<span class="fmt-link" title="The manager's own link, as listed on its PFIC page. Not opened or checked by this tool, because the manager's site asks automated tools not to fetch its documents. Cannot be added to a ZIP.">link only</span>`
    : s.part
      ? `<span title="One document covering many funds; this fund starts on page ${s.part.page}">p.&nbsp;${s.part.page}${s.part.pages ? " of " + s.part.pages : ""}</span>`
      : kb(s.bytes);
  const figs = s.fmt === "html" && s.figures ? `<div class="figs">${
    Object.entries(s.figures).map(([k, v]) =>
      `<span><b>${esc(v)}</b> ${esc(k)}</span>`).join("")
  }<em>as published by the manager &mdash; verify against the source page</em></div>` : "";
  const investees = (s.investees || []).length ? `<details class="investees">
      <summary>${s.investees.length} direct investee fund${s.investees.length === 1 ? "" : "s"}
        listed for holders of this fund</summary>
      <p class="hint">Amounts attributable to one unit of <strong>this</strong> fund
        through the funds below &mdash; not those funds&rsquo; own statements.</p>
      ${s.investees.map((i) => `<div class="inv"><span class="invname">${esc(i.name)}</span>${
        Object.entries(i.figures || {}).map(([k, v]) =>
          `<span><b>${esc(v)}</b> ${esc(k)}</span>`).join("")}</div>`).join("")}
    </details>` : "";
  return `
        <div class="stmt">
          <input type="checkbox" data-stmt="${esc(key)}"
                 ${state.selected.has(key) ? "checked" : ""}>
          <span class="yr">${s.tax_year ?? "&mdash;"}</span>
          <span class="pe">${s.period_end ? "ended " + esc(s.period_end)
            : s.fmt === "link" ? (s.tax_year ? "as listed by the manager" : "year not stated by the manager")
            : "period unread"}${
            s.name ? ` <span class="asname" title="The manager published this year's statement under a different fund name">as &ldquo;${esc(s.name)}&rdquo;</span>` : ""}</span>
          <a href="${esc(s.url)}" target="_blank" rel="noopener">${link}</a>
          <span class="sz">${size}</span>
        </div>${figs}${investees}`;
}

function render() {
  const funds = state.visible = visibleFunds();
  const total = funds.reduce((n, f) => n + f.statements.length, 0);

  const per = state.perPage || funds.length || 1;
  const pages = Math.max(1, Math.ceil(funds.length / per));
  if (state.page > pages) state.page = pages;
  const from = (state.page - 1) * per;
  const shown = state.perPage ? funds.slice(from, from + per) : funds;

  $("count").textContent = funds.length
    ? `${funds.length.toLocaleString()} fund${funds.length === 1 ? "" : "s"}, ${total.toLocaleString()} statement${total === 1 ? "" : "s"}`
      + (state.perPage && funds.length > per
         ? ` — showing ${from + 1}–${from + shown.length}` : "")
    : "";
  $("selall").checked = funds.length > 0 &&
    funds.every((f) => f.statements.every((s) => state.selected.has(keyOf(f, s))));

  const pager = $("pager");
  pager.hidden = pages <= 1;
  if (pages > 1) {
    $("pageinfo").textContent = `Page ${state.page} of ${pages}`;
    $("prevpage").disabled = state.page === 1;
    $("nextpage").disabled = state.page === pages;
  }

  if (!funds.length) {
    const fam = state.family ? familyById(state.family) : null;
    const code = fundCodeHelp(state.query);
    const byName = familyMatchPanels(state.query);
    $("results").innerHTML = code
      ? code
      : fam
      ? familyGuidance(fam)
      : byName
      ? byName
      : `<div class="empty">
          <p><strong>Nothing matched.</strong></p>
          <p>If you expected a statement here, try the manager&rsquo;s name on its
          own &mdash; every company checked is in the index, including those whose
          statements cannot be collected automatically, and searching the name
          shows what was found and where to get it.</p>
          <p>The <em>Not available</em> tab lists the same findings together.</p>
        </div>`;
    updateTray();
    return;
  }

  // A manager's name typed into the search also brings up its guidance when
  // the manager is fetch-by-hand, even though other funds matched the words.
  const alsoByName = state.page === 1 && !state.matches ? familyMatchPanels(state.query) : "";

  $("results").innerHTML = alsoByName + shown.map((f) => {
    const open = state.expanded.has(f.id);
    const m = state.matches?.get(f.id);
    const fam = familyById(f.family);
    const oddFye = fam && fam.fye && fam.fye !== "12-31" && fam.fye !== "varies";
    return `
    <div class="fund ${m ? "match" : ""}">
      <div class="fundhead" data-fund="${esc(f.id)}">
        <input type="checkbox" data-fundck="${esc(f.id)}" aria-label="Select every statement for ${esc(f.name)}"
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
      ${open ? `<div class="stmts">${familyNote(fam)}${
        f.statements.map((s) => statementRow(f, s)).join("")}</div>` : ""}
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
      ${n.action ? `<p class="why"><strong>What to do:</strong> ${esc(n.action)}</p>` : ""}
      <div class="tagline">
        <span class="tag ${esc(n.finding)}">${esc(String(n.finding).replace(/_/g, " "))}</span>
        <span>verified ${esc(n.verified)} (${esc(n.verified_by)})</span>
        ${n.evidence ? `<a href="${esc(n.evidence)}" target="_blank" rel="noopener">evidence</a>` : ""}
      </div>
    </div>`).join("") : `<div class="empty">No negative findings recorded yet.</div>`;
}

function renderHealth() {
  const h = state.index.health || [];
  const label = { ok: "collected", manual: "fetch by hand", request: "on request",
                  none: "nothing posted",
                  empty: "nothing collected", blocked: "refused", down: "unreachable",
                  error: "crawler error", unknown: "not checked yet" };
  $("health").innerHTML = h.map((x) => `
    <div class="hrow">
      <span class="dot ${esc(x.status)}" title="${esc(x.status)}"></span>
      <span class="hname">${esc(x.name)}</span>
      <span class="hnum">${NOT_CRAWLED.has(x.status) ? "" : `${x.statements}/${x.candidates}`}</span>
      <span class="hmsg"><b>${esc(label[x.status] || x.status)}</b>${x.message ? " — " + esc(x.message) : ""}${
        x.requests != null ? ` <span class="hstat" title="How the last crawl got these: statements answered from the cache, requests actually sent to the manager, and how long it took">(${x.from_cache ?? 0} cached, ${x.requests} request${x.requests === 1 ? "" : "s"}${x.seconds != null ? `, ${x.seconds}s` : ""})</span>` : ""}</span>
      <a href="${esc(x.hub)}" target="_blank" rel="noopener">source</a>
    </div>`).join("");
}

function updateTray() {
  const n = state.selected.size;
  $("tray").hidden = n === 0;
  const items = [...state.selected.values()];
  const files = n ? planDownloads(items).length : 0;
  const links = linksOnly(items).length;
  const detail = [];
  if (files !== n) detail.push(`${files} file${files === 1 ? "" : "s"}`);
  if (links) detail.push(`${links} link-only, not downloadable`);
  $("traycount").textContent = `${n.toLocaleString()} statement${n === 1 ? "" : "s"} selected` +
    (detail.length ? ` (${detail.join("; ")})` : "");
}

// --------------------------------------------------------------------------
// client document identification (local only)
// --------------------------------------------------------------------------

let pdfjsPromise = null;
function loadPdfJs() {
  pdfjsPromise ??= import(PDFJS).then((lib) => {
    lib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
    return lib;
  }).catch((e) => { pdfjsPromise = null; throw e; });
  return pdfjsPromise;
}

async function readPdf(file) {
  const pdfjsLib = await loadPdfJs();
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

async function handleFiles(files) {
  const box = $("dropstatus");
  box.hidden = false;
  const pdfs = files.filter((f) => f.type === "application/pdf" || /\.pdf$/i.test(f.name));
  if (!pdfs.length) {
    box.innerHTML = `<strong>That is not a PDF.</strong> Drop a PDF statement, or type
      the fund names into the search box instead.`;
    return;
  }
  box.innerHTML = `Reading ${pdfs.length} file${pdfs.length === 1 ? "" : "s"} locally&hellip;`;
  try {
    let text = "";
    for (const f of pdfs) text += await readPdf(f) + "\n";
    if (!text.trim()) {
      box.innerHTML = `<strong>No text found.</strong> This looks like a scanned PDF.
        The page cannot OCR it without uploading it somewhere, which it will not do &mdash;
        type the fund names into the search box instead.`;
      return;
    }
    const hits = identify(text, state.index.funds);
    state.matches = hits.size ? hits : null;
    state.page = 1;
    state.expanded = new Set([...hits.entries()]
      .sort((a, b) => b[1].score - a[1].score).slice(0, 8).map(([id]) => id));
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

let jszipPromise = null;
function loadJsZip() {
  jszipPromise ??= new Promise((resolve, reject) => {
    if (window.JSZip) return resolve(window.JSZip);
    const s = document.createElement("script");
    s.src = JSZIP;
    s.onload = () => resolve(window.JSZip);
    s.onerror = () => { jszipPromise = null; reject(new Error("could not load the ZIP library")); };
    document.head.appendChild(s);
  });
  return jszipPromise;
}

/**
 * Where to fetch statements through. Browsers will not let a page read a PDF
 * from another site, so the request goes via the site's own /pdf address. The
 * older stand-alone proxy is kept as a fallback for when this page is not
 * being served by the Worker (a local preview, for instance).
 */
let fetchViaPromise = null;
function fetchVia() {
  fetchViaPromise ??= (async () => {
    try {
      const r = await fetch("pdf/health", { cache: "no-store" });
      if (r.ok && (await r.json()).ok) return (u) => `pdf?u=${encodeURIComponent(u)}`;
    } catch { /* fall through */ }
    if (CFG.PDF_PROXY) return (u) => `${CFG.PDF_PROXY}/?u=${encodeURIComponent(u)}`;
    return null;
  })();
  return fetchViaPromise;
}

async function fetchPdf(via, url) {
  let lastError;
  for (let attempt = 0; attempt < 2; attempt++) {     // one retry: transient failures are common
    try {
      const r = await fetch(via(url));
      if (r.ok) return await r.arrayBuffer();
      let why = `HTTP ${r.status}`;
      try { const j = await r.json(); if (j.error) why = j.error + (j.status ? ` (${j.status})` : ""); } catch { /* not JSON */ }
      lastError = new Error(why);
      if (r.status === 400 || r.status === 403) break;  // retrying will not change a refusal
    } catch (e) {
      lastError = e;
    }
  }
  throw lastError;
}

async function downloadZip() {
  const items = [...state.selected.values()];
  if (!items.length) return;
  const files = planDownloads(items);
  if (files.length > 400 &&
      !confirm(`That is ${files.length} documents in one ZIP. It may take several minutes ` +
               `and a lot of memory. Continue?`)) return;

  const prog = $("progress"), bar = $("bar"), ptext = $("ptext"), btn = $("btnzip");
  prog.hidden = false;
  btn.disabled = true;
  ptext.textContent = "Preparing…";
  let done = 0;
  const failed = new Map();        // file name -> reason

  try {
    const needsFetch = files.some((f) => f.kind === "pdf");
    const [JSZip, via] = await Promise.all([loadJsZip(), needsFetch ? fetchVia() : null]);
    if (needsFetch && !via) throw new Error("no download service is available");
    const zip = new JSZip();

    const queue = files.slice();
    const worker = async () => {
      while (queue.length) {
        const f = queue.shift();
        try {
          if (f.kind === "html") {
            // Nothing to fetch - the manager published a table, not a file.
            const { fund, stmt } = f.items[0];
            zip.file(f.name, htmlStatementFile(fund, stmt));
          } else {
            zip.file(f.name, await fetchPdf(via, f.url));
          }
        } catch (e) {
          failed.set(f.name, e.message || String(e));
        }
        done++;
        bar.style.width = `${(done / Math.max(1, files.length)) * 100}%`;
        ptext.textContent = `${done} / ${files.length}`;
      }
    };
    await Promise.all(Array.from(
      { length: Math.min(CFG.MAX_PARALLEL_DOWNLOADS, files.length) }, worker));

    zip.file("manifest.csv", manifestCsv(items, files, failed));
    const links = linksOnly(items);
    if (links.length) zip.file("NOT_DOWNLOADED.txt", notDownloadedText(items));
    if (failed.size) {
      // Each failed document with its own address, so it can still be
      // fetched by hand without going back to the site to find it again.
      zip.file("FAILED.txt",
        "These documents could not be added to the ZIP. Open each address to download it directly.\r\n\r\n" +
        files.filter((f) => failed.has(f.name))
          .map((f) => `${f.name}\r\n  ${f.url}\r\n  reason: ${failed.get(f.name)}`).join("\r\n\r\n"));
    }

    ptext.textContent = "Building the ZIP…";
    const blob = await zip.generateAsync({ type: "blob" });
    triggerDownload(blob, `QEF_statements_${new Date().toISOString().slice(0, 10)}.zip`);
    ptext.textContent = (failed.size
      ? `${done - failed.size} bundled, ${failed.size} failed — see FAILED.txt in the ZIP for direct links`
      : `${done} bundled.`) +
      (links.length ? ` ${links.length} link-only statement${links.length === 1 ? " is" : "s are"} ` +
        `listed in NOT_DOWNLOADED.txt — open those on the manager's site.` : "");
  } catch (e) {
    ptext.textContent = `Could not build the ZIP: ${e.message}. "Open in tabs" still works.`;
  } finally {
    btn.disabled = false;
    setTimeout(() => { prog.hidden = true; bar.style.width = "0"; }, 9000);
  }
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

function setSelected(fund, stmts, on) {
  // Always store the full fund from the index, never the filtered copy, so a
  // selection made under one filter is the same object under another.
  const full = state.index.funds.find((f) => f.id === fund.id) || fund;
  for (const s of stmts) {
    on ? state.selected.set(keyOf(full, s), { fund: full, stmt: s })
       : state.selected.delete(keyOf(full, s));
  }
}

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
      const [fundId, url] = splitKey(ck.dataset.stmt);
      const fund = state.index.funds.find((f) => f.id === fundId);
      const stmt = fund?.statements.find((s) => s.url === url);
      if (stmt) setSelected(fund, [stmt], ck.checked);
      render();
      return;
    }
    const fck = e.target.closest("[data-fundck]");
    if (fck) {
      const fund = state.visible.find((f) => f.id === fck.dataset.fundck);
      if (fund) setSelected(fund, fund.statements, fck.checked);
      render();
      return;
    }
    // Links and the investee disclosure inside an open fund must not toggle it.
    if (e.target.closest("a, details")) return;
    const head = e.target.closest("[data-fund]");
    if (head) {
      const id = head.dataset.fund;
      state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
      render();
    }
  });

  $("selall").addEventListener("change", (e) => {
    const funds = state.visible;
    const count = funds.reduce((n, f) => n + f.statements.length, 0);
    if (e.target.checked && count > 1000 &&
        !confirm(`Select all ${count.toLocaleString()} statements shown? ` +
                 `Narrow the search or pick a tax year first if that is more than you meant.`)) {
      e.target.checked = false;
      return;
    }
    for (const fund of funds) setSelected(fund, fund.statements, e.target.checked);
    render();
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => {
        x.classList.toggle("active", x === tab);
        x.setAttribute("aria-selected", x === tab);
      });
      for (const p of ["results", "negatives", "health"]) {
        $("tab-" + p).hidden = p !== tab.dataset.tab;
      }
    });
  });

  const drop = $("drop"), file = $("file");
  drop.addEventListener("click", () => file.click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); }
  });
  file.addEventListener("change", (e) => { handleFiles([...e.target.files]); e.target.value = ""; });
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => handleFiles([...e.dataTransfer.files]));
  // A PDF dropped anywhere else on the page would otherwise make the browser
  // navigate away to it, losing the selection.
  ["dragover", "drop"].forEach((ev) =>
    window.addEventListener(ev, (e) => { if (!e.target.closest?.("#drop")) e.preventDefault(); }));

  $("btnzip").addEventListener("click", downloadZip);
  $("btnclear").addEventListener("click", () => { state.selected.clear(); render(); });
  $("btncsv").addEventListener("click", () => {
    triggerDownload(new Blob(["﻿" + manifestCsv([...state.selected.values()])],
      { type: "text/csv" }), "qef_manifest.csv");
  });
  $("btnopen").addEventListener("click", () => {
    const urls = [...new Set([...state.selected.values()].map((x) => x.stmt.url))];
    if (urls.length > 12 &&
        !confirm(`Open ${urls.length} tabs? Your browser may block some.`)) return;
    for (const u of urls) window.open(u, "_blank", "noopener");
  });
}

boot();
