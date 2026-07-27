"""
Extract per-page text from PDFs pulled from S3.

Strategy:
- One `PageExtractor` per pipeline run.
- Given `(s3_uri, page_number)`, returns the page's text.
- Underlying pypdf Reader is memoized per s3_uri so the PDF is parsed once
  even if many pages are requested from it.
- Per-page text is memoized so re-requests are free.
- Pages are 1-indexed (matching pageIndex `start_index`/`end_index`).

If a PDF is scanned/image-based, pypdf returns empty strings for its pages.
We detect and flag this at the range level so the ReAct agent gets a warning
rather than silently reasoning over empty text.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from pdf.s3_client import S3Client

logger = logging.getLogger(__name__)


@dataclass
class PageRangeResult:
    s3_uri: str
    page_start: int
    page_end: int
    pages: list["PageText"]
    total_chars: int
    likely_scanned: bool  # True if >80% of pages returned <20 chars of text


@dataclass
class PageText:
    page_number: int  # 1-indexed
    text: str


class PageExtractor:
    """Extract per-page text on demand, with memoization."""

    # Absolute ceiling per fetch_pages call. The tool handler enforces this
    # too, but keeping it here prevents accidental large reads from other
    # callers.
    MAX_SPAN = 15

    def __init__(self, s3: S3Client) -> None:
        self._s3 = s3
        # Cached pypdf readers, keyed by s3_uri.
        self._readers: dict[str, object] = {}
        # Cached page text, keyed by (s3_uri, page_number).
        self._page_cache: dict[tuple[str, int], str] = {}

    def _get_reader(self, s3_uri: str):
        """Return a cached pypdf PdfReader for the given S3 object."""
        if s3_uri in self._readers:
            return self._readers[s3_uri]

        # Lazy import so unit tests that mock get_page can skip pypdf.
        from pypdf import PdfReader

        data = self._s3.get_object_bytes(s3_uri)
        reader = PdfReader(io.BytesIO(data))
        self._readers[s3_uri] = reader
        logger.info(
            "pdf.reader_created",
            extra={"s3_uri": s3_uri, "num_pages": len(reader.pages)},
        )
        return reader

    def num_pages(self, s3_uri: str) -> int:
        reader = self._get_reader(s3_uri)
        return len(reader.pages)  # type: ignore[attr-defined]

    def get_page(self, s3_uri: str, page_number: int) -> str:
        """Return the text of a single page (1-indexed)."""
        key = (s3_uri, page_number)
        if key in self._page_cache:
            return self._page_cache[key]

        reader = self._get_reader(s3_uri)
        total = len(reader.pages)  # type: ignore[attr-defined]
        if page_number < 1 or page_number > total:
            raise IndexError(
                f"Page {page_number} out of range (doc has {total} pages)"
            )

        # pypdf is 0-indexed internally.
        page = reader.pages[page_number - 1]  # type: ignore[attr-defined]
        try:
            text = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001 — pypdf can raise many things
            logger.warning(
                "pdf.extract_failed",
                extra={"s3_uri": s3_uri, "page": page_number, "err": str(e)},
            )
            text = ""

        self._page_cache[key] = text
        return text

    def get_range(
        self, s3_uri: str, page_start: int, page_end: int
    ) -> PageRangeResult:
        """Return text for an inclusive page range. Enforces MAX_SPAN."""
        if page_end < page_start:
            raise ValueError(f"page_end ({page_end}) < page_start ({page_start})")
        span = page_end - page_start + 1
        if span > self.MAX_SPAN:
            raise ValueError(
                f"Range span {span} exceeds MAX_SPAN={self.MAX_SPAN}. "
                f"Drill down further in the outline before fetching pages."
            )

        pages: list[PageText] = []
        total_chars = 0
        empty_or_tiny = 0
        for p in range(page_start, page_end + 1):
            text = self.get_page(s3_uri, p)
            pages.append(PageText(page_number=p, text=text))
            total_chars += len(text)
            if len(text.strip()) < 20:
                empty_or_tiny += 1

        likely_scanned = span > 0 and (empty_or_tiny / span) > 0.8

        return PageRangeResult(
            s3_uri=s3_uri,
            page_start=page_start,
            page_end=page_end,
            pages=pages,
            total_chars=total_chars,
            likely_scanned=likely_scanned,
        )

    def format_range(self, result: PageRangeResult) -> str:
        """Render a PageRangeResult as a string with `[p.N]` markers, ready
        to feed back to the ReAct agent."""
        chunks = []
        for pt in result.pages:
            chunks.append(f"[p.{pt.page_number}]\n{pt.text}")
        formatted = "\n\n".join(chunks)
        if result.likely_scanned:
            formatted = (
                "[WARNING: this page range appears to be scanned/image-based; "
                "extracted text is empty or minimal. Consider a different section.]\n\n"
                + formatted
            )
        return formatted
