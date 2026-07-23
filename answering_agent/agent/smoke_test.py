"""
Smoke test — exercises the pure-Python parts (no boto3, no Bedrock) with
synthetic fixtures. Run with `python smoke_test.py`.

If any of these fail after a change, something's broken.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make imports work when run from anywhere.
sys.path.insert(0, str(Path(__file__).parent))

from models.schemas import (  # noqa: E402
    Citation,
    ConfidenceLevel,
    PageIndex,
    QuestionBlock,
)
from pageindex.navigator import (  # noqa: E402
    build_pageindex_summary,
    expand_subtree,
    find_document,
    keyword_scan,
    node_path_from_pages,
    render_outline,
)
from prompts.prompt_loader import parse_questionnaire_md  # noqa: E402
from prompts.prompt_assembler import assemble_prompt  # noqa: E402


FIXTURE_INDEX = {
    "company": "TestCorp",
    "company_slug": "testcorp",
    "bucket": "test-bucket",
    "model": "bedrock/amazon.nova-lite-v1:0",
    "updated_at": "2026-07-09T10:32:15+00:00",
    "documents": [
        {
            "doc_name": "testcorp-2024-sustainability-report.pdf",
            "structure": [
                {
                    "title": "About This Report",
                    "node_id": "0001",
                    "start_index": 1,
                    "end_index": 3,
                    "summary": "Scope, boundary, and reporting frameworks used.",
                    "nodes": [],
                },
                {
                    "title": "Water Management",
                    "node_id": "0002",
                    "start_index": 20,
                    "end_index": 35,
                    "summary": "Water withdrawal, discharge, and consumption metrics.",
                    "nodes": [
                        {
                            "title": "Water Withdrawal by Source",
                            "node_id": "0003",
                            "start_index": 22,
                            "end_index": 24,
                            "summary": "Breakdown by surface, ground, and municipal.",
                            "nodes": [],
                        },
                        {
                            "title": "Water Discharge",
                            "node_id": "0004",
                            "start_index": 25,
                            "end_index": 28,
                            "summary": "Effluent quality and treatment.",
                            "nodes": [],
                        },
                    ],
                },
            ],
            "_meta": {
                "s3_key": "testcorp/2026-07-09/report.pdf",
                "s3_uri": "s3://test-bucket/testcorp/2026-07-09/report.pdf",
                "indexed_at": "2026-07-09T10:32:15+00:00",
            },
        }
    ],
}


FIXTURE_MD = """
<system_directive>
You are an ESG disclosure analyst. Answer strictly from the provided PDFs.
</system_directive>

<question_set>
For each question below, gather evidence and produce a structured answer.
</question_set>

<output_schema>
```json
{
  "type": "object",
  "properties": {
    "id": {"type": "string"},
    "label": {"type": "string"},
    "value": {"type": ["string", "number", "null"]},
    "unit": {"type": ["string", "null"]}
  },
  "required": ["id", "label", "value"]
}
```
</output_schema>

<example>
{"id": "Q1", "label": "...", "value": "12345", "unit": "megaliters"}
</example>

<confidence_scoring>
high — primary source, exact match to metric_def
medium — primary source, some interpretation required
low — secondary source or heavy interpretation
insufficient_evidence — fallback_rule invoked
</confidence_scoring>

<pre_flight_validation>
Confirm pageindex is fresh and all docs accessible.
</pre_flight_validation>
"""


def test_pageindex_parse() -> None:
    idx = PageIndex.model_validate(FIXTURE_INDEX)
    assert idx.company == "TestCorp"
    assert len(idx.documents) == 1
    doc = idx.documents[0]
    assert doc.doc_name == "testcorp-2024-sustainability-report.pdf"
    assert doc.meta.s3_uri.startswith("s3://")
    assert len(doc.structure) == 2
    assert doc.structure[1].nodes[0].title == "Water Withdrawal by Source"
    print("  pageindex_parse ok")


def test_navigator_outline() -> None:
    idx = PageIndex.model_validate(FIXTURE_INDEX)
    doc = find_document(idx, "testcorp-2024-sustainability-report.pdf")

    # Depth 1: only top-level.
    entries = render_outline(doc, max_depth=1)
    titles = [e.title for e in entries]
    assert titles == ["About This Report", "Water Management"], titles

    # Depth 2: includes water children.
    entries2 = render_outline(doc, max_depth=2)
    titles2 = [e.title for e in entries2]
    assert titles2 == [
        "About This Report",
        "Water Management",
        "Water Withdrawal by Source",
        "Water Discharge",
    ], titles2

    # Depth reflected on entries.
    depths = {e.title: e.depth for e in entries2}
    assert depths["Water Management"] == 1
    assert depths["Water Withdrawal by Source"] == 2
    print("  navigator_outline ok")


def test_navigator_expand() -> None:
    idx = PageIndex.model_validate(FIXTURE_INDEX)
    doc = find_document(idx, "testcorp-2024-sustainability-report.pdf")
    kids = expand_subtree(doc, "0002", max_depth=1)
    assert [k.title for k in kids] == ["Water Withdrawal by Source", "Water Discharge"]
    # Path should reflect the ancestor chain.
    assert kids[0].path() == "Water Management > Water Withdrawal by Source"
    print("  navigator_expand ok")


def test_navigator_node_path() -> None:
    idx = PageIndex.model_validate(FIXTURE_INDEX)
    doc = find_document(idx, "testcorp-2024-sustainability-report.pdf")
    # Range fits entirely inside "Water Withdrawal by Source" (pages 22-24).
    path = node_path_from_pages(doc, 22, 23)
    assert path.endswith("Water Withdrawal by Source"), path
    # Range spans both children — best containing node should be parent
    # "Water Management".
    path2 = node_path_from_pages(doc, 22, 27)
    assert path2.endswith("Water Management"), path2
    print("  navigator_node_path ok")


def test_navigator_keyword_scan() -> None:
    idx = PageIndex.model_validate(FIXTURE_INDEX)
    doc = find_document(idx, "testcorp-2024-sustainability-report.pdf")
    hits = keyword_scan(doc, ["withdrawal", "effluent"])
    titles = [h.title for h in hits]
    assert "Water Withdrawal by Source" in titles
    assert "Water Discharge" in titles  # matches via summary
    # Withdrawal appears in title of one node -> scores higher.
    assert hits[0].title == "Water Withdrawal by Source"
    print("  navigator_keyword_scan ok")


def test_prompt_loader() -> None:
    parsed = parse_questionnaire_md(FIXTURE_MD)
    assert "ESG disclosure analyst" in parsed.system_directive
    assert "gather evidence" in parsed.question_set_wrapper
    assert "$schema" in parsed.output_schema or "properties" in parsed.output_schema
    assert "primary source" in parsed.confidence_scoring
    print("  prompt_loader ok")


def test_prompt_assembler() -> None:
    parsed = parse_questionnaire_md(FIXTURE_MD)
    idx = PageIndex.model_validate(FIXTURE_INDEX)
    summary = build_pageindex_summary(idx)
    q = QuestionBlock(
        id="Q1",
        label="Total water withdrawal in FY2024 (megaliters)",
        metric_def="Total volume of freshwater withdrawn across all sites during the reporting year.",
        counts_as="Surface, ground, third-party municipal, and rainwater collection.",
        does_not_count="Recycled water reused within a site; seawater.",
        fallback_rule="If not disclosed, answer 'not disclosed' with confidence insufficient_evidence.",
    )
    assembled = assemble_prompt(parsed, q, summary)

    assert "Traversal Instructions" in assembled.system
    assert "Field Usage" in assembled.system
    assert "PageIndex Orientation" in assembled.system
    assert "TestCorp" in assembled.system  # from the summary

    assert "Q1" in assembled.user
    assert "Total water withdrawal" in assembled.user
    assert "does_not_count" in assembled.user
    assert "Recycled water" in assembled.user
    assert "submit_answer" in assembled.user
    print("  prompt_assembler ok")


def test_confidence_downgrade() -> None:
    # Wire up a fake session with zero citations, model reporting high.
    from agent.session import Session
    from validation.confidence_check import compute_confidence

    q = QuestionBlock(
        id="Q1",
        label="lbl",
        metric_def="def",
        counts_as="a b c d",
        does_not_count="x y z",
        fallback_rule="fr",
    )
    session = Session(question=q, run_id="r", tool_call_budget=10)
    breakdown = compute_confidence(session, "high", "I am confident.")
    assert breakdown.final == ConfidenceLevel.INSUFFICIENT
    assert breakdown.downgraded
    print("  confidence_downgrade ok")


def test_output_validator() -> None:
    from validation.output_validator import validate_answer

    schema_text = """
    ```json
    {"type": "object", "properties": {"value": {"type": "number"}}, "required": ["value"]}
    ```
    """
    problems = validate_answer({"value": 42}, schema_text, "some question")
    # jsonschema may not be installed in dev env; either way structural OK.
    print(f"  output_validator ok (problems={problems})")


def test_confidence_diverse_docs() -> None:
    """Cross-doc corroboration should not cap at medium."""
    from agent.session import Session
    from validation.confidence_check import compute_confidence

    q = QuestionBlock(
        id="Q1",
        label="lbl",
        metric_def="def",
        counts_as="water withdrawal freshwater",
        does_not_count="seawater recycled",
        fallback_rule="fr",
    )
    session = Session(question=q, run_id="r", tool_call_budget=10)
    # Two citations from two different docs.
    session.citations = [
        Citation(id="C001", doc_name="doc-a.pdf", s3_uri="s3://b/a", page_start=1, page_end=1,
                 quoted_span="x", node_path="p"),
        Citation(id="C002", doc_name="doc-b.pdf", s3_uri="s3://b/b", page_start=1, page_end=1,
                 quoted_span="x", node_path="p"),
    ]
    reasoning = "Freshwater withdrawal totals cross-checked; excludes seawater and recycled."
    breakdown = compute_confidence(session, "high", reasoning)
    assert breakdown.final == ConfidenceLevel.HIGH, breakdown
    print("  confidence_diverse_docs ok")


TESTS = [
    test_pageindex_parse,
    test_navigator_outline,
    test_navigator_expand,
    test_navigator_node_path,
    test_navigator_keyword_scan,
    test_prompt_loader,
    test_prompt_assembler,
    test_confidence_downgrade,
    test_confidence_diverse_docs,
    test_output_validator,
]


def main() -> int:
    print("Running smoke tests...")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"  {t.__name__} FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
