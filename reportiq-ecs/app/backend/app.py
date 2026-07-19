"""
Report IQ + PageIndex — Flask Backend
Account: 610639371721  Region: us-east-1

Report downloader endpoints:
  POST   /api/queries                        Save and optionally trigger a download run
  GET    /api/queries                        List all queries
  POST   /api/queries/<query_id>/run         Trigger a specific query
  GET    /api/runs                           List all download runs
  GET    /api/runs/<run_id>                  Get a specific download run
  POST   /api/runs/reconcile                 Manually reconcile stuck runs
  GET    /api/sources                        List provenance records
  GET    /api/sources/check-key             Check if S3 key exists
  GET    /api/sources/download-url          Get presigned download URL
  GET    /api/sources/list-s3               List S3 objects by prefix
  POST   /api/sources/sync-from-s3          Sync provenance from S3
  POST   /api/sources/upload                Manual file upload fallback
  GET    /api/stats                         Table and S3 counts

PageIndex endpoints:
  POST   /api/pageindex                      Trigger a new indexing run
  GET    /api/pageindex/runs                 List all indexing runs
  GET    /api/pageindex/runs/<run_id>        Get status of a specific run

Shared:
  GET    /health                             Health check


Patches in this revision (1–6, plus 7-8 below):
  1. company + run_id are now included in the AgentCore payload. Previously
     only search_query + web_query* were sent, so the agent stored every file
     under s3 key prefix "unknown/" and wrote provenance with PK company=
     "unknown" — which defeated _list_s3_files_for_run(), the reconciler, and
     grouped Sources. This was the root cause of portal downloads "vanishing".
  2. Native invoke_agent_runtime client now has an explicit long read timeout
     and retries disabled, and the SigV4 HTTP fallback ONLY triggers for a
     genuinely-missing service model (UnknownServiceError / AttributeError) —
     NOT for timeouts. Previously a long bulk run (23 doc queries) hit the
     default 60s botocore read timeout, was caught, and fell through to the
     fallback with the SAME payload, DOUBLE-invoking the agent.
  3. Provenance is written by ONE path only. The backend is the sole writer,
     via _write_provenance_if_missing() keyed on the SAME slug the agent uses
     for S3 (_agent_slug), so there is one row per file and no schema/key
     divergence. _write_provenance (the unconditional writer) is removed from
     the hot path.
  4. run_id is passed through to the agent (see #1) so the agent's S3 metadata
     / any provenance it writes carries the same id as the reportiq-runs row.
  5. Company slug now matches the agent's _slug(): accents are stripped
     (Nestlé -> nestle) AND the agent's exact slug form is included as a prefix
     variant so reconciliation/S3 matching lines up with real keys.
  6. CHUNKED INVOKE. A company's 23 web_query* fields are no longer sent in one
     giant invoke (which timed out, risked the double-invoke path, and produced
     irrelevant/near-neighbour fetches from a 23-class candidate set). They are
     split into chunks of AGENT_CHUNK_SIZE (default 4 -> ~6 chunks) and invoked
     with a BOUNDED thread pool (AGENT_CHUNK_CONCURRENCY, default 2 in flight).
     Each chunk renumbers its queries web_query1.. so the agent always sees a
     small, normal payload. There is still exactly ONE reportiq-runs row per
     company: downloaded results are merged + deduped by s3_key across chunks
     and the row is flushed after each chunk so the UI shows the list grow.
     Per-chunk read timeout is 300s (a 4-query chunk cannot run long enough to
     hit it), which structurally removes the timeout->double-invoke path.
  7. PER-QUERY RESULT TRACKING + MANUAL UPLOAD FALLBACK.
     Each chunk's diagnostics now include a `results` list — one entry per
     query in that chunk with either a matched downloaded file or a
     'failed' status — so the portal can render a per-query row instead of
     only chunk-level counts. This is a best-effort pairing: if the agent
     response tags a downloaded item with its originating query/web_query
     field, that's used; otherwise items are matched positionally in the
     order the chunk's queries were sent (the agent processes web_query1..N
     in order). A new POST /api/sources/upload route lets the portal fall
     back to a manual multipart upload for any query where the agent could
     not find a document; the file is written to S3 under the same slug
     prefix the agent uses, provenance is recorded, and — if a run_id is
     supplied — the matching per-query row in that run's diagnostics is
     flipped from 'failed' to 'downloaded' so the UI shows a Download button
     instead of Upload on the next refresh.
  8. FIXED THE ACTUAL AGENT RESPONSE SCHEMA. Confirmed via raw CloudWatch body
     dumps that the agent's real per-chunk JSON uses `stored` / `duplicates` /
     `no_document_found` — never `downloaded` / `failures`, which patch #7's
     code (and every version before it) was reading. Those keys never existed
     in any real response, so every chunk silently reported downloaded=0,
     failures=0 regardless of what actually happened — including chunks where
     the agent's own logs showed a genuine [store] STORED. `stored` and
     `duplicates` are both real, fully-downloadable successes (a duplicate
     just means the file already existed in S3 under the same hash — nothing
     was lost, nothing needs re-uploading); only `no_document_found` is an
     actual failure. Each item's original agent-side "status" ("stored" vs
     "duplicate") is preserved through to the per-query UI rows as a
     `duplicate` flag so the portal can show "(already in S3)" without
     treating it as anything other than success.
"""
import os, json, uuid, re, threading, hashlib, logging, urllib.request, urllib.error
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import PurePosixPath
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import boto3
import botocore.auth
import botocore.awsrequest
from botocore.config import Config
from botocore.exceptions import ClientError, UnknownServiceError

# ─── Config ───────────────────────────────────────────────────────────────────
REGION            = os.environ.get("AWS_REGION",         "us-east-1")
QUERIES_TABLE     = os.environ.get("QUERIES_TABLE",      "reportiq-web-queries")
PROVENANCE_TABLE  = os.environ.get("PROVENANCE_TABLE",   "edo-coanalyst-report-provenance")
RUNS_TABLE        = os.environ.get("RUNS_TABLE",         "reportiq-runs")
REPORTS_BUCKET    = os.environ.get("REPORTS_BUCKET",     "edo-coanalyst-report-610639371721")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:610639371721:runtime/edo_coanalyst_report-3dAfJRHyfY")
AGENT_QUALIFIER   = os.environ.get("AGENT_QUALIFIER",    "DEFAULT")
STATIC_DIR        = os.environ.get("STATIC_DIR",
    os.path.join(os.path.dirname(__file__), "..", "static"))

# PageIndex runtime
PAGEINDEX_RUNTIME_ARN = os.environ.get(
    "PAGEINDEX_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:610639371721:runtime/pageindex_runtime-rucFhA3V8V")
PAGEINDEX_QUALIFIER   = os.environ.get("PAGEINDEX_QUALIFIER",   "DEFAULT")
PAGEINDEX_RUNS_TABLE  = os.environ.get("PAGEINDEX_RUNS_TABLE",  "pageindex-runs")

# Per-CHUNK read timeout. Each invoke carries only AGENT_CHUNK_SIZE web queries.
# Raised 300s -> 600s after confirming via AWS docs that AgentCore's own
# synchronous invoke has a hard 15-minute execution wall — our old 300s was
# actually TIGHTER than AWS's own ceiling, so we were giving up early on
# legitimately-slow chunks (e.g. large-footprint companies like fcx.com) well
# before AgentCore itself would have. 600s still leaves a comfortable margin
# under the 15-min wall. If a single chunk needs longer than this reliably,
# the durable fix is AgentCore's async invoke pattern, not a bigger number.
AGENT_READ_TIMEOUT = int(os.environ.get("AGENT_READ_TIMEOUT", "600"))

# Chunking: how many web_query* fields per AgentCore invoke, and how many
# invokes may run concurrently. concurrency=2 is the "mix of both" — faster
# than pure sequential, capped low enough to avoid WebSearch 429 throttling.
AGENT_CHUNK_SIZE        = int(os.environ.get("AGENT_CHUNK_SIZE",        "1"))
AGENT_CHUNK_CONCURRENCY = int(os.environ.get("AGENT_CHUNK_CONCURRENCY", "3"))

# A run is considered "stuck" if it has been running for more than this many
# minutes (used only as a cheap outer gate for whether it's worth spawning a
# reconcile check — the real decision uses the heartbeat below).
STUCK_THRESHOLD_MINUTES = 2

# HEARTBEAT: the invoke thread refreshes `heartbeat_at` on the run row every time a
# chunk completes (see _flush_run_row). This is a far more reliable "is this thread
# actually alive" signal than "how long since started_at" — a run legitimately working
# through slow chunks keeps refreshing it, while a thread killed mid-flight (Gunicorn
# worker recycle, ECS task cycle, crash) stops refreshing it immediately. If no chunk
# has reported in longer than this, the run is treated as dead regardless of how many
# chunks are left. MUST stay above AGENT_READ_TIMEOUT (now 600s) with margin — raised
# in lockstep to 13 min so a genuinely slow chunk gets the chance to hit its OWN
# timeout and report an error result (which itself refreshes the heartbeat) before
# the reconciler would otherwise conclude the thread is dead.
HEARTBEAT_STALE_MINUTES = int(os.environ.get("AGENT_HEARTBEAT_STALE_MINUTES", "13"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reportiq")

app = Flask(__name__, static_folder=STATIC_DIR)
CORS(app)

# ─── Global error handler ──────────────────────────────────────────────────────
# BUGFIX: an unhandled exception on ANY route previously fell through to
# Flask/Werkzeug's default HTML error page. The frontend's apiFetch always does
# r.json() on the response, so an HTML error page produced exactly the reported
# symptom: "Unexpected token '<', \"<!doctype \"... is not valid JSON". Worse,
# it meant the real exception (and its traceback) never made it anywhere visible
# — only the frontend's generic parse failure did. Every route now returns valid
# JSON on failure, and the full traceback is logged so CloudWatch shows the exact
# root cause instead of us having to guess at it.
@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    log.exception("[unhandled] %s %s -> %s", request.method, request.path, e)
    return jsonify({"error": str(e), "type": type(e).__name__}), 500

# ─── AWS clients ──────────────────────────────────────────────────────────────
def get_dynamo():
    return boto3.resource("dynamodb", region_name=REGION)

def get_s3():
    # Force SigV4 — required for presigned URLs on KMS-encrypted buckets
    return boto3.client(
        "s3",
        region_name=REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )

def get_agentcore():
    """AgentCore client with long read timeout — used by PageIndex invocations."""
    return boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(read_timeout=900, connect_timeout=10, retries={"max_attempts": 0}),
    )

def _invoke_agentcore(runtime_arn: str, qualifier: str, payload_bytes: bytes) -> bytes:
    """
    Generic AgentCore invoke shared by report-downloader and PageIndex.
    Uses native boto3 client; falls back to SigV4 HTTP only for a missing
    service model (UnknownServiceError / AttributeError) — never on timeout,
    to avoid the double-invoke bug.
    """
    try:
        client = get_agentcore()
        resp = client.invoke_agent_runtime(
            agentRuntimeArn = runtime_arn,
            qualifier       = qualifier,
            payload         = payload_bytes,
            contentType     = "application/json",
            accept          = "application/json",
        )
        body = resp.get("response") or resp.get("body") or resp.get("payload")
        if body is None:
            return b""
        if hasattr(body, "read"):
            return body.read()
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        raw = b""
        for chunk in body:
            raw += chunk if isinstance(chunk, (bytes, bytearray)) else chunk.get("chunk", b"")
        return raw
    except (UnknownServiceError, AttributeError) as e:
        log.warning("[agentcore] service model missing (%s) — using SigV4 HTTP fallback", e)
        return _invoke_agentcore_sigv4_generic(runtime_arn, qualifier, payload_bytes)

def _invoke_agentcore_sigv4_generic(runtime_arn: str, qualifier: str, payload_bytes: bytes) -> bytes:
    """Raw SigV4 HTTP fallback for any runtime ARN."""
    import urllib.parse
    runtime_arn_encoded = urllib.parse.quote(runtime_arn, safe="")
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com"
        f"/runtimes/{runtime_arn_encoded}/invocations"
        f"?qualifier={qualifier}"
    )
    session = boto3.session.Session()
    creds   = session.get_credentials().get_frozen_credentials()
    aws_request = botocore.awsrequest.AWSRequest(
        method="POST", url=url, data=payload_bytes,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    botocore.auth.SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_request)
    prepped = aws_request.prepare()
    req = urllib.request.Request(
        url=prepped.url, data=payload_bytes,
        headers=dict(prepped.headers), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        raise RuntimeError(f"AgentCore HTTP {e.code}: {body.decode('utf-8', errors='replace')}")

# ─── Company slug (MUST match agent._slug so S3 keys / provenance line up) ─────
def _agent_slug(name: str) -> str:
    """
    Reproduce the agent's _slug() EXACTLY (accent-stripped variant):
      unicodedata NFKD -> drop combining marks -> lowercase ->
      non-alphanumeric runs to '-' -> strip leading/trailing '-'.
    Nestlé S.A. -> nestle-s-a ; PACCAR Inc. -> paccar-inc ; Tata Motors -> tata-motors
    Keep this in lockstep with agent.py's _slug().
    """
    s = unicodedata.normalize("NFKD", name or "unknown")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"


# ─── AgentCore invoke ─────────────────────────────────────────────────────────
def _invoke_agentcore_http(payload_bytes: bytes) -> bytes:
    """
    Invoke AgentCore using the native boto3 client (invoke_agent_runtime).

    The native client is configured with a long read timeout and retries
    DISABLED. The SigV4 HTTP fallback is used ONLY when the 'bedrock-agentcore'
    service model is unavailable (old boto3) — never on a timeout/network error,
    because falling back on a timeout re-sends the SAME payload and double-runs
    the agent.
    """
    # ── Preferred: native boto3 client ────────────────────────────────────────
    try:
        client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=Config(
                read_timeout=AGENT_READ_TIMEOUT,
                connect_timeout=10,
                retries={"max_attempts": 0},   # never auto-retry an invoke
            ),
        )
    except (UnknownServiceError, AttributeError) as e:
        # Service model genuinely missing — this is the ONLY case that justifies
        # the raw SigV4 HTTP fallback.
        log.warning("[agentcore] service model missing (%s) — using SigV4 HTTP fallback", e)
        return _invoke_agentcore_sigv4(payload_bytes)

    resp = client.invoke_agent_runtime(
        agentRuntimeArn = AGENT_RUNTIME_ARN,   # full ARN — NOT the bare id
        qualifier       = AGENT_QUALIFIER,
        payload         = payload_bytes,
        contentType     = "application/json",
        accept          = "application/json",
    )
    # Response body is a streaming object
    body = resp.get("response") or resp.get("body") or resp.get("payload")
    if body is None:
        return b""
    if hasattr(body, "read"):
        return body.read()
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    # Iterable of chunks
    raw = b""
    for chunk in body:
        raw += chunk if isinstance(chunk, (bytes, bytearray)) else chunk.get("chunk", b"")
    return raw


def _invoke_agentcore_sigv4(payload_bytes: bytes) -> bytes:
    """Raw SigV4 HTTP invoke — fallback only for a missing service model."""
    import urllib.parse
    runtime_arn_encoded = urllib.parse.quote(AGENT_RUNTIME_ARN, safe="")
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com"
        f"/runtimes/{runtime_arn_encoded}/invocations"
        f"?qualifier={AGENT_QUALIFIER}"
    )
    log.info("[agentcore] fallback URL: %s", url)

    session = boto3.session.Session()
    creds   = session.get_credentials().get_frozen_credentials()
    aws_request = botocore.awsrequest.AWSRequest(
        method="POST", url=url, data=payload_bytes,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    botocore.auth.SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_request)
    prepped = aws_request.prepare()
    req = urllib.request.Request(
        url=prepped.url, data=payload_bytes,
        headers=dict(prepped.headers), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=AGENT_READ_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        raise RuntimeError(f"AgentCore HTTP {e.code}: {body.decode('utf-8', errors='replace')}")


# ─── S3 reconciliation ────────────────────────────────────────────────────────
def _normalize_company(company: str) -> str:
    """Strip accents and lowercase for matching."""
    nfkd = unicodedata.normalize("NFKD", company)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()

def _company_prefix_variants(company: str) -> list:
    """
    Generate all plausible S3 prefix variants the agent might have used.
    Handles accents (Nestlé→nestle), suffixes (S.A., Inc.), spacing.

    IMPORTANT: the FIRST variant is the agent's exact slug form (_agent_slug),
    which is the one the agent actually writes today. The remaining variants are
    tolerant fallbacks for older/edge keys.
    """
    variants = []
    def _add(v):
        if v and v not in variants:
            variants.append(v)

    # 0. The agent's exact slug (authoritative — matches current S3 keys)
    _add(_agent_slug(company))

    norm = _normalize_company(company)
    # 1. Full normalized with hyphens
    _add(norm.replace(" ", "-").replace(".", "").replace(",", ""))
    # 2. With spaces removed entirely
    _add(norm.replace(" ", "").replace(".", "").replace(",", ""))
    # 3. First word only (Nestlé S.A. → nestle)
    first = norm.split()[0].replace(".", "").replace(",", "") if norm.split() else norm
    _add(first)
    # 4. Strip common corporate suffixes
    for suffix in [" sa", " inc", " ltd", " plc", " corp", " co", " group", " ag", " nv", " se"]:
        if norm.endswith(suffix):
            base = norm[:-len(suffix)].strip().replace(" ", "-")
            _add(base)
    return [v + "/" for v in variants if v]

def _s3_prefix_for_company(company: str) -> str:
    """Primary prefix (agent slug) — kept for compatibility."""
    variants = _company_prefix_variants(company)
    return variants[0] if variants else _agent_slug(company) + "/"


def _clean_company_reports(company: str, dynamo=None, s3=None) -> dict:
    """Delete one company's reports and provenance before a fresh run.

    Query definitions and historical run rows are deliberately retained:
    reruns depend on the query record, and run rows are status/audit history.
    Any AWS deletion error is raised so the agent cannot run over stale data.
    """
    if dynamo is None:
        dynamo = get_dynamo()
    if s3 is None:
        s3 = get_s3()

    company_slug = _agent_slug(company)
    prefix = company_slug + "/"
    deleted_s3 = 0

    def _delete_s3_batch(objects):
        nonlocal deleted_s3
        for start in range(0, len(objects), 1000):
            batch = objects[start:start + 1000]
            if not batch:
                continue
            response = s3.delete_objects(
                Bucket=REPORTS_BUCKET,
                Delete={"Objects": batch, "Quiet": True},
            )
            errors = response.get("Errors") or []
            if errors:
                raise RuntimeError(f"S3 cleanup failed for {prefix}: {errors[:3]}")
            deleted_s3 += len(batch)

    # Delete current objects, then all retained versions/delete markers.
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=REPORTS_BUCKET, Prefix=prefix):
        _delete_s3_batch([{"Key": obj["Key"]} for obj in page.get("Contents", [])])

    version_paginator = s3.get_paginator("list_object_versions")
    for page in version_paginator.paginate(Bucket=REPORTS_BUCKET, Prefix=prefix):
        versioned = []
        for field in ("Versions", "DeleteMarkers"):
            versioned.extend({"Key": obj["Key"], "VersionId": obj["VersionId"]}
                             for obj in page.get(field, []))
        _delete_s3_batch(versioned)

    # Provenance is keyed by company slug + S3 key. Query the exact company
    # partition instead of scanning or touching any other company's records.
    provenance = dynamo.Table(PROVENANCE_TABLE)
    deleted_provenance = 0
    query_args = {
        "KeyConditionExpression": "#company = :company",
        "ExpressionAttributeNames": {"#company": "company"},
        "ExpressionAttributeValues": {":company": company_slug},
        "ProjectionExpression": "#company, s3_key",
    }
    while True:
        response = provenance.query(**query_args)
        with provenance.batch_writer() as batch:
            for item in response.get("Items", []):
                batch.delete_item(Key={"company": item["company"], "s3_key": item["s3_key"]})
                deleted_provenance += 1
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key

    summary = {
        "company": company,
        "company_slug": company_slug,
        "s3_deleted": deleted_s3,
        "provenance_deleted": deleted_provenance,
    }
    log.info("[fresh-run-cleanup] company=%r slug=%s s3=%d provenance=%d",
             company, company_slug, deleted_s3, deleted_provenance)
    return summary


def _list_s3_files_for_run(company: str, run_id: str) -> list:
    """
    List S3 objects belonging to this company. Tries multiple prefix variants
    (agent slug first, then accent-/suffix-stripped, first-word) since older
    keys may differ from the current agent naming.
    """
    s3 = get_s3()
    variants = _company_prefix_variants(company)
    log.info("[s3-match] company=%r trying prefixes=%s", company, variants)
    results = []
    seen = set()
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for prefix in variants:
            for page in paginator.paginate(Bucket=REPORTS_BUCKET, Prefix=prefix):
                for obj in page.get("Contents", []):
                    if obj["Key"] in seen:
                        continue
                    seen.add(obj["Key"])
                    results.append({
                        "s3_key":        obj["Key"],
                        "size":          obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                })
    except ClientError as e:
        log.error("[reconcile] S3 list error: %s", e)
    return results


def _reconcile_run(run: dict, dynamo=None) -> bool:
    """
    Check a stuck/running run against S3.
    If files exist in S3 for this company → mark complete + write provenance.
    If no files and run is old enough → mark failed.
    Returns True if status was updated.
    """
    if dynamo is None:
        dynamo = get_dynamo()

    run_id   = run.get("run_id", "")
    company  = run.get("company", "")
    query_id = run.get("query_id", "")
    started  = run.get("started_at", "")

    if not run_id or not company:
        return False

    # Compute age up front (still used as a cheap fallback for legacy rows that
    # predate the heartbeat field, and for the final "no files after N minutes"
    # failure path below).
    age_mins = None
    if started:
        try:
            started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            age_mins   = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60
        except Exception:
            age_mins = None

    # ── Heartbeat-based staleness (the real "is this thread alive" check) ──────
    # heartbeat_at is refreshed by the invoke thread every time a chunk completes.
    # A fresh heartbeat means the thread is genuinely still working — regardless of
    # how many chunks remain — so we leave it alone. A stale (or missing, for very
    # old legacy rows) heartbeat means the thread is dead and we should reconcile
    # NOW rather than wait out an arbitrary "age since start" window.
    heartbeat_at   = run.get("heartbeat_at")
    heartbeat_mins = None
    if heartbeat_at:
        try:
            hb_dt = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
            heartbeat_mins = (datetime.now(timezone.utc) - hb_dt).total_seconds() / 60
        except Exception:
            heartbeat_mins = None

    if heartbeat_mins is not None:
        if heartbeat_mins < HEARTBEAT_STALE_MINUTES:
            log.info("[reconcile] run %s heartbeat %.1f min old (<%d) — still alive, skipping",
                     run_id[:8], heartbeat_mins, HEARTBEAT_STALE_MINUTES)
            return False
        log.info("[reconcile] run %s heartbeat STALE (%.1f min, threshold %d) — treating as dead",
                 run_id[:8], heartbeat_mins, HEARTBEAT_STALE_MINUTES)
    else:
        # No heartbeat field at all — legacy row from before this patch, or the
        # thread died before ever writing one. Fall back to the coarser
        # started_at-based threshold so we don't reconcile something brand new.
        if age_mins is not None and age_mins < STUCK_THRESHOLD_MINUTES:
            log.info("[reconcile] run %s (no heartbeat) only %.1f min old — skipping",
                     run_id[:8], age_mins)
            return False
        log.info("[reconcile] run %s has no heartbeat field — falling back to age-based check", run_id[:8])

    log.info("[reconcile] Checking S3 for run %s company=%s", run_id[:8], company)

    s3_files      = _list_s3_files_for_run(company, run_id)
    finished_at   = datetime.now(timezone.utc).isoformat()
    runs_tbl      = dynamo.Table(RUNS_TABLE)

    if s3_files:
        # Files exist in S3 — mark complete
        log.info("[reconcile] Found %d S3 files for run %s → marking complete", len(s3_files), run_id[:8])

        downloaded_list = [{"s3_key": f["s3_key"],
                            "file_name": f["s3_key"].split("/")[-1]} for f in s3_files]

        # PATCH: the reconciler recovers a run whose backend thread died mid-flight
        # (ECS task cycle, deploy, crash) — the AgentCore agent itself kept working
        # and uploaded to S3 independently of our chunk-tracking thread. Previously
        # this update never touched `diagnostics`, so it stayed frozen at whatever
        # _do_invoke_inner wrote at run START (e.g. "chunks_done": 0). That produced
        # the confusing "0/6 chunks" label on an otherwise-complete run. Since we
        # have no real per-chunk breakdown for a reconciled run, clamp chunks_done
        # to chunks_total (if known) and flag it explicitly so the UI can say
        # "recovered via S3 reconciliation" instead of showing a stale progress bar.
        existing_diag = run.get("diagnostics")
        if isinstance(existing_diag, str):
            try:
                existing_diag = json.loads(existing_diag or "{}")
            except Exception:
                existing_diag = {}
        if not isinstance(existing_diag, dict):
            existing_diag = {}
        chunks_total = existing_diag.get("chunks_total")
        new_diag = dict(existing_diag)
        new_diag["recovered_via_reconciler"] = True
        if isinstance(chunks_total, int) and chunks_total > 0:
            new_diag["chunks_done"] = chunks_total   # clamp — no partial count is meaningful here

        try:
            runs_tbl.update_item(
                Key={"run_id": run_id},
                UpdateExpression=(
                    "SET #st = :s, #fin = :f, #dl = :d, "
                    "#err = :e, #rec = :rec, #dg = :dx"
                ),
                # BUGFIX (confirmed root cause via CloudWatch traceback):
                # "diagnostics" is a DynamoDB reserved keyword. Writing it bare
                # (diagnostics = :dx) throws ValidationException on EVERY call,
                # unconditionally — not intermittently. That's why this write
                # never once succeeded, chunks_done could never advance, and the
                # reconciler's own recovery attempt hit the exact same wall.
                # Every attribute name here is aliased defensively, since
                # DynamoDB's reserved-word list is large and easy to collide
                # with by accident (e.g. "diagnostics" is genuinely on it).
                ExpressionAttributeNames={"#st": "status", "#dg": "diagnostics",
                                          "#fin": "finished_at", "#err": "error_msg",
                                          "#rec": "reconciled", "#dl": "downloaded"},
                ExpressionAttributeValues={
                    ":s":   "complete",
                    ":f":   finished_at,
                    ":d":   json.dumps(downloaded_list),
                    ":e":   "",
                    ":rec": True,
                    ":dx":  json.dumps(new_diag),
                },
            )
        except Exception as ex:
            # BUGFIX: this write was previously unguarded — if it threw (e.g. a
            # large downloaded_list pushing the item past DynamoDB's 400KB limit),
            # the exception propagated straight up through the manual /reconcile
            # API route uncaught, producing an HTML 500 instead of JSON (the
            # "Unexpected token '<'" error). Now it's logged clearly and falls
            # back to a minimal write so status is never left stuck.
            log.error("[reconcile] update_item failed for run %s (type=%s): %s",
                     run_id[:8], type(ex).__name__, ex)
            try:
                runs_tbl.update_item(
                    Key={"run_id": run_id},
                    UpdateExpression="SET #st = :s, #fin = :f, #err = :e, #rec = :rec",
                    ExpressionAttributeNames={"#st": "status", "#fin": "finished_at",
                                              "#err": "error_msg", "#rec": "reconciled"},
                    ExpressionAttributeValues={
                        ":s":   "complete",
                        ":f":   finished_at,
                        ":e":   f"(full reconcile write failed: {ex})"[:1000],
                        ":rec": True,
                    },
                )
            except Exception as ex2:
                log.error("[reconcile] MINIMAL update_item ALSO failed for run %s: %s",
                         run_id[:8], ex2)
                raise   # let the caller's per-run guard (fix #3) record this one as failed

        # Update query status
        if query_id and query_id != "unknown":
            try:
                dynamo.Table(QUERIES_TABLE).update_item(
                    Key={"query_id": query_id},
                    UpdateExpression="SET #st = :s, #upd = :u",
                    ExpressionAttributeNames={"#st": "status", "#upd": "updated_at"},
                    ExpressionAttributeValues={":s": "complete", ":u": finished_at},
                )
            except Exception as ex:
                log.error("[reconcile] query update error: %s", ex)

        # Write provenance only for keys not already stored under this company.
        # Keyed on the agent slug so it matches what the agent itself wrote.
        _write_provenance_if_missing(_agent_slug(company), s3_files, run_id, query_id, finished_at, dynamo)
        return True

    else:
        # No files in S3 — check how old the run is
        if age_mins is not None and age_mins > 15:
            log.info("[reconcile] run %s has no S3 files after %.0f min → marking failed",
                     run_id[:8], age_mins)
            runs_tbl.update_item(
                Key={"run_id": run_id},
                UpdateExpression="SET #st = :s, #fin = :f, #err = :e, #rec = :rec",
                ExpressionAttributeNames={"#st": "status", "#fin": "finished_at",
                                          "#err": "error_msg", "#rec": "reconciled"},
                ExpressionAttributeValues={
                    ":s":   "failed",
                    ":f":   finished_at,
                    ":e":   "No files found in S3 after reconciliation",
                    ":rec": True,
                },
            )
            return True
        log.info("[reconcile] run %s — no S3 files yet, leaving as running", run_id[:8])
        return False


def _write_provenance_if_missing(company_slug: str, s3_files: list, run_id: str,
                                  query_id: str, finished_at: str, dynamo=None):
    """
    SOLE provenance writer (fix #3). Writes one row per file only if a row does
    not already exist for this company_slug + s3_key. `company_slug` MUST be the
    agent slug (_agent_slug) so the PK matches what the agent stores under.

    Idempotent: safe to call from _do_invoke_inner AND the reconciler; the
    get_item guard + composite key dedupe any overlap.
    """
    if dynamo is None:
        dynamo = get_dynamo()

    prov_tbl = dynamo.Table(PROVENANCE_TABLE)
    for f in s3_files:
        s3_key = f.get("s3_key", "") if isinstance(f, dict) else f
        if not s3_key:
            continue
        # Check if record already exists under this company + s3_key
        try:
            existing = prov_tbl.get_item(
                Key={"company": company_slug, "s3_key": s3_key}
            ).get("Item")
            if existing:
                log.debug("[provenance] Already exists: %s / %s — skipping", company_slug, s3_key)
                continue
        except Exception:
            pass  # If check fails, attempt write anyway
        # Write new record
        file_name  = s3_key.split("/")[-1] if s3_key else "unknown"
        source_url = f.get("source_url", "") if isinstance(f, dict) else ""
        try:
            prov_tbl.put_item(Item={
                "company":       company_slug,
                "s3_key":        s3_key,
                "file_name":     file_name,
                "source_url":    source_url,
                "rag_status":    f.get("rag_status", "Pending") if isinstance(f, dict) else "Pending",
                "downloaded_at": finished_at,
                "run_id":        run_id,
                "query_id":      query_id,
                "hash":          hashlib.sha256(s3_key.encode()).hexdigest(),
            })
            log.info("[provenance] Written: %s / %s", company_slug, s3_key)
        except Exception as ex:
            log.error("[provenance] Write error %s: %s", s3_key, ex)


def _summarize_agent_diagnostics(raw: dict) -> dict:
    """
    BUGFIX (root cause of runs getting stuck on 'running'): per_chunk_diag used
    to store the agent's ENTIRE raw diagnostics object for every chunk. For a
    company with a large web-search surface (e.g. HSBC Bank), six chunks of
    verbose agent diagnostics can push the run item past DynamoDB's 400KB
    item-size limit. update_item then throws ValidationException — which
    _flush_run_row catches and only LOGS (so the run silently never advances
    past its initial "running"/0-chunks placeholder), and which the MANUAL
    reconcile endpoint did NOT catch at all (producing the raw HTML 500 the
    frontend choked on). We only ever actually need the cost-relevant metric
    (generated_alias_queries count, per the WebSearch cost-tracking practice)
    plus which top-level keys were present — never the full nested payload.
    """
    if not isinstance(raw, dict):
        return {}
    alias_query_count = 0
    per_query = raw.get("per_query")
    if isinstance(per_query, list):
        for pq in per_query:
            if isinstance(pq, dict):
                aliases = pq.get("generated_alias_queries")
                if isinstance(aliases, list):
                    alias_query_count += len(aliases)
    return {
        "alias_query_count": alias_query_count,
        "keys": sorted(raw.keys())[:20],   # visibility without the heavy payload
    }


def _pair_queries_with_results(chunk_queries: list, downloaded: list, failures: list) -> list:
    """
    PATCH #7: best-effort per-query status for the UI.

    Prefer an explicit query/web_query field carried on a downloaded item (if
    the agent tags its results that way). If nothing is tagged, fall back to
    positional pairing — the agent processes web_query1..N in order within a
    chunk and (in practice) returns downloads in that same order, so the Nth
    query maps to the Nth successful download once matched ones are excluded.

    Every query in the chunk gets exactly one result entry:
      {"query": ..., "status": "downloaded", "s3_key": ..., "file_name": ...,
       "source_url": ...}
    or
      {"query": ..., "status": "failed"}

    A query with no matched file is 'failed' so the portal can offer a manual
    upload button for that specific query.
    """
    dl = [d for d in (downloaded or []) if isinstance(d, dict)]

    # 1) Try explicit tagging first.
    by_query = {}
    for d in dl:
        q = d.get("query") or d.get("web_query")
        if q:
            by_query[q] = d

    results = []
    pos = 0
    for q in chunk_queries:
        match = by_query.get(q)
        if match is None and not by_query and pos < len(dl):
            # 2) No tagging present anywhere in this chunk — fall back to
            # positional pairing across the whole chunk.
            match = dl[pos]
            pos += 1
        if match:
            key = match.get("s3_key") or match.get("key") or ""
            results.append({
                "query":      q,
                "status":     "downloaded",
                "s3_key":     key,
                # PATCH #8: agent items use "report" for the human-readable
                # filename (no "file_name" key exists in the real schema) —
                # fall back through both, then the s3_key basename.
                "file_name":  match.get("file_name") or match.get("report")
                              or (key.split("/")[-1] if key else ""),
                "source_url": match.get("source_url") or match.get("url") or "",
                # True when this result came from the agent's "duplicates"
                # list (a file that already existed in S3) rather than
                # "stored" (a brand-new save). Both are equally real,
                # equally downloadable successes — this flag is purely
                # cosmetic, for an "(already in S3)" note in the UI.
                "duplicate":  match.get("status") == "duplicate",
            })
        else:
            results.append({"query": q, "status": "failed"})
    return results


def _patch_run_with_upload(run_id: str, s3_key: str, file_name: str, query: str,
                            chunk: str, dynamo=None) -> bool:
    """
    PATCH #7 (+ #9 fix below): after a manual upload succeeds, patch the run
    row so the portal's next refresh shows a Download button instead of
    Upload for that query, AND so the run-list "Failures" count actually
    drops:
      - append the file to the run's `downloaded` list (dedup by s3_key)
      - flip the matching per-query row's status to 'downloaded' inside
        diagnostics.per_chunk[*].results (matched by chunk index + query text
        when both are supplied; falls back to matching by query text alone)
      - PATCH #9: remove the matching entry from the run's top-level
        `failures` list too. Previously this list (which feeds
        countFailures() / the Runs table's "Failures" column) was never
        touched by a manual upload — only `downloaded` and the per-query
        diagnostics rows were patched — so a run could show a correct
        per-query "downloaded" row for the uploaded query while the run-list
        Failures count stayed frozen at its original value forever. Entries
        in `failures` may be plain query strings or dicts carrying a
        "query"/"web_query" key (the agent's no_document_found shape isn't
        fully pinned down yet), so both are matched.
    Returns False if the run row doesn't exist (upload + provenance still
    succeed independently — this is purely a UI convenience patch).
    """
    if dynamo is None:
        dynamo = get_dynamo()
    tbl  = dynamo.Table(RUNS_TABLE)
    item = tbl.get_item(Key={"run_id": run_id}).get("Item")
    if not item:
        return False

    try:
        downloaded = json.loads(item.get("downloaded") or "[]")
    except Exception:
        downloaded = []
    if not isinstance(downloaded, list):
        downloaded = []
    if not any(isinstance(d, dict) and d.get("s3_key") == s3_key for d in downloaded):
        downloaded.append({
            "s3_key":      s3_key,
            "file_name":   file_name,
            "source_url":  ("manual-upload: " + query) if query else "manual-upload",
            "manual_upload": True,
        })

    # PATCH #9: drop this query from the top-level failures list so the Runs
    # table's Failures column count actually reflects the manual upload.
    try:
        failures = json.loads(item.get("failures") or "[]")
    except Exception:
        failures = []
    if not isinstance(failures, list):
        failures = []
    try:
        diag = json.loads(item.get("diagnostics") or "{}")
    except Exception:
        diag = {}
    if not isinstance(diag, dict):
        diag = {}

    # The agent's failure payload contains its *prepared* query, while the UI
    # sends the original query stored in diagnostics.results. Preparation can
    # change casing and whitespace, so exact string comparison leaves the
    # top-level failure behind and the Runs table keeps showing the old count.
    def _normalise_query(value):
        return " ".join(str(value or "").split()).casefold()

    query_key = _normalise_query(query)
    result_patched = False
    for pc in (diag.get("per_chunk") or []):
        if not isinstance(pc, dict) or not isinstance(pc.get("results"), list):
            continue
        same_chunk = True
        if chunk:
            same_chunk = (str(pc.get("chunk")) == str(chunk))
        if not same_chunk:
            continue
        for r in pc["results"]:
            if not isinstance(r, dict):
                continue
            if (query_key and _normalise_query(r.get("query")) == query_key
                    and r.get("status") != "downloaded"):
                r.update({
                    "status":     "downloaded",
                    "s3_key":     s3_key,
                    "file_name":  file_name,
                    "manual_upload": True,
                })
                result_patched = True
                # Keep legacy/fallback chunk counts consistent with the
                # per-query result that was just resolved.
                try:
                    pc["failures"] = max(0, int(pc.get("failures") or 0) - 1)
                    pc["downloaded"] = int(pc.get("downloaded") or 0) + 1
                except (TypeError, ValueError):
                    pass
                break

    # Remove exactly one failure: one upload resolves one failed query.  Use a
    # normalised comparison first; if the agent rewrote the prepared query more
    # substantially, a successfully patched failed result is still authoritative
    # evidence that one entry must be removed from the aggregate counter.
    failure_index = None
    if query_key:
        for i, failure in enumerate(failures):
            failure_query = failure
            if isinstance(failure, dict):
                failure_query = failure.get("query") or failure.get("web_query")
            if _normalise_query(failure_query) == query_key:
                failure_index = i
                break
    if failure_index is None and result_patched and failures:
        failure_index = 0
    if failure_index is not None:
        failures.pop(failure_index)

    try:
        tbl.update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #dl = :d, #dg = :dx, #fl = :fa",
            ExpressionAttributeNames={"#dl": "downloaded", "#dg": "diagnostics",
                                      "#fl": "failures"},
            ExpressionAttributeValues={
                ":d":  json.dumps(downloaded),
                ":dx": json.dumps(diag),
                ":fa": json.dumps(failures),
            },
        )
        return True
    except Exception as ex:
        log.error("[upload] run patch write failed for %s: %s", run_id[:8], ex)
        return False


def _get_stuck_runs(dynamo=None) -> list:
    """Scan runs table for any run with status=running."""
    if dynamo is None:
        dynamo = get_dynamo()
    try:
        resp = dynamo.Table(RUNS_TABLE).scan(
            FilterExpression="begins_with(#st, :r)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":r": "running"},
        )
        return resp.get("Items", [])
    except Exception as e:
        log.error("[reconcile] scan error: %s", e)
        return []


# ─── Background reconciler — runs every 60s ───────────────────────────────────
def _background_reconciler():
    import time
    while True:
        time.sleep(60)
        try:
            dynamo = get_dynamo()
            stuck  = _get_stuck_runs(dynamo)
            if stuck:
                log.info("[bg-reconciler] Found %d stuck runs — reconciling", len(stuck))
                for run in stuck:
                    # BUGFIX: this was previously OUTSIDE any per-run try/except.
                    # If _reconcile_run threw for run #1 in the list, the exception
                    # propagated up to the outer try/except and ABORTED the for-loop
                    # entirely — every other stuck run scanned in that same batch
                    # (run #2, #3, ...) silently never got reconciled that cycle, and
                    # would hit the exact same failure (and same abort) on the NEXT
                    # sweep too, since the bad run stays "stuck" forever. Now a
                    # failure on one run is logged and the loop continues to the rest.
                    try:
                        _reconcile_run(run, dynamo)
                    except Exception as ex:
                        log.error("[bg-reconciler] run %s failed: %s",
                                 run.get("run_id", "")[:8], ex)
        except Exception as e:
            log.error("[bg-reconciler] Error: %s", e)

# Start background reconciler thread
_reconciler_thread = threading.Thread(target=_background_reconciler, daemon=True)
_reconciler_thread.start()
log.info("Background reconciler started (every 60s)")


# ═══════════════════════════════════════════════════════════════════════════════
# Routes — static
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(STATIC_DIR, path)


# ═══════════════════════════════════════════════════════════════════════════════
# /api/queries
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/queries", methods=["POST"])
def save_query():
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400

    items  = body if isinstance(body, list) else [body]
    saved  = []
    dynamo = get_dynamo()
    table  = dynamo.Table(QUERIES_TABLE)

    for item in items:
        web_queries = {k: v for k, v in item.items() if k.startswith("web_query")}
        if not web_queries:
            return jsonify({"error": "At least one web_query<N> field required"}), 400

        query_id = str(uuid.uuid4())
        company  = item.get("company", "Unknown")
        now_iso  = datetime.now(timezone.utc).isoformat()

        record = {
            "query_id":     query_id,
            "company":      company,
            "search_query": item.get("search_query", ""),
            "status":       "pending",
            "created_at":   now_iso,
            "updated_at":   now_iso,
            "run_id":       None,
            **web_queries,
        }
        table.put_item(Item=record)
        log.info("Saved query %s for %s", query_id, company)
        saved.append(record)

    trigger = (
        request.args.get("trigger", "false").lower() == "true"
        or any(i.get("trigger_run", True) for i in items)
    )

    run_ids = []
    if trigger:
        for record in saved:
            run_id = _async_invoke(record)
            run_ids.append(run_id)

    return jsonify({"saved": len(saved), "queries": saved,
                    "run_ids": run_ids, "triggered": trigger}), 201


@app.route("/api/queries", methods=["GET"])
def list_queries():
    dynamo = get_dynamo()
    table  = dynamo.Table(QUERIES_TABLE)
    result = table.scan()
    items  = sorted(result.get("Items", []),
                    key=lambda x: x.get("created_at", ""), reverse=True)
    while "LastEvaluatedKey" in result:
        result = table.scan(ExclusiveStartKey=result["LastEvaluatedKey"])
        items += result.get("Items", [])
    return jsonify(items)


@app.route("/api/queries/<query_id>/run", methods=["POST"])
def trigger_query(query_id):
    dynamo = get_dynamo()
    resp   = dynamo.Table(QUERIES_TABLE).get_item(Key={"query_id": query_id})
    item   = resp.get("Item")
    if not item:
        return jsonify({"error": "Query not found"}), 404
    run_id = _async_invoke(item)
    return jsonify({"run_id": run_id, "query_id": query_id, "status": "triggered"})


# ═══════════════════════════════════════════════════════════════════════════════
# /api/runs
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/runs", methods=["GET"])
def list_runs():
    dynamo = get_dynamo()
    table  = dynamo.Table(RUNS_TABLE)
    result = table.scan()
    items  = result.get("Items", [])
    while "LastEvaluatedKey" in result:
        result = table.scan(ExclusiveStartKey=result["LastEvaluatedKey"])
        items += result.get("Items", [])

    # Auto-reconcile any stuck runs inline (non-blocking — fire threads).
    # Guarded by _RECONCILE_INFLIGHT so overlapping /api/runs polls (every 8s)
    # don't spawn duplicate reconcile threads for the same run.
    for item in items:
        if item.get("status") == "running":
            started = item.get("started_at", "")
            try:
                started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                age_mins   = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60
                if age_mins >= STUCK_THRESHOLD_MINUTES:
                    _spawn_reconcile(item)
            except Exception:
                pass

    items = sorted(items, key=lambda x: x.get("started_at", ""), reverse=True)
    return jsonify(items)


@app.route("/api/runs/<run_id>", methods=["GET"])
def get_run(run_id):
    dynamo = get_dynamo()
    resp   = dynamo.Table(RUNS_TABLE).get_item(Key={"run_id": run_id})
    item   = resp.get("Item")
    if not item:
        return jsonify({"error": "Run not found"}), 404
    # Reconcile on individual fetch too
    if item.get("status") == "running":
        _spawn_reconcile(item)
    return jsonify(item)


@app.route("/api/runs/reconcile", methods=["POST"])
def reconcile_runs():
    """Manual trigger — reconcile all stuck runs against S3 right now."""
    dynamo = get_dynamo()
    stuck  = _get_stuck_runs(dynamo)
    fixed  = []
    for run in stuck:
        # BUGFIX: previously _reconcile_run(run, dynamo) was called with no
        # per-run guard. If it threw for ANY stuck run, the exception propagated
        # all the way up through this route with nothing to catch it, and Flask
        # returned its default HTML error page instead of JSON — which is why
        # the frontend showed "Unexpected token '<' ... is not valid JSON" and
        # every OTHER stuck run in the list (including ones that would have
        # succeeded) never got processed either, since the loop never got that
        # far. Now one bad run is reported individually and the rest still run.
        try:
            updated = _reconcile_run(run, dynamo)
            error   = None
        except Exception as ex:
            log.error("[reconcile-api] run %s failed: %s", run.get("run_id", "")[:8], ex)
            updated = False
            error   = str(ex)[:300]
        fixed.append({
            "run_id":  run.get("run_id", "")[:8],
            "company": run.get("company", ""),
            "updated": updated,
            "error":   error,
        })
    return jsonify({
        "stuck_found": len(stuck),
        "updated":     sum(1 for f in fixed if f["updated"]),
        "failed":      [f for f in fixed if f.get("error")],
        "details":     fixed,
    })


# ─── Reconcile thread guard (avoids duplicate in-flight reconciles) ───────────
_RECONCILE_INFLIGHT = set()
_RECONCILE_LOCK = threading.Lock()

def _spawn_reconcile(run: dict):
    rid = run.get("run_id", "")
    if not rid:
        return
    with _RECONCILE_LOCK:
        if rid in _RECONCILE_INFLIGHT:
            return
        _RECONCILE_INFLIGHT.add(rid)

    def _worker():
        try:
            _reconcile_run(run)
        finally:
            with _RECONCILE_LOCK:
                _RECONCILE_INFLIGHT.discard(rid)

    threading.Thread(target=_worker, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
# /api/sources
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/sources", methods=["GET"])
def list_sources():
    dynamo  = get_dynamo()
    table   = dynamo.Table(PROVENANCE_TABLE)
    result  = table.scan()
    items   = result.get("Items", [])
    while "LastEvaluatedKey" in result:
        result = table.scan(ExclusiveStartKey=result["LastEvaluatedKey"])
        items += result.get("Items", [])
    return jsonify(items)


@app.route("/api/sources/check-key", methods=["GET"])
def check_key():
    s3_key = request.args.get("key", "").strip()
    if not s3_key:
        return jsonify({"exists": False, "error": "key param required"}), 400
    s3 = get_s3()
    try:
        s3.head_object(Bucket=REPORTS_BUCKET, Key=s3_key)
        return jsonify({"exists": True, "key": s3_key})
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey"):
            return jsonify({"exists": False, "key": s3_key})
        return jsonify({"exists": False, "error": str(e)}), 500


@app.route("/api/sources/download-url", methods=["GET"])
def presigned_url():
    s3_key = request.args.get("key", "").strip()
    if not s3_key:
        return jsonify({"error": "key param required"}), 400
    s3 = get_s3()
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": REPORTS_BUCKET, "Key": s3_key},
            ExpiresIn=3600,
        )
        return jsonify({"url": url, "key": s3_key, "expires_in": 3600})
    except ClientError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/list-s3", methods=["GET"])
def list_s3():
    prefix    = request.args.get("prefix", "")
    s3        = get_s3()
    results   = []
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=REPORTS_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                results.append({
                    "key":           obj["Key"],
                    "size":          obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                })
    except ClientError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(results)


@app.route("/api/sources/sync-from-s3", methods=["POST"])
def sync_provenance_from_s3():
    """
    Scan the entire S3 bucket and create provenance records for any
    objects that don't already have one. Useful after a manual wipe or
    if provenance writes failed during a run.
    """
    prefix  = request.json.get("prefix", "") if request.is_json else ""
    s3      = get_s3()
    dynamo  = get_dynamo()
    created = 0
    skipped = 0

    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=REPORTS_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                s3_key  = obj["Key"]
                parts   = s3_key.split("/")
                company = parts[0] if len(parts) > 1 else "unknown"
                file_name = parts[-1]

                # Check if record already exists
                prov_tbl = dynamo.Table(PROVENANCE_TABLE)
                try:
                    existing = prov_tbl.get_item(
                        Key={"company": company, "s3_key": s3_key}
                    ).get("Item")
                    if existing:
                        skipped += 1
                        continue
                except Exception:
                    pass

                # Write new provenance record
                try:
                    prov_tbl.put_item(Item={
                        "company":       company,
                        "s3_key":        s3_key,
                        "file_name":     file_name,
                        "source_url":    "",
                        "rag_status":    "Pending",
                        "downloaded_at": obj["LastModified"].isoformat(),
                        "run_id":        "manual-sync",
                        "query_id":      "manual-sync",
                        "hash":          hashlib.sha256(s3_key.encode()).hexdigest(),
                    })
                    created += 1
                except Exception as ex:
                    log.error("[sync-s3] provenance write error %s: %s", s3_key, ex)

    except ClientError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"created": created, "skipped": skipped,
                    "total": created + skipped})


@app.route("/api/sources/upload", methods=["POST"])
def upload_source():
    """
    PATCH #7 — manual upload fallback.

    Used from the Runs detail view when the agent could not find a document
    for a specific query. The person picks a file locally; it is streamed to
    S3 under the SAME slug prefix the agent itself uses (so it appears
    alongside agent-downloaded files for the same company), a provenance row
    is written (SOLE writer path, same as everywhere else), and — if a
    run_id is supplied — the matching per-query row inside that run's
    diagnostics is flipped from 'failed' to 'downloaded' so the portal's
    next refresh shows a Download button instead of Upload for that row.

    Expects multipart/form-data:
      file      - required, the file itself
      company   - required, company display name (used to derive the slug)
      query     - optional, the exact web_query text this file answers
      run_id    - optional, the run whose diagnostics should be patched
      query_id  - optional, the DynamoDB query_id (for provenance linkage)
      chunk     - optional, the chunk index the query belonged to (narrows
                  the patch match when the same query text could appear in
                  more than one chunk)
    """
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file is required (multipart field 'file')"}), 400
    company = (request.form.get("company") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400

    query    = (request.form.get("query")    or "").strip()
    run_id   = (request.form.get("run_id")   or "").strip()
    query_id = (request.form.get("query_id") or "").strip()
    chunk    = (request.form.get("chunk")    or "").strip()

    slug      = _agent_slug(company)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(f.filename)).strip("_") or "upload"
    s3_key    = f"{slug}/manual/{safe_name}"

    try:
        get_s3().put_object(
            Bucket=REPORTS_BUCKET,
            Key=s3_key,
            Body=f.read(),
            ContentType=f.mimetype or "application/octet-stream",
            Metadata={"uploaded-by": "portal-manual", "query": query[:1024]},
            # If the bucket's policy requires SSE-KMS to be specified explicitly
            # on every PUT (rather than relying on the bucket's default
            # encryption setting), uncomment the line below and set
            # SSEKMSKeyId if a non-default CMK is required:
            # ServerSideEncryption="aws:kms",
        )
    except ClientError as e:
        log.error("[upload] S3 put_object failed for %s: %s", s3_key, e)
        return jsonify({"error": f"S3 upload failed: {e}"}), 500

    now_iso = datetime.now(timezone.utc).isoformat()
    dynamo  = get_dynamo()

    try:
        _write_provenance_if_missing(
            slug,
            [{
                "s3_key":     s3_key,
                "source_url": ("manual-upload: " + query) if query else "manual-upload",
                "rag_status": "Pending",
            }],
            run_id or "manual-upload", query_id or "manual-upload", now_iso, dynamo,
        )
    except Exception as ex:
        # The file is already safely in S3; a provenance hiccup shouldn't fail
        # the whole request — log it and continue so the person still gets a
        # success response with the key they can look up manually if needed.
        log.error("[upload] provenance write failed for %s: %s", s3_key, ex)

    patched = False
    if run_id:
        try:
            patched = _patch_run_with_upload(run_id, s3_key, safe_name, query, chunk, dynamo)
        except Exception as ex:
            log.error("[upload] run patch failed for %s: %s", run_id[:8], ex)

    log.info("[upload] company=%s query=%r -> s3_key=%s run_patched=%s",
             company, query, s3_key, patched)

    return jsonify({
        "ok":          True,
        "s3_key":      s3_key,
        "file_name":   safe_name,
        "company":     company,
        "run_patched": patched,
    }), 201


# ═══════════════════════════════════════════════════════════════════════════════
# /api/stats
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/stats", methods=["GET"])
def stats():
    dynamo = get_dynamo()
    counts = {}
    for name, tbl in [("queries", QUERIES_TABLE), ("runs", RUNS_TABLE),
                       ("provenance", PROVENANCE_TABLE)]:
        try:
            counts[name] = dynamo.Table(tbl).scan(Select="COUNT").get("Count", 0)
        except Exception:
            counts[name] = 0
    s3_count = 0
    try:
        for page in get_s3().get_paginator("list_objects_v2").paginate(Bucket=REPORTS_BUCKET):
            s3_count += page.get("KeyCount", 0)
    except Exception:
        pass
    counts["s3_objects"] = s3_count
    return jsonify(counts)


# ═══════════════════════════════════════════════════════════════════════════════
# Async invoke
# ═══════════════════════════════════════════════════════════════════════════════
def _async_invoke(query_record: dict) -> str:
    run_id = str(uuid.uuid4())
    t = threading.Thread(target=_do_invoke, args=(run_id, query_record), daemon=True)
    t.start()
    return run_id


def _do_invoke(run_id: str, query_record: dict):
    try:
        _do_invoke_inner(run_id, query_record)
    except Exception as e:
        # Last-resort guard: never leave a run stuck in 'running'
        log.error("[run %s] FATAL in _do_invoke: %s", run_id[:8], e)
        try:
            get_dynamo().Table(RUNS_TABLE).update_item(
                Key={"run_id": run_id},
                UpdateExpression="SET #st = :s, #fin = :f, #err = :e",
                ExpressionAttributeNames={"#st": "status", "#fin": "finished_at", "#err": "error_msg"},
                ExpressionAttributeValues={
                    ":s": "failed",
                    ":f": datetime.now(timezone.utc).isoformat(),
                    ":e": str(e)[:1000],
                },
            )
        except Exception as ex2:
            log.error("[run %s] Could not write fatal status: %s", run_id[:8], ex2)


# ─── Chunking helpers (PATCH #6) ──────────────────────────────────────────────
def _chunk_web_queries(query_record: dict, size: int) -> list:
    """
    Split the query_record's web_query* fields into ordered chunks of `size`.
    Returns a list of lists of raw query strings, numeric-sorted by the field
    suffix (web_query1, web_query2, ... web_query23) so document order is kept.
    """
    def _idx(k):
        m = re.sub(r"\D", "", k)
        return int(m) if m else 0
    wq_keys  = sorted((k for k in query_record if k.startswith("web_query")), key=_idx)
    queries  = [query_record[k] for k in wq_keys if str(query_record.get(k, "")).strip()]
    size     = max(1, size)
    return [queries[i:i + size] for i in range(0, len(queries), size)]


def _build_chunk_payload(company: str, run_id: str, search_query: str,
                         chunk_queries: list, chunk_index: int) -> dict:
    """
    One chunk = one normal small AgentCore payload. Queries are RENUMBERED
    web_query1.. within the chunk so the agent always sees a clean sequence and
    never has to reason over a 23-class candidate set.
    """
    payload = {
        "company":      company,
        "run_id":       run_id,          # SAME run_id for every chunk (fix #1/#4)
        "search_query": search_query,
        "chunk_index":  chunk_index,     # informational; agent may ignore it
    }
    for i, q in enumerate(chunk_queries, start=1):
        payload["web_query" + str(i)] = q
    return payload


def _invoke_one_chunk(chunk_index: int, chunk_queries: list, company: str,
                      run_id: str, search_query: str) -> dict:
    """Invoke a single chunk and normalise its response into a result dict."""
    payload = _build_chunk_payload(company, run_id, search_query, chunk_queries, chunk_index)
    log.info("[run %s] chunk %d — invoking %d queries", run_id[:8], chunk_index, len(chunk_queries))
    try:
        raw  = _invoke_agentcore_http(json.dumps(payload).encode("utf-8"))
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {"raw": raw.decode("utf-8", errors="replace")}
        # PATCH #8: the agent's REAL response schema uses stored / duplicates /
        # no_document_found — confirmed from raw CloudWatch body dumps. It does
        # NOT use "downloaded" or "failures" (those keys never existed, so this
        # was always silently reading empty defaults, no matter what the agent
        # actually did — the root cause of every chunk showing 0/0 with no error
        # even when the agent's own logs showed real [store] STORED lines).
        #
        # "stored"     -> the agent found and saved a NEW file this call.
        # "duplicates" -> the agent found a matching file that ALREADY existed
        #                 in S3 (same sha256/company/doc-class) and did not
        #                 re-upload it. This is NOT a failure — the document is
        #                 genuinely present in S3 and fully downloadable via the
        #                 s3_key it carries; it's a success from every angle
        #                 that matters to the portal (Sources tab, provenance,
        #                 per-query Download button). Both lists are merged
        #                 into `downloaded` so the entire rest of the pipeline
        #                 (dedup, provenance write, per-query pairing) treats
        #                 them identically. Each item's own "status" field
        #                 ("stored" vs "duplicate") is preserved and passed
        #                 through so the UI can still show a small "(already in
        #                 S3)" note without changing the fact that it's a
        #                 successful, downloadable result.
        # "no_document_found" -> the only real failure list; the agent tried
        #                 exhaustively and failed closed for that query.
        stored     = body.get("stored", [])            if isinstance(body, dict) else []
        duplicates = body.get("duplicates", [])        if isinstance(body, dict) else []
        not_found  = body.get("no_document_found", []) if isinstance(body, dict) else []
        downloaded = list(stored) + list(duplicates)
        failures   = list(not_found)
        diag       = body.get("diagnostics", {}) if isinstance(body, dict) else {}
        log.info("[run %s] chunk %d done — downloaded=%d failures=%d",
                 run_id[:8], chunk_index, len(downloaded), len(failures))
        return {"chunk": chunk_index, "queries": chunk_queries,
                "downloaded": downloaded, "failures": failures,
                # PATCH #7: pre-computed per-query rows so the UI doesn't have
                # to re-derive them and so the pairing logic lives in one place.
                "results": _pair_queries_with_results(chunk_queries, downloaded, failures),
                "diagnostics": diag, "error": None}
    except Exception as e:
        log.error("[run %s] chunk %d ERROR: %s", run_id[:8], chunk_index, e)
        # PATCH #7: even a hard chunk failure gets per-query rows — every query
        # in the chunk is 'failed' so the UI can still offer manual upload
        # instead of only showing an opaque chunk-level error string.
        return {"chunk": chunk_index, "queries": chunk_queries,
                "downloaded": [], "failures": [], "diagnostics": {},
                "results": [{"query": q, "status": "failed"} for q in chunk_queries],
                "error": str(e)[:500]}


def _do_invoke_inner(run_id: str, query_record: dict):
    dynamo   = get_dynamo()
    runs_tbl = dynamo.Table(RUNS_TABLE)
    qry_tbl  = dynamo.Table(QUERIES_TABLE)
    query_id = query_record.get("query_id", "unknown")
    company  = query_record.get("company",  "Unknown")
    search_q = query_record.get("search_query", "")
    now_iso  = datetime.now(timezone.utc).isoformat()

    # PATCH #6: split the 23 web_query* fields into chunks of AGENT_CHUNK_SIZE.
    chunks       = _chunk_web_queries(query_record, AGENT_CHUNK_SIZE)
    chunks_total = len(chunks)

    base_payload = {"company": company, "run_id": run_id,
                    "search_query": search_q,
                    "chunk_size": AGENT_CHUNK_SIZE,
                    "chunks_total": chunks_total}

    # Write running row (downloaded starts empty; diagnostics carries progress).
    # heartbeat_at starts equal to started_at and is refreshed on every chunk
    # completion by _flush_run_row — the reconciler uses staleness of this field
    # (not age-since-started_at) to decide if the tracking thread has died.
    runs_tbl.put_item(Item={
        "run_id":      run_id,
        "query_id":    query_id,
        "company":     company,
        "status":      "running",
        "started_at":  now_iso,
        "heartbeat_at": now_iso,
        "payload":     json.dumps(base_payload),
        "downloaded":  json.dumps([]),
        "failures":    json.dumps([]),
        "diagnostics": json.dumps({
            "chunks_total": chunks_total,
            "chunks_done":  0,
            "chunk_size":   AGENT_CHUNK_SIZE,
            "concurrency":  AGENT_CHUNK_CONCURRENCY,
            "per_chunk":    [],
        }),
    })

    qry_tbl.update_item(
        Key={"query_id": query_id},
        UpdateExpression="SET #st = :s, #rid = :r, #upd = :u",
        ExpressionAttributeNames={"#st": "status", "#rid": "run_id", "#upd": "updated_at"},
        ExpressionAttributeValues={":s": "running", ":r": run_id, ":u": now_iso},
    )

    if chunks_total == 0:
        log.info("[run %s] No web queries to run — marking no_results", run_id[:8])
        finished_at = datetime.now(timezone.utc).isoformat()
        runs_tbl.update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #st = :s, #fin = :f, #err = :e",
            ExpressionAttributeNames={"#st": "status", "#fin": "finished_at", "#err": "error_msg"},
            ExpressionAttributeValues={":s": "no_results", ":f": finished_at,
                                       ":e": "No web_query fields in payload"},
        )
        qry_tbl.update_item(
            Key={"query_id": query_id},
            UpdateExpression="SET #st = :s, #upd = :u",
            ExpressionAttributeNames={"#st": "status", "#upd": "updated_at"},
            ExpressionAttributeValues={":s": "no_results", ":u": finished_at},
        )
        return

    # Cleanup is synchronous and strict: no AgentCore invocation starts until
    # this company's old S3 reports and provenance rows are gone. The outer
    # _do_invoke guard marks the run failed if cleanup cannot complete.
    cleanup = _clean_company_reports(company, dynamo=dynamo)
    runs_tbl.update_item(
        Key={"run_id": run_id},
        UpdateExpression="SET #cleanup = :cleanup",
        ExpressionAttributeNames={"#cleanup": "pre_run_cleanup"},
        ExpressionAttributeValues={":cleanup": json.dumps(cleanup)},
    )

    log.info("[run %s] company=%s — %d chunks of <=%d queries, concurrency=%d",
             run_id[:8], company, chunks_total, AGENT_CHUNK_SIZE, AGENT_CHUNK_CONCURRENCY)

    # ── Aggregate state shared across chunk threads ───────────────────────────
    lock              = threading.Lock()
    downloaded_by_key = {}    # s3_key -> {s3_key, file_name, source_url}
    all_failures      = []
    per_chunk_diag    = []
    chunks_done       = [0]

    def _flush_run_row(final=False, status=None, error_msg=None):
        """Write current aggregate to the ONE run row. Status only set on final."""
        downloaded_list = list(downloaded_by_key.values())
        diag = {
            "chunks_total": chunks_total,
            "chunks_done":  chunks_done[0],
            "chunk_size":   AGENT_CHUNK_SIZE,
            "concurrency":  AGENT_CHUNK_CONCURRENCY,
            "per_chunk":    per_chunk_diag,
        }

        # SAFETY NET: even with the summarized (not raw) agent diagnostics, a
        # company with enough downloaded files + chunk detail could still edge
        # toward DynamoDB's 400KB item limit. Rather than risk update_item
        # throwing (which is what silently froze runs at "running" before),
        # pre-check the serialized size and drop to counts-only per_chunk detail
        # if it's getting large. This trades detail for guaranteed status writes.
        # NOTE: dropping "results" here also removes the per-query Upload/
        # Download rows for this run — the UI's fallback (chunk-level counts
        # table) still renders in that case.
        diag_json = json.dumps(diag)
        if len(diag_json) > 300_000:
            log.warning("[run %s] diagnostics %d bytes — trimming per_chunk detail",
                       run_id[:8], len(diag_json))
            diag = dict(diag)
            diag["per_chunk"] = [
                {"chunk": pc.get("chunk"), "downloaded": pc.get("downloaded"),
                 "failures": pc.get("failures"), "error": pc.get("error")}
                for pc in per_chunk_diag
            ]
            diag["per_chunk_trimmed"] = True
            diag_json = json.dumps(diag)

        try:
            if final:
                runs_tbl.update_item(
                    Key={"run_id": run_id},
                    UpdateExpression=(
                        "SET #st = :s, #fin = :f, #dl = :d, "
                        "#fl = :fa, #dg = :dx, #err = :e, "
                        "#hb = :hb"
                    ),
                    # BUGFIX (confirmed via CloudWatch traceback): "diagnostics"
                    # is a DynamoDB reserved keyword. Left bare, this ENTIRE
                    # update_item throws ValidationException on every call — not
                    # occasionally. That means `downloaded` never got set here
                    # either (one throw kills the whole statement), which is why
                    # files only ever appeared via the reconciler's separate S3
                    # scan instead of through this, the primary/intended path.
                    # Every attribute name here is now aliased defensively.
                    ExpressionAttributeNames={"#st": "status", "#dg": "diagnostics",
                                              "#fin": "finished_at", "#err": "error_msg",
                                              "#dl": "downloaded", "#fl": "failures",
                                              "#hb": "heartbeat_at"},
                    ExpressionAttributeValues={
                        ":s":  status,
                        ":f":  datetime.now(timezone.utc).isoformat(),
                        ":d":  json.dumps(downloaded_list),
                        ":fa": json.dumps(all_failures),
                        ":dx": diag_json,
                        ":e":  error_msg or "",
                        ":hb": datetime.now(timezone.utc).isoformat(),
                    },
                )
            else:
                # Incremental: leave status = running so UI keeps live-syncing.
                # heartbeat_at refresh here is the core fix for stuck-forever runs
                # — it's what lets the reconciler tell "still actively working"
                # apart from "thread died mid-flight" (see _reconcile_run).
                # Same reserved-keyword bugfix as above: #dg aliases diagnostics.
                runs_tbl.update_item(
                    Key={"run_id": run_id},
                    UpdateExpression="SET #dl = :d, #fl = :fa, #dg = :dx, #hb = :hb",
                    ExpressionAttributeNames={"#dg": "diagnostics", "#dl": "downloaded",
                                              "#fl": "failures", "#hb": "heartbeat_at"},
                    ExpressionAttributeValues={
                        ":d":  json.dumps(downloaded_list),
                        ":fa": json.dumps(all_failures),
                        ":dx": diag_json,
                        ":hb": datetime.now(timezone.utc).isoformat(),
                    },
                )
        except Exception as ex:
            log.error("[run %s] flush failed (final=%s, type=%s): %s",
                     run_id[:8], final, type(ex).__name__, ex)
            # LAST RESORT: this is the exact failure mode that left runs stuck
            # forever showing stale "0/N chunks" while the queries table (a much
            # smaller, separately-guarded write) went on to say "complete". If
            # this is the FINAL flush, status must get written no matter what —
            # drop everything except the bare minimum so the row can never be
            # left silently stuck on "running" again. heartbeat_at is included
            # even here, since a status write failing shouldn't ALSO orphan the
            # reconciler's one reliable "is it dead" signal.
            if final:
                try:
                    runs_tbl.update_item(
                        Key={"run_id": run_id},
                        UpdateExpression="SET #st = :s, #fin = :f, #err = :e, #hb = :hb",
                        ExpressionAttributeNames={"#st": "status", "#fin": "finished_at",
                                                  "#err": "error_msg", "#hb": "heartbeat_at"},
                        ExpressionAttributeValues={
                            ":s": status,
                            ":f": datetime.now(timezone.utc).isoformat(),
                            ":e": f"(diagnostics write failed: {ex}) {error_msg or ''}"[:1000],
                            ":hb": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    log.warning("[run %s] minimal final status write succeeded after full write failed", run_id[:8])
                except Exception as ex2:
                    log.error("[run %s] MINIMAL final status write ALSO failed: %s — run may stay stuck",
                             run_id[:8], ex2)
            else:
                # Even the incremental write failed — still refresh heartbeat_at
                # alone if at all possible, so a run that's genuinely alive but
                # hitting transient DynamoDB errors isn't mistaken for dead.
                try:
                    runs_tbl.update_item(
                        Key={"run_id": run_id},
                        UpdateExpression="SET #hb = :hb",
                        ExpressionAttributeNames={"#hb": "heartbeat_at"},
                        ExpressionAttributeValues={":hb": datetime.now(timezone.utc).isoformat()},
                    )
                except Exception:
                    pass

    def _handle_result(res: dict):
        with lock:
            for d in (res.get("downloaded") or []):
                key = d.get("s3_key") or d.get("key") if isinstance(d, dict) else None
                if not key:
                    continue
                if key not in downloaded_by_key:   # dedupe across chunks
                    downloaded_by_key[key] = {
                        "s3_key":     key,
                        # PATCH #8: agent items carry "report" for the display
                        # filename, not "file_name" — same fallback as in
                        # _pair_queries_with_results, kept in lockstep.
                        "file_name":  d.get("file_name") or d.get("report") or key.split("/")[-1],
                        "source_url": d.get("source_url") or d.get("url") or "",
                    }
            if res.get("failures"):
                all_failures.extend(res["failures"])
            per_chunk_diag.append({
                "chunk":             res["chunk"],
                "queries":           res["queries"],
                # PATCH #7: per-query status rows (downloaded/failed), used by
                # the Runs detail view to render one row per query with either
                # a Download or an Upload action.
                "results":           res.get("results") or [],
                "downloaded":        len(res.get("downloaded") or []),
                "failures":          len(res.get("failures") or []),
                "error":             res.get("error"),
                "agent_diagnostics": _summarize_agent_diagnostics(res.get("diagnostics") or {}),
            })
            chunks_done[0] += 1
            _flush_run_row(final=False)   # live update — UI shows the list grow

    # ── Invoke chunks with bounded concurrency ("mix of both") ────────────────
    workers = max(1, min(AGENT_CHUNK_CONCURRENCY, chunks_total))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(_invoke_one_chunk, i + 1, ch, company, run_id, search_q)
            for i, ch in enumerate(chunks)
        ]
        for fut in as_completed(futures):
            try:
                _handle_result(fut.result())
            except Exception as e:
                log.error("[run %s] chunk future crashed: %s", run_id[:8], e)
                with lock:
                    chunks_done[0] += 1
                    per_chunk_diag.append({"chunk": "?", "queries": [], "results": [],
                                           "downloaded": 0, "failures": 0,
                                           "error": str(e)[:500], "agent_diagnostics": {}})
                    _flush_run_row(final=False)

    # ── S3 direct-check fallback: agent may have uploaded without enumerating ──
    if not downloaded_by_key:
        try:
            s3_files = _list_s3_files_for_run(company, run_id)
            if s3_files:
                log.info("[run %s] Found %d S3 files via direct check", run_id[:8], len(s3_files))
                for f in s3_files:
                    key = f["s3_key"]
                    downloaded_by_key.setdefault(key, {
                        "s3_key":     key,
                        "file_name":  key.split("/")[-1],
                        "source_url": f.get("source_url", ""),
                    })
        except Exception as ex:
            log.error("[run %s] S3 check error: %s", run_id[:8], ex)

    # ── Final status (complete-if-any-docs) ───────────────────────────────────
    downloaded = list(downloaded_by_key.values())
    any_error  = any(pc.get("error") for pc in per_chunk_diag)

    if downloaded:
        final_status = "complete"          # any docs → complete (per decision)
        error_msg    = None
    elif any_error:
        final_status = "failed"
        errs      = [pc["error"] for pc in per_chunk_diag if pc.get("error")]
        error_msg = ("; ".join(errs))[:1000] if errs else "All chunks failed"
    else:
        final_status = "no_results"
        error_msg    = None

    finished_at = datetime.now(timezone.utc).isoformat()
    _flush_run_row(final=True, status=final_status, error_msg=error_msg)

    try:
        qry_tbl.update_item(
            Key={"query_id": query_id},
            UpdateExpression="SET #st = :s, #upd = :u",
            ExpressionAttributeNames={"#st": "status", "#upd": "updated_at"},
            ExpressionAttributeValues={":s": final_status, ":u": finished_at},
        )
    except Exception as ex:
        log.error("[run %s] Query status update failed: %s", run_id[:8], ex)

    # FIX #3: single provenance writer, keyed on the agent slug so PKs match the
    # agent's own writes and there is exactly one row per file (deduped above).
    if downloaded:
        try:
            _write_provenance_if_missing(_agent_slug(company), downloaded,
                                         run_id, query_id, finished_at, dynamo)
        except Exception as ex:
            log.error("[run %s] Provenance write failed: %s", run_id[:8], ex)

    log.info("[run %s] Done. status=%s downloaded=%d failures=%d chunks=%d",
             run_id[:8], final_status, len(downloaded), len(all_failures), chunks_total)


# ═══════════════════════════════════════════════════════════════════════════════
# PageIndex — S3 / index helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_s3_prefix(raw: str) -> tuple:
    """Return (bucket, prefix) from a bare prefix or full s3:// URI."""
    raw = raw.strip()
    if raw.startswith("s3://"):
        without_scheme = raw[5:]
        bucket, _, prefix = without_scheme.partition("/")
        prefix = prefix.lstrip("/")
    else:
        bucket = REPORTS_BUCKET
        prefix = raw.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def _list_pdfs_by_prefix(prefix: str, bucket: str, s3) -> list:
    """List all PDFs under an exact S3 prefix."""
    log.info("[pageindex][s3] listing PDFs — bucket=%r prefix=%r", bucket, prefix)
    results = []
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
        log.error("[pageindex][s3] list error for prefix %r: %s", prefix, exc)
    log.info("[pageindex][s3] found %d PDF(s)", len(results))
    return results


def _list_pdfs_for_company_pi(company: str, s3) -> tuple:
    """Try all prefix variants — returns (pdf_list, matched_prefix)."""
    prefixes = _company_prefix_variants(company)
    log.info("[pageindex][s3] company=%r trying prefixes: %s", company, prefixes)
    seen: set = set()
    results   = []
    matched_prefix = prefixes[0]
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in prefixes:
        prefix_results = []
        try:
            for page in paginator.paginate(Bucket=REPORTS_BUCKET, Prefix=prefix):
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
            log.warning("[pageindex][s3] list error for prefix %r: %s", prefix, exc)
        if prefix_results and not results:
            matched_prefix = prefix
        results.extend(prefix_results)
    log.info("[pageindex][s3] found %d PDF(s) for company=%r under prefix=%r",
             len(results), company, matched_prefix)
    return results, matched_prefix


def _pageindex_s3_key(prefix: str, slug: str) -> str:
    return f"{prefix}{slug}_pageindex.json"


def _load_existing_index(bucket: str, s3_key: str, s3) -> dict:
    try:
        obj = s3.get_object(Bucket=bucket, Key=s3_key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return {}
        raise


def _save_pageindex(bucket: str, s3_key: str, data: dict, s3):
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=s3_key, Body=body, ContentType="application/json")
    log.info("[pageindex][s3] saved -> s3://%s/%s (%d doc(s))",
             bucket, s3_key, len(data.get("documents", [])))


def _invoke_pageindex_runtime(bucket: str, s3_key: str, label: str) -> dict:
    """Invoke the PageIndex AgentCore runtime for a single PDF."""
    payload_bytes = json.dumps({
        "bucket": bucket,
        "s3_key": s3_key,
        "label":  label,
    }).encode("utf-8")
    log.info("[pageindex][agentcore] invoking runtime for %s ...", label)
    raw = _invoke_agentcore(PAGEINDEX_RUNTIME_ARN, PAGEINDEX_QUALIFIER, payload_bytes)
    if not raw:
        raise RuntimeError("Empty response from PageIndex runtime")
    result = json.loads(raw.decode("utf-8"))
    if result.get("status") != "ok":
        raise RuntimeError(f"Runtime returned error: {result.get('error')}")
    log.info("[pageindex][agentcore] completed for %s", label)
    return result["index"]


def _write_run_status(run_id: str, update: dict, dynamo=None):
    """Generic DynamoDB updater for pageindex-runs. Never raises."""
    if dynamo is None:
        dynamo = get_dynamo()
    try:
        expr_names  = {}
        expr_values = {}
        set_parts   = []
        for k, v in update.items():
            safe_key = f"#f_{k}"
            val_key  = f":v_{k}"
            expr_names[safe_key]  = k
            expr_values[val_key]  = v
            set_parts.append(f"{safe_key} = {val_key}")
        dynamo.Table(PAGEINDEX_RUNS_TABLE).update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except Exception as ex:
        log.error("[pageindex][run %s] DynamoDB write failed: %s", run_id[:8], ex)


def _update_provenance_rag_status(company: str, s3_key: str, status: str, dynamo=None):
    """
    Update rag_status on a provenance record.
    status: 'Indexed' | 'Failed' | 'Pending'
    Silently skips if the record doesn't exist.
    Never raises.
    """
    if dynamo is None:
        dynamo = get_dynamo()
    try:
        dynamo.Table(PROVENANCE_TABLE).update_item(
            Key={"company": company, "s3_key": s3_key},
            UpdateExpression="SET rag_status = :s, indexed_at = :t",
            ExpressionAttributeValues={
                ":s": status,
                ":t": datetime.now(timezone.utc).isoformat(),
            },
        )
        log.info("[provenance] rag_status=%s  %s / %s", status, company, s3_key)
    except Exception as ex:
        log.warning("[provenance] update skipped for %s / %s: %s", company, s3_key, ex)


# ═══════════════════════════════════════════════════════════════════════════════
# PageIndex — async worker
# ═══════════════════════════════════════════════════════════════════════════════

def _async_pageindex(company: str, s3_prefix: str, force: bool) -> str:
    run_id  = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        get_dynamo().Table(PAGEINDEX_RUNS_TABLE).put_item(Item={
            "run_id":    run_id,
            "company":   company or s3_prefix or "unknown",
            "s3_prefix": s3_prefix or "",
            "status":    "pending",
            "started_at": now_iso,
            "force":     force,
        })
    except Exception as ex:
        log.error("[pageindex][run %s] Initial DynamoDB write failed: %s", run_id[:8], ex)
    t = threading.Thread(
        target=_do_pageindex, args=(run_id, company, s3_prefix, force), daemon=True)
    t.start()
    return run_id


def _do_pageindex(run_id: str, company: str, s3_prefix: str, force: bool):
    try:
        _do_pageindex_inner(run_id, company, s3_prefix, force)
    except Exception as e:
        log.error("[pageindex][run %s] FATAL: %s", run_id[:8], e)
        try:
            _write_run_status(run_id, {
                "status":      "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_msg":   str(e)[:1000],
            })
        except Exception as ex2:
            log.error("[pageindex][run %s] Could not write fatal status: %s", run_id[:8], ex2)


def _do_pageindex_inner(run_id: str, company: str, s3_prefix: str, force: bool):
    s3      = get_s3()
    dynamo  = get_dynamo()
    now_iso = datetime.now(timezone.utc).isoformat()

    if s3_prefix:
        resolved_bucket, resolved_prefix = _parse_s3_prefix(s3_prefix)
        slug            = resolved_prefix.strip("/").split("/")[0] or _agent_slug(company or "unknown")
        display_company = company or slug
        pdfs            = _list_pdfs_by_prefix(resolved_prefix, resolved_bucket, s3)
    else:
        display_company = company.strip()
        slug            = _agent_slug(display_company)
        resolved_bucket = REPORTS_BUCKET
        pdfs, resolved_prefix = _list_pdfs_for_company_pi(display_company, s3)

    pageindex_key    = _pageindex_s3_key(resolved_prefix, slug)
    pageindex_s3_uri = f"s3://{resolved_bucket}/{pageindex_key}"

    _write_run_status(run_id, {
        "status":           "running",
        "company":          display_company,
        "slug":             slug,
        "s3_prefix":        resolved_prefix,
        "pageindex_s3_uri": pageindex_s3_uri,
        "started_at":       now_iso,
    }, dynamo)

    if not pdfs:
        log.warning("[pageindex][run %s] No PDFs found for company=%r", run_id[:8], display_company)
        _write_run_status(run_id, {
            "status":      "no_results",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error_msg":   f"No PDFs found under prefix {resolved_prefix}",
            "indexed":     json.dumps([]),
            "skipped":     json.dumps([]),
        }, dynamo)
        return

    existing        = {} if force else _load_existing_index(resolved_bucket, pageindex_key, s3)
    already_indexed = {doc["_meta"]["s3_key"] for doc in existing.get("documents", [])}
    documents       = list(existing.get("documents", []))
    indexed         = []
    skipped         = []
    error_msg       = None

    for pdf_meta in pdfs:
        s3_key   = pdf_meta["s3_key"]
        doc_name = PurePosixPath(s3_key).name

        if s3_key in already_indexed:
            log.info("[pageindex][run %s] skipping %s — already indexed", run_id[:8], s3_key)
            skipped.append({"s3_key": s3_key, "reason": "already_indexed"})
            continue

        try:
            index_data = _invoke_pageindex_runtime(resolved_bucket, s3_key, doc_name)
        except RuntimeError as exc:
            log.error("[pageindex][run %s] runtime failed for %s: %s", run_id[:8], s3_key, exc)
            skipped.append({"s3_key": s3_key, "reason": str(exc)})
            error_msg = str(exc)
            _update_provenance_rag_status(display_company, s3_key, "Failed", dynamo)
            continue

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

        # Mark as Indexed in provenance so UI badge updates immediately
        _update_provenance_rag_status(display_company, s3_key, "Indexed", dynamo)

        # Save to S3 after every document — progress never lost on failure
        _save_pageindex(resolved_bucket, pageindex_key, {
            "company":      display_company,
            "company_slug": slug,
            "bucket":       resolved_bucket,
            "updated_at":   datetime.now(timezone.utc).isoformat(),
            "documents":    documents,
        }, s3)

        _write_run_status(run_id, {
            "indexed": json.dumps(indexed),
            "skipped": json.dumps(skipped),
        }, dynamo)

    if indexed:
        final_status = "complete"
    elif error_msg and not skipped:
        final_status = "failed"
    elif not indexed and not skipped:
        final_status = "no_results"
    else:
        final_status = "complete"

    _write_run_status(run_id, {
        "status":           final_status,
        "finished_at":      datetime.now(timezone.utc).isoformat(),
        "indexed":          json.dumps(indexed),
        "skipped":          json.dumps(skipped),
        "pageindex_s3_uri": pageindex_s3_uri,
        "error_msg":        error_msg or "",
    }, dynamo)

    log.info("[pageindex][run %s] Done. status=%s indexed=%d skipped=%d pageindex=%s",
             run_id[:8], final_status, len(indexed), len(skipped), pageindex_s3_uri)


# ═══════════════════════════════════════════════════════════════════════════════
# PageIndex — routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/pageindex", methods=["POST"])
def trigger_pageindex():
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400
    company   = (body.get("company")   or "").strip()
    s3_prefix = (body.get("s3_prefix") or "").strip()
    force     = bool(body.get("force", False))
    if not company and not s3_prefix:
        return jsonify({"error": "Either 'company' or 's3_prefix' is required"}), 400
    run_id = _async_pageindex(company, s3_prefix, force)
    log.info("[pageindex][api] triggered run=%s company=%r s3_prefix=%r force=%s",
             run_id[:8], company, s3_prefix, force)
    return jsonify({
        "run_id":    run_id,
        "status":    "triggered",
        "company":   company or s3_prefix,
        "s3_prefix": s3_prefix,
        "force":     force,
    }), 202


@app.route("/api/pageindex/runs", methods=["GET"])
def list_pageindex_runs():
    dynamo = get_dynamo()
    table  = dynamo.Table(PAGEINDEX_RUNS_TABLE)
    result = table.scan()
    items  = result.get("Items", [])
    while "LastEvaluatedKey" in result:
        result = table.scan(ExclusiveStartKey=result["LastEvaluatedKey"])
        items += result.get("Items", [])
    items = sorted(items, key=lambda x: x.get("started_at", ""), reverse=True)
    return jsonify(items)


@app.route("/api/pageindex/runs/<run_id>", methods=["GET"])
def get_pageindex_run(run_id):
    dynamo = get_dynamo()
    resp   = dynamo.Table(PAGEINDEX_RUNS_TABLE).get_item(Key={"run_id": run_id})
    item   = resp.get("Item")
    if not item:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(item)


# ─── Health ───────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)