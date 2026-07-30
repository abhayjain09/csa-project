# AWS Service Quota increase — AgentCore Gateway Web Search Tool rate

**Status:** Request submitted 2026-07-29. Awaiting AWS approval.

## What was requested

| Field | Value |
|---|---|
| Service | Amazon Bedrock AgentCore (`bedrock-agentcore`) |
| Quota name | Rate of Web Search Tool queries |
| Quota code | `L-84A99A88` |
| Region | us-east-1 |
| Account | 610639371721 |
| Current value | 10 transactions/second |
| Requested value | 100 transactions/second |

## Why this quota, and why 100

This is the AgentCore Gateway tool (`GATEWAY_SEARCH_TOOL=web-search-tool___WebSearch`, `agents/download-agent/locals.tf`) the download agent falls back to (`_gateway_search_async`, `agent.py`) whenever its primary Vertex AI search path returns nothing — which real production logs showed happening on nearly every query variant for some companies. It was identified as the single confirmed AWS-side bottleneck for running multiple companies in parallel: every other quota checked (Lambda concurrent executions, Bedrock model TPM/RPM, Fargate on-demand vCPU, AgentCore Runtime active sessions, AgentCore Browser sessions) already has generous headroom relative to this workload's realistic scale — see `quota_report.txt` in this directory for the full `check_quotas.sh` output this was based on.

**The math:** the code's own gateway throttle (`SEARCH_MIN_INTERVAL=1.5s`, a `threading.Lock` in `agent.py`) is scoped *per AgentCore invocation*, not coordinated across separate concurrent invocations. So each invocation independently paces itself at ~0.67 requests/second, but nothing prevents the *aggregate* rate across many concurrent invocations from exceeding the account-wide 10 TPS ceiling. At the target scale below, aggregate demand was projected at 50-80 TPS — already over the original 10 TPS quota at as few as ~2 companies running simultaneously.

## Target scale this supports

Planned config change (not yet applied — see `co-analyst-application/app/backend/app.py`):
- `AGENT_CHUNK_CONCURRENCY`: 3 → **10** (queries per company processed in parallel)
- `BULK_COMPANY_CONCURRENCY`: 4 → target **~8-12** (companies processed in parallel)

With `AGENT_CHUNK_CONCURRENCY=10` and a 100 TPS Gateway quota (80 TPS safe target, ~20% margin):
- Max concurrent AgentCore invocations ≈ 80 ÷ 0.67 ≈ 119
- Max companies ≈ 119 ÷ 10 ≈ **11-12**, bounded by Gateway TPS alone.

## Important: a second constraint also binds at this scale — not yet requested

At `AGENT_CHUNK_CONCURRENCY=10`, **Lambda concurrent executions** (account-wide limit, currently 1,000 per `quota_report.txt`) becomes binding *before* the Gateway quota does, because each invocation's search phase fans out to `SEARCH_FANOUT_WORKERS=8` concurrent Vertex Lambda (`edo-coanalyst-report-vertex-search`) calls:

- Concurrent Lambda invokes = companies × 10 (chunk concurrency) × 8 (fanout)
- At a safe 70% of 1,000 = 700: max concurrent invocations = 700 ÷ 8 = 87.5 → **max ~8-9 companies**, not 11-12.

**To actually reach ~11-12 companies at `AGENT_CHUNK_CONCURRENCY=10`, this Lambda constraint needs addressing too** — either raise the account-wide Lambda concurrent-executions quota, or set a dedicated `reserved_concurrent_executions` on `edo-coanalyst-report-vertex-search` (`agents/download-agent/lambda.tf:157-159`, currently commented out) once the target is confirmed. Not yet requested/applied — flagging here so it isn't lost.

Also unconfirmed: whether other, unrelated Lambda functions elsewhere in this AWS account draw from the same 1,000-execution pool, which would reduce real available headroom below what the raw number suggests.

## Justification text submitted with the request

> We operate a Bedrock AgentCore-based document discovery agent that searches for and retrieves specific compliance/ESG documents (annual reports, codes of conduct, sustainability reports, policy documents, etc.) for a portfolio of companies. Each company requires up to 23 distinct document searches, and we are scaling from processing companies sequentially to running multiple companies concurrently to meet business throughput requirements.
>
> Our agent's primary search path uses a Vertex AI-backed search Lambda; when that path returns no result for a given query (which occurs frequently — often several times per company), it falls back to the AgentCore Gateway's Web Search Tool. At our target concurrency — running approximately 8-12 companies in parallel, each with up to 10 concurrent document searches in flight — the aggregate demand on this fallback search path is projected to reach 50-80 requests/second at peak, which already exceeds the default 10 TPS quota today at even modest concurrency (as few as 2 companies running simultaneously).
>
> We are requesting an increase to 100 TPS to provide adequate headroom (roughly 20-25% utilization at our target steady-state load) to avoid throttling-induced failures, which in our system manifest as documents being reported as "not found" when they do exist — a direct data-quality/completeness impact, not just a latency one.
>
> This is a planned, deliberate scaling change to our production workload (increasing our internal concurrency configuration from 3 to 10 concurrent document searches per company, and from ~4 to ~10 companies processed in parallel) — not anomalous or runaway traffic.

## Related quota data (point-in-time snapshot, 2026-07-29)

Full raw output saved in `quota_report.txt` (same directory). Key figures pulled from it:
- Lambda account concurrent-execution limit: 1,000 (usage observed peaking at 9 in the hour before the request — reflects current low-concurrency operation, not target scale)
- Fargate On-Demand vCPU: 4,000 (already raised well above the AWS default of 6 — no action needed)
- AgentCore Active Session Workloads: 5,000 (default, un-customized — no action needed)
- AgentCore Browser concurrent sessions: 1,000 / session-start rate 30 TPS (no action needed)
- Bedrock Claude Sonnet 5 / Haiku 4.5 token-per-minute and request-per-minute quotas: all far above realistic call volume for this workload (no action needed)

## Next steps

1. Wait for AWS approval on `L-84A99A88` (100 TPS).
2. Decide target `BULK_COMPANY_CONCURRENCY` once the Lambda-concurrency constraint above is resolved (either raise the account Lambda quota, or reserve concurrency on the Vertex search function).
3. Apply the `AGENT_CHUNK_CONCURRENCY`/`BULK_COMPANY_CONCURRENCY` config changes in `co-analyst-application/app/backend/app.py` only after the quota is confirmed approved — raising concurrency before approval would reproduce the exact throttling this request is meant to prevent.
4. Re-run `check_quotas.sh` after approval to confirm the new value is live before scaling up.
