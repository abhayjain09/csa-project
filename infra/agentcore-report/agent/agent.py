"""Report download agent (single AgentCore Runtime) — v39 (AWS-only recall boost).

v39 builds on v38's Gateway search + deterministic AgentCore Browser DOM
fallback (Pattern B) and WIRES IN the following AWS-only recall boosters, all
of which still route every stored document through the SAME fail-closed class
verification (_llm_select_best + _confident) as v38 — maximizing recall never
means storing wrong documents:

  Phase 1  LLM-powered multi-query fan-out (_llm_generate_search_queries) plus a
           bounded parallel search fan-out (_parallel_web_search) wired into
           both _find_best_document and _invoke_sync Stage 1. Filing fallback
           broadened to all matched classes (FILING_FALLBACK_ALL_CLASSES).
  Phase 2  Sitemap enumeration (_harvest_sitemap / _sitemap_resolve) wired as
           the FIRST fallback tier when the primary search isn't confident.
  Phase 3  Browser deep-nav promoted to proactive IR-navigation discovery
           (_is_ir_nav_link + IR_NAV_KEYWORDS) so investor/report/policy
           sections are prioritized in the nav frontier.
  Phase 4  Ordered fallback chain (sitemap -> deep static crawl -> browser root
           nav), every tier gated by _make_browser_verify_fn.
  Phase 5  Relaxed budgets (runtime treated as unlimited; results over speed).

v38 legacy: when the static regex crawl finds no document link on an HTML
landing page (because the links are injected by JavaScript after load), the
page is rendered in AWS's managed headless Chrome over CDP and every <a href>
is read straight from the RENDERED DOM. No screenshots, no vision model, no
extra Bedrock/LLM tokens — it only consumes short AgentCore Browser compute.
Gated behind USE_BROWSER.
"""

import asyncio
import concurrent.futures
import datetime as dt
import random
import threading
import time
import hashlib
import json
import os
import re
import uuid
import unicodedata
from urllib.error import HTTPError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

# ─── Environment ──────────────────────────────────────────────────────────────
BUCKET            = os.environ.get("REPORTS_BUCKET", "")
PROVENANCE_TABLE  = os.environ.get("PROVENANCE_TABLE", "")
REGION            = os.environ.get("APP_REGION") or os.environ.get("AWS_REGION", "us-east-1")
GATEWAY_URL       = os.environ.get("GATEWAY_URL", "")
GATEWAY_SEARCH_TOOL = os.environ.get("GATEWAY_SEARCH_TOOL", "").strip()
GATEWAY_STRIP_SITE = os.environ.get("GATEWAY_STRIP_SITE", "true").lower() != "false"
MAXIMIZE_RECALL = os.environ.get("MAXIMIZE_RECALL", "true").lower() != "false"

DEEP_STATIC_CRAWL = os.environ.get("DEEP_STATIC_CRAWL", "true").lower() != "false"
DEEP_STATIC_MAX_DEPTH = int(os.environ.get("DEEP_STATIC_MAX_DEPTH", "3"))
DEEP_STATIC_MAX_PAGES = int(os.environ.get("DEEP_STATIC_MAX_PAGES", "100"))

BROWSER_DEEP_NAV = os.environ.get("BROWSER_DEEP_NAV", "true").lower() != "false"
BROWSER_NAV_MAX_DEPTH = int(os.environ.get("BROWSER_NAV_MAX_DEPTH", "2"))
BROWSER_NAV_MAX_PAGES = int(os.environ.get("BROWSER_NAV_MAX_PAGES", "60"))

USE_BROWSER       = os.environ.get("USE_BROWSER", "false").lower() == "true"
BROWSER_IDENTIFIER= os.environ.get("BROWSER_IDENTIFIER", "aws.browser.v1")
BROWSER_REGION    = os.environ.get("BROWSER_REGION") or REGION
BROWSER_SESSION_TIMEOUT = int(os.environ.get("BROWSER_SESSION_TIMEOUT_SECONDS", "120"))
BROWSER_WAIT_UNTIL = os.environ.get("BROWSER_WAIT_UNTIL", "domcontentloaded").strip()
BROWSER_SETTLE_MS = int(os.environ.get("BROWSER_SETTLE_MS", "2500"))

BROWSER_ALLOW_OFFDOMAIN_DOCS = os.environ.get("BROWSER_ALLOW_OFFDOMAIN_DOCS", "true").lower() != "false"
BROWSER_DOWNLOAD_IN_SESSION  = os.environ.get("BROWSER_DOWNLOAD_IN_SESSION", "true").lower() != "false"
BROWSER_CLICK_FALLBACK       = os.environ.get("BROWSER_CLICK_FALLBACK", "true").lower() != "false"
BROWSER_CLICK_TIMEOUT_MS     = int(os.environ.get("BROWSER_CLICK_TIMEOUT_MS", "10000"))
BROWSER_MAX_DOC_BYTES        = int(os.environ.get("BROWSER_MAX_DOC_BYTES", str(80 * 1024 * 1024)))
BROWSER_MAX_DOC_CANDIDATES   = int(os.environ.get("BROWSER_MAX_DOC_CANDIDATES", "20"))
BROWSER_MAX_VERIFY_CANDIDATES = int(os.environ.get("BROWSER_MAX_VERIFY_CANDIDATES", "30"))
BROWSER_MAX_CLICK_ATTEMPTS = int(os.environ.get("BROWSER_MAX_CLICK_ATTEMPTS", "12"))
BROWSER_RESOLVE_MAX_SECONDS = float(os.environ.get("BROWSER_RESOLVE_MAX_SECONDS", "1800"))
BROWSER_VERIFY_CLASS = os.environ.get("BROWSER_VERIFY_CLASS", "true").lower() != "false"
BROWSER_VISION_MODEL_ID = os.environ.get("BROWSER_VISION_MODEL_ID", "").strip()
BROWSER_SKIP_CLICK_ON_BLOCK = os.environ.get("BROWSER_SKIP_CLICK_ON_BLOCK", "true").lower() != "false"

MAX_RESULTS       = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "10"))
BEST_MATCHES      = int(os.environ.get("BEST_MATCHES", "1"))
DOC_ONLY          = os.environ.get("DOC_ONLY", "true").lower() != "false"
CURRENT_YEAR      = dt.date.today().year
ENFORCE_SITE_DOMAIN = os.environ.get("ENFORCE_SITE_DOMAIN", "true").lower() != "false"
DOMAIN_FILTER_MODE = os.environ.get("DOMAIN_FILTER_MODE", "soft").strip().lower()

ENABLE_FILING_FALLBACK = os.environ.get("ENABLE_FILING_FALLBACK", "true").lower() != "false"
FILING_FALLBACK_DOC_CLASSES = {
    c.strip().lower()
    for c in os.environ.get(
        "FILING_FALLBACK_DOC_CLASSES",
        "annual report,proxy statement,remuneration report,tax strategy and governance",
    ).split(",")
    if c.strip()
}
_NON_FILING_HUB_CLASSES = {
    c.strip().lower()
    for c in os.environ.get(
        "NON_FILING_HUB_DOC_CLASSES", "tax strategy and governance",
    ).split(",")
    if c.strip()
}
LLM_MODEL_ID      = os.environ.get("LLM_MODEL_ID", "")
MAX_ALIAS_SEARCHES = int(os.environ.get("MAX_ALIAS_SEARCHES", "3"))
SEARCH_ALL_ALIASES = os.environ.get("SEARCH_ALL_ALIASES", "true").lower() != "false"
ALIAS_REGION = os.environ.get("ALIAS_REGION", "all").strip().lower()
ALIAS_MODE = os.environ.get("ALIAS_MODE", "fallback").strip().lower()
TOP_N_FOR_LLM     = int(os.environ.get("TOP_N_FOR_LLM", "6"))
ALIAS_HIT_BOOST   = int(os.environ.get("ALIAS_HIT_BOOST", "1"))
ENABLE_LLM_CLASS_MATCH = os.environ.get("ENABLE_LLM_CLASS_MATCH", "true").lower() != "false"

_bedrock = boto3.client("bedrock-runtime", region_name=REGION) if LLM_MODEL_ID else None
_s3      = boto3.client("s3", region_name=REGION) if BUCKET else None
_table   = boto3.resource("dynamodb", region_name=REGION).Table(PROVENANCE_TABLE) if PROVENANCE_TABLE else None

LLM_SEND_TEMPERATURE = os.environ.get("LLM_SEND_TEMPERATURE", "false").lower() == "true"
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))
SELECTION_MODEL_ID = os.environ.get("SELECTION_MODEL_ID", "").strip() or None

# ═══ v39 env (recall boosters, all AWS-only) ═══
ENABLE_LLM_QUERY_GEN = os.environ.get("ENABLE_LLM_QUERY_GEN", "true").lower() != "false"
LLM_QUERY_GEN_MAX = int(os.environ.get("LLM_QUERY_GEN_MAX", "8"))
SEARCH_FANOUT_WORKERS = int(os.environ.get("SEARCH_FANOUT_WORKERS", "4"))
ENABLE_SITEMAP = os.environ.get("ENABLE_SITEMAP", "true").lower() != "false"
SITEMAP_MAX_URLS = int(os.environ.get("SITEMAP_MAX_URLS", "5000"))
SITEMAP_MAX_NESTED = int(os.environ.get("SITEMAP_MAX_NESTED", "50"))
SITEMAP_FETCH_TIMEOUT = int(os.environ.get("SITEMAP_FETCH_TIMEOUT", "20"))
SITEMAP_MAX_CANDIDATES = int(os.environ.get("SITEMAP_MAX_CANDIDATES", "40"))
FILING_FALLBACK_ALL_CLASSES = os.environ.get("FILING_FALLBACK_ALL_CLASSES", "true").lower() != "false"
FILING_REGISTRY_HOSTS = [
    h.strip().lower() for h in os.environ.get(
        "FILING_REGISTRY_HOSTS",
        "sec.gov,bseindia.com,nseindia.com,archives.nseindia.com,"
        "nsearchives.nseindia.com,annualreports.com,companieshouse.gov.uk,"
        "asx.com.au,sedar.com,sedarplus.ca").split(",") if h.strip()]
IR_NAV_KEYWORDS = tuple(
    k.strip().lower() for k in os.environ.get(
        "IR_NAV_KEYWORDS",
        "investor,investors,annual-report,annualreport,financial,financials,"
        "results,sustainability,esg,governance,policy,policies,code-of-conduct,"
        "ethics,compliance,reports,disclosures,shareholder,filings").split(",")
    if k.strip())
_LLM_QUERY_GEN_CACHE = {}


# ── Model circuit breaker ──────────────────────────────────────────────────
_MODEL_BREAKER: dict[str, float] = {}
_MODEL_BREAKER_LOCK = threading.Lock()
MODEL_BREAKER_COOLDOWN_SECONDS = int(os.environ.get("MODEL_BREAKER_COOLDOWN_SECONDS", "300"))


def _trip_model_breaker(model_id: str) -> None:
    with _MODEL_BREAKER_LOCK:
        _MODEL_BREAKER[model_id] = time.monotonic()
    print(f"[breaker] tripped for model {model_id!r} — an account-access "
          f"failure (not a transient error) was hit; skipping retries on this "
          f"model for the next {MODEL_BREAKER_COOLDOWN_SECONDS}s rather than "
          f"repeating the same doomed call on every remaining query")


def _model_breaker_tripped(model_id: str) -> bool:
    with _MODEL_BREAKER_LOCK:
        ts = _MODEL_BREAKER.get(model_id)
    return ts is not None and (time.monotonic() - ts) < MODEL_BREAKER_COOLDOWN_SECONDS


def _converse(prompt: str, max_tokens: int, model_id: str | None = None) -> str:
    if _bedrock is None:
        raise RuntimeError("bedrock client not configured")
    mid = model_id or LLM_MODEL_ID
    if _model_breaker_tripped(mid):
        raise RuntimeError(
            f"model breaker open for {mid} (recent account-access failure; "
            f"will retry automatically after {MODEL_BREAKER_COOLDOWN_SECONDS}s)")
    infcfg: dict = {"maxTokens": max_tokens}
    if LLM_SEND_TEMPERATURE:
        infcfg["temperature"] = 0
    last_exc = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            resp = _bedrock.converse(
                modelId=mid,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig=infcfg,
            )
            return "".join(b.get("text", "")
                           for b in resp["output"]["message"]["content"]).strip()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            name = type(exc).__name__
            if "ValidationException" in name or "Validation" in str(exc):
                raise
            if "ResourceNotFoundException" in name or "AccessDeniedException" in name:
                _trip_model_breaker(mid)
                raise
            if attempt < LLM_MAX_RETRIES - 1:
                time.sleep(0.6 * (2 ** attempt) + random.uniform(0.0, 0.4))
    raise last_exc  # type: ignore[misc]


def _parse_llm_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start > 0:
        text = text[start:]
    return json.JSONDecoder().raw_decode(text)[0]


UA = "EDO-CoAnalyst/1.0 (+compliance-research)"

SEARCH_MIN_INTERVAL = float(os.environ.get("SEARCH_MIN_INTERVAL", "1.5"))
SEARCH_MAX_RETRIES  = int(os.environ.get("SEARCH_MAX_RETRIES", "4"))
_search_lock = threading.Lock()
_last_search_ts = [0.0]


def _throttle() -> None:
    """Block until at least SEARCH_MIN_INTERVAL has passed since the last call."""
    with _search_lock:
        now = time.monotonic()
        wait = SEARCH_MIN_INTERVAL - (now - _last_search_ts[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.0, 0.4))
        _last_search_ts[0] = time.monotonic()
_DOC_CLASS_MATCH_CACHE: dict[str, list[str]] = {}


# ─── Document class rules (canonical → aliases + near-match exclusions) ───────
_DOC_CLASS_RULES: dict[str, dict] = {
    "annual report": {
        "aliases_by_region": {
            "uk_europe": [
                "annual report and accounts",
                "universal registration document",
                "integrated annual report",
            ],
            "india": [
                "integrated annual report",
                "annual report",
            ],
            "us": [
                "annual report",
                "integrated annual report",
            ],
        },
        "reject": [],
    },
    "sustainability report": {
        "aliases_by_region": {
            "uk_europe": [
                "sustainability report",
                "csrd report",
                "esrs report",
                "sustainability statement",
                "esg report",
                "impact report",
            ],
            "india": [
                "business responsibility and sustainability reporting",
                "sustainability report",
                "esg report",
            ],
            "us": [
                "sustainability report",
                "esg report",
                "impact report",
            ],
        },
        "reject": [],
    },
    "code of conduct": {
        "aliases_by_region": {
            "uk_europe": [
                "code of conduct",
                "code of business conduct and ethics",
                "code of conduct for board of employees, directors and senior management",
                "code of ethics",
                "code of fair disclosure and conduct",
                "employee code of conduct",
            ],
            "india": [
                "code of conduct",
                "code of business conduct and ethics",
                "code of conduct for board of employees, directors and senior management",
                "code of ethics",
                "code of fair disclosure and conduct",
                "employee code of conduct",
            ],
            "us": [
                "code of conduct",
                "code of business conduct and ethics",
                "code of conduct for board of employees, directors and senior management",
                "code of ethics",
                "code of fair disclosure and conduct",
                "employee code of conduct",
            ],
        },
        "reject": [
            "supplier code of conduct",
            "vendor code of conduct",
            "third-party code of conduct",
            "supply chain code of conduct",
        ],
    },
    "supplier code of conduct": {
        "aliases_by_region": {
            "uk_europe": [
                "supplier code of conduct",
                "vendor code of conduct",
                "third-party code of conduct",
                "business partner code of conduct",
                "supply chain code of conduct",
                "sustainable supply chain policy",
                "responsible sourcing policy",
                "supplier charter",
                "procurement code of conduct",
                "supplier esg policy",
            ],
            "india": [
                "supplier code of conduct",
                "vendor code of conduct",
                "third-party code of conduct",
                "business partner code of conduct",
                "supply chain code of conduct",
                "sustainable supply chain policy",
                "responsible sourcing policy",
                "supplier charter",
                "procurement code of conduct",
                "supplier esg policy",
            ],
            "us": [
                "supplier code of conduct",
                "vendor code of conduct",
                "third-party code of conduct",
                "business partner code of conduct",
                "supply chain code of conduct",
                "sustainable supply chain policy",
                "responsible sourcing policy",
                "supplier charter",
                "procurement code of conduct",
                "supplier esg policy",
            ],
        },
        "reject": [],
    },
    "tax strategy and governance": {
        "aliases_by_region": {
            "uk_europe": [
                "tax strategy",
                "tax policy",
                "tax governance policy",
                "transfer pricing policy",
                "related party transaction policy",
            ],
            "india": [
                "tax policy",
            ],
            "us": [
                "tax strategy",
                "tax policy",
                "tax governance policy",
                "transfer pricing policy",
                "related party transaction policy",
            ],
        },
        "reject": [],
    },
    "whistleblowing mechanism": {
        "aliases_by_region": {
            "uk_europe": [
                "whistleblowing policy",
                "speak up policy",
                "ethics hotline policy",
                "grievance & whistleblowing policy",
                "whistleblower policy",
                "ethics hotline",
            ],
            "india": [
                "whistleblowing policy",
                "speak up policy",
                "whistleblower policy",
                "ethics hotline",
            ],
            "us": [
                "whistleblowing policy",
                "speak up policy",
                "whistleblower policy",
                "ethics hotline",
            ],
        },
        "reject": [],
    },
    "occupational health & safety policy": {
        "aliases_by_region": {
            "uk_europe": [
                "health and safety policy",
                "occupational health and safety policy",
                "ohs policy",
                "workplace health and safety policy",
                "health, safety and wellbeing policy",
                "hsse policy",
                "safety policy statement",
            ],
            "india": [
                "health and safety policy",
                "occupational health and safety policy",
                "ohs policy",
                "she policy",
                "qhse policy",
                "hsse policy",
            ],
            "us": [
                "health and safety policy",
                "occupational health and safety policy",
                "ohs policy",
                "she policy",
                "health, safety and wellbeing policy",
                "hsse policy",
            ],
        },
        "reject": [],
    },
    "environmental policy": {
        "aliases_by_region": {
            "uk_europe": [
                "environmental management policy",
                "sustainability policy",
                "climate change policy",
                "climate action policy",
            ],
            "india": [
                "environmental management policy",
                "sustainability policy",
            ],
            "us": [
                "environmental management policy",
                "sustainability policy",
                "climate change policy",
                "climate action policy",
            ],
        },
        "reject": [],
    },
    "anti-bribery and corruption policy": {
        "aliases_by_region": {
            "uk_europe": [
                "anti-bribery and corruption policy",
                "anti-bribery policy",
                "anti-corruption policy",
                "anti-bribery and anti-corruption policy",
                "abc policy",
            ],
            "india": [
                "anti-bribery and corruption policy",
                "anti-bribery policy",
                "anti-corruption policy",
                "policy on prevention of bribery and corruption",
            ],
            "us": [
                "anti-bribery and corruption policy",
                "anti-corruption policy",
                "foreign corrupt practices act policy",
                "abc policy",
            ],
        },
        "reject": [],
    },
}


def _normalize_alias_region(region: str) -> str:
    value = (region or "all").strip().lower().replace("-", "_").replace(" ", "_")
    if value in {"uk", "europe", "uk_and_europe", "uk_europe"}:
        return "uk_europe"
    if value in {"india", "indian"}:
        return "india"
    if value in {"us", "usa", "american"}:
        return "us"
    return "all"


_TLD_REGION_MAP: dict[str, str] = {
    "uk": "uk_europe", "co.uk": "uk_europe", "ie": "uk_europe",
    "de": "uk_europe", "fr": "uk_europe", "nl": "uk_europe", "es": "uk_europe",
    "it": "uk_europe", "se": "uk_europe", "no": "uk_europe", "dk": "uk_europe",
    "fi": "uk_europe", "pl": "uk_europe", "ch": "uk_europe", "at": "uk_europe",
    "be": "uk_europe", "pt": "uk_europe", "eu": "uk_europe", "lu": "uk_europe",
    "gr": "uk_europe", "cz": "uk_europe", "hu": "uk_europe", "ro": "uk_europe",
    "in": "india", "co.in": "india",
    "us": "us",
}


def _infer_alias_region_from_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    parts = [p for p in domain.lower().split(".") if p]
    if len(parts) >= 2:
        two_label = ".".join(parts[-2:])
        if two_label in _TLD_REGION_MAP:
            return _TLD_REGION_MAP[two_label]
    if parts:
        tld = parts[-1]
        if tld in _TLD_REGION_MAP:
            return _TLD_REGION_MAP[tld]
    return None


def _aliases_for_rule(rule: dict, region: str = "all") -> list[str]:
    by_region = rule.get("aliases_by_region", {})
    norm = _normalize_alias_region(region)
    if norm in {"uk_europe", "india", "us"}:
        return list(dict.fromkeys(by_region.get(norm, [])))

    out: list[str] = []
    for key in ("uk_europe", "india", "us"):
        for alias in by_region.get(key, []):
            if alias not in out:
                out.append(alias)
    return out


def _matches_doc_class(text: str, canonical: str, aliases: list[str]) -> bool:
    low = text.lower()
    return canonical in low or any(a in low for a in aliases)


def _llm_match_doc_classes(query: str) -> list[str]:
    """Map noisy/misspelled query text to known canonical document classes."""
    if _bedrock is None or not ENABLE_LLM_CLASS_MATCH:
        return []

    key = (query or "").strip().lower()
    if key in _DOC_CLASS_MATCH_CACHE:
        return _DOC_CLASS_MATCH_CACHE[key]

    canonical_names = list(_DOC_CLASS_RULES.keys())
    prompt = (
        "You are a document-class matcher. "
        "Given a noisy query, pick zero or more best matching canonical classes.\n"
        "Rules:\n"
        "- Handle spelling errors and punctuation variants.\n"
        "- Match intent conservatively; do not guess unrelated classes.\n"
        "- Return only classes from the provided canonical list.\n"
        "- If no confident match, return an empty list.\n\n"
        f"Canonical classes: {json.dumps(canonical_names, ensure_ascii=True)}\n"
        f"Query: {query}\n\n"
        "Output ONLY valid JSON object: {\"matched\": [\"<canonical>\", ...]}"
    )
    try:
        text = _converse(prompt, max_tokens=120)
        obj = _parse_llm_json(text)
        matched = obj.get("matched", []) if isinstance(obj, dict) else []
        allowed = set(canonical_names)
        out = [name for name in matched if isinstance(name, str) and name in allowed]
        _DOC_CLASS_MATCH_CACHE[key] = list(dict.fromkeys(out))
        return _DOC_CLASS_MATCH_CACHE[key]
    except Exception as exc:  # noqa: BLE001
        print(f"[llm] class match failed ({exc}); using deterministic class match only")
        _DOC_CLASS_MATCH_CACHE[key] = []
        return []


def _matched_doc_classes(query: str) -> list[tuple[str, dict]]:
    low = (query or "").lower()
    deterministic = [
        (canonical, rule)
        for canonical, rule in _DOC_CLASS_RULES.items()
        if _matches_doc_class(query, canonical, _aliases_for_rule(rule, "all"))
    ]

    matched_names = {c for c, _ in deterministic}
    pruned = []
    for canonical, rule in deterministic:
        superseded = any(
            other != canonical and canonical in other and other in low
            for other in matched_names
        )
        if not superseded:
            pruned.append((canonical, rule))

    out = list(pruned)
    seen = {c for c, _ in pruned}
    for canonical in _llm_match_doc_classes(query):
        if canonical in seen:
            continue
        rule = _DOC_CLASS_RULES.get(canonical)
        if rule:
            out.append((canonical, rule))
            seen.add(canonical)
    return out


def _filtered_doc_rules(query: str) -> dict[str, dict]:
    matches = _matched_doc_classes(query)
    return {canonical: rule for canonical, rule in matches}


def _doc_rules_summary(query: str, region: str | None = None) -> str:
    matches = _matched_doc_classes(query)
    if not matches:
        return (
            "No explicit canonical document class matched. Infer the closest class, "
            "accept clear regional naming variants, reject clearly different document types."
        )
    parts = []
    active_region = _normalize_alias_region(region if region is not None else ALIAS_REGION)
    for canonical, rule in matches:
        active_aliases = _aliases_for_rule(rule, active_region)
        alias_txt  = ", ".join(active_aliases) if active_aliases else "none"
        reject_txt = ", ".join(rule["reject"])  if rule["reject"]  else "none"
        parts.append(
            f"Canonical class: {canonical}. "
            f"Alias region: {active_region}. "
            f"Accepted aliases: {alias_txt}. "
            f"Near-match exclusions (return NO for these): {reject_txt}."
        )
    return " ".join(parts)


def _alias_queries(original_query: str, region: str | None = None) -> list[str]:
    low = original_query.lower()
    alt_queries: list[str] = []
    active_region = _normalize_alias_region(region if region is not None else ALIAS_REGION)
    matched_classes = _matched_doc_classes(original_query)
    if not matched_classes:
        return alt_queries

    for canonical, rule in matched_classes:
        source_phrase = canonical if canonical in low else ""
        if not source_phrase:
            for a in _aliases_for_rule(rule, "all"):
                if a in low:
                    source_phrase = a
                    break
        targets = [canonical] + list(_aliases_for_rule(rule, active_region))
        if not source_phrase:
            for target in targets:
                if target in low:
                    continue
                alt = f"{original_query.strip()} {target}".strip()
                if alt != original_query and alt not in alt_queries:
                    alt_queries.append(alt)
            continue

        for target in targets:
            if target in low:
                continue
            alt = re.sub(re.escape(source_phrase), target, original_query, flags=re.I)
            if alt != original_query and alt not in alt_queries:
                alt_queries.append(alt)

    if SEARCH_ALL_ALIASES:
        return alt_queries
    return alt_queries[:MAX_ALIAS_SEARCHES]


# ─── Basic helpers ────────────────────────────────────────────────────────────
def _domain(query: str) -> str | None:
    m = re.search(r"site:\s*(\S+)", query or "")
    if not m:
        return None
    raw = _demarkdown(m.group(1))
    raw = raw.strip("[]() ")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).netloc.lower()
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    return host[4:] if host.startswith("www.") else host


def _strip_site(q: str) -> str:
    return re.sub(r"site:\s*\S+", "", q or "").strip()


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "unknown")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"

def _registrable(host: str) -> str:
    """Best-effort eTLD+1 without external deps. Good enough for corp domains."""
    host = (host or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    two_label_tlds = {"co.uk", "com.au", "co.in", "co.jp", "com.br", "co.za",
                      "com.mx", "co.nz", "com.sg", "com.hk", "co.kr"}
    if ".".join(parts[-2:]) in two_label_tlds:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host_matches(url: str, domain: str) -> bool:
    h = _registrable(urlparse(url).netloc)
    d = _registrable(domain)
    if not h or not d:
        return False
    return h == d or h.endswith("." + d) or d.endswith("." + h)


def _resolve_relative_years(q: str) -> str:
    y = CURRENT_YEAR

    def _cy(m):
        sign = (m.group(1) or "").strip()
        num  = int(m.group(2)) if m.group(2) else 0
        if sign in ("-", "\u2013", "\u2014"):
            return str(y - num)
        if sign == "+":
            return str(y + num)
        return str(y)

    q = re.sub(r"current\s*year\s*([-+\u2013\u2014])?\s*(\d+)?", _cy, q, flags=re.I)
    q = re.sub(r"\{\s*year\s*-\s*(\d+)\s*\}", lambda m: str(y - int(m.group(1))), q, flags=re.I)
    q = re.sub(r"\{\s*year\s*\+\s*(\d+)\s*\}", lambda m: str(y + int(m.group(1))), q, flags=re.I)
    q = re.sub(r"\{\s*year\s*\}", str(y), q, flags=re.I)
    return q


def _demarkdown(q: str) -> str:
    if "[" not in q and "]" not in q:
        return q
    q = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]*\)", r"\1", q)
    for ch in "[]()":
        q = q.replace(ch, " ")
    return re.sub(r"\s+", " ", q).strip()


def _clean_query(q: str) -> str:
    q = _demarkdown(q)
    q = q.replace("(", " ").replace(")", " ")
    q = re.sub(r"\s*\+\s*", " ", q)
    return re.sub(r"\s+", " ", q).strip()


_YEAR_RE = r"(?<!\d)(20\d{2})(?!\d)"
_YY_OR_YYYY_RE = r"(?<!\d)(\d{2}|\d{4})(?!\d)"


def _extract_year_intent(text: str) -> set[int]:
    out: set[int] = set()
    t = (text or "").lower()
    for y in re.findall(_YEAR_RE, t):
        out.add(int(y))
    for yy in re.findall(rf"\bfy\s*{_YY_OR_YYYY_RE}", t):
        y = int(yy)
        out.add(y if y >= 1000 else 2000 + y)
    for a, b in re.findall(rf"\bfy\s*{_YY_OR_YYYY_RE}\s*[-/]\s*{_YY_OR_YYYY_RE}", t):
        ya = int(a) if len(a) == 4 else 2000 + int(a)
        yb = int(b) if len(b) == 4 else 2000 + int(b)
        if abs(yb - ya) <= 1:
            out.update({ya, yb})
    for a, b in re.findall(rf"{_YEAR_RE}\s*[-/]\s*{_YY_OR_YYYY_RE}", t):
        ya = int(a)
        yb = int(b) if len(b) == 4 else (ya // 100) * 100 + int(b)
        if abs(yb - ya) <= 1:
            out.update({ya, yb})
    return out


def _year_alignment_score(query: str, candidate_text: str) -> int:
    qy = _extract_year_intent(query)
    if not qy:
        # v39.1 (log-evidence fix): an UNDATED query ("Annual Report" with no
        # year named) previously got a flat 0 here regardless of candidate
        # year — 2016 through 2026 all ranked as equally valid, so whichever
        # stale report the search happened to surface first could win purely
        # by luck of result ordering (observed: a 2022 annual report stored
        # instead of the current one, no year ever named in the query). Give
        # a small, capped recency bonus so a more recent candidate ranks
        # ahead of an older one when nothing in the query forces a specific
        # year. A candidate with NO year in its filename/text (the normal
        # case for undated policy documents) still gets 0 — this only
        # affects candidates that DO carry a year, i.e. dated reports/filings.
        cy = _extract_year_intent(candidate_text)
        if not cy:
            return 0
        return max(0, min(max(cy) - (CURRENT_YEAR - 10), 10))
    cy = _extract_year_intent(candidate_text)
    if not cy:
        return 0
    return 6 if qy.intersection(cy) else -6


# ─── LLM helpers ──────────────────────────────────────────────────────────────
def _llm_rewrite(q: str) -> str:
    if _bedrock is None:
        return q
    filtered_rules = _filtered_doc_rules(q)
    prompt = (
        f"Today is {dt.date.today().isoformat()} (current year {CURRENT_YEAR}).\n"
        "Rewrite the text below into ONE concise web-search query to find the official "
        "company document. Resolve any relative year to an actual year. "
        "Keep any 'site:' operator and URL intact.\n"
        "Year rule: if a year or fiscal year is present, preserve it exactly. "
        "If no year is present, do NOT invent one.\n"
        "International naming rule: preserve topic intent while allowing accepted aliases "
        "but do not broaden into neighboring document types.\n"
        "Do NOT wrap phrases in double quotes. Do NOT add words that are not in the input.\n"
        "Do NOT format the domain or URL as a Markdown link (no [text](url) syntax, no "
        "square brackets, no parentheses around the URL). Output the domain as plain "
        "text exactly as given, e.g. 'site:www.example.com', never "
        "'site:[www.example.com](https://www.example.com)'.\n"
        + (f"Filtered document class rules (query-scoped): {json.dumps(filtered_rules, ensure_ascii=True)}\n" if filtered_rules else "")
        + "Output ONLY the final query string, no quotes, brackets, or extra words.\n\n"
        f"Text: {q}"
    )
    try:
        text = _converse(prompt, max_tokens=150)
        text = text.replace('"', "").strip()
        text = _demarkdown(text)
        return text or q
    except Exception as exc:  # noqa: BLE001
        print(f"[llm] rewrite failed ({exc}); using deterministic query")
        return q


def _llm_select_best(query: str, candidates: list[dict], company: str = "") -> dict:
    if _bedrock is None or not candidates:
        return {
            "selected_url": candidates[0]["url"] if candidates else None,
            "year_match": True, "topic_match": True, "company_match": True,
            "confidence": "low", "reason": "llm-off",
        }

    lines = []
    for i, c in enumerate(candidates[:TOP_N_FOR_LLM], 1):
        lines.append(
            f"{i}. URL: {c['url']}\n"
            f"   Filename: {c['filename']}\n"
            f"   Content-Type: {c['head_ctype']}\n"
            f"   Content sample: {c['content_sample'][:400]}"
        )
    candidates_text = "\n".join(lines)
    filtered_rules = _filtered_doc_rules(query)
    target_years = _extract_year_intent(query)

    if target_years:
        year_rule = (
            f"- Year rule: the target year(s) are {sorted(target_years)}. The chosen "
            "candidate MUST show one of these years in its filename or content sample. "
            "If none match, set selected_url to null and year_match to false.\n"
        )
    else:
        year_rule = (
            "- Year rule: NO specific year was requested. NEVER reject a "
            "candidate for lacking a year, and always set year_match to true "
            "regardless of which year is chosen. However, among candidates "
            "that are otherwise equally strong CLASS matches: if this is a "
            "normally-dated/recurring document (an Annual Report, Proxy "
            "Statement, Sustainability Report, or similar periodic filing), "
            "PREFER the one showing the MOST RECENT year in its filename or "
            "content — do not settle for an old year (e.g. 2020-2022) when a "
            "newer one is present among the candidates just because it "
            "happened to rank first. If this is a policy or other typically "
            "undated document, year is irrelevant — ignore it entirely.\n"
        )

    # v39.1 (log-evidence fix): a real run stored an S.C. Johnson tax-strategy
    # PDF under the Johnson & Johnson corpus — DOMAIN_FILTER_MODE=soft lets an
    # off-domain candidate reach this call, and this prompt previously judged
    # document CLASS only, never document OWNER, so a genuinely-a-tax-
    # strategy-document candidate for the WRONG COMPANY sailed through. Only
    # emitted when the caller has a real company name (not "unknown").
    if company and company.lower() != "unknown":
        company_rule = (
            f"- Company rule: the candidate document must belong to "
            f"'{company}' specifically. If the document is clearly about a "
            f"DIFFERENT company — even one with a similar, overlapping, or "
            f"confusable name (e.g. a same-surname but unrelated company) — "
            f"reject it: set selected_url to null, topic_match to false, and "
            f"company_match to false. Do not accept a document merely "
            f"because it appeared among this company's search results; "
            f"verify the document itself states or clearly implies it "
            f"belongs to '{company}'.\n"
        )
    else:
        company_rule = (
            "- Company rule: no specific company was provided to check "
            "against. Always set company_match to true.\n"
        )

    prompt = (
        f"Query: {query}\n"
        + (f"Company: {company}\n" if company and company.lower() != "unknown" else "")
        + f"Target year(s): {sorted(target_years) if target_years else 'none (ignore year)'}\n\n"
        f"Candidates:\n{candidates_text}\n\n"
        + (f"Filtered document class rules (query-scoped): {json.dumps(filtered_rules, ensure_ascii=True)}\n" if filtered_rules else "")
        + "\nSelect the single candidate that IS the exact document class named in the "
        "Query. Judge the document CLASS, not mere keyword overlap. Sharing a word "
        "(e.g. 'conduct', 'report') is NOT a match.\n"
        "Rules:\n"
        + year_rule
        + company_rule
        + "- Class rule: the candidate must BE the requested class. Reject a "
        "near-neighbor even when it shares words. WRONG matches to reject: a Board's "
        "Report or Directors' Report is NOT an Annual Report and NOT a Sustainability "
        "Report; a 'code of conduct for non-executive / independent directors' is NOT "
        "the company's general 'code of conduct'; the general 'code of conduct' is NOT "
        "a 'supplier code of conduct'; a governance / ethics / index / overview page is "
        "NOT the policy document itself; a Supplier/Vendor Code of Conduct is NOT a "
        "Conflicts of Interest Policy, Anti-Corruption Policy, Whistleblowing Policy, "
        "or any other named policy merely because it discusses that topic in a "
        "section — a document is only a match if it IS the named policy, not if it "
        "MENTIONS the named policy's subject matter. A Strategic Report, an "
        "Annual Report, an ESG Update, an ESG Supplement, an ESG Factbook, a "
        "green/SDG bond report, a CDP score report, or an assurance report is "
        "NOT a standalone Sustainability Report, GHG Emission Report, Impact "
        "Report, or Environment Policy — reject it unless its filename or title "
        "explicitly names the exact requested class.\n"
        "- Accepted aliases from the class rules ARE equivalent and acceptable; the "
        "listed near-match exclusions are NOT acceptable.\n"
        "- Strongly prefer an actual document file (PDF/DOC) over an HTML landing, "
        "index, or overview page. Pick an HTML page ONLY if no document file for the "
        "requested class is present among the candidates.\n"
        "- If NONE of the candidates is genuinely the requested class, set selected_url "
        "to null and topic_match to false. Do NOT settle for the closest available "
        "document — a correct 'no match' is better than a wrong document.\n"
        "- If several genuinely match, prefer the most explicit title/filename match.\n\n"
        "Respond with ONLY valid JSON (no markdown):\n"
        '{"selected_url": "<url or null>", "year_match": true/false, '
        '"topic_match": true/false, "company_match": true/false, '
        '"confidence": "high/medium/low", "reason": "<max 15 words>"}'
    )
    try:
        text = _converse(prompt, max_tokens=200, model_id=SELECTION_MODEL_ID)
        decision = _parse_llm_json(text)
        print(f"[llm] select decision: selected={decision.get('selected_url')} "
              f"topic={decision.get('topic_match')} company={decision.get('company_match')} "
              f"year={decision.get('year_match')} "
              f"conf={decision.get('confidence')} reason={decision.get('reason')}")
        return decision
    except Exception as exc:  # noqa: BLE001
        print(f"[llm] grouped selection failed ({exc}); FAILING CLOSED (no selection)")
        return {
            "selected_url": None,
            "year_match": False, "topic_match": False, "company_match": False,
            "confidence": "low", "reason": f"llm-error({type(exc).__name__})-failed-closed",
        }


def _prepare_query(q: str) -> str:
    """Year resolution + cleanup + optional LLM rewrite. Query stays focused."""
    q = _clean_query(_resolve_relative_years(q.strip()))
    if _bedrock is not None:
        q = _llm_rewrite(q) or q
    q = _demarkdown(q)
    return q.strip()


# ─── HEAD pre-filter ─────────────────────────────────────────────────────────
_DOC_EXTS = (".pdf", ".doc", ".docx", ".rtf")


def _head_check(url: str) -> dict:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    filename = unquote(urlparse(url).path).rsplit("/", 1)[-1] or ""
    ctype = ""
    try:
        req = Request(url, headers=headers, method="HEAD")
        with urlopen(req, timeout=10) as r:  # noqa: S310
            ctype = r.headers.get("Content-Type", "").split(";")[0].lower()
            cd = r.headers.get("Content-Disposition", "")
            fn_m = re.search(r'filename=["\']?([^"\';\s]+)', cd)
            if fn_m:
                filename = fn_m.group(1)
    except Exception:  # noqa: BLE001
        pass

    ok = (
        any(filename.lower().endswith(e) for e in _DOC_EXTS)
        or "pdf" in ctype
        or "msword" in ctype
        or "officedocument" in ctype
        or "html" in ctype
        or ctype == ""
    )
    return {"url": url, "filename": filename, "head_ctype": ctype, "ok": ok}


# ─── Search: AgentCore Gateway managed WebSearch tool ONLY (via MCP) ──────────
def _sigv4_auth():
    import httpx
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    creds = boto3.Session().get_credentials()

    class _Auth(httpx.Auth):
        requires_request_body = True

        def auth_flow(self, request):
            aws_req = AWSRequest(
                method=request.method, url=str(request.url),
                data=request.content, headers=dict(request.headers),
            )
            SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_req)
            request.headers.update(dict(aws_req.headers))
            yield request
    return _Auth()


_GW_DEBUG: dict = {}


def _pick_search_tool(names: list[str]) -> str | None:
    if GATEWAY_SEARCH_TOOL:
        for n in names:
            if n == GATEWAY_SEARCH_TOOL:
                return n
        for n in names:
            if n.lower() == GATEWAY_SEARCH_TOOL.lower():
                return n
    for n in names:
        norm = n.lower().replace("-", "").replace("_", "")
        if "websearch" in norm:
            return n
    for n in names:
        ln = n.lower()
        if "search" in ln and "x_amz" not in ln and "agentcore" not in ln:
            return n
    return None


def _gateway_query_form(query: str) -> str:
    m = re.search(r"site:\s*(\S+)", query or "", re.I)
    if not m:
        return query[:200]
    raw_host = m.group(1)
    if "://" in raw_host:
        raw_host = urlparse(raw_host if "://" in raw_host else "https://" + raw_host).netloc
    raw_host = raw_host.strip("/")
    terms = _strip_site(query).strip()
    result = f"{terms} {raw_host}".strip() if terms else raw_host
    return result[:200]


async def _gateway_search_async(query: str, limit: int) -> tuple[list[dict], str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    dbg: dict = {"tools": [], "is_error": None, "result_keys": [], "raw_snippet": ""}
    native_query = query[:200]
    stripped_query = _gateway_query_form(query)
    dbg["native_query"] = native_query
    dbg["stripped_query"] = stripped_query

    async with streamablehttp_client(GATEWAY_URL, auth=_sigv4_auth()) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            dbg["tools"] = names
            name = _pick_search_tool(names)
            dbg["picked_tool"] = name
            if not name:
                _GW_DEBUG.update(dbg)
                return [], "no-search-tool-on-gateway"

            forms_to_try = [("native-site", native_query)]
            if GATEWAY_STRIP_SITE and stripped_query != native_query:
                forms_to_try.append(("stripped-site", stripped_query))

            for form_label, q in forms_to_try:
                arg_variants = [
                    {"query": q, "maxResults": limit},
                    {"query": q, "count": limit},
                    {"query": q},
                    {"searchQuery": q, "maxResults": limit},
                    {"q": q, "maxResults": limit},
                ]
                for i, args in enumerate(arg_variants):
                    try:
                        res = await session.call_tool(name, args)
                    except Exception as exc:  # noqa: BLE001
                        dbg[f"call_error_{form_label}_{i}"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                        continue
                    dbg["is_error"] = getattr(res, "isError", None)
                    dbg["used_args"] = list(args.keys())
                    hits = _parse_mcp_results(res, dbg)
                    if hits:
                        dbg["matched_form"] = form_label
                        _GW_DEBUG.update(dbg)
                        return hits, f"gateway({form_label})"

            _GW_DEBUG.update(dbg)
            return [], "gateway-empty-all-forms"


def _parse_mcp_results(res, dbg=None) -> list[dict]:
    out, candidates = [], []
    sc = getattr(res, "structuredContent", None)
    if isinstance(sc, dict):
        if dbg is not None:
            dbg["result_keys"].append("structuredContent:" + ",".join(sc.keys()))
        candidates.append(sc)
    for block in getattr(res, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        if dbg is not None and not dbg.get("raw_snippet"):
            dbg["raw_snippet"] = text[:600]
        try:
            candidates.append(json.loads(text))
        except json.JSONDecodeError:
            pass

    def _extract_items(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "webPages", "items", "organic", "data",
                        "documents", "hits"):
                val = data.get(key)
                if isinstance(val, list):
                    return val
                if isinstance(val, dict) and isinstance(val.get("value"), list):
                    return val["value"]
        return []

    seen = set()
    for data in candidates:
        for it in _extract_items(data):
            if not isinstance(it, dict):
                continue
            url = (it.get("url") or it.get("link") or it.get("uri")
                   or it.get("displayUrl") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            title = it.get("title") or it.get("name") or ""
            snippet = it.get("text") or it.get("snippet") or it.get("description") or ""
            out.append({"title": title, "url": url, "snippet": snippet})
    if dbg is not None:
        dbg["parsed_count"] = len(out)
    return out


def _single_web_search(query: str, limit: int) -> tuple[list[dict], str]:
    _throttle()

    if not GATEWAY_URL:
        print("[search] GATEWAY_URL not configured — no search backend available")
        return [], "no-gateway-configured"

    last_exc = None
    for attempt in range(SEARCH_MAX_RETRIES):
        try:
            hits, via = asyncio.run(_gateway_search_async(query, limit))
            if hits:
                return hits, via if attempt == 0 else f"{via}+retry{attempt}"
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < SEARCH_MAX_RETRIES - 1:
                time.sleep(0.8 * (2 ** attempt) + random.uniform(0.0, 0.6))

    return [], (f"gateway-error({type(last_exc).__name__})" if last_exc else "gateway-empty")


# ─── Ranking ──────────────────────────────────────────────────────────────────
_STOP = {"site", "http", "https", "www", "com", "org", "the", "a", "an", "of", "and",
         "to", "for", "in", "on", "policy", "pdf"}


def _keywords(query: str) -> list[str]:
    q = _strip_site(query)
    q = re.sub(r"https?://\S+", " ", q)
    words = re.findall(r"[a-z0-9]+", q.lower())
    return [w for w in words if len(w) > 2 and w not in _STOP]


def _rank(hits: list[dict], query: str, site_domain: str | None) -> list[tuple[int, dict]]:
    terms = _keywords(query)
    scored = []
    for c in hits:
        u = (c.get("url") or "")
        if not u:
            continue
        hay = (c.get("title", "") + " " + c.get("snippet", "") + " "
               + unquote(urlparse(u).path)).lower()
        score = sum(2 for w in terms if w in hay)
        ul = u.lower()
        if ul.endswith(".pdf"):
            score += 4
        elif ul.endswith((".doc", ".docx")):
            score += 3
        if site_domain and _registrable(urlparse(u).netloc) == _registrable(site_domain):
            score += 2
        host_label = urlparse(u).netloc.lower().split(".")[0]
        if re.match(r"^(staging|stage|qa|dev|test|uat|preprod|sandbox)[-.]?", host_label):
            score -= 5
        if not urlparse(u).path.strip("/"):
            score -= 3
        score += _year_alignment_score(query, hay)
        if c.get("_from_alias"):
            score += ALIAS_HIT_BOOST
        if "english" in hay:
            score += 3
        if any(lang in hay for lang in (
                "german", "french", "spanish", "italian", "norwegian", "swedish",
                "danish", "dutch", "polish", "portuguese", "chinese", "japanese",
                "korean", "turkish", "czech", "finnish", "hungarian", "romanian",
                "deutsch", "espanol", "francais", "verhaltenskodex")):
            score -= 4
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


# ─── Fetch helpers ────────────────────────────────────────────────────────────
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "application/pdf,image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _fetch(url: str) -> tuple[bytes, str]:
    headers = dict(_BROWSER_HEADERS)
    try:
        with urlopen(Request(url, headers=headers), timeout=60) as r:  # noqa: S310
            return r.read(), r.headers.get("Content-Type", "application/octet-stream").split(";")[0]
    except HTTPError as exc:
        if exc.code in (403, 401, 406, 429):
            parts = urlparse(url)
            headers["Referer"] = f"{parts.scheme}://{parts.netloc}/"
            with urlopen(Request(url, headers=headers), timeout=60) as r:  # noqa: S310
                return r.read(), r.headers.get("Content-Type", "application/octet-stream").split(";")[0]
        raise


def _is_doc_url(u: str) -> bool:
    return urlparse(u).path.lower().endswith(_DOC_EXTS)


def _is_doc_ctype(ctype: str) -> bool:
    c = (ctype or "").lower()
    return ("pdf" in c or "msword" in c or "officedocument" in c
            or "ms-excel" in c or "application/octet-stream" in c or "rtf" in c)


def _doc_links(html: bytes, base_url: str, domain: str) -> list[str]:
    txt = html.decode("utf-8", "ignore")
    found = []
    for m in re.finditer(r'href=["\']([^"\']+\.(?:pdf|docx?|rtf|xlsx?)(?:\?[^"\']*)?)["\']', txt, re.I):
        found.append(urljoin(base_url, m.group(1)))
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', txt, re.I | re.S):
        href, label = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        if re.search(r"download|\bpdf\b|document|report|policy|conduct|ethic", label, re.I):
            found.append(urljoin(base_url, href))
    out = []
    for u in found:
        if _registrable(urlparse(u).netloc) == _registrable(domain) and u not in out:
            out.append(u)
    return out


def _safe_name(url: str) -> str:
    p = urlparse(url).path.rsplit("/", 1)[-1] or "document"
    return p if "." in p else p + ".html"


def _site_root(query: str) -> str | None:
    m = re.search(r"site:\s*(\S+)", query or "", re.I)
    if not m:
        return None
    raw = m.group(1).rstrip("/")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    return f"{p.scheme}://{p.netloc}/"


def _page_doc_candidates(page_body: bytes, page_url: str, ctx: str) -> tuple[list[str], list[str]]:
    links = _doc_links(page_body, page_url, urlparse(page_url).netloc.lower())
    ext   = [u for u in links if _is_doc_url(u)]
    other = [u for u in links if not _is_doc_url(u)]
    pool  = _rank([{"url": u, "title": ""} for u in (ext or other)], ctx, None)
    return [c["url"] for _, c in pool], links


def _subpage_links(page_body: bytes, page_url: str, query: str, domain: str) -> list[str]:
    txt = page_body.decode("utf-8", "ignore")
    q = re.sub(r"site:\s*\S+", "", query, flags=re.I)
    terms = [t for t in re.findall(r"[a-z]+", q.lower()) if len(t) > 3]
    if not terms:
        return []
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', txt, re.I | re.S):
        href, label = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2)).lower()
        full = urljoin(page_url, href)
        if _registrable(urlparse(full).netloc) != _registrable(domain) or _is_doc_url(full):
            continue
        if full in seen or full.rstrip("/") == page_url.rstrip("/"):
            continue
        haystack = (href + " " + label).lower()
        score = sum(1 for t in terms if t in haystack)
        if score:
            seen.add(full)
            scored.append((score, full))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [u for _, u in scored[:3]]


# ─── AgentCore Browser fallback: deterministic DOM extraction (Pattern B) ─────
_WAF_BLOCK_MARKERS = (
    "attention required", "sorry, you have been blocked", "access denied",
    "are you a human", "captcha", "cloudflare", "request blocked",
    "reference #", "akamai", "incapsula", "perimeterx", "unusual traffic",
    "enable javascript and cookies",
)


def _looks_like_block_page(html: str | None) -> str | None:
    if not html:
        return None
    low = html.lower()
    for marker in _WAF_BLOCK_MARKERS:
        if marker in low:
            return marker
    return None


_JS_HARVEST_LINKS = """
() => {
  const out = [];
  const seen = new Set();
  const push = (u, t) => {
    if (!u) return;
    try { u = new URL(u, document.baseURI).href; } catch (e) { return; }
    if (!/^https?:/i.test(u) || seen.has(u)) return;
    seen.add(u);
    out.push({ url: u, text: (t || '').replace(/\\s+/g, ' ').trim().slice(0, 160) });
  };
  document.querySelectorAll('a[href]').forEach(e => push(e.href, e.textContent));
  document.querySelectorAll('[data-href],[data-url],[data-file],[data-download],[data-pdf]').forEach(e => {
    push(e.getAttribute('data-href') || e.getAttribute('data-url') ||
         e.getAttribute('data-file') || e.getAttribute('data-download') ||
         e.getAttribute('data-pdf'), e.textContent);
  });
  document.querySelectorAll('[onclick]').forEach(e => {
    const oc = e.getAttribute('onclick') || '';
    const m = oc.match(/https?:\\/\\/[^'"\\)\\s]+/);
    if (m) push(m[0], e.textContent);
  });
  return out;
}
"""

_DOWNLOAD_TEXT_RE = re.compile(
    r"download|\bpdf\b|\breport\b|annual|10-?k|financial|statement|policy|"
    r"conduct|sustainab|proxy|filing|document|view\s+report",
    re.I,
)

_GENERIC_FOOTER_TEXT_RE = re.compile(
    r"^\s*(privacy( policy| notice)?|cookie( policy| notice| settings|s)?|"
    r"terms( of (use|service))?|legal( information)?|"
    r"accessibility( statement)?|site\s*map|do not sell my (personal )?"
    r"info(rmation)?|your (privacy|california privacy) (choices|rights)|"
    r"trademark|copyright)\s*$",
    re.I,
)


def _is_plausible_download_control(text: str) -> bool:
    """Positive doc-intent match MINUS generic legal/footer boilerplate."""
    t = (text or "").strip()
    if not t or _GENERIC_FOOTER_TEXT_RE.match(t):
        return False
    return bool(_DOWNLOAD_TEXT_RE.search(t))

_WRONG_CLASS_FILENAME_MARKERS = tuple(
    m.strip().lower() for m in os.environ.get(
        "WRONG_CLASS_FILENAME_MARKERS",
        "assurance,pricewaterhousecoopers,-pwc-,pwc-,deloitte,ey-,kpmg,"
        "guidance,guidelines,methodology,-key-facts,key-facts-,datapack,"
        "data-pack,data-dictionary,factbook,fact-book,-sasb-index,-wef-index,"
        "-index-,compliance-statement,transcript,video-,-animation,"
        "communication-on-progress",
    ).split(",") if m.strip()
)
_STRICT_REJECT_CLASSES = {
    c.strip().lower() for c in os.environ.get(
        "STRICT_REJECT_CLASSES",
        "annual report,sustainability report,environmental policy,"
        "occupational health & safety policy,anti-bribery and corruption policy,"
        "whistleblowing mechanism,tax strategy and governance,"
        "code of conduct,supplier code of conduct,proxy statement,"
        "remuneration report",
    ).split(",") if c.strip()
}

_CLASS_SCOPED_WRONG_MARKERS: dict[str, tuple[str, ...]] = {
    "sustainability report": (
        "strategic-report", "esg-update", "esg-supplement", "esg-factbook",
        "green-bond", "sdg-bond", "cdp-carbon-disclosure",
    ),
    "environmental policy": (
        "strategic-report", "esg-update", "esg-supplement", "esg-factbook",
        "cdp-carbon-disclosure",
    ),
    "_query_text_scoped": {
        "impact report": (
            "strategic-report", "esg-update", "esg-supplement",
            "green-bond", "sdg-bond",
        ),
        "ghg emission report": (
            "strategic-report", "esg-update", "esg-supplement", "esg-factbook",
            "cdp-carbon-disclosure",
        ),
    },
}

def _is_wrong_class_filename(url: str, query: str) -> bool:
    name = unquote(urlparse(url).path).rsplit("/", 1)[-1].lower()
    low_query = (query or "").lower()
    matched = [c for c, _ in _matched_doc_classes(query)]

    if any(c in _STRICT_REJECT_CLASSES for c in matched):
        for marker in _WRONG_CLASS_FILENAME_MARKERS:
            if marker and marker in name:
                return True

    for canonical in matched:
        for marker in _CLASS_SCOPED_WRONG_MARKERS.get(canonical, ()):
            if marker and marker in name:
                return True

    for phrase, markers in _CLASS_SCOPED_WRONG_MARKERS.get("_query_text_scoped", {}).items():
        if phrase in low_query:
            for marker in markers:
                if marker and marker in name:
                    return True

    return False

QUERY_MAX_VERIFIES = int(os.environ.get("QUERY_MAX_VERIFIES", "100"))
QUERY_MAX_SECONDS = float(os.environ.get("QUERY_MAX_SECONDS", "900"))

class _QueryBudget:
    """Shared per-query counter threaded through every resolver tier.

    `rejected` (v39.1, log-evidence fix "Bug 2"): URLs that FAILED CLASS
    VERIFICATION for THIS query are recorded here so a later nav page that
    re-links the same junk PDF (observed: the same IR fact-sheet / patent-
    table PDF sits in the sidebar of dozens of press-release pages) skips it
    on sight instead of re-fetching + re-spending an LLM verify call on a URL
    already proven wrong for this exact document class.

    Deliberately PER-QUERY, not per-run like `known_bad`: a document that is
    the wrong CLASS for "Whistleblowing Policy" may be the right class for a
    different query later in the same run, so a class-rejection must not
    leak across queries the way a transport-layer failure safely can.
    """
    def __init__(self):
        self.verifies = 0
        self.deadline = time.monotonic() + QUERY_MAX_SECONDS
        self.rejected: set[str] = set()

    def can_verify(self) -> bool:
        return (self.verifies < QUERY_MAX_VERIFIES
                and time.monotonic() < self.deadline)

    def note_verify(self) -> None:
        self.verifies += 1

    def time_left(self) -> bool:
        return time.monotonic() < self.deadline

    def why_stopped(self) -> str:
        if self.verifies >= QUERY_MAX_VERIFIES:
            return f"verify budget {QUERY_MAX_VERIFIES} exhausted"
        return f"wall-clock {QUERY_MAX_SECONDS}s exceeded"

    def is_rejected(self, url: str) -> bool:
        return url in self.rejected

    def mark_rejected(self, url: str) -> None:
        self.rejected.add(url)

def _strip_fragment(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/") or url
    except Exception:  # noqa: BLE001
        return url


def _is_navigation_href(url: str, domain: str | None) -> bool:
    if not url:
        return False
    if _is_doc_url(url) or "download=1" in url.lower():
        return False
    if domain and _registrable(urlparse(url).netloc) != _registrable(domain):
        return False
    return url.lower().startswith(("http://", "https://"))


_COOKIE_MODAL_SELECTORS = (
    "#__tealiumGDPRecModal", "#onetrust-banner-sdk", "#onetrust-consent-sdk",
    "#truste-consent-track", ".cookie-modal", "#cookie-banner", "#cookieConsent",
    "[aria-label*='cookie' i]", "[id*='cookie' i][role='dialog']",
)
_COOKIE_ACCEPT_TEXTS = (
    "Accept all", "Accept All", "Accept all cookies", "I accept", "Accept",
    "Agree", "Allow all", "Got it", "Continue", "OK",
)

def _dismiss_cookie_modals(page) -> None:
    for label in _COOKIE_ACCEPT_TEXTS:
        try:
            btn = page.get_by_role("button", name=label, exact=False).first
            btn.click(timeout=800)
            page.wait_for_timeout(150)
            break
        except Exception:  # noqa: BLE001
            continue
    for sel in _COOKIE_MODAL_SELECTORS:
        try:
            page.evaluate(
                "(s)=>{document.querySelectorAll(s).forEach(e=>e.remove());}", sel)
        except Exception:  # noqa: BLE001
            continue

def _click_target_visible(page, text: str, timeout_ms: int = 1500) -> bool:
    try:
        loc = page.get_by_text(text, exact=False).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001
        return False

def _verify_priority(url: str, query: str) -> int:
    name = unquote(urlparse(url).path).rsplit("/", 1)[-1].lower()
    terms = [t for t in _keywords(query) if len(t) > 3]
    if not terms:
        return 0
    return 3 if any(t in name for t in terms) else 0

def _sort_for_verify(urls: list[str], query: str) -> list[str]:
    return sorted(urls, key=lambda u: _verify_priority(u, query), reverse=True)

def _doc_candidate_score(url: str, text: str, query: str, domain: str | None) -> int:
    ranked = _rank([{"url": url, "title": text, "snippet": text}], query, domain)
    base = ranked[0][0] if ranked else 0
    return base + _filing_type_score_adjustment(url, text, query)


_FILING_TYPE_HINTS: dict[str, dict[str, list[str]]] = {
    "annual report": {
        "boost": ["10-k", "10k", "form10-k", "form 10-k", "annual report",
                  "ar20", "integrated annual report", "annual-report"],
        "penalize": ["10-q", "10q", "quarterly", "8-k", "8k",
                     "consolidated_financial_statements", "current report"],
    },
    "proxy statement": {
        "boost": ["def 14a", "def14a", "proxy statement", "proxy-statement",
                  "definitive proxy"],
        "penalize": ["10-q", "10-k", "8-k", "quarterly", "current report"],
    },
    "remuneration report": {
        "boost": ["remuneration report", "directors' remuneration",
                  "remuneration-report"],
        "penalize": ["10-q", "10-k", "8-k"],
    },
}


def _filing_type_score_adjustment(url: str, text: str, query: str) -> int:
    matched = [c for c, _ in _matched_doc_classes(query)]
    if not matched:
        return 0
    hay = (unquote(urlparse(url).path) + " " + (text or "")).lower()
    adj = 0
    for canonical in matched:
        hints = _FILING_TYPE_HINTS.get(canonical)
        if not hints:
            continue
        if any(k in hay for k in hints.get("boost", [])):
            adj += 6
        if any(k in hay for k in hints.get("penalize", [])):
            adj -= 6
    return adj


# ═══════════════════════════════════════════════════════════════════════════
# Generic junk-host filter (NOT company-specific)
# ═══════════════════════════════════════════════════════════════════════════
_JUNK_HOST_LABELS = {
    l.strip().lower() for l in os.environ.get(
        "JUNK_HOST_LABELS",
        "apps,itunes,books,book,podcasts,podcast,music,tv,store,shop,play,"
        "games,gaming,maps,translate,developer,developers,support,community,"
        "forums,forum,help,careers,jobs",
    ).split(",") if l.strip()
}
_JUNK_REGISTRABLE_DOMAINS = {
    d.strip().lower() for d in os.environ.get(
        "JUNK_REGISTRABLE_DOMAINS",
        "play.google.com,facebook.com,twitter.com,x.com,linkedin.com,"
        "instagram.com,youtube.com,wikipedia.org,glassdoor.com,indeed.com,"
        "crunchbase.com,tiktok.com,pinterest.com,reddit.com",
    ).split(",") if d.strip()
}


def _is_junk_host(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    if _registrable(host) in _JUNK_REGISTRABLE_DOMAINS:
        return True
    label = (host[4:] if host.startswith("www.") else host).split(".")[0]
    return label in _JUNK_HOST_LABELS


# v39 Phase 3.1: IR/investor-navigation link detector. True when a harvested
# nav link's URL path or link text names an investor/report/policy section
# (investors, annual-report, sustainability, governance, filings, ...). Used to
# PROACTIVELY prioritize IR/report/policy sections in the browser deep-nav
# frontier (Phase 3.2), independent of whether the link happens to share a
# keyword with the specific query — a document lives under these sections even
# when the query terms don't appear in the section's own nav label.
def _is_ir_nav_link(url: str, text: str = "") -> bool:
    hay = (unquote(urlparse(url or "").path) + " " + (text or "")).lower()
    return any(k in hay for k in IR_NAV_KEYWORDS)


# v39.1 (log-evidence fix, "Bug 1" + "Bug 3"): press-release / news-detail
# detector. Observed on a real Whistleblowing-Policy run (JNJ): the deep-nav
# frontier walked into dozens of /investor-news/news-details/... press
# releases (Bug 1) purely because _is_ir_nav_link matches "investor" and
# "news" — IR_NAV_KEYWORDS was never meant to pull in press releases, just
# report/policy sections — and several IR-collateral PDFs (fact sheets,
# patent tables, earnings-release invitations) reached the LLM verifier
# instead of being dropped pre-LLM (Bug 3), burning verify-budget slots that
# a real annual-report/policy candidate elsewhere on the same run needed.
#
# Deliberately GENERIC (not JNJ-specific): every public company runs a
# newsroom/press-release section with this same structural shape, so this is
# a universal exclude, not a company pattern. Two layers, same as
# _is_wrong_class_filename:
#   - URL PATH markers: reliable across virtually every corporate press
#     center regardless of company (news-details, press-release(s),
#     newsroom, news-release, media-release).
#   - filename/link-TEXT markers: catches press-release PDFs/link labels
#     that don't live under one of the above paths (e.g. hosted at the site
#     root) plus common press-release headline verbs in the link TEXT only
#     (never in a document's own title, to avoid rejecting a real report
#     whose filename happens to share a word).
_PRESS_RELEASE_PATH_MARKERS = tuple(
    m.strip().lower() for m in os.environ.get(
        "PRESS_RELEASE_PATH_MARKERS",
        "news-details,news-detail,press-release,press-releases,newsroom,"
        "news-release,media-release,media-releases,news-and-media,"
        "media-center/news",
    ).split(",") if m.strip()
)
_PRESS_RELEASE_FILENAME_MARKERS = tuple(
    m.strip().lower() for m in os.environ.get(
        "PRESS_RELEASE_FILENAME_MARKERS",
        "press-release,press_release,pressrelease,earnings-release,"
        "earnings_release,media-release,news-release,fact-sheet,"
        "patent-table,earnings-invitation",
    ).split(",") if m.strip()
)
_PRESS_RELEASE_TEXT_VERB_MARKERS = tuple(
    m.strip().lower() for m in os.environ.get(
        "PRESS_RELEASE_TEXT_VERB_MARKERS",
        "announces,appoints,presents new,reports results,declares dividend,"
        "approval for,receives fda,receives approval",
    ).split(",") if m.strip()
)


def _is_press_release_url(url: str, text: str = "") -> bool:
    """
    True if a harvested link is almost certainly a press release / news
    item rather than an official policy/report document. Runs BEFORE any
    LLM call — see the four call sites this feeds (Tier 1 doc/label
    harvest, deep-nav sub_docs, deep-nav frontier seed + descent).
    """
    try:
        path = unquote(urlparse(url or "").path).lower()
    except Exception:  # noqa: BLE001
        path = (url or "").lower()
    if any(m in path for m in _PRESS_RELEASE_PATH_MARKERS):
        return True
    filename = path.rsplit("/", 1)[-1]
    if any(m in filename for m in _PRESS_RELEASE_FILENAME_MARKERS):
        return True
    low_text = (text or "").lower()
    if any(m in low_text for m in _PRESS_RELEASE_TEXT_VERB_MARKERS):
        return True
    return False


def _browser_resolve_document(page_url: str, domain: str | None, query: str,
                              cache: dict | None = None,
                              verify_fn=None,
                              known_bad: dict[str, str] | None = None,
                              budget: "_QueryBudget | None" = None) -> dict | None:
    """Thin caching wrapper around _browser_resolve_document_uncached."""
    if cache is not None and page_url in cache:
        cached = cache[page_url]
        if cached and cached.get("body"):
            if verify_fn is None or cached.get("_verified_for") == query:
                print(f"[browser] cache hit for {page_url} — reusing previous "
                      f"render/resolution result for this run, no new browser session")
                return cached
            if verify_fn(cached):
                out = dict(cached)
                out["verified"] = True
                out["_verified_for"] = query
                print(f"[browser] cache hit for {page_url}; re-verified OK for "
                      f"this query's document class")
                return out
            print(f"[browser] cache hit for {page_url} but cached document is "
                  f"the WRONG class for this query; resolving fresh instead of "
                  f"reusing it")
            result = _browser_resolve_document_uncached(page_url, domain, query,
                                                         verify_fn, known_bad, budget)
            cache[page_url] = result
            return result
        if cached and cached.get("_had_candidates"):
            print(f"[browser] cache: {page_url} had candidates on a prior query "
                  f"but none matched that class; resolving fresh for this "
                  f"query's class rather than assuming the same miss")
            result = _browser_resolve_document_uncached(page_url, domain, query,
                                                         verify_fn, known_bad, budget)
            cache[page_url] = result
            return result
        print(f"[browser] cache hit for {page_url} — no resolvable document on "
              f"this page (no candidates at all), reusing miss, no new browser session")
        return cached

    result = _browser_resolve_document_uncached(page_url, domain, query, verify_fn, known_bad, budget)
    if cache is not None:
        cache[page_url] = result
    return result


def _make_browser_verify_fn(query: str, budget: "_QueryBudget | None" = None,
                             company: str = ""):
    """verify_fn(candidate_doc) -> bool. Fail-closed class+company check scoped to one candidate."""
    if not (BROWSER_VERIFY_CLASS and _bedrock is not None):
        return None

    def _verify(cand: dict) -> bool:
        url = cand.get("url", "")
        # v39.1 Bug 2: this exact URL already failed class verification once
        # for THIS query (e.g. the same IR fact-sheet PDF linked from a dozen
        # different press-release pages) — skip immediately, no re-fetch,
        # no re-LLM-call.
        if budget is not None and budget.is_rejected(url):
            print(f"[verify] already rejected earlier this query, skipping: {url}")
            cand["_verify_decision"] = {"selected_url": None, "topic_match": False,
                                        "reason": "already-rejected-this-query"}
            return False
        # v39.1 Bug 3: deterministic press-release reject, no LLM call.
        if _is_press_release_url(url):
            print(f"[verify] STRICT reject (press-release/news item, no LLM): {url}")
            cand["_verify_decision"] = {"selected_url": None, "topic_match": False,
                                        "reason": "strict-press-release-reject"}
            if budget is not None:
                budget.mark_rejected(url)
            return False
        if _is_wrong_class_filename(url, query):
            print(f"[verify] STRICT reject (near-neighbor filename, no LLM): {url}")
            cand["_verify_decision"] = {"selected_url": None, "topic_match": False,
                                        "reason": "strict-near-neighbor-filename-reject"}
            if budget is not None:
                budget.mark_rejected(url)
            return False
        if budget is not None and not budget.can_verify():
            print(f"[verify] budget stop ({budget.why_stopped()}): skipping "
                  f"LLM verify for {url}")
            cand["_verify_decision"] = {"selected_url": None, "topic_match": False,
                                        "reason": f"query-budget:{budget.why_stopped()}"}
            return False
        sample = ""
        try:
            if "html" in (cand.get("ctype") or ""):
                sample = cand["body"].decode("utf-8", "ignore")[:1500]
            else:
                sample = cand["body"][:1500].decode("utf-8", "ignore").replace("\x00", " ")
        except Exception:  # noqa: BLE001
            sample = ""
        info = {"url": url, "filename": _safe_name(url),
                "head_ctype": cand.get("ctype", ""), "content_sample": sample}
        if budget is not None:
            budget.note_verify()
        vdec = _llm_select_best(query, [info], company=company)
        cand["_verify_decision"] = vdec
        ok = _confident(vdec, query)
        if not ok:
            print(f"[browser] candidate REJECTED by class check "
                  f"({url}): {vdec.get('reason')}")
            if budget is not None:
                budget.mark_rejected(url)
        return ok

    return _verify


def _browser_resolve_document_uncached(page_url: str, domain: str | None,
                              query: str, verify_fn=None,
                              known_bad: dict[str, str] | None = None,
                              budget: "_QueryBudget | None" = None) -> dict:
    """Layered, GENERIC in-browser document resolver (see module docstring)."""
    _MISS = {"_no_doc": True, "_had_candidates": False}
    if not USE_BROWSER:
        return _MISS
    try:
        from playwright.sync_api import sync_playwright
        from bedrock_agentcore.tools.browser_client import browser_session
    except Exception as exc:  # noqa: BLE001
        print(f"[browser] import failed ({exc}); skipping browser fallback")
        return _MISS

    referer = f"https://{domain}/" if domain else None

    def _acceptable(url: str, ctype: str) -> bool:
        return _is_doc_ctype(ctype) or (
            _is_doc_url(url) and "html" not in (ctype or "").lower())

    def _domain_ok(url: str) -> bool:
        if BROWSER_ALLOW_OFFDOMAIN_DOCS:
            return True
        return not domain or _registrable(urlparse(url).netloc) == _registrable(domain)

    resolved: dict | None = None
    had_candidates = False
    verify_budget = BROWSER_MAX_VERIFY_CANDIDATES if verify_fn else 1
    click_budget = min(BROWSER_MAX_CLICK_ATTEMPTS, verify_budget) if verify_fn else 1
    pool_cap = max(BROWSER_MAX_DOC_CANDIDATES, verify_budget)
    _deadline = time.monotonic() + BROWSER_RESOLVE_MAX_SECONDS

    def _time_left() -> bool:
        if budget is not None and not budget.time_left():
            return False
        return time.monotonic() < _deadline

    try:
        with browser_session(BROWSER_REGION) as client:
            ws_url, headers = client.generate_ws_headers()
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(ws_url, headers=headers)
                try:
                    context = (browser.contexts[0] if browser.contexts
                               else browser.new_context())
                    page = (context.pages[0] if context.pages
                            else context.new_page())

                    # ── Tier 1: navigate + harvest ──
                    nav_error = None
                    try:
                        goto_kwargs = {"wait_until": BROWSER_WAIT_UNTIL,
                                       "timeout": BROWSER_SESSION_TIMEOUT * 1000}
                        if referer:
                            goto_kwargs["referer"] = referer
                        page.goto(page_url, **goto_kwargs)
                    except Exception as nav_exc:  # noqa: BLE001
                        nav_error = str(nav_exc)
                        print(f"[browser] goto warning ({page_url}): {nav_exc}")
                    try:
                        page.wait_for_timeout(BROWSER_SETTLE_MS)
                    except Exception:  # noqa: BLE001
                        pass

                    _dismiss_cookie_modals(page)

                    final_url = None
                    try:
                        final_url = page.url
                    except Exception:  # noqa: BLE001
                        pass
                    if final_url and final_url.rstrip("/") != page_url.rstrip("/"):
                        print(f"[browser] redirected: requested={page_url} "
                              f"final={final_url} referer_sent={referer!r}")

                    try:
                        rendered_html = page.content()
                    except Exception:  # noqa: BLE001
                        rendered_html = None
                    block_marker = _looks_like_block_page(rendered_html)
                    if block_marker:
                        print(f"[browser] possible WAF/bot-challenge page "
                              f"(matched {block_marker!r}) at {final_url or page_url}")

                    try:
                        harvested = page.evaluate(_JS_HARVEST_LINKS) or []
                    except Exception as exc:  # noqa: BLE001
                        print(f"[browser] link harvest failed: {exc}")
                        harvested = []

                    if BROWSER_DEEP_NAV:
                        try:
                            tops = page.query_selector_all(
                                "nav a, nav button, [role=navigation] a, "
                                "header a, .nav__link, li[aria-haspopup]")
                            for el in tops[:25]:
                                try:
                                    el.hover(timeout=800)
                                    page.wait_for_timeout(150)
                                except Exception:  # noqa: BLE001
                                    continue
                            page.wait_for_timeout(400)
                            harvested2 = page.evaluate(_JS_HARVEST_LINKS) or []
                            seen_u = {h.get("url") for h in harvested}
                            for h in harvested2:
                                if h.get("url") not in seen_u:
                                    harvested.append(h)
                                    seen_u.add(h.get("url"))
                            print(f"[browser] hover-reveal: harvested grew to "
                                  f"{len(harvested)} links after menu hover")
                        except Exception as exc:  # noqa: BLE001
                            print(f"[browser] hover-reveal failed: {exc}")

                    doc_links = [h for h in harvested
                                 if _is_doc_url(h.get("url", ""))
                                 and _domain_ok(h["url"])
                                 and not _is_junk_host(h["url"])
                                 and not _is_press_release_url(h["url"], h.get("text", ""))]

                    labeled = [h for h in harvested
                               if not _is_doc_url(h.get("url", ""))
                               and _is_plausible_download_control(h.get("text", ""))
                               and _domain_ok(h.get("url", ""))
                               and not _is_junk_host(h.get("url", ""))
                               and not _is_press_release_url(h.get("url", ""), h.get("text", ""))
                               and not _is_navigation_href(h.get("url", ""), domain)]
                    # v39.1 Bug 1: press-release / news-detail pages are
                    # excluded from nav_links here so they never enter the
                    # deep-nav frontier in the first place (previously
                    # _is_ir_nav_link's "investor"/"news" keywords pulled
                    # dozens of /investor-news/news-details/... press
                    # releases into the frontier on a real run).
                    nav_links = [h["url"] for h in harvested
                                 if _is_navigation_href(h.get("url", ""), domain)
                                 and not _is_junk_host(h.get("url", ""))
                                 and not _is_press_release_url(h.get("url", ""), h.get("text", ""))]



                    ranked_docs = sorted(
                        doc_links,
                        key=lambda h: (_verify_priority(h["url"], query),
                                       _doc_candidate_score(h["url"], h.get("text", ""),
                                                            query, domain)),
                        reverse=True,
                    )[:pool_cap]
                    had_candidates = bool(ranked_docs or labeled)
                    print(f"[browser] harvested {len(harvested)} links: "
                          f"{len(doc_links)} doc-file candidates, "
                          f"{len(labeled)} download-labeled controls "
                          f"(verify_budget={verify_budget}, click_budget={click_budget})"
                          + (f" | nav_error={nav_error}" if nav_error else "")
                          + (f" | block_marker={block_marker}" if block_marker else ""))

                    # ── Tier 2: in-session fetch, WITH in-loop class verify ──
                    if BROWSER_DOWNLOAD_IN_SESSION:
                        req = context.request
                        tried = 0
                        verified_hits: list[dict] = []
                        for h in ranked_docs:
                            if tried >= verify_budget or not _time_left():
                                if not _time_left():
                                    print(f"[browser] Tier 2 stopped: "
                                          f"BROWSER_RESOLVE_MAX_SECONDS "
                                          f"({BROWSER_RESOLVE_MAX_SECONDS}s) exceeded")
                                break
                            cand = h["url"]
                            if known_bad is not None and cand in known_bad:
                                print(f"[browser] skipping known-bad candidate "
                                      f"(failed at transport layer on a prior "
                                      f"query, not class-dependent): {cand} "
                                      f"[{known_bad[cand]}]")
                                continue
                            if budget is not None and budget.is_rejected(cand):
                                print(f"[browser] skipping candidate already "
                                      f"class-rejected earlier this query "
                                      f"(v39.1 Bug 2): {cand}")
                                continue
                            body = None
                            ctype = ""
                            try:
                                r = req.get(cand, headers={"referer": final_url or referer or cand},
                                            timeout=BROWSER_SESSION_TIMEOUT * 1000)
                                status = r.status
                                ctype = (r.headers or {}).get("content-type", "").split(";")[0].lower()
                                if status >= 400 or not _acceptable(cand, ctype):
                                    continue
                                body = r.body()
                            except Exception as exc:  # noqa: BLE001
                                if "abort" in str(exc).lower():
                                    print(f"[browser] in-session GET aborted "
                                          f"({cand}); retrying via navigation + "
                                          f"native download instead")
                                    try:
                                        with page.expect_download(
                                                timeout=BROWSER_CLICK_TIMEOUT_MS) as di:
                                            page.goto(cand, timeout=BROWSER_SESSION_TIMEOUT * 1000)
                                        dl = di.value
                                        with open(dl.path(), "rb") as fh:
                                            body = fh.read()
                                        fname = dl.suggested_filename or ""
                                        ctype = ("application/pdf" if fname.lower().endswith(".pdf")
                                                 else "application/octet-stream")
                                    except Exception as exc2:  # noqa: BLE001
                                        print(f"[browser] navigation-download "
                                              f"fallback also failed ({cand}): {exc2}")
                                        body = None
                                if body is None:
                                    print(f"[browser] in-session GET failed ({cand}): {exc}")
                                    if known_bad is not None:
                                        known_bad[cand] = f"GET failed: {type(exc).__name__}"
                                    continue
                            if not body or len(body) > BROWSER_MAX_DOC_BYTES:
                                continue
                            tried += 1
                            cand_doc = {"url": cand, "body": body,
                                        "ctype": ctype or "application/pdf",
                                        "via": "browser_in_session_fetch"}
                            if verify_fn is not None:
                                if not verify_fn(cand_doc):
                                    print(f"[browser] in-session candidate "
                                          f"{tried}/{verify_budget} wrong class "
                                          f"({cand}); trying next candidate")
                                    continue
                                cand_doc["verified"] = True
                                cand_doc["_verified_for"] = query
                            verified_hits.append(cand_doc)
                            if verify_fn is None:
                                break

                        if verified_hits:
                            def _hit_year(hit: dict) -> int:
                                years = _extract_year_intent(hit["url"])
                                return max(years) if years else -1
                            verified_hits.sort(key=_hit_year, reverse=True)
                            best = verified_hits[0]
                            if len(verified_hits) > 1:
                                print(f"[browser] {len(verified_hits)} candidates "
                                      f"passed class verification; picked most "
                                      f"recent by year: {best['url']}")
                            print(f"[browser] resolved via in-session fetch: "
                                  f"{best['url']} ({len(best['body'])} bytes, "
                                  f"{best['ctype']})")
                            resolved = best

                    # ── Tier 3: click-to-download, WITH in-loop class verify ──
                    skip_clicks = bool(block_marker) and BROWSER_SKIP_CLICK_ON_BLOCK
                    if skip_clicks and labeled:
                        print(f"[browser] skipping Tier 3 click-fallback: WAF/bot "
                              f"challenge marker {block_marker!r} detected on this "
                              f"page (BROWSER_SKIP_CLICK_ON_BLOCK=true)")
                    if resolved is None and BROWSER_CLICK_FALLBACK and labeled and not skip_clicks:
                        clicked = 0
                        seen_labels: set[str] = set()
                        for h in labeled:
                            if clicked >= click_budget or not _time_left():
                                if not _time_left():
                                    print(f"[browser] Tier 3 stopped: "
                                          f"BROWSER_RESOLVE_MAX_SECONDS "
                                          f"({BROWSER_RESOLVE_MAX_SECONDS}s) exceeded")
                                break
                            txt = (h.get("text") or "").strip()
                            if not txt:
                                continue
                            norm_txt = txt.lower()
                            if norm_txt in seen_labels:
                                continue
                            seen_labels.add(norm_txt)
                            click_key = f"click:{page_url}:{norm_txt}"
                            if known_bad is not None and click_key in known_bad:
                                print(f"[browser] skipping known-bad click target "
                                      f"(failed at transport/UI layer on a prior "
                                      f"query, not class-dependent): {txt[:40]!r} "
                                      f"[{known_bad[click_key]}]")
                                continue
                            if not _click_target_visible(page, txt):
                                print(f"[browser] click target not visible "
                                      f"(fast-skip, Fix B): {txt[:40]!r}")
                                if known_bad is not None:
                                    known_bad[click_key] = "not visible (fast-skip)"
                                continue
                            try:
                                loc = page.get_by_text(txt, exact=False).first
                                with page.expect_download(
                                        timeout=BROWSER_CLICK_TIMEOUT_MS) as di:
                                    loc.click(timeout=8000)
                                dl = di.value
                                path = dl.path()
                                with open(path, "rb") as fh:
                                    body = fh.read()
                                fname = dl.suggested_filename or ""
                            except Exception as exc:  # noqa: BLE001
                                print(f"[browser] click-download attempt failed "
                                      f"({txt[:40]!r}): {exc}")
                                if known_bad is not None:
                                    known_bad[click_key] = f"click failed: {type(exc).__name__}"
                                continue
                            if not body or len(body) > BROWSER_MAX_DOC_BYTES:
                                continue
                            clicked += 1
                            ctype = ("application/pdf" if fname.lower().endswith(".pdf")
                                     else "application/octet-stream")
                            dl_url = ""
                            try:
                                dl_url = dl.url
                            except Exception:  # noqa: BLE001
                                pass
                            cand_doc = {"url": dl_url or (final_url or page_url),
                                        "body": body, "ctype": ctype,
                                        "via": "browser_click_download"}
                            if verify_fn is not None:
                                if not verify_fn(cand_doc):
                                    print(f"[browser] click-download candidate "
                                          f"{clicked}/{click_budget} wrong class "
                                          f"({fname or dl_url}); trying next control")
                                    continue
                                cand_doc["verified"] = True
                                cand_doc["_verified_for"] = query
                            print(f"[browser] resolved via click-download: "
                                  f"{fname or dl_url} ({len(body)} bytes)")
                            resolved = cand_doc
                            break

                    # ── v39 Phase 3.2: in-browser deep navigation, IR-first ──
                    if (resolved is None and BROWSER_DEEP_NAV and nav_links
                            and _time_left()):
                        visited_nav: set[str] = {page_url.rstrip("/")}
                        _nav_terms = [t for t in _keywords(query) if len(t) > 3]
                        def _nav_relevant(u: str) -> bool:
                            if not _nav_terms:
                                return True
                            path = unquote(urlparse(u).path).lower()
                            return any(t in path for t in _nav_terms)
                        # Follow IR-nav links even without a query-term match
                        # (docs live under investor/sustainability/governance
                        # sections whose OWN label rarely repeats query terms),
                        # AND sort IR-nav links to the FRONT of the frontier so
                        # the browser reaches report/policy sections first. The
                        # query-term topical filter is retained as a SECONDARY
                        # signal per the brief.
                        _seed_nav, _seen_seed = [], set()
                        for u in nav_links:
                            sf = _strip_fragment(u)
                            if sf in _seen_seed or sf in visited_nav:
                                continue
                            _seen_seed.add(sf)
                            if _is_ir_nav_link(u) or _nav_relevant(u):
                                _seed_nav.append(u)
                        _seed_nav.sort(key=lambda u: 0 if _is_ir_nav_link(u) else 1)
                        if not _seed_nav:
                            _seed_nav = [u for u in nav_links[:5]]
                        frontier = [(u, 1) for u in _seed_nav]
                        print(f"[browser][nav] IR-prioritized frontier: "
                              f"{len(_seed_nav)}/{len(nav_links)} nav links "
                              f"(IR-nav first, query terms {_nav_terms} secondary)")
                        nav_pages_done = 0
                        req = context.request
                        while frontier and resolved is None and _time_left():
                            nav_url, depth = frontier.pop(0)
                            key = _strip_fragment(nav_url)
                            if key in visited_nav or depth > BROWSER_NAV_MAX_DEPTH:
                                continue
                            visited_nav.add(key)
                            if nav_pages_done >= BROWSER_NAV_MAX_PAGES:
                                break
                            nav_pages_done += 1
                            try:
                                page.goto(nav_url, wait_until=BROWSER_WAIT_UNTIL,
                                          timeout=BROWSER_SESSION_TIMEOUT * 1000)
                                page.wait_for_timeout(BROWSER_SETTLE_MS)
                                _dismiss_cookie_modals(page)
                                sub = page.evaluate(_JS_HARVEST_LINKS) or []
                            except Exception as exc:  # noqa: BLE001
                                print(f"[browser][nav] goto failed ({nav_url}): {exc}")
                                continue
                            sub_docs = sorted(
                                [h for h in sub if _is_doc_url(h.get("url", ""))
                                 and _domain_ok(h["url"]) and not _is_junk_host(h["url"])
                                 and not _is_press_release_url(h["url"], h.get("text", ""))],
                                key=lambda h: (_verify_priority(h["url"], query),
                                               _doc_candidate_score(h["url"], h.get("text", ""),
                                                                    query, domain)),
                                reverse=True)[:pool_cap]
                            print(f"[browser][nav] {nav_url}: {len(sub_docs)} doc "
                                  f"candidates (depth={depth}, pages={nav_pages_done})")
                            nav_hits: list[dict] = []
                            for h in sub_docs:
                                if not _time_left():
                                    break
                                cand = h["url"]
                                if known_bad is not None and cand in known_bad:
                                    continue
                                if budget is not None and budget.is_rejected(cand):
                                    continue
                                try:
                                    r = req.get(cand, headers={"referer": nav_url},
                                                timeout=BROWSER_SESSION_TIMEOUT * 1000)
                                    if r.status >= 400:
                                        continue
                                    ct = (r.headers or {}).get("content-type", "").split(";")[0].lower()
                                    if not _acceptable(cand, ct):
                                        continue
                                    body = r.body()
                                except Exception as exc:  # noqa: BLE001
                                    if known_bad is not None:
                                        known_bad[cand] = f"nav GET failed: {type(exc).__name__}"
                                    continue
                                if not body or len(body) > BROWSER_MAX_DOC_BYTES:
                                    continue
                                cd = {"url": cand, "body": body,
                                      "ctype": ct or "application/pdf",
                                      "via": "browser_deep_nav"}
                                if verify_fn is not None:
                                    if not verify_fn(cd):
                                        continue
                                    cd["verified"] = True
                                    cd["_verified_for"] = query
                                nav_hits.append(cd)
                                if verify_fn is None:
                                    break
                            if nav_hits:
                                nav_hits.sort(
                                    key=lambda d: (max(_extract_year_intent(d["url"]))
                                                   if _extract_year_intent(d["url"]) else -1),
                                    reverse=True)
                                resolved = nav_hits[0]
                                print(f"[browser][nav] resolved via deep nav: "
                                      f"{resolved['url']}")
                                break
                            # descend further — query-term topical filter PLUS
                            # IR-nav priority so we keep proactively expanding
                            # into investor/report/policy sections at depth 2.
                            # v39.1 Bug 1: press-release/news-detail pages are
                            # excluded here too, so descent doesn't re-admit
                            # them even though they'd otherwise match
                            # _is_ir_nav_link on "investor"/"news".
                            for h in sub:
                                u2 = h.get("url", "")
                                if (_is_navigation_href(u2, domain)
                                        and not _is_junk_host(u2)
                                        and not _is_press_release_url(u2, h.get("text", ""))
                                        and (_is_ir_nav_link(u2, h.get("text", ""))
                                             or _nav_relevant(u2))):
                                    nk = u2.rstrip("/")
                                    if nk not in visited_nav:
                                        frontier.append((u2, depth + 1))

                    # ── Tier 4: optional vision ──
                    if (resolved is None and BROWSER_VISION_MODEL_ID
                            and _bedrock is not None and _time_left()):
                        vres = _browser_vision_resolve(page, query, domain,
                                                       final_url or page_url,
                                                       _acceptable)
                        if vres and vres.get("body"):
                            if verify_fn is None or verify_fn(vres):
                                if verify_fn is not None:
                                    vres["verified"] = True
                                    vres["_verified_for"] = query
                                resolved = vres
                                had_candidates = True
                            else:
                                print("[browser][vision] resolved candidate "
                                      "rejected by class check")
                finally:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[browser] resolve failed ({page_url}): {exc}")
        return {"_no_doc": True, "_had_candidates": had_candidates}

    if resolved is None:
        print(f"[browser] {page_url}: no document resolved through any tier")
        return {"_no_doc": True, "_had_candidates": had_candidates}
    return resolved


def _browser_vision_resolve(page, query: str, domain: str | None,
                            current_url: str, acceptable_fn) -> dict | None:
    try:
        shot = page.screenshot(full_page=True, type="png")
    except Exception as exc:  # noqa: BLE001
        print(f"[browser][vision] screenshot failed: {exc}")
        return None

    import base64
    prompt = (
        "You are looking at a screenshot of a company web page. The user wants "
        f"to download this document: '{query}'. Identify the single best "
        "clickable control (link or button) on the page that leads to that "
        "document's file (PDF/DOC). Respond with ONLY JSON: "
        '{"link_text": "<exact visible text of the control, or null>"}. '
        "If nothing on the page plausibly leads to the document, return "
        '{"link_text": null}.'
    )
    try:
        resp = _bedrock.converse(
            modelId=BROWSER_VISION_MODEL_ID,
            messages=[{"role": "user", "content": [
                {"text": prompt},
                {"image": {"format": "png", "source": {"bytes": shot}}},
            ]}],
            inferenceConfig={"maxTokens": 100},
        )
        text = "".join(b.get("text", "")
                       for b in resp["output"]["message"]["content"]).strip()
        obj = _parse_llm_json(text)
        link_text = (obj or {}).get("link_text")
    except Exception as exc:  # noqa: BLE001
        print(f"[browser][vision] model call failed: {exc}")
        return None

    if not link_text or not isinstance(link_text, str):
        print("[browser][vision] model found no download control")
        return None

    print(f"[browser][vision] model chose control text: {link_text!r}")
    try:
        loc = page.get_by_text(link_text, exact=False).first
        with page.expect_download(timeout=BROWSER_CLICK_TIMEOUT_MS) as di:
            loc.click(timeout=8000)
        dl = di.value
        with open(dl.path(), "rb") as fh:
            body = fh.read()
        fname = dl.suggested_filename or ""
        if not body or len(body) > BROWSER_MAX_DOC_BYTES:
            return None
        ctype = ("application/pdf" if fname.lower().endswith(".pdf")
                 else "application/octet-stream")
        dl_url = ""
        try:
            dl_url = dl.url
        except Exception:  # noqa: BLE001
            pass
        print(f"[browser][vision] resolved via vision-guided click: "
              f"{fname or dl_url} ({len(body)} bytes)")
        return {"url": dl_url or current_url, "body": body, "ctype": ctype,
                "via": "browser_vision_click"}
    except Exception as exc:  # noqa: BLE001
        print(f"[browser][vision] click failed: {exc}")
        return None


# ─── Store (idempotent, content-addressed) ────────────────────────────────────
def _write_provenance_if_missing(item: dict) -> bool:
    if _table is None:
        return False
    try:
        _table.put_item(Item=item, ConditionExpression="attribute_not_exists(s3_key)")
        return True
    except _table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[provenance] write failed ({exc})")
        return False


def _s3_put_if_missing(key: str, body: bytes, ctype: str, meta: dict) -> None:
    if _s3 is None:
        return
    try:
        _s3.head_object(Bucket=BUCKET, Key=key)
        return
    except Exception:  # noqa: BLE001
        pass
    _s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=ctype, Metadata=meta)


PRESIGN_EXPIRY_SECONDS = int(os.environ.get("PRESIGN_EXPIRY_SECONDS", "3600"))


def _presign(s3_key: str) -> str | None:
    if _s3 is None or not BUCKET or not s3_key:
        return None
    try:
        return _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": s3_key},
            ExpiresIn=PRESIGN_EXPIRY_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[presign] failed for {s3_key}: {exc}")
        return None


def _store(company: str, run_id: str, url: str, body: bytes, ctype: str,
           title: str, query: str = "") -> dict:
    digest  = hashlib.sha256(body).hexdigest()
    s3_key  = f"{_slug(company)}/{digest[:12]}-{_safe_name(url)}"
    _s3_put_if_missing(s3_key, body, ctype,
                       {"source_url": url, "sha256": digest, "run_id": run_id})
    wrote = _write_provenance_if_missing({
        "company": _slug(company), "s3_key": s3_key, "run_id": run_id,
        "report": title or _safe_name(url), "source_url": url, "query": query,
        "hash": digest, "content_type": ctype,
        "downloaded": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "rag_status": "Pending",
    })
    return {
        "status": "stored" if wrote else "duplicate",
        "s3_key": s3_key,
        "s3_uri": f"s3://{BUCKET}/{s3_key}" if BUCKET else "(no bucket configured)",
        "download_url": _presign(s3_key),
        "source_url": url, "content_type": ctype, "sha256": digest,
        "report": title or _safe_name(url),
    }


# ─── Confidence + single-selection search ─────────────────────────────────────
def _confident(decision: dict, query: str = "") -> bool:
    if not decision.get("selected_url") or not decision.get("topic_match"):
        return False
    # v39.1: company-identity gate (default True when absent, e.g. from an
    # older decision dict shape) — a document that IS the right class but
    # belongs to a DIFFERENT company (see _llm_select_best's company_rule)
    # must not be trusted enough to store.
    if not decision.get("company_match", True):
        return False
    if _extract_year_intent(query):
        return bool(decision.get("year_match"))
    return True


def _find_best_document(search_queries: list[str], limit: int, company: str = "") -> dict:
    """
    v39 Phase 1.4: search all query variants via a bounded PARALLEL fan-out
    (_parallel_web_search) through the SAME Gateway tool, merge/dedupe by URL,
    rank + HEAD-filter, sample top candidates, then ONE grouped LLM selection
    across all of them. The actual outbound calls are still serialized by
    _throttle() (rate-limit safety) even though the fan-out is concurrent, so
    this preserves the correctness-first behavior while collapsing the search
    latency across variants. All downstream logic (merge/dedupe, junk-host
    filter, domain-mode accounting, _rank, HEAD pre-filter, sample fetch,
    _llm_select_best) is unchanged from v38.
    """
    primary = search_queries[0]
    fanout = _parallel_web_search(search_queries, limit)
    results_map: dict[str, tuple[list[dict], str]] = {}
    query_logs: list[dict] = []
    for q in search_queries:
        hits, via = fanout.get(q, ([], "not-run"))
        results_map[q] = (hits, via)
        query_logs.append({"query": q, "results_found": len(hits), "via": via})
        print(f"[find][query] {q!r} -> {len(hits)} hits via={via}")

    via = results_map.get(primary, ([], "unknown"))[1]
    by_url: dict[str, dict] = {}
    merged: list[dict] = []
    for q in search_queries:
        is_alias = q != primary
        for hit in results_map.get(q, ([], ""))[0]:
            u = hit.get("url", "")
            if not u:
                continue
            if u not in by_url:
                h = dict(hit)
                h["_from_alias"] = is_alias
                h["_source_query"] = q
                by_url[u] = h
                merged.append(h)
            elif by_url[u].get("_from_alias") and not is_alias:
                by_url[u]["_from_alias"] = False
                by_url[u]["_source_query"] = q

    qdomain = _domain(primary)
    _pre_junk_count = len(merged)
    merged = [h for h in merged if not _is_junk_host(h.get("url", ""))]
    junk_dropped = _pre_junk_count - len(merged)
    if junk_dropped:
        print(f"[find] dropped {junk_dropped} junk-host candidate(s) "
              f"(app store / media / social / aggregator subdomains — generic, "
              f"not company-specific)")

    raw_count = len(merged)
    domain_mode = "off"
    if qdomain and ENFORCE_SITE_DOMAIN:
        on_domain_hits = [h for h in merged if _host_matches(h.get("url", ""), qdomain)]
        off_domain_hits = [h for h in merged if not _host_matches(h.get("url", ""), qdomain)]
        if DOMAIN_FILTER_MODE == "hard":
            if on_domain_hits:
                merged = on_domain_hits
                domain_mode = "hard(on_domain_hits_found)"
            elif raw_count > 0:
                domain_mode = "soft_fallback(no_on_domain_hits)"
            else:
                domain_mode = "no_hits_to_filter"
        else:
            if on_domain_hits and off_domain_hits:
                domain_mode = f"soft(on_domain={len(on_domain_hits)},off_domain_kept={len(off_domain_hits)})"
            elif on_domain_hits:
                domain_mode = "soft(on_domain_only)"
            elif raw_count > 0:
                domain_mode = "soft(off_domain_only)"
            else:
                domain_mode = "no_hits_to_filter"

    probe_only = False
    if not merged and qdomain:
        root = _site_root(primary)
        if root:
            merged = [{"title": "", "url": root, "_probe": True}]
            via += "+direct-probe"
            probe_only = True

    ranked = _rank(merged, primary, qdomain)
    ranked_urls = [p.get("url", "") for _, p in ranked[:TOP_N_FOR_LLM + 4] if p.get("url")]
    print(f"[find] domain_mode={domain_mode} | HEAD pre-filtering {len(ranked_urls)} "
          f"candidates for: {primary[:80]}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        filtered = [h for h in pool.map(_head_check, ranked_urls) if h["ok"]]
    print(f"[find] {len(filtered)}/{len(ranked_urls)} passed HEAD pre-filter")

    candidate_infos: list[dict] = []
    for h in filtered[:TOP_N_FOR_LLM]:
        sample, body_bytes = "", None
        try:
            body_bytes, ctype_head = _fetch(h["url"])
            if "html" in ctype_head:
                sample = body_bytes.decode("utf-8", "ignore")[:1500]
            else:
                sample = body_bytes[:1500].decode("utf-8", "ignore").replace("\x00", " ")
            h = {**h, "head_ctype": ctype_head or h.get("head_ctype", "")}
        except Exception as exc:  # noqa: BLE001
            print(f"[find] sample fetch failed ({h['url']}): {exc}")
        candidate_infos.append({**h, "content_sample": sample, "_body": body_bytes})

    decision = (_llm_select_best(primary, candidate_infos, company=company) if candidate_infos
                else {"selected_url": None, "topic_match": False, "year_match": False,
                      "confidence": "low", "reason": "no-candidates"})

    return {
        "decision": decision, "candidate_infos": candidate_infos, "ranked": ranked,
        "via": via, "on_domain": len(merged),
        "off_domain_dropped": raw_count - len(merged), "query_logs": query_logs,
        "probe_only": probe_only, "domain_mode": domain_mode,
        "junk_dropped": junk_dropped,
    }


# ═══════════════════════════════════════════════════════════════════════════
# v39 NEW: LLM multi-query generation (Phase 1.1/1.2)
# ═══════════════════════════════════════════════════════════════════════════
def _parse_llm_json_array(text):
    text = (text or "").strip()
    text = re.sub("^```(?:json)?" + chr(92) + "s*", "", text, flags=re.I)
    text = re.sub(chr(92) + "s*```$", "", text)
    start = text.find("[")
    if start > 0:
        text = text[start:]
    return json.JSONDecoder().raw_decode(text)[0]


def _llm_generate_search_queries(query, company, domain):
    if _bedrock is None or not ENABLE_LLM_QUERY_GEN:
        return []
    cache_key = query + "||" + str(company) + "||" + str(domain)
    if cache_key in _LLM_QUERY_GEN_CACHE:
        return _LLM_QUERY_GEN_CACHE[cache_key]
    filtered_rules = _filtered_doc_rules(query)
    registries = ", ".join(FILING_REGISTRY_HOSTS[:8])
    prompt = (
        "Today is " + dt.date.today().isoformat() + ".\n"
        "You are a search-query optimizer for finding OFFICIAL company documents "
        "(annual reports, sustainability/ESG reports, governance policies, filings). "
        "Generate up to " + str(LLM_QUERY_GEN_MAX) + " DISTINCT web-search query "
        "strings that maximize the chance of finding the exact document below.\n\n"
        "Company: " + str(company) + "\n"
        "Company website domain: " + str(domain or "unknown") + "\n"
        "Original query: " + query + "\n"
        + (("Matched document-class rules: " + json.dumps(filtered_rules, ensure_ascii=True) + "\n") if filtered_rules else "")
        + "\nCreate a MIX of these query shapes (only where sensible):\n"
        "1. site:DOMAIN scoped query with the exact document class.\n"
        "2. An UNSCOPED query: COMPANY DOCUMENTCLASS YEAR filetype:pdf.\n"
        "3. An IR/investor-subdomain hint query (investors.DOMAIN, "
        "sustainability.DOMAIN, static.DOMAIN) where the file likely lives.\n"
        "4. A filing-registry-scoped query using ONE of these hosts if relevant "
        "to the company jurisdiction: " + registries + ". Example: site:REGISTRY COMPANY CLASS.\n"
        "5. Regional/naming variants of the class (annual report and accounts, "
        "integrated annual report, BRSR, 10-K, DEF 14A as appropriate).\n\n"
        "Rules: preserve any year exactly; never invent a year. Keep each query "
        "under 200 chars. No markdown. Do NOT format domains as markdown links.\n\n"
        "Output ONLY a JSON array of strings."
    )
    try:
        text = _converse(prompt, max_tokens=500)
        arr = _parse_llm_json_array(text)
        out = []
        seen = {query.strip().lower()}
        for q in arr:
            if not isinstance(q, str):
                continue
            q = _demarkdown(q.replace(chr(34), "")).strip()
            if not q or q.lower() in seen:
                continue
            seen.add(q.lower())
            out.append(q[:200])
        out = out[:LLM_QUERY_GEN_MAX]
        _LLM_QUERY_GEN_CACHE[cache_key] = out
        print("[llm-querygen] generated " + str(len(out)) + " variants for " + repr(query))
        return out
    except Exception as exc:
        print("[llm-querygen] failed (" + str(exc) + "); using regex aliases only")
        _LLM_QUERY_GEN_CACHE[cache_key] = []
        return []


# ═══════════════════════════════════════════════════════════════════════════
# v39 NEW: parallel multi-query search fan-out (Phase 1.4)
# ═══════════════════════════════════════════════════════════════════════════
def _parallel_web_search(queries, limit):
    results = {}
    if not queries:
        return results
    workers = max(1, min(SEARCH_FANOUT_WORKERS, len(queries)))

    def _one(q):
        try:
            return q, _single_web_search(q, limit)
        except Exception as exc:
            return q, ([], "error(" + type(exc).__name__ + ")")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for q, res in pool.map(_one, queries):
            results[q] = res
    return results


# ═══════════════════════════════════════════════════════════════════════════
# v39 NEW: sitemap enumeration (Phase 2)
# ═══════════════════════════════════════════════════════════════════════════
_SITEMAP_POLICY_HINTS = re.compile(
    "annual|report|sustainab|esg|policy|policies|governance|conduct|ethic|"
    "whistlebl|anti-brib|corruption|remuneration|proxy|charter|committee|"
    "tax|human-rights|modern-slavery|diversity|environment|health|safety|brsr",
    re.I)
_LOC_RE = "<loc>([^<]+)</loc>"
_SITEMAP_LINE_RE = r"sitemap:\s*(\S+)"


def _fetch_text(url, timeout):
    try:
        with urlopen(Request(url, headers=dict(_BROWSER_HEADERS)), timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception:
        return None


def _sitemap_locs(xml_text):
    return [m.group(1).strip() for m in re.finditer(_LOC_RE, xml_text, re.I)]


def _harvest_sitemap(domain, query):
    if not ENABLE_SITEMAP or not domain:
        return []
    reg = _registrable(domain)
    roots = ["https://" + domain, "https://www." + reg, "https://" + reg]
    seen_roots = set()
    sitemap_urls = []
    for root in roots:
        if root in seen_roots:
            continue
        seen_roots.add(root)
        robots = _fetch_text(root + "/robots.txt", SITEMAP_FETCH_TIMEOUT)
        if robots:
            for line in robots.splitlines():
                m = re.match(_SITEMAP_LINE_RE, line, re.I)
                if m:
                    sitemap_urls.append(m.group(1).strip())
    for root in list(seen_roots):
        sitemap_urls.append(root + "/sitemap.xml")
        sitemap_urls.append(root + "/sitemap_index.xml")
        sitemap_urls.append(root + "/sitemap-index.xml")
    sitemap_urls = list(dict.fromkeys(sitemap_urls))
    all_locs = []
    fetched = set()
    to_process = list(sitemap_urls)
    nested = 0
    while to_process and len(all_locs) < SITEMAP_MAX_URLS and nested < SITEMAP_MAX_NESTED:
        sm = to_process.pop(0)
        if sm in fetched:
            continue
        fetched.add(sm)
        nested += 1
        xml = _fetch_text(sm, SITEMAP_FETCH_TIMEOUT)
        if not xml:
            continue
        for loc in _sitemap_locs(xml):
            if loc.lower().endswith(".xml") or "sitemap" in loc.lower():
                if loc not in fetched:
                    to_process.append(loc)
            else:
                all_locs.append(loc)
        if len(all_locs) >= SITEMAP_MAX_URLS:
            break
    if not all_locs:
        return []
    cands = []
    for u in all_locs:
        if _registrable(urlparse(u).netloc) != reg:
            continue
        path = unquote(urlparse(u).path)
        if _is_doc_url(u) or _SITEMAP_POLICY_HINTS.search(path):
            cands.append(u)
    cands = list(dict.fromkeys(cands))
    ranked = _rank([{"url": u, "title": "", "snippet": ""} for u in cands], query, domain)
    out = [c["url"] for _, c in ranked[:SITEMAP_MAX_CANDIDATES]]
    print("[sitemap] " + str(domain) + ": " + str(len(all_locs)) + " urls -> "
          + str(len(cands)) + " candidates -> top " + str(len(out)))
    return out


def _sitemap_resolve(domain, query, verify_fn, known_bad=None, budget=None):
    cands = _harvest_sitemap(domain, query)
    if not cands:
        return None
    verified_hits = []
    tried = 0
    tbudget = BROWSER_MAX_VERIFY_CANDIDATES if verify_fn else 1
    for cand in cands:
        if budget is not None and not budget.time_left():
            print("[sitemap] stopped: " + budget.why_stopped())
            break
        if tried >= tbudget:
            break
        if known_bad is not None and cand in known_bad:
            continue
        try:
            cb, cc = _fetch(cand)
        except Exception as exc:
            if known_bad is not None:
                known_bad[cand] = "sitemap GET failed: " + type(exc).__name__
            continue
        if not (_is_doc_ctype(cc) or (_is_doc_url(cand) and "html" not in (cc or "").lower())):
            continue
        tried += 1
        cd = {"url": cand, "body": cb, "ctype": cc}
        if verify_fn is not None and not verify_fn(cd):
            continue
        cd["verified"] = True
        cd["_verified_for"] = query
        cd["via"] = "sitemap"
        verified_hits.append(cd)
        if verify_fn is None:
            break
    if not verified_hits:
        return None
    verified_hits.sort(key=lambda d: (max(_extract_year_intent(d["url"]))
                       if _extract_year_intent(d["url"]) else -1), reverse=True)
    best = verified_hits[0]
    print("[sitemap] resolved: " + best["url"] + " (" + str(len(best["body"])) + " bytes)")
    return best


# ─── Entrypoint ───────────────────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload: dict, context=None) -> dict:
    loop = asyncio.get_event_loop()
    ping_task = None
    if context is not None:
        async def _ping_loop():
            while True:
                await asyncio.sleep(20)
                try:
                    await context.ping(status="HEALTHY_BUSY")
                except Exception:  # noqa: BLE001
                    pass
        ping_task = asyncio.create_task(_ping_loop())
    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = await loop.run_in_executor(pool, _invoke_sync, payload)
        return result
    finally:
        if ping_task:
            ping_task.cancel()


def _deep_static_crawl(seed_url: str, domain: str | None, query: str,
                       verify_fn, known_bad: dict | None = None,
                       budget: "_QueryBudget | None" = None) -> dict | None:
    """Fix D/E: recursively fetch same-domain HTML landing pages, regex-crawl
    each for document links, class-verify every doc, return the most-recent
    verified match."""
    if not DEEP_STATIC_CRAWL:
        return None
    visited: set[str] = set()
    frontier = [(seed_url, 0)]
    pages = 0
    verified_hits: list[dict] = []
    while frontier and pages < DEEP_STATIC_MAX_PAGES:
        url, depth = frontier.pop(0)
        if budget is not None and not budget.time_left():
            print(f"[deep-crawl] stopped: {budget.why_stopped()}")
            break
        k = url.rstrip("/")
        if k in visited:
            continue
        visited.add(k)
        pages += 1
        try:
            body, ctype = _fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"[deep-crawl] fetch failed ({url}): {exc}")
            continue
        if "html" not in (ctype or "").lower():
            continue
        cands, _ = _page_doc_candidates(body, url, query)
        doc_cands = [u for u in cands if _is_doc_url(u) and not _is_junk_host(u)]

        def _uy(u):
            ys = _extract_year_intent(u)
            return max(ys) if ys else -1

        doc_cands = sorted(doc_cands, key=lambda u: (_verify_priority(u, query), _uy(u)),
                           reverse=True)
        print(f"[deep-crawl] {url} (depth={depth}, page={pages}): "
              f"{len(doc_cands)} doc candidates")
        for cand in doc_cands:
            if known_bad is not None and cand in known_bad:
                continue
            try:
                cb, cc = _fetch(cand)
            except Exception as exc:  # noqa: BLE001
                if known_bad is not None:
                    known_bad[cand] = f"deep GET failed: {type(exc).__name__}"
                continue
            if not (_is_doc_ctype(cc) or (_is_doc_url(cand)
                    and "html" not in (cc or "").lower())):
                continue
            cd = {"url": cand, "body": cb, "ctype": cc}
            if verify_fn is not None and not verify_fn(cd):
                continue
            cd["verified"] = True
            cd["_verified_for"] = query
            cd["via"] = "deep_static_crawl"
            verified_hits.append(cd)
        if verified_hits:
            break
        if depth < DEEP_STATIC_MAX_DEPTH:
            for sub in _subpage_links(body, url, query, domain or _domain(query) or ""):
                if sub.rstrip("/") not in visited:
                    frontier.append((sub, depth + 1))
    if not verified_hits:
        return None
    verified_hits.sort(
        key=lambda d: (max(_extract_year_intent(d["url"]))
                       if _extract_year_intent(d["url"]) else -1), reverse=True)
    best = verified_hits[0]
    print(f"[deep-crawl] resolved: {best['url']} ({len(best['body'])} bytes)")
    return best


def _invoke_sync(payload: dict) -> dict:
    run_id = (payload or {}).get("run_id") or uuid.uuid4().hex[:8]

    queries = {k.strip(): v for k, v in (payload or {}).items()
               if re.match(r"web_query\d+$", k.strip(), re.I) and v and str(v).strip()}
    if not queries:
        return {"error": "no web_query<N> fields found in payload", "run_id": run_id}

    company_raw = (payload or {}).get("company") or "unknown"
    company = _slug(company_raw)

    region_override = (payload or {}).get("alias_region")

    diag = {
        "search_via": None, "raw_hits": 0, "current_year": CURRENT_YEAR,
        "llm": bool(LLM_MODEL_ID), "alias_mode": ALIAS_MODE,
        "selection_model_id": SELECTION_MODEL_ID or LLM_MODEL_ID,
        "enforce_site_domain": ENFORCE_SITE_DOMAIN,
        "domain_filter_mode": DOMAIN_FILTER_MODE,
        "alias_searches": MAX_ALIAS_SEARCHES, "search_all_aliases": SEARCH_ALL_ALIASES,
        "alias_region_resolution": "payload_override > tld_inference > env_default",
        "alias_region_env_default": _normalize_alias_region(ALIAS_REGION),
        "search_backend": "agentcore_gateway_websearch_only",
        "browser_fallback": USE_BROWSER,
        "browser_vision_enabled": bool(BROWSER_VISION_MODEL_ID),
        "browser_max_verify_candidates": BROWSER_MAX_VERIFY_CANDIDATES,
        "v39_llm_query_gen": ENABLE_LLM_QUERY_GEN,
        "v39_search_fanout_workers": SEARCH_FANOUT_WORKERS,
        "v39_sitemap": ENABLE_SITEMAP,
        "v39_filing_fallback_all_classes": FILING_FALLBACK_ALL_CLASSES,
        "per_query": [],
    }

    stored_by_url:  dict[str, dict] = {}
    stored_by_hash: dict[str, dict] = {}
    stored:     list[dict] = []
    duplicates: list[dict] = []
    failures:   list[dict] = []
    manifest: list[dict] = []
    best_effort: list[dict] = []
    done_queries: set[str] = set()
    domain: str | None = None

    def _record(base_log: dict, status: str, rec: dict | None = None,
                source_url: str | None = None, best: dict | None = None) -> None:
        row = {
            "query": base_log.get("query"),
            "raw": base_log.get("raw"),
            "status": status,
            "stage": base_log.get("stage"),
            "matched_doc_classes": base_log.get("matched_doc_classes"),
            "s3_key": None, "s3_uri": None, "download_url": None,
            "source_url": source_url,
            "report": None,
        }
        if rec is not None:
            row.update({
                "s3_key": rec.get("s3_key"),
                "s3_uri": rec.get("s3_uri"),
                "download_url": rec.get("download_url") or _presign(rec.get("s3_key")),
                "source_url": source_url or rec.get("source_url"),
                "report": rec.get("report"),
                "sha256": rec.get("sha256"),
            })
        if best is not None:
            row["best_effort"] = best
        manifest.append(row)

    _root_crawl_cache: dict[str, dict] = {}
    _known_bad: dict[str, str] = {}

    for raw in queries.values():
        prepared = _prepare_query(str(raw))
        if not prepared or prepared in done_queries:
            continue
        done_queries.add(prepared)
        _budget = _QueryBudget()
        query_domain = _domain(prepared)
        if domain is None:
            domain = query_domain

        inferred_region = _infer_alias_region_from_domain(query_domain)
        if region_override:
            effective_region = _normalize_alias_region(region_override)
            region_source = "payload_override"
        elif inferred_region:
            effective_region = inferred_region
            region_source = f"tld_inference({query_domain})"
        else:
            effective_region = _normalize_alias_region(ALIAS_REGION)
            region_source = "env_default(direct_website)"

        matched_classes = [c for c, _ in _matched_doc_classes(prepared)]

        # ── Stage 1: primary + v39 LLM-generated + alias fan-out (Phase 1.5) ──
        # The primary prepared query stays first (it's the "primary" for domain
        # scoping and selection); the LLM-optimized variants and regex alias
        # variants are appended and deduped, then ALL are searched in one
        # parallel fan-out inside _find_best_document.
        stage1_queries = [prepared]
        _seen_s1 = {prepared.strip().lower()}
        stage1_generated: list[str] = []
        for _q in (_llm_generate_search_queries(prepared, company_raw, query_domain)
                   + _alias_queries(prepared, region=effective_region)):
            kk = (_q or "").strip().lower()
            if _q and kk not in _seen_s1:
                _seen_s1.add(kk)
                stage1_queries.append(_q)
                stage1_generated.append(_q)
        attempt = _find_best_document(stage1_queries, MAX_RESULTS, company=company_raw)
        stage = "primary"
        diag["search_via"] = attempt["via"]
        diag["raw_hits"] += attempt["on_domain"]

        # ── Stage 2: synonym/alias fallback (same Gateway tool, different text) ──
        generated_aliases: list[str] = list(stage1_generated)
        primary_weak = (not _confident(attempt["decision"], prepared)
                        or attempt.get("probe_only"))
        if primary_weak and ALIAS_MODE != "off":
            extra_aliases = _alias_queries(prepared, region=effective_region)
            extra_aliases = [a for a in extra_aliases
                             if a.strip().lower() not in _seen_s1]
            if extra_aliases:
                for a in extra_aliases:
                    _seen_s1.add(a.strip().lower())
                generated_aliases += extra_aliases
                fb = _find_best_document([prepared] + extra_aliases, MAX_RESULTS, company=company_raw)
                fb_better = _confident(fb["decision"], prepared) and not fb.get("probe_only")
                primary_had_doc = (attempt["decision"].get("selected_url")
                                   and not attempt.get("probe_only"))
                if fb_better or (fb["decision"].get("selected_url") and not primary_had_doc):
                    attempt, stage = fb, "alias_fallback"

        decision = attempt["decision"]
        selected_url = decision.get("selected_url")

        # ── Stage 3: unscoped filing-search fallback (v39 Phase 1.2 broadened) ──
        # When FILING_FALLBACK_ALL_CLASSES is on, this triggers for EVERY
        # matched document class (not just the original 4); when no class
        # matched, it falls back to the bare query text.
        filing_fallback_query: str | None = None
        _filing_gate = (
            ENABLE_FILING_FALLBACK and not _confident(decision, prepared)
            and (FILING_FALLBACK_ALL_CLASSES
                 or any(c in FILING_FALLBACK_DOC_CLASSES for c in matched_classes)))
        if _filing_gate:
            if FILING_FALLBACK_ALL_CLASSES:
                fallback_class = (matched_classes[0] if matched_classes
                                  else _strip_site(prepared).strip())
            else:
                fallback_class = next(c for c in matched_classes
                                      if c in FILING_FALLBACK_DOC_CLASSES)
            company_name = str((payload or {}).get("company") or company_raw or "").strip()
            year_hint = ""
            target_years = _extract_year_intent(prepared)
            if target_years:
                year_hint = f" {max(target_years)}"
            if company_name and fallback_class:
                filing_query_suffix = (
                    "official pdf document" if fallback_class in _NON_FILING_HUB_CLASSES
                    else "official filing"
                )
                filing_fallback_query = f"{company_name} {fallback_class}{year_hint} {filing_query_suffix}"
                fb2 = _find_best_document([filing_fallback_query], MAX_RESULTS, company=company_raw)
                if _confident(fb2["decision"], filing_fallback_query) and not fb2.get("probe_only"):
                    attempt, stage = fb2, "unscoped_filing_fallback"
                    decision = attempt["decision"]
                    selected_url = decision.get("selected_url")

        base_log = {
            "query": prepared, "raw": str(raw).strip(), "via": attempt["via"],
            "stage": stage, "on_domain": attempt["on_domain"],
            "off_domain_dropped": attempt["off_domain_dropped"],
            "junk_dropped": attempt.get("junk_dropped", 0),
            "probe_only": attempt.get("probe_only", False),
            "domain_mode": attempt.get("domain_mode", "unknown"),
            "matched_doc_classes": matched_classes,
            "alias_region_used": effective_region,
            "alias_region_source": region_source,
            "generated_alias_queries": generated_aliases,
            "filing_fallback_query": filing_fallback_query,
            "llm_decision": decision,
            "ranked": [{"score": s, "url": c["url"]} for s, c in attempt["ranked"][:6]],
        }

        if not _confident(decision, prepared):
            # ── v39 Phase 2.3 / Phase 4: sitemap enumeration FIRST ──
            # Ordered fallback chain: sitemap -> deep static crawl -> browser
            # root nav, every tier gated by _make_browser_verify_fn.
            sitemap_saved = False
            if ENABLE_SITEMAP:
                _verify_fn = _make_browser_verify_fn(prepared, budget=_budget, company=company_raw)
                sm = _sitemap_resolve(query_domain or _domain(prepared), prepared,
                                      _verify_fn, known_bad=_known_bad, budget=_budget)
                if sm and sm.get("body"):
                    rec = _store(company, run_id, sm["url"], sm["body"],
                                 sm["ctype"], "", prepared)
                    rec["queries"] = [prepared]
                    rec["stage"] = "sitemap"
                    digest = rec["sha256"]
                    if digest not in stored_by_hash and sm["url"] not in stored_by_url:
                        stored_by_url[sm["url"]] = rec
                        stored_by_hash[digest] = rec
                        (stored if rec["status"] == "stored" else duplicates).append(rec)
                    print(f"[store] {rec['status'].upper()} (sitemap): "
                          f"{prepared!r} -> {rec['s3_key']}")
                    base_log["status"] = ("ok" if rec["status"] == "stored"
                                          else "duplicate_existing")
                    base_log["resolved_via"] = "sitemap"
                    base_log["documents"] = [rec["s3_key"]]
                    diag["per_query"].append(base_log)
                    _record(base_log, base_log["status"], rec=rec, source_url=sm["url"])
                    sitemap_saved = True
            if sitemap_saved:
                continue

            # Fix D/E: deep static crawl of the best landing-page candidate.
            saved_via_deep = False
            if DEEP_STATIC_CRAWL and attempt.get("ranked"):
                _verify_fn = _make_browser_verify_fn(prepared, budget=_budget, company=company_raw)
                for _, top_c in attempt["ranked"][:5]:
                    seed = top_c.get("url")
                    if not seed or _is_junk_host(seed):
                        continue
                    dc = _deep_static_crawl(seed, query_domain or _domain(prepared),
                                            prepared, _verify_fn, _known_bad, budget=_budget)
                    if dc and dc.get("body"):
                        rec = _store(company, run_id, dc["url"], dc["body"],
                                     dc["ctype"], "", prepared)
                        rec["queries"] = [prepared]
                        rec["stage"] = "deep_static_crawl"
                        digest = rec["sha256"]
                        if digest not in stored_by_hash and dc["url"] not in stored_by_url:
                            stored_by_url[dc["url"]] = rec
                            stored_by_hash[digest] = rec
                            (stored if rec["status"] == "stored" else duplicates).append(rec)
                        print(f"[store] {rec['status'].upper()} (deep-crawl): "
                              f"{prepared!r} -> {rec['s3_key']}")
                        base_log["status"] = ("ok" if rec["status"] == "stored"
                                              else "duplicate_existing")
                        base_log["resolved_via"] = "deep_static_crawl"
                        base_log["documents"] = [rec["s3_key"]]
                        diag["per_query"].append(base_log)
                        _record(base_log, base_log["status"], rec=rec, source_url=dc["url"])
                        saved_via_deep = True
                        break
            if saved_via_deep:
                continue

            # Last-resort: render the site root in the browser (now IR-aware).
            browser_saved = False
            if USE_BROWSER:
                root = _site_root(prepared)
                if root:
                    _verify_fn = _make_browser_verify_fn(prepared, budget=_budget, company=company_raw)
                    br = _browser_resolve_document(
                        root, query_domain or _domain(prepared), prepared,
                        cache=_root_crawl_cache, verify_fn=_verify_fn,
                        known_bad=_known_bad, budget=_budget)
                    if br and br.get("body"):
                        accept = br.get("verified", False) or _verify_fn is None
                        base_log["browser_verify_decision"] = br.get("_verify_decision")
                        if accept:
                            rec = _store(company, run_id, br["url"], br["body"],
                                         br["ctype"], "", prepared)
                            rec["queries"] = [prepared]
                            rec["stage"] = "browser_root_crawl"
                            digest = rec["sha256"]
                            if digest not in stored_by_hash and br["url"] not in stored_by_url:
                                stored_by_url[br["url"]] = rec
                                stored_by_hash[digest] = rec
                                (stored if rec["status"] == "stored" else duplicates).append(rec)
                            print(f"[store] {rec['status'].upper()} (root-crawl): "
                                  f"{prepared!r} -> {rec['s3_key']} "
                                  f"(sha256={rec['sha256'][:12]}...)")
                            base_log["status"] = ("ok" if rec["status"] == "stored"
                                                  else "duplicate_existing")
                            base_log["resolved_via"] = br.get("via", "browser_root_crawl")
                            base_log["documents"] = [rec["s3_key"]]
                            diag["per_query"].append(base_log)
                            _record(base_log, base_log["status"], rec=rec,
                                    source_url=br.get("url"))
                            browser_saved = True
            if browser_saved:
                continue

            be = None
            ranked_hits = attempt.get("ranked") or []
            if ranked_hits:
                top_score, top_c = ranked_hits[0]
                be = {
                    "candidate_url": top_c.get("url"),
                    "rank_score": top_score,
                    "llm_reason": (decision or {}).get("reason"),
                    "llm_confidence": (decision or {}).get("confidence"),
                    "note": "UNVERIFIED best guess — did NOT pass fail-closed "
                            "class check; not stored to corpus",
                }
                best_effort.append({"query": prepared, **be})
                print(f"[best-effort] {prepared!r}: closest unverified candidate "
                      f"{top_c.get('url')} (score={top_score}, "
                      f"conf={(decision or {}).get('confidence')})")
            base_log["status"] = "no_document_found"
            base_log["documents"] = []
            diag["per_query"].append(base_log)
            _record(base_log, "no_document_found", best=be)
            continue

        doc_body  = next((c.get("_body") for c in attempt["candidate_infos"]
                          if c["url"] == selected_url), None)
        doc_ctype = next((c.get("head_ctype", "") for c in attempt["candidate_infos"]
                          if c["url"] == selected_url), "")
        if doc_body is None:
            try:
                doc_body, doc_ctype = _fetch(selected_url)
            except Exception as exc:  # noqa: BLE001
                failures.append({"query": prepared, "source_url": selected_url,
                                 "status": "failed", "error": str(exc)})
                base_log["status"] = "fetch_failed"
                diag["per_query"].append(base_log)
                _record(base_log, "fetch_failed", source_url=selected_url)
                continue

        if "html" in (doc_ctype or "") and doc_body and _is_doc_url(selected_url):
            block_marker = _looks_like_block_page(
                doc_body[:20000].decode("utf-8", "ignore") if doc_body else None)
            print(f"[warn] selected_url has a document extension but urllib "
                  f"fetch returned HTML ({len(doc_body)} bytes) — likely a "
                  f"WAF/redirect/interstitial, not a real landing page: "
                  f"{selected_url}"
                  + (f" | possible block marker: {block_marker!r}" if block_marker else ""))
            base_log["doc_url_returned_html"] = True
            if block_marker:
                base_log["possible_block_marker"] = block_marker

        if "html" in (doc_ctype or "") and doc_body:
            cands, _ = _page_doc_candidates(doc_body, selected_url, prepared)
            doc_cands = [u for u in cands if _is_doc_url(u) and not _is_junk_host(u)]
            def _url_year(u: str) -> int:
                ys = _extract_year_intent(u)
                return max(ys) if ys else -1
            doc_cands = sorted(doc_cands,
                               key=lambda u: (_verify_priority(u, prepared), _url_year(u)),
                               reverse=True)
            resolved = False
            _static_verify_fn = _make_browser_verify_fn(prepared, budget=_budget, company=company_raw)
            _static_budget = BROWSER_MAX_VERIFY_CANDIDATES if _static_verify_fn else 1
            _static_tried = 0
            for cand in doc_cands:
                if _static_tried >= _static_budget:
                    print(f"[html-crawl] verify budget ({_static_budget}) "
                          f"exhausted for {selected_url}")
                    break
                try:
                    cand_body, cand_ctype = _fetch(cand)
                except Exception as exc:  # noqa: BLE001
                    print(f"[html-crawl] fetch failed ({cand}): {exc}")
                    continue
                if not (_is_doc_ctype(cand_ctype) or (
                    _is_doc_url(cand) and "html" not in (cand_ctype or "").lower()
                )):
                    continue
                _static_tried += 1
                if _static_verify_fn is not None:
                    cand_doc = {"url": cand, "body": cand_body, "ctype": cand_ctype}
                    if not _static_verify_fn(cand_doc):
                        print(f"[html-crawl] candidate {_static_tried}/{_static_budget} "
                              f"wrong class ({cand}); trying next candidate")
                        continue
                doc_body, doc_ctype, selected_url = cand_body, cand_ctype, cand
                resolved = True
                _cy = _url_year(cand)
                print(f"[html-crawl] resolved via static regex crawl: "
                      f"{cand} ({len(cand_body)} bytes, {cand_ctype}"
                      + (f", year={_cy}" if _cy >= 0 else "") + ")")
                break
            if not resolved and not doc_cands:
                print(f"[html-crawl] no document-file links found on landing "
                      f"page via static regex crawl: {selected_url} "
                      f"({len(doc_cands)} candidates after junk-host filter)")
            elif not resolved and doc_cands:
                print(f"[html-crawl] {len(doc_cands)} document-file link(s) "
                      f"found on {selected_url} but none passed class "
                      f"verification or fetch: falling through to browser fallback")

            if not resolved and USE_BROWSER:
                _verify_fn = _make_browser_verify_fn(prepared, budget=_budget, company=company_raw)
                br = _browser_resolve_document(
                    selected_url, query_domain or _domain(prepared), prepared,
                    verify_fn=_verify_fn, known_bad=_known_bad, budget=_budget)
                if br and br.get("body"):
                    accept = br.get("verified", False) or _verify_fn is None
                    base_log["browser_verify_decision"] = br.get("_verify_decision")
                    if accept:
                        doc_body = br["body"]
                        doc_ctype = br["ctype"]
                        selected_url = br["url"]
                        resolved = True
                        base_log["resolved_via"] = br.get("via", "browser")

            if not resolved:
                base_log["status"] = "html_only_no_pdf"
                base_log["source_url"] = selected_url
                base_log["documents"] = []
                diag["per_query"].append(base_log)
                _record(base_log, "html_only_no_pdf", source_url=selected_url)
                continue

        digest = hashlib.sha256(doc_body).hexdigest()

        if digest in stored_by_hash:
            print(f"[store] duplicate WITHIN this run (same bytes already "
                  f"stored by an earlier query in this invocation): "
                  f"{stored_by_hash[digest]['s3_key']}")
            stored_by_hash[digest]["queries"].append(prepared)
            base_log["status"] = "duplicate_in_run"
            base_log["documents"] = [stored_by_hash[digest]["s3_key"]]
            diag["per_query"].append(base_log)
            _record(base_log, "duplicate_in_run", rec=stored_by_hash[digest],
                    source_url=selected_url)
            continue
        if selected_url in stored_by_url:
            print(f"[store] duplicate WITHIN this run (same URL already "
                  f"stored by an earlier query in this invocation): "
                  f"{stored_by_url[selected_url]['s3_key']}")
            stored_by_url[selected_url]["queries"].append(prepared)
            base_log["status"] = "duplicate_in_run"
            base_log["documents"] = [stored_by_url[selected_url]["s3_key"]]
            diag["per_query"].append(base_log)
            _record(base_log, "duplicate_in_run", rec=stored_by_url[selected_url],
                    source_url=selected_url)
            continue

        rec = _store(company, run_id, selected_url, doc_body, doc_ctype, "", prepared)
        rec["queries"] = [prepared]
        rec["stage"] = stage
        stored_by_url[selected_url] = rec
        stored_by_hash[digest] = rec
        (stored if rec["status"] == "stored" else duplicates).append(rec)
        print(f"[store] {rec['status'].upper()}: {prepared!r} -> "
              f"{rec['s3_key']} (sha256={rec['sha256'][:12]}...)")

        base_log["status"] = "ok" if rec["status"] == "stored" else "duplicate_existing"
        base_log["documents"] = [rec["s3_key"]]
        diag["per_query"].append(base_log)
        _record(base_log, base_log["status"], rec=rec, source_url=selected_url)

    if company == "unknown" and domain:
        company = _slug(domain.split(".")[0])

    diag["gateway_debug"] = _GW_DEBUG
    status_counts: dict[str, int] = {}
    for pq in diag["per_query"]:
        s = pq.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    total_q = len(done_queries) or 1
    verified_outcomes = {"ok", "duplicate_existing", "duplicate_in_run"}
    resolved_total = sum(status_counts.get(s, 0) for s in verified_outcomes)
    correctness_pct = round(100.0 * resolved_total / total_q, 1)
    print(f"[run summary] company={company} queries={len(done_queries)} "
          f"new_stored={len(stored)} duplicate_existing={len(duplicates)} "
          f"resolved_total={resolved_total} best_effort={len(best_effort)} "
          f"fetch_failures={len(failures)} "
          f"verified_coverage={correctness_pct}% status_breakdown={status_counts}")
    return {
        "run_id": run_id, "company": company, "domain": domain,
        "bucket": BUCKET, "count": len(stored),
        "resolved_total": resolved_total,
        "verified_coverage_pct": correctness_pct,
        "downloaded": stored, "duplicates": duplicates,
        "failures": failures,
        "manifest": manifest,
        "best_effort": best_effort,
        "diagnostics": diag,
    }


if __name__ == "__main__":
    app.run()