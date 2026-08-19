# QEF Statement Lookup

**Live:** https://qef-lookup.cowboy780718.workers.dev

| Piece | Where | Deployed by |
|---|---|---|
| Site | `qef-lookup` Worker | Auto — pushing to `master` triggers a Cloudflare build |
| PDF proxy | `qef-pdf-proxy` Worker | Manual, one-off. Its code does not change. |
| Crawler | GitHub Actions, nightly ~07:20 UTC | Commits `web/data/index.json`, which publishes the site |

The loop: crawler runs → commits a refreshed index → the push triggers a
Cloudflare build → the live site updates. Nothing to upload by hand.

**Only `wrangler.toml` at the repo root belongs to the site.** If Cloudflare ever
offers a PR renaming it to `qef-pdf-proxy`, decline: that would deploy the
website's files over the proxy and silently break ZIP downloads.


Find and bulk-download PFIC Annual Information Statements (QEF statements) for
Form 8621 work, instead of navigating twenty fund company websites by hand.

Runs entirely on free infrastructure. No server, no database, no monthly bill.

---

## What it does

1. **Identify** — search by fund name, ticker or fund code, or drop a client PDF
   (bank statement, T3, brokerage report) and let the page pull fund names out of it.
2. **Locate** — a pre-built index of every statement the crawler has found and
   verified, so lookups are instant rather than a live search.
3. **Select a period** — filter by the *fund's* tax year, or a custom period-end
   range. The fund's year, not the calendar year: BMO and RBC GAM run to 30 June,
   CI to 31 March.
4. **Bulk download** — multi-select across funds and years, get one ZIP with
   normalised filenames and a manifest CSV for the working paper file.
5. **Stay working** — a nightly crawl re-checks every source and opens a GitHub
   issue the moment one breaks.

### The negative findings register

"No statement exists for this fund" is a research result with the same
working-paper value as a found PDF, and it is where most of the time goes. The
**Not offered** tab records those findings with evidence and a verification
date, so a dead end is investigated once rather than once per client.

The canonical case is CIBC: CIBC-branded mutual funds, CIBC Index Funds, Managed
Portfolios and Axiom Portfolios receive **no** statement, while sister brands
Renaissance and Imperial Pools **do**. Same bank, opposite answers, nothing on
the surface to tell you which is which.

---

## Architecture

```
GitHub Actions (nightly)  ->  crawler/crawl.py  ->  web/data/index.json  ->  git commit
                                                          |
                              Cloudflare Pages  <---------+   (static site)
                                     |
                                     +--> Cloudflare Worker (PDF proxy, for ZIP bundling)
```

| Piece | Service | Free tier |
|---|---|---|
| Crawler | GitHub Actions | Unlimited minutes on a public repo |
| Index | Static JSON in the repo | — |
| Website | Cloudflare Pages | Unlimited sites, custom domain |
| PDF proxy | Cloudflare Worker | 100,000 requests/day |
| Alerting | GitHub Issues | — |

**Client documents never leave your machine.** PDFs dropped on the page are
parsed in the browser with PDF.js. There is no upload endpoint, because there is
no server.

**Nothing is rehosted.** The index stores links; documents are served from each
manager's own site. The Worker proxies bytes on demand so the browser can bundle
a ZIP, and caches them at the edge — it never becomes a document archive.

---

## Setup

### 1. Run the crawler locally

```bash
pip install -r crawler/requirements.txt
python -m playwright install chromium
python crawler/crawl.py --limit 20      # smoke test
python crawler/crawl.py                 # full run
```

Useful flags: `--only mackenzie tdam`, `--static-only`, `--limit N`.

### 2. Serve the site locally

```bash
python -m http.server 8080 --directory web
```

### 3. Deploy the site (Cloudflare Pages)

Connect the repo in the Cloudflare dashboard, set **build output directory** to
`web` and leave the build command empty. That is the whole deployment.

### 4. Deploy the PDF proxy (needed only for ZIP bundling)

```bash
cd worker && npx wrangler deploy
```

Paste the resulting `*.workers.dev` URL into `web/config.js` as `PDF_PROXY`.
Without it the site still works — per-statement links and "Open in tabs" go
straight to the manager's site — but ZIP bundling is unavailable, because
browsers block cross-origin reads of PDFs that don't send CORS headers.

### 5. Turn on the nightly crawl

Actions are enabled on push. The workflow commits `web/data/index.json`, which
triggers a Pages rebuild.

---

## Adding a fund family

Add a block to `crawler/sources.yaml`. No code changes.

```yaml
  - id: newfamily
    name: New Family Investments
    country: CA
    mode: static          # or: browser
    fye: "12-31"          # or "varies"
    hub: https://example.com/tax/pfic
    link_include: ['(?i)pfic.*\.pdf']
```

Then add the PDF host to `ALLOWED_HOSTS` in `worker/worker.js` so the proxy will
fetch it. Test with `python crawler/crawl.py --only newfamily --limit 5`.

---

## How a document gets into the index

Link text lies, filenames lie, and URL patterns drift between years — Mawer and
TD both moved their paths mid-catalogue. So a PDF is admitted only after its
**text** has been checked:

- scores against markers of a real statement (`qualified electing fund`,
  `1.1295-1`, `ordinary earnings`, `net capital gain`, `Form 8621`),
- loses points for FAQ / sample / specimen language,
- needs a score of 3+,
- has its **period end read out of the document**, falling back to the family's
  declared fiscal year end only when the text is unreadable.

Content hashes are stored, so a manager reissuing a corrected statement shows up
as a change rather than passing silently.

---

## Crawler etiquette

- `robots.txt` is honoured; a rule naming `QEFLookupBot` or `*` will stop it.
- One request per host per second.
- Every request carries an `X-Crawler` header naming the project.
- Statements are fetched, verified, and linked — not mirrored.

One thing to know: several hosts (Mackenzie, Global X, Sun Life) sit behind a
WAF that rejects Python's TLS fingerprint regardless of headers, while serving
the identical page to a browser and serving their PDFs to anyone. Rather than
spoof a TLS fingerprint, the crawler escalates to real headless Chromium for the
hub page — one request per family per run. Mackenzie's `robots.txt` explicitly
allows crawling with no crawl-delay, so the block is a crude heuristic rather
than a stated policy. If a manager does state one, add them to a skip list.

---

## Known gaps that affect the workflow

**Fund codes are missing for mutual funds.** Only ~12% of indexed funds carry a
searchable code, and the split is not random:

| Product type | Code coverage | Why |
|---|---|---|
| ETFs (iShares, Vanguard, Mackenzie, Fidelity) | ~100% | Exchange ticker appears in the URL and document |
| Mutual funds (RBC, TD, Renaissance, iA Clarington, CIBC, most of CI) | ~0% | Identified by FundSERV codes (RBF556, TDB900), which client statements show but PFIC statements do not |

Practical effect: dropping in a client statement matches on **fund name**, which
works because most statements print the name alongside the code — but searching
by `RBF556` alone will find nothing. Closing this needs a FundSERV-code-to-fund
mapping from a separate source; it cannot be extracted from the statements.

**Global X publishes one combined table** covering all its ETFs rather than one
document per fund, so it does not fit the one-document-one-fund model and
currently indexes as a single entry. Its `robots.txt` also requests a 30-second
crawl delay, which the crawler honours, making it a deliberately slow family.

**Four families yield nothing yet** — Manulife, Purpose, Sun Life and PIMCO
return their hub but no documents, because their lists are built by JavaScript
in ways the generic expansion does not reach. BMO times out. Each needs its own
selector work.

## Limitations, stated plainly

- **Index coverage is not the same as reality.** A fund absent from the index may
  simply not be crawled yet. Absence is not evidence that no statement exists —
  only the **Not offered** tab carries that meaning.
- **No figure extraction.** The tool finds and fetches documents. Ordinary
  earnings and net capital gain per share are not parsed, and no Form 8621
  worksheet is produced, by design.
- **Fidelity and Global X report per unit *per day*** — unit-days must be
  computed when filing. The tool does not do this.
- **Personalised statements can't be crawled.** Fidelity issues these on request
  for non-ETF series; several managers do the same. The register flags it.
- **Scanned client PDFs won't parse.** The page will say so rather than upload
  the file somewhere to OCR it.
- **Always confirm** the statement matches the fund, series and period before
  filing. A match from a dropped document is a lead, not a determination.
