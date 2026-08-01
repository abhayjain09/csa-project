Here's how the EDO Co-Analyst Download Agent works, end to end — originally written after a session that fixed the timeout architecture, crawl efficiency, and class-verification generalization, and now updated after a follow-up session that added site-first-for-latest annual report routing and a PDF-content year fallback (see the dedicated section below). Everything below is current as of this write-up; **nothing in this doc has been deployed or run against live AWS yet** — see "Deployment status."

## Purpose
Given a company (by name/ticker/CIK), the agent finds, verifies, and downloads specific classes of official corporate compliance documents — annual reports, ESG/sustainability reports, codes of conduct, anti-bribery policies, proxy statements, whistleblowing policies, insider trading policies, tax strategy documents, etc. (21 classes total) — into the S3 corpus, with every stored object backed by a provenance record. The whole design philosophy is **fail-closed**: if the agent can't verify a document with confidence, it stores nothing rather than storing something wrong.

## The five-tier discovery cascade
For a given company + document class, the agent tries tiers in order, stopping as soon as one produces a verified match:

1. **Tier 1 — Vertex AI Search grounded by Gemini** (real Vertex `generateContent` call with `tools: [{"google_search": {}}]` — confirmed genuine grounded search, not a plain LLM guess; see `vertex_search/lambda.py:_vertex_grounded_search`). Runs in an isolated Lambda (`edo-coanalyst-report-vertex-search`), kept separate from the main agent container specifically because it uses GCP credentials (pulled from Secrets Manager via AWS↔GCP Workload Identity Federation) — a deliberate security boundary. Set via `SEARCH_BACKEND=vertex_lambda`.
2. **Tier 2 — Deterministic registry lookup** (`registry_tier.py`) — SEC EDGAR (≤8 req/s, global in-process lock — safe even under bulk concurrency), UK Companies House, or NSE/BSE (India — partial). Sub-second, bypasses LLM verification entirely.
3. **Tier 3 — Sitemap enumeration**.
4. **Tier 4 — Deep static crawl** — now **class-aware** (see "Crawl efficiency" below): steers away from SEC-filing index sections for non-filing classes.
5. **Tier 5 — AgentCore Browser (JS-heavy fallback)** — Playwright via CDP, most expensive/slowest, used last.

## Verification & selection
- **Claude Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) is the default verification/selection model. Override via `SELECTION_MODEL_ID`.
- **Content is now the primary evidence for class match, filename/title is only a supporting signal** (this session's fix — see "Content-over-filename verification" below). Previously the prompt required the filename/title to explicitly name the class or an alias even though the model was also given the actual document text; that's now inverted.
- A shared per-query "verify budget" (`QUERY_MAX_VERIFIES`, default 150) and wall-clock deadline (`QUERY_MAX_SECONDS`, now 1500s — see "Timeout architecture" below) cap total work across all tiers.
- A localized/non-English variant is a hard, deterministic reject (`_is_localized_variant_url`) when English is requested.

## Timeout architecture — corrected this session (was previously misunderstood)
**The old belief that AgentCore's synchronous invoke has a hard ~900s execution wall was WRONG.** Verified against AWS docs this session: 900s is the *default* `idleRuntimeSessionTimeout` (configurable 60-28800s), and the agent's `invoke` entrypoint ([agent.py:5129](agent/agent.py:5129)) already runs a `HEALTHY_BUSY` ping loop every 20s that resets that idle timer for the whole duration of a query — confirmed empirically too: production logs show individual queries completing server-side at 400-736s without being reaped.

**The actual bug was a 10-second inversion**: the client's `AGENT_READ_TIMEOUT` (890s) was *lower* than the agent's own per-query deadline `QUERY_MAX_SECONDS` (900s) — so the client hung up 10s *before* the agent was even allowed to fail closed and return. Any query needing ≥890s was a **guaranteed false-failure**, misreported as "timed out" when the agent was genuinely still working (this is exactly what happened to CBRE/FCX Sustainability Report queries in a real bulk run — confirmed via DynamoDB `diagnostics.per_chunk[].results[]` + AgentCore CloudWatch logs, not guessed).

**Fixed ladder** (each rung must sit above the one below, with margin — this is the invariant to preserve on any future retune):
```
QUERY_MAX_SECONDS        1500s (25m)  agent.py:2675   — agent's own hard deadline, self-terminates and returns a real result
AGENT_READ_TIMEOUT        1620s (27m)  app.py:194      — client waits for that result + ~2min margin
HEARTBEAT_STALE_MINUTES  30m = 1800s   app.py:230      — reconciler won't declare the run dead before this
idleRuntimeSessionTimeout 1800s (30m)  main.tf         — AgentCore keeps the microVM alive this long while idle
maxLifetime               3600s (1h)   main.tf         — hard per-microVM-session instance cap
```
- `main.tf`'s `null_resource.runtime_update` CLI call now passes `--lifecycle-configuration "idleRuntimeSessionTimeout=1800,maxLifetime=3600"`, with a **fallback**: if the deployed AWS CLI predates that flag, the call fails before touching the API (no new runtime version is burned), the script logs a WARN, and retries without the flag — keepalive then rests solely on the existing ping loop, which already worked. This lifecycle change goes through the CLI path deliberately: the native `aws_bedrockagentcore_agent_runtime` Terraform resource has `lifecycle { ignore_changes = all }` (see "Deploy/storage follow-ups"), so anything NOT routed through `null_resource.runtime_update`'s explicit CLI call would silently never apply.
- **Known risk, not yet tested against your deployed SDK version**: [aws/bedrock-agentcore-sdk-python#471](https://github.com/aws/bedrock-agentcore-sdk-python/issues/471) — some SDK versions silently reap the microVM even at `HealthyBusy` status if the `/ping` response's `time_of_last_update` field isn't handled correctly. Watch for this specifically on the first live test of a query that legitimately runs past 900s.
- All three Python-side constants are env-overridable (`QUERY_MAX_SECONDS`, `AGENT_READ_TIMEOUT`, `AGENT_HEARTBEAT_STALE_MINUTES`) for retuning without a code change — just preserve the ordering invariant above.

## Crawl efficiency — new this session (why the timeout was being hit in the first place)
Real bulk-run logs (CBRE/FCX) showed the agent burning its entire verify budget grinding through **hundreds of wrong-class candidates** on giant IR sites before ever reaching the real document — e.g. `ir.cbre.com/financial-reports` alone surfaced 212 candidates (SEC Form 4s, earnings-call transcripts, supplemental-disclosure `.xlsx` files), none of which were ever going to be a Sustainability Report. Two **reordering-only** fixes (nothing is hard-rejected — zero recall/false-negative risk, consistent with fail-closed design):
- **`_IR_NOISE_MARKERS` in `_verify_priority`** ([agent.py:2826](agent/agent.py:2826)) — deprioritizes (`-60`) filenames matching earnings/transcript/press-release/supplemental-disclosure patterns, so they sort to the bottom of the verify queue and only cost a slot if budget remains after every promising candidate.
- **Class-aware frontier steering in `_subpage_links`** ([agent.py:2477](agent/agent.py:2477)) — for non-filing classes (`_FILING_CLASSES = {"annual report", "proxy statement", "remuneration report"}` are exempt, since their docs legitimately live there), nav links into `/sec-filings`, `/financial-reports`, `/press-releases`, etc. (`_FILING_INDEX_PATH_MARKERS`) are demoted (`-50`) so the crawler visits `/sustainability`, `/corporate-responsibility`, `/policies` first; the existing 100-page crawl cap then trims the filing indexes instead of the good sections.
- **Not yet live-verified**: whether this actually gets CBRE/FCX Sustainability Report under the new 1500s deadline, or just reduces it — needs a real re-run to confirm.

## Content-over-filename verification — new this session
The class-verification prompt in `_llm_select_best` ([agent.py:1683-1707](agent/agent.py:1683)) used to instruct the model to reject a candidate *unless its filename/title explicitly named the class or an accepted alias* — even though the model is also given the actual `content_sample`. This produced real false-negatives: e.g. Cisco's Sustainability Report is actually published as the **"Cisco Purpose Report"**, which isn't recognized as a sustainability-report alias, so it kept getting rejected as "no standalone Sustainability/ESG report; only GRI, TCFD, annual summary."

Fixed with two changes:
1. **Prompt rule inverted**: content is now primary evidence; filename/title is a supporting signal only. A generic/misleading filename with clearly-matching content is now ACCEPTED; a good-looking filename with wrong content is still REJECTED. When content is empty/unreadable (blocked fetch, binary garbage), falls back to strict filename/alias matching with confidence capped at medium — this is the safety net for candidates where content genuinely can't be judged.
2. **`"purpose report"` added as a `"sustainability report"` US-region alias** ([agent.py:425-428](agent/agent.py:425)) — it was previously only registered under `impact report`.

## Synonym-aware Tier 1 search — new this session
Previously, alias/synonym coverage for Vertex Tier 1 search worked by firing one **entire separate Gemini grounded-search call per alias-substituted query string** (e.g. 4-5 separate calls just for Whistleblowing Policy's aliases) — real cost/latency for redundant coverage. Now:
- For the Vertex backend specifically, the direct-search block skips the literal alias-substituted query fan-out and instead passes the full alias list as a single `synonyms` hint on ONE grounded-search call — `_vertex_lambda_search(..., synonyms=[...])` → `vertex_search/lambda.py:_document_search_prompt` embeds `"Document type requested: X (also called: syn1, syn2, ...)."` Gemini can weigh all synonyms within one prompt.
- Other backends (`gateway`, plain keyword search, no LLM reasoning over a hint) are unaffected and still get the literal alias-substituted query strings, since they need the actual different phrasing to find synonym-titled documents at all.
- Cuts Tier 1 Vertex calls from ~N (one per alias) down to ~1-2 per query while retaining full synonym coverage.

## Selection-LLM JSON parsing robustness — new this session
Real failure found via DynamoDB inspection: DaVita's Whistleblowing Policy and Sustainability Report queries both failed with `llm-error(JSONDecodeError)-failed-closed` — the selection call's response got truncated by the 200-token cap before completing the JSON object (occasional model preamble eating into the budget), and there was no retry. Fixed in `_llm_select_best` ([agent.py:1706-1739](agent/agent.py:1706)): `max_tokens` 200→400, plus one retry specifically on `JSONDecodeError` (not other exception types) before falling through to the existing fail-closed path.

## Site-first-for-latest annual report routing + PDF-content year fallback — new session (this write-up)
Trigger: a real query for Bilibili's "sustainability report" correctly failed closed on `bilibili.com` and fell through to EDGAR full-text search (also empty, correctly `no_document_found` — that part was working as designed). But it raised a separate, valid question: for the **annual report** class specifically, Google's own top hit is the branded PDF on `ir.bilibili.com`, yet the agent's registry-first routing meant it would grab the SEC EDGAR 20-F (`https://www.sec.gov/Archives/edgar/data/1723690/.../d91984d20f.htm`) instead, without ever trying the company's own site. Two changes, both in `agent.py`, no terraform/infra changes:

1. **`SITE_FIRST_WHEN_LATEST_CLASSES`** (env-overridable, default `"annual report"`) — [agent.py:149](agent/agent.py:149). `_discovery_route()` ([agent.py:1363](agent/agent.py:1363)) gained a `prefer_site_first: bool = False` param: when `True`, it demotes `"registry"` from the front of the route to the tail (still tried as the final fallback if the site turns up nothing — reuses the existing fallback `registry_resolve` call, no new code path there). The call site ([agent.py:6162](agent/agent.py:6162)) sets `prefer_site_first=True` only when **both** (a) the class is in `SITE_FIRST_WHEN_LATEST_CLASSES` and (b) `latest_discovery_mode` is on — i.e. the request is *undated* ("give me the annual report", no explicit year in the query). A request pinned to a specific year (`"Bilibili 2023 annual report"`) is unaffected and still goes EDGAR-first, since EDGAR never prunes old filings the way a site's own document archive can.
2. **`_candidate_document_year()` PDF-content fallback** ([agent.py:1573](agent/agent.py:1573)) — previously only looked at `url`/`title`/`report`/`source_page` text. Some companies serve the annual report from a generic filename or a hashed CDN path (Bilibili's own IR site does this for some assets: `ir.bilibili.com/media/1z2kdszd/...`) with no year anywhere in the metadata. Now, when metadata yields no plausible year and the candidate dict carries downloaded `body` bytes, it falls back to `_pdf_text_sample(body)` (first 4 pages / 4000 chars, same extractor `_hit_recency_year` already trusted for search-hit ranking) and re-runs `_extract_year_intent` on that text. This feeds both `_prefer_newer_document`'s "latest wins" comparison and the new site-first routing's implicit upgrade check (`_needs_latest_document_upgrade`), so a year-less filename doesn't silently look "undated" and lose to an older, better-labeled candidate.

**Verified this session (details of what was and wasn't checked, since this is the newest, least-baked change in the file):**
- `python -m py_compile agent.py tests/test_accuracy_guards.py` — clean.
- 6 new unit tests added to `test_accuracy_guards.py`, all passing: `AnnualReportSiteFirstRoutingTests` (route ordering for prefer_site_first=True/False, and confirms it's a no-op for a non-opted-in class like sustainability report) and `CandidateDocumentYearContentFallbackTests` (content fallback fires when metadata has no year, URL year still wins over content when both exist, `None` when neither exists).
- Full suite re-run before/after: **identical 35 pre-existing stale-path errors**, zero new failures — confirmed via a `git stash`/`stash pop` diff of the error sets, not just eyeballing pass counts.
- **Real-data check, not just synthetic fixtures**: fetched the actual `bilibili-inc-2025-annual-report_en.pdf` (2.1MB, 186 pages) via WebFetch and ran the real `_pdf_text_sample`/`_candidate_document_year` functions (AST-extracted from the live `agent.py`, not reimplemented/mocked) against the real bytes. Page 1 reads "BILIBILI INC. ... 2025 ANNUAL REPORT"; every page repeats "Bilibili Inc. 2025 Annual Report" as a header — comfortably inside the 4-page sample window. `_candidate_document_year` returned `2025` correctly both with the real filename and with the year stripped out of the URL/title to simulate a hashed-path company.
- **NOT verified**: an actual live invocation of this agent's `_invoke_sync` end-to-end against a real company (no AWS/Bedrock/Vertex credentials or `bilibili.com`/`sec.gov` network access from the dev sandbox this was built in). The route-ordering and year-fallback logic is proven correct in isolation and against real document bytes; whether it changes the *final selected document* for Bilibili (or any other company) in a live run — i.e., does the site's `bilibili-inc-2025-annual-report_en.pdf` actually get proposed as a candidate by Tier 1 search and pass the class-verification LLM check before falling through — is unconfirmed. Add this as the first live smoke test after deploy (see updated rollout list below).

## Annual-report-first dependency + section-reference fallback
Full-company runs now treat the Annual Report as a real phase dependency rather
than another item in the concurrent queue. The application isolates and invokes
the Annual Report first. As soon as it is stored, all remaining standalone
report searches start with bounded concurrency. A separate Download Agent
`annual_report_coverage` invocation is deliberately deferred until those
searches finish and is called once, only for explicitly typed clean misses. It
uses the downloader's existing `pypdf` dependency to scan embedded bookmarks,
grounded printed-TOC entries, and topic-bearing headings across all text pages,
then classifies high-confidence substantive sections and writes:

`s3://<reports-bucket>/<company>/_manifests/annual-report-coverage.json`

The remaining report classes run concurrently with `standalone_only=true`.
This overrides the downloader's historic broader-document-section acceptance so
an Annual Report cannot be copied into class-scoped S3 as if it were a standalone
Code of Conduct or policy. Only after a standalone search returns an honest,
clean miss may the application consult the manifest and return
`referenced_in_existing_document`, including the Annual Report S3 key, exact
heading, page range, grounded evidence, and confidence. WAF blocks, timeouts,
transport errors, invented headings, medium/low-confidence matches, Proxy
Statements, and Wolfsberg Questionnaires never enter this fallback.

A `blocked_by_source_waf` or `browser_retry_queued` result keeps its bounded
HTTPS candidate URLs and manual-upload path. The portal shows **Manual
download** for the official-source candidate even while the longer browser
retry is pending (and when that worker is disabled). It never replaces this
state with an Annual Report reference; a person can open the candidate locally,
verify it, and upload the saved standalone document as before.

The portal renders this status as **in annual report** with a download action for
the stored Annual Report and the relevant heading/pages. The original Annual
Report retains its single provenance row; section references live in the run
result and coverage manifest, avoiding collisions in the existing
`company + s3_key` provenance key schema.

Local verification for this addition: Python compilation passed for the
downloader, its separate coverage module, report catalog, application backend,
and focused tests. The Annual Report additions were removed from the PageIndex
runtime; it retains its pre-feature behavior and is not part of this workflow.
The combined regression
suite now passes **97/97 tests**. This session
also corrected the test helpers' obsolete `infra/agentcore-report` and
`reportiq-ecs` paths, which had previously hidden 35 tests behind
`FileNotFoundError`. Live AWS/Bedrock/S3 behavior still requires deployment and
an end-to-end smoke run.

### Exact execution order
For a normal full-company run the effective sequence is now:

1. Clean the company's prior run data once, using the existing cleanup policy.
2. Invoke only the Annual Report request. For an undated request, try the
   official company site first and retain SEC/other configured official
   registries as fallback.
3. Store the verified Annual Report and its normal metadata/provenance.
4. Immediately launch the other requested document classes with existing
   bounded concurrency, setting `standalone_only=true` on every structured
   report. Successful reports, WAF/manual cases, pending retries, timeouts, and
   runtime/storage errors retain their original typed results.
5. After every standalone search finishes, collect only results explicitly
   marked as clean discovery misses. If there are none, skip coverage analysis.
6. Invoke the Download Agent exactly once in `annual_report_coverage` mode,
   passing only the eligible failed classes. This separate invocation never
   enters cleanup or discovery. It scans the entire Annual Report's extractable
   text using embedded bookmarks, grounded printed-TOC titles, topic-relevant
   headings, and section-opening content. Image-only pages fail closed because
   no OCR dependency is added to the downloader.
7. Persist all detected heading notes plus only high-confidence coverage
   matches in the S3 coverage manifest.
8. Check those clean misses once against the manifest and convert only validated
   matches to typed Annual Report section references.

### Result contract
A successful standalone document remains `downloaded`. A section fallback is
not reported as a download or duplicate; it has its own status:

```json
{
  "status": "referenced_in_existing_document",
  "report_class": "code of conduct",
  "referenced_s3_key": "apple/annual-report/apple-2025.pdf",
  "manifest_s3_key": "apple/_manifests/annual-report-coverage.json",
  "heading": "Business Conduct and Ethics",
  "page_start": 72,
  "page_end": 78,
  "confidence": "high",
  "evidence": "Grounded explanation from the indexed section"
}
```

The manifest also records the Annual Report year, source URL, SHA-256/ETag,
extractor name, generation time, every heading note, and the validated coverage
map. It is intentionally not written as one provenance row per referenced class:
the provenance table is keyed by `company + s3_key`, so multiple classes that
point to the same Annual Report would collide.

## Performance regression fix — `_candidate_document_year` memoization (this write-up, found via live bulk-run report)
**User-reported symptom**: after the site-first-routing + PDF-content-year-fallback change above landed, a 23-report batch run went from completing ~4 reports in 30-40 minutes to completing only ~2 reports in ~2 hours. User confirmed annual report itself was fine — the regression hit other classes.

**Root cause**: `_candidate_document_year()`'s new PDF-content fallback (the `_pdf_text_sample(candidate["body"])` call) is NOT free — it computes a SHA-256 hash over the full PDF body on every single call (for the `_pdf_text_sample` cache key), even on a cache hit, and for a genuinely uncached candidate it also runs `pypdf` extraction over the first pages. This function gets called far more than once per candidate: `_needs_latest_document_upgrade(resolved)` is checked at the official_crawl gate, the deep_crawl gate, AND the browser gate ([agent.py:6276-6320](agent/agent.py:6276)), and `_prefer_newer_document(current, candidate)` ([agent.py:1605](agent/agent.py:1605)) calls `_candidate_document_year` on BOTH arguments — so the same already-resolved candidate got its year recomputed (full re-hash + potential re-parse of its PDF body) repeatedly across a single report's discovery cascade. `_query_needs_recency_scan`/`latest_discovery_mode` gates this on **any undated "give me the latest X" request**, not just annual reports — which is the default phrasing for most of the 21 document classes — so every class relying on deep-crawl-with-upgrade-checks (the classes that don't have a year in their URL, e.g. many policy/sustainability/governance documents) paid this cost repeatedly per report, compounding across a 23-report batch into the observed ~4x slowdown and the timeouts that cut successful downloads in half.

**Fix** ([agent.py:1573](agent/agent.py:1573)): `_candidate_document_year` now memoizes its result onto the candidate dict itself (`candidate["_detected_year"]`) on first computation, so the expensive PDF-hash/parse path runs at most **once** per candidate object no matter how many times the discovery cascade re-checks it. Confirmed downstream code only ever reads specific keys off `resolved`/candidate dicts (`resolved["url"]`, `["body"]`, `["ctype"]` — [agent.py:5992](agent/agent.py:5992)), never dumps the whole dict to storage/logs, so the extra cache key is inert.

**Verified this session**: `py_compile` clean; added `test_result_is_memoized_on_the_candidate_dict` to `CandidateDocumentYearContentFallbackTests` in `tests/test_accuracy_guards.py` — asserts the underlying `_pdf_text_sample` stub is called exactly once across three repeated `_candidate_document_year(candidate)` calls on the same dict, and that `candidate["_detected_year"]` is set correctly. Full suite: 84 tests (83 + this new one), same 35 pre-existing stale-path errors, zero new failures. **NOT verified**: an actual live bulk-run re-timing to confirm the 30-40 min baseline is restored — that requires deploying and re-running the same 23-report batch the user used to find this.

## Storage
- Named using content-type-based extension detection (`_safe_name`) — covers Excel formats too.
- Written to S3 (`edo-coanalyst-report-610639371721`) under a **stable, class-scoped, hash-free** `s3://BUCKET/<company>/<report_class>/<filename>` key. Every store OVERWRITES this key — a rerun always yields a fresh copy, not a skipped "duplicate." S3 versioning is on, so overwriting never loses prior bytes.
- Every stored document ALSO gets a `<key>.metadata.json` sidecar (`_write_metadata_sidecar`, [agent.py:4350](agent/agent.py:4350)) for downstream Bedrock Knowledge Base ingestion — company/doc_class/year/ticker/CIK/`capture_method`. **This is intentional, not clutter** — PageIndex's own S3 listing functions (`_list_pdfs_by_prefix`, `_list_pdfs_for_company_pi` in `co-analyst-application/app/backend/app.py`) already hard-filter `key.lower().endswith(".pdf")`, so PageIndex never sees these sidecars regardless.
- Logged in DynamoDB provenance (`edo-coanalyst-report-provenance`) via unconditional upsert; carries `capture_method` (`original_file` vs `page_render`).

## Failure mode
If verification fails or the LLM step errors out at any point, the result is `no_document_found` — the agent never stores an uncertain match. "An honest miss beats a wrong store."

## Deployment status — NOT yet deployed as of this writing
**Current worktree note:** the annual-report-first implementation is local and
has not been deployed. It changes two independently deployed services and
their shared result contract:

- `agents/download-agent`: standalone-only verification support and Annual
  Report first in the canonical catalog, plus the isolated
  `annual_report_coverage` module/invocation mode.
- `co-analyst-application`: three-phase orchestration, coverage-manifest
  persistence, typed reference results, and portal rendering.

It also updates regression/focused tests and this context document. No new
Terraform resource or IAM permission is required: the application already
invokes the Download Agent, and that runtime already reads/writes the reports
bucket. Rebuild/deploy only these two services. The PageIndex agent must not be
rebuilt for this feature; its source is restored to the pre-feature version.

Current verification is local only: compilation and frontend JavaScript syntax
checks pass, `git diff --check` is clean, and the combined suite passes 97/97.
Earlier Bilibili PDF/year checks remain valid, but the new two-service workflow
has not been live-verified against AWS. Recommended rollout order:
1. `cd agents/download-agent && terraform plan` BEFORE bumping the image tag — confirm the `ignore_changes = all` resource proposes no unexpected replace/destroy, isolated from the image/code diff. (Expect an empty/no-op plan this round — no `.tf` files changed.)
2. Confirm the configured selection and deep-scan Bedrock models are enabled in
   the target account; Annual Report coverage uses `DEEP_SCAN_MODEL_ID`.
3. `./scripts/deploy.sh <next_tag>` — watch the CLI output for either "update-agent-runtime ok" (lifecycle flag accepted) or the WARN+retry fallback (older CLI); confirm the runtime version advanced by exactly **1**.
4. Rebuild and deploy `co-analyst-application` after the Download Agent runtime
   version is live. Do not deploy PageIndex for this feature.
5. Run one full-company smoke test. Confirm the Annual Report chunk finishes
   before other document chunks start, the other chunks finish before the one
   Download Agent coverage invocation starts, and the coverage manifest exists
   only when at least one eligible clean miss requires analysis.
6. Use a company with no standalone Code of Conduct but a real dedicated Annual
   Report section. Confirm the UI shows `in annual report`, exact heading/pages,
   and downloads the Annual Report without creating a second policy PDF/provenance row.
7. Confirm a passing mention does not produce a reference, and confirm a WAF
   block/timeout remains blocked or pending rather than becoming a reference.
8. Re-run an undated Bilibili Annual Report query and an explicit-year query to
   confirm the intended site-first/latest versus registry-first/year split.
9. Re-time the 23-report batch and watch Download Agent, Vertex, AgentCore, and
    browser-worker quotas before increasing concurrency.

## Companion service: co-analyst-application (orchestrator/portal)
`co-analyst-application/app/backend/app.py` queues each document request as a "chunk" and invokes this agent's Bedrock AgentCore runtime via boto3.

### Bulk-concurrency fix — new this session
Real observed bug: a single company triggered via `/api/queries?trigger=true` (the direct single-run path) used a raw, unbounded `threading.Thread` with **zero concurrency accounting** — invisible to `BULK_COMPANY_CONCURRENCY` (default 3). So a single already-running company plus a fresh 3-company bulk batch produced 4-6+ concurrent AgentCore invocations, not the intended 3-company ceiling. Fixed: `_async_invoke()` ([app.py:2269](../../co-analyst-application/app/backend/app.py:2269)) now writes a `queued` DynamoDB row (same shape bulk batches use) and submits to the SAME `_BULK_COMPANY_EXECUTOR` bulk uses, instead of firing a bare thread. Every run-starting path now shares one real, race-free concurrency budget (the executor's own queue is atomic within the process — safe since `desired_count=1`, no horizontal scaling configured for this service).

### Timeout-related fixes (see main "Timeout architecture" section above — same ladder, orchestrator side)
- `AGENT_READ_TIMEOUT`: 890s → **1620s** ([app.py:194](../../co-analyst-application/app/backend/app.py:194)) — corrected comment removes the old "900s hard wall" belief.
- `HEARTBEAT_STALE_MINUTES`: 18 → **30** ([app.py:230](../../co-analyst-application/app/backend/app.py:230)), kept above `AGENT_READ_TIMEOUT` with margin, matching the runtime's `idleRuntimeSessionTimeout`.
- The existing `timed_out_pending_check` / `_refresh_timed_out_queries()` DynamoDB-provenance-polling reconciliation (from a prior session) is unchanged and still the mechanism that recovers a false-timeout — but with the corrected ladder, it should trigger far less often since queries mostly finish inside the deadline now.

### Known-but-unfixed bugs, found this session, NOT yet addressed
- **`/api/browser-jobs` ECS launcher has no concurrency cap** ([app.py:1027](../../co-analyst-application/app/backend/app.py:1027) `_enqueue_browser_retries`) — every `blocked_by_source_waf` result immediately calls `ecs.run_task()` with zero rate limiting. Confirmed via live AWS CLI investigation this session: one company (DaVita) alone produced 5+ simultaneous browser-worker Fargate tasks. Scaled across a 10-company bulk run with similar WAF-block rates, this risks the account's Fargate on-demand vCPU service quota or ECS API throttling. **Not fixed — flagged as a real operational risk, no code change made.**
- **`"Human Due Diligence"` query text bug** — observed in a real DynamoDB-inspected chunk result for FCX: the query text for the "human rights due diligence" class showed as `"site:fcx.com Human Due Diligence"` (missing "Rights"), suggesting a truncation/generation bug in `_REPORT_CLASS_ALIASES` or the query-templating code in `co-analyst-application/app/backend/app.py`. **Not investigated further — flagged only.**
- **GCP Vertex/Gemini API quota is the more likely bottleneck than AWS-side limits under bulk load.** The Vertex Lambda's own Terraform ([lambda.tf:157-159](lambda.tf:157)) deliberately leaves `reserved_concurrent_executions` commented out specifically to avoid hitting Vertex QPS quota — with `BULK_COMPANY_CONCURRENCY=3 × AGENT_CHUNK_CONCURRENCY=3 × SEARCH_FANOUT_WORKERS=4` ≈ up to 36 concurrent Gemini calls against a project (`poc-corpdevvertexai`) whose name suggests it's not a high-quota production project. **Not fixed — architectural risk, not something fixable from this repo alone.**

## Accuracy pass (prior session — English/US test matrix, target 90%)
Driven by a 7-company × ~22-class test workbook, overall accuracy was 46.2% (Precision 35.3%, Recall 49.2%). Root causes and fixes (all still in place, unaffected by this session's changes):
- PDF text was never actually read for verification (master cause) — `_pdf_text_sample()` added.
- Cross-company gate defeated by binary content sample — `_company_evidence_in_text()` added; `_confident` fails closed on missing `company_match`.
- Selection model default → Claude Haiku 4.5.
- One-doc-mapped-to-many-classes cascade fixed via class-scoped S3 keys.
- Proxy statement DEF 14A vs DEFA14A/supplement confusion fixed.
- Missed directly-linked PDFs (recall FN) — wider subpage fan-out, `filetype:pdf` probe.
- Recency scoring strengthened so newest-dated document wins.
- **Still not re-verified live**: the 7-company matrix hasn't been re-run since these fixes to confirm ≥90% — that's the only real proof, and it hasn't happened yet, compounded now by this session's additional changes on top.

## Known open issues right now
- The former 35 stale-path test errors are fixed; the combined downloader and
  annual-report-workflow suite passes 97/97 locally.
- Terraform IAM changes (`s3:DeleteObject`, `dynamodb:DeleteItem` for `CLEAN_RERUN_DELETE_EXISTING`) are written but not applied.
- `co-analyst-application`'s `_refresh_timed_out_queries` reconciliation path is untested against live AWS.
- The language gate only enforces "must be English" when English is requested — no positive-match enforcement for an explicitly-requested non-English language yet.
- `page.pdf()` viability over AgentCore's managed browser session (HTML-page-as-document rendering fallback) is still unconfirmed.
- **This session's entire timeout-ladder, crawl-efficiency, content-over-filename, synonym-injection, and bulk-concurrency fix set is unverified against live AWS** — see "Deployment status" above for the specific things to watch on the first live test run.
- **New this write-up — also unverified against live AWS**: the `SITE_FIRST_WHEN_LATEST_CLASSES` routing change and `_candidate_document_year`'s PDF-content fallback. Logic is unit-tested and spot-checked against a real downloaded PDF (see the dedicated section above), but never run through the actual deployed agent against `bilibili.com`/EDGAR live — rollout step 7 above is the first real test.
- **Found via a real bulk run, now fixed but not yet re-timed live**: the PDF-content-year fallback caused a ~4x batch-runtime regression (30-40 min → ~2 hours for 23 reports) and cut successful downloads roughly in half, because `_candidate_document_year` re-hashed/re-parsed the same candidate's PDF body on every repeated check across the official_crawl → deep_crawl → browser cascade. Fixed via memoization (see dedicated section above) and unit-tested, but the actual 23-report batch has not been re-run yet to confirm the timing is restored — rollout step 8 above.
- The ECS browser-worker launcher concurrency risk and the GCP Vertex quota risk (both above) are flagged but not fixed — worth a follow-up session if bulk runs at scale (10+ companies) become routine.

Want me to go deeper into any one tier, the live-test results once you have them, or start on the ECS browser-worker concurrency cap / Vertex quota risk next?
