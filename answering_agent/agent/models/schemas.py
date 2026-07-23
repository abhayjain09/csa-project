"""
Core data models for the agent.

Everything the pipeline exchanges — inputs, tool payloads, citations, answers,
final results — is defined here. Keeping this file the single source of truth
prevents drift between modules.

Design notes:
- Payload inputs (QuestionBlock, RuntimePayload) are lenient on unknown fields
  so upstream systems can add metadata without breaking us.
- Internal artifacts (Citation, ToolCallRecord, AgentResult) are strict.
- Citations carry both s3_uri and node_path because node_id is likely
  ephemeral across reindexes.
- question_set in RuntimePayload is optional — if not supplied the pipeline
  parses it from the questionnaire_md file using parse_question_set.py.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Inputs from the AgentCore payload
# ---------------------------------------------------------------------------


class QuestionBlock(BaseModel):
    """One question in the questionnaire."""

    model_config = ConfigDict(extra="allow")

    id: str
    label: str = Field(..., description="Exact text used in the 'Question' column of output.")
    metric_def: str
    counts_as: str
    does_not_count: str
    fallback_rule: str

    @field_validator("id", "label", "metric_def", "counts_as", "does_not_count", "fallback_rule")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("field must be non-empty")
        return v


class S3Ref(BaseModel):
    """Pointer to an object in S3."""

    s3_uri: str

    @field_validator("s3_uri")
    @classmethod
    def _valid_uri(cls, v: str) -> str:
        if not v.startswith("s3://"):
            raise ValueError("s3_uri must start with s3://")
        return v


class RuntimePayload(BaseModel):
    """The full input to the AgentCore entrypoint.

    question_set is optional — if omitted the pipeline parses it from the
    questionnaire_md file using the QUESTION_BLOCK section.
    If supplied it overrides whatever is in the MD file.
    """

    model_config = ConfigDict(extra="allow")

    pageindex: dict[str, Any] | S3Ref
    questionnaire_md: str | S3Ref
    # Optional — parsed from MD if not supplied.
    question_set: list[QuestionBlock] | None = None
    run_id: str | None = None
    # Optional company name — passed through to RunResult for DynamoDB storage.
    company: str | None = None

    @model_validator(mode="before")
    @classmethod
    def coerce_s3refs(cls, values: dict) -> dict:
        """Detect {"s3_uri": "..."} dicts and coerce them to S3Ref objects
        so isinstance(source, S3Ref) checks work correctly in pipeline.py."""
        pi = values.get("pageindex")
        if isinstance(pi, dict) and set(pi.keys()) == {"s3_uri"}:
            values["pageindex"] = S3Ref(s3_uri=pi["s3_uri"])

        qmd = values.get("questionnaire_md")
        if isinstance(qmd, dict) and set(qmd.keys()) == {"s3_uri"}:
            values["questionnaire_md"] = S3Ref(s3_uri=qmd["s3_uri"])

        return values


# ---------------------------------------------------------------------------
# pageIndex tree
# ---------------------------------------------------------------------------


class PageIndexNode(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str
    node_id: str
    start_index: int = Field(..., ge=1)
    end_index: int = Field(..., ge=1)
    summary: str = ""
    nodes: list[PageIndexNode] = Field(default_factory=list)

    @field_validator("end_index")
    @classmethod
    def _end_gte_start(cls, v: int, info) -> int:
        start = info.data.get("start_index")
        if start is not None and v < start:
            raise ValueError(f"end_index ({v}) < start_index ({start})")
        return v


class PageIndexDocMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    s3_key: str
    s3_uri: str
    indexed_at: str | None = None


class PageIndexDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    doc_name: str
    structure: list[PageIndexNode]
    meta: PageIndexDocMeta = Field(..., alias="_meta")


class PageIndex(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    company: str
    company_slug: str
    bucket: str
    model: str | None = None
    updated_at: str
    documents: list[PageIndexDocument] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Parsed MD sections
# ---------------------------------------------------------------------------


class ParsedQuestionnaire(BaseModel):
    """The MD file split into its six sections.

    question_set_wrapper is the raw content of the <question_set> tag,
    including the --- QUESTION_BLOCK delimiter and all question text.
    The prompt assembler strips everything from the delimiter onwards
    so only the instructional text above it goes into the prompt.
    The pipeline uses parse_question_set_from_text() on this field to
    extract the actual QuestionBlock list.
    """

    system_directive: str
    question_set_wrapper: str
    output_schema: str
    example: str
    confidence_scoring: str
    pre_flight_validation: str


# ---------------------------------------------------------------------------
# Agent session artifacts
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    id: str
    doc_name: str
    s3_uri: str
    page_start: int = Field(..., ge=1)
    page_end: int = Field(..., ge=1)
    quoted_span: str
    node_path: str
    recorded_at: datetime = Field(default_factory=datetime.utcnow)


class ToolCallRecord(BaseModel):
    iteration: int
    tool_name: str
    tool_input: dict[str, Any]
    tool_output_preview: str
    latency_ms: int
    error: str | None = None


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient_evidence"


class ConfidenceBreakdown(BaseModel):
    model_reported: ConfidenceLevel
    computed_floor: ConfidenceLevel
    final: ConfidenceLevel
    reasons: list[str]
    downgraded: bool = False


class AnswerFlag(str, Enum):
    FALLBACK_FIRED = "fallback_fired"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONFIDENCE_DOWNGRADED = "confidence_downgraded"
    NO_CITATIONS = "no_citations"
    SCHEMA_RETRY = "schema_retry"


class QuestionResult(BaseModel):
    question_id: str
    question_label: str
    answer_payload: dict[str, Any]
    citations: list[Citation]
    confidence: ConfidenceBreakdown
    tool_calls_used: int
    latency_ms: int
    flags: list[AnswerFlag] = Field(default_factory=list)
    error: str | None = None


class RunResult(BaseModel):
    """Top-level return value from the pipeline.

    company is included so app.py can store results keyed by company
    without needing to re-derive it from the pageIndex.
    """

    run_id: str
    company: str
    company_slug: str = ""
    pageindex_updated_at: str
    md_file: str = ""          # which questionnaire MD file this result is for
    category: str = ""         # human-readable category derived from md_file name
    results: list[QuestionResult]
    summary_stats: dict[str, Any]


# ---------------------------------------------------------------------------
# Tool I/O envelopes
# ---------------------------------------------------------------------------


class ToolStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    WARNING = "warning"


class ToolResult(BaseModel):
    status: ToolStatus
    data: dict[str, Any] | list[Any] | str | None = None
    message: str | None = None


# Forward-ref cleanup for the self-referencing PageIndexNode.
PageIndexNode.model_rebuild()

