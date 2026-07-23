"""
Per-question mutable state carried through the ReAct loop.

One `Session` is created per QUESTION_BLOCK. Nothing here is thread-safe —
it's owned by exactly one loop.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from models.schemas import Citation, QuestionBlock, ToolCallRecord


@dataclass
class FetchedRange:
    """A record of pages the agent has actually fetched. Used to validate
    citations — the model can only cite what it read."""

    doc_name: str
    page_start: int
    page_end: int
    pages_text: dict[int, str]  # page_number -> text, for substring checks


@dataclass
class Session:
    """State for one question's ReAct loop."""

    question: QuestionBlock
    run_id: str
    tool_call_budget: int
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)

    # Populated as the loop runs.
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    fetched_ranges: list[FetchedRange] = field(default_factory=list)
    submitted_answer: dict[str, Any] | None = None

    # Consecutive fetch_pages calls that yielded no new citation. Used for
    # diminishing-returns nudges.
    consecutive_empty_fetches: int = 0

    # Whether the schema-retry nudge has already been used.
    schema_retry_used: bool = False

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def calls_used(self) -> int:
        return len(self.tool_calls)

    @property
    def calls_remaining(self) -> int:
        return max(0, self.tool_call_budget - self.calls_used)

    @property
    def budget_exhausted(self) -> bool:
        return self.calls_used >= self.tool_call_budget

    def budget_pct_used(self) -> float:
        if self.tool_call_budget == 0:
            return 1.0
        return self.calls_used / self.tool_call_budget

    def elapsed_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)

    # ------------------------------------------------------------------
    # Citation buffer
    # ------------------------------------------------------------------

    def next_citation_id(self) -> str:
        return f"C{len(self.citations) + 1:03d}"

    def add_citation(self, c: Citation) -> None:
        self.citations.append(c)

    def get_citation(self, citation_id: str) -> Citation | None:
        for c in self.citations:
            if c.id == citation_id:
                return c
        return None

    # ------------------------------------------------------------------
    # Fetch tracking
    # ------------------------------------------------------------------

    def record_fetched_range(
        self, doc_name: str, page_start: int, page_end: int, pages_text: dict[int, str]
    ) -> None:
        self.fetched_ranges.append(
            FetchedRange(
                doc_name=doc_name,
                page_start=page_start,
                page_end=page_end,
                pages_text=pages_text,
            )
        )

    def find_fetched_text(
        self, doc_name: str, page_start: int, page_end: int
    ) -> dict[int, str] | None:
        """Return the concatenated fetched text if this range (or a
        superset) has been fetched. Used to validate record_citation.
        """
        target_doc = doc_name.lower().strip()
        for fr in self.fetched_ranges:
            if (
                fr.doc_name.lower().strip() == target_doc
                and fr.page_start <= page_start
                and fr.page_end >= page_end
            ):
                return {
                    p: fr.pages_text[p]
                    for p in range(page_start, page_end + 1)
                    if p in fr.pages_text
                }
        return None
