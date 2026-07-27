"""
Thin S3 wrapper.

- Retries via boto3's built-in adaptive mode (configured at client creation).
- LRU cache of raw bytes keyed by s3_uri. Cache lives on the instance so it's
  scoped to a single pipeline run — no cross-run leakage.
- Accepts either `s3://bucket/key` URIs or (bucket, key) tuples.

Kept intentionally minimal: no downloads to disk, no multipart handling. The
PDFs we deal with are small enough (sustainability reports, 10-Ks) that
in-memory is fine, and simplicity beats cleverness here.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client as Boto3S3Client

logger = logging.getLogger(__name__)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """s3://bucket/key/with/slashes -> ('bucket', 'key/with/slashes')."""
    if not uri.startswith("s3://"):
        raise ValueError(f"Not an S3 URI: {uri}")
    rest = uri[len("s3://"):]
    if "/" not in rest:
        raise ValueError(f"S3 URI missing key: {uri}")
    bucket, key = rest.split("/", 1)
    if not bucket or not key:
        raise ValueError(f"Malformed S3 URI: {uri}")
    return bucket, key


class S3Client:
    """Wrapper around a boto3 S3 client.

    The class exists (rather than a module-level function) so the cache is
    scoped to the instance and can be reset by discarding it. The pipeline
    creates one per run.
    """

    # Sane defaults; overridable at construction time.
    DEFAULT_CACHE_SIZE = 32
    DEFAULT_MAX_ATTEMPTS = 4

    def __init__(
        self,
        boto3_client: "Boto3S3Client | None" = None,
        cache_size: int = DEFAULT_CACHE_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        region: str | None = None,
    ) -> None:
        if boto3_client is None:
            # Import lazily so unit tests that inject a mock don't need boto3.
            import boto3
            from botocore.config import Config

            cfg = Config(
                retries={"max_attempts": max_attempts, "mode": "adaptive"},
                region_name=region,
            )
            boto3_client = boto3.client("s3", config=cfg)
        self._client = boto3_client

        # lru_cache on a bound method is awkward; use a manual cache dict
        # keyed by URI with a hand-rolled LRU eviction.
        self._cache: dict[str, bytes] = {}
        self._cache_order: list[str] = []
        self._cache_size = cache_size

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def get_object_bytes(self, s3_uri: str) -> bytes:
        """Fetch an S3 object's bytes, caching the result."""
        if s3_uri in self._cache:
            # Bump to most-recently-used.
            self._cache_order.remove(s3_uri)
            self._cache_order.append(s3_uri)
            return self._cache[s3_uri]

        bucket, key = parse_s3_uri(s3_uri)
        logger.info("s3.get_object", extra={"bucket": bucket, "key": key})
        response = self._client.get_object(Bucket=bucket, Key=key)
        data: bytes = response["Body"].read()

        self._cache[s3_uri] = data
        self._cache_order.append(s3_uri)
        while len(self._cache_order) > self._cache_size:
            oldest = self._cache_order.pop(0)
            self._cache.pop(oldest, None)

        return data

    def head_object(self, s3_uri: str) -> dict:
        """Cheap existence check for preflight."""
        bucket, key = parse_s3_uri(s3_uri)
        return self._client.head_object(Bucket=bucket, Key=key)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_order.clear()
