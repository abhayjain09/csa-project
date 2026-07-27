"""
Network-free smoke test.

Stubs pydantic and boto3 just enough to import our modules and exercise the
pure-logic paths (navigator, prompt_loader, prompt_assembler). Real runtime
uses actual pydantic — nothing here modifies our code.

Run with: python offline_smoke_test.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path


# --- Stub pydantic minimally so the imports work ---------------------------


def _install_pydantic_stub() -> None:
    pyd = types.ModuleType("pydantic")

    class ConfigDict(dict):
        pass

    def Field(default=None, **kwargs):
        # If default_factory given, return sentinel that BaseModel will call.
        if "default_factory" in kwargs:
            return _FieldSentinel(default_factory=kwargs["default_factory"])
        return _FieldSentinel(default=default)

    class _FieldSentinel:
        def __init__(self, default=None, default_factory=None):
            self.default = default
            self.default_factory = default_factory

    def field_validator(*_args, **_kwargs):
        def decorator(fn):
            return fn
        return decorator

    class BaseModel:
        """Extremely reduced BaseModel that supports:
        - keyword construction
        - default values from Field
        - .model_validate(dict) with basic alias handling
        - .model_dump()
        """

        model_config = ConfigDict()

        def __init__(self, **kwargs):
            annotations = getattr(self.__class__, "__annotations__", {})
            for name in annotations:
                if name in kwargs:
                    setattr(self, name, kwargs[name])
                elif hasattr(self.__class__, name):
                    default_val = getattr(self.__class__, name)
                    if isinstance(default_val, _FieldSentinel):
                        if default_val.default_factory is not None:
                            setattr(self, name, default_val.default_factory())
                        else:
                            setattr(self, name, default_val.default)
                    else:
                        setattr(self, name, default_val)
                else:
                    setattr(self, name, None)

        @classmethod
        def model_validate(cls, data):
            if not isinstance(data, dict):
                raise ValueError(f"Expected dict, got {type(data).__name__}")

            annotations = getattr(cls, "__annotations__", {})
            # Resolve string annotations (from __future__ annotations).
            import typing as _typing
            try:
                resolved = _typing.get_type_hints(cls)
            except Exception:
                resolved = {}

            kwargs = {}
            for name, ann in annotations.items():
                # Prefer the resolved (evaluated) type if available.
                real_ann = resolved.get(name, ann)
                src_key = name
                # Hard-coded alias handling for '_meta'.
                if name == "meta" and "_meta" in data:
                    src_key = "_meta"
                if src_key in data:
                    raw = data[src_key]
                    kwargs[name] = _coerce(raw, real_ann)
                elif hasattr(cls, name):
                    dv = getattr(cls, name)
                    if isinstance(dv, _FieldSentinel):
                        if dv.default_factory is not None:
                            kwargs[name] = dv.default_factory()
                        else:
                            kwargs[name] = dv.default
            return cls(**kwargs)

        def model_dump(self, **_kwargs):
            out = {}
            for name in getattr(self.__class__, "__annotations__", {}):
                val = getattr(self, name, None)
                out[name] = _dump(val)
            return out

        @classmethod
        def model_rebuild(cls):
            pass

    def _coerce(raw, ann):
        # Handle List[SomeModel], nested BaseModel, etc. — very loose.
        origin = getattr(ann, "__origin__", None)
        args = getattr(ann, "__args__", ())
        if origin is list and args:
            inner = args[0]
            if isinstance(raw, list):
                return [_coerce(x, inner) for x in raw]
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            if isinstance(raw, dict):
                return ann.model_validate(raw)
        return raw

    def _dump(val):
        if isinstance(val, BaseModel):
            return val.model_dump()
        if isinstance(val, list):
            return [_dump(x) for x in val]
        if isinstance(val, dict):
            return {k: _dump(v) for k, v in val.items()}
        return val

    pyd.BaseModel = BaseModel
    pyd.ConfigDict = ConfigDict
    pyd.Field = Field
    pyd.field_validator = field_validator
    sys.modules["pydantic"] = pyd


def _install_boto3_stub() -> None:
    # Just enough so imports don't blow up.
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *a, **k: None  # type: ignore
    sys.modules["boto3"] = boto3

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    class Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
    botocore_config.Config = Config
    sys.modules["botocore"] = botocore
    sys.modules["botocore.config"] = botocore_config

    botocore_exc = types.ModuleType("botocore.exceptions")
    class ClientError(Exception): ...
    class BotoCoreError(Exception): ...
    botocore_exc.ClientError = ClientError
    botocore_exc.BotoCoreError = BotoCoreError
    sys.modules["botocore.exceptions"] = botocore_exc


_install_pydantic_stub()
_install_boto3_stub()


sys.path.insert(0, str(Path(__file__).parent))

# --- Tests -----------------------------------------------------------------

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
                    "summary": "Scope and framework.",
                    "nodes": [],
                },
                {
                    "title": "Water Management",
                    "node_id": "0002",
                    "start_index": 20,
                    "end_index": 35,
                    "summary": "Water withdrawal, discharge, consumption.",
                    "nodes": [
                        {
                            "title": "Water Withdrawal by Source",
                            "node_id": "0003",
                            "start_index": 22,
                            "end_index": 24,
                            "summary": "Breakdown by surface, ground, municipal.",
                            "nodes": [],
                        },
                        {
                            "title": "Water Discharge",
                            "node_id": "0004",
                            "start_index": 25,
                            "end_index": 28,
                            "summary": "Effluent quality.",
                            "nodes": [],
                        },
                    ],
                },
            ],
            "_meta": {
                "s3_key": "testcorp/report.pdf",
                "s3_uri": "s3://test-bucket/testcorp/report.pdf",
                "indexed_at": "2026-07-09T10:32:15+00:00",
            },
        }
    ],
}


FIXTURE_MD = """
<system_directive>
You are an ESG analyst.
</system_directive>

<question_set>
For each question, gather evidence.
</question_set>

<output_schema>
```json
{"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
```
</output_schema>

<example>
Example goes here.
</example>

<confidence_scoring>
high/medium/low/insufficient_evidence
</confidence_scoring>

<pre_flight_validation>
Confirm freshness.
</pre_flight_validation>
"""


def run() -> int:
    from models.schemas import PageIndex, QuestionBlock
    from pageindex.navigator import (
        build_pageindex_summary,
        expand_subtree,
        find_document,
        keyword_scan,
        node_path_from_pages,
        render_outline,
    )
    from prompts.prompt_loader import parse_questionnaire_md
    from prompts.prompt_assembler import assemble_prompt

    failures = 0

    def check(name, cond, detail=""):
        nonlocal failures
        if cond:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}  {detail}")
            failures += 1

    # PageIndex parse
    idx = PageIndex.model_validate(FIXTURE_INDEX)
    check("pageindex.company", idx.company == "TestCorp")
    check("pageindex.docs_count", len(idx.documents) == 1)
    doc = idx.documents[0]
    check("pageindex.meta.s3_uri", doc.meta.s3_uri.startswith("s3://"))
    check("pageindex.nested_node",
          doc.structure[1].nodes[0].title == "Water Withdrawal by Source")

    # find_document
    d = find_document(idx, "testcorp-2024-sustainability-report.pdf")
    check("find_document.exact", d.doc_name == doc.doc_name)

    try:
        find_document(idx, "nope.pdf")
        check("find_document.missing_raises", False, "should have raised")
    except KeyError:
        check("find_document.missing_raises", True)

    # render_outline depth=1
    entries = render_outline(doc, max_depth=1)
    titles = [e.title for e in entries]
    check("outline.depth1", titles == ["About This Report", "Water Management"], f"got {titles}")

    # render_outline depth=2
    entries2 = render_outline(doc, max_depth=2)
    titles2 = [e.title for e in entries2]
    expected2 = ["About This Report", "Water Management",
                 "Water Withdrawal by Source", "Water Discharge"]
    check("outline.depth2", titles2 == expected2, f"got {titles2}")

    # depths correct
    depths = {e.title: e.depth for e in entries2}
    check("outline.depth_top", depths["Water Management"] == 1)
    check("outline.depth_child", depths["Water Withdrawal by Source"] == 2)

    # path renders ancestors
    child_entry = [e for e in entries2 if e.title == "Water Withdrawal by Source"][0]
    check("outline.path",
          child_entry.path() == "Water Management > Water Withdrawal by Source",
          f"got {child_entry.path()}")

    # expand_subtree
    kids = expand_subtree(doc, "0002", max_depth=1)
    check("expand.children_count", len(kids) == 2)
    check("expand.first_child", kids[0].title == "Water Withdrawal by Source")

    # expand missing node
    try:
        expand_subtree(doc, "9999", max_depth=1)
        check("expand.missing_raises", False)
    except KeyError:
        check("expand.missing_raises", True)

    # expand leaf (no children)
    leaf_result = expand_subtree(doc, "0001", max_depth=1)
    check("expand.leaf_empty", leaf_result == [])

    # node_path_from_pages: within child
    p1 = node_path_from_pages(doc, 22, 23)
    check("node_path.deepest",
          p1.endswith("Water Withdrawal by Source"), f"got {p1}")
    # spans siblings
    p2 = node_path_from_pages(doc, 22, 27)
    check("node_path.parent_span",
          p2.endswith("Water Management") and not p2.endswith("Withdrawal by Source"),
          f"got {p2}")

    # keyword_scan
    hits = keyword_scan(doc, ["withdrawal", "effluent"])
    hit_titles = [h.title for h in hits]
    check("keyword.finds_withdrawal", "Water Withdrawal by Source" in hit_titles)
    check("keyword.finds_effluent_via_summary", "Water Discharge" in hit_titles)
    check("keyword.title_ranks_higher", hits[0].title == "Water Withdrawal by Source",
          f"top: {hits[0].title}")

    # empty terms
    check("keyword.empty_terms", keyword_scan(doc, []) == [])

    # build_pageindex_summary
    summary = build_pageindex_summary(idx)
    check("summary.company", "TestCorp" in summary)
    check("summary.docs", "testcorp-2024-sustainability-report.pdf" in summary)
    check("summary.top_sections", "Water Management" in summary)

    # prompt_loader
    parsed = parse_questionnaire_md(FIXTURE_MD)
    check("md.system_directive", "ESG analyst" in parsed.system_directive)
    check("md.wrapper", "gather evidence" in parsed.question_set_wrapper)
    check("md.output_schema", "properties" in parsed.output_schema)
    check("md.confidence", "insufficient_evidence" in parsed.confidence_scoring)

    # missing section
    try:
        parse_questionnaire_md("<system_directive>x</system_directive>")
        check("md.missing_raises", False)
    except ValueError:
        check("md.missing_raises", True)

    # prompt_assembler
    q = QuestionBlock(
        id="Q1",
        label="Total water withdrawal in FY2024 (megaliters)",
        metric_def="Total volume of freshwater withdrawn.",
        counts_as="Surface, ground, municipal, rainwater.",
        does_not_count="Recycled water; seawater.",
        fallback_rule="If not disclosed, answer 'not disclosed'.",
    )
    assembled = assemble_prompt(parsed, q, summary)
    check("prompt.system.traversal", "Traversal Instructions" in assembled.system)
    check("prompt.system.field_usage", "Field Usage" in assembled.system)
    check("prompt.system.orientation", "TestCorp" in assembled.system)
    check("prompt.user.qid", "Q1" in assembled.user)
    check("prompt.user.label", "Total water withdrawal in FY2024" in assembled.user)
    check("prompt.user.exclusions", "Recycled water" in assembled.user)
    check("prompt.user.begin", "Begin." in assembled.user)

    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILURES'}")
    return failures


if __name__ == "__main__":
    sys.exit(0 if run() == 0 else 1)
