"""
Parse the question section of a questionnaire MD file into a list of
QuestionBlock objects.

The only structural assumption made here:
- Each question starts with a line beginning 'id:' (case-insensitive).
- Everything from one 'id:' line up to (but not including) the next 'id:'
  line is that question's raw_text.

No field names beyond 'id' are assumed or parsed. The raw_text of each
question is passed verbatim into the prompt — the model reads whatever
fields the author put there.

The caller (prompt_loader.py) is responsible for slicing out the question
section between --- QUESTION_BLOCK --- and --- END --- before calling
parse_question_set_from_text().
"""
from __future__ import annotations

import re

from models.schemas import QuestionBlock

# Matches a line that starts a new question block — case-insensitive.
_ID_LINE_RE = re.compile(r"^id\s*:\s*(.+)", re.IGNORECASE)


def parse_question_set_from_text(question_section: str) -> list[QuestionBlock]:
    """
    Parse the raw question section text into a list of QuestionBlock objects.

    Accepts only the text between --- QUESTION_BLOCK --- and --- END ---,
    not the full MD file.

    Raises ValueError if no questions are found or duplicate IDs exist.
    """
    lines = question_section.splitlines()

    # Collect (line_index, question_id) for every line that opens a new block.
    id_positions: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _ID_LINE_RE.match(line.strip())
        if m:
            id_positions.append((i, m.group(1).strip()))

    if not id_positions:
        raise ValueError(
            "No 'id:' lines found in question section. "
            "Each question must start with 'id: <value>'."
        )

    # Slice raw text between consecutive id: positions.
    questions: list[QuestionBlock] = []
    for idx, (line_start, question_id) in enumerate(id_positions):
        line_end = id_positions[idx + 1][0] if idx + 1 < len(id_positions) else len(lines)
        raw_text = "\n".join(lines[line_start:line_end]).strip()
        questions.append(QuestionBlock(id=question_id, raw_text=raw_text))

    # Duplicate ID check.
    ids = [q.id for q in questions]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for qid in ids:
        if qid in seen:
            duplicates.add(qid)
        seen.add(qid)
    if duplicates:
        raise ValueError(f"Duplicate question IDs found: {sorted(duplicates)}")

    return questions

