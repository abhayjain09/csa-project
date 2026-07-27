"""
Parse the questionnaire MD file into its six named sections.

The MD file follows a fixed structure — each section is wrapped in a
custom tag: <system_directive>, <question_set>, <output_schema>, <example>,
<confidence_scoring>, <pre_flight_validation>.

We extract by regex on the tag pairs. `question_set` is preserved as a raw
string (its wrapper/instructional text is kept for the assembler to reuse
around the injected block; the block itself comes from the payload).
"""
from __future__ import annotations

import re
from typing import Any

from models.schemas import ParsedQuestionnaire, S3Ref
from pdf.s3_client import S3Client

REQUIRED_SECTIONS = (
    "system_directive",
    "question_set",
    "output_schema",
    "example",
    "confidence_scoring",
    "pre_flight_validation",
)


def _extract_section(text: str, name: str) -> str | None:
    """Extract the innerText of <name>...</name>. Returns None if missing."""
    # DOTALL so the content can span newlines. Non-greedy match for content.
    pattern = rf"<{name}\b[^>]*>(.*?)</{name}>"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return None
    return m.group(1).strip()


class QuestionnaireParseError(ValueError):
    """Raised when the MD file is missing required sections."""


def parse_questionnaire_md(text: str) -> ParsedQuestionnaire:
    """Parse the MD text into a ParsedQuestionnaire.

    Reports ALL missing sections in one error, not just the first — makes
    fixing malformed inputs less tedious.
    """
    sections: dict[str, str | None] = {
        name: _extract_section(text, name) for name in REQUIRED_SECTIONS
    }
    missing = [name for name, val in sections.items() if val is None]
    if missing:
        raise QuestionnaireParseError(
            f"Questionnaire MD is missing required section(s): {missing}"
        )
    # mypy knows these are non-None after the missing check above.
    return ParsedQuestionnaire(
        system_directive=sections["system_directive"],  # type: ignore[arg-type]
        question_set_wrapper=sections["question_set"],  # type: ignore[arg-type]
        output_schema=sections["output_schema"],  # type: ignore[arg-type]
        example=sections["example"],  # type: ignore[arg-type]
        confidence_scoring=sections["confidence_scoring"],  # type: ignore[arg-type]
        pre_flight_validation=sections["pre_flight_validation"],  # type: ignore[arg-type]
    )


def load_questionnaire(
    source: str | S3Ref, s3: S3Client | None = None
) -> ParsedQuestionnaire:
    """Load and parse the questionnaire from either an inline string or S3."""
    if isinstance(source, S3Ref):
        if s3 is None:
            raise ValueError("S3Ref source provided but no S3Client given")
        text = s3.get_object_bytes(source.s3_uri).decode("utf-8")
    else:
        text = source
    return parse_questionnaire_md(text)
