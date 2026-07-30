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
from urllib.parse import unquote, urljoin, urlparse

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
    wanted = {
        "_normalize_text", "_company_matches", "_class_matches",
        "_candidate_year", "_language_score", "_candidate_preference_key",
        "_has_preferred_unresolved",
    }
    nodes = []
    for item in tree.body:
        if (isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name)
                        and target.id == "_NON_ENGLISH_CODES"
                        for target in item.targets)):
            nodes.append(item)
        elif isinstance(item, ast.FunctionDef) and item.name in wanted:
            nodes.append(item)
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


def _load_current_language_helpers():
    """AST-extracts _language_preference_score/_is_localized_variant_url from
    the CURRENT agent.py (unlike _load_language_and_scope_helpers above,
    which points at the stale infra/agentcore-report/... path)."""
    path = REPO_ROOT / "agents/download-agent/agent/agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted_assignments = {"_NON_ENGLISH_LANGUAGE_CODES", "_NON_ENGLISH_LANGUAGE_WORDS"}
    wanted_functions = {"_language_preference_score", "_is_localized_variant_url"}
    nodes = []
    for item in tree.body:
        if (isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id in wanted_assignments
                        for target in item.targets)):
            nodes.append(item)
        elif isinstance(item, ast.FunctionDef) and item.name in wanted_functions:
            nodes.append(item)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"re": re, "unquote": unquote, "urlparse": urlparse}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def _load_current_confidence_function():
    """AST-extracts _confident from the CURRENT agent.py (unlike
    _load_confidence_function above, which points at the stale
    infra/agentcore-report/... path)."""
    path = REPO_ROOT / "agents/download-agent/agent/agent.py"
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


def _load_page_render_helpers():
    """AST-extracts _PageRenderAttempts/_try_page_render_fallback from the
    CURRENT agent.py in this repo (agents/download-agent/agent/agent.py) —
    unlike the other _load_* helpers above, which point at infra/agentcore-
    report/... and reportiq-ecs/... paths that do not exist in this repo
    layout (pre-existing staleness, unrelated to this test)."""
    path = REPO_ROOT / "agents/download-agent/agent/agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"_PageRenderAttempts", "_try_page_render_fallback"}
    nodes = [
        item for item in tree.body
        if isinstance(item, (ast.ClassDef, ast.FunctionDef))
        and item.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    import report_specs
    namespace = {
        "report_specs": report_specs,
        "BROWSER_MAX_DOC_BYTES": 80 * 1024 * 1024,
        "BROWSER_PAGE_RENDER_MAX_ATTEMPTS": 3,
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


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
        "_latest_search_query_variants",
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
        "REGISTRY_FIRST_CLASSES": {"annual report", "proxy statement"},
        "LATEST_DOCUMENT_SEARCH": True,
        "LATEST_DOCUMENT_SEARCH_VARIANTS": 6,
        "LATEST_COMPLETED_FISCAL_YEAR_LAG": 1,
        "CURRENT_YEAR": 2026,
        "_extract_year_intent": lambda value: {
            int(year) for year in re.findall(r"\b20\d{2}\b", value or "")
        },
        "_clean_domain": lambda value: str(value or "").lower().strip(),
        "_strip_site": lambda value: re.sub(
            r"site:\s*\S+", "", value or "", flags=re.I).strip(),
        "_query_variant_preserves_years": lambda original, variant: True,
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def _load_language_and_scope_helpers():
    path = REPO_ROOT / "infra/agentcore-report/agent/agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted_assignments = {
        "_NON_ENGLISH_LANGUAGE_CODES",
        "_NON_ENGLISH_LANGUAGE_WORDS",
        "_YEAR_RE",
        "_YY_OR_YYYY_RE",
    }
    wanted_functions = {
        "_candidate_document_year",
        "_extract_year_intent",
        "_language_preference_score",
        "_is_local_scope_report_url",
        "_prefer_newer_document",
    }
    nodes = []
    for item in tree.body:
        if (isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name)
                        and target.id in wanted_assignments
                        for target in item.targets)):
            nodes.append(item)
        elif (isinstance(item, ast.FunctionDef)
              and item.name in wanted_functions):
            nodes.append(item)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "unquote": unquote,
        "urlparse": urlparse,
        "CURRENT_YEAR": 2026,
        "_matched_doc_classes": lambda query: [
            ("sustainability report", {})
        ] if "sustainability" in query.lower() else [
            ("annual report", {})
        ] if "annual" in query.lower() else [],
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def _load_document_link_helpers():
    path = REPO_ROOT / "infra/agentcore-report/agent/agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"_registrable", "_is_official_source_page", "_doc_links"}
    nodes = [
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "urljoin": urljoin,
        "urlparse": urlparse,
        "_is_safe_remote_document_url": lambda url: url.endswith(".pdf"),
        "_is_junk_host": lambda url: False,
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

    def test_latest_fiscal_range_url_outranks_older_report(self):
        helpers = _load_worker_validation_helpers()
        key = helpers["_candidate_preference_key"]
        latest = (
            "https://sustainability.example.com/reports/"
            "company_sr_2024_25.pdf"
        )
        older = (
            "https://sustainability.example.com/reports/"
            "company_sr_2023-24.pdf"
        )
        self.assertEqual(helpers["_candidate_year"](latest), 2025)
        self.assertGreater(key(latest), key(older))

    def test_worker_refuses_old_report_when_latest_url_is_blocked(self):
        helpers = _load_worker_validation_helpers()
        stale = "https://example.com/report_2023-24.pdf"
        latest = "https://example.com/report_2024_25.pdf"
        self.assertTrue(helpers["_has_preferred_unresolved"](
            stale, [latest], True))
        self.assertFalse(helpers["_has_preferred_unresolved"](
            stale, [latest], False))

    def test_transport_integrity_failure_remains_retryable(self):
        path = REPO_ROOT / "infra/agentcore-report/agent/agent.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "_make_browser_verify_fn"
        )
        integrity_branch = next(
            item for item in ast.walk(function)
            if isinstance(item, ast.If)
            and isinstance(item.test, ast.Name)
            and item.test.id == "integrity_error"
        )
        self.assertFalse(any(
            isinstance(node, ast.Attribute)
            and node.attr == "mark_rejected"
            for node in ast.walk(integrity_branch)
        ))


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
        self.assertTrue(payload["reports"][0]["prefer_latest"])
        self.assertFalse(payload["reports"][1]["prefer_latest"])
        self.assertEqual(
            payload["document_preferences"]["preferred_language"], "en")
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
    def test_annual_report_uses_authoritative_registry_before_browser(self):
        route = _load_routing_helpers()["_discovery_route"](
            "annual report", True)
        self.assertEqual(route, [
            "registry",
            "direct_search",
            "official_crawl",
            "deep_crawl",
            "browser",
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

    def test_proxy_statement_uses_authoritative_registry_first(self):
        route = _load_routing_helpers()["_discovery_route"](
            "proxy statement", True)
        self.assertEqual(route[0], "registry")

    def test_undated_search_checks_current_and_previous_fiscal_labels(self):
        variants = _load_routing_helpers()["_latest_search_query_variants"](
            "Acme annual report site:acme.com")
        self.assertIn("Acme annual report 2026", variants)
        self.assertIn("Acme annual report FY2026", variants)
        self.assertIn("Acme annual report 2025", variants)
        self.assertIn("Acme annual report FY2025", variants)


class LanguageScopeAndDomainTests(unittest.TestCase):
    def test_english_and_default_files_rank_above_localized_variants(self):
        score = _load_language_and_scope_helpers()[
            "_language_preference_score"]
        self.assertGreater(
            score("https://example.com/reports/code-of-conduct.pdf"),
            score("https://example.com/reports/code-of-conduct-es.pdf"),
        )
        self.assertGreater(
            score("https://example.com/en-us/reports/policy.pdf"),
            score("https://example.com/es-la/reports/policy.pdf"),
        )

    def test_operation_report_is_not_group_sustainability_report(self):
        is_local = _load_language_and_scope_helpers()[
            "_is_local_scope_report_url"]
        self.assertTrue(is_local(
            "https://example.com/documents/operations/mine/"
            "2025-sustainability-report.pdf",
            "Example sustainability report",
        ))
        self.assertFalse(is_local(
            "https://example.com/reports/2025-sustainability-report.pdf",
            "Example sustainability report",
        ))

    def test_latest_english_document_wins_and_fy_two_digit_is_parsed(self):
        helpers = _load_language_and_scope_helpers()
        prefer = helpers["_prefer_newer_document"]
        year = helpers["_candidate_document_year"]
        older = {"url": "https://example.com/report-2023.pdf"}
        latest = {"url": "https://example.com/FY25-report.pdf"}
        self.assertEqual(year(latest), 2025)
        self.assertIs(prefer(older, latest), latest)

    def test_main_agent_understands_underscored_fiscal_year_range(self):
        helpers = _load_language_and_scope_helpers()
        year = helpers["_candidate_document_year"]
        latest = {
            "url": "https://example.com/asian_paints_sr_2024_25.pdf",
        }
        older = {
            "url": "https://example.com/asian_paints_sr_2023-24.pdf",
        }
        self.assertEqual(year(latest), 2025)
        self.assertEqual(year(older), 2024)
        self.assertIs(helpers["_prefer_newer_document"](older, latest), latest)

    def test_localized_newer_file_does_not_displace_english_default(self):
        prefer = _load_language_and_scope_helpers()[
            "_prefer_newer_document"]
        english = {"url": "https://example.com/report-2025.pdf"}
        localized = {"url": "https://example.com/report-2026-es.pdf"}
        self.assertIs(prefer(english, localized), english)

    def test_official_page_can_attest_document_on_separate_cdn_domain(self):
        links = _load_document_link_helpers()["_doc_links"](
            b'<a href="https://cdn.example-assets.net/latest-report.pdf">'
            b'Download report PDF</a>',
            "https://www.example.com/sustainability/reports",
            "example.com",
            official_domain="example.com",
        )
        self.assertEqual(
            links,
            ["https://cdn.example-assets.net/latest-report.pdf"],
        )

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


class LocalizedVariantRejectionTests(unittest.TestCase):
    """Regression test for the NVIDIA case: a CMS locale-BUCKET path segment
    ("/en-zz/") is not evidence the FILE is English — the filename's own
    "-hy" (Armenian) suffix is the reliable signal, and was previously masked
    because the loose path-level "en" check matched first and returned early."""

    def setUp(self):
        helpers = _load_current_language_helpers()
        self.score = helpers["_language_preference_score"]
        self.is_localized = helpers["_is_localized_variant_url"]

    def test_nvidia_armenian_variant_is_rejected_despite_en_zz_path(self):
        url = ("https://images.nvidia.com/aem-dam/en-zz/Solutions/about-us/"
               "NVIDIA-Code-of-Conduct-External-hy.pdf")
        self.assertEqual(self.score(url), -30)
        self.assertTrue(self.is_localized(url, "en"))

    def test_nvidia_plain_english_variant_still_accepted(self):
        url = ("https://images.nvidia.com/aem-dam/en-zz/Solutions/about-us/"
               "NVIDIA-Code-of-Conduct-External.pdf")
        self.assertEqual(self.score(url), 20)
        self.assertFalse(self.is_localized(url, "en"))

    def test_explicit_en_suffix_still_accepted(self):
        url = ("https://images.nvidia.com/aem-dam/en-zz/Solutions/about-us/"
               "NVIDIA-Code-of-Conduct-External-en.pdf")
        self.assertFalse(self.is_localized(url, "en"))

    def test_generic_en_us_locale_path_still_accepted(self):
        self.assertFalse(self.is_localized("https://example.com/en-us/policy.pdf", "en"))

    def test_generic_localized_filename_suffix_still_rejected(self):
        self.assertTrue(self.is_localized("https://example.com/policy-fr.pdf", "en"))

    def test_generic_localized_directory_still_rejected(self):
        self.assertTrue(self.is_localized("https://example.com/de/policy.pdf", "en"))

    def test_non_english_preference_does_not_enforce_reject(self):
        # Can only positively detect "not English", not confirm an arbitrary
        # OTHER requested language — deliberately unenforced in that case.
        url = ("https://images.nvidia.com/aem-dam/en-zz/Solutions/about-us/"
               "NVIDIA-Code-of-Conduct-External-hy.pdf")
        self.assertFalse(self.is_localized(url, "fr"))


class ConfidenceOverrideTests(unittest.TestCase):
    """Regression test for the bug where a correctly-identified page-render
    candidate (Umicore's real Code of Conduct page, LLM confidence "medium")
    was rejected under a deployed MIN_SELECTION_CONFIDENCE="high", making the
    HTML-page-render fallback unreachable in practice."""

    def _medium_confidence_decision(self):
        return {
            "selected_url": "https://example.com/code-of-conduct/",
            "topic_match": True, "company_match": True, "year_match": True,
            "confidence": "medium",
        }

    def test_medium_confidence_rejected_under_global_high_bar(self):
        confident = _load_current_confidence_function()
        self.assertFalse(confident(self._medium_confidence_decision(), ""))

    def test_medium_confidence_accepted_with_page_render_override(self):
        confident = _load_current_confidence_function()
        self.assertTrue(confident(
            self._medium_confidence_decision(), "", min_confidence="medium"))

    def test_low_confidence_still_rejected_even_with_override(self):
        confident = _load_current_confidence_function()
        decision = self._medium_confidence_decision()
        decision["confidence"] = "low"
        self.assertFalse(confident(decision, "", min_confidence="medium"))


class PageRenderFallbackTests(unittest.TestCase):
    """_try_page_render_fallback: HTML-page-as-document rendering fallback
    (see the plan for the Umicore Code of Conduct case — a page can BE the
    policy with no downloadable file at all)."""

    class _FakePage:
        def __init__(self, html=b"<html>policy text</html>",
                     pdf_bytes=b"%PDF-1.4 fake", pdf_exc=None,
                     content_exc=None):
            self._html = html
            self._pdf_bytes = pdf_bytes
            self._pdf_exc = pdf_exc
            self._content_exc = content_exc
            self.pdf_calls = 0

        def content(self):
            if self._content_exc:
                raise self._content_exc
            return self._html.decode("utf-8")

        def pdf(self, **kwargs):
            self.pdf_calls += 1
            if self._pdf_exc:
                raise self._pdf_exc
            return self._pdf_bytes

    def setUp(self):
        helpers = _load_page_render_helpers()
        self.fallback = helpers["_try_page_render_fallback"]
        self.attempts_cls = helpers["_PageRenderAttempts"]

    def test_ineligible_class_short_circuits_before_verify(self):
        page = self._FakePage()
        verify_calls = []
        verify_fn = lambda cand: verify_calls.append(cand) or True
        result = self.fallback(
            page, "https://example.com/annual-report/", "annual report",
            "Example annual report", verify_fn, self.attempts_cls())
        self.assertIsNone(result)
        self.assertEqual(verify_calls, [])
        self.assertEqual(page.pdf_calls, 0)

    def test_verify_rejection_prevents_render(self):
        page = self._FakePage()
        result = self.fallback(
            page, "https://example.com/code-of-conduct/", "code of conduct",
            "Example code of conduct", lambda cand: False,
            self.attempts_cls())
        self.assertIsNone(result)
        self.assertEqual(page.pdf_calls, 0)

    def test_render_failure_is_swallowed_not_raised(self):
        page = self._FakePage(pdf_exc=RuntimeError("Page.pdf() not available"))
        result = self.fallback(
            page, "https://example.com/code-of-conduct/", "code of conduct",
            "Example code of conduct", lambda cand: True, self.attempts_cls())
        self.assertIsNone(result)
        self.assertEqual(page.pdf_calls, 1)

    def test_successful_render_returns_pdf_candidate(self):
        page = self._FakePage(pdf_bytes=b"%PDF-1.4 real bytes")
        result = self.fallback(
            page, "https://example.com/code-of-conduct/", "code of conduct",
            "Example code of conduct", lambda cand: True, self.attempts_cls())
        self.assertIsNotNone(result)
        self.assertEqual(result["ctype"], "application/pdf")
        self.assertEqual(result["via"], "browser_page_render")
        self.assertTrue(result["verified"])
        self.assertEqual(result["body"], b"%PDF-1.4 real bytes")

    def test_attempt_budget_exhausted_skips_without_calling_verify(self):
        page = self._FakePage()
        verify_calls = []
        verify_fn = lambda cand: verify_calls.append(cand) or True
        attempts = self.attempts_cls(limit=0)
        result = self.fallback(
            page, "https://example.com/code-of-conduct/", "code of conduct",
            "Example code of conduct", verify_fn, attempts)
        self.assertIsNone(result)
        self.assertEqual(verify_calls, [])

    def test_oversized_render_is_discarded(self):
        page = self._FakePage(pdf_bytes=b"x" * (81 * 1024 * 1024))
        result = self.fallback(
            page, "https://example.com/code-of-conduct/", "code of conduct",
            "Example code of conduct", lambda cand: True, self.attempts_cls())
        self.assertIsNone(result)

    def test_ineligible_class_never_marks_attempted(self):
        # Regression: a report_class the fallback never even considers must
        # not tell the caller a real verify happened — otherwise the shared
        # cross-report browser cache would be told "checked" for a page it
        # never actually looked at for THIS class.
        stats = {}
        result = self.fallback(
            self._FakePage(), "https://example.com/annual-report/",
            "annual report", "Example annual report",
            lambda cand: True, self.attempts_cls(), stats=stats)
        self.assertIsNone(result)
        self.assertNotIn("attempted", stats)

    def test_rejected_verify_still_marks_attempted(self):
        # This is the exact bug this test guards: a genuine class-specific
        # judgment happened on the page (even though it was REJECTED), so the
        # caller must be told "attempted" — otherwise the shared
        # _root_crawl_cache (keyed by URL, reused across every report_class in
        # a run) records this page as "no candidates at all, reusing miss for
        # any future class," silently skipping the render check entirely for
        # a LATER report whose browser seed happens to be the same URL.
        stats = {}
        result = self.fallback(
            self._FakePage(), "https://example.com/code-of-conduct/",
            "code of conduct", "Example code of conduct",
            lambda cand: False, self.attempts_cls(), stats=stats)
        self.assertIsNone(result)
        self.assertTrue(stats.get("attempted"))

    def test_successful_render_marks_attempted(self):
        stats = {}
        result = self.fallback(
            self._FakePage(), "https://example.com/code-of-conduct/",
            "code of conduct", "Example code of conduct",
            lambda cand: True, self.attempts_cls(), stats=stats)
        self.assertIsNotNone(result)
        self.assertTrue(stats.get("attempted"))

    def test_budget_exhausted_never_marks_attempted(self):
        stats = {}
        result = self.fallback(
            self._FakePage(), "https://example.com/code-of-conduct/",
            "code of conduct", "Example code of conduct",
            lambda cand: True, self.attempts_cls(limit=0), stats=stats)
        self.assertIsNone(result)
        self.assertNotIn("attempted", stats)


def _load_current_agent_symbols(func_names, assign_names, namespace):
    """AST-extract the named top-level functions/classes and assignments from
    the CURRENT agent.py into a controlled namespace. Used by the accuracy
    guards below so they exercise the shipped code without importing the whole
    agent (which needs boto3/bedrock)."""
    path = REPO_ROOT / "agents/download-agent/agent/agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    func_names = set(func_names or ())
    assign_names = set(assign_names or ())
    nodes = []
    for item in tree.body:
        if (isinstance(item, (ast.FunctionDef, ast.ClassDef))
                and item.name in func_names):
            nodes.append(item)
        elif isinstance(item, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in assign_names
                for t in item.targets):
            nodes.append(item)
        elif isinstance(item, ast.AnnAssign) and (
                isinstance(item.target, ast.Name)
                and item.target.id in assign_names):
            nodes.append(item)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


class PdfTextSampleTests(unittest.TestCase):
    """A1: the verifier must read real PDF prose, not the binary header."""

    def _sample_fn(self):
        import threading as _threading
        ns = {
            "PdfReader": PdfReader, "BytesIO": BytesIO, "hashlib": __import__("hashlib"),
            "threading": _threading, "re": re, "print": lambda *a, **k: None,
            "PDF_TEXT_SAMPLE_MAX_PAGES": 4, "PDF_TEXT_SAMPLE_MAX_CHARS": 4000,
            "_PDF_TEXT_SAMPLE_CACHE": {}, "_PDF_TEXT_SAMPLE_CACHE_LOCK": _threading.Lock(),
        }
        _load_current_agent_symbols(["_pdf_text_sample"], [], ns)
        return ns["_pdf_text_sample"]

    @staticmethod
    def _blank_pdf() -> bytes:
        out = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.write(out)
        return out.getvalue()

    def test_non_pdf_bytes_return_empty(self):
        self.assertEqual(self._sample_fn()(b"<html>not a pdf</html>"), "")

    def test_none_body_returns_empty(self):
        self.assertEqual(self._sample_fn()(None), "")

    def test_blank_pdf_extracts_no_text_gracefully(self):
        # A parseable PDF with no text layer must not raise; it returns "" so
        # the caller falls back to the raw-byte sample.
        self.assertEqual(self._sample_fn()(self._blank_pdf()), "")


class CompanyEvidenceTests(unittest.TestCase):
    """A2: the cross-company gate must require the company to actually appear
    in the document text, not merely that the sample is >= 40 chars."""

    def setUp(self):
        ns = {"re": re, "unicodedata": unicodedata}
        _load_current_agent_symbols(
            ["_normalize_company_text", "_company_name_tokens",
             "_company_evidence_in_text"],
            ["_COMPANY_LEGAL_SUFFIXES"], ns)
        self.evidence = ns["_company_evidence_in_text"]

    def test_full_name_present_is_match(self):
        text = ("Edwards Lifesciences Corporation 2025 Sustainability Report. "
                "This report covers our global operations.")
        self.assertTrue(self.evidence(text, "Edwards Lifesciences Corporation"))

    def test_name_present_despite_legal_suffix_difference(self):
        text = "Edwards Lifesciences is committed to human rights."
        self.assertTrue(self.evidence(text, "Edwards Lifesciences Corporation"))

    def test_ticker_alias_present_is_match(self):
        text = "This policy is issued by the company (NYSE: EW)."
        self.assertTrue(self.evidence(text, "Edwards Lifesciences Corporation",
                                      aliases=["EW"]))

    def test_wrong_company_text_is_rejected(self):
        # The Edwards case: a document that names a DIFFERENT company.
        text = ("Cisco Systems, Inc. Code of Business Conduct. Applies to all "
                "Cisco employees worldwide.")
        self.assertFalse(self.evidence(text, "Edwards Lifesciences Corporation"))

    def test_empty_text_is_rejected(self):
        self.assertFalse(self.evidence("", "Edwards Lifesciences Corporation"))


class ConfidenceCompanyDefaultTests(unittest.TestCase):
    """A3: _confident must fail closed when company_match is absent."""

    def test_missing_company_match_is_rejected(self):
        confident = _load_current_confidence_function()
        self.assertFalse(confident({
            "selected_url": "https://example.com/report.pdf",
            "topic_match": True,
            "year_match": True,
            "confidence": "high",
        }))

    def test_present_company_match_high_is_accepted(self):
        confident = _load_current_confidence_function()
        self.assertTrue(confident({
            "selected_url": "https://example.com/report.pdf",
            "topic_match": True,
            "company_match": True,
            "year_match": True,
            "confidence": "high",
        }))


class ProxySupplementRejectTests(unittest.TestCase):
    """C7: additional/soliciting/supplement proxy filings must be filename-
    rejected for a proxy-statement request; the real DEF 14A must survive."""

    def setUp(self):
        ns = {
            "re": re, "os": __import__("os"),
            "unquote": unquote, "urlparse": urlparse,
            "_matched_doc_classes": lambda q: [("proxy statement", {})],
            "_is_sample_or_template": lambda url: False,
        }
        _load_current_agent_symbols(
            ["_is_wrong_class_filename"],
            ["_WRONG_CLASS_FILENAME_MARKERS", "_STRICT_REJECT_CLASSES",
             "_CLASS_SCOPED_WRONG_MARKERS"], ns)
        self.wrong = ns["_is_wrong_class_filename"]

    def test_defa14a_is_rejected(self):
        self.assertTrue(self.wrong(
            "https://sec.gov/x/company-defa14a-2025.pdf", "proxy statement"))

    def test_additional_materials_is_rejected(self):
        self.assertTrue(self.wrong(
            "https://example.com/2025-definitive-additional-materials.pdf",
            "proxy statement"))

    def test_proxy_supplement_is_rejected(self):
        self.assertTrue(self.wrong(
            "https://example.com/2025-proxy-statement-supplement.pdf",
            "proxy statement"))

    def test_main_def14a_proxy_survives(self):
        self.assertFalse(self.wrong(
            "https://example.com/2025-definitive-proxy-statement.pdf",
            "proxy statement"))


class RecencyScoreTests(unittest.TestCase):
    """C10: for an undated recurring request the newest year must score
    strictly higher, so the agent stops keeping last year's document."""

    def setUp(self):
        ns = {
            "CURRENT_YEAR": 2026,
            "_extract_year_intent": lambda value: {
                int(y) for y in re.findall(r"\b20\d{2}\b", value or "")},
        }
        _load_current_agent_symbols(["_year_alignment_score"], [], ns)
        self.score = ns["_year_alignment_score"]

    def test_newer_beats_older_for_undated_query(self):
        newer = self.score("company sustainability report", "sr 2025")
        older = self.score("company sustainability report", "sr 2022")
        self.assertGreater(newer, older)

    def test_current_year_is_top(self):
        self.assertGreaterEqual(
            self.score("company annual report", "annual report 2026"),
            self.score("company annual report", "annual report 2025"))


class ClassScopedStorageKeyTests(unittest.TestCase):
    """B6/rerun-freshness: the S3 key must be a STABLE <company>/<report_class>/
    <filename> path (no content-hash segment), so identical class requests
    across reruns land on the same object — and every store must OVERWRITE
    rather than skip-if-exists, so a rerun always gets a fresh copy instead of
    silently no-op'ing on unchanged content or piling up a second hash-suffixed
    object on changed content. The bucket has versioning enabled, so overwriting
    never actually loses the prior bytes."""

    def setUp(self):
        self.source = (
            REPO_ROOT / "agents/download-agent/agent/agent.py"
        ).read_text(encoding="utf-8")

    def test_store_builds_class_scoped_key_without_hash(self):
        self.assertIn("_class_seg = _slug(", self.source)
        self.assertIn(
            'f"{_slug(company)}/{_class_seg}/{_safe_name(url, ctype)}"',
            self.source)
        # Regression guard: the key must NOT reintroduce a hash segment.
        self.assertNotIn(
            'f"{_slug(company)}/{_class_seg}/{digest[:12]}-', self.source)

    def test_store_always_overwrites_for_fresh_reruns(self):
        self.assertIn("_s3_put_overwrite(", self.source)
        self.assertIn("_write_provenance_upsert(", self.source)
        self.assertNotIn("_s3_put_if_missing(", self.source)
        self.assertNotIn("_write_provenance_if_missing(", self.source)


if __name__ == "__main__":
    unittest.main()
