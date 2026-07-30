"""
Top-level pipeline. Deterministic Python glue between all the pieces.

Changes from v1:
- Questions are always parsed from the MD file. question_set payload
  override is removed — the MD file is the single source of truth.
- ParsedQuestionnaire no longer has named sections. full_md and questions
  are the only fields.
- QuestionBlock is now opaque — id and raw_text only. question_label in
  QuestionResult is set to the question id since no label field exists.
- Output schema validation removed — schema lives in the MD file and is
  read by the model directly.
- AnswerFlag.SCHEMA_RETRY removed.
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
from validation.confidence_check import compute_confidence
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

    Questions are always parsed from the questionnaire_md file.
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
    parsed_md: ParsedQuestionnaire = load_questionnaire(payload.questionnaire_md, s3)
    questions: list[QuestionBlock] = parsed_md.questions

    logger.info(
        "pipeline.start",
        extra={"run_id": run_id, "n_questions": len(questions)},
    )

    # 3. Preflight -----------------------------------------------------------
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

    # 4. Shared per-run material ---------------------------------------------
    pageindex_summary = build_pageindex_summary(index)

    # 5. Process questions ---------------------------------------------------
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

    # 6. Derive md_file and category from the questionnaire S3 URI -----------
    md_file = ""
    category = ""
    if isinstance(payload.questionnaire_md, S3Ref):
        md_file = payload.questionnaire_md.s3_uri.split("/")[-1]
        category = _category_from_filename(md_file)

    # 7. Aggregate -----------------------------------------------------------
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
                    question_label=q.id,
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
            question_label=question.id,
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

    confidence = compute_confidence(
        session=session,
        model_reported=submitted.get("confidence", "low"),
        reasoning=submitted.get("reasoning", ""),
    )
    if confidence.downgraded:
        flags.append(AnswerFlag.CONFIDENCE_DOWNGRADED)

    answer_payload = submitted.get("answer", {})

    # Pull question_label from the first extracted_fields entry if available.
    # Falls back to question.id if the model did not echo it back.
    fields = answer_payload.get("extracted_fields", [])
    question_label = (
        fields[0].get("question_label", question.id)
        if isinstance(fields, list) and fields and isinstance(fields[0], dict)
        else question.id
    )

    return QuestionResult(
        question_id=question.id,
        question_label=question_label,
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
    name = filename
    if name.endswith(".md"):
        name = name[:-3]
    return name.replace("_", " ").replace("-", " ").title()


def _slugify(name: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", name or "unknown")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"
