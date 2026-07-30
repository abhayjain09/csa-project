"""
Dispatch a tool call to the right handler.

Wraps every call in try/except so an unexpected exception surfaces as a
ToolResult(ERROR) instead of killing the loop. Also updates the
diminishing-returns counter based on fetch_pages outcomes.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from agent.session import Session
from models.schemas import PageIndex, ToolCallRecord, ToolResult, ToolStatus
from pdf.page_extractor import PageExtractor
from tools.definitions import TOOL_NAMES
from tools.handlers import HANDLERS

logger = logging.getLogger(__name__)


def _preview(data: Any, limit: int = 500) -> str:
    s = str(data)
    if len(s) > limit:
        return s[:limit] + f"... [truncated, {len(s)} chars total]"
    return s


def dispatch(
    session: Session,
    index: PageIndex,
    extractor: PageExtractor,
    tool_name: str,
    tool_input: dict[str, Any],
) -> ToolResult:
    """Route to the correct handler, record the call, return the result."""
    started = time.time()
    error: str | None = None

    if tool_name not in TOOL_NAMES:
        result = ToolResult(
            status=ToolStatus.ERROR,
            message=f"Unknown tool '{tool_name}'. Available: {sorted(TOOL_NAMES)}",
        )
    else:
        handler = HANDLERS[tool_name]
        try:
            # Before the call: if this is a fetch_pages, snapshot the
            # citation count so we can detect "yielded no new citation".
            pre_citations = len(session.citations)

            result = handler(session, index, extractor, tool_input)

            # After the call: update diminishing-returns counter.
            if tool_name == "fetch_pages" and result.status != ToolStatus.ERROR:
                if len(session.citations) == pre_citations:
                    session.consecutive_empty_fetches += 1
                else:
                    session.consecutive_empty_fetches = 0
            # record_citation resets its own counter in the handler; other
            # tools don't affect it.
        except Exception as e:  # noqa: BLE001
            logger.exception("dispatcher_unexpected_error", extra={"tool": tool_name})
            error = f"Internal error in {tool_name}: {e}"
            result = ToolResult(status=ToolStatus.ERROR, message=error)

    latency_ms = int((time.time() - started) * 1000)
    session.tool_calls.append(
        ToolCallRecord(
            iteration=session.calls_used + 1,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output_preview=_preview(
                result.model_dump(exclude_none=True) if hasattr(result, "model_dump") else result
            ),
            latency_ms=latency_ms,
            error=result.message if result.status == ToolStatus.ERROR else None,
        )
    )
    return result
