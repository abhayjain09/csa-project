# Playwright CLI + Skills — Local Setup for Report-Site Recon

Goal: use Claude Code + `@playwright/cli` on your personal machine to interactively
explore an official company site (and its sub-URLs) that the download-agent is
currently failing on, and produce a **portable recon report** you can turn into
a code change in `agent.py` / `browser_worker.py`. This is a dev-time exploration
tool, not a replacement for the production browser tier.

---

## 1. One-time install

```bash
node --version   # need v18+
npm --version
git --version
```

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

First run of `claude` will prompt you to log in (browser flow via your Claude
Pro/Max/Team account, or an API key).

```bash
mkdir -p ~/report-site-recon && cd ~/report-site-recon
npm init -y
npm install -D @playwright/test
npm install -g @playwright/cli@latest
playwright-cli --version
```

```bash
playwright-cli install --skills
playwright-cli install-browser
```

Verify:

```bash
ls .claude/skills/playwright-cli/            # SKILL.md, references/
playwright-cli open https://www.google.com --headed   # close the browser after
```

Skills install **project-local** (`<project>/.claude/skills/`) — start `claude`
from inside `~/report-site-recon` every time so it discovers them.

---

## 2. Drop in the verification-bar context

The download-agent's whole design is **fail-closed**: it only stores a document
when it's highly confident it's the right one for the right company. If your
local Claude Code session doesn't know these rules, it'll happily accept
documents the real agent would reject — making the recon useless. Save this as
`~/report-site-recon/CLAUDE.md`:

```markdown
# Context: EDO Co-Analyst Download Agent — report verification bar

I am recon-ing an official company website for the production download-agent
(agents/download-agent/agent/agent.py). The agent downloads one of 23 report
classes per company. When I explore a site, I am looking for:

- Which sub-URL(s) actually host the document for the target report class
  (investor relations, sustainability/ESG hub, governance/policies page,
  SEC-filings index, press-releases page — note which).
- The exact click/nav path to reach a real, direct document link (not just
  a "Reports" landing page).
- Whether the real document is a downloadable file (PDF/DOC) or an
  extensionless HTML route that renders a document inline.
- Whether the page is a JS-rendered SPA shell (empty on first static fetch,
  populated after client-side render) vs. plain server-rendered HTML.
- Any WAF / bot-challenge / CAPTCHA page encountered, and at which exact URL.
- Whether reaching the document required first visiting a "referer" landing
  page (cookies/session established there) before the direct URL worked.

## The 23 report classes (must match exactly)
annual report, code of conduct, anti-bribery and corruption policy,
conflicts of interest policy, insider trading policy, discrimination and
harassment policy, supplier code of conduct, whistleblowing mechanism,
sustainability report, ghg emission report, environmental policy,
environment health & safety policy, biodiversity policy, impact report,
human rights policy, human rights due diligence, modern slavery statement,
remuneration report, proxy statement, risk management policy, tax strategy
and governance, wolfsberg questionnaire, occupational health & safety policy

## What counts as a match (do NOT accept anything weaker)
- Content is the primary evidence, not the filename/title. A vague filename
  with clearly matching content is fine; a good filename with wrong content
  is not.
- The company's own name (as tokens) must appear in the document's visible
  text — a document that never mentions the company by name is rejected
  even if everything else looks right.
- The newest-dated version wins when multiple years are available.
- A subsidiary/country/site/facility-only report does NOT satisfy a
  group-wide class request (annual report, sustainability report, etc.).
- A quarterly/interim/8-K/10-Q is NOT an annual report. A DEFA14A/DEFM14A is
  NOT the definitive annual proxy (DEF 14A only).
- Non-English documents are rejected when English is requested.
- For annual report / proxy statement / remuneration report, filing-index
  pages (SEC EDGAR-style) are in-scope; for other classes, prefer
  sustainability/policy pages over press-release or filing-index pages.

## What I am NOT doing here
- Not writing production Python code in this session.
- Not submitting any real form data — read-only exploration only.
- Not trying to bypass a CAPTCHA or WAF challenge — if one appears, record
  where it appeared and stop; that is itself a useful recon finding.
```

---

## 3. The Planner prompt (fill in the blanks, run in `claude`)

```text
Act as the Planner from the playwright-cli test-generation skill, using the
rules in CLAUDE.md.

Target company: <COMPANY NAME>
Official domain: <DOMAIN, e.g. example.com>
Report class to find: <ONE OF THE 23 CLASSES ABOVE>
Known symptom from production: <e.g. "no_document_found" / "blocked_by_source_waf"
  on https://example.com/investors/annual-report / "downloaded the wrong
  document — a quarterly report instead of the annual report">

Explore https://<DOMAIN> and its sub-URLs using playwright-cli to find the
correct <REPORT CLASS> document.

Rules:
- Read-only exploration — do not submit forms with real data
- Do not write any code
- Take a snapshot after every navigation; note element refs for anything
  that looks like a document link, "Reports"/"Investors"/"Sustainability"
  nav item, or year selector
- If you hit a WAF/bot-challenge/CAPTCHA page, record the exact URL and stop
  probing that URL — do not try to bypass it
- Try the most likely sub-URLs first: /investors, /investor-relations,
  /sustainability, /esg, /about/governance, /policies, /sec-filings,
  /financial-reports, /newsroom (deprioritize this one)
- Save findings as a numbered Markdown file at
  recon/<company-slug>-<report-class-slug>.md with these sections:
  1. Sub-URL(s) that host the document, with the exact path
  2. Click/nav sequence to reach the direct document link from the homepage
  3. Document delivery type: downloadable file vs. extensionless HTML route
     vs. requires JS render
  4. Any WAF/bot-challenge encountered (URL + what triggered it)
  5. Whether a referer/landing-page visit was needed before the direct URL
     worked
  6. A recommended one-line change for agent.py's alias table or crawl
     priority (e.g. "add '/sustainability-hub' to the sitemap-priority list
     for this domain" or "this domain needs SITE_FIRST_WHEN_LATEST_CLASSES
     treatment for annual report")
```

---

## 4. The Healer prompt (when the Planner run gets stuck/blocked)

```text
Act as the Healer from the playwright-cli test-generation skill.

The exploration in recon/<company-slug>-<report-class-slug>.md stalled at:
<paste the point where it got blocked/confused>

Diagnose using the healing discipline:
1. Re-open the last successful URL and take a fresh snapshot
2. Classify what happened: WAF/bot-challenge, JS-rendered shell with no
   static content, redirect loop, or a genuinely missing document on this
   site
3. If it's a WAF/bot-challenge: do NOT retry the same raw URL repeatedly.
   Try navigating via the landing page first (establish referer/cookies),
   then retry the direct URL once. If still blocked, stop and record it as
   a WAF-blocked finding — this mirrors the production browser_worker's
   circuit breaker (it also gives up after repeated blocks and returns a
   manual-download candidate instead of hammering the domain).
4. If it's a JS-rendered shell: note that a static crawl (agent.py Tier 3)
   would need a render+harvest probe here, same as the render fallback
   already used before full browser navigation.
5. Update recon/<company-slug>-<report-class-slug>.md with the resolution
   or the confirmed dead end.

Do not attempt to bypass any CAPTCHA or bot-challenge.
```

---

## 5. Three concrete cases to recon first

These came from Google `site:` searches — each surfaced a PDF that talks about
the target topic but is very likely **not** the standalone document the class
requires. That distinction (umbrella code-of-conduct document containing a
section vs. a real standalone policy; an unrelated adjacent document vs. the
actual report) is exactly what a static Google snippet can't tell you and a
browser-navigated recon of the real site can. Do not treat the PDF URL below
as confirmed correct — verify it in-browser against the company's own
governance/sustainability pages before deciding it's a match or a miss.

### Case A — eHealth, Conflicts of Interest Policy
- Search surfaced: `https://s204.q4cdn.com/837903328/files/doc_governance/2026/Mar/03/Code-of-Business-Conduct-amended-12-16-2025-2-20-26-HQ-address-update-4fe3fb.pdf`
  — titled **"Code of Business Conduct"**, not "Conflicts of Interest Policy".
  Q4CDN (`s204.q4cdn.com`) is a shared IR-hosting CDN, not the company's own
  domain — expect it to be linked from `ehealthinsurance.com`'s or
  `ir.ehealth.com`'s governance page, not on `q4cdn.com` itself.
- Open in browser: `https://ehealthinsurance.com` → find the investor
  relations / corporate governance nav item (likely redirects to a
  `ir.ehealth.com` or similar IR subdomain) → look for a governance documents
  page and confirm whether:
  1. A standalone "Conflicts of Interest Policy" exists as its own document, or
  2. The Code of Business Conduct is the only source and contains a genuine,
     substantive Conflicts of Interest section (not a passing mention) —
     which is the only condition under which the umbrella doc is an
     acceptable alias for this class.
- Fill into the Planner template: company=`eHealth, Inc.`, domain=`ehealthinsurance.com`
  (check IR subdomain), class=`conflicts of interest policy`, symptom=
  "search returns the Code of Business Conduct instead of a standalone policy —
  confirm whether a standalone doc exists and whether the umbrella doc's
  section is substantive enough to alias".

### Case B — JFrog, Anti-Corruption and Bribery Policy
- Search surfaced: `https://s21.q4cdn.com/528621000/files/doc_downloads/1/FINAL-JFrog-Global-Code-of-Business-Conduct-and-Ethics-approved-2-11-2025-docx-a623d1.pdf`
  — titled **"JFrog Global Code of Business Conduct and Ethics"**, same
  umbrella-document pattern as Case A, also on Q4CDN.
- Open in browser: `https://jfrog.com` → investor relations / governance page
  → confirm whether a standalone anti-corruption/anti-bribery policy exists
  separately, or whether the Code of Conduct's anti-corruption section is the
  only source and is substantive.
- Fill into the Planner template: company=`JFrog Ltd.`, domain=`jfrog.com`,
  class=`anti-bribery and corruption policy`, symptom= same umbrella-doc
  question as Case A.

### Case C — Micron, GHG Emission Report
- Search surfaced: `https://sg.micron.com/content/dam/micron/global/public/programs/sustainability/documents/2026-micron-assurance-statement.pdf`
  — titled **"2026 Micron Assurance Statement"**. This is a third-party
  verification/assurance letter ABOUT Micron's emissions data, not the GHG
  report itself — per `report_specs.py`'s own validation rule for this class,
  a document that merely references emissions data without being the
  standalone GHG/Scope 1-2-3 report is NOT a match. This one looks like a
  likely miss, not an alias case.
- Open in browser: `https://www.micron.com/about/our-commitment/sustainability`
  (or the site's sustainability hub) → look for a dedicated GHG/Scope 1-2-3
  emissions report or a substantive emissions section inside the
  Sustainability Report — the Assurance Statement should NOT be the final
  answer.
- Fill into the Planner template: company=`Micron Technology, Inc.`,
  domain=`micron.com`, class=`ghg emission report`, symptom=
  "search only surfaces an Assurance Statement (not the report itself) —
  find the real standalone GHG report or the Sustainability Report's
  emissions section".

## 6. Porting the result back

The recon markdown file is the deliverable — it's what you bring back to this
repo. It should let you (or me) make a targeted, reviewed change to one of:

- `agents/download-agent/agent/agent.py` — `_DOC_CLASS_RULES` alias table,
  `SITE_FIRST_WHEN_LATEST_CLASSES`, or Tier 3/4 crawl-priority lists
- `co-analyst-application/app/backend/browser_worker.py` — WAF circuit
  breaker thresholds, or URL seeds fed to the Bedrock planner for that domain

Nothing from the local Claude Code / Playwright CLI session gets deployed —
it only produces the markdown findings that justify the actual code change.