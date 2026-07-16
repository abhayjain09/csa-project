"""Regression coverage for latest Annual/Sustainability Report selection."""

import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


# Permit importing agent.py without the container-only runtime dependencies.
_boto3 = types.ModuleType("boto3")
_boto3.client = lambda *args, **kwargs: None
_boto3.resource = lambda *args, **kwargs: None
sys.modules.setdefault("boto3", _boto3)

_botocore = types.ModuleType("botocore")
_botocore_config = types.ModuleType("botocore.config")
_botocore_config.Config = lambda *args, **kwargs: object()
sys.modules.setdefault("botocore", _botocore)
sys.modules.setdefault("botocore.config", _botocore_config)

_agentcore = types.ModuleType("bedrock_agentcore")
_agentcore_runtime = types.ModuleType("bedrock_agentcore.runtime")


class _DummyApp:
    def entrypoint(self, fn):
        return fn


_agentcore_runtime.BedrockAgentCoreApp = _DummyApp
sys.modules.setdefault("bedrock_agentcore", _agentcore)
sys.modules.setdefault("bedrock_agentcore.runtime", _agentcore_runtime)

for name in ("REPORTS_BUCKET", "PROVENANCE_TABLE", "LLM_MODEL_ID",
             "LAMBDA_SEARCH_FUNCTION"):
    os.environ.pop(name, None)

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import agent  # noqa: E402


class FiscalYearParsingTests(unittest.TestCase):
    def test_common_indian_fiscal_year_formats_include_end_year(self):
        examples = (
            "FY2024-25", "FY 24/25", "FY 2024–25", "2024-2025",
            "2024/25", "2024_25",
        )
        for value in examples:
            with self.subTest(value=value):
                self.assertEqual({2024, 2025}, agent._extract_year_intent(value))

    def test_upload_year_does_not_override_reporting_period(self):
        url = "/uploads/2027/Annual-Report-FY2025-26.pdf"
        self.assertEqual(
            2026,
            agent._report_recency_key(
                url, "Example Annual Report site:example.com"),
        )

    def test_newer_fiscal_range_is_not_capped_to_previous_year(self):
        url = "/uploads/2026/Annual-Report-FY2025-26.pdf"
        self.assertEqual(2026, agent._reporting_period_end_year(url))

    def test_explicit_end_year_matches_equivalent_fiscal_range(self):
        self.assertEqual(
            6,
            agent._year_alignment_score(
                "Example Annual Report 2025",
                "Example-Annual-Report-FY2024-25.pdf",
            ),
        )

    def test_query_variant_guard_accepts_equivalent_fiscal_notation(self):
        self.assertTrue(agent._query_variant_preserves_years(
            "Example Annual Report 2025",
            "Example Annual Report FY2024-25 filetype:pdf",
        ))
        self.assertFalse(agent._query_variant_preserves_years(
            "Example Annual Report 2025",
            "Example Annual Report FY2023-24 filetype:pdf",
        ))
        self.assertFalse(agent._query_variant_preserves_years(
            "Example Sustainability Report",
            "Example Sustainability Report 2025 filetype:pdf",
        ))
        self.assertFalse(agent._query_variant_preserves_years(
            "Example Annual Reports 2023 and 2024",
            "Example Annual Report 2024 filetype:pdf",
        ))


class DocumentHostTests(unittest.TestCase):
    def test_official_investor_page_accepts_any_safe_external_document_host(self):
        q4_url = "https://s22.q4cdn.com/123/files/AR_2025.pdf"
        other_cdn_url = "https://documents.vendor-example.net/FY2025-26.pdf"
        html = (
            f'<a href="{q4_url}">2025 Annual Report PDF</a>'
            f'<a href="{other_cdn_url}">FY2025-26 Annual Report PDF</a>'
        ).encode()
        links = agent._doc_links(
            html,
            "https://investors.example.com/investors/annual-reports",
            "investors.example.com",
            official_domain="investors.example.com",
        )
        self.assertIn(q4_url, links)
        self.assertIn(other_cdn_url, links)

    def test_external_document_host_is_not_accepted_from_unattested_page(self):
        external_url = "https://documents.vendor-example.net/AR_2025.pdf"
        html = f'<a href="{external_url}">2025 Annual Report PDF</a>'.encode()
        links = agent._doc_links(
            html,
            "https://www.example.com/news/article",
            "www.example.com",
            official_domain="www.example.com",
        )
        self.assertNotIn(external_url, links)


class LatestPreferenceTests(unittest.TestCase):
    def test_undated_annual_and_sustainability_queries_remain_undated(self):
        with mock.patch.object(agent, "CURRENT_YEAR", 2026):
            for report_class in ("annual report", "sustainability report"):
                query = f"Example {report_class} site:example.com"
                with self.subTest(report_class=report_class):
                    prepared, preferred = agent._apply_latest_completed_fiscal_year(
                        query, known_class=report_class)
                    self.assertEqual(query, prepared)
                    self.assertEqual(2026, preferred)

    def test_latest_search_variants_cover_end_year_and_indian_fy(self):
        variants = agent._latest_period_search_queries(
            "Example Annual Report site:example.com", 2026)
        self.assertIn("Example Annual Report 2026 site:example.com", variants)
        self.assertIn("Example Annual Report FY2025-26 site:example.com", variants)
        self.assertIn("Example Annual Report 2025 site:example.com", variants)
        self.assertIn("Example Annual Report FY2024-25 site:example.com", variants)

    def test_integrated_report_alias_is_ranked_as_an_annual_report(self):
        query = "Example Annual Report site:example.com"
        latest_integrated = (
            "https://example.com/investors/integrated-reports/"
            "Example-IR-2025-26.pdf")
        older_annual = (
            "https://example.com/investors/annual-reports/"
            "Example-Annual-Report-2022-23.pdf")
        with mock.patch.object(agent, "CURRENT_YEAR", 2026):
            ordered = agent._sort_for_verify(
                [older_annual, latest_integrated], query)
        self.assertEqual(latest_integrated, ordered[0])

    def test_shared_recency_key_prefers_fiscal_period_end_year(self):
        query = "Example Sustainability Report site:example.com"
        with mock.patch.object(agent, "CURRENT_YEAR", 2026):
            older = agent._report_recency_key(
                "/uploads/2026/Sustainability-Report-FY2024-25.pdf", query)
            latest = agent._report_recency_key(
                "/uploads/2027/Sustainability-Report-FY2025-26.pdf", query)
        self.assertEqual(2025, older)
        self.assertEqual(2026, latest)
        self.assertGreater(latest, older)

    def test_scan_continues_until_current_reporting_end_year(self):
        query = "Example Annual Report site:example.com"
        with mock.patch.object(agent, "CURRENT_YEAR", 2026):
            self.assertFalse(agent._reporting_year_goal_satisfied(
                "/docs/Annual-Report-FY2024-25.pdf", query))
            self.assertTrue(agent._reporting_year_goal_satisfied(
                "/docs/Annual-Report-FY2025-26.pdf", query))

    def test_sitemap_selects_latest_available_fiscal_report(self):
        landing = "https://www.example.com/sustainability/reports/"
        latest = "https://www.example.com/docs/Sustainability-Report-FY2025-26.pdf"
        older = "https://www.example.com/docs/Sustainability-Report-FY2024-25.pdf"
        html = (
            f'<a href="{older}">Sustainability Report FY2024-25</a>'
            f'<a href="{latest}">Sustainability Report FY2025-26</a>'
        ).encode()
        payloads = {
            landing: (html, "text/html"),
            older: (b"%PDF older", "application/pdf"),
            latest: (b"%PDF latest", "application/pdf"),
        }
        with mock.patch.object(agent, "CURRENT_YEAR", 2026), \
                mock.patch.object(agent, "_harvest_sitemap", return_value=[landing]), \
                mock.patch.object(agent, "_fetch", side_effect=lambda url: payloads[url]):
            resolved = agent._sitemap_resolve(
                "www.example.com",
                "Example Sustainability Report site:www.example.com",
                lambda candidate: True,
            )
        self.assertEqual(latest, resolved["url"])

    def test_latest_selection_falls_back_when_preferred_year_is_missing(self):
        query = "Example Annual Report site:example.com"
        candidates = [
            "/docs/Annual-Report-FY2023-24.pdf",
            "/docs/Annual-Report-FY2024-25.pdf",
        ]
        with mock.patch.object(agent, "CURRENT_YEAR", 2026):
            selected = max(candidates,
                           key=lambda url: agent._report_recency_key(url, query))
        self.assertTrue(selected.endswith("FY2024-25.pdf"))

    def test_orchestrator_does_not_commit_older_search_hit_before_sitemap(self):
        older = "https://example.com/docs/Annual-Report-FY2024-25.pdf"
        latest = "https://example.com/docs/Annual-Report-FY2025-26.pdf"
        found = {
            "decision": {
                "selected_url": older,
                "topic_match": True,
                "company_match": True,
                "reason": "older search hit",
            },
            "candidate_infos": [{
                "url": older,
                "_body": b"%PDF older",
                "head_ctype": "application/pdf",
            }],
            "via": "test-search",
            "domain_mode": "hard",
        }

        def fake_store(company, run_id, url, body, ctype, title, query,
                       report_class=None, year=None):
            return {
                "status": "stored",
                "s3_key": "example/report.pdf",
                "sha256": "latest-hash",
                "source_url": url,
                "year": year,
            }

        with mock.patch.object(agent, "CURRENT_YEAR", 2026), \
                mock.patch.object(agent, "ENABLE_REGISTRY_TIER", False), \
                mock.patch.object(agent, "USE_BROWSER", False), \
                mock.patch.object(agent, "_alias_queries", return_value=[]), \
                mock.patch.object(agent, "_llm_generate_search_queries",
                                  return_value=[]), \
                mock.patch.object(agent, "_find_best_document",
                                  return_value=found), \
                mock.patch.object(agent, "_make_browser_verify_fn",
                                  return_value=lambda candidate: True), \
                mock.patch.object(agent, "_sitemap_resolve", return_value={
                    "url": latest,
                    "body": b"%PDF latest",
                    "ctype": "application/pdf",
                    "via": "sitemap_landing_page",
                }), \
                mock.patch.object(agent, "_store", side_effect=fake_store):
            result = agent._invoke_sync({
                "company": {"name": "Example", "domain": "example.com"},
                "reports": [{"report_class": "annual report"}],
                "browser_enabled": False,
            })

        self.assertEqual(latest, result["stored"][0]["source_url"])
        self.assertEqual(2026, result["stored"][0]["year"])
        self.assertEqual("sitemap", result["stored"][0]["stage"])


if __name__ == "__main__":
    unittest.main()
