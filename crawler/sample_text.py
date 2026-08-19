#!/usr/bin/env python3
"""Cache the extracted text of a few statements per family, so fund-name
parsing can be iterated on offline instead of re-downloading every attempt.

    python sample_text.py            # build/refresh the sample
    python sample_text.py --n 10
"""
import argparse
import json
import re
from pathlib import Path

import crawl

OUT = crawl.CACHE / "textsample"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6, help="documents per family")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = crawl.load_manifest()

    by_family: dict[str, list[str]] = {}
    for url, v in manifest.items():
        if v.get("verified") and not v.get("miss"):
            by_family.setdefault(v.get("family", "unknown"), []).append(url)

    total = 0
    for fam, urls in sorted(by_family.items()):
        # Spread the sample across the family rather than taking the head,
        # so different document layouts and years are represented.
        step = max(1, len(urls) // args.n)
        picks = urls[::step][: args.n]
        for i, url in enumerate(picks):
            dest = OUT / f"{fam}__{i}.txt"
            if dest.exists():
                continue
            st, blob, _ = crawl.fetch(url, binary=True)
            if not (isinstance(st, int) and st == 200 and blob and blob.startswith(b"%PDF")):
                continue
            text = crawl.pdf_text(blob, pages=3)
            if not text.strip():
                continue
            dest.write_text(url + "\n" + text, encoding="utf-8")
            total += 1
        print(f"  {fam:<24} {len(picks)} sampled")

    print(f"\n{total} new document(s) cached in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
