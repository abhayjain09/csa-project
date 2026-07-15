"""Vertex AI grounded-search Lambda — ISOLATED Tier 2 discovery engine.

Replaces the AgentCore managed WebSearch tool as the candidate-URL generator
for the Report IQ / EDO Co-Analyst download agent.

Contract (direct boto3 RequestResponse invoke from the AgentCore runtime):

  IN  : {"query": "<text>", "site": "<domain or ''>", "max_results": <int>}
  OUT : {"results": [{"title","url","snippet"}, ...],
         "via": "vertex-grounding", "count": <int>}

Design rules that mirror the agent's own principles:
  * We NEVER return the model's generated answer text. Gemini can emit a
    hallucinated / reconstructed URL even with grounding on. The ONLY
    authoritative sources are the grounding chunks, and each of those is a
    vertexaisearch.cloud.google.com redirect that must be resolved to its real
    destination. The agent still runs every URL we return through its own
    fail-closed _llm_select_best + _confident gate — this Lambda is a
    candidate generator, never a source of truth.
  * The GCP service-account key is fetched from Secrets Manager into THIS tiny
    function only. It never enters the big agent container that renders
    untrusted third-party pages with Playwright. That isolation is the whole
    reason this stayed a separate Lambda.

Security note: lock the GCP service account down in GCP IAM to
roles/aiplatform.user only, so even a leaked key can do nothing but call
Vertex inference.
"""

import concurrent.futures
import json
import os
import threading
import urllib.request

import boto3
from google.oauth2 import service_account
import google.auth.transport.requests

# ─── Config (env-driven) ─────────────────────────────────────────────────────
SECRET_NAME         = os.environ.get("GCP_SECRET_NAME", "GCP_Vertex_Service_Account_Key")
VERTEX_LOCATION     = os.environ.get("VERTEX_LOCATION", "us-central1")
VERTEX_MODEL_ID     = os.environ.get("VERTEX_MODEL_ID", "gemini-2.5-flash")
REDIRECT_WORKERS    = int(os.environ.get("REDIRECT_WORKERS", "8"))
REDIRECT_TIMEOUT    = int(os.environ.get("REDIRECT_TIMEOUT", "5"))
DEFAULT_MAX_RESULTS = int(os.environ.get("DEFAULT_MAX_RESULTS", "10"))
VERTEX_HTTP_TIMEOUT = int(os.environ.get("VERTEX_HTTP_TIMEOUT", "60"))
GCP_SCOPES          = ["https://www.googleapis.com/auth/cloud-platform"]

UA = "EDO-CoAnalyst/1.0 (+compliance-research)"

_secrets = boto3.client("secretsmanager")

# ─── Cached credentials (warm-invoke reuse, IN-MEMORY ONLY, never on disk) ────
_creds = None
_project_id = None
_creds_lock = threading.Lock()


def _get_credentials():
    """Fetch the SA key once per warm container, mint/refresh the OAuth token
    on expiry. Held in module memory only."""
    global _creds, _project_id
    with _creds_lock:
        if _creds is None:
            resp = _secrets.get_secret_value(SecretId=SECRET_NAME)
            info = json.loads(resp["SecretString"])
            _project_id = info.get("project_id")
            _creds = service_account.Credentials.from_service_account_info(
                info, scopes=GCP_SCOPES)
        if not _creds.valid:
            _creds.refresh(google.auth.transport.requests.Request())
        return _creds, _project_id


# ─── Vertex grounded generateContent ─────────────────────────────────────────
def _vertex_grounded_search(query: str, project_id: str, token: str) -> dict:
    url = (f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/"
           f"{project_id}/locations/{VERTEX_LOCATION}/publishers/google/models/"
           f"{VERTEX_MODEL_ID}:generateContent")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=VERTEX_HTTP_TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


# ─── Redirect resolution (grounding chunks → real destination URLs) ──────────
def _resolve_one(item: tuple) -> dict | None:
    """item = (chunk_dict, snippet_text). Resolve the vertexaisearch redirect
    to the real destination URL. Falls back to GET, then to the raw redirect
    (the agent HEAD-checks everything downstream anyway)."""
    chunk, snippet = item
    web = chunk.get("web", {}) if isinstance(chunk, dict) else {}
    redirect_uri = web.get("uri")
    title = web.get("title", "") or ""
    if not redirect_uri:
        return None

    real_url = redirect_uri
    for method in ("HEAD", "GET"):
        try:
            r = urllib.request.Request(
                redirect_uri, method=method, headers={"User-Agent": UA})
            with urllib.request.urlopen(r, timeout=REDIRECT_TIMEOUT) as resp:  # noqa: S310
                real_url = resp.geturl()
            break
        except Exception:  # noqa: BLE001
            continue
    return {"title": title, "url": real_url, "snippet": snippet or title}


def _build_snippet_map(grounding: dict) -> dict:
    """Map groundingChunk index -> concatenated supporting segment text so the
    agent's _rank() has something better than the bare title to score on."""
    idx_to_text: dict[int, list[str]] = {}
    for s in grounding.get("groundingSupports", []) or []:
        seg = ((s.get("segment") or {}).get("text") or "").strip()
        if not seg:
            continue
        for ci in s.get("groundingChunkIndices", []) or []:
            idx_to_text.setdefault(ci, []).append(seg)
    return {i: " ".join(txts)[:400] for i, txts in idx_to_text.items()}


# ─── Handler ─────────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    event = event or {}
    query = str(event.get("query") or "").strip()
    site = str(event.get("site") or "").strip()
    try:
        max_results = int(event.get("max_results") or DEFAULT_MAX_RESULTS)
    except (TypeError, ValueError):
        max_results = DEFAULT_MAX_RESULTS

    if not query:
        return {"results": [], "via": "vertex-error", "count": 0,
                "error": "no query provided"}

    # site scoping is a SOFT hint to Google Search grounding (it reformulates
    # its own queries), exactly like the managed tool — the agent keeps its
    # DOMAIN_FILTER_MODE=soft workaround downstream.
    search_query = query
    if site and f"site:{site}" not in query.lower():
        search_query = f"{query} site:{site}"

    try:
        creds, project_id = _get_credentials()
        data = _vertex_grounded_search(search_query, project_id, creds.token)
    except Exception as exc:  # noqa: BLE001
        print(f"[vertex] search failed: {type(exc).__name__}: {exc}")
        return {"results": [], "via": "vertex-error", "count": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    candidates = data.get("candidates") or [{}]
    grounding = (candidates[0].get("groundingMetadata") or {}) if candidates else {}
    chunks = grounding.get("groundingChunks") or []
    snippet_map = _build_snippet_map(grounding)

    items = [(chunk, snippet_map.get(i, "")) for i, chunk in enumerate(chunks)]

    results: list[dict] = []
    seen: set[str] = set()
    if items:
        workers = max(1, min(REDIRECT_WORKERS, len(items)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for res in pool.map(_resolve_one, items):
                if res and res.get("url") and res["url"] not in seen:
                    seen.add(res["url"])
                    results.append(res)

    print(f"[vertex] query={search_query!r} chunks={len(chunks)} "
          f"resolved={len(results)} model={VERTEX_MODEL_ID}")
    return {
        "results": results[:max_results],
        "via": "vertex-grounding",
        "count": len(results),
    }