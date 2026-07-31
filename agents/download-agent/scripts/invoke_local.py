#!/usr/bin/env python3
"""Invoke the AgentCore runtime from your laptop and print the S3 keys.

Usage:
  python scripts/invoke_local.py <agent_runtime_arn> [payload.json] [--region us-east-1]
  python scripts/invoke_local.py <agent_runtime_arn> --company="Microsoft" \
      [--domain=microsoft.com] [--workers=3] [--region=us-east-1]

Reads payload.example.json by default. Requires: pip install boto3, and AWS
credentials with bedrock-agentcore:InvokeAgentRuntime on the runtime ARN.
"""
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    region = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--region=")), "us-east-1")
    if not args:
        print(__doc__)
        sys.exit(1)
    arn = args[0]
    company = next((a.split("=", 1)[1] for a in sys.argv
                    if a.startswith("--company=")), "").strip()
    domain = next((a.split("=", 1)[1] for a in sys.argv
                   if a.startswith("--domain=")), "").strip()
    if not company:
        payload_path = args[1] if len(args) > 1 else "scripts/payload.example.json"
        with open(payload_path) as f:
            payload = f.read()

    # 600s read timeout — browser/LLM verification can take several minutes.
    # connect_timeout stays short (10s) so a network issue fails fast.
    client = boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(connect_timeout=10, read_timeout=600),
    )

    def invoke_one(payload_text):
        response = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            qualifier="DEFAULT",
            payload=payload_text.encode("utf-8"),
        )
        response_body = (response["response"].read()
                         if hasattr(response["response"], "read")
                         else response["response"])
        if isinstance(response_body, bytes):
            response_body = response_body.decode("utf-8", "ignore")
        try:
            return json.loads(response_body)
        except json.JSONDecodeError:
            import ast
            return ast.literal_eval(response_body)

    print(f"Invoking {arn.split('/')[-1]} …", flush=True)
    t0 = time.time()
    if company:
        # A single runtime processes reports sequentially and can hit its wall
        # clock limit on 23 browser-heavy classes. Fan out bounded one-class
        # jobs, just like the production portal, and merge their manifests.
        from pathlib import Path
        import importlib.util

        specs_path = (Path(__file__).resolve().parents[1]
                      / "agent" / "report_specs.py")
        spec = importlib.util.spec_from_file_location("report_specs", specs_path)
        report_specs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(report_specs)
        run_id = uuid.uuid4().hex[:8]
        workers = int(next((a.split("=", 1)[1] for a in sys.argv
                            if a.startswith("--workers=")), "3"))
        workers = max(1, min(workers, 6))
        jobs = []
        for index, report_class in enumerate(
                report_specs.ALL_REPORT_CLASSES, start=1):
            jobs.append((index, report_class, json.dumps({
                "company": {"name": company, "domain": domain},
                "run_id": run_id,
                "reports": [{
                    "report_class": report_class,
                    "request_id": f"all:{index:02d}",
                }],
                "browser_enabled": True,
                "document_preferences": {
                    "preferred_language": "en", "prefer_latest": True,
                },
            })))
        responses = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(invoke_one, payload_text): (index, report_class)
                for index, report_class, payload_text in jobs
            }
            for future in as_completed(futures):
                index, report_class = futures[future]
                try:
                    responses.append((index, future.result()))
                    print(f"[{len(responses):02d}/23] finished {report_class}",
                          flush=True)
                except Exception as exc:
                    responses.append((index, {
                        "stored": [], "duplicates": [],
                        "manifest": [{
                            "request_id": f"all:{index:02d}",
                            "report_class": report_class,
                            "status": "failed", "downloaded": False,
                            "reason": str(exc)[:500],
                        }],
                    }))
        responses.sort(key=lambda item: item[0])
        stored = [entry for _, response in responses
                  for entry in response.get("stored", [])]
        duplicates = [entry for _, response in responses
                      for entry in response.get("duplicates", [])]
        manifest = [entry for _, response in responses
                    for entry in response.get("manifest", [])]
        missing = [entry["report_class"] for entry in manifest
                   if not entry.get("downloaded")]
        data = {
            "run_id": run_id, "company": company,
            "stored": stored, "duplicates": duplicates,
            "manifest": manifest,
            "missing_report_classes": missing,
            "complete": len(manifest) == 23 and not missing,
            "counts": {
                "requested": 23,
                "complete": sum(bool(x.get("downloaded")) for x in manifest),
                "missing": len(missing),
                "stored": len(stored), "duplicates": len(duplicates),
            },
        }
    else:
        data = invoke_one(payload)
    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s\n")

    print(json.dumps(data, indent=2))
    print("\n--- S3 keys ---")
    downloaded = data.get("stored", []) + data.get("duplicates", [])
    for d in downloaded:
        print(d["s3_uri"], " <-", d["source_url"])
    if not downloaded:
        print("(none downloaded)")
        diag = data.get("diagnostics", {})
        if diag:
            print(json.dumps(diag, indent=2))
        if data.get("failures"):
            print("  failures:", data["failures"])
    if data.get("manifest"):
        counts = data.get("counts", {})
        print(f"\n--- Completeness: {counts.get('complete', 0)}/"
              f"{counts.get('requested', len(data['manifest']))} ---")
        for item in data["manifest"]:
            mark = "OK" if item.get("downloaded") else "MISSING"
            print(f"{mark:7} {item.get('report_class')}")


if __name__ == "__main__":
    main()
