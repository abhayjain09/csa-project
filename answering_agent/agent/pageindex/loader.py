"""
Load and validate a pageIndex JSON blob.

Accepts either:
- an inline dict (already parsed), or
- an S3Ref pointing at a JSON object in S3.

Validation surfaces ALL problems at once (pydantic's default), not just the
first — this matters for preflight where the caller wants a complete error list.
"""
from __future__ import annotations

import json
from typing import Any

from models.schemas import PageIndex, S3Ref
from pdf.s3_client import S3Client


def load_pageindex(source: dict[str, Any] | S3Ref, s3: S3Client | None = None) -> PageIndex:
    """
    Parameters
    ----------
    source : dict or S3Ref
        The pageIndex payload. Dict is used as-is; S3Ref triggers a GetObject.
    s3 : S3Client, optional
        Required when `source` is an S3Ref. Passing it in (rather than
        constructing internally) keeps this function pure and testable.

    Returns
    -------
    PageIndex
        Fully validated tree.

    Raises
    ------
    ValueError
        On malformed JSON or schema violation. The pydantic ValidationError
        (wrapped) contains the full field-by-field breakdown.
    """
    if isinstance(source, S3Ref):
        if s3 is None:
            raise ValueError("S3Ref source provided but no S3Client given")
        raw_bytes = s3.get_object_bytes(source.s3_uri)
        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"pageIndex at {source.s3_uri} is not valid JSON: {e}") from e
    else:
        payload = source

    if not isinstance(payload, dict):
        raise ValueError(f"pageIndex must be a JSON object, got {type(payload).__name__}")

    return PageIndex.model_validate(payload)
