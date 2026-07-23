"""
Compose the final prompt for one question.

Order:
1. system_directive               (from MD)
2. traversal_instructions         (constant, hand-authored)
3. field_usage_note               (constant, hand-authored)
4. pageindex_summary              (auto-built)
5. question_set_wrapper           (instructional text ABOVE --- QUESTION_BLOCK only)
6. question_block                 (one question, formatted)
7. output_schema                  (from MD)
8. example                        (from MD)
9. confidence_scoring             (from MD)

Change from v1:
The <question_set> content now contains the --- QUESTION_BLOCK delimiter and
all raw question text. We strip everything from the delimiter onwards so only
the instructional text above it goes into the prompt — the model should not
see all questions in the set, only the one it is answering.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from models.schemas import ParsedQuestionnaire, QuestionBlock

_HERE = Path(__file__).parent
TRAVERSAL_INSTRUCTIONS = (_HERE / "traversal_instructions.md").read_text(encoding="utf-8")
FIELD_USAGE_NOTE = (_HERE / "field_usage_note.md").read_text(encoding="utf-8")

# Matches the --- QUESTION_BLOCK delimiter line.
_BLOCK_DELIMITER_RE = re.compile(r"-{3,}\s*QUESTION_BLOCK", re.IGNORECASE)


@dataclass
class AssembledPrompt:
    system: str
    user: str


def _extract_wrapper_instructions(question_set_wrapper: str) -> str:
    """
    Return only the instructional text that appears ABOVE the
    --- QUESTION_BLOCK delimiter in the <question_set> content.

    If there is no delimiter (older MD format where questions came from
    the payload), the full wrapper text is returned unchanged.
    """
    lines = question_set_wrapper.splitlines()
    result_lines: list[str] = []
    for line in lines:
        if _BLOCK_DELIMITER_RE.search(line):
            break
        result_lines.append(line)
    return "\n".join(result_lines).strip()


def _format_question_block(q: QuestionBlock) -> str:
    return (
        f"<question_block>\n"
        f"  id: {q.id}\n"
        f"  label: {q.label}\n"
        f"  metric_def: {q.metric_def}\n"
        f"  counts_as: {q.counts_as}\n"
        f"  does_not_count: {q.does_not_count}\n"
        f"  fallback_rule: {q.fallback_rule}\n"
        f"</question_block>"
    )


def assemble_prompt(
    parsed: ParsedQuestionnaire,
    question: QuestionBlock,
    pageindex_summary: str,
) -> AssembledPrompt:
    """Compose the final prompt for a single question."""

    # Strip question blocks from the wrapper — only keep instructional text.
    wrapper_instructions = _extract_wrapper_instructions(parsed.question_set_wrapper)

    system_parts = [
        "# System Directive",
        parsed.system_directive,
        "",
        "# Traversal Instructions",
        TRAVERSAL_INSTRUCTIONS,
        "",
        "# Question Block — Field Usage",
        FIELD_USAGE_NOTE,
        "",
        "# PageIndex Orientation",
        pageindex_summary,
    ]
    system_text = "\n".join(system_parts)

    user_parts = [
        "# Question Set Context",
        wrapper_instructions,
        "",
        "# Current Question",
        _format_question_block(question),
        "",
        "# Output Schema",
        parsed.output_schema.strip(),
        "",
        "# Example",
        parsed.example.strip(),
        "",
        "# Confidence Scoring",
        parsed.confidence_scoring.strip(),
        "",
        "Begin. Use the tools to gather evidence, then call submit_answer.",
    ]
    user_text = "\n".join(user_parts)

    return AssembledPrompt(system=system_text, user=user_text)

