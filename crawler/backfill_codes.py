#!/usr/bin/env python3
"""Re-derive fund codes from document URLs already in the cache.

Several managers name their statement files by FundSERV code - RBC's
/pfic/2025/rbf1664_e.pdf, National Bank's MFIN4001-pfic-2025.pdf - so the code
a client statement shows can be recovered from the URL with no network access
at all. This walks the existing cache and fills in any codes the URL patterns
can supply, so adding a new pattern does not mean re-downloading a catalogue.

    python backfill_codes.py --dry-run
    python backfill_codes.py
"""
import argparse
import collections

import crawl


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest = crawl.load_manifest()
    gained = collections.Counter()
    examples: dict[str, tuple] = {}

    for url, v in manifest.items():
        if not v.get("verified") or v.get("miss"):
            continue
        before = list(v.get("tickers") or [])
        # URL-only derivation: pass empty text so nothing is invented from
        # document content that was not already recorded.
        found = crawl.parse_tickers("", v.get("url") or url)
        merged = sorted(set(before) | set(found))
        if merged != before:
            fam = v.get("family", "unknown")
            gained[fam] += 1
            examples.setdefault(fam, (v.get("fund_name"), merged, (v.get("url") or url)))
            if not args.dry_run:
                v["tickers"] = merged

    if not gained:
        print("No new codes derivable from cached URLs.")
        return 0

    for fam, n in gained.most_common():
        name, codes, u = examples[fam]
        print(f"  {fam:<24} +{n:<5} e.g. {codes} <- {u.rsplit('/', 1)[-1]}")
    print(f"\n{sum(gained.values())} statement(s) {'would gain' if args.dry_run else 'gained'} codes")

    if not args.dry_run:
        crawl.save_manifest(manifest)
        print("manifest saved - rerun crawl.py --rebuild-only to publish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
