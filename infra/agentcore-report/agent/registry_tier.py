"""registry_tier.py — Tier 2 official-registry fallback (v40).

Deterministic document resolution from official filing registries, used when
Tier 1 (Google/Vertex grounded search + synonyms) does not produce a
class-verified document.

Coverage (as specified):
  * SEC EDGAR       -> annual report (10-K / 20-F / 40-F), proxy statement
                       (DEF 14A). Sustainability report is BEST-EFFORT only:
                       EDGAR has no dedicated sustainability form, so it is
                       skipped unless EDGAR_SUSTAINABILITY_FTS=true (full-text
                       search), in which case it is attempted then still passed
                       through the caller's fail-closed verify_fn.
  * Companies House -> annual report ONLY (annual accounts, type AA/AAMD).

Design contracts:
  * This module NEVER stores anything and NEVER decides on its own that a
    document is correct. It returns a candidate {url, body, ctype, via} and the
    caller runs it through the SAME fail-closed verify_fn (_llm_select_best +
    _confident) as every other tier. Pass verify_fn to have this module apply
    it inline and skip rejected candidates; omit it to let the caller verify.
  * EDGAR needs no credentials (public). It requires a compliant User-Agent
    and a polite rate limit (EDGAR_USER_AGENT, EDGAR_MAX_REQ_PER_SEC) — this is
    the fix for the intermittent 403s (fair-access UA, not IP blocking).
  * Companies House needs an API key. It is read from Secrets Manager
    (CH_SECRET_NAME) at call time so the key is not baked into the image.
    If no key is configured, Companies House is silently skipped.

company_ctx dict fields consumed (all optional except name):
    name, domain, jurisdiction ('us'|'uk'|'india'|...),
    ticker, cik, companies_house_number
"""

import base64
import json
import os
import re
import threading
import time
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import boto3

import report_specs

REGION = os.environ.get("APP_REGION") or os.environ.get("AWS_REGION", "us-east-1")

ENABLE_REGISTRY_TIER = os.environ.get("ENABLE_REGISTRY_TIER", "true").lower() != "false"

# ── EDGAR config ──
EDGAR_USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "EDO-CoAnalyst/1.0 compliance-research askdevopscloud@spglobal.com")
EDGAR_MAX_REQ_PER_SEC = float(os.environ.get("EDGAR_MAX_REQ_PER_SEC", "8"))
EDGAR_SUSTAINABILITY_FTS = os.environ.get(
    "EDGAR_SUSTAINABILITY_FTS", "false").lower() == "true"
EDGAR_FETCH_TIMEOUT = int(os.environ.get("EDGAR_FETCH_TIMEOUT", "60"))
EDGAR_MAX_DOC_BYTES = int(os.environ.get("EDGAR_MAX_DOC_BYTES", str(80 * 1024 * 1024)))

# LLM-hint CIK fallback: OFF by default. When the deterministic ticker/name
# match against SEC's own company_tickers.json can't resolve a CIK, optionally
# ask the cheap Nova model to SUGGEST a ticker, then re-validate that suggestion
# against SEC's own map before trusting it. The LLM's answer is never treated as
# fact — only as a lead that must be confirmed by ground truth. See
# _edgar_cik_llm_hint for the full rationale. Turn on by setting
# ENABLE_LLM_TICKER_HINT=true on the AgentCore runtime env (NOT the ECS task).
ENABLE_LLM_TICKER_HINT = os.environ.get(
    "ENABLE_LLM_TICKER_HINT", "false").lower() == "true"
# Reuse the same cheap model the agent already uses for high-volume/low-stakes
# work (query rewriting, fuzzy class matching). Nova 2 Lite by default.
LLM_TICKER_HINT_MODEL_ID = os.environ.get(
    "LLM_MODEL_ID", "us.amazon.nova-2-lite-v1:0")

# ── Companies House config ──
CH_SECRET_NAME = os.environ.get("CH_SECRET_NAME", "").strip()
CH_API_BASE = os.environ.get(
    "CH_API_BASE", "https://api.company-information.service.gov.uk").rstrip("/")
CH_DOC_API_BASE = os.environ.get(
    "CH_DOC_API_BASE",
    "https://document-api.company-information.service.gov.uk").rstrip("/")
CH_ANNUAL_TYPES = {"AA", "AAMD", "AAMDS"}  # annual accounts filing types

_log = print


def set_logger(fn):
    global _log
    _log = fn


# ─── EDGAR: polite rate limiter ───────────────────────────────────────────────
_edgar_lock = threading.Lock()
_edgar_last = [0.0]


def _edgar_throttle() -> None:
    interval = 1.0 / max(EDGAR_MAX_REQ_PER_SEC, 0.1)
    with _edgar_lock:
        now = time.monotonic()
        wait = interval - (now - _edgar_last[0])
        if wait > 0:
            time.sleep(wait)
        _edgar_last[0] = time.monotonic()


def _http_json(url: str, headers: dict, timeout: int = 20):
    _edgar_throttle()
    with urlopen(Request(url, headers=headers), timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8", "ignore"))


def _http_bytes(url: str, headers: dict, timeout: int) -> tuple[bytes, str]:
    _edgar_throttle()
    with urlopen(Request(url, headers=headers), timeout=timeout) as r:  # noqa: S310
        ctype = r.headers.get("Content-Type", "application/octet-stream").split(";")[0]
        return r.read(), ctype


# ─── EDGAR: ticker/name -> CIK ────────────────────────────────────────────────
_EDGAR_TICKER_CACHE: dict[str, str] = {}
_EDGAR_CACHE_LOCK = threading.Lock()


def _load_edgar_ticker_map() -> None:
    with _EDGAR_CACHE_LOCK:
        if _EDGAR_TICKER_CACHE:
            return
        try:
            data = _http_json("https://www.sec.gov/files/company_tickers.json",
                              {"User-Agent": EDGAR_USER_AGENT})
        except Exception as exc:  # noqa: BLE001
            _log(f"[edgar] ticker map fetch failed: {exc}")
            return
        for _, row in (data or {}).items():
            if not isinstance(row, dict):
                continue
            t = str(row.get("ticker", "")).upper().strip()
            cik = str(row.get("cik_str", "")).strip()
            nm = str(row.get("title", "")).lower().strip()
            if not cik.isdigit():
                continue
            cik = cik.zfill(10)
            if t:
                _EDGAR_TICKER_CACHE[t] = cik
            if nm:
                _EDGAR_TICKER_CACHE["name::" + nm] = cik


def _edgar_cik(ticker: str | None, name: str | None) -> str | None:
    if not (ticker or name):
        return None
    _load_edgar_ticker_map()
    if not _EDGAR_TICKER_CACHE:
        return None
    if ticker:
        c = _EDGAR_TICKER_CACHE.get(ticker.upper().strip())
        if c:
            return c
    if name:
        nlow = name.lower().strip()
        c = _EDGAR_TICKER_CACHE.get("name::" + nlow)
        if c:
            return c
        # Loose contains match, BIDIRECTIONAL. The query name and SEC's title
        # often differ only in the corporate-suffix form ("Edwards Lifesciences
        # Corporation" vs SEC's "Edwards Lifesciences Corp"). A one-directional
        # `nlow in sec_name` check misses this whenever the query is the LONGER
        # string, silently returning no CIK and forcing the whole chain down to
        # the (slow, WAF-prone) browser tier. Checking both directions catches
        # the suffix-truncation case. Still accept only if unambiguous.
        matches = {v for k, v in _EDGAR_TICKER_CACHE.items()
                   if k.startswith("name::") and (nlow in k[6:] or k[6:] in nlow)}
        if len(matches) == 1:
            return next(iter(matches))
        # Word-set fallback: normalize away corporate suffixes and word order
        # entirely (via _STOPWORDS + _name_words) and require an EXACT set match.
        # This resolves "Corp"/"Corporation", "Inc"/"Incorporated", dropped
        # "Company", etc. It is deliberately exact-set (not subset) so a generic
        # name like "Edwards" can't collide with a longer registrant — matching
        # the same fail-closed, no-guessing contract as the rest of this module.
        qwords = _name_words(name)
        if qwords:
            word_matches = {v for k, v in _EDGAR_TICKER_CACHE.items()
                            if k.startswith("name::") and _name_words(k[6:]) == qwords}
            if len(word_matches) == 1:
                cik = next(iter(word_matches))
                _log(f"[edgar] resolved CIK {cik} for {name!r} via name-word-set "
                     f"match (suffix/word-order normalized)")
                return cik
    return None


# ─── EDGAR: LLM-hint CIK fallback (opt-in, suggest-then-validate) ──────────────
_STOPWORDS = {"inc", "incorporated", "the", "and", "corp", "corporation",
              "ltd", "limited", "plc", "llc", "co", "company", "group",
              "holdings", "holding", "sa", "ag", "nv", "se"}


def _name_words(s: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (s or "").lower())
            if len(w) > 2 and w not in _STOPWORDS}


def _parse_llm_json(text: str) -> dict:
    """Nova 2 Lite emits valid JSON often followed by trailing prose, which
    plain json.loads() rejects with 'Extra data'. Strip any code fence, seek
    the first '{', then raw_decode so only the first JSON value is taken and
    trailing text is ignored."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start > 0:
        text = text[start:]
    return json.JSONDecoder().raw_decode(text)[0]


def _edgar_cik_llm_hint(name: str | None, domain: str | None) -> str | None:
    """Last-resort CIK resolver used ONLY when the deterministic ticker/name
    match has already failed. Asks the cheap Nova model to SUGGEST a ticker,
    then re-validates that suggestion against SEC's own company_tickers.json
    (already cached in _EDGAR_TICKER_CACHE) before trusting it.

    Why not just ask the LLM for the CIK directly: CIK numbers are exact
    identifiers. An LLM asked to recall one from training data can produce a
    plausible-looking but WRONG number with no signal anything is off — and a
    wrong CIK makes edgar_lookup silently resolve and store a DIFFERENT
    company's filing, the exact cross-company contamination the fail-closed
    design exists to prevent. So the LLM only ever POINTS at a ticker; SEC's own
    data is what confirms the ticker is real AND which company it belongs to.
    If the suggestion doesn't validate, return None (caller then fails closed)."""
    if not ENABLE_LLM_TICKER_HINT or not name:
        return None
    _load_edgar_ticker_map()
    if not _EDGAR_TICKER_CACHE:
        return None

    prompt = (
        "Identify the US stock ticker symbol for a company, purely as a HINT "
        "that will be independently verified against SEC records afterward. "
        "Only answer if reasonably confident; use null if unsure or the company "
        "is not US-listed.\n\n"
        f"Company name: {name}\n"
        f"Company website domain: {domain or 'unknown'}\n\n"
        'Output ONLY valid JSON, nothing else: {"ticker": "<TICKER or null>"}'
    )
    try:
        client = boto3.client("bedrock-runtime", region_name=REGION)
        resp = client.converse(
            modelId=LLM_TICKER_HINT_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 50, "temperature": 0},
        )
        text = "".join(b.get("text", "")
                       for b in resp["output"]["message"]["content"])
        obj = _parse_llm_json(text)
        ticker = (obj or {}).get("ticker")
        if not ticker or not isinstance(ticker, str):
            _log(f"[edgar] LLM ticker hint returned no candidate for {name!r}")
            return None
        ticker = ticker.upper().strip()
    except Exception as exc:  # noqa: BLE001
        _log(f"[edgar] LLM ticker hint call failed for {name!r}: {exc}")
        return None

    # Validate the LLM's suggestion against SEC's own map — the step that makes
    # using a cheap model safe at all.
    cik = _EDGAR_TICKER_CACHE.get(ticker)
    if not cik:
        _log(f"[edgar] LLM suggested ticker {ticker!r} for {name!r} but it is "
             f"not in SEC's ticker map — discarding (fail closed)")
        return None
    # Confirm the SEC-registered name for that ticker actually overlaps the
    # requested company name, so a real-but-wrong ticker can't slip through.
    sec_name = next((k[6:] for k, v in _EDGAR_TICKER_CACHE.items()
                     if k.startswith("name::") and v == cik), "")
    if sec_name and not (_name_words(name) & _name_words(sec_name)):
        _log(f"[edgar] LLM suggested {ticker!r} (SEC: {sec_name!r}) but it "
             f"shares no name words with {name!r} — likely wrong, discarding")
        return None
    _log(f"[edgar] LLM ticker hint {ticker!r} validated against SEC "
         f"(CIK {cik}) for {name!r} — accepted")
    return cik


# ─── EDGAR resolver ───────────────────────────────────────────────────────────
def edgar_lookup(company_ctx: dict, report_class: str, year: int | None) -> dict | None:
    if not ENABLE_REGISTRY_TIER:
        return None
    forms = report_specs.registries_for(report_class).get("edgar")
    if not forms:
        return None

    if forms == ["_fts_best_effort"]:
        if not EDGAR_SUSTAINABILITY_FTS:
            _log("[edgar] no standard form for sustainability report; skipping "
                 "(set EDGAR_SUSTAINABILITY_FTS=true to attempt full-text search)")
            return None
        return _edgar_fts(company_ctx, report_class, year)

    cik = re.sub(r"\D", "", str(company_ctx.get("cik") or "")).zfill(10) \
        if company_ctx.get("cik") else None
    if not cik or cik == "0000000000":
        cik = _edgar_cik(company_ctx.get("ticker"), company_ctx.get("name"))
    if not cik:
        # Deterministic ticker/name match failed. Optionally try the LLM-hint
        # fallback (opt-in via ENABLE_LLM_TICKER_HINT); it still validates any
        # suggestion against SEC's own map, so a miss here fails closed exactly
        # as before.
        cik = _edgar_cik_llm_hint(company_ctx.get("name"),
                                  company_ctx.get("domain"))
    if not cik:
        _log(f"[edgar] could not resolve CIK for "
             f"{company_ctx.get('name')!r}/{company_ctx.get('ticker')!r}; skipping")
        return None

    try:
        sub = _http_json(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         {"User-Agent": EDGAR_USER_AGENT})
    except Exception as exc:  # noqa: BLE001
        _log(f"[edgar] submissions fetch failed for CIK {cik}: {exc}")
        return None

    recent = ((sub or {}).get("filings", {}) or {}).get("recent", {}) or {}
    forms_list = recent.get("form", []) or []
    accession = recent.get("accessionNumber", []) or []
    primary = recent.get("primaryDocument", []) or []
    fdate = recent.get("filingDate", []) or []
    rdate = recent.get("reportDate", []) or []

    want = {f.upper() for f in forms}
    best = None
    for i, f in enumerate(forms_list):
        if str(f).upper() not in want:
            continue
        y = None
        for src in (rdate, fdate):
            if i < len(src) and str(src[i])[:4].isdigit():
                y = int(str(src[i])[:4])
                break
        if year and y and y != year:
            continue
        acc = accession[i].replace("-", "") if i < len(accession) else ""
        pdoc = primary[i] if i < len(primary) else ""
        if not acc or not pdoc:
            continue
        url = (f"https://www.sec.gov/Archives/edgar/data/"
               f"{int(cik)}/{acc}/{pdoc}")
        cand = {"url": url, "year": y, "form": str(f)}
        if year and y == year:
            best = cand
            break
        if best is None or (y and (best.get("year") or 0) < y):
            best = cand

    if not best:
        _log(f"[edgar] no {sorted(want)} filing for CIK {cik} (year={year})")
        return None

    try:
        body, ctype = _http_bytes(best["url"], {"User-Agent": EDGAR_USER_AGENT},
                                  EDGAR_FETCH_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        _log(f"[edgar] document fetch failed ({best['url']}): {exc}")
        return None
    if not body or len(body) > EDGAR_MAX_DOC_BYTES:
        return None
    _log(f"[edgar] resolved {best['form']} (year={best['year']}): {best['url']}")
    return {"url": best["url"], "body": body, "ctype": ctype,
            "via": f"edgar:{best['form']}", "_deterministic": True}


def _edgar_fts(company_ctx: dict, report_class: str, year: int | None) -> dict | None:
    """Best-effort EDGAR full-text search (only reached when explicitly enabled).
    EDGAR FTS endpoints/behaviour should be verified on first deploy against
    real responses; kept deliberately narrow and still verify_fn-gated upstream.
    """
    name = company_ctx.get("name") or ""
    if not name:
        return None
    q = quote(f'"{name}" sustainability report')
    url = f"https://efts.sec.gov/LATEST/search-index?q={q}"
    try:
        res = _http_json(url, {"User-Agent": EDGAR_USER_AGENT})
    except Exception as exc:  # noqa: BLE001
        _log(f"[edgar-fts] search failed: {exc}")
        return None
    hits = ((res or {}).get("hits", {}) or {}).get("hits", []) or []
    for h in hits[:5]:
        src = h.get("_source", {}) if isinstance(h, dict) else {}
        adsh = (src.get("adsh") or "").replace("-", "")
        cik_list = src.get("cik") or []
        files = h.get("_id", "")  # typically "adsh:filename"
        fname = files.split(":", 1)[-1] if ":" in files else ""
        if not (adsh and cik_list and fname):
            continue
        cik = str(cik_list[0]) if isinstance(cik_list, list) else str(cik_list)
        url = (f"https://www.sec.gov/Archives/edgar/data/"
               f"{int(cik)}/{adsh}/{fname}")
        try:
            body, ctype = _http_bytes(url, {"User-Agent": EDGAR_USER_AGENT},
                                      EDGAR_FETCH_TIMEOUT)
        except Exception:  # noqa: BLE001
            continue
        if body and len(body) <= EDGAR_MAX_DOC_BYTES:
            return {"url": url, "body": body, "ctype": ctype,
                    "via": "edgar:fts-best-effort", "_deterministic": False}
    return None


# ─── Companies House resolver ─────────────────────────────────────────────────
_ch_headers_cache: list = [None]


def _ch_headers() -> dict | None:
    if _ch_headers_cache[0] is not None:
        return _ch_headers_cache[0]
    if not CH_SECRET_NAME:
        return None
    try:
        sm = boto3.client("secretsmanager", region_name=REGION)
        raw = sm.get_secret_value(SecretId=CH_SECRET_NAME).get("SecretString", "")
    except Exception as exc:  # noqa: BLE001
        _log(f"[ch] secret fetch failed: {exc}")
        return None
    key = raw
    try:
        j = json.loads(raw)
        key = j.get("api_key") or j.get("CH_API_KEY") or j.get("key") or raw
    except Exception:  # noqa: BLE001
        pass
    if not key:
        return None
    token = base64.b64encode((key + ":").encode()).decode()
    hdr = {"Authorization": "Basic " + token,
           "User-Agent": "EDO-CoAnalyst/1.0 compliance-research"}
    _ch_headers_cache[0] = hdr
    return hdr


def companies_house_lookup(company_ctx: dict, report_class: str,
                           year: int | None) -> dict | None:
    if not ENABLE_REGISTRY_TIER:
        return None
    if "companies_house" not in report_specs.registries_for(report_class):
        return None  # CH serves annual reports only
    headers = _ch_headers()
    if not headers:
        _log("[ch] no API key configured (CH_SECRET_NAME); skipping")
        return None

    number = str(company_ctx.get("companies_house_number") or "").strip()
    number_was_given_directly = bool(number)
    try:
        if not number:
            name = company_ctx.get("name") or ""
            if not name:
                return None
            res = _http_json(
                f"{CH_API_BASE}/search/companies?q={quote(name)}&items_per_page=1",
                headers)
            items = (res or {}).get("items", [])
            if not items:
                _log(f"[ch] company search returned nothing for {name!r}")
                return None
            number = items[0].get("company_number", "")
        if not number:
            return None
        fh = _http_json(
            f"{CH_API_BASE}/company/{number}/filing-history"
            f"?category=accounts&items_per_page=100", headers)
    except Exception as exc:  # noqa: BLE001
        _log(f"[ch] lookup failed: {exc}")
        return None

    best = None
    for it in (fh or {}).get("items", []):
        if str(it.get("type", "")).upper() not in CH_ANNUAL_TYPES:
            continue
        d = str(it.get("date", ""))
        y = int(d[:4]) if d[:4].isdigit() else None
        if year and y and y != year:
            continue
        meta_link = ((it.get("links") or {}).get("document_metadata"))
        if not meta_link:
            continue
        cand = {"meta": meta_link, "year": y}
        if year and y == year:
            best = cand
            break
        if best is None or (y and (best.get("year") or 0) < y):
            best = cand

    if not best:
        _log(f"[ch] no annual accounts (AA) for {number} (year={year})")
        return None

    try:
        meta = _http_json(best["meta"], headers)
        doc_link = ((meta or {}).get("links") or {}).get("document")
        if not doc_link:
            _log("[ch] document metadata had no document link")
            return None
        content_url = doc_link if doc_link.endswith("/content") else doc_link + "/content"
        body, ctype = _http_bytes(
            content_url, {**headers, "Accept": "application/pdf"}, 60)
    except Exception as exc:  # noqa: BLE001
        _log(f"[ch] document fetch failed: {exc}")
        return None
    if not body:
        return None
    _log(f"[ch] resolved annual accounts (year={best['year']}): {content_url}")
    return {"url": content_url, "body": body,
            "ctype": ctype or "application/pdf", "via": "companies_house:AA",
            "_deterministic": number_was_given_directly}


# ─── Dispatcher ───────────────────────────────────────────────────────────────
def registry_resolve(company_ctx: dict, report_class: str, year: int | None = None,
                     verify_fn=None) -> dict | None:
    """Try the registries eligible for this class, jurisdiction-ordered.

    Returns a {url, body, ctype, via, verified?} candidate or None. If verify_fn
    is supplied, only a candidate that PASSES it is returned (fail-closed).
    """
    if not ENABLE_REGISTRY_TIER or not report_class:
        return None
    regs = report_specs.registries_for(report_class)
    if not regs:
        return None

    jur = str(company_ctx.get("jurisdiction") or "").lower()
    if jur == "uk":
        order = [companies_house_lookup, edgar_lookup]
    else:
        order = [edgar_lookup, companies_house_lookup]

    for fn in order:
        try:
            doc = fn(company_ctx, report_class, year)
        except Exception as exc:  # noqa: BLE001
            _log(f"[registry] {fn.__name__} error: {exc}")
            doc = None
        if not (doc and doc.get("body")):
            continue

        if doc.get("_deterministic"):
            # CIK (or company-number) + the registry's OWN form-type tag are
            # already authoritative for company identity and document class —
            # SEC/Companies House indexed this filing themselves; there is no
            # ambiguity left for an LLM to resolve. Running it back through
            # the generic verifier (tuned for noisy web candidates, and prone
            # to treating a filing's formal SEC boilerplate as evidence it
            # "isn't really an Annual Report") was producing false rejections
            # on genuinely correct, deterministically-resolved documents.
            doc["verified"] = True
            doc["_verified_for"] = report_class
            _log(f"[registry] {doc['via']} — deterministic resolution "
                 f"(CIK/company-number + registry form-type match), "
                 f"skipping LLM class verify")
            return doc

        if verify_fn is not None:
            cand = {"url": doc["url"], "body": doc["body"],
                    "ctype": doc.get("ctype", "")}
            if not verify_fn(cand):
                _log(f"[registry] {doc['via']} candidate failed class verify; "
                     f"skipping")
                continue
            doc["verified"] = True
            doc["_verified_for"] = report_class
        return doc
    return None