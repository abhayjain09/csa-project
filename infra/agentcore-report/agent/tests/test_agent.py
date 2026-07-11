import importlib.util
import os
from pathlib import Path
import sys
import unittest


os.environ["REQUIRE_LLM_VALIDATION"] = "false"
MODULE_PATH = Path(__file__).resolve().parents[1] / "agent.py"
SPEC = importlib.util.spec_from_file_location("report_agent", MODULE_PATH)
assert SPEC and SPEC.loader
agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent
SPEC.loader.exec_module(agent)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.company = agent.Company(
            legal_name="Example Corporation",
            aliases=("Example Corp",),
            official_domains=("example.com",),
        )

    def test_valid_registry_annual_report_passes(self):
        request = agent.DocumentRequest("annual-2024", "annual_report", 2024)
        candidate = agent.Candidate(
            url="https://www.sec.gov/Archives/example-10k-2024.html",
            source_tier="registry",
            title="Example Corporation 2024 Annual Report (Form 10-K)",
            registry_identity_verified=True,
            body=b"<html><title>Example Corporation Annual Report 2024</title><body>Form 10-K for Example Corporation 2024</body></html>",
            content_type="text/html",
        )
        validation = agent._validate(candidate, self.company, request)
        self.assertTrue(validation.accepted)
        self.assertGreaterEqual(validation.score, 80)

    def test_wrong_document_class_is_rejected(self):
        request = agent.DocumentRequest("annual-2024", "annual_report", 2024)
        candidate = agent.Candidate(
            url="https://example.com/proxy-statement-2024.pdf",
            source_tier="official_site",
            title="Example Corporation 2024 Proxy Statement",
            body=b"<html><body>Example Corporation Proxy Statement 2024</body></html>",
            content_type="text/html",
        )
        validation = agent._validate(candidate, self.company, request)
        self.assertFalse(validation.accepted)
        self.assertIn("contains rejected neighbouring document class", validation.reasons)

    def test_link_extraction_keeps_only_official_or_trusted_hosts(self):
        company = agent.Company(
            legal_name="Example Corporation",
            official_domains=("example.com",),
            trusted_document_hosts=("q4cdn.com",),
        )
        page = b'''<a href="/reports/annual.pdf">Annual report</a>
        <a href="https://s1.q4cdn.com/report.pdf">Download</a>
        <a href="https://other.example.net/report.pdf">Ignore</a>'''
        links = agent._extract_links(page, "https://www.example.com/investors", company)
        self.assertEqual([link.url for link in links], [
            "https://www.example.com/reports/annual.pdf",
            "https://s1.q4cdn.com/report.pdf",
        ])

    def test_ticker_is_resolved_through_sec_map(self):
        original_fetch = agent._sec_fetch_json
        agent._sec_ticker_cache = None
        agent._sec_fetch_json = lambda _url: {"0": {"ticker": "EXMP", "cik_str": 12345}}
        try:
            self.assertEqual(agent._sec_cik_for_ticker("exmp"), "12345")
        finally:
            agent._sec_fetch_json = original_fetch
            agent._sec_ticker_cache = None


if __name__ == "__main__":
    unittest.main()
