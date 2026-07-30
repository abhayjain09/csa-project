"""
Preflight checks — run before any LLM call, catch malformed inputs cheaply.

Every check returns a list of problem strings (empty = pass). All checks
run even if earlier ones fail; the caller sees the full picture.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-not-found]

from models.schemas import PageIndex, QuestionBlock
from pdf.s3_client import S3Client

logger = logging.getLogger(__name__)


class PreflightError(ValueError):
    """Aggregates all preflight problems into one error."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "Preflight failed:\n  - " + "\n  - ".join(problems)
        )


def check_pageindex_freshness(index: PageIndex, staleness_days: int) -> list[str]:
    """Warn (not fail) if the index is stale. Returns a warning string, or []."""
    try:
        # Support both 'Z' and '+00:00' suffixes.
        raw = index.updated_at.replace("Z", "+00:00")
        updated = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return [f"pageIndex.updated_at is not a valid ISO timestamp: {index.updated_at!r}"]

    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - updated
    if age > timedelta(days=staleness_days):
        return [
            f"pageIndex is {age.days} days old (threshold {staleness_days}). "
            f"Consider rebuilding before running."
        ]
    return []


def check_documents_accessible(index: PageIndex, s3: S3Client) -> list[str]:
    """HEAD one object per document to confirm S3 access. Cheaper than
    downloading, catches missing objects/permissions at preflight."""
    problems: list[str] = []
    for doc in index.documents:
        try:
            s3.head_object(doc.meta.s3_uri)
        except (ClientError, BotoCoreError) as e:
            problems.append(
                f"Cannot access '{doc.doc_name}' at {doc.meta.s3_uri}: {e}"
            )
        except Exception as e:  # noqa: BLE001
            problems.append(
                f"Unexpected error checking '{doc.doc_name}': {e}"
            )
    return problems


def check_question_blocks(questions: list[QuestionBlock]) -> list[str]:
    """Pydantic already validates individual blocks; this catches
    cross-block issues (duplicate IDs, empty set)."""
    problems: list[str] = []
    if not questions:
        problems.append("question_set is empty")
        return problems
    ids_seen: dict[str, int] = {}
    for i, q in enumerate(questions):
        if q.id in ids_seen:
            problems.append(
                f"Duplicate question id '{q.id}' at positions "
                f"{ids_seen[q.id]} and {i}"
            )
        else:
            ids_seen[q.id] = i
    return problems


def run_preflight(
    index: PageIndex,
    questions: list[QuestionBlock],
    s3: S3Client,
    staleness_days: int,
) -> tuple[list[str], list[str]]:
    """
    Run all preflight checks.

    Returns
    -------
    (errors, warnings)
        Errors block the run; warnings are logged and continue.
    """
    errors: list[str] = []
    warnings: list[str] = []

    warnings.extend(check_pageindex_freshness(index, staleness_days))
    errors.extend(check_documents_accessible(index, s3))
    errors.extend(check_question_blocks(questions))

    return errors, warnings
