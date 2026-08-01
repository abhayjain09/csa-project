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

Answering Agent endpoints:
  GET    /api/answering-agent/questionnaires              List questionnaire MD files in S3
  POST   /api/answering-agent/run                         Trigger answering run for a company
  GET    /api/answering-agent/runs/<run_id>               Poll run status
  GET    /api/answering-agent/companies                   List all companies with results
  GET    /api/answering-agent/companies/<slug>            Get categories for a company
  GET    /api/answering-agent/companies/<slug>/<category> Get Q&A for one category

Shared:
  GET    /health                             Health check
"""

"""
Patches in this revision (1-6, plus 7-8 below):
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
     split into chunks of AGENT_CHUNK_SIZE (default 1) and invoked with a
     BOUNDED thread pool (AGENT_CHUNK_CONCURRENCY, default 3 in flight).
     Each chunk renumbers its queries web_query1.. so the agent always sees a
     small, normal payload. There is still exactly ONE reportiq-runs row per
     company: downloaded results are merged + deduped by s3_key across chunks
     and the row is flushed after each chunk so the UI shows the list grow.
     The per-chunk read timeout is configured separately below and remains
     above the agent's own per-query deadline.
  7. PER-QUERY RESULT TRACKING + MANUAL UPLOAD FALLBACK.
     Each chunk's diagnostics now include a `results` list — one entry per
     query in that chunk with either a matched downloaded file or a
     'failed' status — so the portal can render a per-query row instead of
     only chunk-level counts. This is a best-effort pairing: if the agent
     response echoes the stable request_id assigned to each web query. Exact
     original-query matching is the compatibility fallback; positional
     matching is forbidden because it can attach a later successful document
     to an earlier failed question. A new POST /api/sources/upload route lets
     the portal fall
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
  9. WAF BROWSER FALLBACK. A typed blocked_by_source_waf result creates an
     idempotent DynamoDB job and launches a one-off Chromium task on the
     existing ECS cluster. Worker and read-path reconciliation update the
     original per-query row without racing the normal chunk flush.
 10. BULK COMPANY QUEUE. Multi-company submissions persist every run as queued
     and execute at most BULK_COMPANY_CONCURRENCY companies simultaneously
     (default 3); each completion automatically starts the next company.
 11. CLASS-SCOPED, PDF-SAFE STORAGE. Chunk payloads now include structured
     report_class values so AgentCore never falls back to "uncategorized".
     Manual uploads and presigned downloads reject mislabeled/corrupt PDFs.
"""
import os, json, uuid, re, threading, hashlib, logging, urllib.request, urllib.error
import time, random
import unicodedata
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import PurePosixPath
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, quote
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from werkzeug.exceptions import NotFound as WerkzeugNotFound
import boto3
import botocore.auth
import botocore.awsrequest
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError, UnknownServiceError
from pypdf import PdfReader

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

# Durable browser fallback. AgentCore remains the synchronous discovery tier;
# only a typed blocked_by_source_waf result can launch this longer ECS task.
BROWSER_WORKER_ENABLED = os.environ.get(
    "BROWSER_WORKER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
BROWSER_JOBS_TABLE = os.environ.get(
    "BROWSER_JOBS_TABLE", "reportiq-browser-jobs")
BROWSER_ECS_CLUSTER = os.environ.get("BROWSER_ECS_CLUSTER", "")
BROWSER_ECS_TASK_DEFINITION = os.environ.get(
    "BROWSER_ECS_TASK_DEFINITION", "")
BROWSER_ECS_CONTAINER_NAME = os.environ.get(
    "BROWSER_ECS_CONTAINER_NAME", "browser-worker")
BROWSER_ECS_SUBNET_IDS = [
    item.strip() for item in os.environ.get(
        "BROWSER_ECS_SUBNET_IDS", "").split(",") if item.strip()]
BROWSER_ECS_SECURITY_GROUP_IDS = [
    item.strip() for item in os.environ.get(
        "BROWSER_ECS_SECURITY_GROUP_IDS", "").split(",") if item.strip()]
BROWSER_ECS_ASSIGN_PUBLIC_IP = os.environ.get(
    "BROWSER_ECS_ASSIGN_PUBLIC_IP", "false").strip().lower() in {
        "1", "true", "yes", "on"}

# PageIndex runtime
PAGEINDEX_RUNTIME_ARN = os.environ.get(
    "PAGEINDEX_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:610639371721:runtime/pageindex_runtime-rucFhA3V8V")
PAGEINDEX_QUALIFIER   = os.environ.get("PAGEINDEX_QUALIFIER",   "DEFAULT")
PAGEINDEX_RUNS_TABLE  = os.environ.get("PAGEINDEX_RUNS_TABLE",  "pageindex-runs")

# Answering Agent runtime
ANSWERING_RUNTIME_ARN   = os.environ.get(
    "ANSWERING_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:610639371721:runtime/report_iq_aswering_agent_dev-0cS5fh9bFb")
ANSWERING_QUALIFIER      = os.environ.get("ANSWERING_QUALIFIER",     "DEFAULT")
ANSWERING_RUNS_TABLE     = os.environ.get("ANSWERING_RUNS_TABLE",    "answering-runs")
ANSWERING_RESULTS_TABLE  = os.environ.get("ANSWERING_RESULTS_TABLE", "answering-results")
QUESTIONNAIRES_PREFIX    = os.environ.get("QUESTIONNAIRES_PREFIX",   "questionnaires/")
QUESTIONNAIRES_BUCKET    = os.environ.get("QUESTIONNAIRES_BUCKET",   REPORTS_BUCKET)

# Per-CHUNK read timeout. Each invoke carries only AGENT_CHUNK_SIZE web queries.
# Raised 300s -> 600s -> 890s -> 1620s.
#
# CORRECTION (supersedes the old "900s hard wall" reasoning): 900s is NOT an
# AgentCore execution ceiling. It is the DEFAULT `idleRuntimeSessionTimeout`,
# and the agent already runs a HEALTHY_BUSY ping loop every 20s
# (agent/agent.py `invoke` entrypoint) that resets that idle timer for the whole
# duration of a query — so the session stays alive far past 900s, up to
# `maxLifetime` (default 8h, now pinned explicitly in the runtime's
# lifecycleConfiguration; see main.tf). Confirmed by production logs where
# individual queries completed server-side at 400-736s without being reaped.
#
# The real bug this timeout had was a 10-SECOND INVERSION: it sat at 890s while
# the agent's own per-query deadline (QUERY_MAX_SECONDS) was 900s — i.e. the
# client hung up 10s BEFORE the agent was even allowed to fail closed and
# return, so any near-limit query was a guaranteed false-failure. The invariant
# must be: QUERY_MAX_SECONDS < AGENT_READ_TIMEOUT < idleRuntimeSessionTimeout.
# With QUERY_MAX_SECONDS now 1500s (25-min hard cap per query), this is raised
# to 1620s: the client waits for the agent's full deadline plus ~2 min to store
# the result and serialize the JSON response, and still sits below the 1800s
# idle-session timeout. Override via AGENT_READ_TIMEOUT if you retune the ladder.
AGENT_READ_TIMEOUT = int(os.environ.get("AGENT_READ_TIMEOUT", "1620"))

# Chunking: how many web_query* fields per AgentCore invoke, and how many
# invokes may run concurrently. The default of 3 is faster than sequential
# execution while remaining bounded to reduce WebSearch/AgentCore throttling.
AGENT_CHUNK_SIZE        = int(os.environ.get("AGENT_CHUNK_SIZE",        "1"))
AGENT_CHUNK_CONCURRENCY = int(os.environ.get("AGENT_CHUNK_CONCURRENCY", "3"))
BULK_COMPANY_CONCURRENCY = max(
    1, int(os.environ.get("BULK_COMPANY_CONCURRENCY", "4")))

# A full-company run has one explicit dependency: acquire the Annual Report
# before launching independent searches. Annual Report analysis is deliberately
# deferred until those searches finish, and is limited to their clean misses.
# Only classes whose
# product contract permits a substantive section of a broader report as the
# final fallback are eligible for an Annual Report reference. Authoritative
# standalone filings such as Proxy Statements and Wolfsberg Questionnaires are
# deliberately excluded.
ANNUAL_REPORT_REFERENCE_CLASSES = (
    "code of conduct",
    "anti-bribery and corruption policy",
    "conflicts of interest policy",
    "insider trading policy",
    "discrimination and harassment policy",
    "supplier code of conduct",
    "whistleblowing mechanism",
    "sustainability report",
    "ghg emission report",
    "environmental policy",
    "environment, health & safety policy",
    "occupational health & safety policy",
    "biodiversity policy",
    "impact report",
    "human rights policy",
    "human rights due diligence",
    "modern slavery statement",
    "remuneration report",
    "risk management policy",
    "tax strategy and governance",
)

# One executor per backend process. A single bulk request is handled by one
# process, so ten submitted companies occupy at most three worker threads while
# the remaining seven stay queued in submission order.
_BULK_COMPANY_EXECUTOR = ThreadPoolExecutor(
    max_workers=BULK_COMPANY_CONCURRENCY,
    thread_name_prefix="reportiq-bulk-company",
)

# ─── Manual "kill" signal for in-flight runs ───────────────────────────────────
# In-memory only — sufficient because the Dockerfile pins gunicorn to a single
# worker process (same assumption _BULK_COMPANY_EXECUTOR already relies on).
# DELETE /api/runs/<run_id> flags the run here, then deletes its DynamoDB row.
# _do_invoke_inner checks this flag between chunks and, once it sees it, stops
# dispatching further work and returns WITHOUT writing to the run row again —
# otherwise update_item's default upsert behaviour would silently recreate the
# row we just deleted.
_KILLED_RUN_IDS: set = set()
_KILLED_RUN_IDS_LOCK = threading.Lock()


def _mark_run_killed(run_id: str) -> None:
    with _KILLED_RUN_IDS_LOCK:
        _KILLED_RUN_IDS.add(run_id)


def _consume_run_kill(run_id: str) -> bool:
    """Return True (and forget) exactly once if this run_id was killed."""
    with _KILLED_RUN_IDS_LOCK:
        if run_id in _KILLED_RUN_IDS:
            _KILLED_RUN_IDS.remove(run_id)
            return True
    return False


def _is_run_killed(run_id: str) -> bool:
    """Peek without clearing — used mid-loop to decide whether to keep going."""
    with _KILLED_RUN_IDS_LOCK:
        return run_id in _KILLED_RUN_IDS

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
# chunks are left. MUST stay above AGENT_READ_TIMEOUT (now 1620s = 27 min) with
# margin — raised in lockstep to 30 min so a genuinely slow chunk gets the chance to
# hit its OWN timeout and report an error result (which itself refreshes the
# heartbeat) before the reconciler would otherwise conclude the thread is dead.
# 30 min (1800s) also matches the runtime's idleRuntimeSessionTimeout ceiling, so the
# reconciler never declares a run dead while AgentCore itself would still keep the
# session alive. If you retune the timeout ladder, keep this the largest value.
HEARTBEAT_STALE_MINUTES = int(os.environ.get("AGENT_HEARTBEAT_STALE_MINUTES", "30"))

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


def _pdf_integrity_error(filename: str, content_type: str, body: bytes) -> str:
    """Return an error for a mislabeled/unreadable PDF, otherwise an empty string."""
    expects_pdf = (
        (filename or "").lower().endswith(".pdf")
        or "pdf" in (content_type or "").lower()
    )
    if not expects_pdf:
        return ""
    if not body:
        return "PDF is empty"
    if b"%PDF-" not in body[:1024]:
        return "file is not a PDF (missing %PDF header)"
    try:
        reader = PdfReader(BytesIO(body), strict=False)
        if reader.is_encrypted:
            return "encrypted PDFs are not supported"
        if len(reader.pages) < 1:
            return "PDF has no readable pages"
    except Exception as exc:
        return f"PDF parse failed: {type(exc).__name__}"
    return ""


def _safe_manual_source_url(value: str) -> str:
    """Keep only a bounded HTTPS source URL supplied by the recovery UI."""
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2048:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if (parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username or parsed.password):
        return ""
    return candidate


def _s3_pdf_integrity_error(s3, s3_key: str) -> str:
    """Cheaply reject existing HTML/XML error objects mislabeled as PDFs."""
    if not (s3_key or "").lower().endswith(".pdf"):
        return ""
    try:
        response = s3.get_object(
            Bucket=REPORTS_BUCKET,
            Key=s3_key,
            Range="bytes=0-1023",
        )
        prefix = response["Body"].read()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return "S3 object does not exist"
        raise
    if b"%PDF-" not in prefix[:1024]:
        return "S3 object is not a PDF; it is likely an HTML/XML error response"
    return ""


def get_ecs():
    return boto3.client("ecs", region_name=REGION)

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


AGENT_THROTTLE_MAX_RETRIES = int(os.environ.get("AGENT_THROTTLE_MAX_RETRIES", "3"))


def _is_throttling_error(e: Exception) -> bool:
    """True for a ThrottlingException/429 from either invoke path.

    Safe to retry (unlike a read-timeout): a throttled call never reached
    the agent, so re-sending the same payload cannot double-run it.
    """
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ThrottlingException", "TooManyRequestsException"):
            return True
    text = str(e).lower()
    return "throttl" in text or "too many requests" in text or "http 429" in text


def _invoke_agentcore_http_with_retry(payload_bytes: bytes) -> bytes:
    """_invoke_agentcore_http, retrying only on throttling with backoff."""
    attempt = 0
    while True:
        try:
            return _invoke_agentcore_http(payload_bytes)
        except Exception as e:  # noqa: BLE001
            if not _is_throttling_error(e) or attempt >= AGENT_THROTTLE_MAX_RETRIES:
                raise
            wait = 1.5 * (2 ** attempt) + random.uniform(0.0, 0.5)
            attempt += 1
            log.warning(
                "[agentcore] throttled (attempt %d/%d) — retrying in %.1fs: %s",
                attempt, AGENT_THROTTLE_MAX_RETRIES, wait, e)
            time.sleep(wait)


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
    """Reset one company's page index before a fresh run.

    Only deletes the pageindex JSON file from S3 — never touches the
    source PDFs. Provenance records are also cleared so rag_status
    resets to Pending and the UI shows documents as unindexed.

    Query definitions and historical run rows are deliberately retained.
    """
    if dynamo is None:
        dynamo = get_dynamo()
    if s3 is None:
        s3 = get_s3()

    company_slug = _agent_slug(company)
    prefix       = company_slug + "/"
    deleted_s3   = 0

    # ONLY delete the pageindex JSON — never touch source PDFs
    pageindex_key = _pageindex_s3_key(prefix, company_slug)
    try:
        s3.delete_object(Bucket=REPORTS_BUCKET, Key=pageindex_key)
        deleted_s3 = 1
        log.info("[cleanup] deleted pageindex JSON: %s", pageindex_key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("NoSuchKey", "404"):
            raise
        log.info("[cleanup] pageindex JSON not found, skipping: %s", pageindex_key)

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
        "company":            company,
        "company_slug":       company_slug,
        "s3_deleted":         deleted_s3,
        "provenance_deleted": deleted_provenance,
    }
    log.info("[fresh-run-cleanup] company=%r slug=%s s3=%d provenance=%d",
             company, company_slug, deleted_s3, deleted_provenance)
    return summary

def _list_s3_files_for_run(company: str, run_id: str) -> list:
    """
    List report objects belonging to this exact run.

    Metadata sidecars and unrelated historical objects under the same company
    prefix are deliberately excluded; presenting either as this run's download
    can create stale links or JSON files masquerading as reports.
    """
    s3 = get_s3()
    variants = _company_prefix_variants(company)
    log.info("[s3-match] company=%r trying prefixes=%s", company, variants)
    results = []
    seen = set()
    report_suffixes = (
        ".pdf", ".doc", ".docx", ".rtf", ".xlsx", ".xls", ".xlsm")
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for prefix in variants:
            for page in paginator.paginate(Bucket=REPORTS_BUCKET, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key in seen or not key.lower().endswith(report_suffixes):
                        continue
                    seen.add(key)
                    head = s3.head_object(Bucket=REPORTS_BUCKET, Key=key)
                    object_run_id = (
                        head.get("Metadata", {}).get("run_id", "").strip())
                    if run_id and object_run_id != run_id:
                        log.info(
                            "[s3-match] skipping key from another/legacy run: %s",
                            key,
                        )
                        continue
                    results.append({
                        "s3_key":        key,
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
    identity = raw.get("company_identity")
    if not isinstance(identity, dict):
        identity = {}
    return {
        "alias_query_count": alias_query_count,
        "keys": sorted(raw.keys())[:20],   # visibility without the heavy payload
        "identity_status": identity.get("status"),
        "identity_method": identity.get("method"),
        "identity_ticker": identity.get("ticker"),
        "identity_cik": identity.get("cik"),
    }


def _pair_queries_with_results(chunk_queries: list, downloaded: list,
                               failures: list, agent_results: list | None = None,
                               chunk_index: int = 0) -> list:
    """
    PATCH #7: best-effort per-query status for the UI.

    Prefer the stable request_id assigned by this backend and echoed by the
    agent. Exact original-query matching is the compatibility fallback for an
    older agent. Positional pairing is intentionally forbidden: when earlier
    queries fail and a later query succeeds, positional pairing maps the later
    document to the wrong question — the Ball/Freeport fan-out failure mode.

    Every query in the chunk gets exactly one result entry:
      {"query": ..., "status": "downloaded", "s3_key": ..., "file_name": ...,
       "source_url": ...}
    or
      {"query": ..., "status": "failed"}

    A query with no matched file is 'failed' so the portal can offer a manual
    upload button for that specific query.
    """
    dl = [d for d in (downloaded or []) if isinstance(d, dict)]
    authoritative = [
        item for item in (agent_results or []) if isinstance(item, dict)
    ]
    by_request_id = {
        str(item.get("request_id")): item
        for item in authoritative
        if item.get("request_id")
    }
    by_exact_query = {}
    for item in authoritative + dl:
        query = item.get("query") or item.get("original_query")
        if query:
            by_exact_query[str(query)] = item

    results = []
    for position, q in enumerate(chunk_queries, start=1):
        request_id = f"{chunk_index}:{position}"
        match = by_request_id.get(request_id) or by_exact_query.get(str(q))
        if match and match.get("status") == "blocked_by_source_waf":
            browser_job_status = match.get("browser_job_status")
            queued = bool(
                match.get("browser_job_id")
                and browser_job_status in {
                    None, "queued", "launched", "running"})
            results.append({
                "request_id": request_id,
                "query": q,
                "status": (
                    "browser_retry_queued" if queued
                    else "blocked_by_source_waf"),
                "reason": match.get("reason") or (
                    "The official source blocked automated access."),
                "browser_job_id": match.get("browser_job_id", ""),
                "candidate_urls": match.get("candidate_urls") or [],
                "candidates_verified": match.get("candidates_verified"),
                "annual_report_reference_eligible": False,
                "report_class": match.get("report_class"),
                "official_domain": match.get("official_domain"),
            })
            continue
        if match and match.get("status") not in {"failed", "no_document_found"}:
            key = match.get("s3_key") or match.get("key") or ""
            if not key:
                results.append({
                    "request_id": request_id,
                    "query": q,
                    "status": "failed",
                    "reason": "agent success result had no S3 key",
                })
                continue
            results.append({
                "request_id": request_id,
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
                "duplicate":  bool(match.get("duplicate")
                                   or match.get("status") == "duplicate"),
                "annual_report_reference_eligible": False,
            })
        else:
            reason = (match or {}).get("reason") or "no exact agent result mapping"
            clean_discovery_miss = bool(
                match and match.get("status") in {
                    "failed", "no_document_found"})
            results.append({
                "request_id": request_id,
                "query": q,
                "status": "failed",
                "reason": reason,
                # This explicit bit prevents transport/storage/mapping errors
                # that happen to render as "failed" from being mistaken for a
                # clean discovery miss by the later Annual Report fallback.
                "annual_report_reference_eligible": clean_discovery_miss,
            })
    return results


def _enqueue_browser_retries(agent_results: list, company: str, run_id: str,
                             query_id: str) -> list:
    """Create idempotent browser jobs and launch one-off tasks on this cluster.

    The agent controls admission by returning the typed WAF status. Candidate
    URLs are bounded, exact URLs discovered for the requested report, and the
    worker independently revalidates scheme/domain/content before storing.
    """
    results = [
        dict(item) if isinstance(item, dict) else item
        for item in (agent_results or [])
    ]
    if not BROWSER_WORKER_ENABLED:
        return results
    required = (
        BROWSER_ECS_CLUSTER,
        BROWSER_ECS_TASK_DEFINITION,
        BROWSER_ECS_SUBNET_IDS,
        BROWSER_ECS_SECURITY_GROUP_IDS,
    )
    if not all(required):
        log.error("[browser-fallback] enabled but ECS network/task settings "
                  "are incomplete; refusing to launch")
        return results

    jobs = get_dynamo().Table(BROWSER_JOBS_TABLE)
    ecs = get_ecs()
    now_iso = datetime.now(timezone.utc).isoformat()
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "blocked_by_source_waf":
            continue
        candidates = [
            str(url).strip() for url in (item.get("candidate_urls") or [])
            if str(url).strip().lower().startswith("https://")
        ][:8]
        if not candidates:
            continue
        request_id = str(item.get("request_id") or "")
        identity = "\n".join([run_id, request_id, *candidates])
        job_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        record = {
            "job_id": job_id,
            "run_id": run_id,
            "query_id": query_id,
            "request_id": request_id,
            "company": company,
            "query": str(item.get("query") or ""),
            "prepared_query": str(item.get("prepared_query") or ""),
            "report_class": str(item.get("report_class") or ""),
            "year": str(item.get("year") or ""),
            "preferred_language": str(
                item.get("preferred_language") or "en"),
            "prefer_latest": bool(item.get("prefer_latest", True)),
            "official_domain": str(item.get("official_domain") or ""),
            "candidate_urls": json.dumps(candidates),
            "status": "queued",
            "created_at": now_iso,
            "updated_at": now_iso,
            "expires_at": int(
                (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),
        }
        try:
            jobs.put_item(
                Item=record,
                ConditionExpression="attribute_not_exists(job_id)",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                existing = jobs.get_item(
                    Key={"job_id": job_id}).get("Item", {})
                existing_status = existing.get("status", "queued")
                if existing_status in {"queued", "launched", "running"}:
                    item["browser_job_id"] = job_id
                elif existing_status == "downloaded" and existing.get("s3_key"):
                    item.update({
                        "status": "downloaded",
                        "s3_key": existing["s3_key"],
                        "source_url": existing.get("source_url", ""),
                        "duplicate": bool(existing.get("duplicate")),
                        "browser_job_id": job_id,
                    })
                item["browser_job_status"] = existing_status
                continue
            log.error("[browser-fallback] job create failed %s: %s",
                      job_id, exc)
            continue

        try:
            response = ecs.run_task(
                cluster=BROWSER_ECS_CLUSTER,
                taskDefinition=BROWSER_ECS_TASK_DEFINITION,
                launchType="FARGATE",
                count=1,
                platformVersion="LATEST",
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": BROWSER_ECS_SUBNET_IDS,
                        "securityGroups": BROWSER_ECS_SECURITY_GROUP_IDS,
                        "assignPublicIp": (
                            "ENABLED" if BROWSER_ECS_ASSIGN_PUBLIC_IP
                            else "DISABLED"),
                    },
                },
                overrides={
                    "containerOverrides": [{
                        "name": BROWSER_ECS_CONTAINER_NAME,
                        "environment": [
                            {"name": "BROWSER_JOB_ID", "value": job_id},
                        ],
                    }],
                },
                startedBy="reportiq-waf-fallback",
                tags=[
                    {"key": "ReportIqRunId", "value": run_id},
                    {"key": "ReportIqBrowserJobId", "value": job_id},
                ],
                enableECSManagedTags=True,
                propagateTags="TASK_DEFINITION",
            )
            failures = response.get("failures") or []
            tasks = response.get("tasks") or []
            if failures or not tasks:
                raise RuntimeError(
                    f"ECS RunTask returned no task: {failures}")
            task_arn = tasks[0].get("taskArn", "")
            jobs.update_item(
                Key={"job_id": job_id},
                UpdateExpression=(
                    "SET #st = :s, task_arn = :t, updated_at = :u"),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":s": "launched", ":t": task_arn, ":u": now_iso},
            )
            item["browser_job_id"] = job_id
            item["browser_job_status"] = "launched"
            log.info("[browser-fallback] launched job=%s task=%s run=%s",
                     job_id, task_arn.rsplit("/", 1)[-1], run_id[:8])
        except Exception as exc:
            jobs.update_item(
                Key={"job_id": job_id},
                UpdateExpression=(
                    "SET #st = :s, error_msg = :e, updated_at = :u"),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":s": "launch_failed",
                    ":e": str(exc)[:1000],
                    ":u": datetime.now(timezone.utc).isoformat(),
                },
            )
            item["browser_job_id"] = job_id
            item["browser_job_status"] = "launch_failed"
            log.error("[browser-fallback] launch failed job=%s: %s",
                      job_id, exc)
    return results


def _refresh_browser_retry_run(run: dict, dynamo=None) -> dict:
    """Reconcile terminal browser jobs into a run.

    The worker normally performs this patch itself. This read-path safety net
    also handles a task that downloaded the file but exited before patching, or
    a Fargate task that stopped before Python started (for example image-pull or
    network initialization failure).
    """
    if run.get("status") != "browser_retry_pending":
        return run
    dynamo = dynamo or get_dynamo()
    jobs_table = dynamo.Table(BROWSER_JOBS_TABLE)
    response = jobs_table.scan(
        FilterExpression="#run = :run",
        ExpressionAttributeNames={"#run": "run_id"},
        ExpressionAttributeValues={":run": run.get("run_id", "")},
    )
    jobs = response.get("Items", [])
    while response.get("LastEvaluatedKey"):
        response = jobs_table.scan(
            ExclusiveStartKey=response["LastEvaluatedKey"],
            FilterExpression="#run = :run",
            ExpressionAttributeNames={"#run": "run_id"},
            ExpressionAttributeValues={":run": run.get("run_id", "")},
        )
        jobs.extend(response.get("Items", []))
    if not jobs:
        return run

    active_statuses = {"queued", "launched", "running"}
    task_arns = [
        job.get("task_arn") for job in jobs
        if job.get("status") in active_statuses and job.get("task_arn")
    ]
    if task_arns and BROWSER_ECS_CLUSTER:
        try:
            descriptions = get_ecs().describe_tasks(
                cluster=BROWSER_ECS_CLUSTER, tasks=task_arns)
            tasks_by_arn = {
                task.get("taskArn"): task
                for task in descriptions.get("tasks", [])
            }
            for job in jobs:
                if job.get("status") not in active_statuses:
                    continue
                task = tasks_by_arn.get(job.get("task_arn"))
                if not task or task.get("lastStatus") != "STOPPED":
                    continue
                reason = (
                    task.get("stoppedReason")
                    or "ECS task stopped before worker reported a result")
                try:
                    jobs_table.update_item(
                        Key={"job_id": job["job_id"]},
                        UpdateExpression=(
                            "SET #st = :st, error_msg = :e, "
                            "updated_at = :u"),
                        ConditionExpression="#st IN (:q, :l, :r)",
                        ExpressionAttributeNames={"#st": "status"},
                        ExpressionAttributeValues={
                            ":st": "failed",
                            ":q": "queued",
                            ":l": "launched",
                            ":r": "running",
                            ":e": reason[:1000],
                            ":u": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    job["status"] = "failed"
                    job["error_msg"] = reason[:1000]
                except ClientError as exc:
                    if exc.response.get("Error", {}).get(
                            "Code") != "ConditionalCheckFailedException":
                        raise
                    refreshed = jobs_table.get_item(
                        Key={"job_id": job["job_id"]}).get("Item", {})
                    job.update(refreshed)
        except Exception as exc:
            log.warning("[browser-fallback] task reconciliation failed for "
                        "run=%s: %s", run.get("run_id", "")[:8], exc)

    terminal = {
        "downloaded", "blocked_by_source_waf", "failed", "launch_failed",
    }
    if not all(job.get("status") in terminal for job in jobs):
        return run

    try:
        downloaded = json.loads(run.get("downloaded") or "[]")
    except Exception:
        downloaded = []
    try:
        failures = json.loads(run.get("failures") or "[]")
    except Exception:
        failures = []
    try:
        diagnostics = json.loads(run.get("diagnostics") or "{}")
    except Exception:
        diagnostics = {}

    successful_ids = set()
    jobs_by_request = {}
    for job in jobs:
        request_id = job.get("request_id", "")
        jobs_by_request[request_id] = job
        if job.get("status") != "downloaded" or not job.get("s3_key"):
            continue
        successful_ids.add(request_id)
        result = {
            "s3_key": job["s3_key"],
            "file_name": job["s3_key"].rsplit("/", 1)[-1],
            "source_url": job.get("source_url", ""),
            "duplicate": bool(job.get("duplicate")),
            "browser_job_id": job.get("job_id", ""),
        }
        if not any(
                isinstance(item, dict)
                and item.get("s3_key") == result["s3_key"]
                for item in downloaded):
            downloaded.append(result)
    failures = [
        item for item in failures
        if not (isinstance(item, dict)
                and item.get("request_id") in successful_ids)
    ]
    for chunk in diagnostics.get("per_chunk", []):
        for row in chunk.get("results", []):
            job = jobs_by_request.get(row.get("request_id", ""))
            if not job:
                continue
            if job.get("status") == "downloaded" and job.get("s3_key"):
                row.update({
                    "status": "downloaded",
                    "s3_key": job["s3_key"],
                    "file_name": job["s3_key"].rsplit("/", 1)[-1],
                    "source_url": job.get("source_url", ""),
                    "duplicate": bool(job.get("duplicate")),
                    "browser_job_id": job.get("job_id", ""),
                })
                row.pop("reason", None)
            else:
                row["status"] = job.get("status", "failed")
                row["reason"] = job.get(
                    "error_msg", "long-running browser did not download")

    final_status = "complete" if downloaded else "no_results"
    old_version = int(run.get("browser_patch_version", 0))
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        dynamo.Table(RUNS_TABLE).update_item(
            Key={"run_id": run["run_id"]},
            UpdateExpression=(
                "SET #st = :st, #dl = :dl, #fl = :fl, #dg = :dg, "
                "#ver = :new, #fin = :fin"),
            ConditionExpression=(
                "(attribute_not_exists(#ver) OR #ver = :old)"),
            ExpressionAttributeNames={
                "#st": "status", "#dl": "downloaded", "#fl": "failures",
                "#dg": "diagnostics", "#ver": "browser_patch_version",
                "#fin": "finished_at",
            },
            ExpressionAttributeValues={
                ":st": final_status,
                ":dl": json.dumps(downloaded),
                ":fl": json.dumps(failures),
                ":dg": json.dumps(diagnostics),
                ":old": old_version,
                ":new": old_version + 1,
                ":fin": now_iso,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get(
                "Code") == "ConditionalCheckFailedException":
            return dynamo.Table(RUNS_TABLE).get_item(
                Key={"run_id": run["run_id"]}).get("Item", run)
        raise
    query_id = run.get("query_id", "")
    if query_id:
        dynamo.Table(QUERIES_TABLE).update_item(
            Key={"query_id": query_id},
            UpdateExpression="SET #st = :st, updated_at = :u",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":st": final_status, ":u": now_iso},
        )
    run.update({
        "status": final_status,
        "downloaded": json.dumps(downloaded),
        "failures": json.dumps(failures),
        "diagnostics": json.dumps(diagnostics),
        "browser_patch_version": old_version + 1,
        "finished_at": now_iso,
    })
    return run


def _refresh_timed_out_queries(run: dict, dynamo=None) -> dict:
    """Read-path reconciliation for queries left 'timed_out_pending_check' by
    _invoke_one_chunk (see AGENT_READ_TIMEOUT above for why a client read-
    timeout is not evidence the agent actually failed).

    Same idea as _refresh_browser_retry_run, applied to a different async gap:
    there, a browser worker keeps running after the invoking request returns;
    here, AgentCore's OWN synchronous invoke keeps running after the client
    gave up on it. Both cases need something to check back later for a result
    that may still land. This checks the shared provenance table (the same
    table agent.py itself writes to) for a document matching the timed-out
    query's company + report_class, stamped AFTER this specific invoke
    started — and only gives up (finalizes 'failed') once
    HEARTBEAT_STALE_MINUTES has passed with nothing found, mirroring exactly
    how long the reconciler itself waits before concluding a run's own invoke
    thread is genuinely dead rather than just slow.
    """
    try:
        diagnostics = json.loads(run.get("diagnostics") or "{}")
    except Exception:
        return run
    per_chunk = diagnostics.get("per_chunk", [])
    pending_rows = [
        row for chunk in per_chunk for row in chunk.get("results", [])
        if isinstance(row, dict)
        and row.get("status") == "timed_out_pending_check"
    ]
    if not pending_rows:
        return run  # cheap no-op for the overwhelming majority of reads

    dynamo = dynamo or get_dynamo()
    company_slug = _agent_slug(run.get("company", ""))
    provenance_items = []
    try:
        provenance = dynamo.Table(PROVENANCE_TABLE)
        query_args = {
            "KeyConditionExpression": "#company = :company",
            "ExpressionAttributeNames": {"#company": "company"},
            "ExpressionAttributeValues": {":company": company_slug},
        }
        while True:
            resp = provenance.query(**query_args)
            provenance_items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_args["ExclusiveStartKey"] = last_key
    except Exception as exc:
        log.warning("[timeout-recheck] provenance query failed for run=%s "
                   "company=%s: %s", run.get("run_id", "")[:8], company_slug, exc)
        return run

    now = datetime.now(timezone.utc)
    already_claimed_keys = {
        item.get("s3_key") for item in json.loads(run.get("downloaded") or "[]")
        if isinstance(item, dict)
    }
    newly_downloaded = []
    newly_failed_ids = set()
    changed = False

    for row in pending_rows:
        invoked_at = str(row.get("invoked_at") or "")
        report_class = row.get("report_class") or ""
        match = None
        for item in provenance_items:
            if item.get("doc_class") != report_class:
                continue
            downloaded_at = str(item.get("downloaded") or "")
            s3_key = item.get("s3_key")
            if (downloaded_at > invoked_at and s3_key
                    and s3_key not in already_claimed_keys):
                match = item
                break
        if match:
            result = {
                "s3_key": match["s3_key"],
                "file_name": match["s3_key"].rsplit("/", 1)[-1],
                "source_url": match.get("source_url", ""),
                "duplicate": False,
                "request_id": row.get("request_id", ""),
            }
            row.update({
                "status": "downloaded",
                "s3_key": match["s3_key"],
                "file_name": result["file_name"],
                "source_url": result["source_url"],
                "duplicate": False,
            })
            row.pop("reason", None)
            newly_downloaded.append(result)
            already_claimed_keys.add(match["s3_key"])
            changed = True
            log.info("[timeout-recheck] run=%s request_id=%s recovered "
                    "s3_key=%s after client read-timeout",
                    run.get("run_id", "")[:8], row.get("request_id"),
                    match["s3_key"])
            continue
        try:
            invoked_dt = datetime.fromisoformat(invoked_at.replace("Z", "+00:00"))
            elapsed_minutes = (now - invoked_dt).total_seconds() / 60
        except Exception:
            elapsed_minutes = HEARTBEAT_STALE_MINUTES + 1  # malformed timestamp — don't wait forever
        if elapsed_minutes > HEARTBEAT_STALE_MINUTES:
            row["status"] = "failed"
            row["reason"] = (
                f"AgentCore did not respond within the client read timeout, "
                f"and no matching document appeared in provenance within "
                f"{HEARTBEAT_STALE_MINUTES} minutes afterward.")
            newly_failed_ids.add(row.get("request_id", ""))
            changed = True
            log.info("[timeout-recheck] run=%s request_id=%s finalized as "
                    "failed after %.1f min with no matching document",
                    run.get("run_id", "")[:8], row.get("request_id"),
                    elapsed_minutes)
        # else: still within the recheck window — leave pending, try again
        # on the next read-path poll.

    if not changed:
        return run

    try:
        downloaded = json.loads(run.get("downloaded") or "[]")
    except Exception:
        downloaded = []
    try:
        failures = json.loads(run.get("failures") or "[]")
    except Exception:
        failures = []
    downloaded.extend(newly_downloaded)
    for row in pending_rows:
        if row.get("status") == "failed" and row.get("request_id") in newly_failed_ids:
            failures.append({
                "request_id": row.get("request_id", ""),
                "query": row.get("query", ""),
                "reason": row.get("reason", ""),
            })

    old_version = int(run.get("timeout_patch_version", 0))
    try:
        dynamo.Table(RUNS_TABLE).update_item(
            Key={"run_id": run["run_id"]},
            UpdateExpression=(
                "SET #dl = :dl, #fl = :fl, #dg = :dg, #ver = :new"),
            ConditionExpression="(attribute_not_exists(#ver) OR #ver = :old)",
            ExpressionAttributeNames={
                "#dl": "downloaded", "#fl": "failures",
                "#dg": "diagnostics", "#ver": "timeout_patch_version",
            },
            ExpressionAttributeValues={
                ":dl": json.dumps(downloaded),
                ":fl": json.dumps(failures),
                ":dg": json.dumps(diagnostics),
                ":old": old_version,
                ":new": old_version + 1,
            },
        )
        run.update({
            "downloaded": json.dumps(downloaded),
            "failures": json.dumps(failures),
            "diagnostics": json.dumps(diagnostics),
            "timeout_patch_version": old_version + 1,
        })
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Another reader/writer patched this run first — fine, they made
            # equivalent forward progress; pick it up fresh next poll instead
            # of retrying and risking a double-append.
            return dynamo.Table(RUNS_TABLE).get_item(
                Key={"run_id": run["run_id"]}).get("Item", run)
        raise
    return run


def _patch_run_with_upload(run_id: str, s3_key: str, file_name: str, query: str,
                           chunk: str, dynamo=None,
                           source_url: str = "") -> dict:
    """
    PATCH #7 (+ #9, + manual-replace fix below): after a manual upload
    succeeds, patch the run row so the portal's next refresh shows a Download
    button for that query, AND so the run-list "Failures" count actually
    drops:
      - replace/append the file in the run's `downloaded` list (dedup by
        s3_key; an existing entry for the SAME query's previous s3_key, if
        any, is dropped rather than left alongside the new one)
      - flip the matching per-query row's status to 'downloaded' inside
        diagnostics.per_chunk[*].results (matched by chunk index + query text
        when both are supplied; falls back to matching by query text alone).
        This now OVERWRITES an already-'downloaded' row too — the agent can
        grab the wrong document, and a manual upload against the same query
        must be able to correct it, not just fill in a missing one.
      - PATCH #9: remove the matching entry from the run's top-level
        `failures` list too. Entries in `failures` may be plain query strings
        or dicts carrying a "query"/"web_query" key (the agent's
        no_document_found shape isn't fully pinned down yet), so both are
        matched.
    Returns {"patched": bool, "old_s3_key": str|None}. `old_s3_key` is set
    when this upload superseded a different s3_key for the same query — the
    caller is responsible for deleting that now-orphaned object from S3 (see
    upload_source()). "patched" is False if the run row doesn't exist (the
    upload + provenance write still succeed independently — this patch is
    purely a UI/cleanup convenience).
    """
    if dynamo is None:
        dynamo = get_dynamo()
    tbl  = dynamo.Table(RUNS_TABLE)
    item = tbl.get_item(Key={"run_id": run_id}).get("Item")
    if not item:
        return {"patched": False, "old_s3_key": None}

    try:
        downloaded = json.loads(item.get("downloaded") or "[]")
    except Exception:
        downloaded = []
    if not isinstance(downloaded, list):
        downloaded = []

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
    old_s3_key = None
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
            if query_key and _normalise_query(r.get("query")) == query_key:
                was_downloaded = r.get("status") == "downloaded"
                prior_key = r.get("s3_key")
                if was_downloaded and prior_key and prior_key != s3_key:
                    old_s3_key = prior_key
                r.update({
                    "status":     "downloaded",
                    "s3_key":     s3_key,
                    "file_name":  file_name,
                    "source_url": source_url or r.get("source_url", ""),
                    "manual_upload": True,
                })
                result_patched = True
                # Keep legacy/fallback chunk counts consistent with the
                # per-query result that was just resolved (only decrement
                # `failures` when this query genuinely was one — replacing an
                # already-downloaded row must not double-count).
                if not was_downloaded:
                    try:
                        pc["failures"] = max(0, int(pc.get("failures") or 0) - 1)
                        pc["downloaded"] = int(pc.get("downloaded") or 0) + 1
                    except (TypeError, ValueError):
                        pass
                break

    if old_s3_key:
        downloaded = [
            d for d in downloaded
            if not (isinstance(d, dict) and d.get("s3_key") == old_s3_key)
        ]
    if not any(isinstance(d, dict) and d.get("s3_key") == s3_key for d in downloaded):
        downloaded.append({
            "s3_key":      s3_key,
            "file_name":   file_name,
            "source_url":  source_url or (
                ("manual-upload: " + query) if query else "manual-upload"),
            "manual_upload": True,
        })

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
    if failure_index is None and result_patched and failures and not old_s3_key:
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
        return {"patched": True, "old_s3_key": old_s3_key}
    except Exception as ex:
        log.error("[upload] run patch write failed for %s: %s", run_id[:8], ex)
        return {"patched": False, "old_s3_key": None}


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


# Statuses that mean "a run for this company is already in flight" — used to
# stop a second run for the SAME company from being started concurrently.
# This matters because _clean_company_reports() (called at the start of every
# run) deletes ALL of a company's existing S3 objects/provenance before that
# run downloads anything; two concurrent runs for the same company would race
# on that cleanup and could delete files the other run just stored.
_ACTIVE_RUN_STATUSES = {"queued", "running", "browser_retry_pending"}


class ActiveRunConflict(Exception):
    """Raised when a company already has an in-flight run."""

    def __init__(self, run: dict):
        self.run = run
        company = run.get("company", "This company")
        status  = run.get("status", "active")
        super().__init__(f"{company} already has an active run ({status}).")


def _find_active_run_for_company(company: str, dynamo=None) -> dict | None:
    """Return the in-flight run for this company, if any.

    A plain scan-then-check — not perfectly atomic against two near-
    simultaneous submissions for the same company, but this guards a
    human clicking buttons in the UI, not a tight automated retry loop, so
    the tiny race window is an acceptable trade-off for staying simple.
    """
    if dynamo is None:
        dynamo = get_dynamo()
    if not company:
        return None
    try:
        resp = dynamo.Table(RUNS_TABLE).scan(
            FilterExpression=(
                "#c = :c AND (#st = :s1 OR #st = :s2 OR #st = :s3)"),
            ExpressionAttributeNames={"#c": "company", "#st": "status"},
            ExpressionAttributeValues={
                ":c":  company,
                ":s1": "queued",
                ":s2": "running",
                ":s3": "browser_retry_pending",
            },
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except Exception as e:
        log.error("[guard] active-run scan failed for company=%r: %s",
                  company, e)
        return None  # fail-open: a scan hiccup shouldn't block a real run


# Grace window before a "queued" run is treated as orphaned rather than
# genuinely waiting behind BULK_COMPANY_CONCURRENCY. Must comfortably exceed
# how long a submit() takes to be picked up by a free executor thread in the
# SAME live process (near-instant) so this only fires for rows whose original
# in-memory ThreadPoolExecutor submission was lost (process restart/crash).
QUEUED_RESUME_GRACE_SECONDS = int(
    os.environ.get("QUEUED_RESUME_GRACE_SECONDS", "90"))


def _get_running_count(dynamo=None) -> int:
    if dynamo is None:
        dynamo = get_dynamo()
    try:
        resp = dynamo.Table(RUNS_TABLE).scan(
            FilterExpression="#st = :r",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":r": "running"},
            Select="COUNT",
        )
        return resp.get("Count", 0)
    except Exception as e:
        log.error("[reconcile] running-count scan error: %s", e)
        return BULK_COMPANY_CONCURRENCY  # fail safe: assume full, don't resubmit


def _get_queued_runs(dynamo=None) -> list:
    """Scan runs table for any run with status=queued."""
    if dynamo is None:
        dynamo = get_dynamo()
    try:
        resp = dynamo.Table(RUNS_TABLE).scan(
            FilterExpression="#st = :q",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":q": "queued"},
        )
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = dynamo.Table(RUNS_TABLE).scan(
                ExclusiveStartKey=resp["LastEvaluatedKey"],
                FilterExpression="#st = :q",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":q": "queued"},
            )
            items.extend(resp.get("Items", []))
        return items
    except Exception as e:
        log.error("[reconcile] queued scan error: %s", e)
        return []


def _resume_stale_queued_runs(dynamo=None) -> list:
    """Resubmit 'queued' runs whose original in-memory executor submission was
    lost (e.g. a backend restart/crash/deploy).

    _async_invoke / _queue_bulk_invocations persist a run row as "queued" and
    ONLY start it by handing a callable to the in-process _BULK_COMPANY_EXECUTOR
    (a ThreadPoolExecutor). That pending-task queue is purely in-memory and does
    not survive a process restart, so a row can be stranded at "queued" forever
    with nothing left to ever execute it — this is why runs pile up in "queued"
    with progress permanently stuck at 0/N chunks. A row genuinely waiting for a
    free BULK_COMPANY_CONCURRENCY slot in the SAME live process starts almost
    immediately once a slot frees, so any "queued" row older than
    QUEUED_RESUME_GRACE_SECONDS while a slot is actually free must be orphaned,
    not just waiting in line.
    """
    if dynamo is None:
        dynamo = get_dynamo()
    queued = _get_queued_runs(dynamo)
    if not queued:
        return []

    now = datetime.now(timezone.utc)

    def _age_seconds(run: dict) -> float:
        queued_at = run.get("queued_at", "")
        try:
            dt = datetime.fromisoformat(queued_at.replace("Z", "+00:00"))
            return (now - dt).total_seconds()
        except Exception:
            return float("inf")  # missing/garbled timestamp — treat as stale

    eligible = sorted(
        (r for r in queued if _age_seconds(r) > QUEUED_RESUME_GRACE_SECONDS),
        key=_age_seconds, reverse=True,
    )
    if not eligible:
        return []

    available = max(0, BULK_COMPANY_CONCURRENCY - _get_running_count(dynamo))
    resumed = []
    queries_tbl = dynamo.Table(QUERIES_TABLE)
    for run in eligible[:available]:
        run_id   = run.get("run_id", "")
        query_id = run.get("query_id", "")
        if not run_id or not query_id:
            continue
        record = queries_tbl.get_item(Key={"query_id": query_id}).get("Item")
        if not record:
            log.error(
                "[reconcile] queued run %s has no matching query %s — "
                "cannot resubmit", run_id[:8], query_id)
            continue
        record = dict(record)
        record["run_id"] = run_id
        log.info(
            "[reconcile] resubmitting orphaned queued run %s (company=%s, "
            "queued %.0fs ago)", run_id[:8], run.get("company", ""),
            _age_seconds(run))
        _BULK_COMPANY_EXECUTOR.submit(_do_invoke, run_id, record)
        resumed.append(run_id)
    return resumed


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
            resumed = _resume_stale_queued_runs(dynamo)
            if resumed:
                log.info("[bg-reconciler] resumed %d orphaned queued run(s): %s",
                         len(resumed), [r[:8] for r in resumed])
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
    try:
        return send_from_directory(STATIC_DIR, path)
    except WerkzeugNotFound:
        # send_from_directory raises NotFound for any missing static asset
        # (favicon.ico is the common case — no favicon is shipped). Left
        # uncaught, Flask's generic error handler logged this as an
        # unhandled exception and returned 500 instead of a plain 404.
        return jsonify({"error": "not found"}), 404


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
    bulk_batch_id = None
    skipped = []
    if trigger:
        # Guard against two runs for the SAME company executing concurrently
        # (see _find_active_run_for_company) — both against runs already in
        # flight from an earlier submission, and against duplicate company
        # names within this very batch. Skipped entries are still saved as
        # queries above; they're just not started as a second run.
        seen_companies = set()
        eligible = []
        for record in saved:
            company = record.get("company", "Unknown")
            company_key = (company or "").strip().casefold()
            if company_key in seen_companies:
                skipped.append({
                    "company": company,
                    "reason": "duplicate company in this submission",
                })
                continue
            active = _find_active_run_for_company(company, dynamo)
            if active:
                skipped.append({
                    "company":  company,
                    "reason":   f"already has an active run "
                                f"(status={active.get('status', '')})",
                    "run_id":   active.get("run_id", ""),
                })
                continue
            seen_companies.add(company_key)
            eligible.append(record)

        if len(eligible) > 1:
            run_ids, bulk_batch_id = _queue_bulk_invocations(eligible)
        elif len(eligible) == 1:
            try:
                run_ids.append(_async_invoke(eligible[0]))
            except ActiveRunConflict as exc:
                skipped.append({
                    "company": eligible[0].get("company", "Unknown"),
                    "reason":  str(exc),
                    "run_id":  exc.run.get("run_id", ""),
                })

    return jsonify({"saved": len(saved), "queries": saved,
                    "run_ids": run_ids, "triggered": trigger,
                    "bulk_batch_id": bulk_batch_id,
                    "bulk_company_concurrency": (
                        BULK_COMPANY_CONCURRENCY
                        if bulk_batch_id else None),
                    "queued": (
                        max(0, len(run_ids) - BULK_COMPANY_CONCURRENCY)
                        if bulk_batch_id else 0),
                    "skipped": skipped}), 201


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
    try:
        run_id = _async_invoke(item)
    except ActiveRunConflict as exc:
        return jsonify({
            "error":           str(exc),
            "active_run_id":   exc.run.get("run_id", ""),
            "active_status":   exc.run.get("status", ""),
        }), 409
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
    for index, item in enumerate(items):
        if item.get("status") == "running":
            started = item.get("started_at", "")
            try:
                started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                age_mins   = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60
                if age_mins >= STUCK_THRESHOLD_MINUTES:
                    _spawn_reconcile(item)
            except Exception:
                pass
        elif item.get("status") == "browser_retry_pending":
            try:
                items[index] = _refresh_browser_retry_run(item, dynamo)
            except Exception as exc:
                log.warning("[browser-fallback] run reconciliation failed "
                            "for %s: %s", item.get("run_id", "")[:8], exc)
        # Independent of the status branches above — a run can be "complete"
        # at the top level while still carrying per-query rows left pending
        # by a client read-timeout (see _refresh_timed_out_queries). Cheap
        # no-op when there are none.
        try:
            items[index] = _refresh_timed_out_queries(items[index], dynamo)
        except Exception as exc:
            log.warning("[timeout-recheck] run reconciliation failed for "
                        "%s: %s", item.get("run_id", "")[:8], exc)

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
    elif item.get("status") == "browser_retry_pending":
        item = _refresh_browser_retry_run(item, dynamo)
    try:
        item = _refresh_timed_out_queries(item, dynamo)
    except Exception as exc:
        log.warning("[timeout-recheck] run reconciliation failed for %s: %s",
                    run_id[:8], exc)
    return jsonify(item)


@app.route("/api/runs/<run_id>", methods=["DELETE"])
def kill_run(run_id):
    """Kill a run (best-effort) and remove its row from the Runs table.

    In-flight AgentCore chunk calls already executing cannot be forcibly
    interrupted, but no further chunks are dispatched once _do_invoke_inner
    observes the kill flag (see _is_run_killed/_consume_run_kill), and it
    never writes to this run_id again — so deleting the row here is final,
    not something a lagging background write can resurrect.

    For a run in browser_retry_pending, the ECS browser-worker task(s) for it
    are also stopped directly, since that separate process's own patch-back
    (browser_worker.py:_patch_run) would otherwise recreate the row after we
    delete it.
    """
    dynamo   = get_dynamo()
    runs_tbl = dynamo.Table(RUNS_TABLE)
    item     = runs_tbl.get_item(Key={"run_id": run_id}).get("Item")
    if not item:
        return jsonify({"error": "Run not found"}), 404

    _mark_run_killed(run_id)

    if item.get("status") == "browser_retry_pending" and BROWSER_ECS_CLUSTER:
        try:
            jobs_tbl = dynamo.Table(BROWSER_JOBS_TABLE)
            resp = jobs_tbl.scan(
                FilterExpression="#run = :run",
                ExpressionAttributeNames={"#run": "run_id"},
                ExpressionAttributeValues={":run": run_id},
            )
            for job in resp.get("Items", []):
                if job.get("status") not in {"queued", "launched", "running"}:
                    continue
                task_arn = job.get("task_arn")
                if task_arn:
                    try:
                        get_ecs().stop_task(
                            cluster=BROWSER_ECS_CLUSTER, task=task_arn,
                            reason="Killed from Report IQ portal")
                    except Exception as exc:
                        log.warning("[kill] stop_task failed for %s: %s",
                                    task_arn, exc)
                try:
                    jobs_tbl.update_item(
                        Key={"job_id": job["job_id"]},
                        UpdateExpression="SET #st = :s, updated_at = :u",
                        ExpressionAttributeNames={"#st": "status"},
                        ExpressionAttributeValues={
                            ":s": "failed",
                            ":u": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                except Exception as exc:
                    log.warning("[kill] browser job status update failed for "
                                "%s: %s", job.get("job_id", ""), exc)
        except Exception as exc:
            log.error("[kill] browser job cleanup failed for run %s: %s",
                       run_id[:8], exc)

    query_id = item.get("query_id")
    if query_id and query_id != "unknown":
        try:
            dynamo.Table(QUERIES_TABLE).update_item(
                Key={"query_id": query_id},
                UpdateExpression="SET #st = :s, #u = :u",
                ExpressionAttributeNames={"#st": "status", "#u": "updated_at"},
                ExpressionAttributeValues={
                    ":s": "killed",
                    ":u": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            log.error("[kill] query status update failed for %s: %s",
                       query_id, exc)

    try:
        runs_tbl.delete_item(Key={"run_id": run_id})
    except Exception as exc:
        log.error("[kill] delete_item failed for run %s: %s", run_id[:8], exc)
        return jsonify({"error": f"Failed to delete run: {exc}"}), 500

    log.info("[kill] run %s (company=%s, was %s) killed and removed",
              run_id[:8], item.get("company", ""), item.get("status", ""))
    return jsonify({"ok": True, "run_id": run_id, "removed": True})


@app.route("/api/browser-jobs/<job_id>", methods=["GET"])
def get_browser_job(job_id):
    """Return durable status for a WAF browser retry."""
    item = get_dynamo().Table(BROWSER_JOBS_TABLE).get_item(
        Key={"job_id": job_id}).get("Item")
    if not item:
        return jsonify({"error": "Browser job not found"}), 404
    raw_candidates = item.get("candidate_urls")
    if isinstance(raw_candidates, str):
        try:
            item["candidate_urls"] = json.loads(raw_candidates)
        except Exception:
            item["candidate_urls"] = []
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
    resumed = _resume_stale_queued_runs(dynamo)

    return jsonify({
        "stuck_found":   len(stuck),
        "updated":       sum(1 for f in fixed if f["updated"]),
        "failed":        [f for f in fixed if f.get("error")],
        "details":       fixed,
        "queued_resumed": [r[:8] for r in resumed],
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
    """Return a download link for an S3 key.

    NOTE: despite the route name (kept for frontend compatibility), this no
    longer returns an S3-signed URL. AWS caps SigV4 presigned URLs at a hard
    maximum of 7 days no matter what ExpiresIn is requested — there is no way
    to make one that never expires. A persistent link instead has to proxy
    through this backend: /api/sources/download-file streams the object
    directly using our own IAM credentials on every request, so it keeps
    working for as long as the backend does, with no expiry.
    """
    s3_key = request.args.get("key", "").strip()
    if not s3_key:
        return jsonify({"error": "key param required"}), 400
    s3 = get_s3()
    try:
        integrity_error = _s3_pdf_integrity_error(s3, s3_key)
        if integrity_error:
            status = 404 if integrity_error == "S3 object does not exist" else 422
            return jsonify({
                "error": integrity_error,
                "key": s3_key,
                "valid_pdf": False,
            }), status
        url = "/api/sources/download-file?key=" + quote(s3_key, safe="")
        return jsonify({
            "url": url,
            "key": s3_key,
            "expires_in": None,
            "valid_pdf": True if s3_key.lower().endswith(".pdf") else None,
        })
    except ClientError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/download-file", methods=["GET"])
def download_file():
    """Persistent (non-expiring) download link — streams the S3 object
    through this backend on every request instead of a time-limited
    presigned URL. See the note on presigned_url() above for why a truly
    non-expiring S3-signed URL isn't possible.
    """
    s3_key = request.args.get("key", "").strip()
    if not s3_key:
        return jsonify({"error": "key param required"}), 400
    s3 = get_s3()
    integrity_error = _s3_pdf_integrity_error(s3, s3_key)
    if integrity_error:
        status = 404 if integrity_error == "S3 object does not exist" else 422
        return jsonify({"error": integrity_error, "key": s3_key}), status

    try:
        obj = s3.get_object(Bucket=REPORTS_BUCKET, Key=s3_key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        status = 404 if code in {"404", "NoSuchKey", "NotFound"} else 500
        return jsonify({"error": str(e)}), status

    download_name = re.sub(
        r"[^A-Za-z0-9._-]+", "_", PurePosixPath(s3_key).name
    ).strip("._") or "download"
    body = obj["Body"]

    def _stream():
        try:
            for chunk in body.iter_chunks(chunk_size=64 * 1024):
                yield chunk
        finally:
            body.close()

    resp = Response(
        stream_with_context(_stream()),
        mimetype=obj.get("ContentType") or "application/octet-stream",
    )
    resp.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    if obj.get("ContentLength") is not None:
        resp.headers["Content-Length"] = str(obj["ContentLength"])
    return resp


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
      source_url - optional HTTPS candidate opened for the manual download;
                   retained as provenance for the uploaded file
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
    source_url = _safe_manual_source_url(request.form.get("source_url") or "")

    slug      = _agent_slug(company)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(f.filename)).strip("_") or "upload"
    s3_key    = f"{slug}/manual/{safe_name}"
    body      = f.read()
    integrity_error = _pdf_integrity_error(
        safe_name, f.mimetype or "", body)
    if integrity_error:
        return jsonify({
            "error": f"Upload rejected: {integrity_error}",
            "file_name": safe_name,
        }), 422

    try:
        get_s3().put_object(
            Bucket=REPORTS_BUCKET,
            Key=s3_key,
            Body=body,
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
                "source_url": source_url or (
                    ("manual-upload: " + query) if query else "manual-upload"),
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
    old_s3_key = None
    if run_id:
        try:
            patch_result = _patch_run_with_upload(
                run_id, s3_key, safe_name, query, chunk, dynamo,
                source_url=source_url)
            patched = patch_result.get("patched", False)
            old_s3_key = patch_result.get("old_s3_key")
        except Exception as ex:
            log.error("[upload] run patch failed for %s: %s", run_id[:8], ex)

    # A manual upload against a query the agent already answered means the
    # agent's file was wrong — remove it from S3 (and its provenance row) so
    # it doesn't linger as a stale/duplicate document for this company.
    replaced_agent_download = False
    if old_s3_key and old_s3_key != s3_key:
        s3 = get_s3()
        try:
            s3.delete_object(Bucket=REPORTS_BUCKET, Key=old_s3_key)
            s3.delete_object(
                Bucket=REPORTS_BUCKET, Key=old_s3_key + ".metadata.json")
            replaced_agent_download = True
        except ClientError as ex:
            log.error("[upload] failed to delete superseded download %s: %s",
                       old_s3_key, ex)
        try:
            dynamo.Table(PROVENANCE_TABLE).delete_item(
                Key={"company": slug, "s3_key": old_s3_key})
        except Exception as ex:
            log.error("[upload] failed to delete superseded provenance "
                      "%s/%s: %s", slug, old_s3_key, ex)

    log.info("[upload] company=%s query=%r -> s3_key=%s run_patched=%s "
              "replaced=%s (old_key=%s)", company, query, s3_key, patched,
              replaced_agent_download, old_s3_key)

    return jsonify({
        "ok":                      True,
        "s3_key":                  s3_key,
        "file_name":               safe_name,
        "company":                 company,
        "run_patched":             patched,
        "replaced_agent_download": replaced_agent_download,
        "old_s3_key":              old_s3_key,
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
    """Route a single triggered run through the SAME bounded executor bulk
    batches use, instead of firing an unbounded thread outside that cap.

    Previously this started a raw threading.Thread with no concurrency
    accounting at all — invisible to BULK_COMPANY_CONCURRENCY. A single run
    triggered this way stayed running indefinitely while a LATER bulk batch's
    own 3 slots started on top of it, so the real concurrency ceiling wasn't
    3 companies, it was 3 + however many single runs were already in flight
    (observed live: an already-running single company + a fresh 3-company
    bulk batch = 4-6 concurrent AgentCore invocations at once). Submitting to
    _BULK_COMPANY_EXECUTOR instead gives every run-starting path ONE shared,
    race-free budget — a DynamoDB "how many are running" scan-then-launch
    would have the same check-then-act race two concurrent requests could
    both pass; the executor's own queue is atomic within this process, which
    is sufficient since desired_count=1 (single backend task, no
    autoscaling) means there's no cross-process concurrency to coordinate.
    """
    query_id = query_record.get("query_id", "unknown")
    company = query_record.get("company", "Unknown")
    dynamo = get_dynamo()

    existing = _find_active_run_for_company(company, dynamo)
    if existing:
        raise ActiveRunConflict(existing)

    run_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    dynamo.Table(RUNS_TABLE).put_item(Item={
        "run_id":   run_id,
        "query_id": query_id,
        "company":  company,
        "status":   "queued",
        "queued_at": now_iso,
    })
    dynamo.Table(QUERIES_TABLE).update_item(
        Key={"query_id": query_id},
        UpdateExpression="SET #st = :s, #rid = :r, #upd = :u",
        ExpressionAttributeNames={"#st": "status", "#rid": "run_id", "#upd": "updated_at"},
        ExpressionAttributeValues={":s": "queued", ":r": run_id, ":u": now_iso},
    )
    record = dict(query_record)
    record["run_id"] = run_id
    record["status"] = "queued"
    record["updated_at"] = now_iso
    _BULK_COMPANY_EXECUTOR.submit(_do_invoke, run_id, record)
    return run_id


def _queue_bulk_invocations(query_records: list[dict]) -> tuple[list[str], str]:
    """Persist all bulk runs as queued, then execute three companies at a time.

    Run IDs are returned immediately for every company. ThreadPoolExecutor
    retains the remaining callables in FIFO submission order; as soon as one
    company finishes, the next queued company starts without requiring the
    browser session that submitted the batch to remain open.
    """
    batch_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    dynamo = get_dynamo()
    runs_table = dynamo.Table(RUNS_TABLE)
    queries_table = dynamo.Table(QUERIES_TABLE)
    scheduled: list[tuple[str, dict]] = []

    for position, record in enumerate(query_records, start=1):
        run_id = str(uuid.uuid4())
        query_id = record.get("query_id", "unknown")
        company = record.get("company", "Unknown")
        chunks_total = len(_chunk_web_queries(record, AGENT_CHUNK_SIZE))
        runs_table.put_item(Item={
            "run_id": run_id,
            "query_id": query_id,
            "company": company,
            "status": "queued",
            "queued_at": now_iso,
            "bulk_batch_id": batch_id,
            "bulk_position": position,
            "bulk_size": len(query_records),
            "payload": json.dumps({
                "company": company,
                "run_id": run_id,
                "search_query": record.get("search_query", ""),
                "chunk_size": AGENT_CHUNK_SIZE,
                "chunks_total": chunks_total,
                "bulk_batch_id": batch_id,
                "bulk_position": position,
            }),
            "downloaded": json.dumps([]),
            "failures": json.dumps([]),
            "diagnostics": json.dumps({
                "chunks_total": chunks_total,
                "chunks_done": 0,
                "chunk_size": AGENT_CHUNK_SIZE,
                "concurrency": AGENT_CHUNK_CONCURRENCY,
                "bulk_company_concurrency": BULK_COMPANY_CONCURRENCY,
                "bulk_batch_id": batch_id,
                "bulk_position": position,
                "bulk_size": len(query_records),
                "per_chunk": [],
            }),
        })
        queries_table.update_item(
            Key={"query_id": query_id},
            UpdateExpression="SET #st = :s, #rid = :r, #upd = :u",
            ExpressionAttributeNames={
                "#st": "status", "#rid": "run_id", "#upd": "updated_at"},
            ExpressionAttributeValues={
                ":s": "queued", ":r": run_id, ":u": now_iso},
        )
        record["run_id"] = run_id
        record["status"] = "queued"
        record["updated_at"] = now_iso
        scheduled.append((run_id, record))

    # Submit only after every queue row exists, so the UI always receives a
    # complete batch and can display the seven waiting companies immediately.
    for run_id, record in scheduled:
        _BULK_COMPANY_EXECUTOR.submit(_do_invoke, run_id, record)

    log.info("[bulk %s] queued %d companies; concurrency=%d",
             batch_id[:8], len(scheduled), BULK_COMPANY_CONCURRENCY)
    return [run_id for run_id, _ in scheduled], batch_id


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


# ─── Chunking helpers (PATCH #6 + class-safe structured payloads) ─────────────
_REPORT_CLASS_ALIASES = (
    ("supplier code of conduct", (
        "supplier code of conduct", "vendor code of conduct",
        "third party code of conduct", "business partner code of conduct",
    )),
    ("anti-bribery and corruption policy", (
        "anti corruption and bribery policy",
        "anti bribery and corruption policy",
        "anti-corruption and bribery policy",
        "anti-bribery and corruption policy",
    )),
    ("conflicts of interest policy", (
        "conflicts of interest policy", "conflict of interest policy",
    )),
    ("discrimination and harassment policy", (
        "discrimination and harassment policy",
        "anti discrimination and harassment policy",
    )),
    ("whistleblowing mechanism", (
        "whistleblowing policy", "whistleblower policy",
        "speak up policy", "ethics hotline policy",
    )),
    ("environment, health & safety policy", (
        "environment health and safety policy",
        "environmental health and safety policy",
        "environment health safety policy",
        "ehs policy", "hse policy", "qhse policy", "hsse policy",
    )),
    ("occupational health & safety policy", (
        "occupational health and safety policy",
        "health and safety policy",
    )),
    ("tax strategy and governance", (
        "tax strategy and policy document", "tax strategy and governance",
        "tax strategy", "tax policy",
    )),
    ("political contributions and lobbying policy", (
        "political contributions and lobbying policy",
        "political contributions policy", "lobbying policy",
    )),
    ("corporate governance guidelines", (
        "corporate governance guidelines", "governance guidelines",
    )),
    ("insider trading policy", (
        "insider trading policy", "share trading policy",
    )),
    ("ghg emission report", (
        "ghg emission report", "greenhouse gas emissions report",
        "emissions report",
    )),
    ("environmental policy", (
        "environment policy", "environmental policy",
    )),
    ("sustainability report", (
        "sustainability report", "esg report",
    )),
    ("annual report", (
        "annual report", "report and accounts",
    )),
    ("proxy statement", (
        "proxy statement", "definitive proxy", "def 14a",
    )),
    ("code of conduct", (
        "code of conduct", "code of business conduct", "code of ethics",
    )),
    ("biodiversity policy", ("biodiversity policy",)),
    ("impact report", ("impact report",)),
    ("human rights policy", ("human rights policy",)),
    ("human rights due diligence", (
        "human due diligence", "human rights due diligence",
        "human rights impact assessment",
    )),
    ("modern slavery statement", (
        "modern slavery statement", "modern slavery act statement",
        "slavery and human trafficking statement",
        "transparency in supply chains statement",
    )),
    ("remuneration report", ("remuneration report", "compensation report")),
    ("risk management policy", ("risk management policy",)),
    ("wolfsberg questionnaire", ("wolfsberg questionnaire",)),
)


def _infer_report_class(query: str, company: str = "") -> str:
    """Map a legacy web query to a stable class; never return uncategorized."""
    without_scope = re.sub(
        r"\b(?:site|filetype):\s*\S+", " ", str(query or ""), flags=re.I)
    normalized = re.sub(
        r"[^a-z0-9]+", " ", without_scope.lower()).strip()
    for canonical, aliases in _REPORT_CLASS_ALIASES:
        if any(re.sub(r"[^a-z0-9]+", " ", alias.lower()).strip() in normalized
               for alias in aliases):
            return canonical

    fallback = without_scope
    if company:
        fallback = re.sub(
            rf"^\s*{re.escape(company)}\b", " ", fallback, flags=re.I)
    fallback = re.sub(r"\b(?:19|20)\d{2}\b", " ", fallback)
    fallback = re.sub(
        r"\b(?:latest|official|download|document|file|pdf)\b",
        " ", fallback, flags=re.I)
    fallback = re.sub(r"[^A-Za-z0-9]+", " ", fallback).strip().lower()
    return fallback[:120] or "other official document"


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


def _partition_annual_report_phase(query_record: dict, company: str,
                                   size: int) -> tuple[list, list]:
    """Return isolated Annual Report chunks followed by ordinary chunks.

    Annual Report queries are always single-query chunks so no unrelated
    discovery starts in the same AgentCore invocation before the report has
    been stored and analysed.
    """
    ordered_queries = [
        query
        for chunk in _chunk_web_queries(query_record, size)
        for query in chunk
    ]
    annual_chunks = [[query] for query in ordered_queries
                     if _infer_report_class(query, company) == "annual report"]
    remaining_queries = [
        query for query in ordered_queries
        if _infer_report_class(query, company) != "annual report"
    ]
    chunk_size = max(1, size)
    remaining_chunks = [
        remaining_queries[index:index + chunk_size]
        for index in range(0, len(remaining_queries), chunk_size)
    ]
    return annual_chunks, remaining_chunks


def _build_chunk_payload(company: str, run_id: str, search_query: str,
                         chunk_queries: list, chunk_index: int) -> dict:
    """
    One chunk = one normal small AgentCore payload.

    The legacy web_query fields remain for compatibility and domain inference,
    while ``reports`` is authoritative. Supplying report_class explicitly keeps
    AgentCore on its structured input path and prevents valid downloads from
    being written beneath ``uncategorized/``.
    """
    payload = {
        "company":      company,
        "run_id":       run_id,          # SAME run_id for every chunk (fix #1/#4)
        "search_query": search_query,
        "chunk_index":  chunk_index,     # informational; agent may ignore it
        "web_query_ids": {},
        "document_preferences": {
            "preferred_language": "en",
            "prefer_latest": True,
            "allow_source_attested_external_documents": True,
        },
        "reports": [],
    }
    for i, q in enumerate(chunk_queries, start=1):
        key = "web_query" + str(i)
        request_id = f"{chunk_index}:{i}"
        payload[key] = q
        payload["web_query_ids"][key] = request_id
        report = {
            "query": str(q),
            "request_id": request_id,
            "report_class": _infer_report_class(str(q), company),
            # The application performs embedded-section fallback only after
            # every standalone discovery tier fails, using the independently
            # generated Annual Report coverage manifest.
            "standalone_only": True,
        }
        years = [
            int(value) for value in re.findall(
                r"\b(?:19|20)\d{2}\b", str(q))
        ]
        if years:
            report["year"] = max(years)
        report["preferred_language"] = "en"
        report["prefer_latest"] = not bool(years)
        payload["reports"].append(report)
    return payload


def _annual_report_manifest_key(company: str) -> str:
    return f"{_agent_slug(company)}/_manifests/annual-report-coverage.json"


def _annual_report_key_from_chunk(result: dict, company: str) -> str:
    """Return the successfully downloaded Annual Report key from one chunk."""
    for item in result.get("results") or []:
        if not isinstance(item, dict) or item.get("status") != "downloaded":
            continue
        if _infer_report_class(item.get("query", ""), company) == "annual report":
            return str(item.get("s3_key") or "")
    return ""


def _annual_report_failed_classes(per_chunk_results: list,
                                  company: str) -> list[str]:
    """Return unique clean-miss classes that need Annual Report analysis.

    This runs only after standalone searches finish. A chunk-level error and
    every typed pending/blocked/error result are excluded; PageIndex therefore
    receives no work for successful downloads or failures that still require
    retry/manual recovery.
    """
    classes = []
    for chunk in per_chunk_results or []:
        if not isinstance(chunk, dict) or chunk.get("error"):
            continue
        for item in chunk.get("results") or []:
            if (not isinstance(item, dict)
                    or item.get("status") != "failed"
                    or item.get(
                        "annual_report_reference_eligible") is not True):
                continue
            report_class = _infer_report_class(
                str(item.get("query") or ""), company)
            if (report_class in ANNUAL_REPORT_REFERENCE_CLASSES
                    and report_class not in classes):
                classes.append(report_class)
    return classes


def _create_annual_report_coverage_manifest(
    company: str,
    annual_report_s3_key: str,
    requested_classes: list[str],
    s3=None,
) -> dict | None:
    """Index the Annual Report in the separate PageIndex runtime and persist
    a durable coverage manifest. Fail closed: an analysis or validation error
    returns None and can never convert a failed standalone search to a
    reference result.
    """
    if not annual_report_s3_key:
        return None
    eligible = [
        value for value in dict.fromkeys(
            str(item or "").strip().lower() for item in requested_classes
        )
        if value in ANNUAL_REPORT_REFERENCE_CLASSES
    ]
    if not eligible:
        return None

    payload = json.dumps({
        "bucket": REPORTS_BUCKET,
        "s3_key": annual_report_s3_key,
        "label": PurePosixPath(annual_report_s3_key).name,
        "mode": "annual_report_coverage",
        "report_classes": eligible,
    }).encode("utf-8")
    try:
        raw = _invoke_agentcore(
            PAGEINDEX_RUNTIME_ARN, PAGEINDEX_QUALIFIER, payload)
        result = json.loads(raw.decode("utf-8")) if raw else {}
        if result.get("status") != "ok":
            raise RuntimeError(result.get("error") or "coverage runtime failed")

        headings = result.get("headings") or []
        raw_coverage = result.get("coverage") or {}
        coverage = {}
        for report_class, match in raw_coverage.items():
            canonical = str(report_class or "").strip().lower()
            if canonical not in eligible or not isinstance(match, dict):
                continue
            if (match.get("match") != "substantive_section"
                    or match.get("confidence") != "high"):
                continue
            try:
                page_start = int(match.get("page_start"))
                page_end = int(match.get("page_end"))
            except (TypeError, ValueError):
                continue
            if page_start < 1 or page_end < page_start:
                continue
            coverage[canonical] = {
                "match": "substantive_section",
                "heading": str(match.get("heading") or "")[:500],
                "page_start": page_start,
                "page_end": page_end,
                "confidence": "high",
                "evidence": str(match.get("evidence") or "")[:1000],
            }

        s3 = s3 or get_s3()
        head = s3.head_object(
            Bucket=REPORTS_BUCKET, Key=annual_report_s3_key)
        provenance = {}
        try:
            provenance = get_dynamo().Table(PROVENANCE_TABLE).get_item(
                Key={
                    "company": _agent_slug(company),
                    "s3_key": annual_report_s3_key,
                }
            ).get("Item") or {}
        except Exception as exc:
            log.warning(
                "[annual-coverage] provenance enrichment failed for %s: %s",
                annual_report_s3_key, exc)
        annual_year = provenance.get("year")
        try:
            annual_year = int(annual_year) if annual_year is not None else None
        except (TypeError, ValueError):
            annual_year = None
        manifest_key = _annual_report_manifest_key(company)
        manifest = {
            "schema_version": 1,
            "company": company,
            "company_slug": _agent_slug(company),
            "annual_report_s3_key": annual_report_s3_key,
            "annual_report_s3_uri": (
                f"s3://{REPORTS_BUCKET}/{annual_report_s3_key}"),
            "annual_report_etag": str(head.get("ETag") or "").strip('"'),
            "annual_report_sha256": (
                provenance.get("hash")
                or (head.get("Metadata") or {}).get("sha256")
                or ""),
            "annual_report_year": annual_year,
            "annual_report_source_url": (
                provenance.get("source_url")
                or (head.get("Metadata") or {}).get("source_url")
                or ""),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "extractor": "pageindex-annual-report-coverage",
            "manifest_s3_key": manifest_key,
            "headings": headings,
            "coverage": coverage,
        }
        s3.put_object(
            Bucket=REPORTS_BUCKET,
            Key=manifest_key,
            Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        log.info(
            "[annual-coverage] stored %d headings / %d references -> s3://%s/%s",
            len(headings), len(coverage), REPORTS_BUCKET, manifest_key)
        return manifest
    except Exception as exc:
        log.error(
            "[annual-coverage] failed closed for %s (%s): %s",
            annual_report_s3_key, type(exc).__name__, exc)
        return None


def _apply_annual_report_references(result: dict, company: str,
                                    manifest: dict | None) -> dict:
    """Replace honest standalone-search misses with typed section references.

    Transport errors, timeouts, WAF blocks and browser retries remain untouched:
    those paths did not exhaust discovery cleanly and therefore are not eligible
    for the final-tier Annual Report fallback.
    """
    if not manifest or result.get("error"):
        return result
    coverage = manifest.get("coverage") or {}
    referenced_classes = set()
    updated_results = []
    for item in result.get("results") or []:
        if (not isinstance(item, dict)
                or item.get("status") != "failed"
                or item.get("annual_report_reference_eligible") is not True):
            updated_results.append(item)
            continue
        report_class = _infer_report_class(item.get("query", ""), company)
        match = coverage.get(report_class)
        if not isinstance(match, dict):
            updated_results.append(item)
            continue
        referenced_classes.add(report_class)
        referenced = {
            **item,
            "status": "referenced_in_existing_document",
            "report_class": report_class,
            "referenced_s3_key": manifest["annual_report_s3_key"],
            "referenced_s3_uri": manifest["annual_report_s3_uri"],
            "manifest_s3_key": manifest.get("manifest_s3_key"),
            "heading": match.get("heading"),
            "page_start": match.get("page_start"),
            "page_end": match.get("page_end"),
            "confidence": "high",
            "evidence": match.get("evidence"),
            "reason": (
                "No standalone document was found after all discovery tiers; "
                "a verified substantive section exists in the stored Annual "
                "Report."),
        }
        referenced.pop("annual_report_reference_eligible", None)
        updated_results.append(referenced)
    result["results"] = updated_results
    if referenced_classes:
        result["failures"] = [
            item for item in (result.get("failures") or [])
            if _infer_report_class(
                (item or {}).get("query", "") if isinstance(item, dict) else "",
                company,
            ) not in referenced_classes
        ]
        result["annual_report_references"] = len(referenced_classes)
    return result


def _invoke_one_chunk(chunk_index: int, chunk_queries: list, company: str,
                      run_id: str, query_id: str, search_query: str) -> dict:
    """Invoke a single chunk and normalise its response into a result dict."""
    payload = _build_chunk_payload(company, run_id, search_query, chunk_queries, chunk_index)
    log.info("[run %s] chunk %d — invoking %d queries", run_id[:8], chunk_index, len(chunk_queries))
    invoked_at = datetime.now(timezone.utc).isoformat()
    try:
        raw  = _invoke_agentcore_http_with_retry(json.dumps(payload).encode("utf-8"))
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
        agent_results = body.get("results", [])        if isinstance(body, dict) else []
        agent_results = _enqueue_browser_retries(
            agent_results, company, run_id, query_id)
        downloaded = list(stored) + list(duplicates)
        for item in agent_results:
            if (isinstance(item, dict)
                    and item.get("status") == "downloaded"
                    and item.get("s3_key")
                    and not any(
                        isinstance(existing, dict)
                        and existing.get("s3_key") == item["s3_key"]
                        for existing in downloaded)):
                downloaded.append(item)
        failures   = list(not_found)
        recovered_request_ids = {
            item.get("request_id") for item in agent_results
            if isinstance(item, dict)
            and item.get("status") == "downloaded"
            and item.get("request_id")
        }
        if recovered_request_ids:
            failures = [
                item for item in failures
                if not (isinstance(item, dict)
                        and item.get("request_id") in recovered_request_ids)
            ]
        diag       = body.get("diagnostics", {}) if isinstance(body, dict) else {}
        log.info("[run %s] chunk %d done — downloaded=%d failures=%d",
                 run_id[:8], chunk_index, len(downloaded), len(failures))
        return {"chunk": chunk_index, "queries": chunk_queries,
                "downloaded": downloaded, "failures": failures,
                # PATCH #7: pre-computed per-query rows so the UI doesn't have
                # to re-derive them and so the pairing logic lives in one place.
                "results": _pair_queries_with_results(
                    chunk_queries, downloaded, failures,
                    agent_results=agent_results, chunk_index=chunk_index),
                "diagnostics": diag, "error": None}
    except Exception as e:
        log.error("[run %s] chunk %d ERROR: %s", run_id[:8], chunk_index, e)
        # A read-timeout on THIS client is not evidence the agent failed — it's
        # evidence the client gave up listening. AgentCore's own execution wall
        # is ~900s and AGENT_READ_TIMEOUT is now within 10s of it (see the
        # constant's comment), so most genuine "the agent is broken" failures
        # surface as something else (HTTPError, ValidationException, etc).
        # A read-timeout specifically gets a PENDING status instead of a
        # permanent "failed" — _refresh_timed_out_queries() (read-path, same
        # pattern as _refresh_browser_retry_run for WAF jobs) later checks
        # provenance for a document the agent may have stored AFTER this
        # client gave up, and only finalizes "failed" once TIMEOUT_RECHECK_
        # WINDOW_MINUTES has passed with nothing found. Every other exception
        # keeps the original immediate "failed" behavior — this is deliberately
        # narrow, not a general "assume success" softening of error handling.
        is_read_timeout = (
            isinstance(e, ReadTimeoutError) or "read timeout" in str(e).lower())
        if is_read_timeout:
            log.info("[run %s] chunk %d — read-timeout, deferring to "
                     "post-hoc provenance check instead of failing now",
                     run_id[:8], chunk_index)
            results = [
                {
                    "request_id": f"{chunk_index}:{position}",
                    "query": q,
                    "status": "timed_out_pending_check",
                    "report_class": _infer_report_class(str(q), company),
                    "invoked_at": invoked_at,
                    "reason": (
                        "AgentCore did not respond within the client read "
                        "timeout; the agent may still complete this "
                        "server-side — checking for a stored document."),
                }
                for position, q in enumerate(chunk_queries, start=1)
            ]
        else:
            # PATCH #7: even a hard chunk failure gets per-query rows — every
            # query in the chunk is 'failed' so the UI can still offer manual
            # upload instead of only showing an opaque chunk-level error string.
            if _is_throttling_error(e):
                reason = (
                    f"AgentCore Gateway throttled this request after "
                    f"{AGENT_THROTTLE_MAX_RETRIES} retries — try again later "
                    f"or re-run this query.")
            else:
                reason = str(e)[:500]
            results = [
                {
                    "request_id": f"{chunk_index}:{position}",
                    "query": q,
                    "status": "failed",
                    "reason": reason,
                }
                for position, q in enumerate(chunk_queries, start=1)
            ]
        return {"chunk": chunk_index, "queries": chunk_queries,
                "downloaded": [], "failures": [], "diagnostics": {},
                "results": results,
                "error": str(e)[:500]}


def _do_invoke_inner(run_id: str, query_record: dict):
    # A queued run can be killed before its callable ever leaves the executor's
    # internal queue. Bail out before touching AWS or the (now-deleted) row.
    if _consume_run_kill(run_id):
        log.info("[run %s] killed before it started — skipping", run_id[:8])
        return

    dynamo   = get_dynamo()
    runs_tbl = dynamo.Table(RUNS_TABLE)
    qry_tbl  = dynamo.Table(QUERIES_TABLE)
    query_id = query_record.get("query_id", "unknown")
    company  = query_record.get("company",  "Unknown")
    search_q = query_record.get("search_query", "")
    now_iso  = datetime.now(timezone.utc).isoformat()

    # Annual Report is a real dependency for a full-company run, not merely a
    # higher-priority item in the same executor queue. Isolate it into phase 1;
    # all other chunks remain phase 2 and retain bounded parallelism.
    annual_chunks, remaining_chunks = _partition_annual_report_phase(
        query_record, company, AGENT_CHUNK_SIZE)
    chunks = annual_chunks + remaining_chunks
    chunks_total = len(chunks)
    queued_run = runs_tbl.get_item(
        Key={"run_id": run_id}).get("Item", {})
    bulk_metadata = {
        key: queued_run[key]
        for key in (
            "queued_at", "bulk_batch_id", "bulk_position", "bulk_size")
        if key in queued_run
    }

    base_payload = {"company": company, "run_id": run_id,
                    "search_query": search_q,
                    "chunk_size": AGENT_CHUNK_SIZE,
                    "chunks_total": chunks_total,
                    "annual_report_phase": bool(annual_chunks)}

    # Claim the run atomically before doing any work. Two callables can race
    # to invoke the SAME run_id when a queued run is resubmitted by
    # _resume_stale_queued_runs() while the original executor task (thought
    # lost to a process restart) is in fact still alive — this condition
    # rejects the second caller instead of double-invoking AgentCore / running
    # cleanup twice.
    try:
        runs_tbl.update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #st = :running, #u = :now",
            ConditionExpression=(
                "attribute_not_exists(#st) OR #st = :queued"),
            ExpressionAttributeNames={"#st": "status", "#u": "updated_at"},
            ExpressionAttributeValues={
                ":running": "running", ":queued": "queued", ":now": now_iso},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get(
                "Code") == "ConditionalCheckFailedException":
            log.info(
                "[run %s] already claimed by another invocation — skipping "
                "duplicate start", run_id[:8])
            return
        raise

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
        **bulk_metadata,
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
            # Once killed, the row is (about to be) deleted — any further
            # update_item here would silently resurrect it via upsert.
            if not _is_run_killed(run_id):
                _flush_run_row(final=False)   # live update — UI shows the list grow

    def _record_future_error(exc: Exception):
        log.error("[run %s] chunk future crashed: %s", run_id[:8], exc)
        with lock:
            chunks_done[0] += 1
            per_chunk_diag.append({"chunk": "?", "queries": [], "results": [],
                                   "downloaded": 0, "failures": 0,
                                   "error": str(exc)[:500],
                                   "agent_diagnostics": {}})
            if not _is_run_killed(run_id):
                _flush_run_row(final=False)

    # ── Phase 1: acquire the Annual Report before all other searches ──
    annual_report_s3_key = ""
    for index, chunk in enumerate(annual_chunks, start=1):
        if _is_run_killed(run_id):
            break
        try:
            result = _invoke_one_chunk(
                index, chunk, company, run_id, query_id, search_q)
            _handle_result(result)
            annual_report_s3_key = (
                _annual_report_key_from_chunk(result, company)
                or annual_report_s3_key)
        except Exception as exc:
            _record_future_error(exc)

    # ── Phase 2: standalone searches in bounded parallelism ─────────────────
    phase2_start = len(annual_chunks) + 1
    workers = max(1, min(AGENT_CHUNK_CONCURRENCY, len(remaining_chunks) or 1))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(
                _invoke_one_chunk, phase2_start + offset, chunk,
                company, run_id, query_id, search_q)
            for offset, chunk in enumerate(remaining_chunks)
        ]
        for fut in as_completed(futures):
            if _is_run_killed(run_id):
                # Chunks already in flight can't be interrupted, but nothing
                # still queued gets dispatched, and we stop touching the row.
                for f in futures:
                    f.cancel()
                break
            try:
                _handle_result(fut.result())
            except Exception as exc:
                _record_future_error(exc)

    # ── Phase 3: PageIndex once, for clean standalone misses only ────────
    # Successful, WAF-blocked, pending, timed-out and errored searches never
    # become PageIndex topics. This keeps independent report discovery off the
    # PageIndex critical path and avoids classifying sections nobody needs.
    if _is_run_killed(run_id):
        failed_reference_classes = []
        annual_coverage_manifest = None
    else:
        failed_reference_classes = _annual_report_failed_classes(
            per_chunk_diag, company)
        annual_coverage_manifest = _create_annual_report_coverage_manifest(
            company,
            annual_report_s3_key,
            failed_reference_classes,
        )

    converted_request_ids = set()
    converted_queries = set()
    if annual_coverage_manifest:
        for chunk in per_chunk_diag:
            before_failures = sum(
                1 for item in (chunk.get("results") or [])
                if isinstance(item, dict)
                and item.get("status") == "failed"
                and item.get("annual_report_reference_eligible") is True
            )
            updated = _apply_annual_report_references(
                {
                    "error": chunk.get("error"),
                    "results": chunk.get("results") or [],
                    "failures": [],
                },
                company,
                annual_coverage_manifest,
            )
            chunk["results"] = updated.get("results") or []
            references = [
                item for item in chunk["results"]
                if isinstance(item, dict)
                and item.get("status") == "referenced_in_existing_document"
            ]
            for item in references:
                if item.get("request_id"):
                    converted_request_ids.add(str(item["request_id"]))
                if item.get("query"):
                    converted_queries.add(str(item["query"]))
            converted_count = min(before_failures, len(references))
            if converted_count:
                chunk["failures"] = max(
                    0, int(chunk.get("failures") or 0) - converted_count)

        if converted_request_ids or converted_queries:
            all_failures[:] = [
                item for item in all_failures
                if not (
                    isinstance(item, dict)
                    and (
                        (item.get("request_id")
                         and str(item["request_id"])
                         in converted_request_ids)
                        or (not item.get("request_id")
                            and str(item.get("query") or "")
                            in converted_queries)
                    )
                )
            ]
            if not _is_run_killed(run_id):
                _flush_run_row(final=False)

    if _consume_run_kill(run_id):
        log.info("[run %s] killed mid-run — stopping without a final write",
                 run_id[:8])
        return

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

    browser_jobs_pending = any(
        result.get("status") == "browser_retry_queued"
        for pc in per_chunk_diag
        for result in (pc.get("results") or [])
        if isinstance(result, dict)
    )

    if browser_jobs_pending:
        final_status = "browser_retry_pending"
        error_msg = None
    elif downloaded:
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

def _invoke_pageindex_runtime_chunk(
    bucket: str, s3_key: str, label: str,
    page_start: int, page_end: int, toc_slice: list
) -> dict:
    """
    Invoke the PageIndex runtime for a specific page range of a PDF.
    Passes pre-extracted TOC slice so the runtime skips Phases 1 and 2.
    """
    payload_bytes = json.dumps({
        "bucket":     bucket,
        "s3_key":     s3_key,
        "label":      label,
        "page_start": page_start,
        "page_end":   page_end,
        "toc":        toc_slice,   # pre-extracted TOC — skips phases 1+2
    }).encode("utf-8")
    log.info("[pageindex][agentcore] invoking chunk %d-%d for %s",
             page_start, page_end, label)
    raw = _invoke_agentcore(PAGEINDEX_RUNTIME_ARN, PAGEINDEX_QUALIFIER, payload_bytes)
    if not raw:
        raise RuntimeError("Empty response from PageIndex runtime")
    result = json.loads(raw.decode("utf-8"))
    if result.get("status") != "ok":
        raise RuntimeError(f"Runtime returned error: {result.get('error')}")
    return result["index"]

def _merge_chunk_indexes(chunk_results: list, doc_name: str) -> dict:
    """
    Merge multiple chunk index results into one coherent document index.

    chunk_results must already be sorted in document order (by page_start)
    before calling — caller is responsible for sorting.
    PageIndex node dicts do not contain a start_index field so we cannot
    sort here. Renumbers all node_ids to avoid collisions across chunks.
    """
    all_nodes = []
    for result in chunk_results:
        if not result:
            continue
        nodes = result.get("structure", [])
        if nodes:
            all_nodes.extend(nodes)

    if not all_nodes:
        log.warning("[merge] no nodes from any chunk for %s — returning single-node fallback", doc_name)
        title = doc_name.rsplit('.', 1)[0].replace('_', ' ').replace('-', ' ').title()
        return {
            "doc_name":  doc_name,
            "structure": [{
                "title":   title,
                "node_id": "0001",
                "summary": "",
                "nodes":   [],
            }]
        }

    # Renumber node_ids sequentially across the full merged tree.
    # chunk_results is already in page order (sorted by caller).
    counter = [1]
    def _renumber(nodes):
        for node in nodes:
            node["node_id"] = str(counter[0]).zfill(4)
            counter[0] += 1
            if node.get("nodes"):
                _renumber(node["nodes"])
    _renumber(all_nodes)

    log.info("[merge] merged %d top-level nodes from %d chunks for %s",
             len(all_nodes), len(chunk_results), doc_name)
    return {
        "doc_name":  doc_name,
        "structure": all_nodes,
    }


def _write_run_status(run_id: str, update: dict, dynamo=None, company: str = None):
    """Generic DynamoDB updater for pageindex-runs. Never raises.
    Table schema: PK=company, SK=run_id — both are required for every write.
    company defaults to 'unknown' if not supplied (should always be supplied).
    """
    if dynamo is None:
        dynamo = get_dynamo()
    # company is required by the table's composite key; fall back to 'unknown'
    # only as a safety net so we never silently drop a status write.
    key_company = company or update.get("company") or "unknown"
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
            Key={"company": key_company, "run_id": run_id},
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

def _get_report_label_from_provenance(company_slug: str, s3_key: str, dynamo=None) -> str:
    """
    Look up the human-readable report name from the provenance table.
    Returns the 'report' field if found and non-empty, otherwise empty string.
    Provenance table schema: PK=company (slug), SK=s3_key.
    Never raises — returns empty string on any error so caller falls back
    to the S3 filename.
    """
    if dynamo is None:
        dynamo = get_dynamo()
    try:
        resp = dynamo.Table(PROVENANCE_TABLE).get_item(
            Key={"company": company_slug, "s3_key": s3_key}
        )
        item = resp.get("Item")
        if item:
            return (item.get("report") or "").strip()
    except Exception as ex:
        log.warning("[pageindex] provenance lookup failed for %s / %s: %s",
                    company_slug, s3_key, ex)
    return ""

# ═══════════════════════════════════════════════════════════════════════════════
# PageIndex — async worker
# ═══════════════════════════════════════════════════════════════════════════════

def _get_pdf_page_count_and_toc(bucket: str, s3_key: str, s3) -> tuple:
    """
    Stream a PDF from S3 and return (page_count, toc_items).
    toc_items is a list of {"title": ..., "page": ...} dicts extracted
    from embedded PDF bookmarks — empty list if no bookmarks found.
    PyMuPDF primary for page count (more reliable on complex PDFs),
    pypdf fallback. Never raises — returns (0, []) on any error.
    """
    try:
        obj  = s3.get_object(Bucket=bucket, Key=s3_key)
        body = obj["Body"].read()

        # Page count — PyMuPDF primary, pypdf fallback
        page_count = 0
        try:
            import fitz
            fitz_doc   = fitz.open(stream=body, filetype="pdf")
            page_count = fitz_doc.page_count
            fitz_doc.close()
            log.info("[pageindex] PyMuPDF page_count=%d for %s", page_count, s3_key)
        except Exception as fitz_err:
            log.warning("[pageindex] PyMuPDF failed (%s) — falling back to pypdf for %s",
                        fitz_err, s3_key)
            try:
                reader     = PdfReader(BytesIO(body), strict=False)
                page_count = len(reader.pages)
                if page_count == 0:
                    raise ValueError("pypdf returned 0 pages")
                log.info("[pageindex] pypdf page_count=%d for %s", page_count, s3_key)
            except Exception as pypdf_err:
                log.warning(
                    "[pageindex] both PyMuPDF and pypdf failed for %s (%s) "
                    "— assuming page_count=300", s3_key, pypdf_err
                )
                page_count = 300

        # TOC from embedded PDF bookmarks — always use pypdf for this
        # (PyMuPDF outline API is different and pypdf is reliable here)
        toc_items = []
        try:
            reader = PdfReader(BytesIO(body), strict=False)
            def _walk_outline(outline, reader):
                for item in outline:
                    if isinstance(item, list):
                        _walk_outline(item, reader)
                    else:
                        try:
                            page_num = reader.get_destination_page_number(item) + 1
                            toc_items.append({
                                "title": item.title,
                                "page":  page_num,
                            })
                        except Exception:
                            pass
            if reader.outline:
                _walk_outline(reader.outline, reader)
        except Exception as toc_err:
            log.warning("[pageindex] TOC extraction failed for %s: %s", s3_key, toc_err)

        log.info("[pageindex] s3_key=%s page_count=%d toc_items=%d",
                 s3_key, page_count, len(toc_items))
        return page_count, toc_items

    except Exception as exc:
        log.warning("[pageindex] page count/toc extraction failed for %s: %s", s3_key, exc)
        return 0, []

def _split_toc_into_chunks(toc_items: list, total_pages: int, target_pages: int = 60) -> list:
    """
    Split a flat TOC list into chunks of ~target_pages pages each,
    always splitting at section boundaries — never mid-section.

    When no TOC is available, target_pages is reduced to 50 so each chunk
    stays within the 840s AgentCore timeout even in brute-force mode.

    Returns list of:
    {
        "page_start": int,
        "page_end":   int,
        "toc_slice":  [ {"title": ..., "page": ...}, ... ]
    }
    """
    if not toc_items:
        # No TOC — PageIndex must scan every page individually.
        # Use smaller chunks so each runtime call stays under budget.
        no_toc_target = min(target_pages, 40)
        chunks = []
        page = 1
        while page <= total_pages:
            end = min(page + no_toc_target - 1, total_pages)
            chunks.append({"page_start": page, "page_end": end, "toc_slice": []})
            page = end + 1
        log.info("[pageindex] no TOC — using smaller chunks of %d pages (%d total chunks)",
                 no_toc_target, len(chunks))
        return chunks

    chunks      = []
    chunk_start = 1
    chunk_toc   = []

    for i, item in enumerate(toc_items):
        chunk_toc.append(item)
        current_page = item["page"]
        next_page    = toc_items[i + 1]["page"] if i + 1 < len(toc_items) else total_pages + 1

        # Split when chunk reaches target size AND we're at a section boundary.
        # page_end = next_page - 1 so the last section in the chunk gets its
        # full content (current_page is only the START of that section).
        if (next_page - chunk_start) >= target_pages or (current_page - chunk_start) >= target_pages:
            chunks.append({
                "page_start": chunk_start,
                "page_end":   next_page - 1,
                "toc_slice":  chunk_toc,
            })
            chunk_start = next_page
            chunk_toc   = []

    # Last chunk
    if chunk_toc or chunk_start <= total_pages:
        chunks.append({
            "page_start": chunk_start,
            "page_end":   total_pages,
            "toc_slice":  chunk_toc,
        })

    log.info("[pageindex] split %d pages into %d chunks", total_pages, len(chunks))
    return chunks

def _async_pageindex(company: str, s3_prefix: str, force: bool,
                     s3_keys: list = None) -> str:
    run_id      = str(uuid.uuid4())
    now_iso     = datetime.now(timezone.utc).isoformat()
    # company is the PK — must be consistent with every subsequent _write_run_status call.
    key_company = (company or s3_prefix or "unknown").strip()
    try:
        get_dynamo().Table(PAGEINDEX_RUNS_TABLE).put_item(Item={
            "company":       key_company,
            "run_id":        run_id,
            "s3_prefix":     s3_prefix or "",
            "status":        "pending",
            "started_at":    now_iso,
            "force":         force,
            "selective_run": bool(s3_keys),
            "selected_keys": json.dumps(s3_keys or []),
        })
    except Exception as ex:
        log.error("[pageindex][run %s] Initial DynamoDB write failed: %s", run_id[:8], ex)
    t = threading.Thread(
        target=_do_pageindex, args=(run_id, company, s3_prefix, force),
        kwargs={"s3_keys": s3_keys}, daemon=True)
    t.start()
    return run_id


def _do_pageindex(run_id: str, company: str, s3_prefix: str, force: bool,
                  s3_keys: list = None):
    key_company = (company or s3_prefix or "unknown").strip()
    try:
        _do_pageindex_inner(run_id, company, s3_prefix, force, s3_keys=s3_keys)
    except Exception as e:
        log.error("[pageindex][run %s] FATAL: %s", run_id[:8], e)
        try:
            _write_run_status(run_id, {
                "status":      "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_msg":   str(e)[:1000],
            }, company=key_company)
        except Exception as ex2:
            log.error("[pageindex][run %s] Could not write fatal status: %s", run_id[:8], ex2)


def _do_pageindex_inner(run_id: str, company: str, s3_prefix: str, force: bool,
                       s3_keys: list = None):
    """
    Core indexing worker.

    Behaviour matrix (s3_keys = selective list | None):
      s3_keys=None, force=False  index all unindexed docs (default)
      s3_keys=None, force=True   wipe all and re-index entire company
      s3_keys=[..], force=False  index only selected docs if not yet indexed
      s3_keys=[..], force=True   force re-index only selected docs; leave others untouched
    """
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

    # key_company must match what _async_pageindex wrote as the PK
    key_company      = (company or s3_prefix or "unknown").strip()
    pageindex_key    = _pageindex_s3_key(resolved_prefix, slug)
    pageindex_s3_uri = f"s3://{resolved_bucket}/{pageindex_key}"
    s3_keys_set      = set(s3_keys) if s3_keys else None

    _write_run_status(run_id, {
        "status":           "running",
        # NOTE: "company" is the DynamoDB partition key — never include it in
        # the SET expression, only in Key={}. key_company is passed via company= kwarg.
        "slug":             slug,
        "s3_prefix":        resolved_prefix,
        "pageindex_s3_uri": pageindex_s3_uri,
        "started_at":       now_iso,
    }, dynamo, company=key_company)

    if not pdfs:
        log.warning("[pageindex][run %s] No PDFs found for company=%r", run_id[:8], display_company)
        _write_run_status(run_id, {
            "status":      "no_results",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error_msg":   f"No PDFs found under prefix {resolved_prefix}",
            "indexed":     json.dumps([]),
            "skipped":     json.dumps([]),
        }, dynamo, company=key_company)
        return

    # ── Selective key validation ──────────────────────────────────────────────
    if s3_keys_set:
        available_keys = {p["s3_key"] for p in pdfs}
        not_found_keys = s3_keys_set - available_keys
        if not_found_keys:
            msg = (
                f"Requested s3_keys not found under prefix {resolved_prefix!r}: "
                + ", ".join(sorted(not_found_keys))
            )
            log.error("[pageindex][run %s] %s", run_id[:8], msg)
            _write_run_status(run_id, {
                "status":      "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_msg":   msg,
                "indexed":     json.dumps([]),
                "skipped":     json.dumps([]),
            }, dynamo, company=key_company)
            return
        pdfs = [p for p in pdfs if p["s3_key"] in s3_keys_set]
        log.info("[pageindex][run %s] selective run — %d of %d PDFs selected",
                 run_id[:8], len(pdfs), len(available_keys))

    # ── Cleanup / existing index handling ────────────────────────────────────
    existing = _load_existing_index(resolved_bucket, pageindex_key, s3)

    if force and not s3_keys_set:
        # Full wipe — existing behaviour
        log.info("[pageindex][run %s] full force — wiping company index", run_id[:8])
        cleanup = _clean_company_reports(display_company, dynamo=dynamo, s3=s3)
        _write_run_status(run_id, {
            "pre_run_cleanup": json.dumps(cleanup),
        }, dynamo, company=key_company)
        existing  = {}
        documents = []
    elif force and s3_keys_set:
        # Selective force — remove only selected entries, leave rest intact
        log.info("[pageindex][run %s] selective force — removing %d selected entries",
                 run_id[:8], len(s3_keys_set))
        documents = [
            doc for doc in existing.get("documents", [])
            if doc["_meta"]["s3_key"] not in s3_keys_set
        ]
        for sk in s3_keys_set:
            _update_provenance_rag_status(display_company, sk, "Pending", dynamo)
    else:
        documents = list(existing.get("documents", []))

    # already_indexed: keys to SKIP. Selected keys are never skipped.
    if s3_keys_set:
        already_indexed = set()
    elif force:
        already_indexed = set()
    else:
        already_indexed = {doc["_meta"]["s3_key"] for doc in existing.get("documents", [])}

    indexed   = []
    skipped   = []
    error_msg = None

    for pdf_meta in pdfs:
        s3_key   = pdf_meta["s3_key"]
        # Try provenance table first for a human-readable name, fall back to
        # the S3 filename. slug is used as the provenance table PK.
        report_label = _get_report_label_from_provenance(slug, s3_key, dynamo)
        doc_name     = report_label or PurePosixPath(s3_key).name

        if s3_key in already_indexed:
            log.info("[pageindex][run %s] skipping %s — already indexed", run_id[:8], s3_key)
            skipped.append({"s3_key": s3_key, "reason": "already_indexed"})
            continue
        try:
            # For large PDFs (>60 pages) use TOC-guided parallel chunking.
            # For small PDFs use existing single runtime call unchanged.
            page_count, toc_items = _get_pdf_page_count_and_toc(
                resolved_bucket, s3_key, s3)

            if page_count > 60:
                log.info("[pageindex][run %s] large PDF %s (%d pages) — chunked path",
                         run_id[:8], s3_key, page_count)

                # No embedded bookmarks — extract TOC via runtime (1 LLM call)
                if not toc_items:
                    log.info("[pageindex][run %s] no embedded bookmarks — extracting TOC via runtime",
                             run_id[:8])
                    toc_payload = json.dumps({
                        "bucket": resolved_bucket,
                        "s3_key": s3_key,
                        "label":  doc_name,
                        "mode":   "extract_toc",
                    }).encode("utf-8")
                    raw = _invoke_agentcore(
                        PAGEINDEX_RUNTIME_ARN, PAGEINDEX_QUALIFIER, toc_payload)
                    if raw:
                        toc_result = json.loads(raw.decode("utf-8"))
                        toc_items  = toc_result.get("toc", [])
                        log.info("[pageindex][run %s] extracted %d TOC items via runtime",
                                 run_id[:8], len(toc_items))

                chunks = _split_toc_into_chunks(toc_items, page_count)
                log.info("[pageindex][run %s] %d chunks for %s",
                         run_id[:8], len(chunks), s3_key)

                # Fire chunks with bounded concurrency (2 in-flight).
                # Full parallelism saturates the runtime's ThreadPoolExecutor
                # and LLM API quota simultaneously, making timeouts more likely.
                CHUNK_CONCURRENCY = 2
                # Each entry is (page_start, result) so merge sorts correctly
                # regardless of future completion order.
                chunk_results = []   # list of (page_start, result_dict)
                failed_chunks = []
                with ThreadPoolExecutor(max_workers=CHUNK_CONCURRENCY) as chunk_pool:
                    futures = {
                        chunk_pool.submit(
                            _invoke_pageindex_runtime_chunk,
                            resolved_bucket, s3_key, doc_name,
                            chunk["page_start"], chunk["page_end"],
                            chunk["toc_slice"]
                        ): chunk for chunk in chunks
                    }
                    for future in as_completed(futures):
                        chunk = futures[future]
                        try:
                            result = future.result(timeout=840)
                            chunk_results.append((chunk["page_start"], result))
                            log.info(
                                "[pageindex][run %s] chunk pages %d-%d done for %s",
                                run_id[:8], chunk["page_start"], chunk["page_end"], s3_key
                            )
                        except Exception as chunk_exc:
                            log.error(
                                "[pageindex][run %s] chunk pages %d-%d failed for %s: %s",
                                run_id[:8], chunk["page_start"], chunk["page_end"],
                                s3_key, chunk_exc
                            )
                            failed_chunks.append(chunk)

                if not chunk_results:
                    raise RuntimeError(
                        f"All {len(chunks)} chunks failed for {s3_key}"
                    )

                if failed_chunks:
                    log.warning(
                        "[pageindex][run %s] %d/%d chunks failed for %s — "
                        "merging %d successful chunks in document order",
                        run_id[:8], len(failed_chunks), len(chunks), s3_key,
                        len(chunk_results)
                    )

                # Sort by page_start before merging so document order is correct
                chunk_results.sort(key=lambda x: x[0])
                ordered_results = [r for _, r in chunk_results]
                index_data = _merge_chunk_indexes(ordered_results, doc_name)

            else:
                index_data = _invoke_pageindex_runtime(resolved_bucket, s3_key, doc_name)

        except RuntimeError as exc:
            log.error("[pageindex][run %s] runtime failed for %s: %s", run_id[:8], s3_key, exc)
            skipped.append({"s3_key": s3_key, "reason": str(exc)})
            error_msg = str(exc)
            _update_provenance_rag_status(display_company, s3_key, "Failed", dynamo)
            continue
        except Exception as exc:
            log.error("[pageindex][run %s] chunked indexing failed for %s: %s",
                      run_id[:8], s3_key, exc)
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
        }, dynamo, company=key_company)

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
    }, dynamo, company=key_company)

    log.info("[pageindex][run %s] Done. status=%s indexed=%d skipped=%d pageindex=%s",
             run_id[:8], final_status, len(indexed), len(skipped), pageindex_s3_uri)


# ═══════════════════════════════════════════════════════════════════════════════
# PageIndex — routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/pageindex", methods=["POST"])
def trigger_pageindex():
    """
    Trigger a PageIndex run.

    Body fields:
      company    str           Company name (or use s3_prefix)
      s3_prefix  str           S3 prefix override
      force      bool          Re-index even if already indexed
      s3_keys    list[str]     Optional — index only these specific S3 keys.

    Behaviour matrix:
      s3_keys=None, force=false  index all unindexed docs (default)
      s3_keys=None, force=true   wipe and re-index all docs for company
      s3_keys=[..], force=false  index only selected docs if not yet indexed
      s3_keys=[..], force=true   force re-index only selected docs; leave others untouched
    """
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400
    company   = (body.get("company")   or "").strip()
    s3_prefix = (body.get("s3_prefix") or "").strip()
    force     = bool(body.get("force", False))
    s3_keys   = body.get("s3_keys")

    if not company and not s3_prefix:
        return jsonify({"error": "Either 'company' or 's3_prefix' is required"}), 400

    if s3_keys is not None:
        if not isinstance(s3_keys, list) or len(s3_keys) == 0:
            return jsonify({"error": "'s3_keys' must be a non-empty list of S3 key strings"}), 400
        if not all(isinstance(k, str) and k.strip() for k in s3_keys):
            return jsonify({"error": "'s3_keys' entries must be non-empty strings"}), 400
        s3_keys = [k.strip() for k in s3_keys]

    run_id = _async_pageindex(company, s3_prefix, force, s3_keys=s3_keys)
    log.info(
        "[pageindex][api] triggered run=%s company=%r s3_prefix=%r "
        "force=%s selective=%s keys=%d",
        run_id[:8], company, s3_prefix, force,
        bool(s3_keys), len(s3_keys) if s3_keys else 0,
    )
    return jsonify({
        "run_id":        run_id,
        "status":        "triggered",
        "company":       company or s3_prefix,
        "s3_prefix":     s3_prefix,
        "force":         force,
        "s3_keys":       s3_keys,
        "selective_run": bool(s3_keys),
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
    """
    Table schema is PK=company, SK=run_id — we don't have company in this
    route, so we scan with a filter on run_id. This is acceptable because
    run IDs are UUIDs (unique) and runs tables are small.
    For high-volume use, add a GSI on run_id.
    """
    dynamo = get_dynamo()
    try:
        resp = dynamo.Table(PAGEINDEX_RUNS_TABLE).scan(
            FilterExpression="#rid = :rid",
            ExpressionAttributeNames={"#rid": "run_id"},
            ExpressionAttributeValues={":rid": run_id},
        )
        items = resp.get("Items", [])
        # handle pagination (unlikely for a UUID lookup but correct to handle)
        while "LastEvaluatedKey" in resp:
            resp = dynamo.Table(PAGEINDEX_RUNS_TABLE).scan(
                FilterExpression="#rid = :rid",
                ExpressionAttributeNames={"#rid": "run_id"},
                ExpressionAttributeValues={":rid": run_id},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items += resp.get("Items", [])
    except Exception as ex:
        log.error("[pageindex] get_run scan failed run_id=%s: %s", run_id[:8], ex)
        return jsonify({"error": str(ex)}), 500
    if not items:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(items[0])


# ═══════════════════════════════════════════════════════════════════════════════
# Answering Agent — helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _category_from_filename(filename: str) -> str:
    """
    Derive a human-readable category name from an MD filename.
    code_of_conduct.md  ->  Code Of Conduct
    water_metrics.md    ->  Water Metrics
    """
    name = filename
    if name.endswith(".md"):
        name = name[:-3]
    return name.replace("_", " ").replace("-", " ").title()


def _s3_key_from_uri(s3_uri: str) -> str:
    """Return the object key from an s3://bucket/key URI."""
    candidate = str(s3_uri or "").strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() != "s3" or not parsed.netloc:
        return ""
    return parsed.path.lstrip("/")


def _safe_provenance_source_url(value: str) -> str:
    """Return a bounded official HTTP(S) URL from a provenance record."""
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2048:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password):
        return ""
    return candidate


def _load_citation_provenance(
    company_slug: str,
    citations_by_result: list,
    dynamo=None,
) -> dict:
    """
    Resolve citation document keys to their original official download URLs.

    PageIndex citations intentionally store the durable S3 URI. The report
    downloader's provenance table stores the corresponding original
    ``source_url`` under the composite key ``(company, s3_key)``. Read each
    unique key once and return only validated HTTP(S) URLs to the API caller.
    """
    if dynamo is None:
        dynamo = get_dynamo()

    s3_keys = {
        _s3_key_from_uri(citation.get("s3_uri", ""))
        for citations in citations_by_result
        for citation in (citations or [])
        if isinstance(citation, dict)
    }
    s3_keys.discard("")
    if not company_slug or not s3_keys:
        return {}

    table = dynamo.Table(PROVENANCE_TABLE)
    provenance = {}
    for s3_key in s3_keys:
        try:
            item = table.get_item(
                Key={"company": company_slug, "s3_key": s3_key}
            ).get("Item", {})
        except Exception as ex:
            log.warning(
                "[answering] provenance lookup failed company=%s key=%s: %s",
                company_slug,
                s3_key,
                ex,
            )
            continue

        source_url = _safe_provenance_source_url(item.get("source_url", ""))
        if not source_url:
            continue
        provenance[s3_key] = {
            "source_url": source_url,
            "source_title": (
                item.get("report")
                or item.get("file_name")
                or PurePosixPath(s3_key).name
            ),
        }

    return provenance


def _enrich_citations_with_provenance(
    citations: list,
    provenance_by_key: dict,
) -> list:
    """Attach official source details without replacing citation evidence."""
    enriched = []
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        row = dict(citation)
        s3_key = _s3_key_from_uri(row.get("s3_uri", ""))
        source = provenance_by_key.get(s3_key, {})
        row["source_url"] = source.get("source_url", "")
        row["source_title"] = source.get("source_title", "")
        enriched.append(row)
    return enriched


def _list_questionnaire_md_files() -> list:
    """
    List all .md files under QUESTIONNAIRES_PREFIX in QUESTIONNAIRES_BUCKET.
    Returns list of { filename, s3_key, s3_uri, category }.
    """
    s3  = get_s3()
    out = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=QUESTIONNAIRES_BUCKET,
                                       Prefix=QUESTIONNAIRES_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.lower().endswith(".md"):
                    continue
                filename = key.split("/")[-1]
                out.append({
                    "filename": filename,
                    "s3_key":   key,
                    "s3_uri":   f"s3://{QUESTIONNAIRES_BUCKET}/{key}",
                    "category": _category_from_filename(filename),
                })
    except ClientError as e:
        log.error("[answering] list_questionnaire_md_files error: %s", e)
    return out


def _invoke_answering_runtime(
    questionnaire_s3_uri: str,
    pageindex_s3_uri: str,
    company: str,
    run_id: str,
) -> dict:
    """
    Invoke the answering agent runtime for ONE questionnaire MD file.
    Same pattern as _invoke_pageindex_runtime.
    Raises RuntimeError on error status.
    """
    payload_bytes = json.dumps({
        "run_id":           run_id,
        "company":          company,
        "pageindex":        {"s3_uri": pageindex_s3_uri},
        "questionnaire_md": {"s3_uri": questionnaire_s3_uri},
    }).encode("utf-8")

    log.info(
        "[answering][agentcore] invoking runtime — company=%r md=%s",
        company, questionnaire_s3_uri,
    )
    raw = _invoke_agentcore(
        ANSWERING_RUNTIME_ARN,
        ANSWERING_QUALIFIER,
        payload_bytes,
    )
    if not raw:
        raise RuntimeError("Empty response from answering runtime")

    response = json.loads(raw.decode("utf-8"))
    if response.get("status") != "ok":
        raise RuntimeError(
            f"Answering runtime returned error: "
            f"{response.get('message') or response.get('error_type')}"
        )

    log.info(
        "[answering][agentcore] completed — company=%r md=%s",
        company, questionnaire_s3_uri,
    )
    return response["result"]


def _write_answering_run_status(run_id: str, update: dict, dynamo=None, session_id: str = None):
    """
    Generic DynamoDB updater for answering-runs table.
    Table schema: PK=session_id, SK=run_id — both required for every write.
    session_id should be passed explicitly; falls back to scanning for the row.
    Never raises.
    """
    if dynamo is None:
        dynamo = get_dynamo()

    # If session_id not supplied, look it up via scan (slower but safe fallback)
    key_session = session_id
    if not key_session:
        try:
            resp = dynamo.Table(ANSWERING_RUNS_TABLE).scan(
                FilterExpression="#rid = :rid",
                ExpressionAttributeNames={"#rid": "run_id"},
                ExpressionAttributeValues={":rid": run_id},
                ProjectionExpression="session_id",
            )
            items = resp.get("Items", [])
            key_session = items[0]["session_id"] if items else "unknown"
        except Exception:
            key_session = "unknown"

    try:
        expr_names  = {}
        expr_values = {}
        set_parts   = []
        key_attrs = {"company", "run_id"}
        for k, v in update.items():
            if k in key_attrs:
                continue
            safe_key = f"#f_{k}"
            val_key  = f":v_{k}"
            expr_names[safe_key]  = k
            expr_values[val_key]  = v
            set_parts.append(f"{safe_key} = {val_key}")
        dynamo.Table(ANSWERING_RUNS_TABLE).update_item(
            Key={"session_id": key_session, "run_id": run_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except Exception as ex:
        log.error(
            "[answering][run %s] DynamoDB run status write failed: %s",
            run_id[:8], ex,
        )


def _delete_old_answering_results(company_slug: str, md_file: str, dynamo=None):
    """
    Delete all existing answering-results rows for a given company_slug + md_file
    before writing fresh results. Prevents duplicate rows accumulating across runs.
    """
    if dynamo is None:
        dynamo = get_dynamo()
    table = dynamo.Table(ANSWERING_RESULTS_TABLE)
    try:
        resp = table.scan(
            FilterExpression="#cs = :slug AND #mf = :md",
            ExpressionAttributeNames={"#cs": "company_slug", "#mf": "md_file"},
            ExpressionAttributeValues={":slug": company_slug, ":md": md_file},
            ProjectionExpression="run_id, result_id",
        )
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(
                FilterExpression="#cs = :slug AND #mf = :md",
                ExpressionAttributeNames={"#cs": "company_slug", "#mf": "md_file"},
                ExpressionAttributeValues={":slug": company_slug, ":md": md_file},
                ProjectionExpression="run_id, result_id",
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items += resp.get("Items", [])
        if not items:
            return
        for i in range(0, len(items), 25):
            with table.batch_writer() as batch:
                for item in items[i:i + 25]:
                    batch.delete_item(Key={"run_id": item["run_id"], "result_id": item["result_id"]})
        log.info("[answering] deleted %d stale rows for company_slug=%s md_file=%s", len(items), company_slug, md_file)
    except Exception as ex:
        log.error("[answering] failed to delete old results for %s/%s: %s", company_slug, md_file, ex)


def _write_answering_results(run_result: dict, dynamo=None):
    """
    Write every question result from a RunResult dict into the
    answering-results DynamoDB table.

    Deletes all previous rows for the same company_slug + md_file before
    writing so re-runs overwrite rather than append.

    Table schema: PK=run_id (String), SK=result_id (String)
    result_id format: md_file#question_id  (e.g. code_of_conduct.md#q1)

    company_slug is stored as a regular attribute so the query routes
    can still filter/group by company.
    """
    if dynamo is None:
        dynamo = get_dynamo()

    company_slug = run_result.get("company_slug", "")
    md_file      = run_result.get("md_file", "")
    category     = run_result.get("category", "")
    run_id       = run_result.get("run_id", "")
    company      = run_result.get("company", "")
    created_at   = datetime.now(timezone.utc).isoformat()
    table        = dynamo.Table(ANSWERING_RESULTS_TABLE)

    # Delete stale rows before writing fresh ones.
    if company_slug and md_file:
        _delete_old_answering_results(company_slug, md_file, dynamo)

    for q in run_result.get("results", []):
        question_id = q.get("question_id", "")
        result_id   = f"{md_file}#{question_id}"
        try:
            table.put_item(Item={
                "run_id":          run_id,        # PK
                "result_id":       result_id,     # SK  (md_file#question_id)
                "company_slug":    company_slug,  # regular attribute for filtering
                "company":         company,
                "md_file":         md_file,
                "category":        category,
                "question_id":     question_id,
                "question_label":  q.get("question_label", ""),
                "answer":          json.dumps(q.get("answer_payload", {})),
                "confidence":      q.get("confidence", {}).get("final", ""),
                "confidence_full": json.dumps(q.get("confidence", {})),
                "citations":       json.dumps([
                    {
                        "id":          c.get("id", ""),
                        "doc_name":    c.get("doc_name", ""),
                        "page_start":  c.get("page_start"),
                        "page_end":    c.get("page_end"),
                        "quoted_span": c.get("quoted_span", ""),
                        "node_path":   c.get("node_path", ""),
                        "s3_uri":      c.get("s3_uri", ""),
                    }
                    for c in q.get("citations", [])
                ]),
                "flags":           json.dumps(q.get("flags", [])),
                "tool_calls_used": q.get("tool_calls_used", 0),
                "error":           q.get("error") or "",
                "created_at":      created_at,
            })
        except Exception as ex:
            log.error(
                "[answering][run %s] DynamoDB result write failed for %s: %s",
                run_id[:8], result_id, ex,
            )


def _async_answering(company: str, pageindex_s3_uri: str, md_files: list = None) -> str:
    """
    Start a background answering run for one company.
    If md_files is supplied only those files are processed (single file re-run).
    Otherwise all questionnaire MD files in S3 are used.
    Returns run_id immediately.
    """
    run_id   = str(uuid.uuid4())
    now_iso  = datetime.now(timezone.utc).isoformat()
    md_files = md_files if md_files is not None else _list_questionnaire_md_files()

    # answering-runs table schema: PK=session_id (String), SK=run_id (String).
    # We have no separate session concept — use company slug as session_id so
    # all runs for a company share a logical session and can be queried together.
    session_id = _agent_slug(company)
    try:
        get_dynamo().Table(ANSWERING_RUNS_TABLE).put_item(Item={
            "session_id":       session_id,      # PK
            "run_id":           run_id,          # SK
            "company":          company,
            "company_slug":     _agent_slug(company),
            "pageindex_s3_uri": pageindex_s3_uri,
            "status":           "running",
            "started_at":       now_iso,
            "heartbeat_at":     now_iso,
            "md_total":         len(md_files),
            "md_done":          0,
            "md_files":         json.dumps([f["filename"] for f in md_files]),
            "error_msg":        "",
        })
    except Exception as ex:
        log.error(
            "[answering][run %s] Initial DynamoDB write failed: %s",
            run_id[:8], ex,
        )

    t = threading.Thread(
        target=_do_answering,
        args=(run_id, company, pageindex_s3_uri, md_files),
        daemon=True,
    )
    t.start()
    return run_id


def _do_answering(run_id: str, company: str, pageindex_s3_uri: str, md_files: list):
    """Outer wrapper — ensures run never stays stuck on 'running'."""
    session_id = _agent_slug(company)
    try:
        _do_answering_inner(run_id, company, pageindex_s3_uri, md_files, session_id)
    except Exception as e:
        log.error("[answering][run %s] FATAL: %s", run_id[:8], e)
        try:
            _write_answering_run_status(run_id, {
                "status":      "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_msg":   str(e)[:1000],
            }, session_id=session_id)
        except Exception as ex2:
            log.error(
                "[answering][run %s] Could not write fatal status: %s",
                run_id[:8], ex2,
            )


from concurrent.futures import ThreadPoolExecutor, as_completed

def _do_answering_inner(
    run_id: str,
    company: str,
    pageindex_s3_uri: str,
    md_files: list,
    session_id: str = None,
):
    dynamo     = get_dynamo()
    any_error  = False
    session_id = session_id or _agent_slug(company)
    md_done    = 0
    lock       = threading.Lock()

    def _process_md(md):
        try:
            run_result = _invoke_answering_runtime(
                questionnaire_s3_uri=md["s3_uri"],
                pageindex_s3_uri=pageindex_s3_uri,
                company=company,
                run_id=run_id,
            )
            _write_answering_results(run_result, dynamo)
            return None
        except Exception as ex:
            log.error(
                "[answering][run %s] Failed for md=%s: %s",
                run_id[:8], md["filename"], ex,
            )
            return str(ex)

    # Run MD files in parallel — max 3 concurrent AgentCore sessions
    max_workers = int(os.environ.get("ANSWERING_MD_CONCURRENCY", "3"))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_md, md): md for md in md_files}
        for fut in as_completed(futures):
            md = futures[fut]
            err = fut.result()
            if err:
                any_error = True
            with lock:
                md_done += 1
            _write_answering_run_status(run_id, {
                "md_done":      md_done,
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }, dynamo, session_id=session_id)

    final_status = "failed" if (any_error and md_done == 0) else "complete"
    _write_answering_run_status(run_id, {
        "status":      final_status,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "md_done":     md_done,
    }, dynamo, session_id=session_id)

    log.info(
        "[answering][run %s] Done. status=%s md_done=%d/%d",
        run_id[:8], final_status, md_done, len(md_files),
    )

# ═══════════════════════════════════════════════════════════════════════════════
# Answering Agent — routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/answering-agent/questionnaires", methods=["GET"])
def list_questionnaires():
    """List all questionnaire MD files available in S3."""
    return jsonify(_list_questionnaire_md_files())


@app.route("/api/answering-agent/questionnaires/<filename>", methods=["GET"])
def get_questionnaire(filename):
    """Fetch the content of a single questionnaire MD file from S3."""
    if not filename.endswith(".md"):
        return jsonify({"error": "filename must end in .md"}), 400
    s3  = get_s3()
    key = QUESTIONNAIRES_PREFIX + filename
    try:
        obj  = s3.get_object(Bucket=QUESTIONNAIRES_BUCKET, Key=key)
        text = obj["Body"].read().decode("utf-8")
        return jsonify({
            "filename": filename,
            "s3_key":   key,
            "s3_uri":   f"s3://{QUESTIONNAIRES_BUCKET}/{key}",
            "category": _category_from_filename(filename),
            "content":  text,
        })
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchKey", "404"):
            return jsonify({"error": f"{filename} not found"}), 404
        log.error("[questionnaire] get %s: %s", filename, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/answering-agent/questionnaires", methods=["POST"])
def create_questionnaire():
    """
    Create a new questionnaire MD file in S3.

    Body:
        { "name": "Code Of Conduct", "content": "...md text..." }

    The display name is slugified to a filename:
        "Code Of Conduct" -> "code_of_conduct.md"
    Returns 409 if the file already exists.
    """
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400
    name    = (body.get("name") or "").strip()
    content = body.get("content") or ""
    if not name:
        return jsonify({"error": "name is required"}), 400
    filename = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + ".md"
    key      = QUESTIONNAIRES_PREFIX + filename
    s3       = get_s3()
    # Check for duplicates.
    try:
        s3.head_object(Bucket=QUESTIONNAIRES_BUCKET, Key=key)
        return jsonify({"error": f"{filename} already exists. Use PUT to update."}), 409
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            log.error("[questionnaire] head %s: %s", filename, e)
            return jsonify({"error": str(e)}), 500
    # Write.
    try:
        s3.put_object(
            Bucket=QUESTIONNAIRES_BUCKET,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown",
        )
        log.info("[questionnaire] created %s", key)
        return jsonify({
            "filename": filename,
            "s3_key":   key,
            "s3_uri":   f"s3://{QUESTIONNAIRES_BUCKET}/{key}",
            "category": _category_from_filename(filename),
        }), 201
    except ClientError as e:
        log.error("[questionnaire] put %s: %s", filename, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/answering-agent/questionnaires/<filename>", methods=["PUT"])
def update_questionnaire(filename):
    """
    Overwrite an existing questionnaire MD file in S3.

    Body:
        { "content": "...updated md text..." }
    """
    if not filename.endswith(".md"):
        return jsonify({"error": "filename must end in .md"}), 400
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400
    content = body.get("content") or ""
    key     = QUESTIONNAIRES_PREFIX + filename
    s3      = get_s3()
    try:
        s3.put_object(
            Bucket=QUESTIONNAIRES_BUCKET,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown",
        )
        log.info("[questionnaire] updated %s", key)
        return jsonify({
            "filename": filename,
            "s3_key":   key,
            "s3_uri":   f"s3://{QUESTIONNAIRES_BUCKET}/{key}",
            "category": _category_from_filename(filename),
        })
    except ClientError as e:
        log.error("[questionnaire] put %s: %s", filename, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/answering-agent/questionnaires/<filename>", methods=["DELETE"])
def delete_questionnaire(filename):
    """Delete a questionnaire MD file from S3."""
    if not filename.endswith(".md"):
        return jsonify({"error": "filename must end in .md"}), 400
    key = QUESTIONNAIRES_PREFIX + filename
    s3  = get_s3()
    try:
        s3.delete_object(Bucket=QUESTIONNAIRES_BUCKET, Key=key)
        log.info("[questionnaire] deleted %s", key)
        return jsonify({"deleted": filename})
    except ClientError as e:
        log.error("[questionnaire] delete %s: %s", filename, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/answering-agent/run", methods=["POST"])
def trigger_answering_run():
    """
    Trigger an answering run for one company across all questionnaire MD files.

    Body:
        { "company": "Paccar" }
        or
        { "company": "Paccar", "pageindex_s3_uri": "s3://..." }

    If pageindex_s3_uri is not supplied, it is resolved automatically from
    the most recent completed pageindex run for this company.
    """
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400

    company          = (body.get("company") or "").strip()
    pageindex_s3_uri = (body.get("pageindex_s3_uri") or "").strip()
    md_file_filter   = (body.get("md_file") or "").strip()

    if not company:
        return jsonify({"error": "company is required"}), 400

    # Auto-resolve pageindex_s3_uri from the most recent completed
    # pageindex run if not supplied by the caller.
    if not pageindex_s3_uri:
        try:
            dynamo = get_dynamo()
            resp   = dynamo.Table(PAGEINDEX_RUNS_TABLE).scan(
                FilterExpression="#co = :c AND #st = :s",
                ExpressionAttributeNames={"#co": "company", "#st": "status"},
                ExpressionAttributeValues={":c": company, ":s": "complete"},
            )
            items = resp.get("Items", [])
            if items:
                latest           = max(items, key=lambda x: x.get("started_at", ""))
                pageindex_s3_uri = latest.get("pageindex_s3_uri", "")
        except Exception as ex:
            log.error("[answering] pageindex lookup failed for company=%r: %s", company, ex)

    if not pageindex_s3_uri:
        return jsonify({
            "error": (
                f"No completed pageindex run found for company '{company}'. "
                "Run /api/pageindex first, or supply pageindex_s3_uri explicitly."
            )
        }), 400

    # If md_file supplied, run only that file; otherwise run all.
    if md_file_filter:
        all_files = _list_questionnaire_md_files()
        md_files  = [f for f in all_files if f["filename"] == md_file_filter]
        if not md_files:
            return jsonify({"error": f"MD file '{md_file_filter}' not found in S3."}), 404
    else:
        md_files = None  # _async_answering will list all files

    run_id = _async_answering(company, pageindex_s3_uri, md_files=md_files)

    log.info(
        "[answering][api] triggered run=%s company=%r pageindex=%s md_file=%s",
        run_id[:8], company, pageindex_s3_uri, md_file_filter or "all",
    )
    return jsonify({
        "run_id":           run_id,
        "status":           "triggered",
        "company":          company,
        "pageindex_s3_uri": pageindex_s3_uri,
        "md_file":          md_file_filter or "all",
    }), 202


@app.route("/api/answering-agent/runs/<run_id>", methods=["GET"])
def get_answering_run(run_id):
    """
    Poll the status of an answering run.
    Table schema is PK=session_id, SK=run_id — we don't have session_id in
    this route so we scan with a filter. For high-volume use, add a GSI on run_id.
    """
    dynamo = get_dynamo()
    try:
        resp = dynamo.Table(ANSWERING_RUNS_TABLE).scan(
            FilterExpression="#rid = :rid",
            ExpressionAttributeNames={"#rid": "run_id"},
            ExpressionAttributeValues={":rid": run_id},
        )
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = dynamo.Table(ANSWERING_RUNS_TABLE).scan(
                FilterExpression="#rid = :rid",
                ExpressionAttributeNames={"#rid": "run_id"},
                ExpressionAttributeValues={":rid": run_id},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items += resp.get("Items", [])
    except Exception as ex:
        log.error("[answering] get_run scan failed run_id=%s: %s", run_id[:8], ex)
        return jsonify({"error": str(ex)}), 500
    if not items:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(items[0])


@app.route("/api/answering-agent/companies", methods=["GET"])
def list_answering_companies():
    """
    List all companies that have answering results stored.
    Scans answering-runs for distinct companies, returns the most recent
    run per company.
    """
    dynamo = get_dynamo()
    table  = dynamo.Table(ANSWERING_RUNS_TABLE)
    result = table.scan()
    items  = result.get("Items", [])
    while "LastEvaluatedKey" in result:
        result = table.scan(ExclusiveStartKey=result["LastEvaluatedKey"])
        items += result.get("Items", [])

    # Deduplicate by company_slug — keep the most recent run per company.
    by_slug = {}
    for item in items:
        slug = item.get("company_slug", "")
        if not slug:
            continue
        existing = by_slug.get(slug)
        if existing is None or item.get("started_at", "") > existing.get("started_at", ""):
            by_slug[slug] = item

    companies = [
        {
            "company":      v.get("company", ""),
            "company_slug": v.get("company_slug", ""),
            "last_run_id":  v.get("run_id", ""),
            "last_run_at":  v.get("started_at", ""),
            "status":       v.get("status", ""),
            "md_done":      v.get("md_done", 0),
            "md_total":     v.get("md_total", 0),
        }
        for v in sorted(
            by_slug.values(),
            key=lambda x: x.get("started_at", ""),
            reverse=True,
        )
    ]
    return jsonify(companies)


@app.route("/api/answering-agent/companies/<slug>", methods=["GET"])
def get_answering_company(slug):
    """
    Return all categories (one per MD file) for a company.
    answering-results table: PK=run_id, SK=result_id.
    company_slug is a regular attribute — scan with filter.
    For high-volume use, add a GSI on company_slug.
    """
    dynamo = get_dynamo()
    table  = dynamo.Table(ANSWERING_RESULTS_TABLE)

    try:
        resp = table.scan(
            FilterExpression="#cs = :slug",
            ExpressionAttributeNames={"#cs": "company_slug"},
            ExpressionAttributeValues={":slug": slug},
            ProjectionExpression=(
                "md_file, category, question_id, confidence, created_at, run_id, company"
            ),
        )
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(
                FilterExpression="#cs = :slug",
                ExpressionAttributeNames={"#cs": "company_slug"},
                ExpressionAttributeValues={":slug": slug},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
                ProjectionExpression=(
                    "md_file, category, question_id, confidence, created_at, run_id, company"
                ),
            )
            items += resp.get("Items", [])
    except Exception as ex:
        log.error(
            "[answering] company scan failed for slug=%s: %s", slug, ex,
        )
        return jsonify({"error": str(ex)}), 500

    if not items:
        return jsonify({"error": "Company not found"}), 404

    # Group by md_file.
    by_md = {}
    for item in items:
        md = item.get("md_file", "")
        if md not in by_md:
            by_md[md] = {
                "md_file":        md,
                "category":       item.get("category", _category_from_filename(md)),
                "question_count": 0,
                "run_id":         item.get("run_id", ""),
                "last_updated":   item.get("created_at", ""),
            }
        by_md[md]["question_count"] += 1

    company_name = items[0].get("company", slug) if items else slug

    return jsonify({
        "company":      company_name,
        "company_slug": slug,
        "categories":   sorted(by_md.values(), key=lambda x: x["category"]),
    })


@app.route("/api/answering-agent/companies/<slug>/<category>", methods=["GET"])
def get_answering_category(slug, category):
    """
    Return all Q&A results for one company + category.
    answering-results table: PK=run_id, SK=result_id.
    Scan with filter on company_slug + md_file.

    <category> accepts either:
    - URL slug form:  code-of-conduct
    - Exact md_file:  code_of_conduct.md
    Both are tried.
    """
    dynamo = get_dynamo()
    table  = dynamo.Table(ANSWERING_RESULTS_TABLE)

    # Normalise category param to md_file name.
    md_file_guess = category.replace("-", "_")
    if not md_file_guess.endswith(".md"):
        md_file_guess += ".md"

    try:
        resp = table.scan(
            FilterExpression="#cs = :slug AND #mf = :md",
            ExpressionAttributeNames={"#cs": "company_slug", "#mf": "md_file"},
            ExpressionAttributeValues={":slug": slug, ":md": md_file_guess},
        )
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(
                FilterExpression="#cs = :slug AND #mf = :md",
                ExpressionAttributeNames={"#cs": "company_slug", "#mf": "md_file"},
                ExpressionAttributeValues={":slug": slug, ":md": md_file_guess},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items += resp.get("Items", [])
    except Exception as ex:
        log.error(
            "[answering] category scan failed slug=%s category=%s: %s",
            slug, category, ex,
        )
        return jsonify({"error": str(ex)}), 500

    if not items:
        return jsonify({"error": "Category not found"}), 404

    # Parse JSON-stored fields back to objects.
    parsed_items = []
    for item in items:
        try:
            citations = json.loads(item.get("citations", "[]"))
        except Exception:
            citations = []
        if not isinstance(citations, list):
            citations = []
        try:
            confidence_full = json.loads(item.get("confidence_full", "{}"))
        except Exception:
            confidence_full = {}
        if not isinstance(confidence_full, dict):
            confidence_full = {}
        try:
            flags = json.loads(item.get("flags", "[]"))
        except Exception:
            flags = []
        if not isinstance(flags, list):
            flags = []
        try:
            answer = json.loads(item.get("answer", "{}"))
        except Exception:
            answer = {}

        parsed_items.append({
            "question_id":     item.get("question_id", ""),
            "question_label":  item.get("question_label", ""),
            "answer":          answer,
            "confidence":      item.get("confidence", ""),
            "confidence_full": confidence_full,
            "citations":       citations,
            "flags":           flags,
            "tool_calls_used": item.get("tool_calls_used", 0),
            "error":           item.get("error", "") or None,
        })

    # Resolve every unique cited S3 key against the downloader's provenance
    # table. This keeps the internal bucket URI out of the UI and gives each
    # citation the original official document URL.
    provenance_by_key = _load_citation_provenance(
        slug,
        [result["citations"] for result in parsed_items],
        dynamo,
    )
    results = []
    for result in parsed_items:
        result["citations"] = _enrich_citations_with_provenance(
            result["citations"],
            provenance_by_key,
        )
        results.append(result)

    results.sort(key=lambda r: r["question_id"])
    category_name = items[0].get("category", "") if items else category

    return jsonify({
        "company_slug": slug,
        "category":     category_name,
        "md_file":      md_file_guess,
        "results":      results,
        "total":        len(results),
    })


# ─── Health ───────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
