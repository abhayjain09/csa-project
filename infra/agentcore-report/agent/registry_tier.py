"""registry_tier.py — official-registry resolver (v43).

Deterministic document resolution from official filing registries. Validated
annual-report and proxy identities may use this before web search; other
eligible classes use it as a fallback.

Coverage (as specified):
  * SEC EDGAR       -> annual report (10-K / 20-F / 40-F), proxy statement
                       (DEF 14A). Sustainability report is BEST-EFFORT only:
                       EDGAR has no dedicated sustainability form, so it is
                       skipped unless EDGAR_SUSTAINABILITY_FTS=true (full-text
                       search), in which case it is attempted then still passed
                       through the caller's fail-closed verify_fn.
  * Companies House -> annual report ONLY (annual accounts, type AA/AAMD).

Design contracts:
  * This module NEVER stores anything. Deterministic CIK/company-number plus
    registry form-type matches are accepted without a generic LLM verifier;
    non-deterministic registry candidates still use the caller's fail-closed
    verifier.
  * Vertex/LLM ticker and CIK values are hints only. Available identifiers and
    the requested legal name must converge on one SEC company_tickers record.
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


# ─── EDGAR: validated ticker/name -> CIK ─────────────────────────────────────
_EDGAR_TICKER_CACHE: dict[str, str] = {}
_EDGAR_CACHE_LOCK = threading.Lock()
_STOPWORDS = {"inc", "incorporated", "the", "and", "corp", "corporation",
              "ltd", "limited", "plc", "llc", "co", "company", "group",
              "holdings", "holding", "sa", "ag", "nv", "se"}


def _name_words(s: str) -> frozenset[str]:
    """Normalize only corporate suffixes; never use subset/contains matching."""
    return frozenset(
        w for w in re.split(r"[^a-z0-9]+", (s or "").lower())
        if len(w) > 2 and w not in _STOPWORDS
    )


def _normalise_cik(value) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits or len(digits) > 10:
        return None
    return digits.zfill(10)


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


def _sec_names_for_cik(cik: str) -> list[str]:
    return sorted({
        key[6:] for key, value in _EDGAR_TICKER_CACHE.items()
        if key.startswith("name::") and value == cik
    })


def _sec_tickers_for_cik(cik: str) -> list[str]:
    return sorted({
        key for key, value in _EDGAR_TICKER_CACHE.items()
        if not key.startswith("name::") and value == cik
    })


def _ciks_for_exact_name(name: str | None) -> set[str]:
    """Return SEC CIKs whose registered name is an exact normalized match.

    Exact raw names win. Otherwise corporate suffix spelling and punctuation
    are normalized, but subset/substring matches are intentionally forbidden:
    "Edwards" must not resolve "Edwards Lifesciences Corporation".
    """
    value = (name or "").strip().lower()
    if not value:
        return set()
    exact = _EDGAR_TICKER_CACHE.get("name::" + value)
    if exact:
        return {exact}
    words = _name_words(value)
    if not words:
        return set()
    return {
        cik for key, cik in _EDGAR_TICKER_CACHE.items()
        if key.startswith("name::") and _name_words(key[6:]) == words
    }


def enrich_company_identity(company_ctx: dict, identity_hint: dict | None = None) -> dict:
    """Validate and enrich company identity against SEC's official ticker map.

    Vertex/Gemini values are hints only. A CIK is accepted only when all
    available identifiers converge on one SEC record and the supplied company
    name (or grounded legal-name hint) exactly matches that SEC registrant after
    conservative corporate-suffix normalization.
    """
    ctx = dict(company_ctx or {})
    hint = dict(identity_hint or {})
    _load_edgar_ticker_map()
    validation = {
        "status": "unresolved",
        "method": None,
        "reason": "SEC ticker map unavailable",
        "hint_used": bool(hint),
    }
    ctx["_identity_validation"] = validation
    if not _EDGAR_TICKER_CACHE:
        return ctx

    requested_name = str(ctx.get("name") or "").strip()
    requested_name_ciks = _ciks_for_exact_name(requested_name)
    hint_name = str(hint.get("legal_name") or "").strip()
    hint_name_ciks = _ciks_for_exact_name(hint_name)

    identifier_signals: list[tuple[str, str]] = []
    for label, ticker in (
        ("payload_ticker", ctx.get("ticker")),
        ("vertex_ticker", hint.get("ticker")),
    ):
        value = str(ticker or "").upper().strip()
        if value:
            cik = _EDGAR_TICKER_CACHE.get(value)
            if cik:
                identifier_signals.append((label, cik))
    known_ciks = set(_EDGAR_TICKER_CACHE.values())
    for label, raw_cik in (
        ("payload_cik", ctx.get("cik")),
        ("vertex_cik", hint.get("cik")),
    ):
        cik = _normalise_cik(raw_cik)
        if cik and cik in known_ciks:
            identifier_signals.append((label, cik))

    identifier_ciks = {cik for _, cik in identifier_signals}
    if len(identifier_ciks) > 1:
        validation["reason"] = "ticker/CIK signals resolve to different SEC registrants"
        _log(f"[identity] rejected conflicting identifiers for {requested_name!r}: "
             f"{identifier_signals}")
        return ctx

    candidate = next(iter(identifier_ciks), None)
    if candidate is not None:
        # A real identifier is still not sufficient when it belongs to a
        # different company. Require the requested name or the grounded legal
        # name to match the same SEC registrant exactly after suffix cleanup.
        if requested_name and requested_name.lower() != "unknown":
            name_supports_candidate = candidate in requested_name_ciks
        else:
            name_supports_candidate = candidate in hint_name_ciks
        if not name_supports_candidate:
            validation["reason"] = "identifier is real but company name does not match SEC"
            _log(f"[identity] rejected real-but-mismatched identifier CIK "
                 f"{candidate} for {requested_name!r}")
            return ctx
    else:
        # No ticker/CIK was supplied. A unique exact normalized SEC-name match
        # is deterministic and safe enough to enrich with the official values.
        name_candidates = (
            requested_name_ciks
            if requested_name and requested_name.lower() != "unknown"
            else hint_name_ciks
        )
        if len(name_candidates) != 1:
            validation["reason"] = (
                "company name is not a unique exact normalized SEC match")
            return ctx
        candidate = next(iter(name_candidates))

    sec_names = _sec_names_for_cik(candidate)
    sec_tickers = _sec_tickers_for_cik(candidate)
    ctx["cik"] = candidate
    if sec_tickers:
        # Prefer a supplied ticker when it resolves to this CIK; otherwise use
        # the shortest SEC-listed ticker (usually the primary common stock).
        supplied = str(ctx.get("ticker") or hint.get("ticker") or "").upper().strip()
        ctx["ticker"] = supplied if supplied in sec_tickers else min(
            sec_tickers, key=lambda value: (len(value), value))
    if sec_names:
        ctx["official_name"] = sec_names[0]
    if not ctx.get("domain") and hint.get("_domain_attested"):
        ctx["domain"] = str(hint.get("official_domain") or "").strip().lower()
    if not ctx.get("jurisdiction"):
        ctx["jurisdiction"] = "us"
    validation.update({
        "status": "validated",
        "method": "+".join(label for label, _ in identifier_signals)
                  or "sec_exact_normalized_name",
        "reason": "all available signals converge on one SEC registrant",
        "cik": candidate,
        "ticker": ctx.get("ticker") or "",
        "official_name": ctx.get("official_name") or "",
    })
    _log(f"[identity] validated {requested_name!r} -> CIK {candidate}, "
         f"ticker={ctx.get('ticker')!r} via {validation['method']}")
    return ctx


def _edgar_cik(ticker: str | None, name: str | None) -> str | None:
    if not (ticker or name):
        return None
    _load_edgar_ticker_map()
    if not _EDGAR_TICKER_CACHE:
        return None
    resolved = enrich_company_identity(
        {"name": name or "", "ticker": ticker or "", "cik": ""})
    if ((resolved.get("_identity_validation") or {}).get("status")
            == "validated"):
        return resolved.get("cik")
    return None


# ─── EDGAR: LLM-hint CIK fallback (opt-in, suggest-then-validate) ──────────────


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
    # Confirm the SEC-registered name for that ticker is an exact normalized
    # match for the requested company. Token overlap is not sufficient: it was
    # the path by which similarly named companies contaminated one another.
    sec_name = next((k[6:] for k, v in _EDGAR_TICKER_CACHE.items()
                     if k.startswith("name::") and v == cik), "")
    validated = enrich_company_identity(
        {"name": name, "domain": domain or "", "ticker": "", "cik": ""},
        {"ticker": ticker, "cik": cik, "legal_name": sec_name})
    if ((validated.get("_identity_validation") or {}).get("status")
            != "validated"):
        _log(f"[edgar] LLM suggested {ticker!r} (SEC: {sec_name!r}) but it "
             f"does not exactly match {name!r} after suffix normalization — "
             f"discarding")
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

    validated_ctx = enrich_company_identity(company_ctx)
    identity_status = (validated_ctx.get("_identity_validation") or {}).get("status")
    cik = validated_ctx.get("cik") if identity_status == "validated" else None
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
