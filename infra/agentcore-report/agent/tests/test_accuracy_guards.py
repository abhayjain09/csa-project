"""Focused regression tests for identity and per-query mapping safety."""

import ast
import json
import re
import sys
import threading
import time
import types
import unittest
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

from pypdf import PdfReader, PdfWriter


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


def _load_worker_validation_helpers():
    path = REPO_ROOT / "reportiq-ecs/app/backend/browser_worker.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"_normalize_text", "_company_matches", "_class_matches"}
    nodes = [
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "unicodedata": unicodedata,
        "unquote": unquote,
        "urlparse": urlparse,
        "_LEGAL_SUFFIXES": {
            "inc", "incorporated", "corp", "corporation", "company", "co",
            "limited", "ltd", "plc", "llc", "holdings", "group",
        },
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def _load_bulk_queue_helpers(dynamo, executor, invoke_fn):
    path = REPO_ROOT / "reportiq-ecs/app/backend/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"_queue_bulk_invocations", "_chunk_web_queries"}
    nodes = [
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "uuid": uuid,
        "datetime": datetime,
        "timezone": timezone,
        "json": json,
        "get_dynamo": lambda: dynamo,
        "RUNS_TABLE": "runs",
        "QUERIES_TABLE": "queries",
        "AGENT_CHUNK_SIZE": 1,
        "AGENT_CHUNK_CONCURRENCY": 3,
        "BULK_COMPANY_CONCURRENCY": 3,
        "_BULK_COMPANY_EXECUTOR": executor,
        "_do_invoke": invoke_fn,
        "re": re,
        "log": types.SimpleNamespace(info=lambda *args, **kwargs: None),
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def _load_structured_payload_helpers():
    path = REPO_ROOT / "reportiq-ecs/app/backend/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted_functions = {"_infer_report_class", "_build_chunk_payload"}
    nodes = []
    for item in tree.body:
        if (isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name)
                        and target.id == "_REPORT_CLASS_ALIASES"
                        for target in item.targets)):
            nodes.append(item)
        elif (isinstance(item, ast.FunctionDef)
              and item.name in wanted_functions):
            nodes.append(item)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"re": re}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def _load_pdf_integrity_helper(relative_path: str, function_name: str):
    path = REPO_ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == function_name
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "BytesIO": BytesIO,
        "PdfReader": PdfReader,
        "urlparse": urlparse,
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[function_name]


def _load_manual_source_url_helper():
    path = REPO_ROOT / "reportiq-ecs/app/backend/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "_safe_manual_source_url"
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"urlsplit": urlparse}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_safe_manual_source_url"]


def _load_worker_terminal_helper(jobs_table):
    path = REPO_ROOT / "reportiq-ecs/app/backend/browser_worker.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "_all_run_jobs_terminal"
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "jobs_table": jobs_table,
        "_TERMINAL_JOB_STATUSES": {
            "downloaded", "blocked_by_source_waf", "failed", "launch_failed",
        },
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_all_run_jobs_terminal"]


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

    def test_waf_result_preserves_exact_candidates_and_browser_job(self):
        pair = _load_pairing_function()
        candidate = (
            "https://www.spglobal.com/content/dam/spglobal/vendor-code.pdf")
        results = pair(
            ["S&P Global Supplier Code of Conduct"],
            [],
            [],
            [{
                "request_id": "2:1",
                "status": "blocked_by_source_waf",
                "reason": "source blocked",
                "candidate_urls": [candidate],
                "browser_job_id": "job-123",
            }],
            chunk_index=2,
        )
        self.assertEqual(results[0]["status"], "browser_retry_queued")
        self.assertEqual(results[0]["browser_job_id"], "job-123")
        self.assertEqual(results[0]["candidate_urls"], [candidate])


class BrowserWorkerValidationTests(unittest.TestCase):
    def test_sp_global_vendor_code_matches_company_and_specific_class(self):
        helpers = _load_worker_validation_helpers()
        text = (
            "S&P Global Vendor Code of Conduct. This code establishes "
            "requirements for every supplier and business partner.")
        url = "https://www.spglobal.com/docs/vendor-code-of-conduct.pdf"
        self.assertTrue(helpers["_company_matches"](
            "S&P Global", text, url))
        ok, _ = helpers["_class_matches"](
            "supplier code of conduct", text, url, "")
        self.assertTrue(ok)

    def test_general_employee_code_is_not_supplier_code(self):
        helpers = _load_worker_validation_helpers()
        ok, _ = helpers["_class_matches"](
            "supplier code of conduct",
            "Cisco Systems Code of Business Conduct for all employees.",
            "https://cisco.com/code-of-conduct.pdf",
            "",
        )
        self.assertFalse(ok)


class StructuredPayloadTests(unittest.TestCase):
    def test_chunk_payload_preserves_explicit_report_classes(self):
        helpers = _load_structured_payload_helpers()
        payload = helpers["_build_chunk_payload"](
            "S&P Global",
            "run-123",
            "",
            [
                "site:spglobal.com Whistleblowing Policy",
                "site:spglobal.com Annual Report 2025",
            ],
            4,
        )
        self.assertEqual(
            [item["report_class"] for item in payload["reports"]],
            ["whistleblowing mechanism", "annual report"],
        )
        self.assertEqual(payload["reports"][0]["request_id"], "4:1")
        self.assertEqual(payload["reports"][1]["year"], 2025)
        self.assertNotIn(
            "uncategorized",
            {item["report_class"] for item in payload["reports"]},
        )
        self.assertEqual(
            payload["web_query_ids"],
            {"web_query1": "4:1", "web_query2": "4:2"},
        )

    def test_unknown_class_uses_stable_fallback_not_uncategorized(self):
        infer = _load_structured_payload_helpers()["_infer_report_class"]
        self.assertEqual(
            infer("site:example.com Responsible AI Principles", "Example Inc"),
            "responsible ai principles",
        )

    def test_portal_labels_map_to_agent_canonical_classes(self):
        infer = _load_structured_payload_helpers()["_infer_report_class"]
        cases = {
            "Anti-Corruption and Bribery Policy":
                "anti-bribery and corruption policy",
            "Environment, Health and Safety Policy":
                "occupational health & safety policy",
            "Tax Strategy and Policy Document":
                "tax strategy and governance",
            "Supplier Code of Conduct":
                "supplier code of conduct",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(infer(query), expected)


class PdfIntegrityTests(unittest.TestCase):
    @staticmethod
    def _valid_pdf() -> bytes:
        output = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.write(output)
        return output.getvalue()

    def test_agent_rejects_html_disguised_as_pdf(self):
        validate = _load_pdf_integrity_helper(
            "infra/agentcore-report/agent/agent.py",
            "_document_integrity_error",
        )
        error = validate(
            "https://example.com/report.pdf",
            "text/html",
            b"<html><h1>404 Not Found</h1></html>",
        )
        self.assertIn("missing %PDF header", error)

    def test_agent_accepts_parseable_pdf(self):
        validate = _load_pdf_integrity_helper(
            "infra/agentcore-report/agent/agent.py",
            "_document_integrity_error",
        )
        self.assertEqual(
            validate(
                "https://example.com/report.pdf",
                "application/pdf",
                self._valid_pdf(),
            ),
            "",
        )

    def test_portal_manual_upload_uses_same_pdf_gate(self):
        validate = _load_pdf_integrity_helper(
            "reportiq-ecs/app/backend/app.py",
            "_pdf_integrity_error",
        )
        self.assertTrue(validate(
            "report.pdf", "application/pdf", b"<Error>NoSuchKey</Error>"))
        self.assertEqual(
            validate("report.pdf", "application/pdf", self._valid_pdf()), "")


class BrowserWorkerPatchTests(unittest.TestCase):
    def test_projection_defines_status_alias_on_every_page(self):
        class FakeJobsTable:
            def __init__(self):
                self.calls = []

            def scan(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {
                        "Items": [{"status": "downloaded"}],
                        "LastEvaluatedKey": {"job_id": "one"},
                    }
                return {"Items": [{"status": "failed"}]}

        table = FakeJobsTable()
        terminal = _load_worker_terminal_helper(table)
        self.assertTrue(terminal("run-123"))
        self.assertEqual(len(table.calls), 2)
        for call in table.calls:
            self.assertEqual(
                call["ExpressionAttributeNames"]["#st"], "status")


class FrontendDownloadTests(unittest.TestCase):
    def test_citation_uses_verified_download_flow_not_json_endpoint(self):
        path = REPO_ROOT / "reportiq-ecs/app/static/index.html"
        source = path.read_text(encoding="utf-8")
        self.assertNotIn(
            'href="/api/sources/download-url?key=', source)
        self.assertIn(
            "downloadFileVerified(decB64(", source)

    def test_terminal_fargate_failure_offers_manual_download_and_upload(self):
        path = REPO_ROOT / "reportiq-ecs/app/static/index.html"
        source = path.read_text(encoding="utf-8")
        self.assertIn("browserFinishedWithoutDownload", source)
        self.assertIn("↗ Manual download", source)
        self.assertIn("⬆ Upload file", source)
        self.assertIn("fd.append('source_url', sourceUrl||'');", source)
        self.assertIn('rel="noopener noreferrer"', source)


class ManualRecoveryUrlTests(unittest.TestCase):
    def test_only_bounded_https_urls_are_kept_for_provenance(self):
        clean = _load_manual_source_url_helper()
        expected = "https://www.spglobal.com/report.pdf"
        self.assertEqual(clean(expected), expected)
        self.assertEqual(clean("javascript:alert(1)"), "")
        self.assertEqual(clean("http://example.com/report.pdf"), "")
        self.assertEqual(clean("https://user:secret@example.com/report.pdf"), "")


class BulkCompanyConcurrencyTests(unittest.TestCase):
    def test_ten_company_bulk_run_never_exceeds_three_active_companies(self):
        class FakeTable:
            def __init__(self):
                self.items = []

            def put_item(self, **kwargs):
                self.items.append(kwargs["Item"])

            def update_item(self, **kwargs):
                return {}

        class FakeDynamo:
            def __init__(self):
                self.tables = {"runs": FakeTable(), "queries": FakeTable()}

            def Table(self, name):
                return self.tables[name]

        dynamo = FakeDynamo()
        lock = threading.Lock()
        active = 0
        maximum = 0

        def invoke(_run_id, _record):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.025)
            with lock:
                active -= 1

        executor = ThreadPoolExecutor(max_workers=3)
        helpers = _load_bulk_queue_helpers(dynamo, executor, invoke)
        records = [
            {
                "query_id": f"query-{index}",
                "company": f"Company {index}",
                "web_query1": "Annual Report",
            }
            for index in range(10)
        ]
        run_ids, batch_id = helpers["_queue_bulk_invocations"](records)
        executor.shutdown(wait=True)

        self.assertEqual(len(run_ids), 10)
        self.assertTrue(batch_id)
        self.assertEqual(maximum, 3)
        self.assertEqual(
            [item["status"] for item in dynamo.tables["runs"].items],
            ["queued"] * 10,
        )


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
