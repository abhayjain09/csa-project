"""Focused regression tests for identity and per-query mapping safety."""

import ast
import json
import re
import sys
import types
import unittest
from pathlib import Path


AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(AGENT_DIR))

if "boto3" not in sys.modules:
    boto3_stub = types.ModuleType("boto3")
    boto3_stub.client = lambda *args, **kwargs: None
    sys.modules["boto3"] = boto3_stub

import registry_tier  # noqa: E402


def _seed_sec_cache():
    registry_tier._EDGAR_TICKER_CACHE.clear()
    registry_tier._EDGAR_TICKER_CACHE.update({
        "EW": "0001099800",
        "name::edwards lifesciences corp": "0001099800",
        "CSCO": "0000858877",
        "name::cisco systems, inc.": "0000858877",
        "BALL": "0000009389",
        "name::ball corp": "0000009389",
    })


def _load_pairing_function():
    path = REPO_ROOT / "reportiq-ecs/app/backend/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "_pair_queries_with_results"
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_pair_queries_with_results"]


def _load_vertex_helpers():
    path = REPO_ROOT / "infra/agentcore-report/vertex_search/lambda.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"_parse_first_json_object", "_clean_identity_hint"}
    nodes = [
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"json": json}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def _load_confidence_function():
    path = REPO_ROOT / "infra/agentcore-report/agent/agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_confident"
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "MIN_SELECTION_CONFIDENCE": "high",
        "_extract_year_intent": lambda query: set(),
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_confident"]


def _load_routing_helpers():
    path = REPO_ROOT / "infra/agentcore-report/agent/agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {
        "_scope_to_official_domain",
        "_official_search_queries",
        "_discovery_route",
    }
    nodes = [
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "REQUIRE_OFFICIAL_DOMAIN_FOR_WEB": True,
        "REGISTRY_FIRST_CLASSES": set(),
        "_clean_domain": lambda value: str(value or "").lower().strip(),
        "_strip_site": lambda value: re.sub(
            r"site:\s*\S+", "", value or "", flags=re.I).strip(),
        "_query_variant_preserves_years": lambda original, variant: True,
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


class CompanyIdentityTests(unittest.TestCase):
    def setUp(self):
        _seed_sec_cache()

    def test_corporate_suffix_normalization_resolves_exact_company(self):
        result = registry_tier.enrich_company_identity({
            "name": "Edwards Lifesciences Corporation",
        })
        self.assertEqual(result["cik"], "0001099800")
        self.assertEqual(result["ticker"], "EW")
        self.assertEqual(
            result["_identity_validation"]["status"], "validated")

    def test_generic_partial_name_fails_closed(self):
        result = registry_tier.enrich_company_identity({"name": "Edwards"})
        self.assertNotIn("cik", result)
        self.assertEqual(
            result["_identity_validation"]["status"], "unresolved")

    def test_real_but_wrong_ticker_is_rejected(self):
        result = registry_tier.enrich_company_identity({
            "name": "Edwards Lifesciences Corporation",
            "ticker": "CSCO",
        })
        self.assertEqual(
            result["_identity_validation"]["status"], "unresolved")

    def test_vertex_hint_must_match_requested_name_and_sec(self):
        result = registry_tier.enrich_company_identity(
            {"name": "Edwards Lifesciences Corporation"},
            {"legal_name": "Edwards Lifesciences Corp",
             "ticker": "EW", "cik": "1099800"},
        )
        self.assertEqual(result["cik"], "0001099800")
        self.assertEqual(
            result["_identity_validation"]["status"], "validated")


class ResultMappingTests(unittest.TestCase):
    def test_later_success_is_not_assigned_to_earlier_failure(self):
        pair = _load_pairing_function()
        queries = ["Annual Report", "Tax Policy"]
        tax_document = {
            "request_id": "4:2",
            "query": "Tax Policy",
            "status": "downloaded",
            "s3_key": "company/tax.pdf",
            "report": "Tax.pdf",
        }
        results = pair(
            queries, [tax_document], [], [tax_document], chunk_index=4)
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[1]["status"], "downloaded")
        self.assertEqual(results[1]["s3_key"], "company/tax.pdf")

    def test_legacy_exact_query_match_is_allowed_without_position(self):
        pair = _load_pairing_function()
        results = pair(
            ["Annual Report", "Tax Policy"],
            [{"query": "Tax Policy", "s3_key": "company/tax.pdf"}],
            [], [], chunk_index=4,
        )
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[1]["status"], "downloaded")


class VertexIdentityContractTests(unittest.TestCase):
    def test_fenced_hint_is_parsed_and_bounded(self):
        helpers = _load_vertex_helpers()
        raw = helpers["_parse_first_json_object"](
            '```json\n{"legal_name":"Edwards Lifesciences Corp",'
            '"ticker":"ew","cik":"1099800",'
            '"official_domain":"https://www.edwards.com/about",'
            '"jurisdiction":"US"}\n```')
        hint = helpers["_clean_identity_hint"](raw)
        self.assertEqual(hint["ticker"], "EW")
        self.assertEqual(hint["cik"], "0001099800")
        self.assertEqual(hint["official_domain"], "edwards.com")
        self.assertEqual(hint["jurisdiction"], "us")

    def test_invalid_cik_is_removed(self):
        helpers = _load_vertex_helpers()
        hint = helpers["_clean_identity_hint"]({
            "legal_name": "Example",
            "ticker": "EX",
            "cik": "not-a-cik",
            "official_domain": "example.com",
            "jurisdiction": "us",
        })
        self.assertIsNone(hint["cik"])


class SelectionConfidenceTests(unittest.TestCase):
    def test_medium_web_selection_is_rejected(self):
        confident = _load_confidence_function()
        self.assertFalse(confident({
            "selected_url": "https://example.com/report.pdf",
            "topic_match": True,
            "company_match": True,
            "year_match": True,
            "confidence": "medium",
        }))

    def test_high_web_selection_is_accepted(self):
        confident = _load_confidence_function()
        self.assertTrue(confident({
            "selected_url": "https://example.com/report.pdf",
            "topic_match": True,
            "company_match": True,
            "year_match": True,
            "confidence": "high",
        }))


class DiscoveryRoutingTests(unittest.TestCase):
    def test_annual_report_uses_registry_after_official_company_path(self):
        route = _load_routing_helpers()["_discovery_route"](
            "annual report", True)
        self.assertEqual(route, [
            "direct_search",
            "official_crawl",
            "deep_crawl",
            "browser",
            "registry",
        ])

    def test_non_deterministic_registry_is_last(self):
        route = _load_routing_helpers()["_discovery_route"](
            "sustainability report", True)
        self.assertEqual(route, [
            "direct_search",
            "official_crawl",
            "deep_crawl",
            "browser",
            "registry",
        ])

    def test_proxy_statement_uses_sec_only_after_browser(self):
        route = _load_routing_helpers()["_discovery_route"](
            "proxy statement", True)
        self.assertEqual(route[-2:], ["browser", "registry"])

    def test_direct_queries_are_officially_scoped_and_use_ticker(self):
        helpers = _load_routing_helpers()
        queries = helpers["_official_search_queries"](
            "Acme annual report 2025 site:wrong.example",
            {
                "domain": "acme.com",
                "ticker": "ACME",
                "cik": "0000123456",
                "_identity_validation": {"status": "validated"},
            },
            ["Acme 10-K 2025"],
            ["Acme annual report FY2025 filetype:pdf"],
        )
        self.assertTrue(queries)
        self.assertTrue(all(q.endswith("site:acme.com") for q in queries))
        self.assertTrue(any('ticker "ACME"' in q for q in queries))
        self.assertTrue(all("wrong.example" not in q for q in queries))
        self.assertTrue(all("0000123456" not in q for q in queries))

    def test_web_discovery_fails_closed_without_official_domain(self):
        helpers = _load_routing_helpers()
        queries = helpers["_official_search_queries"](
            "Acme sustainability report 2025",
            {"domain": "", "_identity_validation": {"status": "unresolved"}},
            [], [],
        )
        self.assertEqual(queries, [])


if __name__ == "__main__":
    unittest.main()
