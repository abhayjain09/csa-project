# Interpreting the Question Block

The next section defines a single question. It has these fields — use them
in this specific way:

- **`id`** — Unique identifier. Include it in your final answer.
- **`label`** — The exact text to place in the "Question" column of your
  output. Do not rewrite or normalize it.
- **`metric_def`** — Your primary matching anchor during traversal.
- **`counts_as`** — Additional disclosures that satisfy the question.
  Treat these as *inclusions*: broaden search accordingly.
- **`does_not_count`** — Disclosures that superficially resemble the
  metric but don't qualify. Treat these as *exclusions*: any candidate
  answer matching these is invalid.
- **`fallback_rule`** — What to do if evidence is missing. Do not apply
  early; only invoke after reasonable exploration.
