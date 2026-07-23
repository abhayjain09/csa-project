"""
Post-hoc confidence check.

The model reports a confidence level in submit_answer. We separately compute
a *floor* from objective signals in the trace, and downgrade if the model
reported higher than the floor.

Signals:
- Citation count (0 → cap at insufficient_evidence)
- Cross-doc diversity (1 doc → cap at medium)
- Coverage of counts_as / does_not_count in the reasoning trace
- Whether fallback_rule fired
"""
from __future__ import annotations

from agent.session import Session
from models.schemas import ConfidenceBreakdown, ConfidenceLevel


_LEVEL_ORDER = {
    ConfidenceLevel.INSUFFICIENT: 0,
    ConfidenceLevel.LOW: 1,
    ConfidenceLevel.MEDIUM: 2,
    ConfidenceLevel.HIGH: 3,
}


def _cap(level: ConfidenceLevel, ceiling: ConfidenceLevel) -> ConfidenceLevel:
    return level if _LEVEL_ORDER[level] <= _LEVEL_ORDER[ceiling] else ceiling


def compute_confidence(
    session: Session,
    model_reported: str,
    reasoning: str,
) -> ConfidenceBreakdown:
    """Compute a floor and combine with the model's report."""
    try:
        reported = ConfidenceLevel(model_reported)
    except ValueError:
        reported = ConfidenceLevel.LOW

    reasons: list[str] = []
    floor = ConfidenceLevel.HIGH  # start optimistic, cap downward

    # 1. Citation count.
    n_cited = len(session.citations)
    if n_cited == 0:
        floor = _cap(floor, ConfidenceLevel.INSUFFICIENT)
        reasons.append("No citations recorded.")
    elif n_cited == 1:
        floor = _cap(floor, ConfidenceLevel.MEDIUM)
        reasons.append("Single citation — no corroboration.")

    # 2. Cross-doc diversity.
    docs_cited = {c.doc_name for c in session.citations}
    if len(docs_cited) == 1 and n_cited > 0:
        floor = _cap(floor, ConfidenceLevel.MEDIUM)
        reasons.append("All citations from a single document.")

    # 3. counts_as / does_not_count referenced in reasoning?
    q = session.question
    reasoning_lc = reasoning.lower()
    if q.counts_as and not _has_any_keyword(reasoning_lc, q.counts_as):
        reasons.append("Reasoning did not explicitly address counts_as criteria.")
        floor = _cap(floor, ConfidenceLevel.MEDIUM)
    if q.does_not_count and not _has_any_keyword(reasoning_lc, q.does_not_count):
        reasons.append("Reasoning did not explicitly address does_not_count criteria.")
        # Softer signal — do not cap, just flag.

    # 4. Fallback fired?
    if reported == ConfidenceLevel.INSUFFICIENT:
        floor = _cap(floor, ConfidenceLevel.INSUFFICIENT)
        reasons.append("fallback_rule invoked (confidence=insufficient_evidence).")

    # Final = min(reported, floor).
    final = reported if _LEVEL_ORDER[reported] <= _LEVEL_ORDER[floor] else floor
    downgraded = _LEVEL_ORDER[final] < _LEVEL_ORDER[reported]
    if downgraded:
        reasons.append(
            f"Downgraded from '{reported.value}' to '{final.value}' based on evidence signals."
        )

    return ConfidenceBreakdown(
        model_reported=reported,
        computed_floor=floor,
        final=final,
        reasons=reasons,
        downgraded=downgraded,
    )


def _has_any_keyword(text: str, spec: str) -> bool:
    """Coarse check: at least one word (>= 4 chars) from the spec appears in
    the reasoning text. Cheap heuristic, not a semantic match — the goal is
    to catch answers that ignored the spec entirely."""
    words = [w for w in spec.lower().split() if len(w) >= 4 and w.isalpha()]
    if not words:
        return True  # nothing meaningful to require
    return any(w in text for w in words)
