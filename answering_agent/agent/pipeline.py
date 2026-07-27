"""
Top-level pipeline. Deterministic Python glue between all the pieces.

Changes from v1:
- question_set is now parsed from the questionnaire MD file if not supplied
  in the payload. The QUESTION_BLOCK section inside <question_set> is parsed
  using utils/parse_question_set.py.
- RunResult now carries company, company_slug, md_file, and category so
  app.py can store results in DynamoDB without re-deriving them.
- prompt_assembler strips the --- QUESTION_BLOCK delimiter and everything
  after it from question_set_wrapper so only instructional text goes into
  the prompt.
- Prompt size is logged at assembly time so token issues are visible in logs.
- max_tokens stop reason in react_loop is handled with a nudge instead of
  silent break.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from agent.bedrock_client import BedrockClient
from agent.react_loop import LoopOutcome, MaxIterationsExceeded, run_react_loop
from agent.session import Session
from config import CONFIG, Config
from models.schemas import (
    AnswerFlag,
    ConfidenceLevel,
    PageIndex,
    ParsedQuestionnaire,
    QuestionBlock,
    QuestionResult,
    RunResult,
    RuntimePayload,
    S3Ref,
)
from pageindex.loader import load_pageindex
from pageindex.navigator import build_pageindex_summary
from pdf.page_extractor import PageExtractor
from pdf.s3_client import S3Client
from prompts.prompt_assembler import assemble_prompt
from prompts.prompt_loader import load_questionnaire
from utils.parse_question_set import parse_question_set_from_text
from validation.confidence_check import compute_confidence
from validation.output_validator import validate_answer
from validation.preflight import PreflightError, run_preflight

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entry function
# ---------------------------------------------------------------------------


def run_pipeline(
    payload: RuntimePayload,
    config: Config = CONFIG,
) -> RunResult:
    """
    Load inputs, run preflight, process every question, aggregate results.

    question_set resolution order:
      1. payload.question_set if supplied (overrides MD)
      2. Parsed from the QUESTION_BLOCK section of the questionnaire_md file
    """
    run_id = payload.run_id or uuid.uuid4().hex[:16]

    # 1. Infrastructure ------------------------------------------------------
    s3 = S3Client(
        cache_size=config.pdf_cache_size,
        max_attempts=config.s3_max_attempts,
        region=config.aws_region,
    )
    extractor = PageExtractor(s3)
    bedrock = BedrockClient(
        model_id=config.model_id,
        region=config.aws_region,
        max_output_tokens=config.max_output_tokens,
    )

    # 2. Load inputs ---------------------------------------------------------
    index: PageIndex = load_pageindex(payload.pageindex, s3)

    questionnaire_source: str | S3Ref = payload.questionnaire_md
    parsed_md: ParsedQuestionnaire = load_questionnaire(questionnaire_source, s3)

    # 3. Resolve question_set ------------------------------------------------
    # If the payload supplies question_set use it directly.
    # Otherwise parse from the QUESTION_BLOCK section of the MD file.
    if payload.question_set:
        questions = payload.question_set
        logger.info(
            "pipeline.questions_from_payload",
            extra={"run_id": run_id, "n": len(questions)},
        )
    else:
        raw_blocks = parse_question_set_from_text(parsed_md.question_set_wrapper)
        questions = [QuestionBlock(**b) for b in raw_blocks]
        logger.info(
            "pipeline.questions_from_md",
            extra={"run_id": run_id, "n": len(questions)},
        )

    logger.info(
        "pipeline.start",
        extra={"run_id": run_id, "n_questions": len(questions)},
    )

    # 4. Preflight -----------------------------------------------------------
    errors, warnings = run_preflight(
        index=index,
        questions=questions,
        s3=s3,
        staleness_days=config.staleness_warn_days,
    )
    for w in warnings:
        logger.warning("preflight.warning", extra={"warn": w, "run_id": run_id})
    if errors:
        raise PreflightError(errors)

    # 5. Shared per-run material ---------------------------------------------
    pageindex_summary = build_pageindex_summary(index)

    # 6. Process questions ---------------------------------------------------
    results: list[QuestionResult] = _process_questions(
        questions=questions,
        run_id=run_id,
        parsed_md=parsed_md,
        pageindex_summary=pageindex_summary,
        index=index,
        extractor=extractor,
        bedrock=bedrock,
        config=config,
    )

    # 7. Derive md_file and category from the questionnaire S3 URI -----------
    md_file = ""
    category = ""
    if isinstance(payload.questionnaire_md, S3Ref):
        md_file = payload.questionnaire_md.s3_uri.split("/")[-1]
        category = _category_from_filename(md_file)

    # 8. Aggregate -----------------------------------------------------------
    summary_stats = _summarize(results)

    company_slug = _slugify(payload.company or index.company)

    return RunResult(
        run_id=run_id,
        company=payload.company or index.company,
        company_slug=company_slug,
        pageindex_updated_at=index.updated_at,
        md_file=md_file,
        category=category,
        results=results,
        summary_stats=summary_stats,
    )


# ---------------------------------------------------------------------------
# Per-question processing
# ---------------------------------------------------------------------------


def _process_questions(
    questions: list[QuestionBlock],
    run_id: str,
    parsed_md: ParsedQuestionnaire,
    pageindex_summary: str,
    index: PageIndex,
    extractor: PageExtractor,
    bedrock: BedrockClient,
    config: Config,
) -> list[QuestionResult]:
    if config.max_parallel_questions <= 1 or len(questions) == 1:
        return [
            _run_one_question(
                q, run_id, parsed_md, pageindex_summary, index, extractor, bedrock, config
            )
            for q in questions
        ]

    results: dict[int, QuestionResult] = {}
    with ThreadPoolExecutor(max_workers=config.max_parallel_questions) as ex:
        futures = {
            ex.submit(
                _run_one_question,
                q,
                run_id,
                parsed_md,
                pageindex_summary,
                index,
                extractor,
                bedrock,
                config,
            ): i
            for i, q in enumerate(questions)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "pipeline.question_uncaught", extra={"idx": i, "run_id": run_id}
                )
                q = questions[i]
                results[i] = QuestionResult(
                    question_id=q.id,
                    question_label=q.label,
                    answer_payload={},
                    citations=[],
                    confidence=_min_confidence(),
                    tool_calls_used=0,
                    latency_ms=0,
                    flags=[],
                    error=f"Uncaught pipeline error: {e}",
                )
    return [results[i] for i in range(len(questions))]


def _run_one_question(
    question: QuestionBlock,
    run_id: str,
    parsed_md: ParsedQuestionnaire,
    pageindex_summary: str,
    index: PageIndex,
    extractor: PageExtractor,
    bedrock: BedrockClient,
    config: Config,
) -> QuestionResult:
    session = Session(
        question=question,
        run_id=run_id,
        tool_call_budget=config.tool_call_budget_per_question,
    )
    logger.info(
        "question.start",
        extra={"run_id": run_id, "question_id": question.id, "trace_id": session.trace_id},
    )

    prompt = assemble_prompt(parsed_md, question, pageindex_summary)

    # Log prompt size so max_tokens issues are visible in CloudWatch
    logger.info(
        "prompt.assembled",
        extra={
            "run_id": run_id,
            "question_id": question.id,
            "system_chars": len(prompt.system),
            "user_chars": len(prompt.user),
            "total_chars": len(prompt.system) + len(prompt.user),
        },
    )

    flags: list[AnswerFlag] = []
    error: str | None = None
    outcome: LoopOutcome | None = None
    try:
        outcome = run_react_loop(
            session=session,
            prompt=prompt,
            index=index,
            extractor=extractor,
            bedrock=bedrock,
            hard_iteration_cap=config.hard_iteration_cap,
        )
    except MaxIterationsExceeded as e:
        error = str(e)
        logger.error(
            "question.max_iterations",
            extra={"run_id": run_id, "question_id": question.id},
        )
    except Exception as e:  # noqa: BLE001
        error = f"ReAct loop failure: {e}"
        logger.exception(
            "question.loop_failed",
            extra={"run_id": run_id, "question_id": question.id},
        )

    if session.submitted_answer is None:
        return QuestionResult(
            question_id=question.id,
            question_label=question.label,
            answer_payload={},
            citations=session.citations,
            confidence=_min_confidence(),
            tool_calls_used=session.calls_used,
            latency_ms=session.elapsed_ms(),
            flags=[AnswerFlag.NO_CITATIONS] if not session.citations else [],
            error=error or "Loop terminated without submit_answer",
        )

    submitted = session.submitted_answer
    if outcome is not None and outcome.forced_submit:
        flags.append(AnswerFlag.BUDGET_EXHAUSTED)
    if submitted.get("confidence") == "insufficient_evidence":
        flags.append(AnswerFlag.FALLBACK_FIRED)
    if not session.citations:
        flags.append(AnswerFlag.NO_CITATIONS)

    answer_payload = submitted.get("answer", {})
    schema_problems = validate_answer(
        answer_payload,
        parsed_md.output_schema,
        question_label=question.label,
    )
    if schema_problems and config.allow_schema_retry and not session.schema_retry_used:
        flags.append(AnswerFlag.SCHEMA_RETRY)
        logger.warning(
            "question.schema_violation",
            extra={
                "run_id": run_id,
                "question_id": question.id,
                "problems": schema_problems,
            },
        )

    confidence = compute_confidence(
        session=session,
        model_reported=submitted.get("confidence", "low"),
        reasoning=submitted.get("reasoning", ""),
    )
    if confidence.downgraded:
        flags.append(AnswerFlag.CONFIDENCE_DOWNGRADED)

    return QuestionResult(
        question_id=question.id,
        question_label=question.label,
        answer_payload=answer_payload,
        citations=session.citations,
        confidence=confidence,
        tool_calls_used=session.calls_used,
        latency_ms=session.elapsed_ms(),
        flags=flags,
        error=error,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _min_confidence():
    from models.schemas import ConfidenceBreakdown

    return ConfidenceBreakdown(
        model_reported=ConfidenceLevel.INSUFFICIENT,
        computed_floor=ConfidenceLevel.INSUFFICIENT,
        final=ConfidenceLevel.INSUFFICIENT,
        reasons=["Question failed before submit_answer"],
        downgraded=False,
    )


def _summarize(results: list[QuestionResult]) -> dict[str, Any]:
    n = len(results)
    errors = sum(1 for r in results if r.error)
    by_confidence: dict[str, int] = {}
    total_calls = 0
    total_latency = 0
    for r in results:
        by_confidence[r.confidence.final.value] = (
            by_confidence.get(r.confidence.final.value, 0) + 1
        )
        total_calls += r.tool_calls_used
        total_latency += r.latency_ms
    return {
        "n_questions": n,
        "n_errors": errors,
        "by_confidence": by_confidence,
        "avg_tool_calls": (total_calls / n) if n else 0,
        "avg_latency_ms": (total_latency / n) if n else 0,
    }


def _category_from_filename(filename: str) -> str:
    """
    Derive a human-readable category name from an MD filename.
    code_of_conduct.md  ->  Code Of Conduct
    water_metrics.md    ->  Water Metrics
    """
    name = filename
    if name.endswith(".md"):
        name = name[:-3]
    return name.replace("_", " ").replace("-", " ").title()


def _slugify(name: str) -> str:
    """Simple slug — lowercase, non-alphanumeric runs to hyphens."""
    import unicodedata
    s = unicodedata.normalize("NFKD", name or "unknown")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"

