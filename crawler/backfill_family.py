#!/usr/bin/env python3
"""One-time backfill: stamp `family` onto manifest entries written before the
crawler started recording it. Safe to re-run; only fills what is missing.

    python backfill_family.py [--dry-run]
"""
import argparse
import json
import urllib.parse as urlparse

import crawl

# Host (or host substring) -> family id. Kept here rather than in sources.yaml
# because it is throwaway migration data, not configuration.
HOST_FAMILY = {
    "mackenzieinvestments.com": "mackenzie",
    "td.com": "tdam",
    "vanguard.ca": "vanguard-ca",
    "fund-docs.vanguard.com": "vanguard-ca",
    "russellinvestments.com": "russell-ca",
    "cifinancial.com": "ci-gam",
    "mawer.com": "mawer",
    "azurefd.net": "mawer",
    "cibc.com": "cibc",
    "blackrock.com": "blackrock-ishares-ca",
    "capitalgroup.com": "capitalgroup-ca",
    "fidelity.ca": "fidelity-ca",
    "iaclarington.com": "ia-clarington",
    "renaissanceinvestments.ca": "renaissance",
    "rbcgam.com": "rbc-gam",
    "bmogam.com": "bmo-gam",
    "bmogamhub.com": "bmo-gam",
    "manulifeim.com": "manulife-im",
    "globalx.ca": "globalx-ca",
    "purposeinvest.com": "purpose",
    "sunlifeglobalinvestments.com": "sunlife-gi",
    "pimco.com": "pimco-ca",
    "dimensional.com": "dimensional-ca",
}


def family_for(url: str) -> str | None:
    host = urlparse.urlsplit(url).netloc.lower()
    for needle, fid in HOST_FAMILY.items():
        if needle in host:
            return fid
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest = crawl.load_manifest()
    filled, unknown = {}, set()
    for url, v in manifest.items():
        if v.get("family"):
            continue
        fid = family_for(url)
        if fid:
            filled[fid] = filled.get(fid, 0) + 1
            if not args.dry_run:
                v["family"] = fid
        else:
            unknown.add(urlparse.urlsplit(url).netloc)

    for fid, n in sorted(filled.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>6}  {fid}")
    if unknown:
        print(f"\n  unmapped hosts: {sorted(unknown)}")
    print(f"\n{sum(filled.values())} entries {'would be' if args.dry_run else ''} stamped")

    if not args.dry_run:
        crawl.save_manifest(manifest)
        print("manifest saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
