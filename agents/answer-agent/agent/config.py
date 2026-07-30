"""
Runtime configuration.

All tunables live here so future runs can adjust behavior without code
changes. Values can be overridden via environment variables (prefixed
`AGENT_`) which is the AgentCore Runtime convention.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _str_env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Config:
    # Model
    model_id: str = _str_env("AGENT_MODEL_ID", "amazon.nova-pro-v1:0")
    max_output_tokens: int = _int_env("AGENT_MAX_OUTPUT_TOKENS", 8096)
    # temperature removed — deprecated for Claude Sonnet in Bedrock Converse API

    # Budgets
    tool_call_budget_per_question: int = _int_env("AGENT_TOOL_BUDGET", 15)
    hard_iteration_cap: int = _int_env("AGENT_HARD_ITER_CAP", 50)

    # PDF / S3
    max_page_span: int = _int_env("AGENT_MAX_PAGE_SPAN", 15)
    pdf_cache_size: int = _int_env("AGENT_PDF_CACHE_SIZE", 32)
    s3_max_attempts: int = _int_env("AGENT_S3_MAX_ATTEMPTS", 4)

    # Parallelism
    max_parallel_questions: int = _int_env("AGENT_MAX_PARALLEL", 1)

    # PageIndex staleness — warn if updated_at is older than N days.
    staleness_warn_days: int = _int_env("AGENT_STALENESS_DAYS", 30)

    # Region
    aws_region: str = _str_env("AWS_REGION", "us-east-1")

    # Output-schema retry
    allow_schema_retry: bool = _str_env("AGENT_SCHEMA_RETRY", "1") == "1"


CONFIG = Config()

