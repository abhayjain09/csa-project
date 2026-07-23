#!/usr/bin/env python3
"""
invoke_local.py — Fire a single invocation at the live AgentCore runtime.

Useful for smoke-testing after a deploy without running the full indexer.

Usage
-----
    # Using --bucket and --s3-key directly (simplest)
    python scripts/invoke_local.py --arn <runtime_arn> --bucket my-bucket --s3-key paccar/report.pdf

    # Using a payload file
    python scripts/invoke_local.py --arn <runtime_arn> --payload scripts/payload.example.json

    # Using inline JSON
    python scripts/invoke_local.py --arn <runtime_arn> --json '{"bucket":"my-bucket","s3_key":"paccar/report.pdf"}'
"""

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

def main():
    parser = argparse.ArgumentParser(
        description="Invoke the PageIndex AgentCore runtime with a single payload.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--arn",
        default=os.environ.get("AGENTCORE_RUNTIME_ARN"),
        help="AgentCore runtime ARN (or set AGENTCORE_RUNTIME_ARN env var)",
    )
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")

    # Three ways to provide the payload — pick one
    payload_group = parser.add_mutually_exclusive_group()
    payload_group.add_argument("--payload", help="Path to a JSON payload file")
    payload_group.add_argument("--json", help="Inline JSON payload string")
    payload_group.add_argument("--bucket", help="S3 bucket (use with --s3-key)")

    parser.add_argument("--s3-key", help="S3 key of the PDF (use with --bucket)")
    parser.add_argument("--label", help="Optional display label")

    args = parser.parse_args()

    # ── Validate ARN ──────────────────────────────────────────────────────────
    if not args.arn:
        print("ERROR: --arn is required (or set AGENTCORE_RUNTIME_ARN env var)", file=sys.stderr)
        sys.exit(1)

    # ── Build payload ─────────────────────────────────────────────────────────
    if args.payload:
        with open(args.payload, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    elif args.json:
        payload = json.loads(args.json)
    elif args.bucket:
        if not args.s3_key:
            print("ERROR: --s3-key is required when using --bucket", file=sys.stderr)
            sys.exit(1)
        payload = {"bucket": args.bucket, "s3_key": args.s3_key}
        if args.label:
            payload["label"] = args.label
    else:
        print("ERROR: provide one of --payload, --json, or --bucket/--s3-key", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    print("Runtime : {}".format(args.arn))
    print("Region  : {}".format(args.region))
    print("Payload : {}".format(json.dumps(payload)))
    print()

    # ── Invoke ────────────────────────────────────────────────────────────────
    client = boto3.client(
        "bedrock-agentcore",
        region_name=args.region,
        config=Config(
            read_timeout=900,
            connect_timeout=10,
            retries={"max_attempts": 0},
        )
    )
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=args.arn,
            payload=json.dumps(payload),
        )
    except ClientError as exc:
        print("ERROR: invocation failed: {}".format(exc), file=sys.stderr)
        sys.exit(1)

    raw_response = response["response"].read()
    result = json.loads(raw_response)

    # ── Print result ──────────────────────────────────────────────────────────
    status = result.get("status", "unknown")
    print("Status  : {}".format(status))
    print()

    if status == "ok":
        index = result.get("index", {})
        print("doc_name          : {}".format(index.get("doc_name", "—")))
        print("top-level sections: {}".format(len(index.get("structure", []))))
        print()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("ERROR from runtime: {}".format(result.get("error", "no error message")), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
invoke_local.py — Fire a single invocation at the live AgentCore runtime.

Useful for smoke-testing after a deploy without running the full indexer.

Usage
-----
    # Using --bucket and --s3-key directly (simplest)
    python scripts/invoke_local.py --arn <runtime_arn> --bucket my-bucket --s3-key paccar/report.pdf

    # Using a payload file
    python scripts/invoke_local.py --arn <runtime_arn> --payload scripts/payload.example.json

    # Using inline JSON
    python scripts/invoke_local.py --arn <runtime_arn> --json '{"bucket":"my-bucket","s3_key":"paccar/report.pdf"}'
"""

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError


def main():
    parser = argparse.ArgumentParser(
        description="Invoke the PageIndex AgentCore runtime with a single payload.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--arn",
        default=os.environ.get("AGENTCORE_RUNTIME_ARN"),
        help="AgentCore runtime ARN (or set AGENTCORE_RUNTIME_ARN env var)",
    )
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")

    # Three ways to provide the payload — pick one
    payload_group = parser.add_mutually_exclusive_group()
    payload_group.add_argument("--payload", help="Path to a JSON payload file")
    payload_group.add_argument("--json", help="Inline JSON payload string")
    payload_group.add_argument("--bucket", help="S3 bucket (use with --s3-key)")

    parser.add_argument("--s3-key", help="S3 key of the PDF (use with --bucket)")
    parser.add_argument("--label", help="Optional display label")

    args = parser.parse_args()

    # ── Validate ARN ──────────────────────────────────────────────────────────
    if not args.arn:
        print("ERROR: --arn is required (or set AGENTCORE_RUNTIME_ARN env var)", file=sys.stderr)
        sys.exit(1)

    # ── Build payload ─────────────────────────────────────────────────────────
    if args.payload:
        with open(args.payload, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    elif args.json:
        payload = json.loads(args.json)
    elif args.bucket:
        if not args.s3_key:
            print("ERROR: --s3-key is required when using --bucket", file=sys.stderr)
            sys.exit(1)
        payload = {"bucket": args.bucket, "s3_key": args.s3_key}
        if args.label:
            payload["label"] = args.label
    else:
        print("ERROR: provide one of --payload, --json, or --bucket/--s3-key", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    print("Runtime : {}".format(args.arn))
    print("Region  : {}".format(args.region))
    print("Payload : {}".format(json.dumps(payload)))
    print()

    # ── Invoke ────────────────────────────────────────────────────────────────
    client = boto3.client("bedrock-agentcore", region_name=args.region)
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=args.arn,
            payload=json.dumps(payload),
        )
    except ClientError as exc:
        print("ERROR: invocation failed: {}".format(exc), file=sys.stderr)
        sys.exit(1)

    raw_response = response["response"].read()
    result = json.loads(raw_response)

    # ── Print result ──────────────────────────────────────────────────────────
    status = result.get("status", "unknown")
    print("Status  : {}".format(status))
    print()

    if status == "ok":
        index = result.get("index", {})
        print("doc_name          : {}".format(index.get("doc_name", "—")))
        print("top-level sections: {}".format(len(index.get("structure", []))))
        print()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("ERROR from runtime: {}".format(result.get("error", "no error message")), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

