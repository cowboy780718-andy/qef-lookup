"""Put a wording change on the live site without waiting for the nightly crawl.

The notes a reader sees - why a manager has nothing to list, what to do about
it, the caveat on its funds - are written in crawler/sources.yaml and
crawler/negatives.yaml, and reach the site through web/data/index.json, which
only the crawler rebuilds. This copies just those fields into the published
index, leaving every fund and statement exactly as the crawler left them.

    python tools/sync_notes.py

Run it on a freshly pulled index.json, then commit the result.
"""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "data" / "index.json"
FIELDS = ("name", "fye", "hub", "access", "guidance", "action", "caveat", "covers")
NOT_CRAWLED = ("manual", "request", "none")


def main() -> int:
    raw = INDEX.read_text(encoding="utf-8")
    index = json.loads(raw)
    if json.dumps(index, indent=1, default=str) != raw:
        print("index.json is not in the crawler's format; not touching it", file=sys.stderr)
        return 1
    load = lambda name: yaml.safe_load((ROOT / "crawler" / name).read_text(encoding="utf-8"))
    src = {f["id"]: f for f in load("sources.yaml")["families"]}

    families = []
    for fam in index["families"]:
        s = src.get(fam["id"])
        if s is None:
            families.append(fam)
            continue
        out = {}
        for key, value in fam.items():
            if key in ("action", "page"):
                continue
            out[key] = s.get(key) if key in FIELDS else value
            if key == "hub":
                out["page"] = s.get("page")
            if key == "guidance":
                out["action"] = s.get("action")
        families.append(out)
    index["families"] = families
    index["negatives"] = load("negatives.yaml")["negatives"]
    for h in index["health"]:
        s = src.get(h["id"], {})
        if s.get("access") in NOT_CRAWLED:
            h["message"] = (s.get("guidance") or "").strip()

    out = json.dumps(index, indent=1, default=str)
    if out == raw:
        print("nothing to change")
        return 0
    INDEX.write_text(out, encoding="utf-8", newline="")
    print("index.json updated - notes only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
