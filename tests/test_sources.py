"""The registry files are edited by hand; a typo in one stops a whole family.

These checks catch the mistakes that are easy to make and hard to notice: a
pattern that does not compile, a family id used twice, a manager marked
"fetch by hand" with no explanation to show the reader.
"""
import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawler"))
import crawl  # noqa: E402


def load(name):
    return yaml.safe_load((ROOT / "crawler" / name).read_text(encoding="utf-8"))


class SourceRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load("sources.yaml")
        cls.families = cls.cfg["families"]
        cls.ids = [f["id"] for f in cls.families]

    def test_ids_are_unique_and_simple(self):
        self.assertEqual(len(self.ids), len(set(self.ids)))
        for fid in self.ids:
            self.assertRegex(fid, r"^[a-z0-9-]+$")

    def test_every_family_has_the_basics(self):
        for f in self.families:
            for key in ("id", "name", "hub", "fye"):
                self.assertIn(key, f, f.get("id"))
            self.assertTrue(f["hub"].startswith("https://"), f["id"])
            self.assertRegex(str(f["fye"]), r"^(\d\d-\d\d|varies)$", f["id"])
            self.assertIn(f.get("mode", "static"), ("static", "browser"), f["id"])
            self.assertIn(f.get("access"), (None, *crawl.NOT_CRAWLED), f["id"])

    def test_every_pattern_compiles(self):
        def patterns(f):
            yield from f.get("link_include") or []
            yield from f.get("link_exclude") or []
            for key in ("name_pattern", "statement_page_pattern"):
                if f.get(key):
                    yield f[key]
            if f.get("follow"):
                yield f["follow"]["page_pattern"]
            if f.get("derive"):
                yield f["derive"]["pair_pattern"]
            if f.get("link_section"):
                yield f["link_section"]["start"]
                if f["link_section"].get("end"):
                    yield f["link_section"]["end"]
            if f.get("json_api", {}).get("token_pattern"):
                yield f["json_api"]["token_pattern"]
            for pattern, _replacement in f.get("name_fix") or []:
                yield pattern
        for f in [self.cfg["defaults"], *self.families]:
            for p in patterns(f):
                try:
                    re.compile(p)
                except re.error as exc:
                    self.fail(f"{f.get('id', 'defaults')}: bad pattern {p!r}: {exc}")

    def test_a_family_we_cannot_collect_explains_itself(self):
        # The guidance is what the site shows instead of results. Without it
        # the reader gets "nothing found", which reads as "no statement exists".
        for f in self.families:
            if f.get("access") in crawl.NOT_CRAWLED:
                self.assertGreater(len((f.get("guidance") or "").strip()), 80, f["id"])

    def test_guidance_never_claims_a_manager_does_not_issue_statements(self):
        # We record what was found on a public website on a date. Whether a
        # manager would issue a statement on request is not ours to assert.
        banned = re.compile(r"does\s+not\s+issue|do\s+not\s+issue|refuses?\s+to|"
                            r"will\s+not\s+(?:issue|provide)", re.I)
        for f in self.families:
            for key in ("guidance",):
                self.assertIsNone(banned.search(f.get(key) or ""), f"{f['id']}.{key}")
        for n in load("negatives.yaml")["negatives"]:
            text = f"{n.get('scope', '')} {n.get('note', '')}"
            # CIBC's entry quotes the manager's own statement; that one is theirs.
            if n.get("family") == "cibc":
                continue
            self.assertIsNone(banned.search(text), n.get("scope"))

    def test_url_templates_expand(self):
        for f in self.families:
            for tmpl in f.get("year_urls") or []:
                self.assertIn("2025", tmpl.format(year=2025), f["id"])
            for tmpl in (f.get("derive") or {}).get("url_templates") or []:
                url = tmpl.format(year=2025, ticker="XIU", ticker_lower="xiu")
                self.assertTrue(url.startswith("https://"), f["id"])
            if f.get("split"):
                self.assertIn(f["split"].get("by"), ("pages", "rows"), f["id"])
                if f["split"]["by"] == "rows":
                    self.assertTrue(f["split"].get("name_prefix"), f["id"])

    def test_negatives_point_at_real_families(self):
        for n in load("negatives.yaml")["negatives"]:
            self.assertIn(n["family"], self.ids + ["*"], n.get("scope"))
            for key in ("scope", "finding", "evidence", "verified", "verified_by"):
                self.assertIn(key, n, n.get("scope"))

    def test_fund_code_prefixes_point_at_real_families(self):
        for p in load("fundcodes.yaml").get("prefixes", []):
            self.assertRegex(p["prefix"], r"^[A-Z]{3}$")
            if p.get("family"):
                self.assertIn(p["family"], self.ids, p["prefix"])
            if p.get("resolve"):
                self.assertIn("{code}", p["resolve"], p["prefix"])

    def test_the_proxy_allows_only_what_the_index_links_to(self):
        # hosts.json is generated; if someone hand-edits a host into it that
        # no statement uses, the proxy would serve it. Keep the two in step.
        import json
        import urllib.parse as urlparse
        index = json.loads((ROOT / "web" / "data" / "index.json").read_text(encoding="utf-8"))
        hosts_file = ROOT / "web" / "data" / "hosts.json"
        if not hosts_file.exists():
            self.skipTest("hosts.json not generated yet")
        hosts = set(json.loads(hosts_file.read_text(encoding="utf-8"))["hosts"])
        linked = {urlparse.urlsplit(s["url"]).netloc.lower()
                  for f in index["funds"] for s in f["statements"]
                  if s.get("fmt", "pdf") == "pdf"}
        self.assertEqual(hosts - linked, set(), "hosts.json lists hosts no statement uses")

    def test_crawler_reads_the_registry_it_ships_with(self):
        self.assertTrue((crawl.ROOT / "sources.yaml").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
