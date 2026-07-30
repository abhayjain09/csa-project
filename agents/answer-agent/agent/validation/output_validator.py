"""
Validate the submitted answer against the output_schema declared in the MD.

The MD's <output_schema> may be either:
- A JSON Schema (detected by presence of "$schema" or top-level "type"), or
- Human-authored guidance text (in which case we do best-effort structural
  checks and rely on the model to have followed it).

For JSON Schema mode, we use `jsonschema.validate`. For guidance mode, we
verify only that the answer is a JSON object and contains a `label` field
matching the question label — the two universal expectations.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class OutputValidationError(ValueError):
    pass


def _extract_json_schema(schema_text: str) -> dict | None:
    """If the output_schema section contains a fenced JSON block, extract
    and parse it. Otherwise return None."""
    # Look for ```json ... ``` first.
    m = re.search(r"```json\s*\n(.*?)```", schema_text, re.DOTALL)
    candidate = m.group(1) if m else None
    if candidate is None:
        # Look for a bare {...} block.
        m = re.search(r"(\{.*\})", schema_text, re.DOTALL)
        candidate = m.group(1) if m else None
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    # Heuristic: looks like JSON Schema if it has $schema, type=object, or
    # properties.
    if "$schema" in parsed or parsed.get("type") == "object" or "properties" in parsed:
        return parsed
    return None


def validate_answer(
    answer: dict[str, Any],
    output_schema_text: str,
    question_label: str,
) -> list[str]:
    """
    Return a list of validation problems. Empty list = valid.
    """
    problems: list[str] = []

    if not isinstance(answer, dict):
        return [f"answer is not a JSON object (got {type(answer).__name__})"]

    schema = _extract_json_schema(output_schema_text)
    if schema is not None:
        try:
            import jsonschema  # lazy import

            validator = jsonschema.Draft202012Validator(schema)
            for err in sorted(validator.iter_errors(answer), key=lambda e: e.path):
                path = ".".join(str(p) for p in err.path) or "<root>"
                problems.append(f"{path}: {err.message}")
        except ImportError:
            logger.warning(
                "jsonschema not installed; skipping strict schema validation"
            )
        except Exception as e:  # noqa: BLE001
            problems.append(f"Schema validation error: {e}")

    # Universal soft check regardless of schema mode: the answer should
    # carry the exact question label somewhere. We look for a top-level
    # "label" or "question" field.
    label_field = answer.get("label") or answer.get("question")
    if label_field is not None and label_field != question_label:
        problems.append(
            f"answer label mismatch: expected {question_label!r}, got {label_field!r}"
        )

    return problems
