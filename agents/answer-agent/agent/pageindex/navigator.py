"""
Read-only navigation over a loaded PageIndex.

These are the workhorse functions behind the agent's traversal tools:
- resolve documents by name (with helpful fuzzy-match error messages)
- render outlines to a given depth
- expand a specific subtree
- resolve a node_id to its ancestor path (used when writing citations)
- scan titles+summaries for keyword hits

Everything here is pure — no S3, no LLM, no PDF I/O. That constraint makes the
tool handlers thin wrappers and keeps unit tests fast.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from models.schemas import PageIndex, PageIndexDocument, PageIndexNode


# ---------------------------------------------------------------------------
# Document resolution
# ---------------------------------------------------------------------------


class DocumentNotFoundError(KeyError):
    """Raised when a doc_name doesn't match any document in the index."""


def find_document(index: PageIndex, doc_name: str) -> PageIndexDocument:
    """Case-insensitive exact match on doc_name. If not found, raise with a
    list of available docs — the agent will see this in the tool_result and
    can self-correct."""
    target = doc_name.strip().lower()
    for doc in index.documents:
        if doc.doc_name.lower() == target:
            return doc
    available = [d.doc_name for d in index.documents]
    raise DocumentNotFoundError(
        f"No document named '{doc_name}'. Available: {available}"
    )


# ---------------------------------------------------------------------------
# Outline rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutlineEntry:
    """A single row in a rendered outline. Flat list of these is much easier
    for the LLM to reason over than a nested dict."""

    node_id: str
    title: str
    summary: str
    page_start: int
    page_end: int
    depth: int
    ancestor_titles: tuple[str, ...]

    def path(self, doc_name: str | None = None) -> str:
        parts = list(self.ancestor_titles) + [self.title]
        if doc_name:
            parts.insert(0, doc_name)
        return " > ".join(parts)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "summary": self.summary,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "depth": self.depth,
            "path": self.path(),
        }


def _walk(
    nodes: list[PageIndexNode],
    depth: int,
    max_depth: int,
    ancestors: tuple[str, ...],
) -> Iterator[OutlineEntry]:
    for n in nodes:
        yield OutlineEntry(
            node_id=n.node_id,
            title=n.title,
            summary=n.summary,
            page_start=n.start_index,
            page_end=n.end_index,
            depth=depth,
            ancestor_titles=ancestors,
        )
        if depth < max_depth and n.nodes:
            yield from _walk(n.nodes, depth + 1, max_depth, ancestors + (n.title,))


def render_outline(
    doc: PageIndexDocument, max_depth: int = 1
) -> list[OutlineEntry]:
    """Flatten the tree to `max_depth` levels. Depth 1 = only top-level
    sections; depth 2 = top-level + their immediate children; etc."""
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")
    return list(_walk(doc.structure, depth=1, max_depth=max_depth, ancestors=()))


# ---------------------------------------------------------------------------
# Subtree expansion
# ---------------------------------------------------------------------------


class NodeNotFoundError(KeyError):
    """Raised when a node_id doesn't exist in the given document."""


@dataclass(frozen=True)
class LocatedNode:
    """A node together with the ancestor chain that led to it. Used both for
    expansion and for citation resolution."""

    node: PageIndexNode
    ancestor_titles: tuple[str, ...]  # excludes the node's own title


def _find_node(
    nodes: list[PageIndexNode], node_id: str, ancestors: tuple[str, ...]
) -> LocatedNode | None:
    for n in nodes:
        if n.node_id == node_id:
            return LocatedNode(node=n, ancestor_titles=ancestors)
        if n.nodes:
            hit = _find_node(n.nodes, node_id, ancestors + (n.title,))
            if hit is not None:
                return hit
    return None


def locate_node(doc: PageIndexDocument, node_id: str) -> LocatedNode:
    """Find a node by id, returning it with its ancestor path."""
    hit = _find_node(doc.structure, node_id, ancestors=())
    if hit is None:
        raise NodeNotFoundError(f"No node '{node_id}' in document '{doc.doc_name}'")
    return hit


def expand_subtree(
    doc: PageIndexDocument, node_id: str, max_depth: int = 1
) -> list[OutlineEntry]:
    """Return the children of `node_id`, flattened to `max_depth`. The node
    itself is NOT included in the output — the agent already has it from the
    parent outline call."""
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")
    located = locate_node(doc, node_id)
    if not located.node.nodes:
        return []
    new_ancestors = located.ancestor_titles + (located.node.title,)
    return list(_walk(located.node.nodes, depth=1, max_depth=max_depth, ancestors=new_ancestors))


# ---------------------------------------------------------------------------
# Node path (for citations)
# ---------------------------------------------------------------------------


def node_path_from_pages(
    doc: PageIndexDocument, page_start: int, page_end: int
) -> str:
    """Given a page range, find the deepest node that fully contains it, and
    return its path. Used by record_citation to attach a durable
    `node_path` to a citation.

    "Deepest containing node" — because we want the most specific reference.
    If the range spans multiple sections, falls back to the nearest common
    ancestor.
    """
    if page_end < page_start:
        raise ValueError("page_end < page_start")

    best: LocatedNode | None = None
    best_depth = -1

    def visit(nodes: list[PageIndexNode], ancestors: tuple[str, ...], depth: int) -> None:
        nonlocal best, best_depth
        for n in nodes:
            if n.start_index <= page_start and n.end_index >= page_end:
                # This node contains the whole range.
                if depth > best_depth:
                    best = LocatedNode(node=n, ancestor_titles=ancestors)
                    best_depth = depth
                if n.nodes:
                    visit(n.nodes, ancestors + (n.title,), depth + 1)

    visit(doc.structure, ancestors=(), depth=0)

    if best is None:
        # No single node contains the range (e.g., range spans two top-level
        # sections). Return the doc name as a coarse fallback.
        return doc.doc_name

    parts = list(best.ancestor_titles) + [best.node.title]
    return " > ".join([doc.doc_name] + parts)


# ---------------------------------------------------------------------------
# Keyword scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeywordHit:
    node_id: str
    title: str
    summary: str
    page_start: int
    page_end: int
    path: str
    matched_terms: tuple[str, ...]
    score: int  # count of unique terms matched, weighted 2x for title hits


def keyword_scan(
    doc: PageIndexDocument, terms: list[str], min_score: int = 1
) -> list[KeywordHit]:
    """Case-insensitive substring scan over title + summary of every node.
    Ranked by score (title hits weighted higher). No full-text search — that
    would require reading PDFs, defeating the purpose of the index."""
    if not terms:
        return []
    normalized = [t.strip().lower() for t in terms if t.strip()]
    if not normalized:
        return []

    hits: list[KeywordHit] = []

    def visit(nodes: list[PageIndexNode], ancestors: tuple[str, ...]) -> None:
        for n in nodes:
            title_lc = n.title.lower()
            summary_lc = n.summary.lower()
            matched: list[str] = []
            score = 0
            for t in normalized:
                in_title = t in title_lc
                in_summary = t in summary_lc
                if in_title or in_summary:
                    matched.append(t)
                    score += 2 if in_title else 1
            if score >= min_score:
                path = " > ".join([doc.doc_name] + list(ancestors) + [n.title])
                hits.append(
                    KeywordHit(
                        node_id=n.node_id,
                        title=n.title,
                        summary=n.summary,
                        page_start=n.start_index,
                        page_end=n.end_index,
                        path=path,
                        matched_terms=tuple(matched),
                        score=score,
                    )
                )
            if n.nodes:
                visit(n.nodes, ancestors + (n.title,))

    visit(doc.structure, ancestors=())
    hits.sort(key=lambda h: (-h.score, h.page_start))
    return hits


# ---------------------------------------------------------------------------
# Convenience: pageindex_summary for the prompt
# ---------------------------------------------------------------------------


def build_pageindex_summary(index: PageIndex) -> str:
    """Compact summary injected into the prompt so the agent knows what
    documents exist without seeing the whole tree."""
    lines = [
        f"Company: {index.company}",
        f"PageIndex last updated: {index.updated_at}",
        f"Documents ({len(index.documents)}):",
    ]
    for doc in index.documents:
        top_titles = [n.title for n in doc.structure]
        preview = ", ".join(top_titles[:5])
        if len(top_titles) > 5:
            preview += f", ... (+{len(top_titles) - 5} more)"
        lines.append(f"  - {doc.doc_name} — top sections: {preview}")
    return "\n".join(lines)
