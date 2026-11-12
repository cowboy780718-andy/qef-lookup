#!/usr/bin/env python3
"""Start the local preview of the site:  python tools/dev.py

Runs tools/dev_server.mjs with whatever JavaScript runtime is to hand: Node if
it is installed, otherwise the copy that ships inside the Playwright package
the crawler already depends on. So nothing extra needs installing.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_node():
    found = shutil.which("node")
    if found:
        return found
    try:
        import playwright
    except ImportError:
        return None
    driver = Path(playwright.__file__).parent / "driver"
    for name in ("node.exe", "node"):
        if (driver / name).exists():
            return str(driver / name)
    return None


def main():
    node = find_node()
    if not node:
        print("No JavaScript runtime found. Run:  pip install -r crawler/requirements.txt",
              file=sys.stderr)
        return 1
    extra = sys.argv[1:] or [str(ROOT / "tools" / "dev_server.mjs")]
    return subprocess.call([node, *extra], cwd=ROOT, env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main())
