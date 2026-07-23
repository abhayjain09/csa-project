"""
Concrete implementations of each tool.

Every handler:
- Takes (session, index, extractor, tool_input) and returns a ToolResult.
- Catches its own errors and returns them as ToolResult(status=ERROR, ...) —
  never raises. That way the dispatcher just serializes and the model gets
  a chance to self-correct.

The record_citation handler is the integrity guard: it verifies the quoted
span was actually in a fetched page before accepting the citation.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from agent.session import Session
from models.schemas import Citation, PageIndex, ToolResult, ToolStatus
from pageindex.navigator import (
    DocumentNotFoundError,
    NodeNotFoundError,
    build_pageindex_summary,
    expand_subtree,
    find_document,
    keyword_scan,
    node_path_from_pages,
    render_outline,
)
from pdf.page_extractor import PageExtractor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _err(msg: str) -> ToolResult:
    return ToolResult(status=ToolStatus.ERROR, message=msg)


def _normalize_for_match(s: str) -> str:
    """Collapse whitespace so quote matching survives line-break noise from
    PDF extraction."""
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def handle_list_documents(
    session: Session,
    index: PageIndex,
    extractor: PageExtractor,
    tool_input: dict[str, Any],
) -> ToolResult:
    del session, extractor, tool_input  # unused

    docs = []
    for doc in index.documents:
        top = [
            {
                "node_id": n.node_id,
                "title": n.title,
                "summary": n.summary,
                "pages": f"{n.start_index}-{n.end_index}",
            }
            for n in doc.structure
        ]
        docs.append({"doc_name": doc.doc_name, "top_sections": top})

    return ToolResult(
        status=ToolStatus.OK,
        data={
            "company": index.company,
            "documents": docs,
            "orientation": build_pageindex_summary(index),
        },
    )


def handle_get_outline(
    session: Session,
    index: PageIndex,
    extractor: PageExtractor,
    tool_input: dict[str, Any],
) -> ToolResult:
    del session, extractor
    doc_name = tool_input.get("doc_name", "")
    depth = int(tool_input.get("depth", 1))
    try:
        doc = find_document(index, doc_name)
    except DocumentNotFoundError as e:
        return _err(str(e))
    try:
        entries = render_outline(doc, max_depth=depth)
    except ValueError as e:
        return _err(str(e))
    return ToolResult(
        status=ToolStatus.OK,
        data={
            "doc_name": doc.doc_name,
            "depth": depth,
            "entries": [e.to_dict() for e in entries],
        },
    )


def handle_expand_node(
    session: Session,
    index: PageIndex,
    extractor: PageExtractor,
    tool_input: dict[str, Any],
) -> ToolResult:
    del session, extractor
    doc_name = tool_input.get("doc_name", "")
    node_id = tool_input.get("node_id", "")
    depth = int(tool_input.get("depth", 1))
    try:
        doc = find_document(index, doc_name)
    except DocumentNotFoundError as e:
        return _err(str(e))
    try:
        entries = expand_subtree(doc, node_id, max_depth=depth)
    except NodeNotFoundError as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))
    if not entries:
        return ToolResult(
            status=ToolStatus.WARNING,
            message=f"Node '{node_id}' has no children. Fetch pages directly if this is the leaf you want.",
            data={"entries": []},
        )
    return ToolResult(
        status=ToolStatus.OK,
        data={
            "doc_name": doc.doc_name,
            "parent_node_id": node_id,
            "depth": depth,
            "entries": [e.to_dict() for e in entries],
        },
    )


def handle_fetch_pages(
    session: Session,
    index: PageIndex,
    extractor: PageExtractor,
    tool_input: dict[str, Any],
) -> ToolResult:
    doc_name = tool_input.get("doc_name", "")
    try:
        page_start = int(tool_input["page_start"])
        page_end = int(tool_input["page_end"])
    except (KeyError, ValueError, TypeError):
        return _err("page_start and page_end must be integers")

    try:
        doc = find_document(index, doc_name)
    except DocumentNotFoundError as e:
        return _err(str(e))

    s3_uri = doc.meta.s3_uri

    try:
        result = extractor.get_range(s3_uri, page_start, page_end)
    except ValueError as e:
        return _err(str(e))
    except IndexError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("fetch_pages_failed")
        return _err(f"Unexpected error fetching pages: {e}")

    # Register with the session so record_citation can verify against it.
    pages_text = {pt.page_number: pt.text for pt in result.pages}
    session.record_fetched_range(doc.doc_name, page_start, page_end, pages_text)

    formatted = extractor.format_range(result)

    return ToolResult(
        status=ToolStatus.OK if not result.likely_scanned else ToolStatus.WARNING,
        message=(
            "This page range appears scanned/image-based — extracted text is minimal."
            if result.likely_scanned
            else None
        ),
        data={
            "doc_name": doc.doc_name,
            "page_start": page_start,
            "page_end": page_end,
            "total_chars": result.total_chars,
            "text": formatted,
        },
    )


def handle_keyword_scan(
    session: Session,
    index: PageIndex,
    extractor: PageExtractor,
    tool_input: dict[str, Any],
) -> ToolResult:
    del session, extractor
    doc_name = tool_input.get("doc_name", "")
    terms = tool_input.get("terms", [])
    if not isinstance(terms, list) or not terms:
        return _err("terms must be a non-empty list of strings")
    try:
        doc = find_document(index, doc_name)
    except DocumentNotFoundError as e:
        return _err(str(e))
    hits = keyword_scan(doc, terms)
    return ToolResult(
        status=ToolStatus.OK,
        data={
            "doc_name": doc.doc_name,
            "terms": terms,
            "hits": [
                {
                    "node_id": h.node_id,
                    "title": h.title,
                    "summary": h.summary,
                    "path": h.path,
                    "page_start": h.page_start,
                    "page_end": h.page_end,
                    "score": h.score,
                    "matched_terms": list(h.matched_terms),
                }
                for h in hits[:20]  # cap on wire size
            ],
            "total_matches": len(hits),
        },
    )


def handle_record_citation(
    session: Session,
    index: PageIndex,
    extractor: PageExtractor,
    tool_input: dict[str, Any],
) -> ToolResult:
    del extractor
    doc_name = tool_input.get("doc_name", "")
    try:
        page_start = int(tool_input["page_start"])
        page_end = int(tool_input["page_end"])
    except (KeyError, ValueError, TypeError):
        return _err("page_start and page_end must be integers")
    quoted_span = tool_input.get("quoted_span", "")
    if not isinstance(quoted_span, str) or len(quoted_span) < 5:
        return _err("quoted_span must be a string of at least 5 characters")
    if len(quoted_span) > 300:
        return _err("quoted_span exceeds 300 characters — trim to the load-bearing sentence")

    try:
        doc = find_document(index, doc_name)
    except DocumentNotFoundError as e:
        return _err(str(e))

    # Integrity: were these pages actually fetched?
    fetched = session.find_fetched_text(doc.doc_name, page_start, page_end)
    if fetched is None:
        return _err(
            f"Cannot cite pages {page_start}-{page_end} of '{doc.doc_name}' — "
            "you must fetch_pages that range (or a superset) before citing."
        )

    # Integrity: does the quoted_span actually appear in the fetched text?
    normalized_quote = _normalize_for_match(quoted_span)
    concatenated = " ".join(_normalize_for_match(t) for t in fetched.values())
    if normalized_quote not in concatenated:
        return _err(
            "quoted_span not found in the fetched pages. Quote must be a "
            "verbatim substring of the page text. Whitespace is normalized "
            "but content must match exactly."
        )

    # Attach durable metadata that the model didn't (and shouldn't) supply.
    node_path = node_path_from_pages(doc, page_start, page_end)
    citation = Citation(
        id=session.next_citation_id(),
        doc_name=doc.doc_name,
        s3_uri=doc.meta.s3_uri,
        page_start=page_start,
        page_end=page_end,
        quoted_span=quoted_span,
        node_path=node_path,
    )
    session.add_citation(citation)
    # Successful citation resets the diminishing-returns counter.
    session.consecutive_empty_fetches = 0

    return ToolResult(
        status=ToolStatus.OK,
        data={
            "citation_id": citation.id,
            "node_path": citation.node_path,
            "total_citations": len(session.citations),
        },
    )


def handle_submit_answer(
    session: Session,
    index: PageIndex,
    extractor: PageExtractor,
    tool_input: dict[str, Any],
) -> ToolResult:
    """The dispatcher recognizes this as terminal, but we still validate the
    payload structurally here. Full output-schema validation happens after
    the loop returns."""
    del index, extractor

    answer = tool_input.get("answer")
    cited_ids = tool_input.get("cited_ids", [])
    confidence = tool_input.get("confidence")
    reasoning = tool_input.get("reasoning", "")

    if not isinstance(answer, dict):
        return _err("answer must be a JSON object")
    if not isinstance(cited_ids, list) or not all(isinstance(x, str) for x in cited_ids):
        return _err("cited_ids must be a list of strings")
    if confidence not in ("high", "medium", "low", "insufficient_evidence"):
        return _err("confidence must be one of: high, medium, low, insufficient_evidence")
    if not isinstance(reasoning, str) or len(reasoning) < 10:
        return _err("reasoning must be a non-empty string (>= 10 chars)")

    # Verify every cited ID resolves.
    missing = [cid for cid in cited_ids if session.get_citation(cid) is None]
    if missing:
        return _err(
            f"Unknown citation IDs: {missing}. "
            f"Only IDs returned by record_citation may be cited."
        )

    # If confidence != insufficient_evidence, require at least one citation.
    if confidence != "insufficient_evidence" and not cited_ids:
        return _err(
            "Confidence '{}' requires at least one citation. "
            "Either add citations or set confidence to insufficient_evidence.".format(confidence)
        )

    session.submitted_answer = {
        "answer": answer,
        "cited_ids": cited_ids,
        "confidence": confidence,
        "reasoning": reasoning,
    }
    return ToolResult(
        status=ToolStatus.OK,
        data={"accepted": True, "citation_count": len(cited_ids)},
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


HANDLERS = {
    "list_documents": handle_list_documents,
    "get_outline": handle_get_outline,
    "expand_node": handle_expand_node,
    "fetch_pages": handle_fetch_pages,
    "keyword_scan": handle_keyword_scan,
    "record_citation": handle_record_citation,
    "submit_answer": handle_submit_answer,
}
