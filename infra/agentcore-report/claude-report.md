# Target Architecture for Report IQ / EDO Co-Analyst: A Registry-First, Tiered Document Retrieval Pipeline on AWS

## TL;DR
- **Stop treating this as a search problem and re-architect it as a registry-first, tiered discovery pipeline.** For US, UK, and EU listed companies, official filing registries (SEC EDGAR, UK Companies House, ESEF/OAM repositories) deterministically cover annual reports, proxy statements, and increasingly BRSR/ESG filings — replacing fragile open-web search for the majority of high-value document classes. Reserve web search and headless browsing only for the genuinely un-registered classes (codes of conduct, whistleblowing/anti-bribery/insider-trading policies, tax strategy, supplier codes) that live only on company websites.
- **Your SEC 403 is almost certainly a User-Agent/fair-access problem, not AWS-IP blocking** — a compliant `User-Agent` header (org name + contact email) plus ≤10 req/s makes EDGAR's JSON/Archives endpoints work from AWS. The NSE/BSE anti-bot problem is real but solvable with a cookie-priming session pattern; it does NOT require third-party proxies for most cases.
- **Replace AgentCore Gateway WebSearch with a two-part backend**: Amazon Nova Web Grounding / AgentCore Web Search for cited discovery, backstopped by a Google Programmable Search Engine or Brave Search API (Brave is on AWS Marketplace, billable through AWS) called from a Lambda action group when you need true `site:` scoping. Keep your fail-closed LLM verification layer exactly as-is. Move crawling from the monolith to purpose-built Lambda (headless Chromium) + Fargate (long jobs) workers behind a Step Functions orchestrator.

## Key Findings

### 1. The single biggest architectural error is search-first discovery
Your current design leans on open-web search to find documents that are, for most of your coverage universe, already sitting in structured, free, official registries. The published literature on ESG/sustainability-report collection at scale confirms two camps: academic/statistical crawlers (e.g., the UK ONS "Measuring Sustainability Reporting" Scrapy project; a 2026 Nature Scientific Reports NLP pipeline that scraped 220 factory sites with BeautifulSoup and got usable data from only 82) rely on recursive keyword-flagged crawling, while commercial ESG data operations lean on regulatory filing feeds plus targeted crawling. The commercial-vendor pattern (registries + targeted crawl + verification) is the one you should emulate, not the open-web-search pattern.

### 2. SEC EDGAR: the 403 is fixable without leaving AWS
The evidence is strong that EDGAR's 403s are driven by fair-access policy — a missing/generic User-Agent and/or exceeding 10 req/s — not by blanket blocking of AWS datacenter IP ranges. SEC's official "Accessing EDGAR Data" page states the max request rate is 10 requests/second "regardless of the number of machines used to submit requests," and requires you to "declare your user agent in request headers" in the format `Sample Company Name AdminContact@<domain>.com`. Multiple independent developers (GitHub issue jadchaar/sec-edgar-downloader #77) report that switching from a generic UA to `Company Name email@company.com` fixed persistent 403s, including in Lambda-style deployments — one commenter noting "I provided a real company name and my actual company email, it worked like a charm!" There is no official SEC documentation or credible first-hand report of the SEC banning AWS CIDR ranges for its APIs.

Key nuance: `efts.sec.gov` (full-text search) is itself an AWS API Gateway fronting an OpenSearch cluster — no Akamai, no captcha — and returns 200 reliably from cloud IPs. The `www.sec.gov` HTML site sits behind Akamai (where datacenter IPs do carry negative trust), but the JSON APIs and `/Archives/` document paths you actually need are not gated the same way.

**Actionable EDGAR endpoints (all free, no key):**
- Full-text search: `https://efts.sec.gov/LATEST/search-index?q=...&forms=...&dateRange=custom&startdt=...&enddt=...` (returns JSON: accession numbers, CIK, form type, filing URLs; coverage from 2001)
- Submissions history per company: `https://data.sec.gov/submissions/CIK##########.json` (10-digit zero-padded CIK)
- Bulk archives (recompiled nightly ~3am ET) to avoid per-document crawling: `submissions.zip` and `companyfacts.zip` under the EDGAR bulk-data paths; the SEC's own APIs page states "The most efficient means to fetch large amounts of API data is the bulk archive ZIP files, which are recompiled nightly."
- Quarterly full index: `https://www.sec.gov/Archives/edgar/full-index/YYYY/QTRn/master.gz`
- Ticker→CIK map: `https://www.sec.gov/files/company_tickers.json`

This covers 10-K (annual reports), DEF 14A (proxy statements), 20-F/40-F (foreign filers), and 8-K exhibits deterministically for US issuers. Best practice: ~8 req/s with ~125ms sleep, conditional GETs with ETag.

### 3. India (NSE/BSE): anti-bot is real but solvable; BRSR is available at the exchange
NSE (`nseindia.com`, `nsearchives.nseindia.com`) aggressively 403s naive requests and blocks cloud-provider IPs (PythonAnywhere staff confirmed "the National Stock Exchange of India block[s] incoming connections from cloud service providers"). The reliable workaround is a **cookie-priming session pattern**: issue a first GET to a human-facing NSE page (e.g., `/report-detail/...`) with a full browser-like header set (`User-Agent`, `Accept`, `Accept-Language`, `Referer`) to obtain session cookies, then reuse that `requests.Session()` (with cookies) for the API/archive call. Community libraries (BennyThadikaran's `NseIndiaApi`, throttled to 3 req/s) implement exactly this. BSE (`bseindia.com`) is considerably more forgiving; its corporate-filing PDFs live under predictable `xml-data/corpfiling/AttachHis/<uuid>.pdf` paths, and an unofficial `BseIndiaApi` resolves scrip codes and corporate actions.

Critically, **BRSR (Business Responsibility & Sustainability Report) filings are hosted at the exchanges** — both under `bseindia.com/xml-data/corpfiling/` and `nsearchives.nseindia.com/corporate/` — so India's mandatory ESG disclosures are registry-retrievable, not search-dependent. Per Regulation 34 of the SEBI (Listing Obligations and Disclosure Requirements) Regulations 2015, starting FY2022-23 the top 1,000 listed entities by market capitalization must submit a BRSR (140 indicators: 98 essential + 42 leadership), introduced via Gazette notification SEBI/LAD-NRO/GN/2021/22 dated May 5, 2021. Cookie-priming from AWS may still hit intermittent NSE blocks; BSE should be the primary India source with NSE as fallback.

### 4. UK & Europe: Companies House API and ESEF/OAM repositories are the registries
- **UK Companies House** offers a free official API (register for a key): a Filing History API and a **Document API** (`document-api.company-information.service.gov.uk/document/{id}/content`) that returns filed accounts and resolutions as PDFs at no per-call charge. Caveat: roughly 60–75% of the ~2.2 million annual account filings are submitted electronically in XBRL/iXBRL (machine-readable), while the remaining 25–40% — between ~550,000 and ~880,000 sets of accounts per year — are filed on paper or uploaded as scanned PDF images that need OCR. This deterministically covers UK statutory annual accounts.
- **UK regulated filings** (annual financial reports for listed issuers) live in the FCA's National Storage Mechanism (NSM), now upgraded for ESEF/UKSEF taxonomies.
- **EU ESEF filings**: Since 2019 the Transparency Directive requires issuers to file AFRs in XHTML/iXBRL to national Officially Appointed Mechanisms (OAMs). There is no single EU access point yet — ESAP is due 2027. In the interim, **XBRL International's `filings.xbrl.org`** aggregates ESEF filings on a best-effort basis, and ESMA publishes its own `esef_toolkit` for extraction. A per-country OAM map is on ESMA's Databases and Registers page.

### 5. Registry vs. website coverage by document class
| Document class | Primary registry source | Registry coverage | Needs website discovery? |
|---|---|---|---|
| Annual reports / 10-K / 20-F | SEC EDGAR, Companies House, ESEF/OAM, NSE/BSE | High (US/UK/EU/India listed) | Only for private/unlisted |
| Proxy statements (DEF 14A) | SEC EDGAR | High (US) | No for US listed |
| BRSR / ESG / sustainability | NSE/BSE (India), often in AR elsewhere | Medium-High (India); Low elsewhere | Yes outside India |
| Codes of conduct | None | None | **Yes — website only** |
| Anti-bribery / whistleblowing / insider-trading policies | None (occasionally in AR governance sections) | Very low | **Yes — website only** |
| Tax strategy (UK) | Company website (UK legal requirement to publish online) | None | **Yes — website only** |
| Supplier codes of conduct | None | None | **Yes — website only** |
| Health & safety policies | None | None | **Yes — website only** |

This is the crux: roughly half your document classes are registry-covered and half are irreducibly website-only. The website-only classes are exactly where search + browser matter — and where you should concentrate that fragile capability rather than using it for everything.

### 6. Search backend options, compared

| Option | AWS-native? | `site:` scoping | Quality for corporate PDFs | No-NAT compatible | Cost |
|---|---|---|---|---|---|
| AgentCore Gateway WebSearch (current) | Yes (managed, zero egress) | No — your core complaint | Poor for specific docs | Yes (stays in AWS) | $7 / 1,000 queries |
| Amazon Nova Web Grounding | Yes (Bedrock built-in tool) | No (model-driven queries) | Fair; cited, but adopts outdated/3rd-party info | Yes | Bedrock inference + grounding fee |
| Kendra Web Crawler v2 (build an index) | Yes | N/A — you define seed/sitemap URLs | Good over KNOWN sites; not discovery | Needs proxy for internet crawl | Kendra index cost |
| Google Programmable Search Engine (PSE) | Via Lambda action group | **Yes — best site: control** | Good; capped 10k/day | Lambda outside VPC | $5 / 1,000 |
| Brave Search API (AWS Marketplace) | **Yes — billed through AWS** | Partial (query operators) | Good; independent 40B-page index | Lambda outside VPC | From $5 / 1,000 (~$5 CPM); $5 monthly credit for new users, no traditional free tier as of early 2026 |
| Bing/other SERP APIs via Marketplace | Via Marketplace | Yes | Good but pricier | Lambda outside VPC | Higher |

Notes on the search tier: Brave markets an index "of 40 billion pages… we add or refresh more than 100 million pages each day," with "Zero Data Retention," SOC 2 Type II, and AWS Marketplace availability — meaning it can be procured and billed through AWS without violating your "AWS-only" constraint. Two things worth flagging as vendor claims rather than established fact: AWS markets both AgentCore Web Search and Nova Web Grounding as "zero data egress" / "queries never leave AWS," and independent testing (Classmethod/DevelopersIO) found the AgentCore tool returned outdated employee counts and adopted loosely-related third-party sites — consistent with your "not Google-quality" complaint. The honest read: AWS-native search will not match Google for finding a specific buried PDF, which is precisely why site-scoped PSE/Brave via a Lambda action group is the pragmatic backstop.

### 7. Crawl/browser layer on AWS: Lambda vs Fargate vs AgentCore
- **Lambda + headless Chromium (`@sparticuz/chromium` + Playwright/Puppeteer)**: best for short, parallel, per-URL render/click-to-download jobs. Real constraints: 15-min hard limit; cold starts add 5–10s launching Chromium; needs 1.5–2GB+ memory (practitioners use 2–3GB); container-image packaging (Microsoft Playwright base image) is more reliable than zip layers; watch `/tmp` filling on warm invocations (use unique `--user-data-dir` and clean up). ARM64 binaries available from Chromium v135+.
- **ECS Fargate long-running Playwright workers**: best for deep IR-navigation sessions, large multi-page crawls, and jobs exceeding 15 minutes. Persistent, no cold-start penalty per URL, more memory headroom.
- **AgentCore Browser (current)**: managed, session-isolated, observable (Live View/Session Replay), CDP/Playwright-compatible — but you pay per vCPU-second ($0.0895/vCPU-hr) and GB-second ($0.00945/GB-hr), and default 15-min idle sessions accrue memory cost. Best reserved for the hardest interactive cases, not bulk.

**WAF/bot-detection reality check (Cloudflare/Akamai) from AWS IPs:** This is the one area where honesty matters most. Datacenter IPs (AWS) carry negative trust scores in Akamai Bot Manager and Cloudflare. Within a pure AWS-only constraint (no third-party residential proxies), you **cannot reliably defeat aggressive bot protection** — you can only reduce friction via realistic headers, cookie-priming sessions, CDP-driven real-Chromium rendering (AgentCore Browser or Lambda Chromium), and slow, human-like pacing. Do not promise 100% coverage of Cloudflare/Akamai-protected sites without proxies. This reinforces the registry-first strategy: registries don't bot-block.

### 8. The no-NAT/egress question, solved
Your VPC has no NAT gateway (VPC-endpoint-only egress). Options, in order of preference:
1. **Run internet-facing workers as Lambda functions OUTSIDE the VPC.** A Lambda not attached to a VPC has default outbound internet access — no NAT needed. This is the cleanest path for search-API calls (PSE/Brave), registry HTTP fetches (EDGAR, Companies House), and headless-Chromium crawlers. Keep VPC-attached components only where they must reach private resources.
2. **VPC-proxy pattern**: a non-VPC Lambda does the internet I/O and is invoked (RequestResponse) by a VPC Lambda via the AWS Lambda API (which is internet-facing / reachable via interface endpoint). Avoids NAT entirely.
3. **Add a NAT gateway** only if you insist all egress originates inside the VPC. Per AWS VPC pricing (2026, us-east-1): $0.045/hour (≈$32.40/month per gateway per AZ) plus $0.045/GB processed — so internet-bound egress stacks to ≈$0.135/GB, and a 3-AZ HA setup is ≈$97–98.55/month baseline before data. Generally unnecessary here.
4. **IPv6 egress-only internet gateway** for VPC workers if you go dual-stack.
Keep S3/DynamoDB/Bedrock access on interface/gateway VPC endpoints as today.

## Details: Recommended Target Architecture

### Tiered discovery pipeline
Orchestrate with **AWS Step Functions**, one execution per (company × document-class) request, writing provenance to your existing DynamoDB table and content-addressed objects to S3.

**Tier 0 — Registry APIs (deterministic, do this first):**
- US: EDGAR full-text search + submissions API + bulk index. Lambda (non-VPC), compliant User-Agent, ≤8 req/s, ETag caching.
- UK: Companies House Filing History + Document API (API key in Secrets Manager).
- EU: `filings.xbrl.org` / OAM per-country; ESMA `esef_toolkit`.
- India: BSE corpfiling (primary) + NSE archives (cookie-primed fallback) for annual reports and BRSR.
- Aggregator backstop: `responsibilityreports.com` / `annualreports.com` (IR Solutions) — useful but treat as unofficial (their disclaimers explicitly disclaim accuracy/completeness), so verify class before storing.

**Tier 1 — Known IR URL patterns & sitemaps (deterministic-ish):**
- Probe conventional paths (`/investors`, `/investor-relations`, `/sustainability`, `/governance`, `/policies`) and parse `sitemap.xml`.
- Exploit IR-platform CDNs with predictable patterns: Q4 Inc serves documents from `s*.q4cdn.com/<id>/files/doc_downloads/...` and `doc_financials/...`; Notified and EQS host large fractions of corporate IR sites with similarly regular structures. Detecting the IR platform lets you template URL construction.
- Look for schema.org markup and RSS feeds on IR pages.
- Run as non-VPC Lambda with `urllib`/`httpx`; no browser needed for static pages.

**Tier 2 — Site-scoped search (only when Tiers 0–1 miss):**
- Lambda action group calling Google PSE (best `site:` control, 10k/day cap) or Brave Search API (AWS Marketplace, AWS-billed). Constrain queries with `site:<company-domain>` + document-class keywords.
- Optionally keep Nova Web Grounding / AgentCore Web Search for broad cited discovery where you don't yet know the domain.

**Tier 3 — Headless browser (last resort, website-only classes):**
- Lambda + `@sparticuz/chromium` + Playwright for short click-to-download and JS-rendered pages.
- Fargate Playwright workers for deep IR navigation / long jobs.
- AgentCore Browser reserved for the hardest interactive/observability-critical cases.
- Cookie-priming + realistic headers; accept that Cloudflare/Akamai sites may fail without proxies.

**Verification (unchanged, fail-closed):** Every candidate document passes your existing Bedrock LLM class verification (Nova 2 Lite for high-volume, Claude Haiku 4.5 for selection judgments) before it is written to the S3 corpus. Preserve the fail-closed principle: a document that cannot be positively verified as the correct class for the correct company is discarded, not stored. This is your most valuable existing asset — do not touch it.

### Migration path from the v39 monolith
1. **Carve out the verification layer** into its own Lambda (or keep in AgentCore) with a stable contract — it becomes a shared service all tiers call.
2. **Stand up Tier 0 registry Lambdas first** (EDGAR, Companies House, BSE). Measure how much of your corpus these alone satisfy; for US/UK/India listed annual reports and US proxies this should be a large fraction and will immediately reduce search/crawl load and failures.
3. **Introduce Step Functions** to sequence Tier 0→3 with early exit on verified hit.
4. **Move crawling out of the monolith** into Lambda-Chromium and Fargate workers; decommission the deep static regex crawler.
5. **Swap the search backend**: add the PSE/Brave Lambda action group for site-scoped search; demote AgentCore WebSearch to broad discovery only.
6. **Retire the monolith** as each capability is extracted; the AgentCore agent becomes a thin orchestrator or is replaced by Step Functions + Lambda.

### What will and won't be solved
- **Solved:** SEC 403 (User-Agent + rate + bulk files; runs fine from AWS). UK/EU/US annual reports and US proxies (registries). India BRSR + annual reports (exchanges). No-NAT egress (non-VPC Lambda). Search `site:` scoping (PSE/Brave action group).
- **Partially solved:** NSE anti-bot (cookie-priming works but may intermittently fail from AWS IPs — prefer BSE). JS-rendered IR sites (Lambda/Fargate Chromium).
- **Not fully solvable within AWS-only constraint:** Aggressive Cloudflare/Akamai bot-walls without residential/rotating proxies. Mitigate by leaning on registries and accepting bounded coverage gaps for the hardest sites. Org-level SCPs restricting Bedrock models remain a governance constraint — confirm Nova 2 Lite / Claude Haiku 4.5 / any Nova Premier (for Web Grounding) are permitted before committing.

## Recommendations
1. **Immediately (week 1–2):** Fix EDGAR access — set a compliant `User-Agent` (org + contact email), throttle to ≤8 req/s, and pull bulk `submissions.zip`/index files nightly to a "known filings" table. This alone should sharply cut US-document failures and prove the registry-first thesis. Benchmark: % of US annual-report/proxy requests satisfied by EDGAR alone.
2. **Weeks 3–6:** Add Companies House (UK) and BSE (India, incl. BRSR) Tier-0 Lambdas outside the VPC. Add `filings.xbrl.org` for EU. Benchmark: registry hit-rate per jurisdiction; target the majority of listed-company annual reports registry-sourced.
3. **Weeks 6–10:** Build the Step Functions orchestrator with Tier 0→3 early-exit and the shared verification service. Move crawling to Lambda-Chromium + Fargate. Add the PSE/Brave site-scoped search action group.
4. **Ongoing:** Keep the fail-closed verifier untouched. Track per-class, per-jurisdiction coverage and the crawl-failure rate on Cloudflare/Akamai sites.
5. **Thresholds that change the plan:** If registry hit-rate for listed annual reports/proxies exceeds ~80–90%, further search/crawl investment is low-value — freeze it. If Cloudflare/Akamai crawl failures on website-only policy classes exceed a tolerable threshold and materially hurt coverage, that is the ONE case where you should escalate the AWS-only constraint to leadership and evaluate a proxy service procured through AWS Marketplace (bringing it "within AWS billing"), since no pure-AWS technique reliably defeats those walls. If AgentCore Browser costs dominate, shift bulk browsing to Fargate.

## Caveats
- AWS's "zero data egress" claims for AgentCore Web Search and Nova Web Grounding are the vendor's characterization; validate against your compliance requirements. Independent testing found the AgentCore WebSearch tool surfaced outdated and loosely-related results — matching your quality complaint.
- The SEC-IP conclusion rests on strong but partly practitioner-sourced evidence; the definitive proof is empirical — test a compliant-UA request from your actual Lambda. The `www.sec.gov` HTML site (Akamai) differs from the JSON/Archives endpoints; use the APIs, not HTML scraping.
- Companies House structured (machine-readable) financial data covers roughly 60–75% of accounts; the remaining 25–40% are scanned/paper PDFs requiring OCR/parsing downstream.
- Aggregators (annualreports.com/responsibilityreports.com) explicitly disclaim accuracy and completeness in their terms; use as backstop with verification, not as a primary source of truth.
- ESAP (unified EU access point) is not live until 2027; until then EU coverage depends on per-country OAMs and the best-effort `filings.xbrl.org`.
- AgentCore pricing has multiple independently billable components (Runtime, Browser, Gateway, observability with no built-in cap); model total cost before scaling browsing on AgentCore.
- Org-level SCPs may block some Bedrock models; verify model availability in-region before committing the verification/grounding design.
- Brave Search API no longer offers a traditional free tier for new users as of early 2026 ($5 monthly credit ≈ 1,000 queries); budget accordingly if you choose it over Google PSE.
