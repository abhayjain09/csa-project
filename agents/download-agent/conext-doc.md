Here's how the EDO Co-Analyst Download Agent works, end to end — updated after a session that fixed the timeout architecture, crawl efficiency, and class-verification generalization. Everything below is current as of this write-up; **nothing in this doc has been deployed or run against live AWS yet** — see "Deployment status."

## Purpose
Given a company (by name/ticker/CIK), the agent finds, verifies, and downloads specific classes of official corporate compliance documents — annual reports, ESG/sustainability reports, codes of conduct, anti-bribery policies, proxy statements, whistleblowing policies, insider trading policies, tax strategy documents, etc. (21 classes total) — into the S3 corpus, with every stored object backed by a provenance record. The whole design philosophy is **fail-closed**: if the agent can't verify a document with confidence, it stores nothing rather than storing something wrong.

## The six-tier discovery cascade
For a given company + document class, the agent tries tiers in order, stopping as soon as one produces a verified match:

1. **Tier 1 — Vertex AI Search grounded by Gemini** (real Vertex `generateContent` call with `tools: [{"google_search": {}}]` — confirmed genuine grounded search, not a plain LLM guess; see `vertex_search/lambda.py:_vertex_grounded_search`). Runs in an isolated Lambda (`edo-coanalyst-report-vertex-search`), kept separate from the main agent container specifically because it uses GCP credentials (pulled from Secrets Manager via AWS↔GCP Workload Identity Federation) — a deliberate security boundary. Set via `SEARCH_BACKEND=vertex_lambda`.
2. **Tier 2 — Deterministic registry lookup** (`registry_tier.py`) — SEC EDGAR (≤8 req/s, global in-process lock — safe even under bulk concurrency), UK Companies House, or NSE/BSE (India — partial). Sub-second, bypasses LLM verification entirely.
3. **Tier 3 — Sitemap enumeration**.
4. **Tier 4 — Deep static crawl** — now **class-aware** (see "Crawl efficiency" below): steers away from SEC-filing index sections for non-filing classes.
5. **Tier 5 — Targeted Google recovery** — if the broad search and static tiers fail, issue at most three materially different Google-grounded probes using the exact validated legal name, canonical class/aliases, year, official domain, and a relevant path hint learned from Tier 1 results. URLs already sampled in Tier 1 are suppressed. Search ranking explicitly boosts validated legal-name/ticker/official-domain evidence, but the content-based company gate still makes the final decision.
6. **Tier 6 — AgentCore Browser (JS-heavy fallback)** — Playwright via CDP, most expensive/slowest, used last.

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

## Storage
- Named using content-type-based extension detection (`_safe_name`) — covers Excel formats too.
- Written to S3 (`edo-coanalyst-report-610639371721`) under a **stable, class-scoped, hash-free** `s3://BUCKET/<company>/<report_class>/<filename>` key. Every store OVERWRITES this key — a rerun always yields a fresh copy, not a skipped "duplicate." S3 versioning is on, so overwriting never loses prior bytes.
- Every stored document ALSO gets a `<key>.metadata.json` sidecar (`_write_metadata_sidecar`, [agent.py:4350](agent/agent.py:4350)) for downstream Bedrock Knowledge Base ingestion — company/doc_class/year/ticker/CIK/`capture_method`. **This is intentional, not clutter** — PageIndex's own S3 listing functions (`_list_pdfs_by_prefix`, `_list_pdfs_for_company_pi` in `co-analyst-application/app/backend/app.py`) already hard-filter `key.lower().endswith(".pdf")`, so PageIndex never sees these sidecars regardless.
- Logged in DynamoDB provenance (`edo-coanalyst-report-provenance`) via unconditional upsert; carries `capture_method` (`original_file` vs `page_render`).

## Failure mode
If verification fails or the LLM step errors out at any point, the result is `no_document_found` — the agent never stores an uncertain match. "An honest miss beats a wrong store."

## Deployment status — NOT yet deployed as of this writing
Everything in this doc is uncommitted on `feature/download-agent-fix` and has **not** been deployed or run live. Verification this session was structural only: `python -m py_compile`, the unit test suite (77 tests, same 35 pre-existing stale-path errors as baseline — see "Known open issues" — zero new failures), `terraform fmt`, and a bash syntax check on the new CLI heredoc. **Nothing here is live-verified.** Recommended rollout order:
1. `cd agents/download-agent && terraform plan` BEFORE bumping the image tag — confirm the `ignore_changes = all` resource proposes no unexpected replace/destroy, isolated from the image/code diff.
2. Confirm `us.anthropic.claude-haiku-4-5-20251001-v1:0` is enabled in the target Bedrock account.
3. `./scripts/deploy.sh <next_tag>` — watch the CLI output for either "update-agent-runtime ok" (lifecycle flag accepted) or the WARN+retry fallback (older CLI); confirm the runtime version advanced by exactly **1**.
4. **Live test target**: re-run a query that previously false-failed near the old ~900s mark (CBRE or Freeport-McMoRan "Sustainability Report" — both confirmed via DynamoDB inspection this session to have failed with `"AgentCore did not respond within the client read timeout, and no matching document appeared in provenance within 18 minutes afterward"`). Confirm it now completes and returns a real result (found or honest `no_document_found`) instead of a false timeout.
5. Watch for `[find] budget stop` / verify-exhaustion log lines dropping on big IR sites (CBRE, FCX) — confirms the crawl-efficiency fix is actually reducing wasted work, not just tolerating it via the longer deadline.
6. Confirm Cisco's Sustainability Report resolves via the "Purpose Report" alias / content-over-filename fix.
7. Deploy `co-analyst-application` (the orchestrator) **separately** — different repo/ECS service — for the `AGENT_READ_TIMEOUT`/`HEARTBEAT_STALE_MINUTES` changes and the bulk-concurrency fix (next section) to take effect.

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
- `tests/test_accuracy_guards.py`'s `_load_*_helper` functions point at stale paths (`infra/agentcore-report/...`, `reportiq-ecs/...`) that don't exist in this repo layout — pre-existing, unrelated to any session's changes, **35 test errors every run**, not yet fixed. Any future test-run check should filter these out rather than treat them as real regressions (verified this session: zero *new* failures added on top of this baseline, multiple times).
- Terraform IAM changes (`s3:DeleteObject`, `dynamodb:DeleteItem` for `CLEAN_RERUN_DELETE_EXISTING`) are written but not applied.
- `co-analyst-application`'s `_refresh_timed_out_queries` reconciliation path is untested against live AWS.
- The language gate only enforces "must be English" when English is requested — no positive-match enforcement for an explicitly-requested non-English language yet.
- `page.pdf()` viability over AgentCore's managed browser session (HTML-page-as-document rendering fallback) is still unconfirmed.
- **This session's entire timeout-ladder, crawl-efficiency, content-over-filename, synonym-injection, and bulk-concurrency fix set is unverified against live AWS** — see "Deployment status" above for the specific things to watch on the first live test run.
- The ECS browser-worker launcher concurrency risk and the GCP Vertex quota risk (both above) are flagged but not fixed — worth a follow-up session if bulk runs at scale (10+ companies) become routine.

Want me to go deeper into any one tier, the live-test results once you have them, or start on the ECS browser-worker concurrency cap / Vertex quota risk next?
