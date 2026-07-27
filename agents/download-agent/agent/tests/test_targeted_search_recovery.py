"""Focused tests for the post-crawl Google recovery pass."""

import ast
import re
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import unquote, urlparse


AGENT_PATH = Path(__file__).resolve().parents[1] / "agent.py"


def _load_helpers():
    tree = ast.parse(AGENT_PATH.read_text(encoding="utf-8"))
    wanted_assignments = {"_RECOVERY_PATH_HINTS", "_COMPANY_LEGAL_SUFFIXES"}
    wanted_functions = {
        "_scope_to_official_domain",
        "_company_identity_names",
        "_targeted_search_queries",
        "_discovery_route",
        "_normalize_company_text",
        "_company_name_tokens",
        "_company_search_result_score",
    }
    nodes = []
    for item in tree.body:
        if (isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name)
                        and target.id in wanted_assignments
                        for target in item.targets)):
            nodes.append(item)
        elif isinstance(item, ast.FunctionDef) and item.name in wanted_functions:
            nodes.append(item)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "unicodedata": unicodedata,
        "unquote": unquote,
        "urlparse": urlparse,
        "REQUIRE_OFFICIAL_DOMAIN_FOR_WEB": True,
        "TARGETED_SEARCH_MAX_QUERIES": 3,
        "REGISTRY_FIRST_CLASSES": {"annual report", "proxy statement"},
        "_clean_domain": lambda value: str(value or "").lower().strip(),
        "_strip_site": lambda value: re.sub(
            r"site:\s*\S+", "", value or "", flags=re.I).strip(),
        "_extract_year_intent": lambda value: {
            int(year) for year in re.findall(r"\b20\d{2}\b", value or "")
        },
        "_query_variant_preserves_years": lambda original, variant: (
            {int(year) for year in re.findall(r"\b20\d{2}\b", original or "")}
            == {int(year) for year in re.findall(r"\b20\d{2}\b", variant or "")}
        ),
        "_matched_doc_classes": lambda value: [],
        "_host_matches": lambda url, domain: (
            (urlparse(url).hostname or "").removeprefix("www.")
            == str(domain or "").removeprefix("www.")
        ),
    }
    exec(compile(module, str(AGENT_PATH), "exec"), namespace)
    return namespace


class TargetedSearchRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_helpers()

    def test_recovery_queries_anchor_legal_name_year_domain_and_path(self):
        queries = self.helpers["_targeted_search_queries"](
            "Cisco sustainability report 2025 site:cisco.com",
            {
                "name": "Cisco",
                "domain": "cisco.com",
                "ticker": "CSCO",
                "_identity_validation": {
                    "official_name": "Cisco Systems, Inc.",
                },
            },
            "sustainability report",
            synonyms=["ESG report", "purpose report"],
            prior_urls=["https://cisco.com/sustainability/reports"],
        )
        self.assertEqual(len(queries), 3)
        self.assertTrue(all('"Cisco Systems, Inc."' in q for q in queries))
        self.assertTrue(all("2025" in q and q.endswith("site:cisco.com")
                            for q in queries))
        self.assertTrue(any("inurl:sustainability" in q for q in queries))
        self.assertTrue(any('"purpose report"' in q for q in queries))

    def test_company_identity_score_prefers_exact_official_company(self):
        score = self.helpers["_company_search_result_score"]
        ctx = {
            "name": "Cisco",
            "domain": "cisco.com",
            "ticker": "CSCO",
            "_identity_validation": {
                "official_name": "Cisco Systems, Inc.",
            },
        }
        exact = score({
            "title": "Cisco Systems 2025 Purpose Report",
            "snippet": "Official CSCO sustainability disclosure",
            "url": "https://cisco.com/reports/purpose-report.pdf",
        }, ctx, "cisco.com")
        different_company = score({
            "title": "Cisco Oilfield Services sustainability report",
            "snippet": "A different organization",
            "url": "https://cdn.example.net/report.pdf",
        }, ctx, "cisco.com")
        self.assertGreater(exact, different_company)
        self.assertEqual(different_company, 0)

    def test_recovery_stage_runs_after_static_crawl_before_browser(self):
        route = self.helpers["_discovery_route"](
            "sustainability report", True)
        self.assertLess(route.index("deep_crawl"), route.index("targeted_search"))
        self.assertLess(route.index("targeted_search"), route.index("browser"))


if __name__ == "__main__":
    unittest.main()
