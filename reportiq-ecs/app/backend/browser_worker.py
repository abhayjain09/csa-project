"""Long-running ECS browser fallback for Report IQ.

Only jobs admitted by the synchronous agent's ``blocked_by_source_waf`` result
reach this worker. It retries exact official document URLs in a persistent
Chromium session, optionally through an approved outbound proxy, and fails
closed unless the downloaded PDF matches both the company and report class.
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
import unicodedata
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import unquote, urlparse

import boto3
from botocore.exceptions import ClientError
from playwright.sync_api import sync_playwright
from pypdf import PdfReader


REGION = os.environ.get("AWS_REGION", "us-east-1")
BROWSER_JOBS_TABLE = os.environ.get(
    "BROWSER_JOBS_TABLE", "reportiq-browser-jobs")
RUNS_TABLE = os.environ.get("RUNS_TABLE", "reportiq-runs")
QUERIES_TABLE = os.environ.get("QUERIES_TABLE", "reportiq-web-queries")
PROVENANCE_TABLE = os.environ.get(
    "PROVENANCE_TABLE", "edo-coanalyst-report-provenance")
REPORTS_BUCKET = os.environ.get(
    "REPORTS_BUCKET", "edo-coanalyst-report-610639371721")
JOB_ID = os.environ.get("BROWSER_JOB_ID", "").strip()
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium")
MAX_DOCUMENT_BYTES = int(os.environ.get(
    "BROWSER_WORKER_MAX_DOCUMENT_BYTES", str(50 * 1024 * 1024)))
NAV_TIMEOUT_MS = int(os.environ.get("BROWSER_WORKER_NAV_TIMEOUT_MS", "90000"))
MAX_ATTEMPTS = max(1, int(os.environ.get(
    "BROWSER_WORKER_MAX_ATTEMPTS", "3")))
RETRY_DELAY_SECONDS = max(0, int(os.environ.get(
    "BROWSER_WORKER_RETRY_DELAY_SECONDS", "20")))
RUN_PATCH_WAIT_SECONDS = max(60, int(os.environ.get(
    "BROWSER_WORKER_RUN_PATCH_WAIT_SECONDS", "7200")))

dynamo = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
jobs_table = dynamo.Table(BROWSER_JOBS_TABLE)
runs_table = dynamo.Table(RUNS_TABLE)
queries_table = dynamo.Table(QUERIES_TABLE)
provenance_table = dynamo.Table(PROVENANCE_TABLE)

_BLOCK_MARKERS = (
    "access denied",
    "request rejected",
    "reference #",
    "akamai",
    "bot detection",
    "captcha",
    "verify you are human",
    "temporarily blocked",
)
_TERMINAL_JOB_STATUSES = {
    "downloaded", "blocked_by_source_waf", "failed", "launch_failed",
}
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "company", "co", "limited",
    "ltd", "plc", "llc", "holdings", "group",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-") or "unknown"


def _safe_filename(url: str) -> str:
    name = unquote(urlparse(url).path).rsplit("/", 1)[-1] or "document.pdf"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name.lower().endswith(".pdf"):
        name = (name or "document") + ".pdf"
    return name[:180]


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = value.replace("s&p", "sp").replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _same_official_domain(host: str, official_domain: str) -> bool:
    host = (host or "").lower().strip(".")
    official = (official_domain or "").lower().strip(".")
    if official.startswith("www."):
        official = official[4:]
    if host.startswith("www."):
        host = host[4:]
    return bool(official) and (
        host == official or host.endswith("." + official))


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return bool(infos)


def _safe_candidate(url: str, official_domain: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or not host or parsed.username
                or parsed.password or parsed.port not in (None, 443)):
            return False
        if not parsed.path.lower().endswith(".pdf"):
            return False
        if not _same_official_domain(host, official_domain):
            return False
        return _public_host(host)
    except (ValueError, OSError):
        return False


def _proxy_config() -> dict | None:
    """Accept either a raw proxy URL or a Secrets Manager JSON value."""
    raw = os.environ.get("BROWSER_OUTBOUND_PROXY", "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = {"server": raw}
    if isinstance(value, str):
        value = {"server": value}
    if not isinstance(value, dict):
        raise ValueError("BROWSER_OUTBOUND_PROXY must be a URL or JSON object")
    server = value.get("server") or value.get("url")
    parsed = urlparse(str(server or ""))
    if (not server or parsed.scheme not in {"http", "https"}
            or not parsed.hostname):
        raise ValueError("proxy secret requires an http(s) server/url")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    clean_server = f"{parsed.scheme}://{host}"
    if parsed.port:
        clean_server += f":{parsed.port}"
    result = {"server": clean_server}
    username = value.get("username") or parsed.username
    password = value.get("password") or parsed.password
    if username:
        result["username"] = unquote(str(username))
    if password:
        result["password"] = unquote(str(password))
    return result


def _extract_pdf_text(body: bytes) -> str:
    reader = PdfReader(BytesIO(body), strict=False)
    chunks = []
    page_count = len(reader.pages)
    indices = list(range(min(page_count, 18)))
    if page_count > 18:
        indices.extend(range(max(18, page_count - 3), page_count))
    for index in dict.fromkeys(indices):
        try:
            chunks.append(reader.pages[index].extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks)[:180_000]


def _company_matches(company: str, text: str, url: str) -> bool:
    haystack = _normalize_text(text + " " + unquote(urlparse(url).path))
    company_norm = _normalize_text(company)
    if company_norm and company_norm in haystack:
        return True
    tokens = [
        token for token in company_norm.split()
        if token not in _LEGAL_SUFFIXES and len(token) >= 2
    ]
    return bool(tokens) and all(
        re.search(rf"\b{re.escape(token)}\b", haystack) for token in tokens)


def _class_matches(report_class: str, text: str, url: str,
                   year: str) -> tuple[bool, str]:
    haystack = _normalize_text(text + " " + unquote(urlparse(url).path))
    canonical = _normalize_text(report_class)
    rules = {
        "annual report": (
            ("annual report", "report and accounts", "form 10 k", "form 20 f"),
            ("quarterly report", "form 10 q"),
        ),
        "proxy statement": (
            ("proxy statement", "definitive proxy", "def 14a"),
            ("preliminary proxy",),
        ),
        "sustainability report": (
            ("sustainability report", "esg report", "impact report",
             "sustainability statement", "brsr"),
            ("annual report",),
        ),
        "code of conduct": (
            ("code of conduct", "business conduct and ethics", "code of ethics"),
            ("supplier code", "vendor code", "third party code"),
        ),
        "supplier code of conduct": (
            ("supplier code", "vendor code", "third party code",
             "business partner code", "responsible sourcing"),
            (),
        ),
        "tax strategy and governance": (
            ("tax strategy", "tax policy", "tax governance"),
            (),
        ),
        "whistleblowing mechanism": (
            ("whistleblowing policy", "whistleblower policy",
             "speak up policy", "ethics hotline"),
            (),
        ),
        "occupational health and safety policy": (
            ("health and safety policy", "occupational health and safety",
             "workplace health and safety", "hse policy", "hsse policy"),
            (),
        ),
        "environmental policy": (
            ("environmental policy", "environment policy",
             "environmental management policy"),
            ("sustainability report",),
        ),
        "anti bribery and corruption policy": (
            ("anti bribery", "anti corruption", "bribery and corruption policy"),
            (),
        ),
        "conflicts of interest policy": (
            ("conflict of interest policy", "conflicts of interest policy"),
            (),
        ),
        "discrimination and harassment policy": (
            ("anti discrimination", "discrimination and harassment policy",
             "harassment policy"),
            (),
        ),
    }
    accepted, rejected = rules.get(canonical, ((), ()))
    if not accepted:
        query_tokens = [
            token for token in canonical.split()
            if token not in {"and", "the", "of", "policy", "report"}
            and len(token) > 3
        ]
        if not query_tokens or not all(token in haystack for token in query_tokens):
            return False, "unsupported class did not match all specific terms"
    elif not any(term in haystack for term in accepted):
        return False, f"content is not a {report_class}"
    if rejected and any(term in haystack for term in rejected):
        return False, f"content matches an excluded near-neighbour class"
    if canonical in {"annual report", "proxy statement"} and year:
        if str(year) not in haystack:
            return False, f"required year {year} is absent"
    return True, "company and class verified"


def _verify_pdf(job: dict, url: str, body: bytes) -> tuple[bool, str, str]:
    if not body.startswith(b"%PDF"):
        return False, "response is not a PDF", ""
    if len(body) > MAX_DOCUMENT_BYTES:
        return False, "PDF exceeds configured maximum size", ""
    try:
        text = _extract_pdf_text(body)
    except Exception as exc:
        return False, f"PDF parse failed: {type(exc).__name__}", ""
    if len(_normalize_text(text)) < 80:
        return False, "PDF has insufficient extractable text", text
    if not _company_matches(job.get("company", ""), text, url):
        return False, "company identity is absent from PDF", text
    ok, reason = _class_matches(
        job.get("report_class", ""), text, url, job.get("year", ""))
    return ok, reason, text


def _response_body(context, page, url: str, referer: str) -> tuple:
    status = 0
    ctype = ""
    body = None
    marker = ""
    response = page.goto(
        url, wait_until="commit", timeout=NAV_TIMEOUT_MS, referer=referer)
    if response is not None:
        status = response.status
        ctype = (response.headers or {}).get(
            "content-type", "").split(";")[0].lower()
        if status < 400 and (
                "pdf" in ctype or urlparse(url).path.lower().endswith(".pdf")):
            try:
                body = response.body()
            except Exception:
                body = None
    if not body and status < 400:
        try:
            request_response = context.request.get(
                url,
                headers={
                    "referer": referer,
                    "accept": "application/pdf,*/*;q=0.8",
                },
                timeout=NAV_TIMEOUT_MS,
            )
            status = request_response.status
            ctype = (request_response.headers or {}).get(
                "content-type", "").split(";")[0].lower()
            if status < 400:
                body = request_response.body()
                if body and not body.startswith(b"%PDF"):
                    sample = body[:100_000].decode("utf-8", "ignore").lower()
                    marker = next(
                        (term for term in _BLOCK_MARKERS if term in sample), "")
        except Exception:
            pass
    return status, ctype, body, marker


def _download(job: dict) -> tuple:
    raw_candidates = job.get("candidate_urls") or "[]"
    candidates = (
        json.loads(raw_candidates)
        if isinstance(raw_candidates, str) else raw_candidates)
    official_domain = job.get("official_domain", "")
    candidates = [
        url for url in candidates
        if isinstance(url, str) and _safe_candidate(url, official_domain)
    ][:8]
    if not candidates:
        raise ValueError("job has no safe same-domain PDF candidates")

    launch_args = {
        "headless": True,
        "executable_path": CHROMIUM_PATH,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    proxy = _proxy_config()
    if proxy:
        launch_args["proxy"] = proxy
    last_reason = "no candidate returned a PDF"
    blocked = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_args)
        try:
            context = browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"),
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "DNT": "1",
                },
            )
            root = f"https://{official_domain}/"
            for attempt in range(1, MAX_ATTEMPTS + 1):
                page = context.new_page()
                try:
                    try:
                        page.goto(
                            root, wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_MS)
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    for url in candidates:
                        try:
                            status, _, body, marker = _response_body(
                                context, page, url, root)
                        except Exception as exc:
                            last_reason = (
                                f"navigation failed: {type(exc).__name__}")
                            continue
                        if status in {401, 403, 406, 429} or marker:
                            blocked = True
                            last_reason = marker or f"HTTP {status}"
                            continue
                        if not body:
                            last_reason = f"empty response (HTTP {status})"
                            continue
                        ok, reason, _ = _verify_pdf(job, url, body)
                        if ok:
                            return url, body, "application/pdf", False
                        last_reason = reason
                finally:
                    page.close()
                if attempt < MAX_ATTEMPTS and RETRY_DELAY_SECONDS:
                    time.sleep(RETRY_DELAY_SECONDS)
        finally:
            browser.close()
    return None, None, last_reason, blocked


def _store(job: dict, url: str, body: bytes, ctype: str) -> dict:
    digest = hashlib.sha256(body).hexdigest()
    company_slug = _slug(job.get("company", ""))
    class_slug = _slug(job.get("report_class", "")) or "uncategorized"
    filename = _safe_filename(url)
    s3_key = (
        f"{company_slug}/{class_slug}/{digest[:12]}-{filename}")
    metadata = {
        "source_url": url,
        "sha256": digest,
        "run_id": job.get("run_id", ""),
        "browser_job_id": job.get("job_id", ""),
    }
    try:
        s3.head_object(Bucket=REPORTS_BUCKET, Key=s3_key)
        duplicate = True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise
        s3.put_object(
            Bucket=REPORTS_BUCKET,
            Key=s3_key,
            Body=body,
            ContentType=ctype,
            Metadata=metadata,
        )
        duplicate = False
    sidecar = {
        "company": company_slug,
        "company_name": job.get("company", ""),
        "doc_class": job.get("report_class") or None,
        "doc_classes": [job.get("report_class")] if job.get("report_class") else [],
        "year": job.get("year") or None,
        "source_url": url,
        "sha256": digest,
        "content_type": ctype,
        "run_id": job.get("run_id", ""),
        "request_id": job.get("request_id", ""),
        "query": job.get("query", ""),
        "prepared_query": job.get("prepared_query", ""),
        "resolved_via": "ecs_browser_worker",
    }
    s3.put_object(
        Bucket=REPORTS_BUCKET,
        Key=s3_key + ".metadata.json",
        Body=json.dumps(sidecar).encode("utf-8"),
        ContentType="application/json",
    )
    provenance = {
        "company": company_slug,
        "s3_key": s3_key,
        "run_id": job.get("run_id", ""),
        "report": filename,
        "source_url": url,
        "query": job.get("query", ""),
        "prepared_query": job.get("prepared_query", ""),
        "request_id": job.get("request_id", ""),
        "doc_class": job.get("report_class") or None,
        "year": job.get("year") or None,
        "hash": digest,
        "content_type": ctype,
        "downloaded": _now(),
        "rag_status": "Pending",
        "resolved_via": "ecs_browser_worker",
    }
    try:
        provenance_table.put_item(
            Item=provenance,
            ConditionExpression="attribute_not_exists(s3_key)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get(
                "Code") != "ConditionalCheckFailedException":
            raise
    return {
        "s3_key": s3_key,
        "file_name": filename,
        "source_url": url,
        "duplicate": duplicate,
        "browser_job_id": job.get("job_id", ""),
    }


def _all_run_jobs_terminal(run_id: str) -> bool:
    response = jobs_table.scan(
        FilterExpression="#run = :run",
        ExpressionAttributeNames={"#run": "run_id"},
        ExpressionAttributeValues={":run": run_id},
        ProjectionExpression="#st",
    )
    statuses = [
        item.get("status", "") for item in response.get("Items", [])]
    while response.get("LastEvaluatedKey"):
        response = jobs_table.scan(
            ExclusiveStartKey=response["LastEvaluatedKey"],
            FilterExpression="#run = :run",
            ExpressionAttributeNames={"#run": "run_id"},
            ExpressionAttributeValues={":run": run_id},
            ProjectionExpression="#st",
        )
        statuses.extend(
            item.get("status", "") for item in response.get("Items", []))
    return bool(statuses) and all(
        status in _TERMINAL_JOB_STATUSES for status in statuses)


def _patch_run(job: dict, result: dict | None, failure_status: str = "") -> None:
    run_id = job.get("run_id", "")
    if not run_id:
        return
    # The normal chunk fan-out owns the run row while its status is "running".
    # A fast browser task must not overwrite a later incremental/final flush.
    # Wait for that owner to transition the run to browser_retry_pending first.
    wait_deadline = time.monotonic() + RUN_PATCH_WAIT_SECONDS
    while time.monotonic() < wait_deadline:
        current = runs_table.get_item(
            Key={"run_id": run_id}).get("Item", {})
        if current.get("status") != "running":
            break
        time.sleep(5)
    else:
        raise RuntimeError(
            "main run remained active beyond browser patch wait window")

    for _ in range(6):
        run = runs_table.get_item(Key={"run_id": run_id}).get("Item")
        if not run:
            return
        try:
            downloaded = json.loads(run.get("downloaded") or "[]")
        except Exception:
            downloaded = []
        try:
            failures = json.loads(run.get("failures") or "[]")
        except Exception:
            failures = []
        try:
            diagnostics = json.loads(run.get("diagnostics") or "{}")
        except Exception:
            diagnostics = {}

        if result and not any(
                item.get("s3_key") == result["s3_key"]
                for item in downloaded if isinstance(item, dict)):
            downloaded.append(result)
        if result:
            failures = [
                item for item in failures
                if not (isinstance(item, dict)
                        and item.get("request_id") == job.get("request_id"))
            ]
        for chunk in diagnostics.get("per_chunk", []):
            for row in chunk.get("results", []):
                if row.get("request_id") != job.get("request_id"):
                    continue
                if result:
                    row.update({
                        "status": "downloaded",
                        "s3_key": result["s3_key"],
                        "file_name": result["file_name"],
                        "source_url": result["source_url"],
                        "duplicate": result["duplicate"],
                        "browser_job_id": job.get("job_id", ""),
                    })
                    row.pop("reason", None)
                else:
                    row["status"] = (
                        failure_status or "blocked_by_source_waf")
                    row["reason"] = job.get(
                        "error_msg", "long-running browser did not download")

        all_terminal = _all_run_jobs_terminal(run_id)
        if not all_terminal:
            run_status = "browser_retry_pending"
        elif downloaded:
            run_status = "complete"
        else:
            run_status = "no_results"
        old_version = int(run.get("browser_patch_version", 0))
        names = {
            "#st": "status",
            "#dl": "downloaded",
            "#fl": "failures",
            "#dg": "diagnostics",
            "#ver": "browser_patch_version",
            "#fin": "finished_at",
        }
        values = {
            ":st": run_status,
            ":dl": json.dumps(downloaded),
            ":fl": json.dumps(failures),
            ":dg": json.dumps(diagnostics),
            ":old": old_version,
            ":new": old_version + 1,
            ":fin": _now(),
        }
        condition = "(attribute_not_exists(#ver) OR #ver = :old)"
        try:
            runs_table.update_item(
                Key={"run_id": run_id},
                UpdateExpression=(
                    "SET #st = :st, #dl = :dl, #fl = :fl, #dg = :dg, "
                    "#ver = :new, #fin = :fin"),
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            query_id = job.get("query_id", "")
            if query_id:
                queries_table.update_item(
                    Key={"query_id": query_id},
                    UpdateExpression="SET #st = :st, updated_at = :u",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={
                        ":st": run_status, ":u": _now()},
                )
            return
        except ClientError as exc:
            if exc.response.get("Error", {}).get(
                    "Code") != "ConditionalCheckFailedException":
                raise
    raise RuntimeError("could not patch run after concurrent browser updates")


def _set_job(status: str, **attributes) -> None:
    names = {"#st": "status"}
    values = {":st": status, ":u": _now()}
    assignments = ["#st = :st", "updated_at = :u"]
    for index, (key, value) in enumerate(attributes.items()):
        name_key = f"#n{index}"
        value_key = f":v{index}"
        names[name_key] = key
        values[value_key] = value
        assignments.append(f"{name_key} = {value_key}")
    jobs_table.update_item(
        Key={"job_id": JOB_ID},
        UpdateExpression="SET " + ", ".join(assignments),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def main() -> int:
    if not JOB_ID:
        print("BROWSER_JOB_ID is required", file=sys.stderr)
        return 2
    job = jobs_table.get_item(Key={"job_id": JOB_ID}).get("Item")
    if not job:
        print(f"browser job not found: {JOB_ID}", file=sys.stderr)
        return 2
    job["job_id"] = JOB_ID
    _set_job("running", started_at=_now())
    try:
        url, body, detail, blocked = _download(job)
        if not url or not body:
            status = "blocked_by_source_waf" if blocked else "failed"
            job["error_msg"] = str(detail)[:1000]
            _set_job(status, error_msg=job["error_msg"], finished_at=_now())
            _patch_run(job, None, status)
            print(json.dumps({
                "job_id": JOB_ID, "status": status, "reason": detail}))
            return 1
        result = _store(job, url, body, detail)
        _set_job(
            "downloaded",
            s3_key=result["s3_key"],
            source_url=url,
            duplicate=result["duplicate"],
            finished_at=_now(),
        )
        _patch_run(job, result)
        print(json.dumps({
            "job_id": JOB_ID,
            "status": "downloaded",
            "s3_key": result["s3_key"],
        }))
        return 0
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:1000]
        job["error_msg"] = message
        try:
            _set_job("failed", error_msg=message, finished_at=_now())
            _patch_run(job, None, "failed")
        except Exception as patch_exc:
            print(f"job/run failure update also failed: {patch_exc}",
                  file=sys.stderr)
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
