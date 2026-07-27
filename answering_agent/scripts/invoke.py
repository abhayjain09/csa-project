#!/usr/bin/env python3
"""
invoke.py — Invoke the deployed PageIndex ReAct agent and print the result.

Usage:
    python3 scripts/invoke.py \\
        --endpoint-arn  <runtime_endpoint_arn> \\
        --payload       scripts/sample_payload.json \\
        --region        us-east-1 \\
        [--session-id   my-session-001]   # omit to let AgentCore assign one

The endpoint ARN comes from:
    terraform output -raw runtime_endpoint_arn   (from infra/)

Authentication:
    Uses whatever AWS credentials are active in the environment
    (IAM user, assumed role, EC2 instance profile, etc.).
    The caller needs bedrock-agentcore:InvokeAgentRuntime permission.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from botocore.config import Config


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Invoke the PageIndex ReAct agent.")
    p.add_argument(
        "--endpoint-arn", required=True,
        help="AgentCore runtime endpoint ARN (terraform output -raw runtime_endpoint_arn)",
    )
    p.add_argument(
        "--payload", required=True,
        help="Path to a JSON file containing the RuntimePayload.",
    )
    p.add_argument(
        "--region", default="us-east-1",
        help="AWS region (default: us-east-1)",
    )
    p.add_argument(
        "--session-id", default=None,
        help="Optional session ID. Omit to let AgentCore assign one.",
    )
    p.add_argument(
        "--output", choices=["pretty", "compact", "raw"], default="pretty",
        help="Output format (default: pretty)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    payload_path = Path(args.payload)
    if not payload_path.exists():
        print(f"ERROR: payload file not found: {payload_path}", file=sys.stderr)
        return 1

    try:
        payload_dict = json.loads(payload_path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: payload file is not valid JSON: {e}", file=sys.stderr)
        return 1

    try:
        import boto3
    except ImportError:
        print("ERROR: boto3 is not installed. Run: pip install boto3", file=sys.stderr)
        return 1

    client = boto3.client(
    "bedrock-agentcore",
    region_name=args.region,
    config=Config(
        read_timeout=1200,      # 15 minutes
        connect_timeout=10,
        retries={"max_attempts": 0}  # no retries on timeout — each run is long
    )
)

    invoke_kwargs: dict = {
        "agentRuntimeArn": args.endpoint_arn,
        "payload": json.dumps(payload_dict).encode("utf-8"),
    }
    if args.session_id:
        invoke_kwargs["runtimeSessionId"] = args.session_id

    print(f"Invoking endpoint: {args.endpoint_arn}")
    print(f"Session ID: {args.session_id or '(AgentCore-assigned)'}")
    print(f"Questions : {len(payload_dict.get('question_set', []))}")
    print()

    try:
        response = client.invoke_agent_runtime(**invoke_kwargs)
    except client.exceptions.ClientError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Print the session ID AgentCore assigned (useful for log lookups).
    assigned_session = response.get("runtimeSessionId", "")
    if assigned_session:
        print(f"Assigned session ID: {assigned_session}")

    raw_body = response["response"].read()
    try:
        result = json.loads(raw_body)
    except json.JSONDecodeError:
        print("Response (raw):")
        print(raw_body.decode("utf-8", errors="replace"))
        return 0

    if args.output == "pretty":
        print(json.dumps(result, indent=2))
    elif args.output == "compact":
        print(json.dumps(result))
    else:
        print(raw_body.decode("utf-8"))

    # Exit code: 1 if the response itself contains an error status.
    if isinstance(result, dict) and result.get("status") == "error":
        print(f"\nAgent returned error: {result.get('error_type')} — {result.get('message')}",
              file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

