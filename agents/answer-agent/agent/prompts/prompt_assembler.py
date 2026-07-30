"""
Compose the final prompt for one question.

System prompt:
    1. Full MD file verbatim  (all instructions, schemas, examples — everything
       the author wrote, passed through without inspection)
    2. Traversal instructions (constant, baked into container image)
    3. PageIndex orientation  (auto-built from the loaded PageIndex)

User prompt:
    1. Current question raw text (verbatim from the MD file)
    2. Instruction to answer only this question

The assembler has zero knowledge of what sections or fields exist in the MD
file. Adding, removing, or renaming sections in the MD requires no code change.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from models.schemas import ParsedQuestionnaire, QuestionBlock

_HERE = Path(__file__).parent
TRAVERSAL_INSTRUCTIONS = (_HERE / "traversal_instructions.md").read_text(encoding="utf-8")


@dataclass
class AssembledPrompt:
    system: str
    user: str


def assemble_prompt(
    parsed: ParsedQuestionnaire,
    question: QuestionBlock,
    pageindex_summary: str,
) -> AssembledPrompt:
    """Compose the final system + user prompt for a single question."""

    system_parts = [
        parsed.full_md,
        "",
        "# Traversal Instructions",
        TRAVERSAL_INSTRUCTIONS,
        "",
        "# PageIndex Orientation",
        pageindex_summary,
    ]
    system_text = "\n".join(system_parts)

    user_parts = [
        "# Current Question",
        "Answer only the following question. Do not answer any other questions.",
        "",
        question.raw_text,
        "",
        "Begin. Use the tools to gather evidence, then call submit_answer.",
    ]
    user_text = "\n".join(user_parts)

    return AssembledPrompt(system=system_text, user=user_text)

