#!/usr/bin/env python3
"""Diagnose why a family yields no verified statements.

    python debug_family.py fidelity-ca
    python debug_family.py blackrock-ishares-ca --show-text
"""
import argparse
import re
import sys

import yaml

import crawl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("family")
    ap.add_argument("--show-text", action="store_true")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    cfg = yaml.safe_load((crawl.ROOT / "sources.yaml").read_text(encoding="utf-8"))
    fam = next((f for f in cfg["families"] if f["id"] == args.family), None)
    if not fam:
        print(f"no such family: {args.family}")
        return 1

    print(f"hub: {fam['hub']}")
    status, html, _ = crawl.fetch(fam["hub"])
    if status in (403, 429) or not html:
        print(f"  plain HTTP -> {status}; escalating to browser")
        html = crawl.render(fam["hub"])
        status = 200 if html else status
    if not html:
        print(f"  FAILED: {status}")
        return 1
    print(f"  status={status} bytes={len(html)}")

    all_pdfs = sorted({m.group(1) for m in
                       re.finditer(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I)})
    print(f"  raw .pdf hrefs on page: {len(all_pdfs)}")
    for u in all_pdfs[:8]:
        print(f"    {u[:120]}")

    inc = fam.get("link_include", cfg["defaults"]["link_include"])
    exc = fam.get("link_exclude", cfg["defaults"]["link_exclude"])
    print(f"\n  include={inc}\n  exclude={exc}")
    links = crawl.collect_links(html, fam["hub"], inc, exc)
    print(f"  after filtering: {len(links)}")
    for u in links[: args.n]:
        print(f"    {u[:120]}")

    print("\n  --- verification on first few ---")
    for u in links[: args.n]:
        st, blob, _ = crawl.fetch(u, binary=True)
        if not isinstance(st, int) or st != 200 or not blob:
            print(f"    {st} <- {u[:100]}")
            continue
        if not blob.startswith(b"%PDF"):
            print(f"    NOT-A-PDF ({blob[:24]!r}) <- {u[:90]}")
            continue
        text = crawl.pdf_text(blob)
        ok, score = crawl.verify_statement(text)
        print(f"    ok={ok} score={score} chars={len(text)} "
              f"period={crawl.parse_period(text)} name={str(crawl.parse_fund_name(text))[:44]}")
        print(f"      {u[:110]}")
        if args.show_text:
            print("      " + re.sub(r"\s+", " ", text[:500]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
