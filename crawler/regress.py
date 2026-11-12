#!/usr/bin/env python3
"""Parser regression check against real documents.

Before a change to the name / period / verification parsers ships, run this.
It takes a sample of statements from the published index, downloads each one
again, and reports every document whose fund name, period or verdict would now
come out differently. Differences are not necessarily wrong - a fix is supposed
to change something - but each one should be a change you meant to make.

    python crawler/regress.py              # 6 documents per family
    python crawler/regress.py --per 12 --only mackenzie ci-gam

Makes one polite request per sampled document (same throttle, robots.txt
handling and identification as the crawler). Writes nothing.
"""
import argparse
import io
import json
import logging
import sys
from collections import defaultdict

import re

import yaml

import crawl

logging.getLogger("pypdf").setLevel(logging.ERROR)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=6, help="documents per family")
    ap.add_argument("--only", nargs="*", help="family ids")
    ap.add_argument("--index", default=str(crawl.OUT))
    args = ap.parse_args()
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    index = json.loads(open(args.index, encoding="utf-8").read())
    sources = yaml.safe_load((crawl.ROOT / "sources.yaml").read_text(encoding="utf-8"))
    fixes = {f["id"]: f.get("name_fix") or [] for f in sources["families"]}
    by_family = defaultdict(list)
    for fund in index["funds"]:
        for s in fund["statements"]:
            if s.get("fmt", "pdf") == "pdf" and not s.get("part"):
                by_family[fund["family"]].append((fund, s))

    changed = checked = unreachable = 0
    for fam, items in sorted(by_family.items()):
        if args.only and fam not in args.only:
            continue
        for fund, s in crawl._spread(items, args.per):
            url = s["url"]
            status, blob, _ = crawl.fetch(url, binary=True)
            if not (status == 200 and crawl._is_pdf(blob)):
                status, blob, _ = crawl.curl_fetch(url, binary=True)
            if not (status == 200 and crawl._is_pdf(blob)):
                unreachable += 1
                print(f"[{fam}] unreachable ({status}): {url}", file=out)
                continue
            checked += 1
            text = crawl.pdf_text(blob)
            ok, _score = crawl.verify_statement(text)
            name = crawl.parse_fund_name(text)
            for pattern, replacement in fixes.get(fam, []):
                if name:
                    name = re.sub(pattern, replacement, name)
            period = crawl.parse_period(text)
            new_name = crawl.tidy_name(name) if name else None
            old_name = s.get("name") or fund["name"]
            diffs = []
            if not ok:
                diffs.append("NO LONGER VERIFIES")
            if crawl.slug(new_name or "") != crawl.slug(old_name):
                diffs.append(f"name: {old_name!r} -> {new_name!r}")
            elif new_name != old_name:
                diffs.append(f"name (display only): {old_name!r} -> {new_name!r}")
            if period != s.get("period_end"):
                diffs.append(f"period: {s.get('period_end')} -> {period}")
            if diffs:
                changed += 1
                print(f"[{fam}] {url.rsplit('/', 1)[-1]}", file=out)
                for d in diffs:
                    print(f"      {d}", file=out)
    print(f"\n{checked} document(s) checked, {changed} differ, {unreachable} unreachable",
          file=out)
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
