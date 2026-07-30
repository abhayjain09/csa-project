#!/usr/bin/env bash
# Read-only AWS quota/usage audit for the co-analyst-application + download-agent
# stack. Safe to run repeatedly — no writes, no mutations, just describes/lists.
#
# Requires: AWS CLI v2 configured (aws configure / SSO / profile) with a role
# that can at least read service-quotas, lambda, and cloudwatch. Missing
# permissions on any one section will print an error for that section only
# and the script will keep going — nothing here uses `set -e`.
#
# Usage:
#   chmod +x check_quotas.sh
#   ./check_quotas.sh > quota_report.txt 2>&1
# then share quota_report.txt back.

REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION="edo-coanalyst-report-vertex-search"

hr() { printf '\n============================================================\n%s\n============================================================\n' "$1"; }

# Portable "1 hour ago" for both GNU date (Linux) and BSD date (macOS)
one_hour_ago() {
  date -u -v-1H +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S
}
now_utc() { date -u +%Y-%m-%dT%H:%M:%S; }

hr "0. Identity / region sanity check"
aws sts get-caller-identity --output table
echo "Using region: $REGION"

hr "1. Discover the exact AgentCore service code"
aws service-quotas list-services --region "$REGION" \
  --query "Services[?contains(ServiceName, 'AgentCore') || contains(ServiceName, 'Bedrock')]" \
  --output table

# Try the most likely code; if this is wrong, section 2 will just come back empty
# and step 1's output above will show the real one to substitute.
AGENTCORE_CODE="bedrock-agentcore"

hr "2. ALL AgentCore quotas (Gateway + Runtime + Browser) — customized values"
aws service-quotas list-service-quotas --region "$REGION" \
  --service-code "$AGENTCORE_CODE" \
  --query "Quotas[].{Name:QuotaName,Code:QuotaCode,Value:Value,Adjustable:Adjustable,Unit:Unit}" \
  --output table

hr "2b. ALL AgentCore quotas — AWS defaults (in case none were customized above)"
aws service-quotas list-aws-default-service-quotas --region "$REGION" \
  --service-code "$AGENTCORE_CODE" \
  --query "Quotas[].{Name:QuotaName,Code:QuotaCode,Value:Value,Adjustable:Adjustable}" \
  --output table

hr "3. Filtered: Web Search Tool rate (Gateway)"
aws service-quotas list-service-quotas --region "$REGION" --service-code "$AGENTCORE_CODE" \
  --query "Quotas[?contains(QuotaName, 'Web Search')]" --output table
aws service-quotas list-aws-default-service-quotas --region "$REGION" --service-code "$AGENTCORE_CODE" \
  --query "Quotas[?contains(QuotaName, 'Web Search')]" --output table

hr "4. Filtered: AgentCore Runtime active sessions"
aws service-quotas list-service-quotas --region "$REGION" --service-code "$AGENTCORE_CODE" \
  --query "Quotas[?contains(QuotaName, 'Active session')]" --output table
aws service-quotas list-aws-default-service-quotas --region "$REGION" --service-code "$AGENTCORE_CODE" \
  --query "Quotas[?contains(QuotaName, 'Active session')]" --output table

hr "5. Filtered: AgentCore Browser quotas"
aws service-quotas list-service-quotas --region "$REGION" --service-code "$AGENTCORE_CODE" \
  --query "Quotas[?contains(QuotaName, 'Browser')]" --output table
aws service-quotas list-aws-default-service-quotas --region "$REGION" --service-code "$AGENTCORE_CODE" \
  --query "Quotas[?contains(QuotaName, 'Browser')]" --output table

hr "6. Lambda — account-wide concurrency settings"
aws lambda get-account-settings --region "$REGION" \
  --query "AccountLimit.{TotalConcurrency:ConcurrentExecutions,UnreservedConcurrency:UnreservedConcurrentExecutions}" \
  --output table

hr "6b. Lambda — via Service Quotas (should match 6 above)"
aws service-quotas get-service-quota --region "$REGION" \
  --service-code lambda --quota-code L-B99A9384 \
  --query "Quota.{Name:QuotaName,Value:Value}" --output table

hr "6c. Lambda — this specific function's reserved concurrency (expect: none set)"
aws lambda get-function-concurrency --region "$REGION" \
  --function-name "$LAMBDA_FUNCTION" --output table

hr "7. Bedrock — on-demand model invocation quotas (Claude/Anthropic)"
aws service-quotas list-service-quotas --region "$REGION" --service-code bedrock \
  --query "Quotas[?contains(QuotaName, 'Anthropic') || contains(QuotaName, 'Claude')]" --output table
aws service-quotas list-aws-default-service-quotas --region "$REGION" --service-code bedrock \
  --query "Quotas[?contains(QuotaName, 'Anthropic') || contains(QuotaName, 'Claude')]" --output table

hr "8. Fargate — on-demand vCPU quota"
aws service-quotas list-service-quotas --region "$REGION" --service-code fargate \
  --query "Quotas[?contains(QuotaName, 'On-Demand')]" --output table
aws service-quotas list-aws-default-service-quotas --region "$REGION" --service-code fargate \
  --query "Quotas[?contains(QuotaName, 'On-Demand')]" --output table

hr "9. Recent usage — Lambda concurrent executions (last hour, max)"
aws cloudwatch get-metric-statistics --region "$REGION" \
  --namespace AWS/Lambda --metric-name ConcurrentExecutions \
  --dimensions Name=FunctionName,Value="$LAMBDA_FUNCTION" \
  --start-time "$(one_hour_ago)" --end-time "$(now_utc)" \
  --period 60 --statistics Maximum --output table

hr "10. Recent usage — Lambda throttles (last hour, sum)"
aws cloudwatch get-metric-statistics --region "$REGION" \
  --namespace AWS/Lambda --metric-name Throttles \
  --dimensions Name=FunctionName,Value="$LAMBDA_FUNCTION" \
  --start-time "$(one_hour_ago)" --end-time "$(now_utc)" \
  --period 60 --statistics Sum --output table

hr "11. Recent usage — Bedrock invocation throttles (last hour, sum)"
aws cloudwatch get-metric-statistics --region "$REGION" \
  --namespace AWS/Bedrock --metric-name InvocationThrottles \
  --start-time "$(one_hour_ago)" --end-time "$(now_utc)" \
  --period 60 --statistics Sum --output table

hr "DONE — copy everything above and share it back"