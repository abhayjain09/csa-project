import re

PATH = "agent/agent.py"
src = open(PATH).read()

if "def _llm_generate_search_queries" in src:
    print("v39 already applied (found _llm_generate_search_queries). Aborting to avoid double-apply.")
    raise SystemExit(0)

# ── char helpers so no quote/backslash appears literally in regex ──
BS = chr(92); DQ = chr(34); SQ = chr(39)

# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 1: new env constants — inserted right after the SELECTION_MODEL_ID line
# ═══════════════════════════════════════════════════════════════════════════
ENV_BLOCK = '''
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
    if k.strip()]
_LLM_QUERY_GEN_CACHE = {}
'''

anchor = 'SELECTION_MODEL_ID = os.environ.get("SELECTION_MODEL_ID", "").strip() or None'
if anchor not in src:
    print("Could not find SELECTION_MODEL_ID anchor. Aborting.")
    raise SystemExit(1)
src = src.replace(anchor, anchor + "\n" + ENV_BLOCK, 1)

# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 2: relax budgets (Phase 5) — replace the two budget constant lines
# ═══════════════════════════════════════════════════════════════════════════
src = src.replace(
    'QUERY_MAX_VERIFIES = int(os.environ.get("QUERY_MAX_VERIFIES", "25"))',
    'QUERY_MAX_VERIFIES = int(os.environ.get("QUERY_MAX_VERIFIES", "100"))')
src = src.replace(
    'QUERY_MAX_SECONDS = float(os.environ.get("QUERY_MAX_SECONDS", "90"))',
    'QUERY_MAX_SECONDS = float(os.environ.get("QUERY_MAX_SECONDS", "900"))')
src = src.replace(
    'BROWSER_RESOLVE_MAX_SECONDS = float(os.environ.get("BROWSER_RESOLVE_MAX_SECONDS", "600"))',
    'BROWSER_RESOLVE_MAX_SECONDS = float(os.environ.get("BROWSER_RESOLVE_MAX_SECONDS", "1800"))')
src = src.replace(
    'BROWSER_NAV_MAX_PAGES = int(os.environ.get("BROWSER_NAV_MAX_PAGES", "25"))',
    'BROWSER_NAV_MAX_PAGES = int(os.environ.get("BROWSER_NAV_MAX_PAGES", "60"))')
src = src.replace(
    'DEEP_STATIC_MAX_PAGES = int(os.environ.get("DEEP_STATIC_MAX_PAGES", "40"))',
    'DEEP_STATIC_MAX_PAGES = int(os.environ.get("DEEP_STATIC_MAX_PAGES", "100"))')

# ═══════════════════════════════════════════════════════════════════════════
# BLOCK 3: new functions — appended just before the "# ─── Entrypoint" marker
# All regex built with chr() so paste can't corrupt this file's OWN source.
# ═══════════════════════════════════════════════════════════════════════════
# Build regex literals for the new functions using char codes.
# ([^<]+)  ->
LOC_RE = ("([^<]+)" + "")   # no backslash/quote issues; plain chars
# sitemap: * (\S+)  ->  robots.txt Sitemap line
SITEMAP_LINE_RE = ("sitemap:" + BS + "s*(" + BS + "S+)")

NEW_FUNCS = '''
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
        "Today is " + dt.date.today().isoformat() + ".\\n"
        "You are a search-query optimizer for finding OFFICIAL company documents "
        "(annual reports, sustainability/ESG reports, governance policies, filings). "
        "Generate up to " + str(LLM_QUERY_GEN_MAX) + " DISTINCT web-search query "
        "strings that maximize the chance of finding the exact document below.\\n\\n"
        "Company: " + str(company) + "\\n"
        "Company website domain: " + str(domain or "unknown") + "\\n"
        "Original query: " + query + "\\n"
        + (("Matched document-class rules: " + json.dumps(filtered_rules, ensure_ascii=True) + "\\n") if filtered_rules else "")
        + "\\nCreate a MIX of these query shapes (only where sensible):\\n"
        "1. site:DOMAIN scoped query with the exact document class.\\n"
        "2. An UNSCOPED query: COMPANY DOCUMENTCLASS YEAR filetype:pdf.\\n"
        "3. An IR/investor-subdomain hint query (investors.DOMAIN, "
        "sustainability.DOMAIN, static.DOMAIN) where the file likely lives.\\n"
        "4. A filing-registry-scoped query using ONE of these hosts if relevant "
        "to the company jurisdiction: " + registries + ". Example: site:REGISTRY COMPANY CLASS.\\n"
        "5. Regional/naming variants of the class (annual report and accounts, "
        "integrated annual report, BRSR, 10-K, DEF 14A as appropriate).\\n\\n"
        "Rules: preserve any year exactly; never invent a year. Keep each query "
        "under 200 chars. No markdown. Do NOT format domains as markdown links.\\n\\n"
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
_LOC_RE = "''' + LOC_RE + '''"
_SITEMAP_LINE_RE = "''' + SITEMAP_LINE_RE + '''"


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

'''

entry_anchor = "# ─── Entrypoint"
if entry_anchor not in src:
    print("Could not find Entrypoint anchor. Aborting.")
    raise SystemExit(1)
src = src.replace(entry_anchor, NEW_FUNCS + "\n" + entry_anchor, 1)

open(PATH, "w").write(src)
print("v39 BLOCKS 1-3 applied: env constants, budget relaxation, new functions.")
print("NOTE: BLOCK 4 (wire into _find_best_document + _invoke_sync) is applied by apply_v39_wire.py next.")