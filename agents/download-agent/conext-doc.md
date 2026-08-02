Here's how the EDO Co-Analyst Download Agent works, end to end. This document
contains historical implementation notes followed by the current behavior. Use
the handoff snapshot immediately below as the source of truth for a new chat.
The first persistent-browser version was deployed and its production diagnostic
archive was analyzed on 2026-08-02. The corrective changes described immediately
below are local and are not deployed yet; see "Deployment status."

## SEC HTML and post-browser annual coverage correction — 2026-08-02

The `csa-browser-diagnostics.Zwbry0.tar.gz` archive produced zero Annual Report
references for three different reasons: Bilibili's valid SEC Form 20-F HTML was
rejected by the PDF-only coverage reader, JFrog's PDF coverage call hit the
generic 900-second client timeout, and Fortis never delivered an Annual Report
to S3. Interactive verification confirmed that the Fortis investor hub and its
extensionless Annual Report route are public and that the Bilibili SEC filing
is valid `text/html`; public accessibility does not prevent selective CDN/WAF
blocking of AWS/headless traffic.

Current local corrections (not deployed yet):

- `annual_coverage.py` now detects PDF versus HTML from S3 metadata, filename,
  and bytes. PDF extraction remains unchanged; SEC 10-K/20-F HTML gets a
  non-executing structural parser for semantic headings, SEC Item headings,
  topic headings, anchors, summaries, and explicit `html_section` locations.
- Coverage grounding now uses stable `heading_id` values rather than asking the
  model to repeat page ranges. HTML section ordinals are never presented as PDF
  page numbers. The UI displays `HTML section N` for HTML references.
- Candidate headings are selected per requested report class and classified in
  bounded batches of five classes. This prevents hundreds of early risk
  headings from crowding a late `ITEM 16B. CODE OF ETHICS` section out of a
  single first-100-headings prompt. A dedicated matching section that
  explicitly identifies/incorporates/links a requested policy is accepted as
  `dedicated_reference`; passing mentions remain rejected.
- Annual coverage no longer blocks the main company run. A DynamoDB-leased
  background reconciler starts only after standalone/persistent-browser work is
  terminal, retries failed coverage calls up to three times, and patches
  `referenced_in_existing_document` rows back into run diagnostics. A new run
  for the same company is prevented while coverage is pending/running so its
  pre-run cleanup cannot delete the Annual Report being analyzed.
- The generic AgentCore client and SigV4 fallback now use
  `AGENT_READ_TIMEOUT` (1620 seconds by default), removing the separate
  900-second coverage timeout that failed JFrog.
- Browser clicks now capture native downloads plus same-tab, popup, and new-tab
  PDF network responses. A single blocked candidate no longer makes WAF status
  sticky across the whole job; terminal rows persist a `failure_kind` such as
  `actual_waf`, `invalid_candidate`, `challenge_html`, `transport_failure`, or
  `document_not_found`.
- Percent-encoded URL spaces are decoded before year extraction, so a path such
  as `august%2011%202026` resolves to 2026 rather than the false year 2011.
- No Terraform/IAM change is required for this correction. The existing ECS
  task role already allows DynamoDB read/write, S3 read/write, and
  `bedrock-agentcore:InvokeAgentRuntime`; provider `default_tags` continue to
  apply all mandatory tags. The existing
  `.github/workflows/coanalyst-ecs-deploy.yml` deploys the application/browser
  image unchanged. The Download Agent must still be deployed separately with
  `agents/download-agent/scripts/deploy.sh <new-tag>` before running that ECS
  workflow.

Verification: the real 4.4 MB Bilibili Form 20-F produced 275 grounded HTML
headings locally, including two `CODE OF ETHICS` sections; the late substantive
section was retained. Python compilation, `git diff --check`, and 110 focused
accuracy/lifecycle tests pass. Live AWS behavior still requires both
deployments and a fresh three-company canary.

## Production browser incident follow-up — 2026-08-02

### Second production rerun (`csa-browser-diagnostics.ShejHn.tar.gz`)

The later two-hour archive confirms the first corrective deployment improved
coverage but exposed three additional application defects. At capture time:

- JFrog run `ba2b45c6-7c10-4ff7-8179-e210b7a9fefe` had six verified downloads
  (including its Annual Report) and eight browser jobs still represented as
  five terminal WAF blocks plus three queued jobs.
- Fortis run `fb2f5c55-38b0-4e61-a0b9-1206d5cbafba` displayed three direct
  downloads. The persistent browser had also successfully verified and stored
  the anti-bribery policy, so S3/provenance already contained a fourth result,
  but run reconciliation hid that success until every serial browser job became
  terminal.
- The Fortis Annual Report job reached the official report hub but direct
  `/drupal-data/*.pdf` requests returned HTTP 403. Candidate probing reused the
  planner's working page, replacing the useful investor-hub DOM on every probe.
  The planner consequently operated on the last rejected page, repeatedly
  issued `back`, and re-tested the same links for most of its 18-step budget.
- The primary `us.anthropic.claude-sonnet-5` verifier failed every request with
  `ValidationException` because `temperature` is deprecated for that model.
  The Nova fallback worked and approved the valid Fortis anti-bribery policy.
- Browser-state IAM is now correct: encrypted state objects exist for Bilibili,
  Fortis, and JFrog and no state-bucket AccessDenied appears in the archive.
- Seven SQS jobs were visible and one was in flight; the DLQ was empty. The ECS
  application and browser services were both healthy at desired/running count
  one. The saved sessions therefore work, but the single serial worker plus the
  old navigation loop caused the long backlog.
- The apparent third-company omission was not a collection failure. Bilibili
  run `a6bdfba2-16bf-4f9e-83f4-b7f4423e951a` was manually killed while its run
  status was still `running`. Kill cleanup only cancelled browser jobs for a
  `browser_retry_pending` run, so its queued supplier-code job ran later as an
  orphan even though the parent run had been deleted.

Additional local corrections after this archive (not deployed yet):

- Candidate verification now uses an isolated probe page and never destroys
  the visual planner's current DOM. Long investor pages retain up to 500
  interactive/link items for deterministic discovery.
- Candidate URLs are attempted at most twice during the navigation sweep, the
  planner is returned to the seed that exposed the strongest report link, and
  three identical ineffective actions stop the job instead of consuming the
  remaining model/navigation budget.
- The shared Bedrock Converse request uses only portable `maxTokens`; the
  unsupported `temperature` parameter was removed, restoring Claude Sonnet 5
  while retaining Nova fallback behavior.
- More common WAF response markers are typed as blocking responses rather than
  generic non-PDF failures.
- Browser reconciliation merges each terminal downloaded job into the run/UI
  immediately while keeping the overall run `browser_retry_pending` until all
  jobs finish. It no longer hides a document already present in S3/provenance.
- Deleting any active run now paginates and cancels its queued/running browser
  jobs regardless of the parent run status, preventing orphan queue work.
- Python compilation, shell syntax, `git diff --check`, and 104 focused
  regression tests pass for this second corrective pass.

### Iris/Vertex/browser re-audit — 2026-08-02 (current local worktree)

The full discovery flow was re-audited against `iris.md`. Iris's useful generic
pattern is: start on the attested official site, inspect structured interactive
elements, use the company's own search with progressively broader phrases,
refresh the page snapshot after each state change, and use external search only
as bounded discovery. Its Capital One example also demonstrates why external
results cannot be accepted directly: a same-query result for a different
company appeared alongside the correct official source. Report IQ therefore
retains its stricter byte/content/company/class/year verification gates.

Additional changes from this audit (not deployed yet):

- Browser observations now assign stable `riq-*` references to links, buttons,
  fields, embedded documents, and data-backed download controls. Planner click
  and type actions use those references first, with visible text only as a
  fallback. This removes ambiguity on archive pages containing repeated years,
  repeated `Download` labels, and icon-only search controls.
- Native company-site search now receives a bounded, company-specific sequence:
  exact company + class + year, class + year, company + class + PDF, and (for
  undated requests) class + latest. Recent actions are supplied to the planner
  so it does not retry the same phrase indefinitely. A fresh observation is
  already taken after each action.
- The browser also inspects bounded JSON/XHR responses (maximum 2 MiB and
  10,000 scalar nodes) for official PDF/download/class-matching routes. This
  catches JavaScript investor archives that fetch document URLs through an API
  before rendering them, without adding a search or model call.
- Nova 2 Lite remains the low-cost primary navigation model. Claude Sonnet 5 is
  used only when the primary planner request errors or once after the primary
  planner repeats an ineffective action three times. This is a targeted model
  escalation, not a costly Sonnet call on every browser step. Existing Bedrock
  permissions already include both model/profile ARN forms; Terraform only
  exports the new fallback-model environment value.
- Vertex discovery no longer launches the old broad set of near-duplicate
  recency and LLM-generated queries. Gemini already receives the official
  company, domain, class aliases, year/latest intent, preferred language,
  standalone requirement, ticker, and jurisdiction as structured facts. It now
  performs one exact official-document pass. A second, differently
  worded official archive/library/PDF-download pass runs only if the first pass
  does not expose an official direct document. Calls are capped at two per
  requested class by default (`VERTEX_SEARCH_MAX_CALLS=2`).
- The Vertex prompt now explicitly asks for both direct downloads and official
  investor-report/governance-library landing pages, includes today's date for
  `latest` intent, and asks for exact-title plus broader archive formulations.
  The Lambda's source default is aligned with Terraform at
  `gemini-2.5-flash`. No model upgrade was made because the observed failure was
  navigation state/WAF behavior rather than model reasoning quality.
- The bulk query label `Human Due Diligence` is corrected to `Human Rights Due
  Diligence`; the shorter form remains only as a legacy inference alias.
- No additional GCP service is required. Vertex AI Search would require a
  maintained indexed datastore and is not a general arbitrary-company web
  index; another Cloud Run browser would duplicate the persistent ECS browser;
  and Search Console/Indexing APIs do not provide public-web document search.
  The existing Gemini Google Search grounding + deterministic site crawl +
  persistent browser is the appropriate layered design.

The loop remains bounded and fail-closed: deterministic link probing first,
then observe -> one constrained model action -> validate progress -> observe
again, with two attempts per document URL, three-repeat detection, one optional
strong-model rescue, and the existing total step limit. CAPTCHA/login bypass is
still prohibited, and persistent cookies improve continuity but do not promise
that an AWS egress IP will pass a source WAF.

Local verification after this audit: Python compilation, Terraform formatting
for the co-analyst stack, `git diff --check`, and 104/104 focused regression
tests pass. Live Vertex, Bedrock, Playwright, WAF, and document-result quality
still require deployment and a fresh canary.

Deployment now requires both paths because this audit changed both services:

1. From `agents/download-agent`, run `./scripts/deploy.sh <new-agent-tag>
   <new-vertex-tag>`. That rebuilds the AgentCore image and the isolated Vertex
   Lambda image and applies both tags.
2. Run `.github/workflows/coanalyst-ecs-deploy.yml` for the persistent browser
   code and fallback-model environment variable. The workflow needs no new
   input because the Terraform variable has a safe default.
3. Start a fresh Fortis canary after both deployments; old terminal jobs will
   not be reopened. Inspect Annual Report first, then validate every stored
   company/class/year before the 23-report run.

The archive `csa-browser-diagnostics.ZATFWB.tar.gz` proves the queue/service
architecture is running, but also explains why the Fortis canary still returned
only two reports:

- Run `fb20611d-b6f8-4ae9-8bbd-3eefb4b231b1` produced 19 browser jobs; at
  capture time 15 had ended `blocked_by_source_waf`, one was running, and three
  were queued. None had downloaded a document.
- The Annual Report job had an official, useful landing-page seed
  (`fortishealthcare.com/investors/annual-reports/476`), but weak search hits
  were visited first. The actual Annual Report is served from an official HTTPS
  route with no `.pdf` suffix. The worker's `_safe_candidate()` rejected that
  route before checking its response content, so the browser could never accept
  it even when the website exposed it.
- The worker role lacked `s3:ListBucket` on the browser-state bucket. S3 therefore
  returned AccessDenied rather than a clean missing-key result when loading a
  domain's first session state.
- The primary verifier returned a Bedrock `ValidationException`, but the old
  exception handler discarded its message and rejected every otherwise-valid
  candidate. Planner/action exceptions were similarly reduced to only an
  exception class.
- The single worker is intentionally serial. In this canary, 19 jobs at roughly
  three-to-five minutes each can take about an hour; two immediate direct
  downloads while browser jobs remain pending is therefore expected. A warm
  browser/session may reduce blocking but cannot guarantee that a source WAF
  will allow the AWS egress IP.

Corrective code now in the worktree:

- Extensionless official and explicitly attested URLs pass the transport gate.
  Bytes are still fail-closed: a result is stored only after `%PDF` integrity,
  size/parse, company, class, year, standalone, and high-confidence model checks.
- Seeds are ranked generically using official domain, requested document-class
  aliases, year, language, and near-neighbour exclusions. Every ranked
  Vertex/search/official seed is inspected deterministically before the visual
  planner spends its action budget. Visible matching links are eligible even
  when their URL does not end in `.pdf`.
- The planner prompt is generated from the company, class, year, accepted
  synonyms, excluded near-neighbours, and current visible page controls. It can
  use year tabs, archives, accordions, the company's site search, native
  downloads, PDF viewers, and observed links without inventing URLs or bypassing
  CAPTCHA/login controls.
- Verification retries with the configured fallback Bedrock model when the
  primary model/profile errors or returns invalid JSON. Full bounded model and
  Playwright error details plus ranked URLs/rejection reasons are now logged.
- Terraform adds the missing prefix-scoped `s3:ListBucket`, exports the SQS
  visibility timeout to the worker lease logic, and exposes a verifier-fallback
  model variable. Existing broad Bedrock inference-profile/foundation-model
  invoke permissions cover both configured models.
- All taggable AWS resources continue to inherit the six mandatory provider
  `default_tags`; browser resources also retain their resource-specific `Name`
  tags. Policy/configuration resources that AWS does not support tagging are
  not exceptions to a tag requirement—they have no tag field.
- `collect_browser_diagnostics.sh` no longer hard-codes a Fortis S3 prefix. It
  accepts table-name environment overrides and captures browser jobs and
  provenance for every recent run, making the same archive useful for any
  company. For multi-company reruns it also creates `run-summary.tsv`, captures
  the deployed task/execution roles and their policies, and lists browser-state
  object keys so IAM/session persistence can be correlated with each run.

Verification of these corrective changes: Python compilation passes, 93 focused
regression tests pass, `terraform fmt -check` passes, and Terraform 1.15.8
validates the configuration. A real plan could not be run locally because the
current default AWS credentials return `InvalidClientTokenId`; the GitHub Actions
OIDC deployment role must produce and review the authoritative plan.

Deployment required for the current worktree: deploy the Download Agent plus
Vertex Lambda with `agents/download-agent/scripts/deploy.sh`, then run
`.github/workflows/coanalyst-ecs-deploy.yml`. The original browser-only
corrective pass did not change Download Agent code, but the later Iris/Vertex
re-audit does. Old terminal browser jobs are not automatically reopened; start
a fresh Fortis canary after both services are stable.

## Persistent browser service update — 2026-08-02 (deployed baseline)

- Replaced one `ecs.run_task()` per WAF-blocked report with an encrypted SQS
  queue and one always-running `reportiq-browser-worker` ECS service.
- The Download Agent now returns up to 12 bounded official landing-page URLs as
  `browser_seed_urls` alongside exact blocked PDF candidates. Vertex/search
  discovery therefore survives into the long browser fallback instead of only
  the first landing page being used.
- The persistent worker keeps one Chromium process and isolated per-domain
  Playwright contexts. Cookies and local storage are saved to a separate,
  encrypted, seven-day S3 state bucket and restored after task/context recycle.
- The worker first retries exact URLs with the live session, then runs one
  bounded LLM/vision navigation loop over all supplied landing pages and the
  official root. Allowed actions are constrained to observed/seed URLs and
  visible controls; arbitrary Playwright code, CAPTCHA bypass, login bypass,
  and untrusted page instructions are prohibited.
- It captures native downloads, PDF responses/viewers, iframe/embed/data URLs,
  and eligible HTML policies rendered to PDF. Every result still requires PDF
  integrity, deterministic company/class signals, and a high-confidence Bedrock
  company/class/year/standalone decision before S3/provenance writes.
- Browser jobs are processed serially by the single service, eliminating the
  former unbounded Fargate task launcher. Jobs that finish before their parent
  run are reconciled from DynamoDB without blocking the queue consumer.
- Native temporary downloads are explicitly deleted. Persistent browser state
  is kept outside the reports bucket and expires automatically.
- New infrastructure: SQS queue + DLQ, encrypted browser-state S3 bucket,
  persistent ECS service, queue/model/session-state IAM, and SQS/Bedrock Runtime
  access through the worker's already-approved HTTPS egress path.
- Local verification: Python compilation, Terraform formatting, focused browser
  regressions, Annual Report workflow regressions, and the combined 101-test
  suite pass. Terraform formatting and `terraform validate` also pass using a
  temporary Terraform 1.15.8 binary (the system Terraform remains 1.6.5, below
  this project's >=1.10.0 requirement). A real `terraform plan` still must run
  against the deployment backend/account before apply.
- This baseline was subsequently deployed. Production diagnostics confirm the
  queue, desired-count-one ECS service, and worker were running; the corrective
  worktree changes are documented in the incident follow-up above.

### Deployment recommendation

**Yes—deploy the corrective `co-analyst-application` change, then run a fresh
controlled canary before another full-company rollout.** The baseline service is
healthy but cannot accept the observed extensionless Fortis Annual Report route.
The remaining uncertainty is source-site/WAF behavior after the corrected worker
reaches the official report hub.

Deployment order:

1. Run `.github/workflows/coanalyst-ecs-deploy.yml` and review its saved plan.
   Expect an ECS task-definition/service update and an in-place task-role policy
   update; do not approve unexpected replacement/deletion actions.
2. Confirm the browser ECS service is stable, queue polling is active, and no
   AccessDenied/network/model errors appear in
   `/ecs/reportiq-browser-worker`.
3. Start a new Fortis run (terminal jobs from the old run remain terminal),
   verify the Annual Report first, and inspect the new rejection/model logs.
4. Inspect every downloaded document for correct company/class/year, then run
   the 23-report batch only after the canary succeeds.

Rollback is to redeploy the prior co-analyst image/task definition, or disable
`enable_browser_worker` and apply the application stack. Persistent session
state expires after seven days; report objects are never deleted by that
rollback.

### GitHub Actions deployment pipeline audit

`/.github/workflows/coanalyst-ecs-deploy.yml` is the actual deployment path for
`co-analyst-application`; `scripts/deploy.sh` is not required. The workflow is
compatible with this change without a mandatory Terraform-input change:

- its push filter includes both `co-analyst-application/app/**` and
  `co-analyst-application/terraform/**`;
- the shared image build includes Chromium, Playwright, the API, and the
  persistent worker, and `linux/amd64` matches `cpu_architecture = "X86_64"`;
- Terraform 1.15.3 satisfies the stack's `>= 1.10.0` requirement;
- the saved plan/apply flow automatically includes the new `.tf` files; and
- `enable_browser_worker = true` in `terraform.tfvars` creates the
  desired-count-one service.

For this apply, confirm `AWS_DEPLOY_ROLE_ARN` can update IAM role policies and
ECS task definitions/services. The runtime task role must receive the new
prefix-scoped browser-state `s3:ListBucket` statement. Also confirm Bedrock
access to the primary/fallback verifier and planner models and HTTPS egress from
the configured private subnets.

Recommended workflow hardening (not yet applied because the workflow is outside
the two directories authorized for edits): add Terraform format/validate checks,
a deployment concurrency group, and a post-apply step that waits for both ECS
services to become stable and fails if the browser worker has fewer than one
running task. Without the last check, Terraform apply can finish without proving
that Chromium started and the worker is polling SQS.

## New-chat handoff snapshot — 2026-08-02

### Repository state

- Branch: `main`; current HEAD at the time of this snapshot: `8fb0a19`.
- The annual-report implementation landed primarily in `4d6eade` and was
  finalized with isolated Download Agent coverage logic in `a550653`; focused
  tests were committed in `ab95414`.
- The worktree was clean before this documentation-only update.
- The final design changes **two deployable services only**: the Download Agent
  and `co-analyst-application`.
- The existing PageIndex agent is not used by this feature. Its temporary Annual
  Report coverage additions were removed and its original indexing behavior was
  restored. Do not deploy PageIndex for this feature.

### Final product behavior

1. For a full-company 23-report run, the application removes the Annual Report
   query from the normal queue and invokes it first as a single dependency.
2. For an undated Annual Report request, discovery tries the official company
   site first, then SEC/other configured official registries. Explicit-year
   Annual Report requests retain registry-first behavior.
3. Once the verified Annual Report is stored in S3, all other reports start
   immediately with bounded parallelism. Defaults are one report per AgentCore
   invocation and three report invocations in flight per company
   (`AGENT_CHUNK_SIZE=1`, `AGENT_CHUNK_CONCURRENCY=3`).
4. Every non-annual structured report is sent with `standalone_only=true`.
   Therefore an Annual Report section cannot be downloaded and stored under a
   standalone Code of Conduct/policy/report class.
5. After all standalone searches finish, the application collects only results
   carrying the explicit `annual_report_reference_eligible=true` marker. This
   marker is set only for an authoritative agent `failed`/`no_document_found`
   discovery miss—not for a generic UI-level `failed` result.
6. If there are no eligible clean misses, or no Annual Report was downloaded,
   coverage analysis is skipped entirely.
7. Otherwise the application invokes the **Download Agent once more** with
   `mode=annual_report_coverage`, the Annual Report S3 key, and only the eligible
   failed classes. This mode exits before normal cleanup/discovery and cannot
   delete or download company files.
8. The isolated `annual_coverage.py` module reads the stored PDF (100 MiB default
   limit), scans all extractable text pages, embedded bookmarks, printed TOC
   entries grounded back to real physical pages, topic-bearing headings, and
   section-opening text. It uses the downloader's existing `pypdf` dependency.
9. The Download Agent calls `DEEP_SCAN_MODEL_ID` once for strict classification.
   A result is accepted only when it is high confidence and its exact heading
   and physical page range exist in the extracted index. Invented headings,
   adjusted page numbers, medium/low confidence, and passing mentions are
   rejected.
10. The application validates the response again and writes the durable manifest
    to `<company>/_manifests/annual-report-coverage.json` in the reports bucket.
    Only then are matching clean misses converted to
    `referenced_in_existing_document`.

### Failure and WAF boundaries

- `blocked_by_source_waf`, `browser_retry_queued`,
  `timed_out_pending_check`, transport/chunk errors, storage failures, and
  unmapped results never enter Annual Report fallback.
- A WAF-blocked report keeps its bounded HTTPS candidate URLs. The portal shows
  **Manual download** and **Upload file**, including while the longer browser
  retry is pending. It is never changed to `in annual report`.
- Proxy Statements and Wolfsberg Questionnaires are never eligible for Annual
  Report section fallback. Only classes in
  `ANNUAL_REPORT_REFERENCE_CLASSES` can be considered.
- If coverage extraction/model invocation fails, the manifest is not created
  and all original failed statuses remain unchanged (fail closed).
- Image-only/scanned Annual Reports have no OCR path in the Download Agent.
  Those pages produce no coverage reference rather than an uncertain match.

### Stored result and provenance behavior

- A real standalone file remains `status=downloaded` and keeps its own
  class-scoped S3 object and provenance row.
- An Annual Report section is a reference, not a second download. Its typed
  result includes `referenced_s3_key`, `manifest_s3_key`, exact heading,
  physical page range, evidence, and `confidence=high`.
- The Annual Report keeps one provenance row. References are stored in the run
  diagnostics and manifest, avoiding collisions in the existing
  `company + s3_key` DynamoDB key.
- The portal shows an **in annual report** badge and downloads the already stored
  Annual Report when the user clicks the reference action.

### Implementation map

- `agents/download-agent/agent/agent.py`: `standalone_only` enforcement and the
  early `annual_report_coverage` invocation route.
- `agents/download-agent/agent/annual_coverage.py`: independent PDF heading/TOC
  extraction, strict model classification, S3 read boundary, and coverage
  response. This file is included in the Download Agent Docker image.
- `agents/download-agent/agent/report_specs.py`: Annual Report first in the
  canonical report catalog.
- `agents/download-agent/agent/Dockerfile`: copies `annual_coverage.py`.
- `co-analyst-application/app/backend/app.py`: Annual-first partitioning,
  bounded parallel phase, clean-miss marker, one post-search Download Agent
  coverage call, manifest persistence, and typed reference application.
- `co-analyst-application/app/static/index.html`: Annual Report first in bulk
  queries, `in annual report` rendering, safe S3-key downloads, and WAF manual
  recovery URL/action.
- `tests/test_annual_report_workflow.py` and
  `agents/download-agent/agent/tests/test_accuracy_guards.py`: focused and
  regression coverage. They are not deployed.

### Local verification completed

- Python compilation passed for the downloader, isolated coverage module,
  application backend, and unchanged PageIndex runtime.
- Frontend inline JavaScript parsed successfully with Node.js.
- `git diff --check` passed.
- Combined focused/regression suite: **97/97 passing**.
- Tests cover Annual Report isolation, post-parallel one-time coverage ordering,
  standalone payloads, clean-miss eligibility, WAF/timeout/error exclusion,
  stable manifest path, exact high-confidence matching, invented heading/page
  rejection, printed-TOC grounding, Download Agent—not PageIndex—invocation,
  Docker packaging, and WAF manual recovery rendering.

### Deployment and live validation still required

Deploy in this order:

1. Rebuild/deploy `agents/download-agent` with a new image tag.
2. After its DEFAULT endpoint advances, rebuild/deploy
   `co-analyst-application` with a new image tag.
3. Do not rebuild/deploy PageIndex for this feature.

No Terraform source or dependency change is required for this feature. The
application already has authority to invoke the Download Agent, and the Download
Agent already reads/writes the reports bucket. Still confirm the configured
`DEEP_SCAN_MODEL_ID` is enabled in the target Bedrock account.

Live checks not yet performed:

- Docker builds for the two final images.
- A deployed `annual_report_coverage` invocation reading a real S3 PDF.
- Manifest creation and metadata/provenance enrichment in AWS.
- A positive clean-miss → Annual Report section reference.
- A passing mention/invented heading negative test against a real report.
- A WAF/manual-download run proving it never becomes an Annual Report reference.
- A full 23-report run confirming ordering, concurrency, final UI behavior, and
  elapsed time after the earlier PDF-year memoization performance fix.

## Purpose
Given a company (by name/ticker/CIK), the agent finds, verifies, and downloads specific classes of official corporate compliance documents — annual reports, ESG/sustainability reports, codes of conduct, anti-bribery policies, proxy statements, whistleblowing policies, insider trading policies, tax strategy documents, etc. (23 classes total) — into the S3 corpus, with every stored object backed by a provenance record. The whole design philosophy is **fail-closed**: if the agent can't verify a document with confidence, it stores nothing rather than storing something wrong.

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

### Other issues found in that session
- **Browser-worker concurrency risk — fixed in the current worktree.** The old
  `/api/browser-jobs` path launched one Fargate task per blocked report with no
  cap. It has been replaced by the persistent SQS-backed ECS service described
  at the top of this document, which processes jobs serially and reuses browser
  sessions.
- **`"Human Due Diligence"` query text bug — fixed in the current worktree.**
  The bulk UI label itself omitted `Rights`, so the backend inferred the right
  canonical class but still sent a weaker search phrase. The UI now emits
  `Human Rights Due Diligence`; the backend keeps the short phrase only as a
  backward-compatible alias and prioritizes the full canonical wording.
- **GCP Vertex/Gemini quota remains worth monitoring, but the local call
  multiplier is now bounded.** Vertex no longer uses `SEARCH_FANOUT_WORKERS`:
  each active document class makes one Gemini call and at most one sequential
  rescue call. With the current `BULK_COMPANY_CONCURRENCY=3 ×
  AGENT_CHUNK_CONCURRENCY=3`, the expected instantaneous ceiling is therefore
  about nine calls rather than the former ~36-call fan-out. The Lambda still
  leaves `reserved_concurrent_executions` unset; apply a cap only if production
  metrics show GCP throttling, because a speculative cap would merely move the
  bottleneck into Lambda throttles.

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
- The former stale-path test errors are fixed; the combined downloader and
  annual-report-workflow suite passes 104/104 locally.
- Terraform IAM changes (`s3:DeleteObject`, `dynamodb:DeleteItem` for `CLEAN_RERUN_DELETE_EXISTING`) are written but not applied.
- `co-analyst-application`'s `_refresh_timed_out_queries` reconciliation path is untested against live AWS.
- The language gate only enforces "must be English" when English is requested — no positive-match enforcement for an explicitly-requested non-English language yet.
- `page.pdf()` viability over AgentCore's managed browser session (HTML-page-as-document rendering fallback) is still unconfirmed.
- **This session's entire timeout-ladder, crawl-efficiency, content-over-filename, synonym-injection, and bulk-concurrency fix set is unverified against live AWS** — see "Deployment status" above for the specific things to watch on the first live test run.
- **New this write-up — also unverified against live AWS**: the `SITE_FIRST_WHEN_LATEST_CLASSES` routing change and `_candidate_document_year`'s PDF-content fallback. Logic is unit-tested and spot-checked against a real downloaded PDF (see the dedicated section above), but never run through the actual deployed agent against `bilibili.com`/EDGAR live — rollout step 8 above is the first real test.
- **Found via a real bulk run, now fixed but not yet re-timed live**: the PDF-content-year fallback caused a ~4x batch-runtime regression (30-40 min → ~2 hours for 23 reports) and cut successful downloads roughly in half, because `_candidate_document_year` re-hashed/re-parsed the same candidate's PDF body on every repeated check across the official_crawl → deep_crawl → browser cascade. Fixed via memoization (see dedicated section above) and unit-tested, but the actual 23-report batch has not been re-run yet to confirm the timing is restored — rollout step 9 above.
- The former ECS browser-worker launcher concurrency risk is fixed by the
  persistent queue consumer. GCP Vertex quota remains an external capacity risk
  worth monitoring during bulk runs, now with at most two sequential calls per
  active document class rather than broad query fan-out.
