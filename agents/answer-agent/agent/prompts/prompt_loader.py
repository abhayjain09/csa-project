"""
Load and parse the questionnaire MD file.

The MD file is the single source of truth. No section names are assumed.
The only structural contract with the MD file author is:

    1. Place --- QUESTION_BLOCK --- exactly once to mark where questions begin.
    2. Place --- END --- after the last question to mark where they end.
       If multiple --- END --- markers exist, the first one after
       --- QUESTION_BLOCK --- is used.
    3. Start each question with an 'id:' line (case-insensitive).

Everything above --- QUESTION_BLOCK --- is prompt instructions and flows
into the prompt verbatim. Everything between the two delimiters is parsed
into QuestionBlock objects. The delimiters themselves are case-insensitive
and require 3+ dashes on each side.
"""
from __future__ import annotations

import re

from models.schemas import ParsedQuestionnaire, S3Ref
from pdf.s3_client import S3Client
from utils.parse_question_set import parse_question_set_from_text

_QUESTION_BLOCK_RE = re.compile(r"-{3,}\s*QUESTION_BLOCK\s*-{3,}", re.IGNORECASE)
_END_RE = re.compile(r"-{3,}\s*END\s*-{3,}", re.IGNORECASE)


def parse_questionnaire_md(text: str) -> ParsedQuestionnaire:
    """
    Parse the full MD text into a ParsedQuestionnaire.

    Finds --- QUESTION_BLOCK --- and the first --- END --- after it,
    extracts the question section between them, and parses it into
    QuestionBlock objects. The full MD text is preserved as-is.

    Raises ValueError with a clear message for any structural problem.
    """
    # Locate --- QUESTION_BLOCK ---
    block_match = _QUESTION_BLOCK_RE.search(text)
    if not block_match:
        raise ValueError(
            "--- QUESTION_BLOCK --- not found in MD file. "
            "Add '--- QUESTION_BLOCK ---' to mark where questions begin."
        )

    # Locate first --- END --- after the block delimiter.
    end_match = _END_RE.search(text, pos=block_match.end())
    if not end_match:
        raise ValueError(
            "--- END --- not found after --- QUESTION_BLOCK --- in MD file. "
            "Add '--- END ---' after the last question."
        )

    # Slice the question section between the two delimiters.
    question_section = text[block_match.end():end_match.start()].strip()

    if not question_section:
        raise ValueError(
            "No content found between --- QUESTION_BLOCK --- and --- END ---. "
            "Add at least one question starting with 'id:'."
        )

    # Parse questions — raises ValueError on no questions or duplicate IDs.
    questions = parse_question_set_from_text(question_section)

    return ParsedQuestionnaire(
        full_md=text,
        questions=questions,
    )


def load_questionnaire(
    source: str | S3Ref,
    s3: S3Client | None = None,
) -> ParsedQuestionnaire:
    """Load and parse the questionnaire from either an inline string or S3."""
    if isinstance(source, S3Ref):
        if s3 is None:
            raise ValueError("S3Ref source provided but no S3Client given")
        text = s3.get_object_bytes(source.s3_uri).decode("utf-8")
    else:
        text = source
    return parse_questionnaire_md(text)

