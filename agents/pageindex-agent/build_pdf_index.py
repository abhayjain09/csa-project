"""
build_pdf_index.py — Page-index builder for a single company.

For each PDF stored in S3 for the given company, this script:
  1. Lists all PDFs under the company's S3 prefix (e.g. paccar/).
  2. Checks if a pageindex JSON already exists in that same prefix and loads
     it — already-indexed s3_keys are skipped (incremental mode).
  3. Invokes the AgentCore runtime for each new PDF — the runtime streams
     the PDF from S3, runs page_index_main(), and returns the index JSON.
  4. Saves the consolidated pageindex JSON back to the SAME S3 prefix:
         s3://<bucket>/<company-slug>/<company-slug>_pageindex.json

Output format
-------------
{
  "company": "Paccar",
  "company_slug": "paccar",
  "bucket": "your-bucket",
  "updated_at": "2026-07-09T10:32:15+00:00",
  "documents": [
    {
      "doc_name": "paccar-2024-sustainability-report.pdf",
      "structure": [ ... ],
      "_meta": {
        "s3_key":     "paccar/report.pdf",
        "s3_uri":     "s3://your-bucket/paccar/report.pdf",
        "indexed_at": "2026-07-09T10:32:15+00:00"
      }
    }
  ]
}

Usage
-----
    python build_pdf_index.py --company "Nestlé S.A."
    python build_pdf_index.py --s3-prefix xylem/
    python build_pdf_index.py --s3-prefix s3://edo-coanalyst-report-610639371721/xylem/
    COMPANY=Xylem python build_pdf_index.py

Environment variables
---------------------
    REPORTS_BUCKET          S3 bucket holding the downloaded reports
                            (default: edo-coanalyst-report-610639371721)
    AWS_REGION              (default: us-east-1)
    AGENTCORE_RUNTIME_ARN   ARN of the AgentCore runtime to invoke
                            (output by: cd infra && terraform output -raw runtime_arn)
"""

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional, List

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BUCKET                = os.environ.get("REPORTS_BUCKET", "edo-coanalyst-report-610639371721")
REGION                = os.environ.get("AWS_REGION", "us-east-1")
AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("pageindex-builder")

# Filename written inside the company's S3 prefix
_PAGEINDEX_FILENAME = "{slug}_pageindex.json"


# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
def _get_s3():
    return boto3.client(
        "s3",
        region_name=REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def _get_agentcore_client():
    return boto3.client("bedrock-agentcore", region_name=REGION)


# ---------------------------------------------------------------------------
# Company-name normalisation
# ---------------------------------------------------------------------------
def _normalize(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _normalize(name)).strip("-") or "unknown"


def _s3_prefix_variants(company: str) -> list[str]:
    norm = _normalize(company)
    variants: set[str] = set()
    variants.add(norm.replace(" ", "-").replace(".", "").replace(",", ""))
    variants.add(norm.replace(" ", "").replace(".", "").replace(",", ""))
    first = norm.split()[0].replace(".", "").replace(",", "") if norm.split() else norm
    variants.add(first)
    for suffix in (" sa", " inc", " ltd", " plc", " corp", " co", " group", " ag", " nv", " se"):
        if norm.endswith(suffix):
            base = norm[: -len(suffix)].strip().replace(" ", "-")
            variants.add(base)
    return [v + "/" for v in sorted(variants) if v]


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
def _parse_s3_prefix(raw: str) -> tuple[str, str]:
    """Return (bucket, prefix) from a bare prefix or full s3:// URI."""
    raw = raw.strip()
    if raw.startswith("s3://"):
        without_scheme = raw[5:]
        bucket, _, prefix = without_scheme.partition("/")
        prefix = prefix.lstrip("/")
    else:
        bucket = BUCKET
        prefix = raw.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def _list_pdfs_by_prefix(prefix: str, bucket: str, s3) -> List[dict]:
    """List all PDFs under a single exact S3 prefix."""
    log.info("[s3] listing PDFs — bucket=%r prefix=%r", bucket, prefix)
    results: List[dict] = []
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if not key.lower().endswith(".pdf"):
                    continue
                results.append({
                    "s3_key":        key,
                    "size":          obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                })
    except ClientError as exc:
        log.error("[s3] list error for prefix %r: %s", prefix, exc)
    log.info("[s3] found %d PDF(s)", len(results))
    return results


def _list_pdfs_for_company(company: str, s3) -> tuple[List[dict], str]:
    """
    Try all prefix variants for the company.
    Returns (pdf_list, matched_prefix) — matched_prefix is the first
    variant that returned results, used as the canonical output location.
    """
    prefixes = _s3_prefix_variants(company)
    log.info("[s3] company=%r — trying prefixes: %s", company, prefixes)
    seen: set[str] = set()
    results: List[dict] = []
    matched_prefix = prefixes[0]  # fallback if nothing found
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in prefixes:
        prefix_results: List[dict] = []
        try:
            for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key: str = obj["Key"]
                    if key in seen or not key.lower().endswith(".pdf"):
                        continue
                    seen.add(key)
                    prefix_results.append({
                        "s3_key":        key,
                        "size":          obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    })
        except ClientError as exc:
            log.warning("[s3] list error for prefix %r: %s", prefix, exc)
        if prefix_results and not results:
            matched_prefix = prefix   # first prefix that has PDFs is canonical
        results.extend(prefix_results)
    log.info("[s3] found %d PDF(s) for company=%r under prefix=%r", len(results), company, matched_prefix)
    return results, matched_prefix


# ---------------------------------------------------------------------------
# S3 pageindex persistence
# All reads/writes go to S3 — no local files written.
# ---------------------------------------------------------------------------
def _pageindex_s3_key(prefix: str, slug: str) -> str:
    """
    Returns the S3 key for the pageindex JSON file.
    e.g. prefix="paccar/"  slug="paccar"  -> "paccar/paccar_pageindex.json"
    """
    filename = _PAGEINDEX_FILENAME.format(slug=slug)
    return str(PurePosixPath(prefix.rstrip("/")) / filename)


def _load_existing_index(bucket: str, s3_key: str, s3) -> dict:
    """Load an existing pageindex from S3, or return an empty skeleton."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=s3_key)
        data = json.loads(obj["Body"].read())
        log.info("[s3] loaded existing pageindex from s3://%s/%s (%d doc(s))",
                 bucket, s3_key, len(data.get("documents", [])))
        return data
    except s3.exceptions.NoSuchKey:
        log.info("[s3] no existing pageindex at s3://%s/%s — starting fresh", bucket, s3_key)
        return {"documents": []}
    except ClientError as exc:
        log.warning("[s3] could not load existing pageindex: %s — starting fresh", exc)
        return {"documents": []}


def _save_index(bucket: str, s3_key: str, data: dict, s3) -> None:
    """Write the consolidated pageindex JSON back to S3."""
    body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    s3.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=body,
        ContentType="application/json",
    )
    log.info("[s3] saved pageindex -> s3://%s/%s (%d doc(s))",
             bucket, s3_key, len(data.get("documents", [])))


# ---------------------------------------------------------------------------
# AgentCore invocation
# The PDF is NOT sent in the payload — the runtime streams it from S3,
# avoiding payload size limits on large PDFs.
# ---------------------------------------------------------------------------
def _invoke_runtime(bucket: str, s3_key: str, label: str) -> dict:
    """
    Invoke the AgentCore runtime for a single PDF.
    Returns the raw index dict from page_index_main() on success.
    Raises RuntimeError on failure.
    """
    if not AGENTCORE_RUNTIME_ARN:
        raise RuntimeError(
            "AGENTCORE_RUNTIME_ARN is not set. "
            "Run: export AGENTCORE_RUNTIME_ARN=$(cd infra && terraform output -raw runtime_arn)"
        )

    client  = _get_agentcore_client()
    payload = json.dumps({"bucket": bucket, "s3_key": s3_key, "label": label})

    log.info("[agentcore] invoking runtime for %s …", label)
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            payload=payload,
        )
    except ClientError as exc:
        raise RuntimeError(f"AgentCore invocation failed: {exc}") from exc

    raw    = response["response"].read()
    result = json.loads(raw)

    if result.get("status") != "ok":
        raise RuntimeError(f"Runtime returned error: {result.get('error')}")

    log.info("[agentcore] runtime completed for %s", label)
    return result["index"]


# ---------------------------------------------------------------------------
# Core entrypoint
# ---------------------------------------------------------------------------
def build_pageindex_for_company(
    company: Optional[str] = None,
    force: bool = False,
    s3_prefix: Optional[str] = None,
) -> dict:
    """
    Index all PDFs for a company and save the result back to the same S3 prefix.

    Parameters
    ----------
    company:
        Human-readable company name (e.g. "Nestlé S.A.", "Xylem").
        Used to derive the S3 prefix via multi-variant discovery.
        Either *company* or *s3_prefix* must be supplied.
    force:
        Discard any existing pageindex and re-index every PDF from scratch.
    s3_prefix:
        Exact S3 prefix, e.g. "xylem/" or "s3://my-bucket/xylem/".
        Skips prefix-variant discovery. Either *company* or *s3_prefix* must
        be supplied.

    Returns
    -------
    Summary dict: company, slug, pageindex_s3_uri, indexed, skipped.
    """
    if not company and not s3_prefix:
        raise ValueError("Either 'company' or 's3_prefix' must be provided")

    s3 = _get_s3()

    # ── Resolve bucket, prefix, slug, display name ───────────────────────────
    if s3_prefix:
        resolved_bucket, resolved_prefix = _parse_s3_prefix(s3_prefix)
        slug            = resolved_prefix.strip("/").split("/")[0] or _slug(company or "unknown")
        display_company = company or slug
        log.info("[main] fast-path: bucket=%r prefix=%r slug=%r", resolved_bucket, resolved_prefix, slug)
        pdfs = _list_pdfs_by_prefix(resolved_prefix, resolved_bucket, s3)
    else:
        if not company or not company.strip():
            raise ValueError("'company' must be a non-empty string")
        display_company = company.strip()
        slug            = _slug(display_company)
        resolved_bucket = BUCKET
        pdfs, resolved_prefix = _list_pdfs_for_company(display_company, s3)

    # S3 key where the consolidated pageindex lives (same prefix as the PDFs)
    pageindex_key     = _pageindex_s3_key(resolved_prefix, slug)
    pageindex_s3_uri  = f"s3://{resolved_bucket}/{pageindex_key}"

    if not pdfs:
        log.warning("[main] no PDFs found for company=%r — nothing to do", display_company)
        return {
            "company":          display_company,
            "slug":             slug,
            "pageindex_s3_uri": pageindex_s3_uri,
            "indexed":          [],
            "skipped":          [],
        }

    # ── Load existing index (or start fresh if --force) ──────────────────────
    existing        = {} if force else _load_existing_index(resolved_bucket, pageindex_key, s3)
    already_indexed = {doc["_meta"]["s3_key"] for doc in existing.get("documents", [])}
    documents       = list(existing.get("documents", []))
    indexed: List[dict] = []
    skipped: List[dict] = []

    for pdf_meta in pdfs:
        s3_key   = pdf_meta["s3_key"]
        doc_name = PurePosixPath(s3_key).name  # filename only e.g. "paccar-2024.pdf"

        # Skip PDFs that are already in the index
        if s3_key in already_indexed:
            log.info("[main] skipping %s — already indexed", s3_key)
            skipped.append({"s3_key": s3_key, "reason": "already_indexed"})
            continue

        # ── Invoke AgentCore runtime ─────────────────────────────────────────
        try:
            index_data = _invoke_runtime(resolved_bucket, s3_key, doc_name)
        except RuntimeError as exc:
            log.error("[main] runtime failed for %s: %s — skipping", s3_key, exc)
            skipped.append({"s3_key": s3_key, "reason": str(exc)})
            continue

        # ── Build document entry matching the output format spec ─────────────
        document = {
            "doc_name":  index_data.get("doc_name", doc_name),
            "structure": index_data.get("structure", []),
            "_meta": {
                "s3_key":     s3_key,
                "s3_uri":     f"s3://{resolved_bucket}/{s3_key}",
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        documents.append(document)
        indexed.append({"s3_key": s3_key})

        # ── Persist to S3 after every document ───────────────────────────────
        # Progress is never lost if a later document fails.
        _save_index(resolved_bucket, pageindex_key, {
            "company":      display_company,
            "company_slug": slug,
            "bucket":       resolved_bucket,
            "updated_at":   datetime.now(timezone.utc).isoformat(),
            "documents":    documents,
        }, s3)

    log.info(
        "[main] done — company=%r  indexed=%d  skipped=%d  pageindex=%s",
        display_company, len(indexed), len(skipped), pageindex_s3_uri,
    )
    return {
        "company":          display_company,
        "slug":             slug,
        "pageindex_s3_uri": pageindex_s3_uri,
        "indexed":          indexed,
        "skipped":          skipped,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build PageIndex JSON for all PDFs of a company stored in S3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--company", "-c", default=os.environ.get("COMPANY"),
                        help="Company name (e.g. 'Nestlé S.A.'). Also reads COMPANY env var.")
    parser.add_argument("--s3-prefix", "-p", default=os.environ.get("S3_PREFIX"),
                        help="Exact S3 prefix, e.g. 'xylem/' or 's3://bucket/xylem/'. "
                             "Also reads S3_PREFIX env var.")
    parser.add_argument("--force", action="store_true",
                        help="Discard existing pageindex and re-index all PDFs from scratch.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if not args.company and not args.s3_prefix:
        print("ERROR: --company or --s3-prefix (or COMPANY / S3_PREFIX env var) is required.",
              file=sys.stderr)
        sys.exit(1)

    result = build_pageindex_for_company(
        company=args.company,
        force=args.force,
        s3_prefix=args.s3_prefix,
    )
    print(json.dumps(result, indent=2))

