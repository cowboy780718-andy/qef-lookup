# QEF Statement Lookup

**Live:** https://qef-lookup.cowboy780718.workers.dev

Find and bulk-download PFIC Annual Information Statements (QEF statements) for
Form 8621 work, instead of navigating forty fund company websites by hand.

Runs entirely on free infrastructure. No server to look after, no database, no
monthly bill.

---

## What it does

1. **Identify** - search by fund name, ticker or fund code (several at once,
   separated by commas), or drop a client PDF (brokerage statement, T3) and let
   the page pick the fund names out of it. The PDF is read inside the browser
   and never uploaded.
2. **Locate** - a pre-built index of every statement the crawler has found and
   verified, so lookups are instant rather than a live search.
3. **Select a period** - filter by the *fund's* tax year, or a custom period-end
   range. The fund's year, not the calendar year: BMO and RBC run to 30 June,
   CI to 31 March, NEI to 30 September, Hamilton and Global X to 30 April.
4. **Bulk download** - multi-select across funds and years, get one ZIP with
   normalised filenames and a manifest CSV for the working paper file.
5. **Stay working** - a nightly crawl re-checks every source, and opens a GitHub
   issue when one needs attention.

### What each manager is, to this tool

Every manager that has been looked at is in the *Fund family* list, whatever was
found - a manager missing from the list looks like one nobody thought of.

| Kind | Meaning | Count |
|---|---|---:|
| Collected | Statements are fetched, opened and verified. They can go in a ZIP. | 27 |
| Links only | The manager's own links are listed, but the documents are never fetched, because its `robots.txt` asks automated tools not to. Shown as "link only - not checked". | 4 |
| On request | The manager says it issues statements on request and posts none (Sun Life). | 1 |
| Fetch by hand | Statements are posted but cannot be collected (PIMCO). | 1 |
| Nothing located | No statement could be found on the manager's public website on the date given. | 8 |

Wording matters for the last group. The tool records what was found on a public
website on a date. It never says a manager "does not issue" statements - one may
be available on request - and a test (`tests/test_sources.py`) fails the build
if that phrasing creeps into the registry.

---

## How it fits together

```
GitHub Actions (nightly)  ->  crawler/crawl.py  ->  web/data/index.json  ->  git commit
                                                    web/data/hosts.json        |
                                                                               v
                                     Cloudflare (rebuilds the site on every commit)
                                       |-- files under web/   the page, the index
                                       '-- worker/site.js     /pdf?u=...  (ZIP downloads)
```

| Piece | Where it runs | How it gets deployed |
|---|---|---|
| Crawler | GitHub Actions, nightly | Nothing to deploy - it runs from the repo |
| Site | Cloudflare Worker `qef-lookup` | Automatically, on every push to `master` |
| Download helper (`/pdf`) | Part of the same Worker | With the site |

The loop: crawler runs -> commits a refreshed index -> the push triggers a
Cloudflare build -> the live site updates. Nothing is uploaded by hand.

**Client documents never leave your machine.** PDFs dropped on the page are
parsed in the browser with PDF.js. There is no upload endpoint.

**Nothing is rehosted.** The index stores links; documents are served from each
manager's own site. `/pdf` fetches a statement on demand so the browser can put
it in a ZIP - browsers will not let a page read another site's PDF directly -
and it is not an archive.

**The `/pdf` helper is not an open proxy.** It only fetches from hosts listed in
`web/data/hosts.json`, which the crawler regenerates from the index every night;
the reply must really be a PDF; and other websites cannot use it.

> `worker/worker.js` + `worker/wrangler.toml` are the *old* stand-alone proxy
> (`qef-pdf-proxy`). Its list of allowed hosts was kept by hand and went stale,
> which is why ZIPs failed for six managers. The page now only falls back to it
> if `/pdf` is missing. Once `/pdf` is seen working on the live site, the old
> Worker can be deleted from Cloudflare.
>
> **Only `wrangler.toml` at the repo root belongs to the site.** If Cloudflare
> ever offers a pull request renaming it to `qef-pdf-proxy`, decline it.

---

## Working with the nightly crawler

`web/data/index.json` and `web/data/hosts.json` are generated, and the nightly
job commits them. If you also change them locally you will get a merge conflict
on a 6 MB file.

**Always Pull before you Push.** In GitHub Desktop, if the top bar shows a
number next to a down arrow, click that first.

If a conflict happens anyway, do not hand-merge it. Take the crawler's copy:

```bash
git checkout --theirs web/data/index.json web/data/hosts.json
git add web/data/index.json web/data/hosts.json
```

### Running things by hand (GitHub -> Actions -> "Crawl QEF statements" -> Run workflow)

| Field | Use |
|---|---|
| `only` | Crawl just these managers, e.g. `mackenzie tdam` (ids are in `crawler/sources.yaml`) |
| `refresh` | Re-read every listed document, ignoring the cache. Use with `only` after changing a manager's settings |
| `static_only` | Skip the managers that need a headless browser |
| `since_year` | Oldest fund tax year to index (the nightly run uses 2021) |

---

## Running it on your own computer

```bash
pip install -r crawler/requirements.txt
python -m playwright install chromium
```

```bash
python crawler/crawl.py --since-year 2021 --only mawer --limit 5   # try one manager
python -m unittest discover -s tests                                # crawler tests
python tools/dev.py --test tests/test_lib.mjs tests/test_worker.mjs # site and /pdf tests
python tools/dev.py                                                 # the site, at http://localhost:8099
```

`tools/dev.py` starts the site exactly as Cloudflare serves it - static files
plus `worker/site.js` for `/pdf` - so ZIP downloads can be tried locally. It
uses Node if that is installed, or otherwise the copy bundled with Playwright,
so nothing extra needs installing. Given arguments, it passes them to that same
runtime, which is how the second line above runs the JavaScript tests.

Before changing how names, periods or verification work, run

```bash
python crawler/regress.py
```

It re-downloads a sample of real statements from the published index and shows
every one whose fund name, period or verdict would now come out differently.

---

## Adding a manager

Add a block to `crawler/sources.yaml`. No code changes for the common cases.

```yaml
  - id: newfamily
    name: New Family Investments
    country: CA
    mode: static          # or: browser (the page builds its list with JavaScript)
    fye: "12-31"          # fund year end, or "varies"
    hub: https://example.com/tax/pfic
    link_include: ['(?i)pfic.*\.pdf']
```

Then `python crawler/crawl.py --only newfamily --limit 5`. The download helper
picks the new host up by itself on the next nightly run.

What a block can say, each added for a real manager:

| Key | For | Example |
|---|---|---|
| `link_include` / `link_exclude` | Which links on the page are statements | most |
| `extra_hubs` | A second page to read with the same filters | NBI's French page, Dynamic's archive |
| `follow` | One page per fund, each linking its own statement | Manulife, Purpose, Evolve, Harvest |
| `link_section` | Take every PDF under one heading, whatever it is called | Mawer (files named by hash) |
| `url_scan` | Addresses that are in the page's data but not in a link | Starlight |
| `json_api` | The page loads its list from an endpoint | Dimensional, Franklin Templeton |
| `derive` | Statements exist at a predictable address but are not linked | BlackRock |
| `year_urls` | One document per year at a fixed address | Desjardins |
| `doc_type: html_table` | The statement is a table on the page, not a file | Hamilton |
| `split` | One document covers many funds | Guardian (by pages), Global X (by rows) |
| `listing_only` | List the manager's links without fetching them | Capital Group, iA Clarington, Lysander, Franklin Templeton |
| `access: request / manual / none` | Not crawled; `guidance` says why in one sentence, `action` says what to do in one more | Sun Life, PIMCO, AGF |
| `page` | Where to send a person, when it is not the page the crawler starts from | Hamilton, Evolve |
| `caveat` | A note shown on every fund of this manager | "amounts are per unit PER DAY" |
| `covers` | Fund names to match for a manager with nothing to list | Sun Life's 17 funds |
| `name_fix`, `delay` | Repair a broken text layer; pace a rate-limiting host | Sprott; Harvest |

---

## How a document gets into the index

Link text lies, filenames lie, and URL patterns drift between years. So a PDF is
admitted only after its **text** has been checked:

- it scores against markers of a real statement (`qualified electing fund`,
  `1.1295-1`, `ordinary earnings`, `net capital gain`, `Form 8621`),
- loses points for FAQ / sample / specimen language, and needs a score of 3+,
- has its **period end read out of the document** - never assumed from the
  manager's usual year end.

"Links only" entries are the exception, and are labelled as such everywhere:
nothing was opened, so nothing was verified.

A statement that has been verified is only dropped from the index after its
address has positively gone (404) on three nights running. One failed request -
a timeout, a rate limit - never removes anything.

---

## Crawler etiquette

- `robots.txt` is honoured, read by the standard longest-match rule; a rule
  naming `QEFLookupBot` or `*` will stop it. Where a manager's file forbids its
  documents but not its listing page, only the listing page is read.
- A published `Crawl-delay` is respected. Otherwise about three requests a
  second per host, slowing automatically when a host answers 429.
- A known document is looked at again only every 10 days (30 for closed tax
  years), on a night of its own, with a conditional request - so most nights most
  managers receive a handful of requests, not their whole catalogue.
- If a host refuses twelve requests in a row, it is left alone for the night.
- Every request carries an `X-Crawler` header naming the project and its address.
- Bot-protection is not worked around. PIMCO's tax centre withholds its content
  from automated browsers on purpose, so PIMCO is "fetch by hand".

---

## Limitations, stated plainly

- **Index coverage is not the same as reality.** A fund absent from the index may
  simply not be covered. Absence is not evidence that no statement exists - only
  a manager's own note, shown above its results, carries that meaning.
- **No figure extraction.** The tool finds and fetches documents. Ordinary
  earnings and net capital gain are not parsed out of PDFs, and no Form 8621
  worksheet is produced, by design. (The one exception is forced: Hamilton
  publishes its statements as a web table, so the table row is transcribed
  verbatim, because there is no file.)
- **Several managers report per unit *per day*** (Fidelity, Hamilton, Global X,
  Starlight, Mackenzie) - unit-days must be computed. The tool does not do this.
- **Some managers post only the latest year** at a fixed address (Dimensional,
  NEI, Capital Group, Harvest). Earlier years appear here only from the date the
  manager was first crawled.
- **Fund codes are mostly missing for mutual funds.** Client statements show
  FundSERV codes (RBF556, TDB900); PFIC statements rarely print them. RBC and
  National Bank put the code in the file name, so those are searchable. For the
  rest, searching a code explains whose it is and where to resolve it to a name.
- **Personalised statements can't be crawled** (Fidelity's non-ETF series, and
  others on request).
- **Scanned client PDFs won't parse.** The page says so rather than upload the
  file somewhere to OCR it.
- **Always confirm** the statement matches the fund, series and period before
  using it. A match from a dropped document is a lead, not a determination.
