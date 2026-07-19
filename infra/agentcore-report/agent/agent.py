"""Report download agent (single AgentCore Runtime) — v45.

v45 keeps annual reports and proxy statements on the official-company path
first: direct Google URL search -> official sitemap/landing-page crawl -> deep
static crawl -> browser navigation. A validated CIK is used on SEC EDGAR only
when the official-company path does not produce the requested document.

v44 made discovery identity-first and official-domain scoped. Ticker aliases
enrich Google probes while CIKs are consumed directly by SEC EDGAR.

v43 added Google-grounded Vertex company-identity hints with official SEC
validation, registry-first annual/proxy resolution, hard official-domain web
filtering, stable per-query result IDs, class-scoped S3 keys, and browser
navigation-to-archive handling.

v42 bridges relevant HTML landing pages discovered through sitemaps to the
documents they link, reserves search sampling capacity for official-domain
results, and rejects LLM query variants that invent or drop report years.

v41 adds completed-fiscal-year targeting for undated Annual Report requests,
browser-first resolution for filing classes, reserved cross-tier verification
capacity, bounded static candidates, and investor-page-attested CDN discovery.

v40 builds on v39's Gateway/Vertex search + deterministic AgentCore Browser DOM
fallback and restructures the resolver into an explicit, ordered tier chain:

  Tier 1  Google (Vertex grounded) web search + synonym/alias fan-out.
  Tier 2  Official registry fallback (SEC EDGAR + Companies House) via the
          registry_tier module — EDGAR for annual reports (10-K/20-F/40-F) and
          proxy statements (DEF 14A), sustainability best-effort; Companies
          House for annual accounts only. Every registry hit still passes the
          SAME fail-closed class verifier.
  Tier 3  Sitemap enumeration -> deep static crawl.
  Tier 4  AgentCore Browser (Playwright) — JS nav, year selection, menu expand,
          download-control identification. Gated per-run by browser_enabled.

Per-class discovery + validation is unified in report_specs.py; Tier 2 lives in
registry_tier.py. Input accepts a structured {company{}, reports[]} payload and
still normalizes the legacy web_query<N> shape. Every stored document is written
with a <key>.metadata.json sidecar for the downstream Knowledge Base.

All v39.1 fail-closed fixes are preserved: press-release filter, per-query
rejected set, cross-company gate, capped-recency year alignment.
"""

import asyncio
import concurrent.futures
import datetime as dt
import ipaddress
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
from botocore.config import Config
from bedrock_agentcore.runtime import BedrockAgentCoreApp

import report_specs
import registry_tier

app = BedrockAgentCoreApp()

CODE_VERSION = os.environ.get("CODE_VERSION", "v45")

# Tier 2 (official registry fallback) master switch. registry_tier.py reads its
# own EDGAR_* / CH_* configuration from the environment.
ENABLE_REGISTRY_TIER = os.environ.get("ENABLE_REGISTRY_TIER", "true").lower() != "false"
registry_tier.set_logger(print)

# ─── Environment ──────────────────────────────────────────────────────────────
BUCKET            = os.environ.get("REPORTS_BUCKET", "")
PROVENANCE_TABLE  = os.environ.get("PROVENANCE_TABLE", "")
REGION            = os.environ.get("APP_REGION") or os.environ.get("AWS_REGION", "us-east-1")
GATEWAY_URL       = os.environ.get("GATEWAY_URL", "")
GATEWAY_SEARCH_TOOL = os.environ.get("GATEWAY_SEARCH_TOOL", "").strip()
GATEWAY_STRIP_SITE = os.environ.get("GATEWAY_STRIP_SITE", "true").lower() != "false"
MAXIMIZE_RECALL = os.environ.get("MAXIMIZE_RECALL", "true").lower() != "false"

# ── Tier 2 search backend: "vertex_lambda" (isolated Vertex Lambda) or "gateway" ──
SEARCH_BACKEND = os.environ.get("SEARCH_BACKEND", "gateway").strip().lower()
LAMBDA_SEARCH_FUNCTION = os.environ.get("LAMBDA_SEARCH_FUNCTION", "").strip()
LAMBDA_INVOKE_TIMEOUT = int(os.environ.get("LAMBDA_INVOKE_TIMEOUT", "120"))
VERTEX_FALLBACK_TO_GATEWAY = os.environ.get("VERTEX_FALLBACK_TO_GATEWAY", "false").lower() == "true"
ENABLE_VERTEX_IDENTITY = os.environ.get(
    "ENABLE_VERTEX_IDENTITY", "true").lower() != "false"
REQUIRE_VALIDATED_REGISTRY_IDENTITY = os.environ.get(
    "REQUIRE_VALIDATED_REGISTRY_IDENTITY", "true").lower() != "false"
REQUIRE_OFFICIAL_DOMAIN_FOR_WEB = os.environ.get(
    "REQUIRE_OFFICIAL_DOMAIN_FOR_WEB", "true").lower() != "false"
REGISTRY_FIRST_CLASSES = {
    c.strip().lower()
    for c in os.environ.get(
        "REGISTRY_FIRST_CLASSES", "").split(",")
    if c.strip()
}

DEEP_STATIC_CRAWL = os.environ.get("DEEP_STATIC_CRAWL", "true").lower() != "false"
DEEP_STATIC_MAX_DEPTH = int(os.environ.get("DEEP_STATIC_MAX_DEPTH", "3"))
DEEP_STATIC_MAX_PAGES = int(os.environ.get("DEEP_STATIC_MAX_PAGES", "100"))
DEEP_STATIC_MAX_DOC_CANDIDATES_PER_PAGE = int(os.environ.get(
    "DEEP_STATIC_MAX_DOC_CANDIDATES_PER_PAGE", "20"))

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
BROWSER_RESERVED_VERIFIES = int(os.environ.get("BROWSER_RESERVED_VERIFIES", "20"))
BROWSER_MAX_CLICK_ATTEMPTS = int(os.environ.get("BROWSER_MAX_CLICK_ATTEMPTS", "12"))
BROWSER_RESOLVE_MAX_SECONDS = float(os.environ.get("BROWSER_RESOLVE_MAX_SECONDS", "1800"))
BROWSER_VERIFY_CLASS = os.environ.get("BROWSER_VERIFY_CLASS", "true").lower() != "false"
BROWSER_VISION_MODEL_ID = os.environ.get("BROWSER_VISION_MODEL_ID", "").strip()
BROWSER_SKIP_CLICK_ON_BLOCK = os.environ.get("BROWSER_SKIP_CLICK_ON_BLOCK", "true").lower() != "false"

MAX_RESULTS       = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "10"))
BEST_MATCHES      = int(os.environ.get("BEST_MATCHES", "1"))
DOC_ONLY          = os.environ.get("DOC_ONLY", "true").lower() != "false"
CURRENT_YEAR      = dt.date.today().year
LATEST_COMPLETED_FISCAL_YEAR_LAG = int(os.environ.get(
    "LATEST_COMPLETED_FISCAL_YEAR_LAG", "1"))
LATEST_COMPLETED_FISCAL_YEAR_CLASSES = {
    c.strip().lower()
    for c in os.environ.get(
        "LATEST_COMPLETED_FISCAL_YEAR_CLASSES", "annual report",
    ).split(",")
    if c.strip()
}
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
SEARCH_OFFICIAL_DOMAIN_RESERVE = int(os.environ.get(
    "SEARCH_OFFICIAL_DOMAIN_RESERVE", "3"))
ALIAS_HIT_BOOST   = int(os.environ.get("ALIAS_HIT_BOOST", "1"))
ENABLE_LLM_CLASS_MATCH = os.environ.get("ENABLE_LLM_CLASS_MATCH", "true").lower() != "false"
ENFORCE_COMPANY_SAMPLE_EVIDENCE = os.environ.get(
    "ENFORCE_COMPANY_SAMPLE_EVIDENCE", "true").lower() != "false"
COMPANY_SAMPLE_MIN_CHARS = int(os.environ.get("COMPANY_SAMPLE_MIN_CHARS", "40"))
MIN_SELECTION_CONFIDENCE = os.environ.get(
    "MIN_SELECTION_CONFIDENCE", "high").strip().lower()

_bedrock = boto3.client("bedrock-runtime", region_name=REGION) if LLM_MODEL_ID else None
_s3      = boto3.client("s3", region_name=REGION) if BUCKET else None
__table   = boto3.resource("dynamodb", region_name=REGION).Table(PROVENANCE_TABLE) if PROVENANCE_TABLE else None
_table   = __table
_lambda  = (boto3.client(
                "lambda", region_name=REGION,
                config=Config(read_timeout=LAMBDA_INVOKE_TIMEOUT,
                              connect_timeout=10,
                              retries={"max_attempts": 2}))
            if LAMBDA_SEARCH_FUNCTION else None)

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
SITEMAP_MAX_LANDING_PAGES = int(os.environ.get(
    "SITEMAP_MAX_LANDING_PAGES", "12"))
SITEMAP_MAX_DOC_LINKS_PER_PAGE = int(os.environ.get(
    "SITEMAP_MAX_DOC_LINKS_PER_PAGE", "30"))
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
_VERTEX_IDENTITY_CACHE: dict[str, dict] = {}
_VERTEX_IDENTITY_CACHE_LOCK = threading.Lock()


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

_EDGAR_UA = os.environ.get(
    "EDGAR_USER_AGENT",
    "EDO-CoAnalyst/1.0 compliance-research askdevopscloud@spglobal.com")

def _ua_for(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return UA
    return _EDGAR_UA if host.endswith("sec.gov") else UA

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
    "proxy statement": {
        "aliases_by_region": {
            "uk_europe": [
                "proxy statement",
                "notice of annual general meeting",
            ],
            "india": [
                "proxy statement",
                "notice of annual general meeting",
            ],
            "us": [
                "proxy statement",
                "definitive proxy statement",
                "def 14a",
                "notice of annual meeting and proxy statement",
            ],
        },
        "reject": [
            "preliminary proxy statement",
        ],
    },
    "remuneration report": {
        "aliases_by_region": {
            "uk_europe": [
                "remuneration report",
                "directors' remuneration report",
                "directors remuneration report",
                "compensation report",
            ],
            "india": [
                "remuneration report",
                "compensation report",
            ],
            "us": [
                "remuneration report",
                "compensation discussion and analysis",
                "executive compensation report",
            ],
        },
        "reject": [
            "remuneration policy",
        ],
    },
}

# ── Vocabulary parity check (fail loud at import, not silently at runtime) ──
# The proxy-statement bug existed because _DOC_CLASS_RULES (this file) and
# report_specs.REPORT_SPECS had divergent key lists: a class in one but not the
# other silently loses either alias/discovery + Tier-2-for-legacy-payloads or
# its per-class validation contract + registry routing. This surfaces any
# future drift as one grep-able log line at container start.
def _check_class_vocabulary_parity() -> None:
    rules = set(_DOC_CLASS_RULES)
    specs = set(getattr(report_specs, "REPORT_SPECS", {}))
    only_specs = sorted(specs - rules)
    only_rules = sorted(rules - specs)
    if only_specs:
        print(f"[warn][class-parity] in report_specs.REPORT_SPECS but NOT in "
              f"_DOC_CLASS_RULES (no alias/discovery; no Tier 2 for legacy "
              f"web_query payloads): {only_specs}")
    if only_rules:
        print(f"[warn][class-parity] in _DOC_CLASS_RULES but NOT in "
              f"report_specs.REPORT_SPECS (no per-class validation contract or "
              f"registry routing): {only_rules}")
    if not (only_specs or only_rules):
        print(f"[class-parity] OK — {len(rules)} document classes aligned "
              f"across _DOC_CLASS_RULES and report_specs.REPORT_SPECS")

_check_class_vocabulary_parity()


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
def _clean_domain(value: str | None) -> str:
    raw = _demarkdown(str(value or "")).strip("[]() /")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if not host or "." not in host:
        return ""
    return host[4:] if host.startswith("www.") else host


def _domain(query: str) -> str | None:
    m = re.search(r"site:\s*(\S+)", query or "")
    if not m:
        return None
    return _clean_domain(m.group(1)) or None


def _strip_site(q: str) -> str:
    return re.sub(r"site:\s*\S+", "", q or "").strip()


def _scope_to_official_domain(query: str, domain: str | None) -> str:
    """Replace any user/model site operator with the validated official domain."""
    clean = re.sub(r"\bsite:\s*\S+", "", query or "", flags=re.I).strip()
    clean = re.sub(r"\s+", " ", clean)
    return f"{clean} site:{domain}".strip() if domain else clean


def _official_search_queries(query: str, company_ctx: dict,
                             aliases: list[str],
                             generated: list[str]) -> list[str]:
    """Build official-domain Google probes with safe identity enrichment.

    A validated ticker is a useful discovery alias on IR sites, so it gets one
    dedicated query variant. The CIK is deliberately not added to corporate
    website searches: it is an exact registry key and is sent directly to SEC
    EDGAR, where it has higher precision and avoids polluting website results.
    """
    domain = _clean_domain((company_ctx or {}).get("domain"))
    if not domain and REQUIRE_OFFICIAL_DOMAIN_FOR_WEB:
        return []

    candidates = [query]
    validation = (company_ctx or {}).get("_identity_validation") or {}
    if validation.get("status") == "validated":
        ticker = str((company_ctx or {}).get("ticker") or "").upper().strip()
        if ticker and not re.search(
                rf"\b(?:ticker|stock\s+symbol)\s*:?\s*[\"']?"
                rf"{re.escape(ticker)}(?:[\"']|\b)",
                query.upper(), re.I):
            candidates.append(
                f'{_strip_site(query)} ticker "{ticker}"')
    candidates.extend(aliases or [])
    candidates.extend(generated or [])

    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        scoped = _scope_to_official_domain(str(candidate or ""), domain)
        if not scoped or not _query_variant_preserves_years(query, scoped):
            continue
        key = scoped.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(scoped)
    return out


def _discovery_route(report_class: str | None,
                     registry_eligible: bool) -> list[str]:
    """Return the authoritative per-class discovery order."""
    canonical = (report_class or "").strip().lower()
    route: list[str] = []
    if registry_eligible and canonical in REGISTRY_FIRST_CLASSES:
        route.append("registry")
    route.extend([
        "direct_search",
        "official_crawl",
        "deep_crawl",
        "browser",
    ])
    if registry_eligible and "registry" not in route:
        route.append("registry")
    return route


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

    # v40: inject the per-class validation contract from report_specs so every
    # document class carries an explicit "what IS / what is NOT this class"
    # instruction into the fail-closed verifier.
    _spec_lines = []
    for _c, _ in _matched_doc_classes(query):
        _sp = report_specs.validation_prompt(
            _c, company=company,
            year=(max(target_years) if target_years else None))
        if _sp:
            _spec_lines.append(_sp)
    _spec_contract = (("- Per-class validation contract: " + " ".join(_spec_lines)
                       + "\n") if _spec_lines else "")

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
        + _spec_contract
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
        # Contamination guard: if a company was required and the SELECTED
        # candidate has no readable content sample (e.g. the fetch 403'd, as
        # the SEC search-tier URLs do), then company_match was decided on the
        # filename alone — exactly how a wrong-company filing slips the
        # cross-company gate. Fail closed rather than trust a filename.
        if (ENFORCE_COMPANY_SAMPLE_EVIDENCE and company
                and company.lower() != "unknown" and decision.get("selected_url")):
            _sel = decision.get("selected_url")
            _samp = ""
            for _c in candidates[:TOP_N_FOR_LLM]:
                if _c.get("url") == _sel:
                    _samp = (_c.get("content_sample") or "").strip()
                    break
            if len(_samp) < COMPANY_SAMPLE_MIN_CHARS:
                print(f"[llm] contamination guard: selected {_sel} has no "
                      f"readable content sample ({len(_samp)} chars) but a "
                      f"company match was required — cannot confirm company "
                      f"identity from filename alone; FAILING CLOSED")
                decision["selected_url"] = None
                decision["company_match"] = False
                decision["topic_match"] = False
                decision["reason"] = "company-unverifiable-empty-sample-failed-closed"
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


def _with_year_before_site(query: str, year: int) -> str:
    """Add a fiscal year without corrupting a trailing or leading site: operator."""
    site_match = re.search(r"\bsite:\s*\S+", query or "", re.I)
    if not site_match:
        return f"{query.strip()} {year}".strip()
    site = site_match.group(0)
    remainder = (query[:site_match.start()] + " " + query[site_match.end():]).strip()
    return re.sub(r"\s+", " ", f"{remainder} {year} {site}").strip()


def _apply_latest_completed_fiscal_year(query: str,
                                         known_class: str | None = None,
                                         known_year: int | None = None
                                         ) -> tuple[str, int | None]:
    """Resolve an undated recurring report request to the latest completed FY.

    Annual reports describe a completed fiscal year. In calendar year 2026 the
    preferred annual report is therefore FY2025, even when it was published in
    2026. Explicit historical years always win.
    """
    explicit = _extract_year_intent(query)
    if known_year or explicit:
        return query, known_year or max(explicit)
    classes = ({(known_class or "").strip().lower()} if known_class else
               {c for c, _ in _matched_doc_classes(query)})
    if not classes.intersection(LATEST_COMPLETED_FISCAL_YEAR_CLASSES):
        return query, None
    target = CURRENT_YEAR - max(1, LATEST_COMPLETED_FISCAL_YEAR_LAG)
    resolved = _with_year_before_site(query, target)
    print(f"[year] latest completed fiscal year: {query!r} -> {resolved!r}")
    return resolved, target


# ─── HEAD pre-filter ─────────────────────────────────────────────────────────
# Excel (.xlsx/.xls/.xlsm) is a first-class report format here: many companies
# publish ESG / emissions data as workbooks, not PDFs. _is_doc_url and every
# downstream fetch/HEAD/safety check key off this tuple, so an omitted extension
# means those documents are never even recognized as candidates.
_DOC_EXTS = (".pdf", ".doc", ".docx", ".rtf", ".xlsx", ".xls", ".xlsm")


def _head_check(url: str) -> dict:
    headers = {"User-Agent": _ua_for(url), "Accept": "*/*"}
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


# ─── Search: AgentCore Gateway managed WebSearch tool + isolated Vertex Lambda ─
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


def _invoke_vertex_lambda(payload: dict) -> dict:
    """Invoke the isolated Vertex Lambda and return its decoded JSON body."""
    if _lambda is None or not LAMBDA_SEARCH_FUNCTION:
        return {"error": "no-lambda-configured"}
    try:
        resp = _lambda.invoke(
            FunctionName=LAMBDA_SEARCH_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[vertex-lambda] invoke error: {type(exc).__name__}: {exc}")
        return {"error": f"lambda-invoke-error({type(exc).__name__})"}

    if resp.get("FunctionError"):
        try:
            err = resp["Payload"].read().decode("utf-8", "ignore")[:300]
        except Exception:  # noqa: BLE001
            err = ""
        print(f"[vertex-lambda] function error: {err}")
        return {"error": "lambda-function-error"}

    try:
        body = json.loads(resp["Payload"].read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[vertex-lambda] payload parse failed: {exc}")
        return {"error": f"lambda-parse-error({type(exc).__name__})"}
    return body if isinstance(body, dict) else {"error": "lambda-non-object-response"}


def _vertex_lambda_search(query: str, limit: int) -> tuple[list[dict], str]:
    """Tier 1 discovery via the ISOLATED Vertex grounded-search Lambda."""
    site = _domain(query)
    body = _invoke_vertex_lambda({
        "mode": "document_search",
        "query": _strip_site(query) if site else query,
        "site": site or "",
        "max_results": limit,
    })
    if body.get("error") and not body.get("results"):
        return [], str(body["error"])

    raw = body.get("results", []) if isinstance(body, dict) else []
    out, seen = [], set()
    _dropped_redirects = 0
    for it in raw:
        if not isinstance(it, dict):
            continue
        u = it.get("url") or ""
        if not u or u in seen:
            continue
        # Defensive guard (belt-and-suspenders; the Lambda should already drop
        # these). An unresolved Vertex grounding redirect 403s on fetch, has no
        # real domain for site-scoping, and only burns a HEAD/sample slot — so
        # it must never enter the candidate pool.
        if urlparse(u).netloc.lower().endswith("vertexaisearch.cloud.google.com"):
            _dropped_redirects += 1
            continue
        seen.add(u)
        out.append({"title": it.get("title", ""), "url": u,
                    "snippet": it.get("snippet", "")})
    if _dropped_redirects:
        print(f"[vertex-lambda] dropped {_dropped_redirects} unresolved "
              f"grounding-redirect URL(s) that leaked from the Lambda")
    via = body.get("via", "vertex-lambda") if isinstance(body, dict) else "vertex-lambda"
    return out, via


def _vertex_company_identity_hint(company_ctx: dict) -> dict:
    """Get grounded company identifiers as hints; never trust them directly."""
    if not ENABLE_VERTEX_IDENTITY or _lambda is None:
        return {}
    name = str(company_ctx.get("name") or "").strip()
    domain = str(company_ctx.get("domain") or "").strip().lower()
    if not name or name.lower() == "unknown":
        return {}
    cache_key = f"{name.lower()}||{domain}"
    with _VERTEX_IDENTITY_CACHE_LOCK:
        cached = _VERTEX_IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    body = _invoke_vertex_lambda({
        "mode": "company_identity",
        "company_name": name,
        "site": domain,
        "max_results": 8,
    })
    hint = dict(body.get("identity_hint") or {})
    sources = [
        item for item in (body.get("results") or [])
        if isinstance(item, dict) and item.get("url")
    ]

    # A Vertex-proposed domain may enrich a legacy payload only when at least
    # one resolved grounding source is actually hosted on that same domain.
    # This is source attestation, not trust in generated model text.
    hinted_domain = str(hint.get("official_domain") or "").strip().lower()
    if hinted_domain:
        identity_stop = {
            "inc", "incorporated", "corp", "corporation", "company",
            "limited", "ltd", "plc", "group", "holdings", "holding",
        }
        identity_terms = [
            term for term in re.findall(r"[a-z0-9]+", name.lower())
            if len(term) > 2 and term not in identity_stop
        ]
        hint["_domain_attested"] = bool(identity_terms) and any(
            _host_matches(item.get("url", ""), hinted_domain)
            and all(
                term in (
                    str(item.get("title") or "") + " "
                    + str(item.get("snippet") or "") + " "
                    + str(item.get("url") or "")
                ).lower()
                for term in identity_terms
            )
            for item in sources
        )
    hint["_grounding_sources"] = [
        {"title": str(item.get("title") or "")[:200],
         "url": str(item.get("url") or "")[:1000]}
        for item in sources[:8]
    ]
    with _VERTEX_IDENTITY_CACHE_LOCK:
        _VERTEX_IDENTITY_CACHE[cache_key] = dict(hint)
    print(f"[identity] Vertex hint for {name!r}: "
          f"ticker={hint.get('ticker')!r} cik={hint.get('cik')!r} "
          f"domain={hint.get('official_domain')!r} "
          f"sources={len(sources)} (untrusted until SEC validation)")
    return hint


def _single_web_search(query: str, limit: int) -> tuple[list[dict], str]:
    # ── Tier 1: Vertex Lambda backend (no global _throttle; the Lambda + Vertex
    # handle concurrency; fan-out is bounded by SEARCH_FANOUT_WORKERS).
    # Optionally falls through to the Gateway path when it returns nothing. ──
    if SEARCH_BACKEND in ("vertex", "vertex_lambda", "lambda"):
        hits, via = _vertex_lambda_search(query, limit)
        if hits or not (VERTEX_FALLBACK_TO_GATEWAY and GATEWAY_URL):
            return hits, via
        print(f"[search] vertex returned nothing ({via}); falling back to gateway")

    _throttle()

    if not GATEWAY_URL:
        print("[search] GATEWAY_URL not configured — no search backend available")
        return [], "no-gateway-configured"

    # v40 (bug fix): always return a tuple — the pre-v40 tail fell off the end
    # here and returned None when the gateway fallback was attempted, which is
    # what produced the 'cannot unpack non-iterable NoneType' batch crash.
    try:
        return asyncio.run(_gateway_search_async(query, limit))
    except Exception as exc:  # noqa: BLE001
        print(f"[search] gateway path failed: {type(exc).__name__}: {exc}")
        return [], f"gateway-error({type(exc).__name__})"


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
        score += _filing_type_score_adjustment(u, hay, query)
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

KNOWN_DOCUMENT_CDN_DOMAINS = {
    d.strip().lower()
    for d in os.environ.get(
        "KNOWN_DOCUMENT_CDN_DOMAINS",
        os.environ.get("TRUSTED_DOCUMENT_CDN_DOMAINS", "q4cdn.com"),
    ).split(",")
    if d.strip()
}


def _fetch(url: str) -> tuple[bytes, str]:
    headers = dict(_BROWSER_HEADERS)
    headers["User-Agent"] = _ua_for(url)
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


def _is_investor_page(url: str) -> bool:
    parsed = urlparse(url or "")
    host = parsed.netloc.lower().split(":")[0]
    path = unquote(parsed.path).lower()
    return (host.startswith(("investors.", "investor."))
            or "/investors/" in path
            or "/investor-relations/" in path
            or "/investor_relations/" in path)


def _is_known_document_cdn(url: str) -> bool:
    """Ranking hint only; never an acceptance requirement."""
    host = urlparse(url or "").netloc.lower().split(":")[0]
    reg = _registrable(host)
    return any(host == d or host.endswith("." + d) or reg == _registrable(d)
               for d in KNOWN_DOCUMENT_CDN_DOMAINS)


def _is_safe_remote_document_url(url: str) -> bool:
    """Reject non-web, credential-bearing, local, and private-network targets."""
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        except ValueError:
            if "." not in host:
                return False
        return _is_doc_url(url)
    except Exception:  # noqa: BLE001
        return False


def _is_official_investor_source(page_url: str,
                                 official_domain: str | None) -> bool:
    """True only when an investor page belongs to the requested official site."""
    if not official_domain or not _is_investor_page(page_url):
        return False
    page_host = urlparse(page_url or "").netloc.lower().split(":")[0]
    return _registrable(page_host) == _registrable(official_domain)


def _doc_links(html: bytes, base_url: str, domain: str,
               official_domain: str | None = None) -> list[str]:
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
        same_site = _registrable(urlparse(u).netloc) == _registrable(domain)
        source_attested_document = (
            _is_official_investor_source(base_url, official_domain)
            and _is_safe_remote_document_url(u)
            and not _is_junk_host(u)
        )
        if (same_site or source_attested_document) and u not in out:
            out.append(u)
    return out


_CTYPE_EXT = (
    ("pdf", ".pdf"),
    ("officedocument.wordprocessingml", ".docx"),
    ("msword", ".doc"),
    ("officedocument.spreadsheetml", ".xlsx"),
    ("ms-excel", ".xls"),
    ("rtf", ".rtf"),
)


def _safe_name(url: str, ctype: str = "") -> str:
    p = unquote(urlparse(url).path).rsplit("/", 1)[-1] or "document"
    if p.lower().endswith(_DOC_EXTS) or p.lower().endswith((".htm", ".html")):
        return p
    # No usable extension on the URL path (e.g. '...report2025pdf' with a
    # missing dot, or an extensionless CDN key). Name by the CONTENT-TYPE of
    # what was actually fetched instead of blindly appending .html — that
    # mislabeled a real PDF served without an extension.
    c = (ctype or "").lower()
    for needle, ext in _CTYPE_EXT:
        if needle in c:
            return p + ext
    return p + (".html" if ("html" in c or not c) else ".bin")


def _site_root(query: str) -> str | None:
    m = re.search(r"site:\s*(\S+)", query or "", re.I)
    if not m:
        return None
    raw = m.group(1).rstrip("/")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    return f"{p.scheme}://{p.netloc}/"


def _page_doc_candidates(page_body: bytes, page_url: str, ctx: str,
                         official_domain: str | None = None
                         ) -> tuple[list[str], list[str]]:
    links = _doc_links(page_body, page_url, urlparse(page_url).netloc.lower(),
                       official_domain=official_domain)
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


# ─── AgentCore Browser fallback: deterministic DOM extraction ─────────────────
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

# Universal rejects: a sample / template / specimen / example / mock document is
# never the real deliverable for ANY class (observed in prod: an "Impact Report"
# query stored 'Board Sustainability Engagement Report_Sample Report 2025' — a
# specimen, accepted on filename alone). These are matched regardless of class,
# unlike _WRONG_CLASS_FILENAME_MARKERS which only apply to _STRICT_REJECT_CLASSES.
_SAMPLE_TEMPLATE_MARKERS = tuple(
    m.strip().lower() for m in os.environ.get(
        "SAMPLE_TEMPLATE_MARKERS",
        "sample report,sample_report,sample-report,_sample_,-sample-,"
        " sample ,specimen,template,-example-,_example_,dummy-,-dummy,"
        "mock-report,placeholder,do-not-use,for-illustration",
    ).split(",") if m.strip()
)


def _is_sample_or_template(url: str) -> bool:
    """True if a candidate's filename marks it as a sample/template/specimen —
    invalid for every document class. Runs before any LLM verify (no model call),
    like the press-release filter."""
    try:
        name = unquote(urlparse(url or "").path).rsplit("/", 1)[-1].lower()
    except Exception:  # noqa: BLE001
        name = (url or "").lower()
    return any(marker and marker in name for marker in _SAMPLE_TEMPLATE_MARKERS)

_CLASS_SCOPED_WRONG_MARKERS: dict[str, tuple[str, ...]] = {
    "proxy statement": (
        # Only the DEFINITIVE annual-meeting proxy (DEF 14A) is wanted. These
        # are the wrong SEC proxy variants the agent was grabbing instead:
        "defa14a", "defa-14a", "defa 14a", "def-a-14a",
        "additional-definitive", "additional definitive",
        "additional-proxy", "additional proxy", "additional-soliciting",
        "additional soliciting", "soliciting-material", "soliciting material",
        "prer14a", "prem14a", "pre-14a", "preliminary-proxy",
        "preliminary proxy",
    ),
    "sustainability report": (
        "strategic-report", "esg-update", "esg-supplement", "esg-factbook",
        "green-bond", "sdg-bond", "cdp-carbon-disclosure",
        "_per_", "-per-", "product_environmental_report",
        "product-environmental-report",
    ),
    "environmental policy": (
        "strategic-report", "esg-update", "esg-supplement", "esg-factbook",
        "cdp-carbon-disclosure",
        "_per_", "-per-", "product_environmental_report",
        "product-environmental-report",
        "environmental_progress_report", "environmental_responsibility_report",
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

    # Universal: sample/template/specimen is wrong for every class.
    if _is_sample_or_template(url):
        return True

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

    `rejected`: URLs that FAILED CLASS VERIFICATION for THIS query are recorded
    here so a later nav page that re-links the same junk PDF skips it on sight.
    Deliberately PER-QUERY, not per-run like `known_bad`.
    """
    def __init__(self):
        self.verifies = 0
        self.deadline = time.monotonic() + QUERY_MAX_SECONDS
        self.rejected: set[str] = set()

    def can_verify(self, reserve: int = 0) -> bool:
        usable_limit = max(0, QUERY_MAX_VERIFIES - max(0, reserve))
        return (self.verifies < usable_limit
                and time.monotonic() < self.deadline)

    def note_verify(self) -> None:
        self.verifies += 1

    def time_left(self) -> bool:
        return time.monotonic() < self.deadline

    def why_stopped(self, reserve: int = 0) -> str:
        usable_limit = max(0, QUERY_MAX_VERIFIES - max(0, reserve))
        if reserve and self.verifies >= usable_limit:
            return (f"pre-browser verify allowance {usable_limit} exhausted; "
                    f"{min(reserve, QUERY_MAX_VERIFIES)} reserved for browser")
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

def _visible_text_locator(page, text: str, timeout_ms: int = 1500):
    """Return a visible text match instead of Playwright's often-hidden `.first`."""
    try:
        matches = page.get_by_text(text, exact=False)
        count = min(matches.count(), 20)
        for index in range(count):
            loc = matches.nth(index)
            try:
                loc.wait_for(state="visible", timeout=timeout_ms)
                return loc
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return None


def _click_target_visible(page, text: str, timeout_ms: int = 1500) -> bool:
    return _visible_text_locator(page, text, timeout_ms=timeout_ms) is not None

def _verify_priority(url: str, query: str) -> int:
    path = unquote(urlparse(url).path).lower()
    name = path.rsplit("/", 1)[-1]
    terms = [t for t in _keywords(query) if len(t) > 3]
    score = 3 if any(t in name for t in terms) else 0
    matched = {c for c, _ in _matched_doc_classes(query)}
    if "annual report" in matched:
        if "/doc_financials/annual/" in path:
            score += 20
        if re.search(r"(?:^|[/_.-])ar[_-]?20\d{2}(?:\.|$)", path):
            score += 16
        if _is_known_document_cdn(url):
            score += 6
        if any(marker in path for marker in (
                "sustainab", "climate", "assurance", "modern_slavery",
                "modern-slavery", "proxy", "10-q", "quarterly", "policy")):
            score -= 30
    return score

def _sort_for_verify(urls: list[str], query: str) -> list[str]:
    return sorted(urls, key=lambda u: _verify_priority(u, query), reverse=True)

def _doc_candidate_score(url: str, text: str, query: str, domain: str | None) -> int:
    ranked = _rank([{"url": url, "title": text, "snippet": text}], query, domain)
    # _rank already includes filing-type boosts/penalties. Do not apply them a
    # second time for browser candidates.
    return ranked[0][0] if ranked else 0


_FILING_TYPE_HINTS: dict[str, dict[str, list[str]]] = {
    "annual report": {
        "boost": ["10-k", "10k", "form10-k", "form 10-k", "annual report",
                  "ar20", "integrated annual report", "annual-report"],
        "penalize": ["10-q", "10q", "quarterly", "8-k", "8k",
                     "consolidated_financial_statements", "current report",
                     "sustainab", "climate", "assurance", "proxy"],
    },
    "proxy statement": {
        "boost": ["def 14a", "def14a", "proxy statement", "proxy-statement",
                  "definitive proxy"],
        "penalize": ["10-q", "10-k", "8-k", "quarterly", "current report",
                     "defa14a", "defa-14a", "additional-definitive",
                     "additional proxy", "soliciting", "prer14a", "prem14a",
                     "preliminary"],
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


def _is_ir_nav_link(url: str, text: str = "") -> bool:
    hay = (unquote(urlparse(url or "").path) + " " + (text or "")).lower()
    return any(k in hay for k in IR_NAV_KEYWORDS)


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
    """True if a harvested link is almost certainly a press release / news item
    rather than an official policy/report document. Runs BEFORE any LLM call."""
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


_HTML_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_HTML_IX_HEADER_CLOSED_RE = re.compile(r"<ix:header\b.*?</ix:header>", re.I | re.S)
_HTML_IX_HEADER_UNCLOSED_RE = re.compile(r"<ix:header\b.*", re.I | re.S)
_HTML_HIDDEN_STYLE_RE = re.compile(
    r"<(div|span)\b[^>]*style\s*=\s*[\"'][^\"']*display\s*:\s*none[^\"']*[\"'][^>]*>.*?</\1>",
    re.I | re.S)
_HTML_ANY_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WS_RE = re.compile(r"\s+")
_PROSE_RUN_RE = re.compile(r"(?:[A-Za-z]{3,}\s+){12,}[A-Za-z]{3,}")
_HTML_ANCHOR_MARKERS = (b"FORM 10-K", b"FORM 10-Q", b"FORM 20-F", b"FORM 40-F",
                        b"ANNUAL REPORT", b"DEF 14A", b"PROXY STATEMENT")


def _extract_visible_text(raw_html: bytes, company: str = "",
                          raw_window: int = 1_000_000, max_chars: int = 1800,
                          anchor_scan_span: int = 5_000_000) -> str:
    """Best-effort plain-text extraction from HTML bytes for LLM verification
    sampling.

    Real inline-XBRL SEC filings (10-K/20-F/DEF 14A) put an <ix:header>
    <ix:hidden>...</ix:hidden></ix:header> block at the very TOP of the
    document containing raw XBRL context/unit/fact definitions — CIKs, dates,
    numbers, zero prose. For a large filer this block can run into the
    hundreds of KB or more, which defeats any FIXED decode/strip window: if
    the block is bigger than the window, the closing tag never appears in the
    truncated text, the strip regex silently finds no match, and the entire
    sample is unstripped hidden-block noise (this was confirmed reproducing
    the exact failure against a synthetic filing shaped like the real one).

    Rather than betting on a window being big enough, this does a fast raw
    BYTE scan (no decoding — just bytes.find, cheap even across megabytes)
    across a much larger span for markers that only appear in the real,
    visible document ("FORM 10-K", "ANNUAL REPORT", the company name itself),
    jumps straight to that offset, and only decodes/strips a bounded window
    around it. A closed/unclosed ix:header pass and a display:none pass
    still run for defense in depth, plus a general "does this look like real
    prose, and if not, scan forward for a run of real words" safety net that
    catches hiding techniques the anchor scan or tag stripping don't.
    """
    anchor_pos = None
    scan_span = raw_html[:anchor_scan_span]
    scan_upper = scan_span.upper()
    markers = list(_HTML_ANCHOR_MARKERS)
    if company:
        words = [w for w in re.split(r"\s+", company.strip()) if len(w) > 2][:2]
        if words:
            markers.append(" ".join(words).upper().encode())
    for marker in markers:
        idx = scan_upper.find(marker.upper())
        if idx != -1 and (anchor_pos is None or idx < anchor_pos):
            anchor_pos = idx
    if anchor_pos is not None:
        start = max(0, anchor_pos - 200)
        raw_html = raw_html[start:start + raw_window]

    try:
        chunk = raw_html[:raw_window].decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""
    if _HTML_IX_HEADER_CLOSED_RE.search(chunk):
        chunk = _HTML_IX_HEADER_CLOSED_RE.sub(" ", chunk)
    elif re.search(r"<ix:header\b", chunk, re.I):
        # Closing tag fell outside this window — the rest of the window is
        # still inside the hidden block. Drop from the opening tag onward
        # rather than leak unstripped hidden content into the sample.
        chunk = _HTML_IX_HEADER_UNCLOSED_RE.sub(" ", chunk)
    chunk = _HTML_HIDDEN_STYLE_RE.sub(" ", chunk)
    chunk = _HTML_HIDDEN_STYLE_RE.sub(" ", chunk)
    chunk = _HTML_SCRIPT_STYLE_RE.sub(" ", chunk)
    chunk = _HTML_ANY_TAG_RE.sub(" ", chunk)
    chunk = _HTML_WS_RE.sub(" ", chunk).strip()

    # General safety net regardless of hiding technique: if the front of the
    # cleaned text still looks like ID/number soup (no long run of real
    # words), scan forward for the first genuine prose run and sample from
    # there instead of assuming prose starts at position 0.
    head = chunk[:max_chars]
    if not _PROSE_RUN_RE.search(head):
        m = _PROSE_RUN_RE.search(chunk)
        if m:
            chunk = chunk[m.start():]
        else:
            chunk = head
    return chunk[:max_chars]


def _make_browser_verify_fn(query: str, budget: "_QueryBudget | None" = None,
                             company: str = "", reserve_verifies: int = 0):
    """verify_fn(candidate_doc) -> bool. Fail-closed class+company check scoped to one candidate."""
    if not (BROWSER_VERIFY_CLASS and _bedrock is not None):
        return None

    def _verify(cand: dict) -> bool:
        url = cand.get("url", "")
        if budget is not None and budget.is_rejected(url):
            print(f"[verify] already rejected earlier this query, skipping: {url}")
            cand["_verify_decision"] = {"selected_url": None, "topic_match": False,
                                        "reason": "already-rejected-this-query"}
            return False
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
        if budget is not None and not budget.can_verify(reserve_verifies):
            print(f"[verify] budget stop ({budget.why_stopped(reserve_verifies)}): skipping "
                  f"LLM verify for {url}")
            cand["_verify_decision"] = {"selected_url": None, "topic_match": False,
                                        "reason": f"query-budget:{budget.why_stopped(reserve_verifies)}"}
            return False
        sample = ""
        try:
            if "html" in (cand.get("ctype") or ""):
                sample = _extract_visible_text(cand["body"], company=company)
                if len(sample) < 200:
                    # Stripped text came back too thin (rare) — fall back to
                    # the old raw-byte behavior rather than sending an empty
                    # sample to the verifier.
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
        return (
            not domain
            or _registrable(urlparse(url).netloc) == _registrable(domain)
            # Known document CDNs are accepted only inside the browser path,
            # where the URL was harvested from an official company page.
            or _is_known_document_cdn(url)
        )

    resolved: dict | None = None
    had_candidates = False
    verify_budget = BROWSER_MAX_VERIFY_CANDIDATES if verify_fn else 1
    click_budget = min(BROWSER_MAX_CLICK_ATTEMPTS, verify_budget) if verify_fn else 1
    pool_cap = max(BROWSER_MAX_DOC_CANDIDATES, verify_budget)
    _deadline = time.monotonic() + BROWSER_RESOLVE_MAX_SECONDS

    def _time_left() -> bool:
        if budget is not None and not budget.can_verify():
            return False
        return time.monotonic() < _deadline

    def _stop_reason() -> str:
        if budget is not None and not budget.can_verify():
            return budget.why_stopped()
        return (f"BROWSER_RESOLVE_MAX_SECONDS "
                f"({BROWSER_RESOLVE_MAX_SECONDS}s) exceeded")

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
                                 and _is_safe_remote_document_url(h["url"])
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
                                    print(f"[browser] Tier 2 stopped: {_stop_reason()}")
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
                                      f"class-rejected earlier this query: {cand}")
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
                                    reject_reason = (cand_doc.get("_verify_decision") or {}).get(
                                        "reason", "class verification failed")
                                    print(f"[browser] in-session candidate "
                                          f"{tried}/{verify_budget} not accepted "
                                          f"({cand}): {reject_reason}; trying next candidate")
                                    continue
                                cand_doc["verified"] = True
                                cand_doc["_verified_for"] = query
                            verified_hits.append(cand_doc)
                            target_years = _extract_year_intent(query)
                            candidate_years = _extract_year_intent(cand)
                            if verify_fn is None or target_years.intersection(candidate_years):
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
                                    print(f"[browser] Tier 3 stopped: {_stop_reason()}")
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
                            loc = _visible_text_locator(page, txt)
                            if loc is None:
                                print(f"[browser] click target not visible "
                                      f"(fast-skip): {txt[:40]!r}")
                                if known_bad is not None:
                                    known_bad[click_key] = "not visible (fast-skip)"
                                continue
                            try:
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

                    # ── Tier 4: in-browser deep navigation, IR-first ──
                    if (resolved is None and BROWSER_DEEP_NAV and nav_links
                            and _time_left()):
                        visited_nav: set[str] = {page_url.rstrip("/")}
                        _nav_terms = [t for t in _keywords(query) if len(t) > 3]
                        def _nav_relevant(u: str) -> bool:
                            if not _nav_terms:
                                return True
                            path = unquote(urlparse(u).path).lower()
                            return any(t in path for t in _nav_terms)
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
                                 and _is_safe_remote_document_url(h["url"])
                                 and _domain_ok(h["url"])
                                 and not _is_junk_host(h["url"])
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
                                target_years = _extract_year_intent(query)
                                candidate_years = _extract_year_intent(cand)
                                if (verify_fn is None
                                        or target_years.intersection(candidate_years)):
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

                    # ── Tier 5: optional vision ──
                    if (resolved is None and BROWSER_VISION_MODEL_ID
                            and _bedrock is not None and _time_left()):
                        vres = _browser_vision_resolve(page, query, domain,
                                                       final_url or page_url,
                                                       _acceptable,
                                                       verify_fn=verify_fn)
                        if vres and vres.get("body"):
                            already_verified = (
                                vres.get("_verified_for") == query)
                            if (already_verified or verify_fn is None
                                    or verify_fn(vres)):
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
                            current_url: str, acceptable_fn,
                            verify_fn=None) -> dict | None:
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

    def _domain_ok(url: str) -> bool:
        return (
            not domain
            or _registrable(urlparse(url).netloc) == _registrable(domain)
            or _is_known_document_cdn(url)
        )

    def _fetch_document(url: str, referer: str) -> dict | None:
        if (not _is_safe_remote_document_url(url)
                or not _domain_ok(url)
                or _is_junk_host(url)
                or _is_press_release_url(url)):
            return None
        try:
            response = page.context.request.get(
                url, headers={"referer": referer},
                timeout=BROWSER_SESSION_TIMEOUT * 1000)
            ctype = (response.headers or {}).get(
                "content-type", "").split(";")[0].lower()
            if response.status >= 400 or not acceptable_fn(url, ctype):
                return None
            body = response.body()
        except Exception as exc:  # noqa: BLE001
            print(f"[browser][vision] document fetch failed ({url}): {exc}")
            return None
        if not body or len(body) > BROWSER_MAX_DOC_BYTES:
            return None
        candidate = {
            "url": url,
            "body": body,
            "ctype": ctype or "application/pdf",
            "via": "browser_vision_navigation",
        }
        if verify_fn is not None:
            if not verify_fn(candidate):
                return None
            candidate["verified"] = True
            candidate["_verified_for"] = query
        return candidate

    def _harvest_after_navigation() -> dict | None:
        try:
            page.wait_for_timeout(BROWSER_SETTLE_MS)
            _dismiss_cookie_modals(page)
            links = page.evaluate(_JS_HARVEST_LINKS) or []
        except Exception as exc:  # noqa: BLE001
            print(f"[browser][vision] post-navigation harvest failed: {exc}")
            return None
        docs = sorted(
            [
                item for item in links
                if _is_doc_url(item.get("url", ""))
                and _is_safe_remote_document_url(item["url"])
                and _domain_ok(item["url"])
                and not _is_junk_host(item["url"])
                and not _is_press_release_url(
                    item["url"], item.get("text", ""))
            ],
            key=lambda item: (
                _verify_priority(item["url"], query),
                _doc_candidate_score(
                    item["url"], item.get("text", ""), query, domain),
            ),
            reverse=True,
        )
        print(f"[browser][vision] navigated to {page.url}; harvested "
              f"{len(docs)} document candidate(s)")
        for item in docs[:BROWSER_MAX_VERIFY_CANDIDATES]:
            candidate = _fetch_document(item["url"], page.url)
            if candidate:
                return candidate
        return None

    try:
        loc = _visible_text_locator(page, link_text, timeout_ms=1500)
        href = None
        if loc is None:
            # A hidden desktop/mobile-menu duplicate may still expose the real
            # href. Reading that href is safer than force-clicking a hidden node.
            try:
                matches = page.get_by_text(link_text, exact=False)
                for index in range(min(matches.count(), 20)):
                    candidate_loc = matches.nth(index)
                    candidate_href = candidate_loc.evaluate(
                        """el => {
                          const a = el.closest('a[href]');
                          return a ? a.href : null;
                        }""")
                    if candidate_href:
                        loc = candidate_loc
                        href = candidate_href
                        break
            except Exception:  # noqa: BLE001
                pass
        elif loc is not None:
            try:
                href = loc.evaluate(
                    """el => {
                      const a = el.closest('a[href]');
                      return a ? a.href : null;
                    }""")
            except Exception:  # noqa: BLE001
                href = None

        if href and _is_doc_url(href):
            direct = _fetch_document(href, page.url or current_url)
            if direct:
                print(f"[browser][vision] resolved direct anchor: {href}")
                return direct

        if href and _is_navigation_href(href, domain):
            page.goto(href, wait_until=BROWSER_WAIT_UNTIL,
                      timeout=BROWSER_SESSION_TIMEOUT * 1000)
            return _harvest_after_navigation()

        if loc is None:
            print("[browser][vision] text existed only in a hidden non-link "
                  "control; refusing force-click")
            return None

        # Button-style controls may emit a native download or navigate via JS.
        before_url = page.url
        try:
            with page.expect_download(
                    timeout=BROWSER_CLICK_TIMEOUT_MS) as download_info:
                loc.click(timeout=min(BROWSER_CLICK_TIMEOUT_MS, 8000))
            download = download_info.value
            with open(download.path(), "rb") as file_handle:
                body = file_handle.read()
            filename = download.suggested_filename or ""
            if not body or len(body) > BROWSER_MAX_DOC_BYTES:
                return None
            download_url = ""
            try:
                download_url = download.url
            except Exception:  # noqa: BLE001
                pass
            candidate = {
                "url": download_url or current_url,
                "body": body,
                "ctype": ("application/pdf"
                          if filename.lower().endswith(".pdf")
                          else "application/octet-stream"),
                "via": "browser_vision_click",
            }
            if verify_fn is None or verify_fn(candidate):
                if verify_fn is not None:
                    candidate["verified"] = True
                    candidate["_verified_for"] = query
                return candidate
            return None
        except Exception as click_exc:  # noqa: BLE001
            # expect_download also raises when the click successfully navigates
            # to an HTML archive. Inspect page state before treating it as a miss.
            if page.url and page.url != before_url:
                return _harvest_after_navigation()
            print(f"[browser][vision] click produced neither download nor "
                  f"navigation: {click_exc}")
            return None
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


def _write_metadata_sidecar(s3_key, company, url, digest, ctype,
                            original_query, prepared_query,
                            request_id="", report_class=None, year=None,
                            run_id="", company_ctx=None):
    """Write <s3_key>.metadata.json so the downstream Bedrock Knowledge Base can
    filter by company / doc_class / year at retrieval time."""
    if _s3 is None or not BUCKET:
        return
    classes = ([report_class] if report_class
               else [c for c, _ in _matched_doc_classes(prepared_query)])
    yrs = ([year] if year else sorted(_extract_year_intent(prepared_query)))
    identity = ((company_ctx or {}).get("_identity_validation") or {})
    meta = {
        "company": _slug(company), "company_name": company,
        "doc_class": classes[0] if classes else None,
        "doc_classes": classes,
        "year": (yrs[-1] if yrs else None),
        "source_url": url, "sha256": digest, "content_type": ctype,
        "run_id": run_id, "request_id": request_id,
        "query": original_query, "prepared_query": prepared_query,
        "ticker": (company_ctx or {}).get("ticker") or None,
        "cik": (company_ctx or {}).get("cik") or None,
        "identity_status": identity.get("status") or "unresolved",
    }
    try:
        _s3.put_object(Bucket=BUCKET, Key=s3_key + ".metadata.json",
                       Body=json.dumps(meta, ensure_ascii=True).encode("utf-8"),
                       ContentType="application/json")
    except Exception as exc:  # noqa: BLE001
        print(f"[metadata] sidecar write failed for {s3_key}: {exc}")


def _store(company: str, run_id: str, url: str, body: bytes, ctype: str,
           title: str, original_query: str, prepared_query: str,
           request_id: str = "", report_class: str | None = None,
           year: int | None = None, company_ctx: dict | None = None) -> dict:
    digest  = hashlib.sha256(body).hexdigest()
    classes = ([report_class] if report_class
               else [c for c, _ in _matched_doc_classes(prepared_query)])
    class_slug = _slug(classes[0]) if classes else "uncategorized"
    # Keep class associations physically distinct. The same combined PDF may
    # legitimately satisfy two independently verified classes; a single shared
    # sidecar would otherwise be overwritten by the last query and later
    # retrieval could fan it out to unrelated questions.
    s3_key = (
        f"{_slug(company)}/{class_slug}/"
        f"{digest[:12]}-{_safe_name(url, ctype)}"
    )
    _s3_put_if_missing(s3_key, body, ctype,
                       {"source_url": url, "sha256": digest, "run_id": run_id})
    _write_metadata_sidecar(
        s3_key, company, url, digest, ctype,
        original_query=original_query, prepared_query=prepared_query,
        request_id=request_id, report_class=report_class, year=year,
        run_id=run_id, company_ctx=company_ctx)
    years = ([year] if year else sorted(_extract_year_intent(prepared_query)))
    wrote = _write_provenance_if_missing({
        "company": _slug(company), "s3_key": s3_key, "run_id": run_id,
        "report": title or _safe_name(url, ctype), "source_url": url,
        "query": original_query, "prepared_query": prepared_query,
        "request_id": request_id,
        "doc_class": classes[0] if classes else None,
        "year": years[-1] if years else None,
        "ticker": (company_ctx or {}).get("ticker") or "",
        "cik": (company_ctx or {}).get("cik") or "",
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
        "report": title or _safe_name(url, ctype),
        "query": original_query, "prepared_query": prepared_query,
        "request_id": request_id,
        "doc_class": classes[0] if classes else None,
        "year": years[-1] if years else None,
    }


# ─── Confidence + single-selection search ─────────────────────────────────────
def _confident(decision: dict, query: str = "") -> bool:
    if not decision.get("selected_url") or not decision.get("topic_match"):
        return False
    if not decision.get("company_match", True):
        return False
    confidence_order = {"low": 0, "medium": 1, "high": 2}
    required = confidence_order.get(MIN_SELECTION_CONFIDENCE, 2)
    actual = confidence_order.get(
        str(decision.get("confidence") or "low").lower(), 0)
    if actual < required:
        return False
    if _extract_year_intent(query):
        return bool(decision.get("year_match"))
    return True


def _find_best_document(search_queries: list[str], limit: int, company: str = "") -> dict:
    """Search all query variants via a bounded PARALLEL fan-out, merge/dedupe by
    URL, rank + HEAD-filter, sample top candidates, then ONE grouped LLM
    selection across all of them (fail-closed)."""
    primary = search_queries[0]
    fanout = _parallel_web_search(search_queries, limit)
    results_map: dict[str, tuple[list[dict], str]] = {}
    query_logs: list[dict] = []
    for q in search_queries:
        # v40 (bug fix): dict.get(key, default) only uses default when the key
        # is MISSING, not when its value is None — read defensively.
        _res = fanout.get(q)
        hits, via = _res if _res is not None else ([], "not-run")
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

    # Drop sample/template/specimen candidates before they reach the LLM
    # selector — the selector judges partly on filename and has accepted a
    # "Sample Report" as a real match. Invalid for every class.
    _pre_sample_count = len(merged)
    merged = [h for h in merged if not _is_sample_or_template(h.get("url", ""))]
    sample_dropped = _pre_sample_count - len(merged)
    if sample_dropped:
        print(f"[find] dropped {sample_dropped} sample/template/specimen "
              f"candidate(s) (not a real deliverable for any class)")

    # Apply the same STRICT filename class-rejection the browser/crawl tiers use,
    # here in the search path too (previously the search path had no filename
    # reject at all). This is what lets a wrong proxy variant — DEFA14A
    # "additional definitive materials" instead of the DEF 14A main proxy —
    # reach the LLM selector and get stored. Only fires when the query maps to a
    # known class, so generic queries are unaffected.
    if _matched_doc_classes(primary):
        _pre_class_count = len(merged)
        merged = [h for h in merged
                  if not _is_wrong_class_filename(h.get("url", ""), primary)]
        class_dropped = _pre_class_count - len(merged)
        if class_dropped:
            print(f"[find] dropped {class_dropped} wrong-class filename "
                  f"candidate(s) (e.g. DEFA14A/additional-proxy, near-neighbor "
                  f"documents) before LLM selection")

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
                merged = []
                domain_mode = "hard_reject(no_on_domain_hits)"
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
    ranked_urls = _ranked_probe_urls(ranked, qdomain, TOP_N_FOR_LLM + 4)
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
                sample = _extract_visible_text(body_bytes, company=company)
                if len(sample) < 200:
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


def _ranked_probe_urls(ranked: list[tuple[int, dict]],
                       official_domain: str | None,
                       cap: int) -> list[str]:
    """Return bounded search probes while reserving slots for official hits.

    Soft-domain mode intentionally retains registry/CDN candidates, but those
    results must not crowd every official-company result out of the small and
    expensive sample sent to the verifier.
    """
    all_urls = [c.get("url", "") for _, c in ranked if c.get("url")]
    if not official_domain or SEARCH_OFFICIAL_DOMAIN_RESERVE <= 0:
        return all_urls[:cap]
    official = [u for u in all_urls if _host_matches(u, official_domain)]
    selected = official[:min(SEARCH_OFFICIAL_DOMAIN_RESERVE, cap)]
    selected_set = set(selected)
    for url in all_urls:
        if url not in selected_set:
            selected.append(url)
            selected_set.add(url)
        if len(selected) >= cap:
            break
    return selected


# ═══════════════════════════════════════════════════════════════════════════
# LLM multi-query generation
# ═══════════════════════════════════════════════════════════════════════════
def _parse_llm_json_array(text):
    text = (text or "").strip()
    text = re.sub("^```(?:json)?" + chr(92) + "s*", "", text, flags=re.I)
    text = re.sub(chr(92) + "s*```$", "", text)
    start = text.find("[")
    if start > 0:
        text = text[start:]
    return json.JSONDecoder().raw_decode(text)[0]


def _query_variant_preserves_years(original: str, variant: str) -> bool:
    """LLM search aliases may rephrase a query, but may not alter its years."""
    return _extract_year_intent(original) == _extract_year_intent(variant)


def _llm_generate_search_queries(query, company, domain):
    if _bedrock is None or not ENABLE_LLM_QUERY_GEN:
        return []
    cache_key = query + "||" + str(company) + "||" + str(domain)
    if cache_key in _LLM_QUERY_GEN_CACHE:
        return _LLM_QUERY_GEN_CACHE[cache_key]
    filtered_rules = _filtered_doc_rules(query)
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
        "1. A direct-document query with the exact class and filetype:pdf.\n"
        "2. An IR/investor-subdomain hint query (investors.DOMAIN, "
        "sustainability.DOMAIN, static.DOMAIN) where the file likely lives.\n"
        "3. Regional/naming variants of the class (annual report and accounts, "
        "integrated annual report, BRSR, 10-K, DEF 14A as appropriate).\n\n"
        "Rules: preserve any year exactly; never invent a year. Do not search "
        "filing registries; the caller queries them directly with validated "
        "identifiers. The caller will force each query onto the official "
        "company domain. Keep each query under 200 chars. No markdown. Do NOT "
        "format domains as markdown links.\n\n"
        "Output ONLY a JSON array of strings."
    )
    try:
        text = _converse(prompt, max_tokens=500)
        arr = _parse_llm_json_array(text)
        out = []
        rejected_year_variants = 0
        seen = {query.strip().lower()}
        for q in arr:
            if not isinstance(q, str):
                continue
            q = _demarkdown(q.replace(chr(34), "")).strip()
            if not q or q.lower() in seen:
                continue
            if not _query_variant_preserves_years(query, q):
                rejected_year_variants += 1
                continue
            seen.add(q.lower())
            out.append(q[:200])
        out = out[:LLM_QUERY_GEN_MAX]
        _LLM_QUERY_GEN_CACHE[cache_key] = out
        print("[llm-querygen] generated " + str(len(out)) + " variants for " + repr(query))
        if rejected_year_variants:
            print("[llm-querygen] rejected " + str(rejected_year_variants)
                  + " variant(s) that invented or dropped a year")
        return out
    except Exception as exc:
        print("[llm-querygen] failed (" + str(exc) + "); using regex aliases only")
        _LLM_QUERY_GEN_CACHE[cache_key] = []
        return []


# ═══════════════════════════════════════════════════════════════════════════
# parallel multi-query search fan-out
# ═══════════════════════════════════════════════════════════════════════════
def _parallel_web_search(queries, limit):
    results = {}
    if not queries:
        return results
    workers = max(1, min(SEARCH_FANOUT_WORKERS, len(queries)))

    def _one(q):
        try:
            res = _single_web_search(q, limit)
            # _single_web_search must return a 2-tuple; guard against a code
            # path that returns None so the fan-out map never holds a None value.
            return q, (res if res is not None else ([], "none-returned"))
        except BaseException as exc:  # noqa: BLE001  (also catches CancelledError)
            print(f"[search] worker crashed for {q!r}: {type(exc).__name__}: {exc}")
            return q, ([], "error(" + type(exc).__name__ + ")")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for q, res in pool.map(_one, queries):
            results[q] = res
    return results


# ═══════════════════════════════════════════════════════════════════════════
# sitemap enumeration (Tier 3a)
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


def _sitemap_landing_score(url: str, query: str) -> int:
    """Prioritize likely report/policy hubs without company-specific routes."""
    path = unquote(urlparse(url or "").path).lower()
    query_terms = [t for t in _keywords(query) if len(t) > 3]
    score = sum(10 for term in query_terms if term in path)
    query_text = " ".join(query_terms)

    policy_query = any(term in query_text for term in (
        "conduct", "ethic", "policy", "whistlebl", "bribery", "corruption",
        "remuneration", "human rights", "modern slavery", "tax strategy"))
    sustainability_query = any(term in query_text for term in (
        "sustainab", "environment", "social", "esg", "brsr", "safety"))
    filing_query = any(term in query_text for term in (
        "annual", "financial", "proxy", "filing", "accounts", "statement"))

    if policy_query:
        route_weights = (("policies", 20), ("policy", 16), ("governance", 7),
                         ("compliance", 6), ("investor", 3))
    elif sustainability_query:
        route_weights = (("sustainability", 12), ("esg", 10), ("brsr", 10),
                         ("reports", 5), ("investor", 3))
    elif filing_query:
        route_weights = (("annual-report", 12), ("financial", 10),
                         ("reports", 8), ("filings", 7), ("investor", 4))
    else:
        route_weights = (("investor", 4), ("reports", 3),
                         ("governance", 3), ("policies", 3))
    score += sum(weight for marker, weight in route_weights if marker in path)
    return score


_RECURRING_DOCUMENT_CLASSES = {
    "annual report", "proxy statement", "remuneration report",
    "sustainability report",
}


def _query_needs_recency_scan(query: str) -> bool:
    return any(canonical in _RECURRING_DOCUMENT_CLASSES
               for canonical, _ in _matched_doc_classes(query))


def _strong_sitemap_doc_match(url: str, query: str) -> bool:
    """True when every meaningful query term is present in the document path."""
    path = unquote(urlparse(url or "").path).lower()
    terms = [term for term in _keywords(query) if len(term) > 3]
    return bool(terms) and all(term in path for term in terms)


def _sitemap_resolve(domain, query, verify_fn, known_bad=None, budget=None,
                     reserve_verifies=0):
    cands = _harvest_sitemap(domain, query)
    if not cands:
        return None

    # Sitemaps commonly list an HTML policy/report hub but omit the documents
    # linked from it. Fetch a bounded set of the most relevant landing pages,
    # extract safe document URLs, and merge them with direct sitemap documents.
    direct_docs = [u for u in cands
                   if _is_doc_url(u) and _is_safe_remote_document_url(u)]
    landing_pages = [u for u in cands if not _is_doc_url(u)]
    landing_pages.sort(key=lambda u: _sitemap_landing_score(u, query), reverse=True)
    landing_pages = landing_pages[:SITEMAP_MAX_LANDING_PAGES]
    discovered_sources: dict[str, str] = {}
    landing_pages_inspected = 0
    for page_url in landing_pages:
        if budget is not None and not budget.can_verify(reserve_verifies):
            break
        try:
            page_body, page_ctype = _fetch(page_url)
        except Exception as exc:
            if known_bad is not None:
                known_bad[page_url] = "sitemap landing GET failed: " + type(exc).__name__
            continue
        landing_pages_inspected += 1
        if "html" not in (page_ctype or "").lower():
            continue
        page_docs, _ = _page_doc_candidates(
            page_body, page_url, query, official_domain=domain)
        for doc_url in page_docs[:SITEMAP_MAX_DOC_LINKS_PER_PAGE]:
            if (_is_safe_remote_document_url(doc_url)
                    and not _is_junk_host(doc_url)):
                discovered_sources.setdefault(doc_url, page_url)
        if any(_strong_sitemap_doc_match(doc_url, query)
               for doc_url in discovered_sources):
            # The highest-ranked landing page exposed an exact-looking file.
            # Let the class verifier decide it now instead of fetching every
            # remaining broad sitemap hub first.
            break

    candidate_urls = list(dict.fromkeys(
        list(discovered_sources) + direct_docs))
    ranked_docs = _rank(
        [{"url": u, "title": "", "snippet": ""} for u in candidate_urls],
        query, domain)
    candidate_urls = [c["url"] for _, c in ranked_docs]
    print("[sitemap] inspected " + str(landing_pages_inspected)
          + " landing page(s), discovered " + str(len(discovered_sources))
          + " linked document(s); " + str(len(candidate_urls))
          + " total document candidate(s)")
    if not candidate_urls:
        return None

    verified_hits = []
    tried = 0
    tbudget = BROWSER_MAX_VERIFY_CANDIDATES if verify_fn else 1
    target_years = _extract_year_intent(query)
    scan_for_recency = not target_years and _query_needs_recency_scan(query)
    for cand in candidate_urls:
        if budget is not None and not budget.can_verify(reserve_verifies):
            print("[sitemap] stopped: " + budget.why_stopped(reserve_verifies))
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
        source_page = discovered_sources.get(cand)
        cd["via"] = "sitemap_landing_page" if source_page else "sitemap"
        if source_page:
            cd["source_page"] = source_page
        verified_hits.append(cd)
        if (verify_fn is None
                or target_years.intersection(_extract_year_intent(cand))
                or not scan_for_recency):
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
                       budget: "_QueryBudget | None" = None,
                       reserve_verifies: int = 0) -> dict | None:
    """Tier 3b: recursively fetch same-domain HTML landing pages, regex-crawl
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
        if budget is not None and not budget.can_verify(reserve_verifies):
            print(f"[deep-crawl] stopped: {budget.why_stopped(reserve_verifies)}")
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
        cands, _ = _page_doc_candidates(
            body, url, query, official_domain=domain)
        doc_cands = [u for u in cands if _is_doc_url(u) and not _is_junk_host(u)]

        def _uy(u):
            ys = _extract_year_intent(u)
            return max(ys) if ys else -1

        doc_cands = sorted(doc_cands, key=lambda u: (_verify_priority(u, query), _uy(u)),
                           reverse=True)
        print(f"[deep-crawl] {url} (depth={depth}, page={pages}): "
              f"{len(doc_cands)} doc candidates")
        for cand in doc_cands[:DEEP_STATIC_MAX_DOC_CANDIDATES_PER_PAGE]:
            if budget is not None and not budget.can_verify(reserve_verifies):
                print(f"[deep-crawl] candidate verification stopped: "
                      f"{budget.why_stopped(reserve_verifies)}")
                break
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
            target_years = _extract_year_intent(query)
            if target_years.intersection(_extract_year_intent(cand)):
                break
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


# ─── Orchestrator ─────────────────────────────────────────────────────────────
def _invoke_sync(payload: dict) -> dict:
    run_id = (payload or {}).get("run_id") or uuid.uuid4().hex[:8]

    # ── Company context: accepts a structured dict OR a legacy string ──
    _raw_company = (payload or {}).get("company")
    if isinstance(_raw_company, dict):
        company_ctx = {
            "name": _raw_company.get("name") or "unknown",
            "domain": _raw_company.get("domain") or "",
            "jurisdiction": (_raw_company.get("jurisdiction") or "").lower(),
            "ticker": _raw_company.get("ticker") or "",
            "cik": _raw_company.get("cik") or "",
            "companies_house_number": _raw_company.get("companies_house_number") or "",
        }
    else:
        company_ctx = {
            "name": _raw_company or "unknown",
            "domain": (payload or {}).get("domain") or "",
            "jurisdiction": ((payload or {}).get("jurisdiction") or "").lower(),
            "ticker": (payload or {}).get("ticker") or "",
            "cik": (payload or {}).get("cik") or "",
            "companies_house_number": (payload or {}).get("companies_house_number") or "",
        }

    company_ctx["domain"] = _clean_domain(company_ctx.get("domain"))
    if not company_ctx["domain"]:
        # Legacy portal payloads commonly place the official domain only in a
        # site: operator. Recover it before identity resolution so the grounded
        # lookup and every downstream official-source gate share one domain.
        domain_inputs = [
            (payload or {}).get("search_query") or "",
            *[
                value for key, value in sorted((payload or {}).items())
                if re.match(r"web_query\d+$", str(key), re.I)
            ],
        ]
        for value in domain_inputs:
            inferred = _domain(str(value or ""))
            if inferred:
                company_ctx["domain"] = inferred
                break

    identity_hint = _vertex_company_identity_hint(company_ctx)
    company_ctx = registry_tier.enrich_company_identity(
        company_ctx, identity_hint=identity_hint)
    company_raw = company_ctx["name"]
    company = _slug(company_raw)

    # ── Per-run browser switch (Tier 4). Browser runs only when the deploy has
    #    USE_BROWSER=true AND the run did not explicitly disable it. ──
    _be = (payload or {}).get("browser_enabled")
    _use_browser = USE_BROWSER if _be is None else (USE_BROWSER and bool(_be))

    region_override = (payload or {}).get("alias_region")
    if region_override is None and company_ctx.get("jurisdiction"):
        region_override = _normalize_alias_region(company_ctx["jurisdiction"])
    if region_override is None:
        region_override = _infer_alias_region_from_domain(company_ctx.get("domain"))

    # ── Build work items from structured `reports` OR legacy web_query<N> ──
    work_items: list[dict] = []
    reports = (payload or {}).get("reports")
    if isinstance(reports, list) and reports:
        _dom = company_ctx.get("domain") or ""
        for _report_index, _r in enumerate(reports, start=1):
            if not isinstance(_r, dict):
                continue
            _rc = str(_r.get("report_class") or _r.get("class") or "").strip()
            if not _rc:
                continue
            try:
                _yr = int(_r.get("year")) if _r.get("year") else None
            except Exception:  # noqa: BLE001
                _yr = None
            _parts = [company_raw, _rc] + ([str(_yr)] if _yr else [])
            _q = " ".join(_parts)
            if _dom:
                _q = f"{_q} site:{_dom}"
            work_items.append({
                "raw": _q,
                "original_query": str(_r.get("query") or _q),
                "request_id": str(_r.get("request_id") or f"report:{_report_index}"),
                "report_class": _rc.lower(),
                "year": _yr,
            })
    else:
        _query_ids = ((payload or {}).get("web_query_ids") or {})
        if not isinstance(_query_ids, dict):
            _query_ids = {}

        def _web_query_index(item):
            match = re.search(r"(\d+)$", str(item[0]))
            return int(match.group(1)) if match else 0

        _queries = sorted([
            (str(k).strip(), v) for k, v in (payload or {}).items()
            if re.match(r"web_query\d+$", str(k).strip(), re.I)
            and v and str(v).strip()
        ], key=_web_query_index)
        for _key, _v in _queries:
            work_items.append({
                "raw": str(_v),
                "original_query": str(_v),
                "request_id": str(_query_ids.get(_key) or _key.lower()),
                "report_class": None,
                "year": None,
            })

    if not work_items:
        return {"error": "payload had neither a `reports` array nor web_query<N> "
                         "fields", "run_id": run_id}

    diag: dict = {
        "run_id": run_id,
        "company": company,
        "company_raw": company_raw,
        "company_identity": company_ctx.get("_identity_validation") or {},
        "identity_grounding_sources": identity_hint.get("_grounding_sources", []),
        "code_version": CODE_VERSION,
        "search_backend": SEARCH_BACKEND,
        "use_browser_deploy": USE_BROWSER,
        "browser_enabled_run": _use_browser,
        "tier2_registry": ENABLE_REGISTRY_TIER,
        "v39_filing_fallback_all_classes": FILING_FALLBACK_ALL_CLASSES,
        "alias_region": region_override,
        "work_item_count": len(work_items),
        "per_query": [],
    }

    stored: list[dict] = []
    duplicates: list[dict] = []
    failures: list[dict] = []
    stored_by_key: dict[str, dict] = {}
    done_queries: set[str] = set()
    query_results: list[dict] = []
    _root_crawl_cache: dict[str, dict] = {}
    _known_bad: dict[str, str] = {}

    def _material_for(url: str, found: dict) -> tuple[bytes | None, str]:
        for ci in found.get("candidate_infos", []):
            if ci.get("url") == url and ci.get("_body") is not None:
                return ci["_body"], ci.get("head_ctype", "")
        try:
            return _fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"[resolve] material fetch failed ({url}): {exc}")
            return None, ""

    def _resolve_from_html(page_url: str, page_body: bytes, dom: str | None,
                           query: str, vfn, budget) -> dict | None:
        cands, _ = _page_doc_candidates(
            page_body, page_url, query, official_domain=dom)
        doc_cands = [u for u in cands
                     if _is_doc_url(u) and not _is_junk_host(u)
                     and not _is_press_release_url(u)]
        doc_cands = _sort_for_verify(doc_cands, query)
        hits: list[dict] = []
        for cand in doc_cands[:BROWSER_MAX_VERIFY_CANDIDATES]:
            if budget is not None and not budget.time_left():
                break
            if _known_bad.get(cand):
                continue
            try:
                cb, cc = _fetch(cand)
            except Exception as exc:  # noqa: BLE001
                _known_bad[cand] = f"html-crawl GET failed: {type(exc).__name__}"
                continue
            if not (_is_doc_ctype(cc) or (_is_doc_url(cand)
                    and "html" not in (cc or "").lower())):
                continue
            cd = {"url": cand, "body": cb, "ctype": cc}
            if vfn is not None and not vfn(cd):
                continue
            cd["via"] = "search+html_crawl"
            hits.append(cd)
            target_years = _extract_year_intent(query)
            if target_years.intersection(_extract_year_intent(cand)):
                break
        if not hits:
            return None
        hits.sort(key=lambda d: (max(_extract_year_intent(d["url"]))
                  if _extract_year_intent(d["url"]) else -1), reverse=True)
        return hits[0]

    def _commit(resolved: dict, prepared: str, stage: str,
                base_log: dict, work_item: dict) -> dict:
        rec = _store(
            company, run_id, resolved["url"], resolved["body"],
            resolved["ctype"], "",
            original_query=work_item["original_query"],
            prepared_query=prepared,
            request_id=work_item["request_id"],
            report_class=work_item.get("report_class"),
            year=base_log.get("known_year"),
            company_ctx=company_ctx,
        )
        rec["stage"] = stage
        if rec["s3_key"] in stored_by_key:
            base_log["status"] = "duplicate_existing"
        else:
            stored_by_key[rec["s3_key"]] = rec
            (stored if rec["status"] == "stored" else duplicates).append(rec)
            base_log["status"] = ("ok" if rec["status"] == "stored"
                                  else "duplicate_existing")
        base_log["resolved_via"] = resolved.get("via", stage)
        base_log["stage"] = stage
        base_log["documents"] = [rec["s3_key"]]
        base_log["result"] = {
            **rec,
            "duplicate": rec.get("status") == "duplicate",
            "status": "downloaded",
            "stage": stage,
        }
        print(f"[store] {rec['status'].upper()} ({stage}): {prepared!r} "
              f"-> {rec['s3_key']}")
        return rec

    # ─────────────────────────── per-report loop ───────────────────────────
    for _item in work_items:
        raw = _item["raw"]
        original_query = _item["original_query"]
        request_id = _item["request_id"]
        known_class = _item.get("report_class")
        known_year = _item.get("year")
        prepared = _prepare_query(str(raw))
        prepared, inferred_year = _apply_latest_completed_fiscal_year(
            prepared, known_class=known_class, known_year=known_year)
        effective_year = known_year or inferred_year
        if company_ctx.get("domain"):
            # The company context is established before discovery. Never let a
            # legacy payload or generated query redirect web discovery to a
            # different site.
            prepared = _scope_to_official_domain(
                prepared, company_ctx["domain"])
        if not prepared:
            continue
        done_queries.add(prepared)
        _budget = _QueryBudget()

        matched_classes = [c for c, _ in _matched_doc_classes(prepared)]
        domain = _domain(prepared) or company_ctx.get("domain") or None
        browser_reserve = BROWSER_RESERVED_VERIFIES if _use_browser else 0
        prebrowser_verify_fn = _make_browser_verify_fn(
            prepared, budget=_budget, company=company_raw,
            reserve_verifies=browser_reserve)
        browser_verify_fn = _make_browser_verify_fn(
            prepared, budget=_budget, company=company_raw)

        base_log: dict = {
            "raw": str(raw).strip(),
            "original_query": original_query,
            "request_id": request_id,
            "prepared": prepared,
            "known_class": known_class,
            "known_year": effective_year,
            "matched_classes": matched_classes,
            "status": "pending",
            "resolved_via": None,
            "stage": None,
            "documents": [],
        }

        resolved: dict | None = None
        stage: str | None = None
        decision = {
            "selected_url": None,
            "topic_match": False,
            "company_match": False,
            "year_match": False,
            "reason": "search-not-run",
        }
        found: dict = {"candidate_infos": []}

        _reg_class = known_class or (
            matched_classes[0] if matched_classes else None)
        _reg_year = effective_year or (
            max(_extract_year_intent(prepared))
            if _extract_year_intent(prepared) else None)
        _identity_validated = (
            (company_ctx.get("_identity_validation") or {}).get("status")
            == "validated")
        _registry_identity_allowed = (
            not REQUIRE_VALIDATED_REGISTRY_IDENTITY
            or _identity_validated
            or bool(company_ctx.get("companies_house_number"))
        )
        _registry_eligible = bool(
            ENABLE_REGISTRY_TIER
            and _reg_class
            and report_specs.registries_for(_reg_class)
            and _registry_identity_allowed
        )
        route = _discovery_route(_reg_class, _registry_eligible)
        attempted_stages: list[str] = []
        base_log["route"] = route
        base_log["official_domain"] = domain
        base_log["web_discovery_allowed"] = bool(
            domain or not REQUIRE_OFFICIAL_DOMAIN_FOR_WEB)
        registry_attempted = False

        # Registry-first remains configurable, but the deployment default is
        # empty: annual/proxy requests exhaust the official-company path before
        # using the validated CIK on SEC EDGAR.
        if route and route[0] == "registry":
            attempted_stages.append("registry")
            registry_attempted = True
            reg = registry_tier.registry_resolve(
                company_ctx, _reg_class, _reg_year,
                verify_fn=prebrowser_verify_fn)
            if reg and reg.get("body"):
                resolved = reg
                stage = "registry"
                decision["reason"] = "deterministic-registry-first"
                base_log["decision_reason"] = decision["reason"]

        # ── Direct document URL search through Google-grounded Vertex ──
        # Every variant is forced onto the established official domain. A
        # validated ticker gets a dedicated alias probe; the exact CIK is kept
        # for the registry API rather than used as noisy free-text.
        search_queries: list[str] = []
        if (resolved is None and "direct_search" in route
                and (domain or not REQUIRE_OFFICIAL_DOMAIN_FOR_WEB)):
            attempted_stages.append("direct_search")
            aliases = _alias_queries(prepared, region_override)
            generated = _llm_generate_search_queries(
                prepared, company_raw, domain)
            search_queries = _official_search_queries(
                prepared, company_ctx, aliases, generated)
            found = _find_best_document(
                search_queries, MAX_RESULTS, company=company_raw)
            decision = found["decision"]
            base_log["via"] = found.get("via")
            base_log["domain_mode"] = found.get("domain_mode")
            base_log["decision_reason"] = decision.get("reason")

            if (_confident(decision, prepared)
                    and decision.get("selected_url")):
                sel = decision["selected_url"]
                body, ctype = _material_for(sel, found)
                if body is not None:
                    if _is_doc_ctype(ctype) or _is_doc_url(sel):
                        resolved = {
                            "url": sel, "body": body,
                            "ctype": ctype or "application/pdf",
                            "via": "search",
                        }
                        stage = "search"
                    else:
                        dug = _resolve_from_html(
                            sel, body, domain, prepared,
                            prebrowser_verify_fn, _budget)
                        if dug:
                            resolved = dug
                            stage = "search+html_crawl"
        elif resolved is None and REQUIRE_OFFICIAL_DOMAIN_FOR_WEB and not domain:
            decision["reason"] = "official-domain-unresolved-web-search-skipped"
            base_log["decision_reason"] = decision["reason"]
            print(f"[search] skipped {prepared!r}: no attested official domain")

        # ── Official website crawl: sitemap + bounded landing pages ──
        if resolved is None and "official_crawl" in route and domain:
            attempted_stages.append("official_crawl")
            sm = _sitemap_resolve(
                domain, prepared, prebrowser_verify_fn,
                known_bad=_known_bad, budget=_budget,
                reserve_verifies=browser_reserve)
            if sm:
                resolved = sm
                stage = "sitemap"

        # ── In-depth same-domain static crawl from the official root ──
        if resolved is None and "deep_crawl" in route and domain:
            root = _site_root(prepared) or (f"https://{domain}/" if domain else None)
            if root:
                attempted_stages.append("deep_crawl")
                dc = _deep_static_crawl(
                    root, domain, prepared, prebrowser_verify_fn,
                    known_bad=_known_bad, budget=_budget,
                    reserve_verifies=browser_reserve)
                if dc:
                    resolved = dc
                    stage = "deep_crawl"

        # ── JavaScript/deep-navigation browser fallback ──
        if (resolved is None and "browser" in route and _use_browser
                and domain):
            root = _site_root(prepared) or (f"https://{domain}/" if domain else None)
            if root:
                attempted_stages.append("browser")
                br = _browser_resolve_document(
                    root, domain, prepared, cache=_root_crawl_cache,
                    verify_fn=browser_verify_fn, known_bad=_known_bad,
                    budget=_budget)
                if br and br.get("body"):
                    resolved = br
                    stage = "browser"

        # Registry lookup is the final fallback in the deployed route. Annual
        # reports and proxy statements reach EDGAR here only after official
        # search, sitemap/landing pages, deep crawl, and browser all fail.
        if (resolved is None and "registry" in route
                and not registry_attempted):
            attempted_stages.append("registry")
            registry_attempted = True
            reg = registry_tier.registry_resolve(
                company_ctx, _reg_class, _reg_year,
                verify_fn=browser_verify_fn)
            if reg and reg.get("body"):
                resolved = reg
                stage = "registry"
        elif (ENABLE_REGISTRY_TIER and _reg_class
              and report_specs.registries_for(_reg_class)
              and not _registry_identity_allowed):
            print(f"[registry] skipped {_reg_class!r}: company identity is not "
                  f"validated and no authoritative company number was supplied")

        base_log["attempted_stages"] = attempted_stages

        if resolved is not None:
            rec = _commit(
                resolved, prepared, stage or "unknown", base_log, _item)
            query_results.append({
                **rec,
                "duplicate": rec.get("status") == "duplicate",
                "status": "downloaded",
                "stage": stage or "unknown",
            })
        else:
            base_log["status"] = "no_document_found"
            failures.append({
                "request_id": request_id,
                "query": original_query,
                "prepared_query": prepared,
                "reason": (decision.get("reason") or "no class-verified document "
                           "found through any tier (failed closed)"),
            })
            query_results.append({
                "request_id": request_id,
                "query": original_query,
                "prepared_query": prepared,
                "status": "failed",
                "reason": (decision.get("reason") or
                           "no class-verified document found"),
            })
            print(f"[result] NO DOCUMENT FOUND (failed closed): {prepared!r}")

        diag["per_query"].append(base_log)

    if company == "unknown" and stored:
        # best-effort: adopt the slug of the first stored doc's key prefix
        try:
            company = stored[0]["s3_key"].split("/", 1)[0]
            diag["company"] = company
        except Exception:  # noqa: BLE001
            pass

    diag["gateway_debug"] = _GW_DEBUG or None
    return {
        "run_id": run_id,
        "company": company,
        "stored": stored,
        "duplicates": duplicates,
        "results": query_results,
        "no_document_found": failures,
        "counts": {
            "stored": len(stored),
            "duplicates": len(duplicates),
            "not_found": len(failures),
            "queries": len(query_results),
        },
        "diagnostics": diag,
    }


if __name__ == "__main__":
    app.run()
