import ast
import importlib.util
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _coverage_module():
    path = ROOT / "agents/download-agent/agent/annual_coverage.py"
    spec = importlib.util.spec_from_file_location("annual_coverage_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app_helpers():
    path = ROOT / "co-analyst-application/app/backend/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = []
    wanted = {
        "_infer_report_class",
        "_chunk_web_queries",
        "_partition_annual_report_phase",
        "_build_chunk_payload",
        "_annual_report_manifest_key",
        "_annual_report_failed_classes",
        "_apply_annual_report_references",
    }
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name)
                        and target.id in {
                            "_REPORT_CLASS_ALIASES",
                            "ANNUAL_REPORT_REFERENCE_CLASSES",
                        }
                        for target in node.targets)):
            nodes.append(node)
        elif (isinstance(node, ast.FunctionDef) and node.name in wanted):
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "re": re,
        "_agent_slug": lambda value: re.sub(
            r"[^a-z0-9]+", "-", value.lower()).strip("-"),
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


class AnnualReferenceTests(unittest.TestCase):
    def setUp(self):
        self.helpers = _app_helpers()
        self.manifest = {
            "annual_report_s3_key": "acme/annual-report/acme-2025.pdf",
            "annual_report_s3_uri": (
                "s3://reports/acme/annual-report/acme-2025.pdf"),
            "manifest_s3_key": (
                "acme/_manifests/annual-report-coverage.json"),
            "coverage": {
                "code of conduct": {
                    "match": "substantive_section",
                    "heading": "Business Conduct and Ethics",
                    "page_start": 72,
                    "page_end": 78,
                    "confidence": "high",
                    "evidence": "The section sets out employee duties and controls.",
                }
            },
        }

    def test_clean_miss_becomes_typed_reference(self):
        result = {
            "error": None,
            "results": [{
                "query": "site:acme.com Code of Conduct",
                "status": "failed",
                "reason": "no class-verified document found",
                "annual_report_reference_eligible": True,
            }],
            "failures": [{
                "query": "site:acme.com Code of Conduct",
                "status": "failed",
            }],
        }
        updated = self.helpers["_apply_annual_report_references"](
            result, "Acme Inc", self.manifest)
        row = updated["results"][0]
        self.assertEqual(row["status"], "referenced_in_existing_document")
        self.assertEqual(row["referenced_s3_key"], self.manifest["annual_report_s3_key"])
        self.assertEqual((row["page_start"], row["page_end"]), (72, 78))
        self.assertEqual(updated["failures"], [])

    def test_untyped_failure_is_not_an_annual_report_reference(self):
        result = {
            "error": None,
            "results": [{
                "query": "site:acme.com Code of Conduct",
                "status": "failed",
                "reason": "transport or result-mapping failure",
            }],
            "failures": [],
        }
        updated = self.helpers["_apply_annual_report_references"](
            result, "Acme Inc", self.manifest)
        self.assertEqual(updated["results"][0]["status"], "failed")

    def test_waf_timeout_and_transport_error_never_become_references(self):
        for status in (
            "blocked_by_source_waf",
            "browser_retry_queued",
            "timed_out_pending_check",
        ):
            with self.subTest(status=status):
                result = {
                    "error": None,
                    "results": [{
                        "query": "site:acme.com Code of Conduct",
                        "status": status,
                    }],
                    "failures": [],
                }
                updated = self.helpers["_apply_annual_report_references"](
                    result, "Acme Inc", self.manifest)
                self.assertEqual(updated["results"][0]["status"], status)

        transport_error = {
            "error": "gateway unavailable",
            "results": [{
                "query": "site:acme.com Code of Conduct",
                "status": "failed",
            }],
            "failures": [],
        }
        updated = self.helpers["_apply_annual_report_references"](
            transport_error, "Acme Inc", self.manifest)
        self.assertEqual(updated["results"][0]["status"], "failed")

    def test_manifest_has_stable_company_scoped_path(self):
        key = self.helpers["_annual_report_manifest_key"]("Acme, Inc.")
        self.assertEqual(key, "acme-inc/_manifests/annual-report-coverage.json")

    def test_annual_report_isolated_in_phase_one_regardless_of_input_order(self):
        record = {
            "web_query1": "site:acme.com Code of Conduct",
            "web_query2": "site:acme.com Sustainability Report",
            "web_query3": "site:acme.com Annual Report",
            "web_query4": "site:acme.com Tax Strategy",
        }
        annual, remaining = self.helpers["_partition_annual_report_phase"](
            record, "Acme Inc", 2)
        self.assertEqual(annual, [["site:acme.com Annual Report"]])
        self.assertEqual(remaining, [
            ["site:acme.com Code of Conduct",
             "site:acme.com Sustainability Report"],
            ["site:acme.com Tax Strategy"],
        ])

    def test_phase_two_payload_requires_standalone_documents(self):
        payload = self.helpers["_build_chunk_payload"](
            "Acme Inc", "run-1", "", ["site:acme.com Code of Conduct"], 2)
        self.assertTrue(payload["reports"][0]["standalone_only"])

    def test_coverage_topics_are_collected_only_after_clean_misses(self):
        chunks = [
            {
                "error": None,
                "results": [
                    {
                        "query": "site:acme.com Code of Conduct",
                        "status": "failed",
                        "annual_report_reference_eligible": True,
                    },
                    {
                        "query": "site:acme.com Human Rights Policy",
                        "status": "downloaded",
                    },
                    {
                        "query": "site:acme.com Supplier Code of Conduct",
                        "status": "blocked_by_source_waf",
                        "annual_report_reference_eligible": False,
                    },
                ],
            },
            {
                "error": "gateway unavailable",
                "results": [{
                    "query": "site:acme.com Environmental Policy",
                    "status": "failed",
                    "annual_report_reference_eligible": True,
                }],
            },
        ]
        classes = self.helpers["_annual_report_failed_classes"](
            chunks, "Acme Inc")
        self.assertEqual(classes, ["code of conduct"])

    def test_coverage_call_is_after_parallel_search_phase(self):
        path = ROOT / "co-analyst-application/app/backend/app.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        invoke = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_do_invoke_inner")
        body = ast.get_source_segment(source, invoke)
        parallel = body.index("with ThreadPoolExecutor")
        coverage = body.index(
            "annual_coverage_manifest = _create_annual_report_coverage_manifest")
        self.assertGreater(coverage, parallel)
        self.assertEqual(
            body.count("_create_annual_report_coverage_manifest("), 1)

    def test_coverage_invokes_download_agent_not_pageindex(self):
        app_path = ROOT / "co-analyst-application/app/backend/app.py"
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        create_manifest = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_create_annual_report_coverage_manifest")
        body = ast.get_source_segment(source, create_manifest)
        self.assertIn("AGENT_RUNTIME_ARN, AGENT_QUALIFIER", body)
        self.assertNotIn("PAGEINDEX_RUNTIME_ARN", body)

        pageindex_source = (
            ROOT / "agents/pageindex-agent/runtime/runtime_handler.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("annual_report_coverage", pageindex_source)

        agent_path = ROOT / "agents/download-agent/agent/agent.py"
        agent_source = agent_path.read_text(encoding="utf-8")
        agent_tree = ast.parse(agent_source)
        invoke_sync = next(
            node for node in agent_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_invoke_sync")
        invoke_body = ast.get_source_segment(agent_source, invoke_sync)
        self.assertIn("annual_coverage.run(", invoke_body)
        self.assertLess(
            invoke_body.index('get("mode") == "annual_report_coverage"'),
            invoke_body.index("run_id ="),
        )

        dockerfile = (
            ROOT / "agents/download-agent/agent/Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("annual_coverage.py", dockerfile)


class CoverageClassifierTests(unittest.TestCase):
    @staticmethod
    def _headings():
        return [{
            "title": "Governance",
            "path": "Governance",
            "page_start": 60,
            "page_end": 90,
            "summary": "Governance overview.",
            "source": "pdf_bookmark",
        }, {
                "title": "Business Conduct and Ethics",
                "path": "Business Conduct and Ethics",
                "page_start": 72,
                "page_end": 78,
                "summary": (
                    "Employee duties, reporting channels, conflicts controls, "
                    "investigation procedures and disciplinary consequences."),
                "source": "printed_toc",
        }]

    def test_accepts_only_high_confidence_exact_indexed_heading(self):
        model_result = {
            "coverage": {
                "code of conduct": {
                    "match": "substantive_section",
                    "heading": "Business Conduct and Ethics",
                    "page_start": 72,
                    "page_end": 78,
                    "confidence": "high",
                    "evidence": "Sustained procedures and employee duties.",
                },
                "risk management policy": {
                    "match": "substantive_section",
                    "heading": "Governance",
                    "page_start": 60,
                    "page_end": 90,
                    "confidence": "medium",
                    "evidence": "Only general governance.",
                },
            }
        }
        result = _coverage_module().classify_coverage(
            self._headings(),
            ["code of conduct", "risk management policy"],
            lambda _prompt, _max_tokens: json.dumps(model_result),
        )
        self.assertEqual(set(result["coverage"]), {"code of conduct"})

    def test_rejects_invented_heading_or_page_range(self):
        model_result = {
            "coverage": {
                "code of conduct": {
                    "match": "substantive_section",
                    "heading": "Code of Conduct",
                    "page_start": 10,
                    "page_end": 12,
                    "confidence": "high",
                    "evidence": "Invented by model.",
                }
            }
        }
        result = _coverage_module().classify_coverage(
            self._headings(), ["code of conduct"],
            lambda _prompt, _max_tokens: json.dumps(model_result),
        )
        self.assertEqual(result["coverage"], {})

    def test_printed_toc_heading_is_grounded_on_physical_page(self):
        module = _coverage_module()

        class FakePage:
            def __init__(self, text):
                self.text = text

            def extract_text(self, **_kwargs):
                return self.text

        class FakeReader:
            outline = []
            pages = [
                FakePage(
                    "Contents\nBusiness Conduct and Ethics ........ 3"),
                FakePage("General governance overview"),
                FakePage(
                    "Business Conduct and Ethics\n"
                    "Employee duties, reporting channels and investigations"),
            ]

        original = module.PdfReader
        module.PdfReader = lambda *_args, **_kwargs: FakeReader()
        try:
            headings = module.extract_heading_index(b"unused fake PDF")
        finally:
            module.PdfReader = original

        grounded = [
            item for item in headings
            if item["title"] == "Business Conduct and Ethics"
            and item["source"] == "printed_toc"
        ]
        self.assertEqual(len(grounded), 1)
        self.assertEqual(grounded[0]["page_start"], 3)


if __name__ == "__main__":
    unittest.main()
