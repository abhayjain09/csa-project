# How to Traverse the PageIndex

You have access to a hierarchical index of the company's disclosure documents.
Each document has been broken into a nested `structure` of nodes; each node
carries a `title`, a short `summary`, and a physical page range
(`start_index` to `end_index`, inclusive, 1-indexed).

Actual page text is fetched on demand — the index itself only contains
structural metadata, not full text. Use the outline to navigate, then read
pages only when a node looks promising.

## Traversal Procedure

Follow this order for every question. Deviate only with a stated reason in
your Thought.

**1. Orient.** Start with `list_documents` to see what's available. Pick the
1–3 documents whose top-level titles best match the metric's *domain*. State
in your Thought which docs you selected and why.

**2. Survey.** For each candidate, call `get_outline` with depth=1. Rank
sections by matching their `title + summary` against the question spec (see
"Question Spec Fields" below). Do NOT fetch pages yet — outline reasoning is
cheap and less distracting.

**3. Drill.** For the top 1–2 candidate sections, call `expand_node` to see
their children. Continue drilling until you're looking at a section that
plausibly contains a direct answer.

**4. Read.** Call `fetch_pages` on the tightest range that likely contains
the answer. MAX 15 pages per call — if the section is larger, drill down
first. Read against the question spec carefully.

**5. Cite.** If the pages contain the answer, call `record_citation` with
the tightest page range and a short verbatim quote (< 300 characters).
Every citation must reference pages you actually fetched. Do not paraphrase
in `quoted_span` — it must be a substring of the page text.

**6. Backtrack if needed.** If pages don't contain the answer, try a
sibling node, then a different section, then a different document. Do NOT
keep fetching new page ranges from the same dead-end area.

**7. Terminate.** Call `submit_answer` with your final payload, citation
IDs, confidence level, and reasoning.

## Question Spec Fields — How to Use Them

Every question gives you four semantic fields. They serve different roles
during traversal:

- **`metric_def`** — What the metric fundamentally is. This is your primary
  anchor for matching against node titles and summaries.
- **`counts_as`** — Inclusion criteria. Expands your search surface —
  disclosures matching these also qualify, even if they don't literally
  use the metric name. Consider synonyms and related disclosures.
- **`does_not_count`** — Exclusion criteria. Actively deprioritize sections
  that match these during ranking. If a page's answer falls under
  `does_not_count`, it is NOT a valid citation.
- **`fallback_rule`** — Governs the terminal decision, not traversal.
  Apply this only after reasonable exploration failed to find an answer.

In every Thought, state which of these four fields is driving your current
action. This makes reasoning auditable and prevents over-indexing on
`metric_def` alone.

## Budget and Termination Rules

- You have a limited tool-call budget. The system will warn you when 80%
  is used. Converge quickly after that.
- If you make two `fetch_pages` calls in a row without recording a
  citation, the system will nudge you to backtrack or submit.
- "Reasonable exploration" before invoking `fallback_rule` means: at least
  2 documents examined (if 2+ exist), at least 3 sections opened, and no
  further plausible branches visible in the outlines.
- Never submit without either citations OR an explicit
  `insufficient_evidence` confidence level. Citation-free "high confidence"
  answers are rejected.

## Anti-patterns to Avoid

- Do not `fetch_pages` before reading the outline.
- Do not fetch large page ranges "just to see" — the 15-page cap is a
  ceiling, not a target; aim for 2–5 pages per fetch.
- Do not cite a page you haven't fetched.
- Do not paraphrase in `quoted_span` — it must be verbatim.
- Do not repeat the same tool call with the same arguments.
- Do not stop at the first plausible section without confirming its
  contents against `counts_as`/`does_not_count`.
