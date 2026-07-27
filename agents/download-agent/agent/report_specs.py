"""report_specs.py — unified per-report-class specification layer (v40).

One place that answers, for every document class the agent handles:

  * year_required    — does _confident() require a year match for this class?
                       (True for periodic filings: annual report, proxy,
                       remuneration report; False for undated policies.)
  * registries       — which official registry can serve this class, and with
                       which form types. Tier 2 (registry_tier.py) reads this
                       to decide eligibility:
                         SEC EDGAR      -> annual report, proxy statement,
                                           sustainability report (best-effort)
                         Companies House-> annual report only
  * validation_prompt— a class-specific instruction injected into the
                       fail-closed LLM verifier (_llm_select_best). Supports
                       two placeholders: {company} and {year_clause}.
  * html_render_eligible — may the AgentCore Browser tier render a confirmed
                       HTML page to PDF and store THAT when no downloadable
                       file exists (some companies publish a policy as a
                       webpage, not a PDF)? True only for standalone company
                       policies where a page IS a legitimate substitute for a
                       document. False (default, deny-by-default) for any
                       class that must be an authoritative filed document —
                       an Annual Report, Proxy Statement, Sustainability/
                       Impact/GHG report, or Wolfsberg Questionnaire — where a
                       rendered snapshot is never an acceptable substitute for
                       the real filing.

This is intentionally a THIN metadata layer that sits ON TOP of the existing,
proven _DOC_CLASS_RULES / alias / reject machinery in agent.py — it does not
replace it. The alias tables still drive discovery/synonym expansion; this
file adds the per-class validation contract + registry routing.

Canonical class names MUST match the keys of _DOC_CLASS_RULES in agent.py.
"""

# ── EDGAR form types by class. "_fts_best_effort" is a sentinel meaning
#    "EDGAR has no dedicated form for this class; only attempt full-text
#    search when EDGAR_SUSTAINABILITY_FTS is enabled, otherwise fall through".
_EDGAR_ANNUAL = ["10-K", "20-F", "10-K405", "10-KSB", "40-F"]
# Proxy statement = the DEFINITIVE annual-meeting proxy only: DEF 14A.
# DEFA14A ("additional definitive materials") is a short supplemental filing,
# NOT the main proxy — including it caused the agent to grab the supplement
# instead of the real proxy (observed on Intuit, DaVita, Cisco: "downloaded the
# additional DEF, not the main proxy"). DEFM14A is a MERGER proxy, a different
# document that could out-rank the annual proxy by recency. edgar_lookup matches
# forms as a set and picks the most recent, so both are removed here rather than
# merely deprioritized. Re-add DEFM14A only if merger proxies become in-scope.
_EDGAR_PROXY = ["DEF 14A"]

REPORT_SPECS: dict[str, dict] = {
    "annual report": {
        "year_required": True,
        "registries": {"edgar": _EDGAR_ANNUAL, "companies_house": ["AA", "AAMD"]},
        "html_render_eligible": False,
        "validation_prompt": (
            "The document must BE a full-year Annual Report for {company} "
            "(acceptable equivalents: Form 10-K, Form 20-F, Annual Report and "
            "Accounts, Integrated Annual Report){year_clause}. A Board's Report, "
            "Directors' Report, a quarterly (10-Q), an 8-K / current report, an "
            "interim/half-year report, or an ESG-only supplement is NOT a match. "
            "For a corporate-group request, a report limited to one subsidiary, "
            "country, site, facility, mine, plant, project, or operation is NOT "
            "the group's Annual Report."
        ),
    },
    "sustainability report": {
        "year_required": False,
        # EDGAR has no standard sustainability form -> best-effort only.
        "registries": {"edgar": ["_fts_best_effort"]},
        "html_render_eligible": False,
        "validation_prompt": (
            "The document must BE a standalone Sustainability / ESG / BRSR / "
            "CSRD-ESRS report for {company}. A Strategic Report, an Annual "
            "Report, an ESG factbook or supplement, a CDP score report, a "
            "green/SDG-bond report, an assurance statement, or a standalone "
            "Impact Report is NOT a match. For a corporate-group request, "
            "reject a sustainability report limited to one subsidiary, "
            "country, region, site, facility, mine, plant, project, or "
            "operation."
        ),
    },
    "impact report": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": False,
        "validation_prompt": (
            "The document must BE a standalone Impact Report (social/purpose/"
            "ESG impact report) for {company}. A full Sustainability/ESG "
            "report, an Annual Report, or an ESG databook/spreadsheet is NOT "
            "a match unless it is explicitly titled and structured as an "
            "Impact Report."
        ),
    },
    "ghg emission report": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": False,
        "validation_prompt": (
            "The document must BE {company}'s standalone GHG/Greenhouse Gas "
            "Emissions report (Scope 1/2/3 emissions inventory, carbon "
            "footprint report). A Sustainability Report or Annual Report "
            "that merely contains an emissions section or ESG databook entry "
            "is NOT a match unless no standalone GHG report exists."
        ),
    },
    "proxy statement": {
        "year_required": True,
        "registries": {"edgar": _EDGAR_PROXY},
        "html_render_eligible": False,
        "validation_prompt": (
            "The document must BE the full definitive annual-meeting Proxy "
            "Statement (SEC Form DEF 14A) for {company}{year_clause} — the "
            "complete proxy with the notice of meeting, proposals, and board/"
            "compensation disclosures. REJECT 'Definitive Additional Materials' "
            "/ DEFA14A, any short soliciting or supplemental filing, a "
            "supplement or amendment to the proxy, a preliminary proxy "
            "(PRE 14A/PRER14A), a merger proxy (DEFM14A), an annual report, or "
            "a 10-K. A short document (a handful of pages) that references or "
            "supplements the proxy is NOT the proxy itself."
        ),
    },
    "remuneration report": {
        "year_required": True,
        "registries": {},
        "html_render_eligible": False,
        "validation_prompt": (
            "The document must BE a Directors' Remuneration Report for "
            "{company}{year_clause}. A full Annual Report that merely contains "
            "a remuneration section is only acceptable if no standalone "
            "remuneration report is available."
        ),
    },
    "code of conduct": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s official Code of Conduct / Code of "
            "Business Conduct and Ethics. It may apply company-wide or to the "
            "Board, senior management, executive leadership and corporate "
            "officers. A Supplier/Vendor Code of Conduct, a code limited only "
            "to non-executive or independent directors, a director appointment "
            "or familiarisation document, or a governance overview page is NOT "
            "a match."
        ),
    },
    "supplier code of conduct": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Supplier / Vendor / Third-Party "
            "Code of Conduct (or Responsible Sourcing / Supply Chain code). The "
            "company's general employee Code of Conduct is NOT a match."
        ),
    },
    "tax strategy and governance": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Tax Strategy / Tax Policy / Tax "
            "Governance document. A general annual report tax note is NOT a match."
        ),
    },
    "whistleblowing mechanism": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Whistleblowing / Speak-Up / "
            "Whistleblower policy. A document that merely mentions a "
            "whistleblowing channel in a section is NOT a match."
        ),
    },
    "occupational health & safety policy": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Occupational Health & Safety "
            "(OHS/HSE/HSSE) policy. A sustainability report section on safety "
            "is NOT a match."
        ),
    },
    "environmental policy": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Environmental / Environmental "
            "Management policy. A sustainability report, ESG supplement, or CDP "
            "report is NOT a match."
        ),
    },
    "insider trading policy": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Insider Trading / Securities "
            "Trading / Share Dealing policy governing employee/director "
            "trading in company securities. A general Code of Conduct that "
            "merely mentions insider trading in a section is NOT a match "
            "unless no standalone policy exists."
        ),
    },
    "anti-bribery and corruption policy": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Anti-Bribery & Corruption (ABC) "
            "policy. A code of conduct that mentions bribery in a section is "
            "NOT a match unless no standalone ABC policy exists."
        ),
    },
    "conflicts of interest policy": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Conflicts of Interest policy. A "
            "code of conduct or ethics policy that merely mentions conflicts "
            "of interest in a section is NOT a match unless no standalone "
            "policy exists."
        ),
    },
    "discrimination and harassment policy": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Discrimination and Harassment / "
            "Anti-Discrimination / Anti-Harassment / Equal Opportunity policy. "
            "A general code of conduct or D&I report is NOT a match unless no "
            "standalone policy exists."
        ),
    },
    "biodiversity policy": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Biodiversity / Nature policy. A "
            "sustainability report section on biodiversity is NOT a match "
            "unless no standalone policy exists."
        ),
    },
    "human rights policy": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Human Rights policy or "
            "statement. A Human Rights Due Diligence report, or a "
            "sustainability report section on human rights, is NOT a match."
        ),
    },
    "human rights due diligence": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Human Rights Due Diligence "
            "report or human rights impact assessment. A general Human "
            "Rights policy statement with no due-diligence process/findings "
            "is NOT a match."
        ),
    },
    "risk management policy": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": True,
        "validation_prompt": (
            "The document must BE {company}'s Risk Management policy or "
            "Enterprise Risk Management framework. An annual report's risk "
            "factors section is NOT a match unless no standalone policy "
            "exists."
        ),
    },
    "wolfsberg questionnaire": {
        "year_required": False,
        "registries": {},
        "html_render_eligible": False,
        "validation_prompt": (
            "The document must BE {company}'s completed Wolfsberg Group "
            "Correspondent Banking Due Diligence Questionnaire (CBDDQ) or "
            "equivalent Wolfsberg AML questionnaire. A general AML/KYC "
            "policy that is not the Wolfsberg questionnaire itself is NOT a "
            "match."
        ),
    },
}

_GENERIC_SPEC = {
    "year_required": False,
    "registries": {},
    "validation_prompt": "",
    "html_render_eligible": False,
}


def spec_for(canonical: str | None) -> dict:
    """Return the spec for a canonical class, or a permissive generic default."""
    return REPORT_SPECS.get((canonical or "").strip().lower(), _GENERIC_SPEC)


def year_required(canonical: str | None) -> bool:
    return bool(spec_for(canonical).get("year_required"))


def registries_for(canonical: str | None) -> dict:
    return spec_for(canonical).get("registries", {}) or {}


def html_render_eligible(canonical: str | None) -> bool:
    """True if a confirmed HTML page may be rendered to PDF and stored as this
    class's document when no downloadable file exists. Deny-by-default: only
    standalone company policies where a webpage is a legitimate substitute for
    a document opt in (see module docstring)."""
    return bool(spec_for(canonical).get("html_render_eligible"))


def validation_prompt(canonical: str | None, company: str = "", year=None) -> str:
    """Render the class validation prompt with {company}/{year_clause} filled."""
    tmpl = spec_for(canonical).get("validation_prompt", "")
    if not tmpl:
        return ""
    year_clause = ""
    if year:
        year_clause = f" for fiscal/reporting year {year}"
    return (tmpl
            .replace("{company}", company or "the named company")
            .replace("{year_clause}", year_clause))
