"""Long-running Fargate browser worker.

This worker handles normal, JavaScript-heavy navigation after the synchronous
runtime queues a job. It detects login pages, WAF pages, and CAPTCHAs and reports
them for human review. It never submits credentials, solves CAPTCHAs, or bypasses
bot protections.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Any
from urllib.parse import urljoin

import boto3
from playwright.sync_api import sync_playwright

import agent


JOB_QUEUE_URL = os.environ["FARGATE_BROWSER_QUEUE_URL"]
RESULT_QUEUE_URL = os.environ["FARGATE_BROWSER_RESULT_QUEUE_URL"]
REGION = os.environ.get("APP_REGION") or os.environ.get("AWS_REGION", "us-east-1")
MAX_PAGES = int(os.environ.get("FARGATE_BROWSER_MAX_PAGES", "40"))
MAX_SECONDS = int(os.environ.get("FARGATE_BROWSER_MAX_SECONDS", "600"))
CLICK_TIMEOUT_MS = int(os.environ.get("FARGATE_BROWSER_CLICK_TIMEOUT_MS", "20000"))

sqs = boto3.client("sqs", region_name=REGION)

_WAF_OR_CAPTCHA_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "verify you are human", "verify you are a human",
    "attention required", "access denied", "bot detection", "cloudflare", "akamai",
)


def _access_block(page) -> str | None:
    """Classify a page that needs human access. Do not attempt to evade it."""
    try:
        if page.locator("input[type='password']").count() > 0:
            return "login_required"
        text = (page.title() + " " + page.locator("body").inner_text(timeout=3000)[:12000]).lower()
        if any(marker in text for marker in _WAF_OR_CAPTCHA_MARKERS):
            return "blocked_waf_or_captcha"
    except Exception:
        return None
    return None


def _parse_job(raw: dict[str, Any]) -> tuple[agent.Company, agent.DocumentRequest]:
    company = agent._parse_company({"company": raw["company"]})
    item = raw["request"]
    request = agent.DocumentRequest(
        id=str(item["id"]),
        document_type=str(item["document_type"]),
        year=item.get("year"),
        allow_search=False,
        allow_browser=True,
    )
    return company, request


def _browser_candidates(company: agent.Company, request: agent.DocumentRequest) -> tuple[list[agent.Candidate], str | None]:
    seeds: list[str] = []
    for domain in company.official_domains:
        root = f"https://{agent._normalise_domain(domain)}"
        seeds.extend(urljoin(root, path) for path in ["/", *agent.DOCUMENT_RULES[request.document_type]["site_paths"]])
    queue: deque[str] = deque(dict.fromkeys(seeds))
    visited: set[str] = set()
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        try:
            while queue and len(visited) < MAX_PAGES and time.monotonic() - started < MAX_SECONDS:
                url = queue.popleft()
                if url in visited or not any(agent._host_matches(url, domain) for domain in company.official_domains):
                    continue
                visited.add(url)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=CLICK_TIMEOUT_MS)
                    page.wait_for_timeout(1000)
                except Exception:
                    continue
                block = _access_block(page)
                if block:
                    return [], block
                agent._browser_select_requested_year(page, request)
                direct = agent._browser_document_candidates(page, company, request)
                if direct:
                    return direct, None
                downloads = agent._browser_click_download(page, company, request)
                if downloads:
                    return downloads, None
                agent._browser_expand_navigation(page, request)
                block = _access_block(page)
                if block:
                    return [], block
                direct = agent._browser_document_candidates(page, company, request)
                if direct:
                    return direct, None
                for next_url in agent._browser_navigation_urls(page, company, request):
                    if next_url not in visited:
                        queue.append(next_url)
        finally:
            browser.close()
    return [], None


def _result(job_id: str, run_id: str, request_id: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"job_id": job_id, "run_id": run_id, "request_id": request_id, "status": status, **extra}


def _process(raw: dict[str, Any]) -> dict[str, Any]:
    job_id = str(raw["job_id"])
    run_id = str(raw["run_id"])
    company, request = _parse_job(raw)
    candidates, access_status = _browser_candidates(company, request)
    if access_status:
        return _result(job_id, run_id, request.id, "manual_review_required", reason=access_status)
    diagnostics: list[str] = []
    candidate, validation = agent._fetch_and_select(candidates, company, request, diagnostics)
    if not candidate or not validation:
        return _result(job_id, run_id, request.id, "not_found", diagnostics=diagnostics)
    stored = agent._store(company, request, run_id, candidate, validation)
    return _result(job_id, run_id, request.id, stored["status"], document=stored)


def _send_result(result: dict[str, Any]) -> None:
    sqs.send_message(QueueUrl=RESULT_QUEUE_URL, MessageBody=json.dumps(result))


def main() -> None:
    print("Fargate browser worker started", flush=True)
    while True:
        response = sqs.receive_message(
            QueueUrl=JOB_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=900,
        )
        for message in response.get("Messages", []):
            raw: dict[str, Any] = {}
            try:
                raw = json.loads(message["Body"])
                result = _process(raw)
            except Exception as exc:
                result = _result(
                    str(raw.get("job_id", "unknown")),
                    str(raw.get("run_id", "unknown")),
                    str((raw.get("request") or {}).get("id", "unknown")),
                    "worker_error",
                    reason=type(exc).__name__,
                )
            _send_result(result)
            sqs.delete_message(QueueUrl=JOB_QUEUE_URL, ReceiptHandle=message["ReceiptHandle"])


if __name__ == "__main__":
    main()
