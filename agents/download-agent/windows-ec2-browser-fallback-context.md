# Windows EC2 persistent-browser fallback context

Last updated: 2026-08-03

Status: design and implementation plan only. The Windows EC2 fallback described
here has not yet been added to Terraform or application code. The existing ECS
browser worker remains the deployed browser fallback until this plan is
implemented and enabled.

## Current observed findings from hands-on testing

The notes below capture what was actually tested on Monday, August 3, 2026.
They do not change the design requirements in this document, but they do add
important current context for the next Codex/operator task.

### Active personal AWS account and current test instance

- Verified AWS CLI identity during testing:
  `arn:aws:iam::730335615031:user/kk_labs_user_991179`
- Current test region: `us-east-1`
- Current Windows instance:
  - Instance ID: `i-0ba477edca7c6899c`
  - Public IPv4: `50.19.191.201`
  - Private IPv4: `172.31.17.64`
  - Instance type: `t3.medium`
  - Platform: `Microsoft Windows Server 2025 Datacenter`
  - SSM agent status: online
- Health checks were verified as `running`, `SystemStatus=ok`,
  `InstanceStatus=ok`.

### What was verified successfully

- AWS CLI access works against the current personal account.
- The instance is reachable over RDP and a headed Windows desktop session was
  opened successfully through `Windows App` on macOS.
- RDP credential validation succeeded for the `Administrator` account.
- The Windows interactive desktop is usable enough for manual browser testing.
- `aws ssm start-session --target i-0ba477edca7c6899c` works.
- Microsoft Edge is available in the interactive Windows session.
- The EC2 `Downloads` folder was visually confirmed in the interactive desktop.

### Current blocker: SSM Run Command is not usable yet

During testing, Session Manager connectivity worked but SSM Run Command did not.
The following behavior was confirmed:

- `aws ssm send-command --document-name AWS-RunPowerShellScript ...` returned a
  valid command id, but invocation status was `Failed`.
- `aws ssm get-command-invocation` returned:
  - `Status=Failed`
  - `StatusDetails=AccessDenied`
  - `ResponseCode=-1`
- The instance role was inspected and had `AdministratorAccess`, so the failure
  is not explained by a missing standard EC2 role policy alone.
- The calling IAM user was subject to a restrictive policy named
  `AWS_EKSECSWithConditions`.

Implication: for the next task, either:

- fix IAM/organization policy so `ssm:SendCommand` and related operations work
  against this managed instance, or
- continue manual headed-browser testing in the open RDP session until that
  AWS-side restriction is removed.

### What was tested locally versus on Windows EC2

Two different test paths were exercised and should not be confused:

1. Local Mac Playwright matrix runs:
   - A deterministic URL-matrix runner was built locally and run from the Codex
     workspace.
   - Those results were generated with Playwright `chromium` on macOS.
   - Early matrix results were explicitly `headless=True`.
   - The local runner was later updated to support headed `chrome`, `msedge`,
     or `chromium`, plus per-case selection and real download saving.
   - Local artifacts were written under:
     `/Users/abhay/Documents/playwrite-testing/tmp/url-matrix-results/`

2. Windows EC2 interactive browser checks:
   - These used the actual headed Windows desktop reached over RDP.
   - Microsoft Edge was the confirmed available browser in that session.
   - This path is the one that matters most for the Windows fallback canary.

### Local tooling created during testing

The following helper scripts were created in the local workspace and may be
useful references for a future Windows-host deployment task:

- `/Users/abhay/Documents/playwrite-testing/scripts/ssm_exec.sh`
  - helper for attempting interactive SSM PowerShell execution
  - updated to preserve transcripts on parse failure
- `/Users/abhay/Documents/playwrite-testing/scripts/url_matrix_test.py`
  - local URL-matrix runner
  - supports browser selection and saves screenshots/downloads
- `/Users/abhay/Documents/playwrite-testing/scripts/url_matrix_ec2.py`
  - Windows-oriented Python runner intended to be executed on the EC2 host
- `/Users/abhay/Documents/playwrite-testing/scripts/run_url_matrix_ec2.cmd`
  - Windows `.cmd` launcher for the EC2-host runner

These scripts were created for testing convenience only; they are not yet
integrated into the production repository or Terraform.

### Current manual/POC matrix observations

The full 26-URL matrix was exercised locally multiple times with deterministic
Playwright. The results are useful as a baseline, but they should not be treated
as the final answer for the Windows EC2 fallback because the main goal is headed
Windows-browser behavior.

Observed local baseline as of August 3, 2026:

- Total URLs tested: `26`
- Clean `200` opens: `8`
- Direct browser downloads saved locally: `5`
- Challenge/blocked detections: `13`
- Timeouts: `0`

Breakdown by company:

- Fortis Healthcare Limited:
  - `12` tested
  - `0` clean `200`
  - `1` direct download observed
  - `11` challenged/blocked, commonly `403` with `Just a moment...`
- Capital One Financial Corporation:
  - `7` tested
  - `3` clean `200`
  - `2` direct downloads observed
  - `2` `Access Denied`/challenge cases
- Fujimi Incorporated:
  - `7` tested
  - `5` clean `200`
  - `2` direct downloads observed
  - `0` challenge detections in the local baseline

Local downloaded artifacts that were positively saved to disk:

- `fortis-05` -> `code of conduct.pdf`
- `capone-06` -> `wolfsberg_2025.pdf`
- `capone-07` -> `third-party-code-of-conduct-10.24.pdf`
- `fujimi-06` -> `a82238391744634.pdf`
- `fujimi-07` -> `a83563292491687.pdf`

Interpretation:

- The local deterministic baseline confirms which URLs can already be captured
  as direct downloads without Windows-specific interaction.
- The main unresolved value of the Windows canary remains:
  - whether headed Windows browser state improves Fortis/WAF outcomes,
  - whether annual-report landing pages with click flows succeed in a real
    interactive session, and
  - whether those headed-browser results can be reproduced deterministically
    through Playwright on the Windows host.

### Practical next step for the next operator/Codex task

The next task should prioritize this sequence:

1. Restore usable SSM Run Command permissions for the current personal AWS
   identity and managed instance.
2. Verify whether stable Google Chrome is installed on the Windows instance;
   if not, install it.
3. Run the Windows-host matrix in a headed browser on the EC2 desktop itself,
   preferring `channel="chrome"` and using `msedge` only as a fallback.
4. Record the exact annual-report click sequence, downloaded filename, capture
   mechanism, and challenge behavior for each landing-page case.
5. Only after headed Windows-host behavior is verified should automation or
   production-integration changes proceed.

## Independent personal-AWS proof-of-concept task

This section is the handoff contract for a separate implementation/test task.
That task must build an isolated proof of concept in the user's personal AWS
account. It must not deploy into, read from, or write to the production Report IQ
AWS account.

### Objective

Prove whether a persistent, headed Google Chrome session on Windows EC2 can:

1. Open the required test URLs that fail or degrade in the existing ECS worker.
2. Navigate an official annual-report landing page and click its Download button.
3. Capture native downloads, popup/new-tab PDF responses, same-tab responses,
   viewer downloads, and same-origin blob downloads.
4. Retain ordinary first-party cookies between jobs and instance restarts.
5. Upload successfully captured test artifacts to an isolated personal-account
   S3 bucket and remove only the local per-job temporary downloads.
6. Produce a machine-readable result matrix without changing production code,
   data, queues, tables, or Terraform state.

### Safety and isolation requirements

- Use a named AWS CLI profile such as `csa-personal`; do not assume `default` is
  the personal account.
- Before every deployment or destructive command, run
  `aws sts get-caller-identity --profile csa-personal` and compare `Account` with
  an explicit `expected_personal_account_id` Terraform variable. Terraform must
  fail its precondition if they differ.
- Use a unique resource prefix such as `csa-win-browser-poc-<short-account-id>`.
- Use a separate Terraform root/state, suggested location:
  `co-analyst-application/windows-browser-poc/terraform`.
- Do not reference production SQS queue URLs, DynamoDB tables, reports bucket,
  browser-state bucket, KMS keys, IAM roles, VPC IDs, or Terraform remote state.
- Use a dedicated canary S3 bucket with versioning, public access blocked, TLS-
  only bucket policy, default encryption, and a short lifecycle policy.
- Use separate POC SQS and DLQ resources. A DynamoDB results table is optional;
  S3 JSON results are enough for the first test.
- Apply the repository's mandatory tags to every taggable resource. Also set
  `Environment=personal-poc`, `Workload=windows-browser-fallback`, and an owner
  tag appropriate for the personal account.
- Add an AWS Budget/alarm before leaving the instance running continuously.
- Do not sign Chrome into a personal or corporate Google account.
- Do not automate CAPTCHA solving, authentication bypass, or rate-limit evasion.

### Recommended POC repository layout

Keep the experiment independent while reusing only safe, copied/refactored logic
that the task can test locally:

```text
co-analyst-application/windows-browser-poc/
  README.md
  requirements.lock
  src/
    worker.py
    browser_capture.py
    url_matrix.json
    result_schema.json
  scripts/
    bootstrap-windows.ps1
    install-worker.ps1
    enqueue-test-jobs.ps1
    collect-results.ps1
    remove-poc.ps1
  terraform/
    versions.tf
    providers.tf
    variables.tf
    main.tf
    iam.tf
    outputs.tf
    terraform.tfvars.example
  tests/
```

The production `browser_worker.py` may be read for behavior, but the initial POC
must not be wired to production application queues or run records. After the POC
passes, shared code can be extracted and production integration can follow the
architecture later in this document.

### POC phases

#### Phase A: manual Windows network/browser matrix

No application automation or LLM is needed. Use normal Google Chrome in the
interactive Windows desktop and test every URL in the required matrix below.
Record the result schema shown under the URL list. This answers whether the
Windows EC2 IP/OS/browser path helps at all.

#### Phase B: deterministic Playwright capture

Install Python and Playwright on the Windows host. Use a persistent Chrome
profile, headed mode, one job at a time, and deterministic selectors based on
visible `Download`, `View PDF`, `Annual Report`, fiscal-year, and language text.
Support `expect_download`, popup, response, navigation, PDF viewer, and blob
capture. Do not call Bedrock or Vertex yet.

Success for Phase B means the worker reproduces the manual result for the annual
report and at least two other click/download cases, uploads bytes to the POC S3
bucket, verifies `HeadObject`, and then removes the job temp directory.

#### Phase C: dynamic LLM navigation

Only after deterministic capture passes, add the bounded planner from this
document. If the personal account has Bedrock model access, use a configured
inference profile and the EC2 instance role. Otherwise, the independent task may
exercise the planner using fixture responses while leaving live LLM navigation
disabled. Vertex is not required for this Windows-browser proof because all test
URLs are already supplied.

#### Phase D: production-integration recommendation

Compare manual, deterministic Playwright, and LLM-guided results. Produce a
short report with success rate, elapsed time, error categories, screenshots,
download capture mechanism, S3 confirmation, and estimated always-on cost. Do
not connect the POC to production automatically.

### Software/bootstrap requirements

The bootstrap must install or verify:

- A current Windows Server Desktop Experience AMI.
- AWS Systems Manager Agent and a healthy managed-instance registration.
- Stable 64-bit Google Chrome.
- 64-bit Python 3.11 or 3.12.
- A dedicated Python virtual environment under `D:\ReportIQ-Poc`.
- Locked Python dependencies including `playwright`, `boto3`, and `pypdf`, plus
  the minimal validation/image libraries actually imported by the POC.
- `python -m playwright install chromium` as a fallback browser, while normal POC
  execution uses installed Chrome with `channel="chrome"`.
- CloudWatch Agent only if automated log shipping is included in the POC.

Use a dedicated standard Windows user. During the POC, the user may log in
manually and start a Task Scheduler task configured as `Run only when user is
logged on`. Do not store an auto-logon password in Terraform, EC2 user data, S3,
or source control. Prove browser behavior first; automate interactive logon only
after a security review.

No Git installation is required on the instance. Deploy a versioned ZIP through
the POC artifact bucket or SSM. No AWS access keys should exist on disk; use the
EC2 instance profile.

### Minimal isolated POC AWS resources

- One POC artifact/results S3 bucket.
- One Windows-test SQS queue and DLQ.
- One CloudWatch log group, if automated worker logging is enabled.
- One EC2 IAM role/instance profile with SSM, POC queue, POC bucket, and optional
  POC Bedrock access only.
- One no-inbound security group.
- One encrypted Windows EC2 instance or launch template.
- Optional dedicated EIP to keep the test source IP stable.
- Optional AWS Budget and billing alarm.

Start with the existing personal-account Windows instance if it is already
working. The independent task should accept `existing_instance_id`; it must not
attempt to import, replace, stop, or terminate that instance without explicit
authorization. Terraform can safely create the surrounding POC bucket, queue,
role, and log group. If the instance cannot accept the new role without replacing
an existing required profile, stop and report that conflict.

For a newly created test instance, use an encrypted 80 GiB `gp3` root volume,
IMDSv2 required, detailed monitoring optional, no inbound rules, and SSM access.
A reasonable initial size is a current-generation Windows-compatible instance
with 2 vCPU and 8 GiB RAM; resize only if Chrome/Playwright telemetry proves it
necessary.

### POC IAM boundary

The personal-account role must have no production ARNs and no wildcard access to
all S3/DynamoDB/SQS resources. Scope it to:

- The POC input queue and DLQ operations actually used by the worker.
- The POC bucket and its `artifacts/`, `results/`, `downloads/`, and `state/`
  prefixes.
- The dedicated POC CloudWatch log group.
- The selected Bedrock inference profile/model only if Phase C is enabled.
- SSM managed-instance permissions.
- KMS permissions only for a POC customer-managed key, if one is created.

The worker does not need production DynamoDB access for Phase A-C. Store each
result as `results/<test-run-id>/<url-id>.json` and each verified test download
as `downloads/<test-run-id>/<url-id>/<safe-filename>`.

### Required POC outputs

The independent task is complete only when it provides:

- Terraform plan/apply evidence with the verified personal AWS account ID.
- Instance ID, region, public/EIP address, AMI, instance type, Chrome version,
  Python version, worker artifact version, and SSM status.
- One JSON result per URL plus a combined CSV/JSON matrix.
- Screenshot on failure/challenge and before each click-required download.
- Final URL, redirect chain, visible clicked label, capture mechanism, MIME,
  size, SHA-256, and S3 key for each successful download.
- Evidence that `HeadObject` matched size/checksum before the local temp file was
  deleted.
- Evidence that domain profile cookies persisted across one worker restart.
- A list of local temp files remaining after the run and why.
- A cost estimate and an explicit recommendation to proceed or stop.
- A teardown plan and confirmation that teardown does not target the existing
  manually created Windows instance unless explicitly requested.

### Teardown requirements

The POC must be easy to remove. Delete only resources carrying the exact POC
prefix and expected personal account ID. Emptying a versioned S3 bucket and
terminating an EC2 instance are destructive operations and require explicit
confirmation. If the existing Windows instance was supplied, teardown removes
only the POC scheduled task/app directory, IAM attachment if safe, queue, logs,
and POC bucket; it must leave the instance running.

## Why this fallback is being considered

The current discovery path already uses official search results, deterministic
crawling, Vertex search, AgentCore reasoning, and a persistent Playwright worker
on ECS/Fargate. Recent diagnostics showed that the application, queues, IAM,
AgentCore, Vertex, and ECS service are operational. The remaining failures are
mainly source-specific:

- AWS/Linux/headless browser or TLS fingerprint rejection.
- Cloudflare/WAF 403 and 522 responses.
- Pages that require a real navigation sequence before the document is exposed.
- Download buttons implemented with JavaScript, popups, blob URLs, or XHR.
- Referer, cookie, local-storage, or short-lived download-token requirements.
- PDF viewers where the landing page opens but the file is not downloaded until
  the user clicks a Download button.

Manual testing on a newly launched Windows EC2 instance opened three of the five
previously blocked URLs. That is enough evidence to justify a controlled Windows
canary, but not enough to replace the existing worker. It does not prove that all
sites will work: an AWS public IP remains in an AWS ASN, and CAPTCHAs, explicit
bot challenges, origin-server failures, and policy blocks may still fail.

## Recommended architecture

Keep the current Linux ECS browser as the first browser fallback. Add one
always-running Windows EC2 worker as the final automated fallback for eligible
jobs.

```text
Download Agent / Vertex search
          |
          v
Verified direct download and ECS browser attempt
          |
          | unresolved WAF, click-required, or browser-fingerprint failure
          v
Windows escalation SQS queue + DLQ
          |
          v
Windows Server EC2
  - Google Chrome, headed mode
  - Playwright running on the Windows host
  - encrypted persistent per-domain profiles
  - one active navigation at a time
          |
          v
Existing verification gates
  - official-domain and redirect validation
  - PDF/HTML type, size, and parse checks
  - company/class/year/standalone checks
  - Bedrock verifier when required
          |
          v
Reports S3 + metadata sidecar + provenance DynamoDB
          |
          v
Delete the per-job temporary download
```

Use a **separate Windows SQS queue**, not the existing ECS browser queue. If both
workers consume the same queue, the ECS worker can take a job intended for
Windows and vice versa. Separate queues also provide independent retry limits,
visibility timeouts, metrics, DLQs, and cost controls.

Do not expose Chrome DevTools, Playwright, WinRM, or RDP to the internet. The
worker process and Chrome must run on the EC2 host. Administration should use
AWS Systems Manager Session Manager or Fleet Manager. The security group should
have no inbound rules.

## When a job should escalate to Windows

Windows should not receive all 23 report classes for every company. Enqueue only
after ordinary download and the ECS browser have failed, and only when there is
useful official evidence.

Eligible reasons:

- `blocked_by_source_waf` after the ECS per-domain circuit opens.
- A safe official landing page opened but its download requires an interaction.
- The browser saw a matching Download/View PDF control but captured no bytes.
- A popup, blob URL, XHR response, or session-bound link was detected but the ECS
  browser could not complete it.
- A strong official clean miss with a non-root, class-relevant path.
- A domain previously observed as `windows_reachable` while ECS remained blocked.

Do not enqueue:

- No official-domain evidence.
- A generic company homepage with no class signal.
- A verified class mismatch or company mismatch.
- A report class the company clearly does not publish as a standalone document.
- Login-only content, paywalls, robots-policy denials, or a CAPTCHA requiring
  circumvention.
- The same domain while its Windows circuit breaker is cooling down.

Add these fields to the browser job record/message:

```json
{
  "execution_target": "windows_ec2",
  "escalation_reason": "click_required",
  "company": "Example Company",
  "official_domain": "example.com",
  "report_class": "annual report",
  "year": 2025,
  "candidate_urls": [],
  "browser_seed_urls": [],
  "observed_controls": ["Download annual report"],
  "source_page_url": "https://example.com/investors/annual-reports",
  "ecs_attempt_summary": {},
  "run_id": "...",
  "job_id": "..."
}
```

The message must stay bounded: retain at most 12 official seeds and 8 exact
candidates, with URLs normalized and deduplicated.

## Windows browser and session model

Use the installed stable Google Chrome (`channel="chrome"`), not a remotely
controlled browser and not a Windows container. Launch it with Playwright using
a persistent user-data directory and `headless=False`.

A Windows service runs in Session 0 and is not a reliable way to host a visibly
headed Chrome window. For the canary, use a dedicated local Windows user and a
Task Scheduler task configured to run only in that interactive user session.
The worker starts at logon and restarts on failure. The instance must be reserved
for this public-document workload; do not use an administrator's normal browser
profile or personal login session.

Recommended directory separation:

```text
D:\ReportIQ\app\                    deployed application artifact
D:\ReportIQ\profiles\<domain>\     persistent encrypted Chrome profiles
D:\ReportIQ\jobs\<job-id>\         temporary downloads and screenshots
D:\ReportIQ\logs\                   local rolling logs before CloudWatch upload
```

- The EBS volume must be encrypted.
- Use one profile per registrable domain to prevent unrelated sites from sharing
  cookies and storage.
- Keep a bounded least-recently-used profile pool. Suggested initial maximum: 50
  domains and 30 days idle retention.
- Never delete a domain profile when cleaning a completed job. Delete only the
  job's temporary directory.
- Mirror a sanitized Playwright storage-state backup to the existing encrypted
  browser-state S3 bucket if instance replacement recovery is required. Never
  upload Chrome lock files, caches, download history, or arbitrary profile data.
- Do not store login credentials. This fallback is for public documents only.

Start with one active job/navigation at a time. A single shared interactive
desktop and Chrome profile is more reliable when navigation is serial. Parallel
tabs increase WAF request rates, complicate download attribution, and can corrupt
persistent profiles. A maximum of two tabs is reasonable only for a page plus a
popup/download tab.

## Dynamic browser instructions

The planner prompt must be generated for each company and report class. Follow
the useful interaction pattern from `iris.md`, adapted to this bounded worker:

1. Navigate directly to the strongest supplied official URL.
2. Wait for DOM readiness and a short network-idle window.
3. Dismiss only ordinary cookie/consent dialogs.
4. Capture a DOM accessibility snapshot and a screenshot.
5. Rank visible controls using company name, report aliases, requested year,
   `annual report`, `financial report`, `view`, `download`, `PDF`, and language-
   appropriate equivalents.
6. Click a referenced visible control instead of inventing selectors or URLs.
7. After every state-changing click, dropdown selection, tab change, or archive
   expansion, take a fresh snapshot before deciding the next action.
8. Observe Playwright download events, new tabs/popups, navigations, and network
   responses at the same time.
9. Stop immediately when a candidate document is captured; verification decides
   whether it can be stored.
10. Stop on CAPTCHA/login/bot-verification rather than bypassing it.

Planner actions should use a strict schema:

```json
{
  "action": "click|navigate|scroll|select|wait|download|stop",
  "ref": "visible-element-reference",
  "url": "official URL only when action=navigate",
  "reason": "short grounded reason",
  "expected_result": "download|popup|page_change|more_controls"
}
```

Use deterministic actions before an LLM call. The LLM should decide among a
small set of visible elements, not receive hundreds of raw links. Give it:

- Company legal/common names and ticker.
- Official registrable domain and allowed subdomains.
- Exact report class and accepted aliases.
- Requested year or latest intent.
- Current URL/title, visible text, and interactive elements.
- Candidate source URLs already found by Vertex.
- A screenshot only when DOM evidence is insufficient.
- Prior actions and failure reasons so it cannot repeat a loop.

Initial limits:

- 12 planner actions per job.
- Two repeated identical action signatures before stopping or using one stronger
  planner-model rescue.
- Three page-level navigation failures per domain before opening the Windows WAF
  circuit for 30 minutes.
- 20-minute hard deadline per job, with SQS visibility heartbeat extension.
- One first-party site-search attempt with progressively broader query aliases.
- No unbounded pagination. Probe up to three archive pages; switch to discovered
  URL/API patterns for larger uniform archives.

## Annual-report pages with a Download button

The Windows worker must treat an annual-report landing page as a navigation task,
not as a direct HTTP download. The capture sequence should be:

1. Open the official investor-relations annual-report/library page.
2. Accept normal cookies and wait for archive widgets to render.
3. Expand the newest fiscal year or `Annual Reports` accordion if present.
4. Select the newest report matching the requested language/year.
5. Arm `expect_download`, popup, response, and navigation listeners before the
   click.
6. Click the visible Download/View Annual Report button.
7. If a PDF viewer opens, inspect its toolbar Download button and its network
   responses. Prefer the original PDF response bytes over printing the viewer.
8. For a same-origin blob URL, read the blob through the page's authenticated
   context. Do not hand the blob URL to a separate unauthenticated HTTP client.
9. Preserve the landing-page URL, clicked control text, final response URL,
   redirect chain, and capture mechanism as provenance.
10. Verify and upload only the captured document, never the viewer shell.

Supported capture mechanisms should be recorded as one of:

- `windows_browser_native_download`
- `windows_browser_pdf_response`
- `windows_browser_popup_response`
- `windows_browser_authenticated_fetch`
- `windows_browser_verified_html_render`

For Annual Report, do not use `verified_html_render`: annual-report coverage needs
the real PDF/HTML filing content and page structure. If only a viewer shell is
available, fail closed with `viewer_document_not_captured`.

## Download verification, upload, and cleanup

The Windows route must call the same verification logic as the existing ECS
worker. A successful click alone is not success.

Required gates:

- Final URL remains on the official domain or a discovered/attested first-party
  document CDN.
- Response is a supported PDF or filing HTML document, not an error page.
- File signature, MIME type, maximum size, and parser checks pass.
- Company identity is present in document content.
- Report class matches content; standalone-only rules still apply.
- Requested/latest year checks pass where applicable.
- The high-confidence verifier accepts the document when deterministic evidence
  is insufficient.

Upload flow:

1. Download into `D:\ReportIQ\jobs\<job-id>\` with a generated safe filename.
2. Stream a SHA-256 checksum while reading the file.
3. Upload to the existing reports bucket using the current stable company/class
   key convention.
4. Write metadata sidecar and DynamoDB provenance, including Windows capture
   mechanism, source landing page, clicked label, checksum, instance ID, and
   Chrome version.
5. Confirm the S3 object with `HeadObject`; require matching content length and
   stored checksum metadata.
6. Update the browser job and parent run atomically/idempotently.
7. Delete the temporary download and screenshots only after confirmation.
8. On upload/verification failure, retain the temporary directory for a bounded
   diagnostic interval (for example, 24 hours), then clean it automatically.

Never delete the persistent per-domain profile during post-upload cleanup.

Use `job_id + report_class` as the idempotency key so a retried SQS message cannot
append duplicate run results or create uncontrolled S3 keys.

## Infrastructure to add

Recommended Terraform resources, preferably in a new
`co-analyst-application/terraform/windows_browser.tf`:

- Windows browser SQS queue and DLQ with redrive policy.
- CloudWatch log group and alarms for queue age, DLQ depth, worker heartbeat,
  WAF rate, successful captures, and disk usage.
- Dedicated EC2 IAM role and instance profile.
- No-ingress security group with controlled HTTPS/DNS/VPC-endpoint egress.
- Encrypted EBS volume for the app, profiles, and temporary job data.
- Windows Server 2022/2025 launch template using an approved current AMI.
- One-instance Auto Scaling Group or a managed single instance. Prefer an ASG
  only after replacement/bootstrap has been proven to restore the interactive
  worker correctly.
- Optional Elastic IP association when a stable source IP is required.
- Systems Manager State Manager association/bootstrap document.
- S3 artifact location or version manifest for Windows worker releases.
- AWS Backup/DLM policy only if preserving the encrypted profile volume is
  required; otherwise restore sanitized storage state from S3.

All resources must inherit the repository's provider `default_tags`, plus a
resource-specific `Name` tag where that is the existing pattern.

Network recommendation:

- If the goal is to test a different egress identity from the current Fargate
  worker, do not send the Windows instance through the same NAT Gateway.
- A dedicated EIP provides stability and independent allowlisting, but it is
  still an AWS-datacenter address and does not itself solve WAF fingerprinting.
- Place the instance in a tightly controlled public subnet only if required for
  its own public/EIP egress. Keep all inbound rules empty and administer through
  SSM.
- If manual Windows testing shows a site fails from the same instance even in
  ordinary Chrome, automation on that instance will not solve it. Record the
  domain as Windows-blocked and stop retries.

## Minimum EC2 IAM permissions

Create a distinct least-privilege role; do not copy the whole ECS task role.

- Windows queue: `ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`,
  and `GetQueueAttributes`.
- Browser jobs table: `GetItem`, `PutItem`, `UpdateItem`, and the specific query
  operations used by the worker.
- Runs table: `GetItem` and `UpdateItem` for idempotent parent-run reconciliation.
- Provenance table: `GetItem`, `PutItem`, and `UpdateItem` as required by the
  current schema.
- Reports bucket: scoped `GetObject`, `PutObject`, and `HeadObject` access to
  report/metadata keys. Avoid delete permission unless an existing rollback path
  proves it is required.
- Browser-state bucket/prefix: scoped read/write/delete for sanitized state only.
- Bedrock: `bedrock:InvokeModel`/stream permission only for configured planner
  and verifier inference profiles/models.
- CloudWatch Logs permissions for the dedicated log group.
- KMS decrypt/encrypt/data-key permissions only if customer-managed KMS keys are
  used by S3/SQS/EBS.
- `AmazonSSMManagedInstanceCore` for administration and deployment.

The GitHub deployment role will also need permission to upload the Windows
artifact and invoke the approved SSM deployment document if the existing
`coanalyst-ecs-deploy.yml` is extended to deploy Windows code.

## Application changes to implement

Recommended structure:

```text
co-analyst-application/app/backend/
  browser_worker_core.py       shared validation, navigation, storage, provenance
  browser_worker.py            existing Linux/ECS entry point
  windows_browser_worker.py    Windows queue and persistent Chrome entry point
```

Refactor rather than copy the existing worker. Shared behavior must include URL
safety, LLM planner/verifier prompts, document validation, S3 key creation,
metadata sidecars, DynamoDB provenance, parent-run reconciliation, and status
semantics. Platform-specific code should be limited to Chrome launch/profile
paths, interactive-session health, download directories, and Windows telemetry.

Update `app.py` to:

- Recognize Windows-eligible terminal results from the ECS worker.
- Enqueue the separate Windows job once using a conditional DynamoDB update.
- Expose `windows_browser_pending`, `windows_browser_running`,
  `windows_browser_downloaded`, `manual_intervention_required`, and terminal
  Windows failure in the existing job/run APIs.
- Avoid marking the parent company run fully terminal while an eligible Windows
  job remains pending, subject to a bounded maximum wait/status policy.
- Continue allowing annual-coverage processing once the annual report is stored,
  regardless of whether other Windows jobs remain pending.

Add a domain-capability record, either in the browser jobs table with a distinct
key prefix or in a small dedicated table:

```json
{
  "domain": "example.com",
  "ecs_status": "blocked",
  "windows_status": "reachable",
  "last_checked_at": "...",
  "failure_kind": "none",
  "expires_at": 0
}
```

This lets the system learn routing without hard-coded company rules. Capability
records should expire so a temporary WAF change does not become permanent.

## Deployment approach

The existing GitHub workflow deploys the Linux application/ECS/Terraform. The
Windows component needs an additional optional deployment job; `deploy.sh` is
not required.

Recommended pipeline sequence:

1. Test shared browser modules on Linux and Windows-compatible unit fixtures.
2. Build a versioned Windows worker ZIP containing Python source and a locked
   dependency manifest. Do not package Chrome or secrets.
3. Upload the artifact and checksum to a deployment S3 prefix.
4. Apply Terraform so queue, IAM, log group, instance profile, and instance exist.
5. Use SSM Run Command/State Manager to download and verify the artifact, install
   dependencies in a versioned virtual environment, switch the `current`
   junction, and restart the scheduled worker task.
6. Require a worker heartbeat with artifact version, Chrome version, instance ID,
   and interactive-session status before marking the deployment successful.
7. Roll back by switching the `current` junction to the prior artifact.

Keep `enable_windows_browser_worker=false` as the default until the manual URL
matrix and a one-company canary pass.

## Observability and failure states

Every job should log structured events containing `run_id`, `job_id`, company,
class, domain, current URL, action number, result, and elapsed time. Never log
cookies, local storage, authorization headers, full page HTML, or sensitive query
strings.

Distinct terminal reasons are important:

- `windows_source_waf_blocked`
- `windows_origin_unreachable`
- `windows_captcha_or_login_required`
- `windows_navigation_budget_exhausted`
- `windows_download_control_not_found`
- `viewer_document_not_captured`
- `windows_candidate_verification_failed`
- `windows_upload_confirmation_failed`
- `windows_worker_unhealthy`

CloudWatch alarms should cover:

- Oldest Windows queue message above 20 minutes.
- DLQ count above zero.
- Missing worker heartbeat for 10 minutes.
- Disk free space below 15%.
- No interactive desktop/Chrome launch failures.
- Repeated WAF failures for the same domain.

## Cost and operational boundaries

An always-running Windows instance has materially higher fixed cost than the
current Fargate fallback because Windows licensing and EC2 run continuously. It
also needs patching, Chrome updates, disk cleanup, and heartbeat monitoring.

Control cost and complexity by:

- Starting with one small/medium current-generation instance sized after a real
  canary; browser plus screenshots often needs at least 8 GiB RAM.
- Processing serially.
- Using the Windows route only after ECS failure.
- Capping queued jobs per company/domain.
- Stopping retries when manual Chrome also fails.
- Reviewing 7/14-day success metrics before deciding whether always-on operation
  is justified.

A dedicated residential proxy or CAPTCHA-solving service is not part of this
plan. Those introduce legal, contractual, security, and data-governance concerns
and should not be added as an automatic bypass.

## Rollout and acceptance criteria

Phase 0 — manual matrix:

- Test at least 10 URLs across Fortis, Capital One, and Fujimi.
- Record direct open, challenge type, whether a landing-page click is required,
  final URL/domain, whether Chrome downloads bytes, and downloaded MIME/filename.
- Repeat once after a fresh Chrome restart and once with the retained profile.

Phase 1 — isolated canary:

- Create queue/IAM/Windows worker with storage disabled or a canary S3 prefix.
- Replay five known failures, including one annual-report Download button.
- Compare ECS versus Windows outcomes and timings.

Phase 2 — verified writes:

- Enable existing verification and production S3/provenance writes for one
  company.
- Confirm checksum, metadata, idempotency, run reconciliation, and temp cleanup.
- Confirm a wrong-class document is rejected even when the click succeeds.

Phase 3 — controlled production:

- Enable automatic escalation for domains recorded as Windows-reachable.
- Monitor for at least seven days before widening admission.

Acceptance targets:

- At least 60% of URLs that fail ECS but open/download manually on Windows are
  recovered automatically in the canary.
- Zero cross-company or wrong-class documents are stored.
- No duplicate parent-run results on SQS redelivery.
- Temporary files are removed only after verified S3 upload.
- Browser profiles survive worker restart but remain isolated by domain.
- CAPTCHA/login pages fail closed.

## Complete required manual/POC URL matrix

These URLs came from `csa-browser-diagnostics.HH3Xse.tar.gz`. They are intended
to test navigation and download behavior, not to assert that every requested
standalone report exists. Test every numbered URL in Phase A and Phase B. Landing
pages and their direct-document counterparts are intentionally both present: the
comparison reveals whether cookies, referer, JavaScript, or a button-generated
token is required.

### Fortis Healthcare Limited

1. Company homepage/session baseline:
   `https://www.fortishealthcare.com/`
2. Annual Reports landing page — navigate into the newest year and click the
   visible Annual Report Download/View button:
   `https://www.fortishealthcare.com/investors/annual-reports/476`
3. Historical Annual Report landing page — verify whether the click behavior is
   the same as the newest archive:
   `https://www.fortishealthcare.com/investor/annual%20reports/fhl%20annual%20report%202020-21`
4. Code of Conduct landing page — click the visible document preview/download:
   `https://www.fortishealthcare.com/investor/policies%20&%20code/code%20of%20conduct`
5. Workbench Code of Conduct landing page — compare the alternate host, session,
   redirects, and Download control:
   `https://workbench.fortishealthcare.com/investor/policies%20&%20code/code%20of%20conduct`
6. Whistle Blower Policy landing page — click its policy download:
   `https://www.fortishealthcare.com/investor/policies%20&%20code/whistle%20blower%20policy`
7. Whistle Blower direct-PDF control case:
   `https://www.fortishealthcare.com/drupal-data/2023-12/Whistle%20Blower%20Policy.pdf`
8. Insider Trading direct-PDF control case:
   `https://www.fortishealthcare.com/drupal-data/2025-05/Policy%20for%20Prevention%20of%20Insider%20Trading.pdf`
9. Prevention of Sexual Harassment direct-PDF control case:
   `https://www.fortishealthcare.com/drupal-data/2024-06/Policy%20for%20Prevention,%20Prohibition%20&%20Redressal%20of%20Sexual%20Harassment.pdf`
10. Anti-Bribery direct-PDF control case:
    `https://www.fortishealthcare.com/drupal-data/2024-06/Anti-Bribery%20and%20Corruption%20Policy.pdf`
11. Sustainability/BRSR dynamic page:
    `https://www.fortishealthcare.com/sustainability/bsr/BRSR%20Report%20for%20FY%202024-25`
12. Environmental and Social Review direct-PDF control case:
    `https://www.fortishealthcare.com/drupal-data/investors/Environmental_Social_Review_Summary.pdf`

### Capital One Financial Corporation

1. Annual Reports landing page — open the newest year and click the report
   download/view control:
   `https://investor.capitalone.com/financial-results/annual-reports`
2. Alternate Annual Report page — test whether the archive renders and whether
   `#default-id` selects the current item:
   `https://www.capitalone.com/investor/financials/annual-report/#default-id`
3. Investor document HTML node — test redirect, viewer, and visible download
   behavior:
   `https://investor.capitalone.com/node/56191/html`
4. Environmental landing page — test dynamic links to climate/ESG material:
   `https://www.capitalone.com/about/environment/`
5. Wolfsberg landing page — test whether its Download control produces the
   expected PDF in the same browser session:
   `https://www.capitalone.com/digital/wolfsberg-questionnaire/`
6. Direct PDF control case — compare ordinary navigation with opening through
   its first-party landing/referrer context:
   `https://ecm.capitalone.com/WCM/digital/pdfs/wolfsberg_2025.pdf`
7. Direct third-party Code PDF — test first-party CDN acceptance:
   `https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf`

### Fujimi Incorporated

1. Investor library — navigate to the Annual Report section and click the newest
   report rather than copying a generated PDF URL:
   `https://www.fujimiinc.co.jp/english/ir/library/index.html`
2. Investor relations landing page — test archive navigation and language links:
   `https://www.fujimiinc.co.jp/english/ir/index.html`
3. Environmental Policy page — valid official HTML content that may need verified
   HTML-to-PDF capture for the policy class:
   `https://www.fujimiinc.co.jp/english/csr/environment/policy.html`
4. Code of Ethics page — test whether related document controls are added after
   JavaScript rendering:
   `https://www.fujimiinc.co.jp/english/csr/ethics.html`
5. CSR/ESG landing page — test navigation to reports and policy content:
   `https://www.fujimiinc.co.jp/english/csr/index.html`
6. Annual Report PDF found by the agent — use this only as a direct-open baseline
   after first visiting the IR library:
   `https://www.ircms.jp/irexport/fujimiinc/file/a82238391744634.pdf`
7. Proxy/meeting document PDF found by the agent — second CDN direct-open
   baseline:
   `https://www.ircms.jp/irexport/fujimiinc/file/a83563292491687.pdf`

For every URL, record:

```text
Company:
URL:
Opened in Windows Chrome: yes/no
Required cookie acceptance: yes/no
Required click(s), exact visible labels:
Challenge/error shown:
Final page URL:
Download started: yes/no
Downloaded filename and extension:
Was the document visible in a built-in PDF viewer: yes/no
Did direct URL fail but landing-page click work: yes/no
```

The most valuable result is the exact annual-report click sequence: landing URL,
button/element label, whether it opened a popup or viewer, and the final downloaded
filename/URL. That trace should become a generic planner pattern, not a hard-coded
company selector.

## Information still needed before implementation

- Which three of the original five Fortis URLs opened and which two failed.
- The exact annual-report landing URL and visible Download button text you used.
- Whether the Download click emitted a normal file, opened Chrome's PDF viewer,
  or opened a new tab.
- Whether the page worked immediately in a fresh Chrome profile or only after
  cookies/session were established.
- Windows version, instance type, subnet/public-IP path, and whether an EIP is
  attached.
- Whether the instance may be rebuilt by Terraform or must be imported/adopted.
- Whether organization policy permits a dedicated interactive auto-logon user.

These answers affect bootstrap and routing, but the manual URL matrix can proceed
before any infrastructure or application code is changed.

## Copy/paste handoff for a new Codex task

```text
Read this entire file before taking action:
/Users/abhay/Documents/csa-project/agents/download-agent/windows-ec2-browser-fallback-context.md

Build and test only the Independent personal-AWS proof-of-concept described in
that file. Use my named personal AWS profile and verify the AWS account ID before
every deploy/destructive operation. Do not access or modify production AWS
resources, production Terraform state, production queues/tables/buckets, or the
deployed Report IQ application. Keep the POC under
/Users/abhay/Documents/csa-project/co-analyst-application/windows-browser-poc.

First inspect the existing Windows EC2 instance and repository without changing
them. Then create a plan. Prefer adopting the supplied instance through SSM; do
not replace, stop, terminate, or import it without my explicit approval. Build
the isolated Terraform and Windows Playwright test harness, apply only after
showing account/resource safety checks, and test every URL in the Complete
required manual/POC URL matrix. Test annual-report landing-page button clicks,
native downloads, viewer/popup/network-response capture, persistent per-domain
cookies, POC S3 upload confirmation, and safe local cleanup. Do not use a Google
login or CAPTCHA bypass. Return the combined result matrix, screenshots/logs,
cost estimate, teardown commands, and a go/no-go recommendation for production
integration.
```
