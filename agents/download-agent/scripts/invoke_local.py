#!/usr/bin/env python3
"""Invoke the AgentCore runtime from your laptop and print the S3 keys.

Usage:
  python scripts/invoke_local.py <agent_runtime_arn> [payload.json] [--region us-east-1]

Reads payload.example.json by default. Requires: pip install boto3, and AWS
credentials with bedrock-agentcore:InvokeAgentRuntime on the runtime ARN.
"""
import json
import sys
import time

import boto3
from botocore.config import Config


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    region = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--region=")), "us-east-1")
    if not args:
        print(__doc__)
        sys.exit(1)
    arn = args[0]
    payload_path = args[1] if len(args) > 1 else "scripts/payload.example.json"
    with open(payload_path) as f:
        payload = f.read()

    # 300s read timeout — the agent may take 1-3 min for 6 queries with LLM checks.
    # connect_timeout stays short (10s) so a network issue fails fast.
    client = boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(connect_timeout=10, read_timeout=600),
    )
    print(f"Invoking {arn.split('/')[-1]} … (browser runs can take 3-6 min)", flush=True)
    t0 = time.time()
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        qualifier="DEFAULT",
        payload=payload.encode("utf-8"),
    )
    elapsed = time.time() - t0
    body = resp["response"].read() if hasattr(resp["response"], "read") else resp["response"]
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")

    # The runtime may return proper JSON or a Python repr string — handle both.
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        import ast
        try:
            data = ast.literal_eval(body)
        except Exception:
            print("Raw response:\n", body)
            sys.exit(1)
    print(f"Done in {elapsed:.1f}s\n")

    print(json.dumps(data, indent=2))
    print("\n--- S3 keys ---")
    for d in data.get("downloaded", []):
        print(d["s3_uri"], " <-", d["source_url"])
    if not data.get("downloaded"):
        print("(none downloaded)")
        diag = data.get("diagnostics", {})
        if diag:
            print(json.dumps(diag, indent=2))
        if data.get("failures"):
            print("  failures:", data["failures"])


if __name__ == "__main__":
    main()