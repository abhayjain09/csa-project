"""
The ReAct loop for one question.

Flow:
1. Send system + initial user prompt + tool config.
2. Bedrock returns either text, tool_use blocks, or end_turn.
3. For each tool_use, dispatch via tools/dispatcher, build a toolResult
   content block, append the assistant message and the toolResult user
   message to history.
4. If submit_answer was called, exit.
5. Otherwise: check budget/diminishing-returns/nudges; loop.

Nudges are injected as extra text content blocks in the toolResult user
message that follows the misbehavior. That way they arrive in-context
without needing a separate turn.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from agent.bedrock_client import BedrockClient, LLMResponse, LLMToolUse
from agent.session import Session
from models.schemas import PageIndex, ToolResult, ToolStatus
from pdf.page_extractor import PageExtractor
from prompts.prompt_assembler import AssembledPrompt
from tools.definitions import TERMINAL_TOOL, TOOL_SPECS
from tools.dispatcher import dispatch

logger = logging.getLogger(__name__)


# Nudges injected based on session state. These accompany the toolResult
# block for the *next* turn — the model sees them alongside the tool output.

NUDGE_BUDGET_80 = (
    "SYSTEM NOTE: You have used 80% of your tool-call budget. Converge on "
    "an answer now. If evidence is sufficient, call submit_answer. If not, "
    "one more targeted lookup, then submit with insufficient_evidence per "
    "fallback_rule if needed."
)

NUDGE_BUDGET_EXHAUSTED = (
    "SYSTEM NOTE: Your tool-call budget is exhausted. The next turn MUST be "
    "submit_answer. Use whatever citations you have; if none, invoke "
    "fallback_rule with confidence='insufficient_evidence'."
)

NUDGE_DIMINISHING_RETURNS = (
    "SYSTEM NOTE: Two consecutive fetch_pages calls yielded no citation. "
    "Backtrack to the outline and try a different section or document, or "
    "submit with the evidence you have."
)


@dataclass
class LoopOutcome:
    submitted: bool
    forced_submit: bool
    llm_input_tokens: int
    llm_output_tokens: int
    iterations: int


class MaxIterationsExceeded(RuntimeError):
    """Safety valve — should never trigger in normal runs."""


def _build_tool_result_content(
    tool_use_id: str, result: ToolResult
) -> dict[str, Any]:
    """Convert our ToolResult into a Converse `toolResult` content block."""
    # Converse toolResult supports content items of {"text": ...} or
    # {"json": ...}. We prefer json for structured data — the model can parse
    # it natively.
    payload: dict[str, Any] = {}
    if result.data is not None:
        payload["data"] = result.data
    if result.message is not None:
        payload["message"] = result.message
    payload["status"] = result.status.value

    return {
        "toolResult": {
            "toolUseId": tool_use_id,
            "content": [{"json": payload}],
            "status": "error" if result.status == ToolStatus.ERROR else "success",
        }
    }


def _text_content(text: str) -> dict[str, Any]:
    return {"text": text}


def run_react_loop(
    session: Session,
    prompt: AssembledPrompt,
    index: PageIndex,
    extractor: PageExtractor,
    bedrock: BedrockClient,
    hard_iteration_cap: int = 50,
) -> LoopOutcome:
    """
    Drive the loop until submit_answer or budget exhaustion.

    hard_iteration_cap is a safety valve above the tool-call budget — it
    only trips if something's very wrong (e.g., the model refuses to call
    tools). Normal termination is via budget + forced submit.
    """
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [_text_content(prompt.user)]}
    ]

    total_input_tokens = 0
    total_output_tokens = 0
    iterations = 0
    forced_submit = False
    submitted = False
    refused_submit_count = 0  # tracks consecutive end_turn without submit_answer

    # Track whether budget/nudge messages have been issued so we don't spam.
    nudged_80 = False
    nudged_diminishing = False

    while iterations < hard_iteration_cap:
        iterations += 1

        response = bedrock.converse(
            system=prompt.system, messages=messages, tools=TOOL_SPECS
        )
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens

        # Record the assistant turn in the transcript regardless of stop reason.
        assistant_content = _reconstruct_assistant_content(response)
        messages.append({"role": "assistant", "content": assistant_content})

        # No tool uses? Model produced only text and stopped.
        if not response.tool_uses:
            if response.stop_reason == "end_turn":
                # The model ended without calling submit_answer. Nudge back.
                refused_submit_count += 1
                logger.warning(
                    "loop.model_refused_submit",
                    extra={
                        "trace_id": session.trace_id,
                        "consecutive": refused_submit_count,
                    },
                )
                if refused_submit_count >= 3:
                    # Three consecutive refusals — abort rather than spinning
                    # until the AgentCore 15-min execution wall hits.
                    logger.error(
                        "loop.refused_submit_abort",
                        extra={"trace_id": session.trace_id},
                    )
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            _text_content(
                                "You produced text but did not call submit_answer. "
                                "You MUST call submit_answer now — do not produce "
                                "more text. If evidence is insufficient, call "
                                "submit_answer with confidence='insufficient_evidence' "
                                "per fallback_rule. This is mandatory."
                            )
                        ],
                    }
                )
                continue
            if response.stop_reason == "max_tokens":
                # Model hit output token limit mid-response. Nudge it to
                # submit with whatever evidence it has so we get a result
                # instead of silently breaking with no answer.
                logger.warning(
                    "loop.max_tokens_nudge",
                    extra={"trace_id": session.trace_id, "citations": len(session.citations)},
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            _text_content(
                                "SYSTEM NOTE: Your response was cut off because you "
                                "reached the output token limit. Call submit_answer "
                                "immediately with whatever evidence you have gathered. "
                                "If no citations have been recorded, set confidence to "
                                "insufficient_evidence and invoke the fallback_rule."
                            )
                        ],
                    }
                )
                continue
            # Other stop reasons (guardrail, content_filter, etc.) — abort.
            logger.warning(
                "loop.unexpected_stop",
                extra={"trace_id": session.trace_id, "stop": response.stop_reason},
            )
            break

        # Dispatch every tool use in this turn.
        tool_result_blocks: list[dict[str, Any]] = []
        submit_seen = False
        for tu in response.tool_uses:
            result = dispatch(
                session=session,
                index=index,
                extractor=extractor,
                tool_name=tu.name,
                tool_input=tu.input,
            )
            tool_result_blocks.append(_build_tool_result_content(tu.tool_use_id, result))

            if tu.name == TERMINAL_TOOL and result.status == ToolStatus.OK:
                submit_seen = True
                submitted = True

        # Append the toolResult user turn.
        user_turn_content: list[dict[str, Any]] = list(tool_result_blocks)

        # Nudges — appended alongside toolResults so the model sees them
        # with the outputs it's about to reason over.
        if not submitted:
            if session.budget_exhausted:
                # Next turn is the last chance.
                user_turn_content.append(_text_content(NUDGE_BUDGET_EXHAUSTED))
                forced_submit = True
            elif not nudged_80 and session.budget_pct_used() >= 0.8:
                user_turn_content.append(_text_content(NUDGE_BUDGET_80))
                nudged_80 = True
            if (
                not nudged_diminishing
                and session.consecutive_empty_fetches >= 2
            ):
                user_turn_content.append(_text_content(NUDGE_DIMINISHING_RETURNS))
                nudged_diminishing = True

        messages.append({"role": "user", "content": user_turn_content})

        if submitted:
            break

    else:
        # while-else: hard_iteration_cap hit.
        raise MaxIterationsExceeded(
            f"ReAct loop exceeded hard cap of {hard_iteration_cap} iterations"
        )

    return LoopOutcome(
        submitted=submitted,
        forced_submit=forced_submit,
        llm_input_tokens=total_input_tokens,
        llm_output_tokens=total_output_tokens,
        iterations=iterations,
    )


def _reconstruct_assistant_content(response: LLMResponse) -> list[dict[str, Any]]:
    """Rebuild the assistant content blocks from a normalized LLMResponse so
    they can be appended to the message history verbatim.

    We rely on the raw output block list where possible to preserve any
    ordering/metadata Converse expects on subsequent turns.
    """
    raw_content = (
        response.raw.get("output", {}).get("message", {}).get("content", []) or []
    )
    if raw_content:
        return raw_content
    # Fallback: reconstruct from normalized fields.
    blocks: list[dict[str, Any]] = []
    for text in response.text_blocks:
        blocks.append({"text": text})
    for tu in response.tool_uses:
        blocks.append(
            {
                "toolUse": {
                    "toolUseId": tu.tool_use_id,
                    "name": tu.name,
                    "input": tu.input,
                }
            }
        )
    return blocks


