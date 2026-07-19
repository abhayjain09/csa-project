"""Vertex AI grounded-search Lambda — ISOLATED Tier 2 discovery engine.

Replaces the AgentCore managed WebSearch tool as the candidate-URL generator
for the Report IQ / EDO Co-Analyst download agent.

Contracts (direct boto3 RequestResponse invoke from the AgentCore runtime):

  Document discovery:
  IN  : {"mode": "document_search", "query": "<text>",
         "site": "<domain or ''>", "max_results": <int>}
  OUT : {"results": [{"title","url","snippet"}, ...],
         "via": "vertex-grounding", "count": <int>}

  Company identity hinting:
  IN  : {"mode": "company_identity", "company_name": "<name>",
         "site": "<known domain or ''>"}
  OUT : {"identity_hint": {"legal_name","ticker","cik","official_domain",
                           "jurisdiction"},
         "results": [grounding sources...], "via": "vertex-grounding"}

Design rules that mirror the agent's own principles:
  * For document search, we NEVER return the model's generated answer text.
    Gemini can emit a hallucinated / reconstructed URL even with grounding on.
    The ONLY authoritative URL sources are the grounding chunks.
  * For company identity, generated identifiers are returned only as HINTS.
    The agent validates ticker/CIK/name convergence against SEC's official
    company_tickers.json before using them. An unvalidated Gemini identifier
    is never used for an EDGAR lookup or written as authoritative metadata.
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
import urllib.parse
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
IDENTITY_MAX_RESULTS = int(os.environ.get("IDENTITY_MAX_RESULTS", "8"))
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
        "generationConfig": {"temperature": 0, "maxOutputTokens": 512},
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=VERTEX_HTTP_TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


# ─── Redirect resolution (grounding chunks → real destination URLs) ──────────
def _is_unresolved_redirect(url: str) -> bool:
    """True while a URL is still a Vertex grounding redirect (not the real
    destination). These 403 when the agent tries to fetch them, carry no real
    domain for the agent's site-scoping, and only burn a candidate/sample slot
    downstream — so a redirect that never resolved must be DROPPED, not returned."""
    try:
        host = urllib.parse.urlparse(url or "").netloc.lower()
    except Exception:  # noqa: BLE001
        return True
    return host.endswith("vertexaisearch.cloud.google.com")


def _resolve_one(item: tuple) -> dict | None:
    """item = (chunk_dict, snippet_text). Resolve the vertexaisearch redirect
    to its REAL destination URL.

    Previously this fell back to returning the raw redirect when both HEAD and
    GET failed. That leaked unresolved vertexaisearch.cloud.google.com URLs to
    the agent, which then 403 on fetch (confirmed in prod: a leaked redirect was
    the only 'candidate' for an otherwise-findable document). We now DROP any
    chunk we cannot resolve to a non-redirect destination — fail closed, exactly
    like the agent does. Better to return one fewer candidate than a poisoned one."""
    chunk, snippet = item
    web = chunk.get("web", {}) if isinstance(chunk, dict) else {}
    redirect_uri = web.get("uri")
    title = web.get("title", "") or ""
    if not redirect_uri:
        return None

    real_url = None
    for method in ("HEAD", "GET"):
        try:
            r = urllib.request.Request(
                redirect_uri, method=method, headers={"User-Agent": UA})
            with urllib.request.urlopen(r, timeout=REDIRECT_TIMEOUT) as resp:  # noqa: S310
                candidate = resp.geturl()
            # geturl() can hand back the redirect unchanged (e.g. the endpoint
            # answered without a Location, or bounced to itself). Only accept a
            # URL that actually left the vertexaisearch redirect host.
            if candidate and not _is_unresolved_redirect(candidate):
                real_url = candidate
                break
        except Exception:  # noqa: BLE001
            continue

    if not real_url:
        print(f"[vertex] dropping unresolved grounding redirect (title={title!r})")
        return None
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


def _generated_text(data: dict) -> str:
    """Return Gemini's first text part. Used only for non-authoritative hints."""
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict)
    ).strip()


def _parse_first_json_object(text: str) -> dict:
    """Parse one JSON object from fenced or prose-wrapped model output."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    start = cleaned.find("{")
    if start < 0:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _clean_identity_hint(raw: dict) -> dict:
    """Keep only the bounded scalar fields allowed by the Lambda contract."""
    out = {}
    for key in ("legal_name", "ticker", "cik", "official_domain", "jurisdiction"):
        value = raw.get(key)
        if value is None:
            out[key] = None
            continue
        if not isinstance(value, (str, int)):
            out[key] = None
            continue
        text = str(value).strip()
        out[key] = text[:200] if text else None
    if out.get("ticker"):
        out["ticker"] = out["ticker"].upper()[:16]
    if out.get("cik"):
        digits = "".join(ch for ch in out["cik"] if ch.isdigit())
        out["cik"] = digits.zfill(10) if 1 <= len(digits) <= 10 else None
    if out.get("official_domain"):
        domain = out["official_domain"].lower()
        domain = domain.removeprefix("https://").removeprefix("http://")
        out["official_domain"] = domain.split("/", 1)[0].removeprefix("www.")
    if out.get("jurisdiction"):
        out["jurisdiction"] = out["jurisdiction"].lower()[:32]
    return out


def _identity_prompt(company_name: str, known_domain: str) -> str:
    return (
        "Use Google Search to identify the exact corporate identity of the "
        "company below. Prefer the company's official website and SEC.gov. "
        "Ticker and CIK are only for a US SEC registrant. Do not infer or guess "
        "an identifier; return null when sources do not support it. The CIK "
        "must contain digits only. Return ONLY one JSON object with exactly "
        "these keys: legal_name, ticker, cik, official_domain, jurisdiction.\n\n"
        f"Company supplied by user: {company_name}\n"
        f"Known official domain, if supplied: {known_domain or 'unknown'}"
    )


# ─── Handler ─────────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    event = event or {}
    mode = str(event.get("mode") or "document_search").strip().lower()
    query = str(event.get("query") or "").strip()
    site = str(event.get("site") or "").strip()
    try:
        max_results = int(event.get("max_results") or DEFAULT_MAX_RESULTS)
    except (TypeError, ValueError):
        max_results = DEFAULT_MAX_RESULTS

    company_name = str(event.get("company_name") or "").strip()
    if mode == "company_identity":
        query = _identity_prompt(company_name, site)
        max_results = min(max_results, IDENTITY_MAX_RESULTS)

    if not query or (mode == "company_identity" and not company_name):
        return {"results": [], "via": "vertex-error", "count": 0,
                "error": "no query/company_name provided"}

    # site scoping is a SOFT hint to Google Search grounding (it reformulates
    # its own queries), exactly like the managed tool. The agent independently
    # enforces its configured official-domain policy downstream.
    search_query = query
    if mode != "company_identity" and site and f"site:{site}" not in query.lower():
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
    response = {
        "results": results[:max_results],
        "via": "vertex-grounding",
        "count": len(results),
    }
    if mode == "company_identity":
        # This is deliberately named identity_hint. The caller must validate
        # it against SEC data before using ticker/CIK as authoritative values.
        response["identity_hint"] = _clean_identity_hint(
            _parse_first_json_object(_generated_text(data)))
    return response
