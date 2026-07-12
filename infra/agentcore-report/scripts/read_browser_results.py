#!/usr/bin/env python3
"""Read completed Fargate browser-worker results from SQS.

Usage:
  python scripts/read_browser_results.py <results_queue_url> [--region=us-east-1] [--delete]
"""

import json
import sys

import boto3


def main() -> None:
    args = sys.argv[1:]
    queue_url = next((arg for arg in args if not arg.startswith("--")), "")
    region = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--region=")), "us-east-1")
    delete = "--delete" in args
    if not queue_url:
        print(__doc__)
        raise SystemExit(1)
    sqs = boto3.client("sqs", region_name=region)
    response = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=10,
        VisibilityTimeout=30,
    )
    messages = response.get("Messages", [])
    if not messages:
        print("No browser-worker results available.")
        return
    for message in messages:
        print(json.dumps(json.loads(message["Body"]), indent=2))
        if delete:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])


if __name__ == "__main__":
    main()
