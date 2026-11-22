# Working on this repository

Notes for whoever picks this project up next, person or AI assistant. The
README explains how the system works; this file holds the rules and the
current state that are not obvious from the code. Keep it short: an assistant
reads all of it at the start of every session.

## Rules the owner has set

- **Free only.** Cloudflare Workers, GitHub Actions and free tiers (Resend for
  the Contact form). No paid service or domain without asking.
- **Downloads only.** Find and fetch statements; never extract figures or build
  a Form 8621 worksheet. (Hamilton's web tables are transcribed verbatim only
  because there is no file.)
- **Wording:** a manager without statements is "not available" / "could not be
  located" - never "does not issue". `tests/test_sources.py` enforces it.
- **Notes on the site are short:** one sentence of reason, one action item,
  then the link button. Tests cap the length.
- **No analytics, no cookies.** If that ever changes, reword the privacy note in
  the same change.
- **No personal contact details on the site or in this repo.** The Contact
  form's destination address and Resend key are Cloudflare secrets
  (`CONTACT_TO`, `RESEND_API_KEY`, under Settings -> *Runtime* variables).
- **Bot protection is never worked around** (PIMCO stays "fetch by hand").
  `robots.txt` is honoured.
- Every manager that has been checked stays in the Fund family list, with an
  explanation if it has nothing to download.

## Making a change

1. `git pull --rebase origin master` first - the nightly crawl commits
   `web/data/index.json` and `web/data/hosts.json` every day.
2. Run the tests: `python -m unittest discover -s tests` and
   `python tools/dev.py --test tests/test_lib.mjs tests/test_worker.mjs`.
3. Look at it: `python tools/dev.py` serves the site at http://localhost:8099
   exactly as Cloudflare does (the Contact form runs in dry-run mode there).
4. Never commit an `index.json` built by a local crawl - restore it with
   `git checkout web/data/index.json web/data/hosts.json`. A wording change in
   `crawler/sources.yaml` or `negatives.yaml` goes live without a crawl via
   `python tools/sync_notes.py`, which edits only the note fields.
5. Pushing to `master` deploys the site within a couple of minutes.

## Current state (November 2026)

- 41 managers: 31 crawled, 4 links-only, Sun Life on request, PIMCO by hand,
  8 with nothing located. About 4,855 funds and 16,000 statements, 2021 on.
- Earlier years are recovered from managers' old addresses and from the
  Internet Archive (tagged "archived copy"); that backlog is fully read.
- Dimensional's list comes back empty from GitHub's US servers some nights;
  marked `known_issue`, so it is shown but never alerted on.
- Not recoverable: BMO and Dimensional earlier years, Renaissance 2023,
  Desjardins after 2023.

## Gotchas

- GitHub run logs need a sign-in; the published `index.json` carries each
  source's `status`, `detail`, `seconds` and `archived` instead - read those.
- web.archive.org answers 429 for hours if hit in bursts. Be gentle.
- Tests must never write the real crawl cache; `Patched.setUp` stubs
  `save_manifest`.
- Cloudflare's Settings page has two "Variables and secrets" sections. The
  Worker reads only the *Runtime* one.
- Only the root `wrangler.toml` belongs to the site. Decline any Cloudflare
  pull request that renames it.
