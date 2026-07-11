"""Registry-first corporate document retrieval runtime.

This runtime intentionally treats a request as one document need, not a set of
web searches.  It tries authoritative registries first, then links discovered
from verified company domains, then an optional *site-scoped* search provider.
Only one positively validated document is stored for each request.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - deployment installs pypdf
    PdfReader = None


app = BedrockAgentCoreApp()

BUCKET = os.environ.get("REPORTS_BUCKET", "")
PROVENANCE_TABLE = os.environ.get("PROVENANCE_TABLE", "")
REGION = os.environ.get("APP_REGION") or os.environ.get("AWS_REGION", "us-east-1")
MAX_CANDIDATES_PER_TIER = int(os.environ.get("MAX_CANDIDATES_PER_TIER", "8"))
SITEMAP_MAX_URLS = int(os.environ.get("SITEMAP_MAX_URLS", "2000"))
FETCH_TIMEOUT_SECONDS = int(os.environ.get("FETCH_TIMEOUT_SECONDS", "45"))
MAX_DOCUMENT_BYTES = int(os.environ.get("MAX_DOCUMENT_BYTES", str(80 * 1024 * 1024)))
REQUIRE_LLM_VALIDATION = os.environ.get("REQUIRE_LLM_VALIDATION", "true").lower() != "false"
LLM_MODEL_ID = os.environ.get("LLM_MODEL_ID", "").strip()
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
GOOGLE_CX = os.environ.get("GOOGLE_CX", "").strip()
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
COMPANIES_HOUSE_API_KEY = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
PRESIGN_EXPIRY_SECONDS = int(os.environ.get("PRESIGN_EXPIRY_SECONDS", "3600"))

_s3 = boto3.client("s3", region_name=REGION) if BUCKET else None
_table = boto3.resource("dynamodb", region_name=REGION).Table(PROVENANCE_TABLE) if PROVENANCE_TABLE else None
_bedrock = boto3.client("bedrock-runtime", region_name=REGION) if LLM_MODEL_ID else None
_last_sec_request = 0.0
_sec_ticker_cache: dict[str, str] | None = None

CONFIG_PATH = Path(__file__).with_name("config") / "document_types.json"


def _load_document_rules() -> dict[str, dict[str, Any]]:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        rules = json.load(config_file)
    return {entry["id"]: entry for entry in rules["document_types"]}


DOCUMENT_RULES = _load_document_rules()


@dataclass(frozen=True)
class Company:
    legal_name: str
    official_domains: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    ticker: str | None = None
    cik: str | None = None
    country: str | None = None
    companies_house_number: str | None = None
    trusted_document_hosts: tuple[str, ...] = ()

    @property
    def identity_terms(self) -> tuple[str, ...]:
        values = [self.legal_name, *self.aliases]
        return tuple(value.lower().strip() for value in values if len(value.strip()) >= 3)


@dataclass(frozen=True)
class DocumentRequest:
    id: str
    document_type: str
    year: int | None = None
    allow_search: bool = True


@dataclass
class Candidate:
    url: str
    source_tier: str
    title: str = ""
    discovered_from: str | None = None
    registry_identity_verified: bool = False
    body: bytes | None = None
    content_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Validation:
    accepted: bool
    score: int
    reasons: list[str]
    title: str
    extracted_text: str
    llm_verified: bool | None = None


class RetrievalError(Exception):
    """A recoverable discovery or fetch error."""


def _normalise_domain(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^https?://", "", value)
    return value.split("/", 1)[0].removeprefix("www.")


def _host_matches(url: str, allowed_domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    domain = _normalise_domain(allowed_domain)
    return host == domain or host.endswith("." + domain)


def _is_allowed_candidate(candidate: Candidate, company: Company) -> bool:
    """Allow a company host, or a trusted CDN linked from a company host."""
    if any(_host_matches(candidate.url, domain) for domain in company.official_domains):
        return True
    if not candidate.discovered_from:
        return False
    was_discovered_on_company_site = any(
        _host_matches(candidate.discovered_from, domain) for domain in company.official_domains
    )
    return was_discovered_on_company_site and any(
        _host_matches(candidate.url, host) for host in company.trusted_document_hosts
    )


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": "ReportIQ/2.0 document-retrieval",
        "Accept": "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    headers.update(extra or {})
    return headers


def _fetch(url: str, headers: dict[str, str] | None = None) -> tuple[bytes, str]:
    request = Request(url, headers=_headers(headers))  # noqa: S310 - URLs are validated by source tiers.
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
            body = response.read(MAX_DOCUMENT_BYTES + 1)
            if len(body) > MAX_DOCUMENT_BYTES:
                raise RetrievalError(f"document exceeds {MAX_DOCUMENT_BYTES} byte limit")
            content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].lower()
            return body, content_type
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RetrievalError(f"fetch failed: {type(exc).__name__}") from exc


def _fetch_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    body, _ = _fetch(url, headers)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RetrievalError("source returned invalid JSON") from exc


def _sec_fetch_json(url: str) -> dict[str, Any]:
    """EDGAR requires a real contact User-Agent and limits clients to 10 req/s."""
    global _last_sec_request
    if not SEC_USER_AGENT or "@" not in SEC_USER_AGENT:
        raise RetrievalError("SEC_USER_AGENT must contain organisation name and contact email")
    wait = 0.125 - (time.monotonic() - _last_sec_request)
    if wait > 0:
        time.sleep(wait)
    _last_sec_request = time.monotonic()
    return _fetch_json(url, {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"})


def _sec_cik_for_ticker(ticker: str | None) -> str | None:
    """Resolve a supplied US ticker through SEC's official ticker map."""
    global _sec_ticker_cache
    if not ticker:
        return None
    if _sec_ticker_cache is None:
        data = _sec_fetch_json("https://www.sec.gov/files/company_tickers.json")
        _sec_ticker_cache = {
            str(item.get("ticker", "")).upper(): str(item["cik_str"])
            for item in data.values()
            if item.get("ticker") and item.get("cik_str") is not None
        }
    return _sec_ticker_cache.get(ticker.upper().strip())


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value or "unknown"


def _safe_filename(url: str, content_type: str) -> str:
    filename = urlparse(url).path.rsplit("/", 1)[-1] or "document"
    if "." not in filename:
        filename += ".pdf" if "pdf" in content_type else ".html"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", filename)[:160]


def _plain_text(body: bytes, content_type: str) -> str:
    if "pdf" in content_type or body.startswith(b"%PDF"):
        if PdfReader is None:
            return ""
        try:
            reader = PdfReader(io.BytesIO(body))
            return "\n".join((page.extract_text() or "") for page in reader.pages[:8])[:30000]
        except Exception:
            return ""
    raw = body.decode("utf-8", "ignore")
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(re.sub(r"\s+", " ", raw))[:30000]


def _contains_phrase(text: str, phrases: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(phrase.lower() in lowered for phrase in phrases)


def _year_matches(text: str, requested_year: int | None) -> bool:
    if requested_year is None:
        return True
    years = {int(found) for found in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)}
    return requested_year in years


def _candidate_title(candidate: Candidate, text: str) -> str:
    if candidate.title:
        return candidate.title
    match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else _safe_filename(candidate.url, candidate.content_type)


def _llm_confirms(company: Company, request: DocumentRequest, candidate: Candidate, title: str, text: str) -> bool:
    if _bedrock is None:
        return not REQUIRE_LLM_VALIDATION
    prompt = {
        "task": "Confirm whether this is the exact requested corporate document. Reject near matches and different companies.",
        "company": company.legal_name,
        "document_type": request.document_type,
        "requested_year": request.year,
        "candidate": {"url": candidate.url, "title": title, "source_tier": candidate.source_tier, "excerpt": text[:5000]},
        "response_schema": {"accept": "boolean", "reason": "short string"},
    }
    try:
        response = _bedrock.converse(
            modelId=LLM_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": json.dumps(prompt)}]}],
            inferenceConfig={"maxTokens": 120, "temperature": 0},
        )
        output = "".join(part.get("text", "") for part in response["output"]["message"]["content"])
        output = re.sub(r"^```(?:json)?\s*|\s*```$", "", output.strip(), flags=re.I)
        return bool(json.loads(output).get("accept"))
    except Exception as exc:  # Fail closed when the model is required.
        print(f"[verify] LLM validation failed closed: {type(exc).__name__}")
        return False


def _validate(candidate: Candidate, company: Company, request: DocumentRequest) -> Validation:
    rule = DOCUMENT_RULES[request.document_type]
    if candidate.body is None:
        return Validation(False, 0, ["candidate was not fetched"], "", "")
    text = _plain_text(candidate.body, candidate.content_type)
    title = _candidate_title(candidate, text)
    evidence = " ".join([candidate.url, title, text])
    reasons: list[str] = []
    score = 0

    if not ("pdf" in candidate.content_type or "html" in candidate.content_type or candidate.body.startswith(b"%PDF")):
        return Validation(False, 0, ["unsupported content type"], title, text)
    score += 10
    if candidate.source_tier == "registry":
        score += 35
        reasons.append("official registry")
    elif _is_allowed_candidate(candidate, company):
        score += 25
        reasons.append("official-domain provenance")
    else:
        return Validation(False, 0, ["untrusted source domain"], title, text)

    # A report can mention a neighbouring document in its body. Exclusions only
    # disqualify a candidate when its own title or URL identifies it as that type.
    if _contains_phrase(" ".join([candidate.url, title]), rule["reject_terms"]):
        return Validation(False, score, ["contains rejected neighbouring document class"], title, text)
    if _contains_phrase(" ".join([candidate.url, title]), rule["aliases"]):
        score += 30
        reasons.append("document type in URL or title")
    if _contains_phrase(text, rule["aliases"]):
        score += 30
        reasons.append("document type in document text")
    if score < 55:
        return Validation(False, score, reasons + ["requested document class not evidenced"], title, text)

    company_in_text = _contains_phrase(evidence, company.identity_terms)
    if company_in_text:
        score += 25
        reasons.append("company identity in document")
    elif candidate.registry_identity_verified:
        score += 25
        reasons.append("registry identity verified")
    elif _is_allowed_candidate(candidate, company):
        score += 10
        reasons.append("company ownership inferred from official domain")
    else:
        return Validation(False, score, reasons + ["company identity not evidenced"], title, text)

    if not _year_matches(evidence, request.year):
        return Validation(False, score, reasons + ["requested year not evidenced"], title, text)
    if request.year is not None:
        score += 20
        reasons.append("requested year matched")

    deterministic_accept = score >= 80
    llm_verified: bool | None = None
    if deterministic_accept:
        llm_verified = _llm_confirms(company, request, candidate, title, text)
        if not llm_verified:
            return Validation(False, score, reasons + ["LLM verification rejected or unavailable"], title, text, False)
    return Validation(deterministic_accept, score, reasons, title, text, llm_verified)


def _sec_candidates(company: Company, request: DocumentRequest) -> list[Candidate]:
    if request.document_type not in {"annual_report", "proxy_statement"}:
        return []
    forms = {"annual_report": {"10-K", "20-F", "40-F"}, "proxy_statement": {"DEF 14A"}}[request.document_type]
    cik_value = company.cik or _sec_cik_for_ticker(company.ticker)
    if not cik_value:
        return []
    cik = str(cik_value).zfill(10)
    data = _sec_fetch_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = data.get("filings", {}).get("recent", {})
    candidates: list[Candidate] = []
    for index, form in enumerate(recent.get("form", [])):
        if form not in forms:
            continue
        report_date = str((recent.get("reportDate", []) or [""])[index])
        filing_date = str((recent.get("filingDate", []) or [""])[index])
        if request.year and str(request.year) not in {report_date[:4], filing_date[:4]}:
            continue
        accession = str(recent["accessionNumber"][index])
        document = str((recent.get("primaryDocument", []) or [""])[index])
        if not document:
            continue
        archive_cik = str(int(cik))
        url = f"https://www.sec.gov/Archives/edgar/data/{archive_cik}/{accession.replace('-', '')}/{document}"
        candidates.append(Candidate(
            url=url,
            source_tier="registry",
            title=f"{form} {report_date or filing_date}",
            registry_identity_verified=True,
            metadata={"registry": "sec_edgar", "form": form, "accession": accession},
        ))
    return candidates[:MAX_CANDIDATES_PER_TIER]


def _companies_house_candidates(company: Company, request: DocumentRequest) -> list[Candidate]:
    if not COMPANIES_HOUSE_API_KEY or not company.companies_house_number or request.document_type != "annual_report":
        return []
    auth = base64.b64encode(f"{COMPANIES_HOUSE_API_KEY}:".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    number = company.companies_house_number
    data = _fetch_json(f"https://api.company-information.service.gov.uk/company/{number}/filing-history?items_per_page=100", headers)
    candidates: list[Candidate] = []
    for item in data.get("items", []):
        if item.get("category") != "accounts":
            continue
        date = str(item.get("date", ""))
        if request.year and not _year_matches(date, request.year):
            continue
        document_link = (item.get("links") or {}).get("document_metadata")
        if not document_link:
            continue
        document_id = document_link.rstrip("/").rsplit("/", 1)[-1]
        candidates.append(Candidate(
            url=f"https://document-api.company-information.service.gov.uk/document/{document_id}/content",
            source_tier="registry",
            title=str(item.get("description", "Companies House accounts")),
            registry_identity_verified=True,
            metadata={"registry": "companies_house", "filing_date": date, "auth": "companies_house"},
        ))
    return candidates[:MAX_CANDIDATES_PER_TIER]


def _registry_candidates(company: Company, request: DocumentRequest, diagnostics: list[str]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for provider in (_sec_candidates, _companies_house_candidates):
        try:
            candidates.extend(provider(company, request))
        except RetrievalError as exc:
            diagnostics.append(f"{provider.__name__}: {exc}")
    return candidates[:MAX_CANDIDATES_PER_TIER]


def _extract_links(page: bytes, page_url: str, company: Company) -> list[Candidate]:
    text = page.decode("utf-8", "ignore")
    links: list[Candidate] = []
    seen: set[str] = set()
    for match in re.finditer(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", text, flags=re.I | re.S):
        url = urljoin(page_url, html.unescape(match.group(1)).strip())
        label = re.sub(r"<[^>]+>", " ", match.group(2))
        label = html.unescape(re.sub(r"\s+", " ", label)).strip()
        if url in seen or not url.startswith(("https://", "http://")):
            continue
        candidate = Candidate(url=url, source_tier="official_site", title=label, discovered_from=page_url)
        if _is_allowed_candidate(candidate, company):
            seen.add(url)
            links.append(candidate)
    return links


def _score_link(candidate: Candidate, request: DocumentRequest) -> int:
    rule = DOCUMENT_RULES[request.document_type]
    haystack = (candidate.url + " " + candidate.title).lower()
    score = sum(10 for alias in rule["aliases"] if alias in haystack)
    if ".pdf" in urlparse(candidate.url).path.lower():
        score += 8
    if request.year and str(request.year) in haystack:
        score += 8
    if any(term in haystack for term in rule["reject_terms"]):
        score -= 100
    return score


def _sitemap_urls(domain: str) -> list[str]:
    root = f"https://{_normalise_domain(domain)}"
    try:
        body, _ = _fetch(root + "/sitemap.xml")
        root_xml = ET.fromstring(body)
    except (RetrievalError, ET.ParseError):
        return []
    locations = [element.text.strip() for element in root_xml.iter() if element.tag.endswith("loc") and element.text]
    if locations and all(location.endswith(".xml") for location in locations[: min(5, len(locations))]):
        expanded: list[str] = []
        for location in locations[:10]:
            try:
                nested, _ = _fetch(location)
                nested_xml = ET.fromstring(nested)
                expanded.extend(element.text.strip() for element in nested_xml.iter() if element.tag.endswith("loc") and element.text)
            except (RetrievalError, ET.ParseError):
                continue
        locations = expanded
    return [location for location in locations if _host_matches(location, domain)][:SITEMAP_MAX_URLS]


def _official_site_candidates(company: Company, request: DocumentRequest, diagnostics: list[str]) -> list[Candidate]:
    all_candidates: list[Candidate] = []
    rule = DOCUMENT_RULES[request.document_type]
    for domain in company.official_domains:
        root = f"https://{_normalise_domain(domain)}"
        seed_paths = ["/", *rule["site_paths"]]
        pages = [urljoin(root, path) for path in seed_paths]
        try:
            pages.extend(_sitemap_urls(domain))
        except Exception as exc:  # Sitemap is a recall aid, never a reason to fail the request.
            diagnostics.append(f"sitemap {domain}: {type(exc).__name__}")
        for page_url in pages[:SITEMAP_MAX_URLS]:
            haystack = page_url.lower()
            if page_url not in pages[:len(seed_paths)] and not _contains_phrase(haystack, rule["aliases"]):
                continue
            try:
                body, content_type = _fetch(page_url)
            except RetrievalError:
                continue
            if "html" in content_type:
                all_candidates.extend(_extract_links(body, page_url, company))
            else:
                all_candidates.append(Candidate(page_url, "official_site", discovered_from=page_url))
    unique = {candidate.url: candidate for candidate in all_candidates}
    return sorted(unique.values(), key=lambda item: _score_link(item, request), reverse=True)[:MAX_CANDIDATES_PER_TIER]


def _google_candidates(company: Company, request: DocumentRequest) -> list[Candidate]:
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return []
    rule = DOCUMENT_RULES[request.document_type]
    query = " ".join([company.legal_name, rule["search_terms"][0], str(request.year or ""), "filetype:pdf"]).strip()
    candidates: list[Candidate] = []
    for domain in company.official_domains:
        params = urlencode({"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "siteSearch": _normalise_domain(domain), "siteSearchFilter": "i", "num": 10})
        try:
            data = _fetch_json(f"https://www.googleapis.com/customsearch/v1?{params}")
        except RetrievalError:
            continue
        for item in data.get("items", []):
            url = str(item.get("link", ""))
            candidate = Candidate(url, "site_scoped_search", str(item.get("title", "")))
            if _is_allowed_candidate(candidate, company):
                candidates.append(candidate)
    return candidates


def _brave_candidates(company: Company, request: DocumentRequest) -> list[Candidate]:
    if not BRAVE_SEARCH_API_KEY:
        return []
    rule = DOCUMENT_RULES[request.document_type]
    candidates: list[Candidate] = []
    for domain in company.official_domains:
        query = f'site:{_normalise_domain(domain)} "{rule["search_terms"][0]}" {request.year or ""} filetype:pdf'
        try:
            data = _fetch_json("https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": 10}), {"X-Subscription-Token": BRAVE_SEARCH_API_KEY})
        except RetrievalError:
            continue
        for item in (data.get("web") or {}).get("results", []):
            candidate = Candidate(str(item.get("url", "")), "site_scoped_search", str(item.get("title", "")))
            if _is_allowed_candidate(candidate, company):
                candidates.append(candidate)
    return candidates


def _search_candidates(company: Company, request: DocumentRequest) -> list[Candidate]:
    candidates = _google_candidates(company, request) or _brave_candidates(company, request)
    unique = {candidate.url: candidate for candidate in candidates if candidate.url}
    return sorted(unique.values(), key=lambda item: _score_link(item, request), reverse=True)[:MAX_CANDIDATES_PER_TIER]


def _fetch_and_select(candidates: Iterable[Candidate], company: Company, request: DocumentRequest, diagnostics: list[str]) -> tuple[Candidate | None, Validation | None]:
    accepted: list[tuple[Candidate, Validation]] = []
    for candidate in candidates:
        try:
            headers = None
            if candidate.source_tier == "registry" and "sec.gov" in candidate.url:
                headers = {"User-Agent": SEC_USER_AGENT}
            elif candidate.metadata.get("auth") == "companies_house":
                auth = base64.b64encode(f"{COMPANIES_HOUSE_API_KEY}:".encode()).decode()
                headers = {"Authorization": f"Basic {auth}"}
            candidate.body, candidate.content_type = _fetch(candidate.url, headers)
        except RetrievalError as exc:
            diagnostics.append(f"{candidate.source_tier} fetch {candidate.url}: {exc}")
            continue
        validation = _validate(candidate, company, request)
        if validation.accepted:
            accepted.append((candidate, validation))
    if not accepted:
        return None, None
    return max(accepted, key=lambda item: item[1].score)


def _store(company: Company, request: DocumentRequest, run_id: str, candidate: Candidate, validation: Validation) -> dict[str, Any]:
    assert candidate.body is not None
    digest = hashlib.sha256(candidate.body).hexdigest()
    year = str(request.year) if request.year else "undated"
    s3_key = f"{_slug(company.legal_name)}/{request.document_type}/{year}/{digest[:12]}-{_safe_filename(candidate.url, candidate.content_type)}"
    if _s3 is not None:
        _s3.put_object(Bucket=BUCKET, Key=s3_key, Body=candidate.body, ContentType=candidate.content_type, Metadata={"source_url": candidate.url[:1900], "sha256": digest, "request_id": request.id})
    record = {
        "company": _slug(company.legal_name), "s3_key": s3_key, "run_id": run_id,
        "request_id": request.id, "document_type": request.document_type, "year": request.year,
        "report": validation.title, "source_url": candidate.url, "source_tier": candidate.source_tier,
        "source_metadata": candidate.metadata, "hash": digest, "content_type": candidate.content_type,
        "validation_score": validation.score, "validation_reasons": validation.reasons,
        "llm_verified": validation.llm_verified,
        "downloaded": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "rag_status": "Pending",
    }
    status = "stored"
    if _table is not None:
        try:
            _table.put_item(Item=record, ConditionExpression="attribute_not_exists(s3_key)")
        except _table.meta.client.exceptions.ConditionalCheckFailedException:
            status = "duplicate"
    download_url = None
    if _s3 is not None:
        download_url = _s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": s3_key}, ExpiresIn=PRESIGN_EXPIRY_SECONDS)
    return {"status": status, "request_id": request.id, "document_type": request.document_type, "year": request.year, "report": validation.title, "s3_key": s3_key, "s3_uri": f"s3://{BUCKET}/{s3_key}" if BUCKET else None, "download_url": download_url, "source_url": candidate.url, "source_tier": candidate.source_tier, "validation_score": validation.score, "validation_reasons": validation.reasons}


def _parse_company(payload: dict[str, Any]) -> Company:
    raw = payload.get("company")
    if not isinstance(raw, dict):
        raise ValueError("company must be an object with legal_name and official_domains")
    legal_name = str(raw.get("legal_name", "")).strip()
    domains = tuple(_normalise_domain(str(value)) for value in raw.get("official_domains", []) if str(value).strip())
    if not legal_name:
        raise ValueError("company.legal_name is required")
    if not domains and not raw.get("cik") and not raw.get("companies_house_number"):
        raise ValueError("company requires official_domains, cik, or companies_house_number")
    return Company(legal_name=legal_name, official_domains=domains, aliases=tuple(str(value) for value in raw.get("aliases", [])), ticker=raw.get("ticker"), cik=str(raw["cik"]) if raw.get("cik") else None, country=raw.get("country"), companies_house_number=raw.get("companies_house_number"), trusted_document_hosts=tuple(_normalise_domain(str(value)) for value in raw.get("trusted_document_hosts", [])))


def _parse_requests(payload: dict[str, Any]) -> list[DocumentRequest]:
    raw_requests = payload.get("requests")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise ValueError("requests must be a non-empty list; use one item per desired document")
    requests: list[DocumentRequest] = []
    for index, raw in enumerate(raw_requests, 1):
        if not isinstance(raw, dict):
            raise ValueError("every request must be an object")
        document_type = str(raw.get("document_type", "")).strip().lower()
        if document_type not in DOCUMENT_RULES:
            raise ValueError(f"unsupported document_type {document_type!r}; supported: {', '.join(DOCUMENT_RULES)}")
        year = raw.get("year")
        if year is not None and (not isinstance(year, int) or year < 1990 or year > dt.date.today().year + 1):
            raise ValueError("request.year must be a four-digit integer")
        requests.append(DocumentRequest(id=str(raw.get("id") or f"request-{index}"), document_type=document_type, year=year, allow_search=bool(raw.get("allow_search", True))))
    return requests


def _run_request(company: Company, request: DocumentRequest, diagnostics: list[str]) -> dict[str, Any]:
    tiers: list[tuple[str, list[Candidate]]] = [("registry", _registry_candidates(company, request, diagnostics))]
    tiers.append(("official_site", _official_site_candidates(company, request, diagnostics)))
    if request.allow_search:
        tiers.append(("site_scoped_search", _search_candidates(company, request)))
    for tier, candidates in tiers:
        candidate, validation = _fetch_and_select(candidates, company, request, diagnostics)
        if candidate and validation:
            return {"status": "validated", "request": asdict(request), "candidate": candidate, "validation": validation}
        diagnostics.append(f"{request.id}: no validated result from {tier}")
    return {"status": "not_found", "request": asdict(request)}


def _invoke_sync(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        company = _parse_company(payload)
        requests = _parse_requests(payload)
    except ValueError as exc:
        return {"status": "invalid_request", "error": str(exc), "contract": "company + requests[]; see scripts/payload.example.json"}
    run_id = str(payload.get("run_id") or uuid.uuid4().hex[:12])
    diagnostics: list[str] = []
    downloaded: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for request in requests:
        outcome = _run_request(company, request, diagnostics)
        if outcome["status"] == "validated":
            stored = _store(company, request, run_id, outcome["candidate"], outcome["validation"])
            downloaded.append(stored)
            results.append({"request_id": request.id, "status": stored["status"], "source_tier": stored["source_tier"], "s3_key": stored["s3_key"]})
        else:
            results.append({"request_id": request.id, "status": "not_found"})
    return {"run_id": run_id, "company": company.legal_name, "requested": len(requests), "downloaded_count": len(downloaded), "results": results, "downloaded": downloaded, "diagnostics": diagnostics, "policy": {"one_document_per_request": True, "registry_first": True, "require_llm_validation": REQUIRE_LLM_VALIDATION}}


@app.entrypoint
async def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    return _invoke_sync(payload or {})


if __name__ == "__main__":
    app.run()
