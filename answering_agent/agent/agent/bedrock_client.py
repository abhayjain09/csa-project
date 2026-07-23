"""
Minimal wrapper around Bedrock Runtime's Converse API.

Kept dumb on purpose: no retries beyond what boto3 provides, no state, no
history management. The ReAct loop owns all of that. This module only
handles the mechanical mapping between our internal types and the Converse
request/response shape.

Converse request shape reminder:
    {
      "modelId": "...",
      "messages": [ {"role": "user"/"assistant", "content": [...]} ],
      "system":   [ {"text": "..."} ],
      "inferenceConfig": {...},
      "toolConfig": {"tools": [...], "toolChoice": {"auto": {}}}
    }

Content blocks used here:
- {"text": "..."}                          (assistant/user prose)
- {"toolUse": {"toolUseId": ..., "name": ..., "input": {...}}}  (assistant)
- {"toolResult": {"toolUseId": ..., "content": [{"json": {...}}], "status": "success"|"error"}}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

logger = logging.getLogger(__name__)


@dataclass
class LLMToolUse:
    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """Normalized subset of a Converse response we actually care about."""

    stop_reason: str  # "tool_use" | "end_turn" | "max_tokens" | ...
    text_blocks: list[str]
    tool_uses: list[LLMToolUse]
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any]  # full response, for debugging


class BedrockClient:
    """Thin Converse wrapper."""

    def __init__(
        self,
        model_id: str,
        boto3_client: "BedrockRuntimeClient | None" = None,
        region: str | None = None,
        max_output_tokens: int = 8096,
    ) -> None:
        if boto3_client is None:
            import boto3

            boto3_client = boto3.client("bedrock-runtime", region_name=region)
        self._client = boto3_client
        self._model_id = model_id
        self._max_output_tokens = max_output_tokens

    def converse(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """One Converse call with retry on transient ModelErrorException."""
        import time

        request = {
            "modelId": self._model_id,
            "system": [{"text": system}],
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": self._max_output_tokens,
                # temperature intentionally omitted — deprecated for Claude
                # Sonnet models in the Converse API.
            },
            "toolConfig": {"tools": tools, "toolChoice": {"auto": {}}},
        }

        logger.debug(
            "bedrock.converse.request",
            extra={"model": self._model_id, "n_messages": len(messages)},
        )

        last_err = None
        for attempt in range(3):
            try:
                response = self._client.converse(**request)
                logger.debug(
                    "bedrock.converse.response",
                    extra={"stop": response.get("stopReason")},
                )
                return self._normalize(response)
            except Exception as e:
                err_name = type(e).__name__
                # Retry only on ModelErrorException (transient malformed tool
                # use sequence) — all other errors propagate immediately.
                if "ModelErrorException" not in err_name:
                    raise
                last_err = e
                logger.warning(
                    "bedrock.model_error_retry",
                    extra={"attempt": attempt + 1, "err": str(e)},
                )
                time.sleep(2 ** attempt)   # 1s, 2s, 4s backoff

        raise last_err

    def _normalize(self, response: dict[str, Any]) -> LLMResponse:
        stop_reason = response.get("stopReason", "unknown")
        usage = response.get("usage", {}) or {}
        content_blocks = (
            response.get("output", {}).get("message", {}).get("content", []) or []
        )

        text_blocks: list[str] = []
        tool_uses: list[LLMToolUse] = []
        for block in content_blocks:
            if "text" in block:
                text_blocks.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                # Converse returns already-parsed dicts, but be defensive.
                tool_input = tu.get("input", {})
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except json.JSONDecodeError:
                        tool_input = {"_raw": tool_input}
                tool_uses.append(
                    LLMToolUse(
                        tool_use_id=tu.get("toolUseId", ""),
                        name=tu.get("name", ""),
                        input=tool_input,
                    )
                )

        return LLMResponse(
            stop_reason=stop_reason,
            text_blocks=text_blocks,
            tool_uses=tool_uses,
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            raw=response,
        )


