How the Answer was created

Input
items:[
  {
    "content": "Open capitalone.com and locate the Anti-Corruption and Bribery Policy page/document",
    "priority": "high"
  },
  {
    "content": "Extract key policy details (scope, prohibitions, gifts/hospitality, third parties, reporting) with citations",
    "priority": "high"
  },
  {
    "content": "Deliver cited summary and direct link(s) to the policy",
    "priority": "high"
  }
]
Output
status:ok
items:[
  {
    "id": "1",
    "content": "Open capitalone.com and locate the Anti-Corruption and Bribery Policy page/document",
    "status": "in_progress",
    "priority": "high"
  },
  {
    "id": "2",
    "content": "Extract key policy details (scope, prohibitions, gifts/hospitality, third parties, reporting) with citations",
    "status": "pending",
    "priority": "high"
  },
  {
    "id": "3",
    "content": "Deliver cited summary and direct link(s) to the policy",
    "status": "pending",
    "priority": "high"
  }
]
is_complete:No
reason:3 items still not done: "Open capitalone.com and locate the Anti-Corruption", "Extract key policy details (scope, prohibitions, g", "Deliver cited summary and direct link(s) to the po"
message:Created todo list with 3 items.

Input
bundle:core
group_key:playwright
Output
status:activated
group_key:playwright
label:Browser / Playwright
bundle:core
activation_entry:playwright:core
available_tool_names:[
  "browser_navigate",
  "browser_navigate_back",
  "browser_navigate_forward",
  "browser_snapshot",
  "browser_click",
  "browser_type",
  "browser_evaluate",
  "browser_wait_for",
  "browser_close",
  "browser_upload_to_s3",
  "browser_page_save_as_pdf",
  "browser_save_as_html"
]
contains_long_running_tools:No
additional_available_bundles:[
  "forms",
  "diagnostics",
  "page_control",
  "smart_extract"
]
remaining_inactive_groups:[
  "document_processing",
  "crawl4ai",
  "code_execution"
]
active_tool_groups:[
  "google_search",
  "playwright:core"
]
persisted:Yes
message:Tool group playwright activated with bundle core. Continue now with: browser_navigate, browser_navigate_back, browser_navigate_forward, browser_snapshot, browser_click, browser_type, browser_evaluate, browser_wait_for, browser_close, browser_upload_to_s3, browser_page_save_as_pdf, browser_save_as_html

Next: call load_tool_guide(guide_key='browser-playwright') before using this bundle's concrete tools.
related_skills:[
  "data-extraction-strategy",
  "playwright-data-extraction",
  "playwright-web-inspection"
]
skill_hint:IMPORTANT: Before starting this browser task, load the relevant skill for guidance. Use list_skills() then load_skill() for: 'data-extraction-strategy' for choosing the right extraction approach, 'playwright-data-extraction' for structured data extraction from live pages, 'playwright-web-inspection'…
after_tool_callback_injected:[
  "browser_navigate",
  "browser_navigate_back",
  "browser_navigate_forward",
  "browser_snapshot",
  "browser_click",
  "browser_type",
  "browser_evaluate",
  "browser_wait_for",
  "browser_close",
  "browser_upload_to_s3",
  "browser_page_save_as_pdf",
  "browser_save_as_html"
]
agent_tool_names:[
  "file_write",
  "file_read",
  "file_read_lines",
  "file_list",
  "file_move_to_s3",
  "file_query_json",
  "file_search",
  "load_tool_guide",
  "todo_create",
  "todo_item_done",
  "todo_add_items",
  "todo_update_item",
  "todo_get",
  "list_tool_groups",
  "activate_tool_group",
  "mark_task_complete",
  "google_search",
  "browser_navigate",
  "browser_navigate_back",
  "browser_navigate_forward",
  "browser_snapshot",
  "browser_click",
  "browser_type",
  "browser_evaluate",
  "browser_wait_for",
  "browser_close",
  "browser_upload_to_s3",
  "browser_page_save_as_pdf",
  "browser_save_as_html"
]

Input
guide_key:browser-playwright
Output
guide_key:browser-playwright
title:Browser / Playwright
content:# Browser / Playwright Guide

## When to use FIRST

- **The user provides a specific URL or domain** (e.g. "go to oilprice.com") — always start with `browser_navigate` directly. Do not search for it with Google Search or Link Discovery first.
- The user says "go to", "visit", "open", or "check" a specific site.

## What browser tools return

Every `browser_navigate`, `browser_click`, `browser_snapshot`, etc. returns:

1. **Page Text** — readable text content (from `innerText`). Contains all visible text: headings, paragraphs, link text, prices, dates. This is your primary data source.
2. **Interactive Elements** — flat list of clickable/typeable elements with `[ref]` identifiers and URLs. Use these refs for `browser_click`, `browser_type`, etc.
3. **ARIA snapshot on S3** — full accessibility tree saved as `snapshot_artifact`. Only fetch this when you need DOM-level detail not visible in the page text.

**Read the page text first.** Most questions can be answered from the page text alone without navigating to sub-pages or fetching the full ARIA snapshot.

## Hard boundaries

- Never bypass or assist with CAPTCHA, Cloudflare/Turnstile checks, "verify you are human" flows, or similar anti-bot controls.
- If a site presents bot verification, login-only access, or comparable gating, stop that automation path and report the restriction.

## Core workflow

1. `browser_navigate` to the target URL
2. Read the returned **Page Text** and **Interactive Elements**
3. If the answer is in the page text, respond immediately — do not navigate further
4. If you need to interact, use `[ref]` identifiers from Interactive Elements
5. Take a fresh `browser_snapshot` only after page state changes

## Content-first extraction

**Before navigating to detail pages, check whether the listing page already has the data.**

Example: if asked for "top 5 company news from oilprice.com", the listing page already shows headlines, dates, and summaries. Extract from the page text directly. Only visit detail pages if the user specifically asks for full article content.

## Tab management

- `browser_navigate` reuses the current tab by default — it does NOT open a new tab.
- Use `browser_tab_new(url=...)` only when you need two pages open simultaneously for comparison.
- **Maximum 2 tabs** open at any time. Close tabs immediately after extraction with `browser_tab_close`.
- **Never send parallel `browser_navigate` calls** — they queue serially at the server. Send them one at a time.
- Prefer single-tab sequential browsing: navigate → extract → navigate → extract.

## When browser outputs are truncated

If the page text is very large, it gets truncated with a note like `[Page text truncated]`.

- The `snapshot_artifact` field contains a file reference to the full ARIA snapshot on S3.
- Use `file_read(filename='...')` or `file_search(query='...', filename='...')` to inspect the full snapshot.
- Use element-scoped `browser_evaluate(function='(el) => ...', ref='...')` for targeted DOM extraction on a known element.
- Prefer `browser_evaluate` over fetching the full snapshot when you know which element you need.

## Dropdowns and forms (`<select>` / combobox)

1. `browser_snapshot` already shows all `<option>` elements inside each `combobox`. Read the options directly from the snapshot — do NOT use `browser_evaluate` to extract option values unless the snapshot is missing information.
2. Use `browser_select_option` with `values=["OPTION_TEXT"]` using the exact text you see in the snapshot options. You can also use `labels=["Visible Text"]` if value attributes differ from visible text.
3. **CRITICAL:** After every `browser_select_option`, call `browser_snapshot` to wait for the page to update. Many dropdowns trigger AJAX calls that dynamically reload other parts of the page. Never select the next dropdown until you have confirmed the page has updated via a fresh snapshot.
4. For cascading dropdowns (Province → City → ...): select the parent, take a snapshot, read the now-populated child options from the snapshot, then select the child. Always snapshot between each selection.

## `browser_evaluate`

Use `browser_evaluate` for targeted DOM or JS tasks:

- Extracting data not visible in page text (hidden attributes, computed styles)
- Reading data attributes or option values
- Extracting data from the current page's DOM in a single call (do NOT use this to loop through many pages — use crawl4ai instead)
- Replaying API calls discovered through network inspection

Do not guess hidden globals or write speculative JavaScript.

## Pattern discovery before bulk extraction

Before extracting many items, identify the fastest path:

1. Check if the listing page text already has the data
2. API replay from `browser_network_requests`
3. URL-parameterized filtering
4. Reusable DOM extractor via `browser_evaluate`
5. Detail-page traversal only if necessary

If the job is large and you skip this discovery step, tool usage will become inefficient.

## Crawl4ai escalation (MANDATORY)

Browser tools are for **probing and small-scale extraction**. Hard page limits:

- **≤ 3 pages**: browser extraction is fine.
- **4+ pages of uniform structure**: you MUST switch to `crawl4ai`. Use the browser probe to capture the URL pattern (e.g. `?page=N`, `?pageNumber=N`), then activate the crawl4ai tool group and use `crawl_many_urls` to batch-fetch all pages.
- **Never loop through 4+ pages** with `browser_navigate` / `browser_evaluate`. This wastes tool budget exponentially.

Typical escalation workflow:
1. Browser probe: navigate to page 1, extract sample data, discover pagination URL pattern and total page count.
2. Activate crawl4ai: `activate_tool_group(group_key='crawl4ai')`.
3. Generate URL list: `[base_url + '?page=' + str(i) for i in range(1, total_pages+1)]`.
4. Batch crawl: `crawl_many_urls(urls=[...])` with any extraction params discovered during probing.
5. Process results with `file_query_json` or `execute_code`.

## Smart Extract V3 bundle

`browser_smart_extract` is an autonomous long-running tool that probes the website, discovers its structure, then crawls, paginates, and extracts data — all without manual intervention.

- `playwright:smart_extract` is a non-default bundle.
- Invocation hint: `activate_tool_group(group_key='playwright', bundle='smart_extract')`

### When to use browser_smart_extract

Use this tool (without the user asking) when ANY of these apply:

- Pagination exists on the page
- User wants all/many items (>20, "all", "every", "complete list", "export")
- Data spans multiple pages
- Large repeated structured data (tables, listings, catalogs)
- Multi-level hierarchies (listing → detail pages)

For small extractions (single page, few items, no pagination) use standard playwright instead.

### SINGLE-BROWSER CONSTRAINT (ABSOLUTE)

The playwright service uses ONE shared browser instance. NEVER schedule more than one `browser_smart_extract` call in a single response. Calling two at the same time will cause both to immediately fail.

If you need to extract data from multiple URLs, pass ALL the URLs in a single call's `urls` array — not one call per URL.

### NO PROBING REQUIRED

Do NOT call `browser_navigate` or `browser_snapshot` before `browser_smart_extract`. The worker probes the site internally — it navigates pages, discovers page structure, identifies data hierarchies, and handles interactions automatically.

Just pass the starting URL(s) and a clear goal describing what to extract.

### Example call

```python
browser_smart_extract(
    urls=["https://example.com/funds"],
    goal="Extract all fund holdings across all trusts, categories, and funds — including trust_name, category_name, fund_name, security_name, ticker, cusip, shares, market_value, weight",
    max_pages=100,
    output_file_name="fund_holdings.json"
)
```

### Key guidelines

- **Descriptive goal**: Be specific about what data you want. List the exact fields in the goal so the worker knows what to extract.
- **AUTO-PAGINATION**: The tool automatically detects URL-based pagination and infinite scroll. Just pass the first page URL and set `max_pages` to cap processing.
- **AUTO-PROBING**: The worker navigates the site, discovers page levels (listing → detail → data), identifies interactive elements (buttons, dropdowns), and builds an extraction plan automatically.
- **Multi-level support**: The worker handles nested hierarchies (e.g., trusts → categories → funds → holdings) without manual intervention.
- **Downloads**: If the goal mentions downloading files, the worker handles download actions internally. Results contain `downloaded_file` (S3 URI) and `filename` instead of extracted fields.

## Debugging

Use:

- `browser_snapshot`
- `browser_network_requests`
- `browser_console_messages`
- screenshots when needed

Isolate failures with the simplest possible checks before trying complex extraction logic.

## Downloading files and documents (PDFs, HTM/HTML, DOCX, etc.)

When you need to download a file from the web and process it:

1. **Preferred: `browser_upload_to_s3(source='<url>')`** — Downloads the raw file binary via the browser's authenticated context (cookies, user-agent) and uploads to S3. Works for any file type (PDF, HTM, HTML, DOCX, XLSX, etc.). Returns an S3 URI.
2. **For PDFs already open in the viewer: `browser_page_save_as_pdf()`** — If you already navigated to a PDF URL and the browser is showing the PDF viewer (empty page text), this downloads the original PDF binary and uploads to S3.
3. **For HTML/HTM document pages: `browser_save_as_html()`** — If you already navigated to an HTM/HTML document (e.g. SEC filing, annual report served as HTML), this saves the full DOM including tables and structure, uploads to S3, and returns an S3 URI. Use this instead of trying to extract from the page text when the document is large or structurally complex.
4. **Last resort: `file_move_to_s3(source_url='<url>')`** — Direct HTTP download without browser context. Will fail with 403 on sites that require browser cookies or user-agent.

**Do NOT use `browser_evaluate` to fetch file bytes as base64.** This creates megabytes of data that exceeds the LLM context limit and crashes the session. Always use `browser_upload_to_s3`, `browser_page_save_as_pdf`, or `browser_save_as_html` instead.

After downloading, pass the returned `s3_uri` to:
- `read_document(s3_path='<s3_uri>', user_query='...')` for quick extraction
- `index_document(s3_path='<s3_uri>')` for deep PDF analysis

## Saving pages as documents

- `browser_page_save_as_pdf()` — saves the rendered page as PDF and uploads to S3. If the page is displaying an inline/embedded PDF (e.g. a .pdf URL opened in the browser viewer), it auto-detects this and downloads the original PDF binary instead of printing the viewer UI. Returns the S3 URI for use with document tools.
- `browser_save_as_html()` — saves the full DOM as HTML and uploads to S3. Use this when the page IS a document (financial report, filing, etc.) that you need to analyze with `read_document`. See the Web-Sourced Document Rule in the Document Processing guide.

## Citation rules

- `browser_navigate` results include a `source_url` field. Always record it for citation purposes.
- When extracting facts from a browser session, cite the URL you navigated to and the page title (visible in the Page section).
- Format: `[n] Page Title — https://url.visited.com/path`
- If you navigate across multiple pages during extraction, track each URL as a separate citation.
- For data extracted via `browser_evaluate` or `browser_collect_paginated_data`, cite the page URL where the extraction was performed.
group_key:playwright
active:Yes
active_bundles:[
  "core"
]
already_loaded:No

Input
url:https://www.capitalone.com/
Output
text:### Ran Playwright code
```js
await page.goto('https://www.capitalone.com/');
```

### Page state
- Page URL: https://www.capitalone.com/
- Page Title: Capital One | Credit Cards, Checking, Savings & Auto Loans
- Page Snapshot:
```yaml
- img "FDIC-Insured - Backed by the full faith and credit of the U.S. Government" [ref=e7]
- banner "Capital One Main Navigation" [ref=e8]:
  - link "Skip to main content" [ref=e12]:
    - /url: "#default-id"
  - link "Capital One Home" [ref=e14]:
    - /url: https://www.capitalone.com/
    - img "Capital One Home" [ref=e15]
  - link "Credit Cards" [ref=e33]:
    - /url: https://www.capitalone.com/credit-cards/
    - generic [ref=e34]: Credit Cards
  - link "Checking & Savings" [ref=e35]:
    - /url: https://www.capitalone.com/bank/
    - generic [ref=e36]: Checking & Savings
  - link "Auto" [ref=e37]:
    - /url: https://www.capitalone.com/cars/auto-financing
    - generic [ref=e38]: Auto
  - link "Business" [ref=e39]:
    - /url: https://www.capitalone.com/small-business/homepage/
    - generic [ref=e40]: Business
  - link "Commercial" [ref=e41]:
    - /url: https://www.capitalone.com/commercial/
    - generic [ref=e42]: Commercial
  - link "Benefits & Tools" [ref=e43]:
    - /url: unsafe:javascript:void(0);
    - generic [ref=e44]: Benefits & Tools
  - button "Search" [ref=e47]
  - link "Help Center" [ref=e48]:
    - /url: https://www.capitalone.com/help-center/
  - link "locationsText" [ref=e49]:
    - /url: https://locations.capitalone.com/
  - link "Sign In" [ref=e50]:
    - /url: https://verified.capitalone.com/auth/signin
    - generic [ref=e51]: Sign In
- generic [ref=e64]: Username
- textbox "Username" [ref=e65]
- generic [ref=e68]: Password
- textbox "Password" [ref=e69]
- checkbox "Remember me" [ref=e73]
- generic [ref=e74]: Remember me
- link "Want this computer to remember your username? Check this box. Not recommended for shared computers." [ref=e75]:
  - /url: javascript:void(0)
- paragraph [ref=e77]:
  - link "Forgot username or password?" [ref=e78]:
    - /url: https://verified.capitalone.com/sign-in-help/
- button "Sign in" [ref=e81]
- link "Set up online access" [ref=e82]:
  - /url: https://verified.capitalone.com/enroll/#/pii
- heading "Explore Capital One’s Banking and Financial Services" [level=1] [ref=e83]
- heading "Check for card offers" [level=2] [ref=e100]
- paragraph [ref=e101]: See if you’re approved for card offers in as little as 90 seconds.
- link "Check my eligibility" [ref=e104]:
  - /url: https://www.capitalone.com/apply/credit-cards/preapprove/?landingPage=ehp
- paragraph [ref=e115]: CHOOSE A TOPIC
- menu [ref=e120]:
  - menuitem "Meet Capital One" [ref=e121]:
    - heading "Meet Capital One" [level=2] [ref=e122]
  - menuitem "Financial wellness" [ref=e123]:
    - heading "Financial wellness" [level=2] [ref=e124]
  - menuitem "Built-in benefits" [ref=e125]:
    - heading "Built-in benefits" [level=2] [ref=e126]
- heading "Find the right card for you" [level=3] [ref=e141]
- paragraph [ref=e142]: Explore your card offers in as little as 90 seconds–with no impact to your credit score.
- link "Check My Eligibility" [ref=e144]:
  - /url: https://www.capitalone.com/apply/credit-cards/preapprove/
- heading "Bank with confidence" [level=3] [ref=e152]
- paragraph [ref=e153]: Enjoy no fees or minimums for checking and savings accounts.
- link "Explore Banking" [ref=e155]:
  - /url: https://www.capitalone.com/bank/
- heading "Find a car you love" [level=3] [ref=e163]
- paragraph [ref=e164]: Shop cars and pre-qualify for financing with Auto Navigator.
- link "Start Shopping" [ref=e166]:
  - /url: https://www.capitalone.com/cars/
- img "Hand holding credit card" [ref=e176]
- paragraph [ref=e183]: FIND THE RIGHT CARD
- heading "More built-in benefits" [level=2] [ref=e186]
- paragraph [ref=e190]: Narrow your search based on what you’re looking for—like rewards, low intro APR or credit growing cards.
- link "Compare credit cards" [ref=e193]:
  - /url: https://www.capitalone.com/credit-cards/compare/?filter=compareallcards
- img "Person at work on laptop and woman scanning boxes" [ref=e198]
- paragraph [ref=e205]: SIMPLIFY YOUR BUSINESS FINANCES
- heading "Only the best for your business" [level=2] [ref=e208]
- paragraph [ref=e212]: Earn unlimited rewards—travel or cash—with every card. Explore banking options that can help move your business forward.
- link "Explore business solutions" [ref=e215]:
  - /url: https://www.capitalone.com/small-business/homepage/
- heading "DIGITAL TOOLS BUILT FOR EASE" [level=2] [ref=e223]
- tablist [ref=e225]:
  - tab "Anytime account access" [selected] [ref=e227]:
    - heading "Anytime account access." [level=3] [ref=e229]: Anytime account access
    - paragraph [ref=e230]: Monitor your accounts on the go with our top-rated Capital One Mobile app.
  - tab "Redeem your rewards" [ref=e232]:
    - heading "Redeem your rewards." [level=3] [ref=e234]: Redeem your rewards
    - paragraph [ref=e235]: Explore and redeem your Capital One credit card rewards.
  - tab "Monitor your credit" [ref=e238]:
    - heading "Monitor your credit." [level=3] [ref=e241]: Monitor your credit
    - paragraph [ref=e242]: Quickly and easily check your credit score with no impact and for free with CreditWise.
  - tab "Pay & move money" [ref=e243]:
    - heading "Pay & move money." [level=3] [ref=e246]: Pay & move money
    - paragraph [ref=e247]: Make payments, transfer money and deposit checks with ease.
- tabpanel "Anytime account access" [ref=e248]
- link "Explore more digital tools" [ref=e251]:
  - /url: https://www.capitalone.com/digital/tools
- paragraph [ref=e261]: We're more than a bank
- img "Person opens a box to see his purchases" [ref=e270]
- link "Capital One Shopping" [ref=e272]:
  - /url: https://capitaloneshopping.com/lp/mobilewebrd?utm_source=c1lp&creative_id=tile_tab1
  - paragraph [ref=e273]: Capital One Shopping
- paragraph [ref=e274]: Automatically apply free coupons and save with price drop notifications.
- img "Person enjoying coffee and pastry" [ref=e282]
- link "Part bank, part café" [ref=e284]:
  - /url: https://www.capitalone.com/local/
  - paragraph [ref=e285]: Part bank, part café
- paragraph [ref=e287]: Capital One Cafés are open 7 days a week with extended hours for banking.
- link "Capital One Travel" [ref=e296]:
  - /url: https://capitalonetravel.com/?utm_source=navbt
  - paragraph [ref=e297]: Capital One Travel
- paragraph [ref=e298]: Earn rewards when you book a flight, hotel or car with an eligible card.
- contentinfo [ref=e300]:
  - link "Capital One" [ref=e309]:
    - /url: https://www.capitalone.com/
  - link "Instagram" [ref=e313]:
    - /url: https://www.instagram.com/capitalone/
  - link "X" [ref=e314]:
    - /url: https://x.com/capitalone/
  - link "Facebook" [ref=e315]:
    - /url: https://www.facebook.com/capitalone/
  - link "LinkedIn" [ref=e316]:
    - /url: https://www.linkedin.com/company/capital-one/
  - link "YouTube" [ref=e317]:
    - /url: https://www.youtube.com/user/capitalone/
  - heading "Products" [level=3] [ref=e324]
  - list [ref=e326]:
    - listitem [ref=e327]:
      - link "Credit Cards" [ref=e328]:
        - /url: https://www.capitalone.com/credit-cards/
    - listitem [ref=e329]:
      - link "Checking & Savings" [ref=e330]:
        - /url: https://www.capitalone.com/bank/checking-accounts/
    - listitem [ref=e331]:
      - link "Auto" [ref=e332]:
        - /url: https://www.capitalone.com/cars/auto-financing
    - listitem [ref=e333]:
      - link "Business" [ref=e334]:
        - /url: https://www.capitalone.com/small-business/homepage/
    - listitem [ref=e335]:
      - link "Commercial" [ref=e336]:
        - /url: https://www.capitalone.com/commercial/
    - listitem [ref=e337]:
      - link "Capital One Shopping" [ref=e338]:
        - /url: https://smart.link/qpqs440wc5g68?cp_0=c1lp&cp_1=card_menu_control&cp_2=footer&cp_4=usa&creative_id=card_footer_control
    - listitem [ref=e339]:
      - link "CreditWise" [ref=e340]:
        - /url: https://www.capitalone.com/creditwise/?utm_source=Internal-Other&utm_channel=Homepage&utm_medium=Footer&utm_valprop=creditimprovement&utm_campaign=Products&utm_content=signup&utm_cadence=ongoing
  - heading "Get to Know Us" [level=3] [ref=e343]
  - list [ref=e345]:
    - listitem [ref=e346]:
      - link "About" [ref=e347]:
        - /url: https://www.capitalone.com/about/
    - listitem [ref=e348]:
      - link "Corporate Information" [ref=e349]:
        - /url: https://www.capitalone.com/about/corporate-information/
    - listitem [ref=e350]:
      - link "Newsroom" [ref=e351]:
        - /url: https://www.capitalone.com/about/newsroom/
    - listitem [ref=e352]:
      - link "Technology" [ref=e353]:
        - /url: https://www.capitalone.com/tech/
    - listitem [ref=e354]:
      - link "Investors" [ref=e355]:
        - /url: https://investor.capitalone.com/
    - listitem [ref=e356]:
      - link "Careers + Jobs" [ref=e357]:
        - /url: https://www.capitalonecareers.com/
    - listitem [ref=e358]:
      - link "Diversity & Inclusion" [ref=e359]:
        - /url: https://www.capitalone.com/diversity/
    - listitem [ref=e360]:
      - link "Canada" [ref=e361]:
        - /url: https://www.capitalone.ca/
    - listitem [ref=e362]:
      - link "UK" [ref=e363]:
        - /url: https://www.capitalone.co.uk/
  - heading "On the Go" [level=3] [ref=e366]
  - list [ref=e368]:
    - listitem [ref=e369]:
      - link "Locations & ATMs" [ref=e370]:
        - /url: https://locations.capitalone.com/
    - listitem [ref=e371]:
      - link "Capital One Cafés" [ref=e372]:
        - /url: https://www.capitalone.com/local/
    - listitem [ref=e373]:
      - link "Capital One Travel" [ref=e374]:
        - /url: https://capitalonetravel.com/?utm_source=navbau
    - listitem [ref=e375]:
      - link "Mobile App" [ref=e376]:
        - /url: https://www.capitalone.com/digital/tools/mobile/
    - listitem [ref=e377]:
      - link "Meet Eno" [ref=e378]:
        - /url: https://www.capitalone.com/digital/tools/eno/
    - listitem [ref=e379]:
      - link "Digital Tools" [ref=e380]:
        - /url: https://www.capitalone.com/digital/tools/
  - heading "Legal" [level=3] [ref=e383]
  - list [ref=e385]:
    - listitem [ref=e386]:
      - link "Privacy" [ref=e387]:
        - /url: https://www.capitalone.com/privacy/
    - listitem [ref=e388]:
      - link "Patriot Act Certification" [ref=e389]:
        - /url: https://ecm.capitalone.com/WCM/digital/pdfs/patriot_2025.pdf
    - listitem [ref=e390]:
      - link "Wolfsberg Questionnaire" [ref=e391]:
        - /url: https://www.capitalone.com/digital/wolfsberg-questionnaire/
    - listitem [ref=e392]:
      - link "Subpoena Policy" [ref=e393]:
        - /url: https://www.capitalone.com/digital/subpoena-policy/
    - listitem [ref=e394]:
      - link "Additional Disclosures" [ref=e395]:
        - /url: https://www.capitalone.com/digital/disclosures/
  - heading "Support" [level=3] [ref=e398]
  - list [ref=e400]:
    - listitem [ref=e401]:
      - link "COVID-19" [ref=e402]:
        - /url: https://www.capitalone.com/updates/coronavirus/
    - listitem [ref=e403]:
      - link "Help Center" [ref=e404]:
        - /url: https://www.capitalone.com/help-center/
    - listitem [ref=e405]:
      - link "Learn & Grow" [ref=e406]:
        - /url: https://www.capitalone.com/learn-grow/
    - listitem [ref=e407]:
      - link "Resources for Military" [ref=e408]:
        - /url: https://www.capitalone.com/military/
    - listitem [ref=e409]:
      - link "Accessibility Assistance" [ref=e410]:
        - /url: https://www.capitalone.com/accessibility/
    - listitem [ref=e411]:
      - link "Reach @AskCapitalOne" [ref=e412]:
        - /url: https://x.com/askcapitalone
    - listitem [ref=e413]:
      - link "Security" [ref=e414]:
        - /url: https://www.capitalone.com/digital/identity-protection/
    - listitem [ref=e415]:
      - link "Contact Us" [ref=e416]:
        - /url: https://www.capitalone.com/help-center/contact-us/
  - button "Footnotes section content" [expanded] [ref=e426]:
    - paragraph [ref=e427]: Footnotes
  - paragraph [ref=e429]:
    - text: Learn more about
    - link "FDIC insurance coverage" [ref=e430]:
      - /url: https://www.capitalone.com/bank/fdic/
    - text: .
  - paragraph [ref=e433]: Mastercard® is a registered trademark, and the circles design is a trademark of Mastercard International Incorporated.
  - paragraph [ref=e434]: The EMVCo Contactless Symbol and Contactless Indicator mark, consisting of four graduating arcs, are trademarks owned by and used with permission of EMVCo, LLC.
  - generic [ref=e437]: "1"
  - generic [ref=e438]: This product finder tool is informational and educational only and strictly meant to provide an overview of Capital One product offerings based on self-selected user goals. Its results do not constitute financial or investment advice.
  - generic [ref=e441]: "2"
  - generic [ref=e442]: CreditWise availability will vary depending on ability to obtain your credit history from TransUnion®.
  - paragraph [ref=e452]: ©2026 Capital One
  - link "Privacy" [ref=e455]:
    - /url: https://www.capitalone.com/privacy/
  - link "AdChoices" [ref=e457]:
    - /url: https://www.capitalone.com/privacy/online-privacy-policy#collecting-using-sharing-information-accordion-button-5
  - link "Your California Privacy Choices" [ref=e459]:
    - /url: https://www.capitalone.com/privacy/your-california-privacy-choices/
    - text: Your California Privacy Choices
    - img [ref=e460]
  - link "Terms & Conditions" [ref=e462]:
    - /url: https://www.capitalone.com/digital/terms-conditions/
  - 'link "Member: FDIC" [ref=e464]':
    - /url: https://www.fdic.gov/
  - img "Equal Housing Opportunity" [ref=e465]
- button "Feedback Survey" [ref=e466]:
  - img "Feedback Survey" [ref=e467]
```


[⚠ RESTRICTED PAGE: This page contains login form.
Do NOT interact with credential fields, verification challenges, or anti-bot controls.
Report this restriction to the user and suggest alternatives.]
is_error:No
snapshot_artifact:{
  "filename": "browser_snapshot_531e14388675.txt",
  "s3_key": "session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_531e14388675.txt",
  "s3_uri": "s3://dev-olivaw-iris-s3.us-east-1.cognitive/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_531e14388675.txt",
  "local_path": "/tmp/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_531e14388675.txt",
  "content_hash": "531e1438867518460ae70a359c29855e206523a0a1c281900638873b7cfe145b"
}
source_url:https://www.capitalone.com/

Input
url:https://www.capitalone.com/anti-corruption-and-bribery-policy/
Output
text:### Ran Playwright code
```js
await page.goto('https://www.capitalone.com/anti-corruption-and-bribery-policy/');
```

### Page state
- Page URL: https://www.capitalone.com/page-not-found/
- Page Title: Capital One - Page Not Found
- Page Snapshot:
```yaml
- banner "Capital One Main Navigation" [ref=e6]:
  - link "Skip to main content" [ref=e10]:
    - /url: "#default-id"
  - link "Capital One Home" [ref=e12]:
    - /url: https://www.capitalone.com/
    - img "Capital One Home" [ref=e13]
  - link "Credit Cards" [ref=e31]:
    - /url: https://www.capitalone.com/credit-cards/
    - generic [ref=e32]: Credit Cards
  - link "Checking & Savings" [ref=e33]:
    - /url: https://www.capitalone.com/bank/
    - generic [ref=e34]: Checking & Savings
  - link "Auto" [ref=e35]:
    - /url: https://www.capitalone.com/cars/auto-financing
    - generic [ref=e36]: Auto
  - link "Business" [ref=e37]:
    - /url: https://www.capitalone.com/small-business/homepage/
    - generic [ref=e38]: Business
  - link "Commercial" [ref=e39]:
    - /url: https://www.capitalone.com/commercial/
    - generic [ref=e40]: Commercial
  - link "Benefits & Tools" [ref=e41]:
    - /url: unsafe:javascript:void(0);
    - generic [ref=e42]: Benefits & Tools
  - button "Search" [ref=e45]
  - link "Help Center" [ref=e46]:
    - /url: https://www.capitalone.com/help-center/
  - link "locationsText" [ref=e47]:
    - /url: https://locations.capitalone.com/
  - link "Sign In" [ref=e48]:
    - /url: https://verified.capitalone.com/auth/signin
    - generic [ref=e49]: Sign In
- heading "We can’t find that page" [level=2] [ref=e60]
- paragraph [ref=e63]: Let’s get you to where you want to go.
- paragraph [ref=e73]: TRY ONE OF THESE OPTIONS
- img "Search for anything" [ref=e80]
- link "Search our site" [ref=e82]:
  - /url: /search/
  - paragraph [ref=e83]: Search our site
- paragraph [ref=e84]: Enter a word or phrase to find exactly what you’re looking for.
- img "Online support" [ref=e90]
- link "Visit the Help Center" [ref=e92]:
  - /url: https://www.capitalone.com/help-center/
  - paragraph [ref=e93]: Visit the Help Center
- paragraph [ref=e94]: Browse popular topics and FAQs for quick and easy support.
- link "Connect with us" [ref=e101]:
  - /url: https://www.capitalone.com/help-center/contact-us/
  - paragraph [ref=e102]: Connect with us
- paragraph [ref=e103]: If you’d rather talk to a real person, please give us a call.
- contentinfo [ref=e105]:
  - link "Capital One" [ref=e114]:
    - /url: https://www.capitalone.com/
  - navigation "Breadcrumb" [ref=e117]:
    - list [ref=e119]:
      - listitem [ref=e120]:
        - generic [ref=e121]: Page Not Found
  - link "Instagram" [ref=e125]:
    - /url: https://www.instagram.com/capitalone/
  - link "X" [ref=e126]:
    - /url: https://x.com/capitalone/
  - link "Facebook" [ref=e127]:
    - /url: https://www.facebook.com/capitalone/
  - link "LinkedIn" [ref=e128]:
    - /url: https://www.linkedin.com/company/capital-one/
  - link "YouTube" [ref=e129]:
    - /url: https://www.youtube.com/user/capitalone/
  - heading "Products" [level=3] [ref=e136]
  - list [ref=e138]:
    - listitem [ref=e139]:
      - link "Credit Cards" [ref=e140]:
        - /url: https://www.capitalone.com/credit-cards/
    - listitem [ref=e141]:
      - link "Checking & Savings" [ref=e142]:
        - /url: https://www.capitalone.com/bank/checking-accounts/
    - listitem [ref=e143]:
      - link "Auto" [ref=e144]:
        - /url: https://www.capitalone.com/cars/auto-financing
    - listitem [ref=e145]:
      - link "Business" [ref=e146]:
        - /url: https://www.capitalone.com/small-business/homepage/
    - listitem [ref=e147]:
      - link "Commercial" [ref=e148]:
        - /url: https://www.capitalone.com/commercial/
    - listitem [ref=e149]:
      - link "Capital One Shopping" [ref=e150]:
        - /url: https://smart.link/qpqs440wc5g68?cp_0=c1lp&cp_1=card_menu_control&cp_2=footer&cp_4=usa&creative_id=card_footer_control
    - listitem [ref=e151]:
      - link "CreditWise" [ref=e152]:
        - /url: https://www.capitalone.com/creditwise/?utm_source=Internal-Other&utm_channel=Homepage&utm_medium=Footer&utm_valprop=creditimprovement&utm_campaign=Products&utm_content=signup&utm_cadence=ongoing
  - heading "Get to Know Us" [level=3] [ref=e155]
  - list [ref=e157]:
    - listitem [ref=e158]:
      - link "About" [ref=e159]:
        - /url: https://www.capitalone.com/about/
    - listitem [ref=e160]:
      - link "Corporate Information" [ref=e161]:
        - /url: https://www.capitalone.com/about/corporate-information/
    - listitem [ref=e162]:
      - link "Newsroom" [ref=e163]:
        - /url: https://www.capitalone.com/about/newsroom/
    - listitem [ref=e164]:
      - link "Technology" [ref=e165]:
        - /url: https://www.capitalone.com/tech/
    - listitem [ref=e166]:
      - link "Investors" [ref=e167]:
        - /url: https://investor.capitalone.com/
    - listitem [ref=e168]:
      - link "Careers + Jobs" [ref=e169]:
        - /url: https://www.capitalonecareers.com/
    - listitem [ref=e170]:
      - link "Diversity & Inclusion" [ref=e171]:
        - /url: https://www.capitalone.com/diversity/
    - listitem [ref=e172]:
      - link "Canada" [ref=e173]:
        - /url: https://www.capitalone.ca/
    - listitem [ref=e174]:
      - link "UK" [ref=e175]:
        - /url: https://www.capitalone.co.uk/
  - heading "On the Go" [level=3] [ref=e178]
  - list [ref=e180]:
    - listitem [ref=e181]:
      - link "Locations & ATMs" [ref=e182]:
        - /url: https://locations.capitalone.com/
    - listitem [ref=e183]:
      - link "Capital One Cafés" [ref=e184]:
        - /url: https://www.capitalone.com/local/
    - listitem [ref=e185]:
      - link "Capital One Travel" [ref=e186]:
        - /url: https://capitalonetravel.com/?utm_source=navbau
    - listitem [ref=e187]:
      - link "Mobile App" [ref=e188]:
        - /url: https://www.capitalone.com/digital/tools/mobile/
    - listitem [ref=e189]:
      - link "Meet Eno" [ref=e190]:
        - /url: https://www.capitalone.com/digital/tools/eno/
    - listitem [ref=e191]:
      - link "Digital Tools" [ref=e192]:
        - /url: https://www.capitalone.com/digital/tools/
  - heading "Legal" [level=3] [ref=e195]
  - list [ref=e197]:
    - listitem [ref=e198]:
      - link "Privacy" [ref=e199]:
        - /url: https://www.capitalone.com/privacy/
    - listitem [ref=e200]:
      - link "Patriot Act Certification" [ref=e201]:
        - /url: https://ecm.capitalone.com/WCM/digital/pdfs/patriot_2025.pdf
    - listitem [ref=e202]:
      - link "Wolfsberg Questionnaire" [ref=e203]:
        - /url: https://www.capitalone.com/digital/wolfsberg-questionnaire/
    - listitem [ref=e204]:
      - link "Subpoena Policy" [ref=e205]:
        - /url: https://www.capitalone.com/digital/subpoena-policy/
    - listitem [ref=e206]:
      - link "Additional Disclosures" [ref=e207]:
        - /url: https://www.capitalone.com/digital/disclosures/
  - heading "Support" [level=3] [ref=e210]
  - list [ref=e212]:
    - listitem [ref=e213]:
      - link "COVID-19" [ref=e214]:
        - /url: https://www.capitalone.com/updates/coronavirus/
    - listitem [ref=e215]:
      - link "Help Center" [ref=e216]:
        - /url: https://www.capitalone.com/help-center/
    - listitem [ref=e217]:
      - link "Learn & Grow" [ref=e218]:
        - /url: https://www.capitalone.com/learn-grow/
    - listitem [ref=e219]:
      - link "Resources for Military" [ref=e220]:
        - /url: https://www.capitalone.com/military/
    - listitem [ref=e221]:
      - link "Accessibility Assistance" [ref=e222]:
        - /url: https://www.capitalone.com/accessibility/
    - listitem [ref=e223]:
      - link "Reach @AskCapitalOne" [ref=e224]:
        - /url: https://x.com/askcapitalone
    - listitem [ref=e225]:
      - link "Security" [ref=e226]:
        - /url: https://www.capitalone.com/digital/identity-protection/
    - listitem [ref=e227]:
      - link "Contact Us" [ref=e228]:
        - /url: https://www.capitalone.com/help-center/contact-us/
  - button "Footnotes section content" [expanded] [ref=e238]:
    - paragraph [ref=e239]: Footnotes
  - paragraph [ref=e241]:
    - text: Learn more about
    - link "FDIC insurance coverage" [ref=e242]:
      - /url: https://www.capitalone.com/bank/fdic/
    - text: .
  - paragraph [ref=e252]: ©2026 Capital One
  - link "Privacy" [ref=e255]:
    - /url: https://www.capitalone.com/privacy/
  - link "AdChoices" [ref=e257]:
    - /url: https://www.capitalone.com/privacy/online-privacy-policy#collecting-using-sharing-information-accordion-button-5
  - link "Your California Privacy Choices" [ref=e259]:
    - /url: https://www.capitalone.com/privacy/your-california-privacy-choices/
    - text: Your California Privacy Choices
    - img [ref=e260]
  - link "Terms & Conditions" [ref=e262]:
    - /url: https://www.capitalone.com/digital/terms-conditions/
  - 'link "Member: FDIC" [ref=e264]':
    - /url: https://www.fdic.gov/
  - img "Equal Housing Opportunity" [ref=e265]
```
is_error:No
snapshot_artifact:{
  "filename": "browser_snapshot_0a3f7a92533a.txt",
  "s3_key": "session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_0a3f7a92533a.txt",
  "s3_uri": "s3://dev-olivaw-iris-s3.us-east-1.cognitive/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_0a3f7a92533a.txt",
  "…
source_url:https://www.capitalone.com/anti-corruption-and-bribery-policy/

Input
url:https://www.capitalone.com/search/
Output
text:### Ran Playwright code
```js
await page.goto('https://www.capitalone.com/search/');
```

### Page state
- Page URL: https://www.capitalone.com/search/
- Page Title: Search capitalone.com | Capital One
- Page Snapshot:
```yaml
- banner "Capital One Main Navigation" [ref=e6]:
  - link "Skip to main content" [ref=e10]:
    - /url: "#default-id"
  - link "Capital One Home" [ref=e12]:
    - /url: https://www.capitalone.com/
    - img "Capital One Home" [ref=e13]
  - link "Credit Cards" [ref=e31]:
    - /url: https://www.capitalone.com/credit-cards/
    - generic [ref=e32]: Credit Cards
  - link "Checking & Savings" [ref=e33]:
    - /url: https://www.capitalone.com/bank/
    - generic [ref=e34]: Checking & Savings
  - link "Auto" [ref=e35]:
    - /url: https://www.capitalone.com/cars/auto-financing
    - generic [ref=e36]: Auto
  - link "Business" [ref=e37]:
    - /url: https://www.capitalone.com/small-business/homepage/
    - generic [ref=e38]: Business
  - link "Commercial" [ref=e39]:
    - /url: https://www.capitalone.com/commercial/
    - generic [ref=e40]: Commercial
  - link "Benefits & Tools" [ref=e41]:
    - /url: unsafe:javascript:void(0);
    - generic [ref=e42]: Benefits & Tools
  - button "Search" [ref=e45]
  - link "Help Center" [ref=e46]:
    - /url: https://www.capitalone.com/help-center/
  - link "locationsText" [ref=e47]:
    - /url: https://locations.capitalone.com/
  - link "Sign In" [ref=e48]:
    - /url: https://verified.capitalone.com/auth/signin
    - generic [ref=e49]: Sign In
- textbox "Search capitalone.com" [ref=e61]
- button "Submit Search" [ref=e62]
- button "Clear Search" [ref=e63]
- paragraph [ref=e77]:
  - strong [ref=e78]: Popular Searches
- paragraph [ref=e79]:
  - strong [ref=e80]:
    - link "Credit Cards" [ref=e81]:
      - /url: https://www.capitalone.com/search/?qt=Credit%20Cards
- paragraph [ref=e82]:
  - link "Bank Accounts" [ref=e83]:
    - /url: https://www.capitalone.com/search/?qt=Bank%20Accounts
    - strong [ref=e84]: Bank Accounts
- paragraph [ref=e85]:
  - link "Auto Financing" [ref=e86]:
    - /url: https://www.capitalone.com/search/?qt=Auto%20Financing
    - strong [ref=e87]: Auto Financing
- paragraph [ref=e88]:
  - link "Activate Card" [ref=e89]:
    - /url: https://www.capitalone.com/search/?qt=Activate%20Card
    - strong [ref=e90]: Activate Card
- paragraph [ref=e91]:
  - link "Set up Autopay" [ref=e92]:
    - /url: https://www.capitalone.com/search/?qt=Set%20up%20Autopay
    - strong [ref=e93]: Set up Autopay
- paragraph [ref=e94]:
  - link "Send Money with Zelle" [ref=e95]:
    - /url: https://www.capitalone.com/search/?qt=Send%20Money%20with%20Zelle
    - strong [ref=e96]: Send Money with Zelle
- paragraph [ref=e97]
- paragraph [ref=e98]:
  - strong [ref=e99]: Additional Resources
- paragraph [ref=e100]:
  - link "Visit Help Center" [ref=e101]:
    - /url: https://www.capitalone.com/help-center
    - strong [ref=e102]: Visit Help Center
- paragraph [ref=e103]:
  - link "Contact Us" [ref=e104]:
    - /url: https://www.capitalone.com/help-center/contact-us/
    - strong [ref=e105]: Contact Us
- contentinfo [ref=e107]:
  - link "Capital One" [ref=e116]:
    - /url: https://www.capitalone.com/
  - navigation "Breadcrumb" [ref=e119]:
    - list [ref=e121]:
      - listitem [ref=e122]:
        - generic [ref=e123]: Search
  - link "Instagram" [ref=e127]:
    - /url: https://www.instagram.com/capitalone/
  - link "X" [ref=e128]:
    - /url: https://x.com/capitalone/
  - link "Facebook" [ref=e129]:
    - /url: https://www.facebook.com/capitalone/
  - link "LinkedIn" [ref=e130]:
    - /url: https://www.linkedin.com/company/capital-one/
  - link "YouTube" [ref=e131]:
    - /url: https://www.youtube.com/user/capitalone/
  - heading "Products" [level=3] [ref=e138]
  - list [ref=e140]:
    - listitem [ref=e141]:
      - link "Credit Cards" [ref=e142]:
        - /url: https://www.capitalone.com/credit-cards/
    - listitem [ref=e143]:
      - link "Checking & Savings" [ref=e144]:
        - /url: https://www.capitalone.com/bank/checking-accounts/
    - listitem [ref=e145]:
      - link "Auto" [ref=e146]:
        - /url: https://www.capitalone.com/cars/auto-financing
    - listitem [ref=e147]:
      - link "Business" [ref=e148]:
        - /url: https://www.capitalone.com/small-business/homepage/
    - listitem [ref=e149]:
      - link "Commercial" [ref=e150]:
        - /url: https://www.capitalone.com/commercial/
    - listitem [ref=e151]:
      - link "Capital One Shopping" [ref=e152]:
        - /url: https://smart.link/qpqs440wc5g68?cp_0=c1lp&cp_1=card_menu_control&cp_2=footer&cp_4=usa&creative_id=card_footer_control
    - listitem [ref=e153]:
      - link "CreditWise" [ref=e154]:
        - /url: https://www.capitalone.com/creditwise/?utm_source=Internal-Other&utm_channel=Homepage&utm_medium=Footer&utm_valprop=creditimprovement&utm_campaign=Products&utm_content=signup&utm_cadence=ongoing
  - heading "Get to Know Us" [level=3] [ref=e157]
  - list [ref=e159]:
    - listitem [ref=e160]:
      - link "About" [ref=e161]:
        - /url: https://www.capitalone.com/about/
    - listitem [ref=e162]:
      - link "Corporate Information" [ref=e163]:
        - /url: https://www.capitalone.com/about/corporate-information/
    - listitem [ref=e164]:
      - link "Newsroom" [ref=e165]:
        - /url: https://www.capitalone.com/about/newsroom/
    - listitem [ref=e166]:
      - link "Technology" [ref=e167]:
        - /url: https://www.capitalone.com/tech/
    - listitem [ref=e168]:
      - link "Investors" [ref=e169]:
        - /url: https://investor.capitalone.com/
    - listitem [ref=e170]:
      - link "Careers + Jobs" [ref=e171]:
        - /url: https://www.capitalonecareers.com/
    - listitem [ref=e172]:
      - link "Diversity & Inclusion" [ref=e173]:
        - /url: https://www.capitalone.com/diversity/
    - listitem [ref=e174]:
      - link "Canada" [ref=e175]:
        - /url: https://www.capitalone.ca/
    - listitem [ref=e176]:
      - link "UK" [ref=e177]:
        - /url: https://www.capitalone.co.uk/
  - heading "On the Go" [level=3] [ref=e180]
  - list [ref=e182]:
    - listitem [ref=e183]:
      - link "Locations & ATMs" [ref=e184]:
        - /url: https://locations.capitalone.com/
    - listitem [ref=e185]:
      - link "Capital One Cafés" [ref=e186]:
        - /url: https://www.capitalone.com/local/
    - listitem [ref=e187]:
      - link "Capital One Travel" [ref=e188]:
        - /url: https://capitalonetravel.com/?utm_source=navbau
    - listitem [ref=e189]:
      - link "Mobile App" [ref=e190]:
        - /url: https://www.capitalone.com/digital/tools/mobile/
    - listitem [ref=e191]:
      - link "Meet Eno" [ref=e192]:
        - /url: https://www.capitalone.com/digital/tools/eno/
    - listitem [ref=e193]:
      - link "Digital Tools" [ref=e194]:
        - /url: https://www.capitalone.com/digital/tools/
  - heading "Legal" [level=3] [ref=e197]
  - list [ref=e199]:
    - listitem [ref=e200]:
      - link "Privacy" [ref=e201]:
        - /url: https://www.capitalone.com/privacy/
    - listitem [ref=e202]:
      - link "Patriot Act Certification" [ref=e203]:
        - /url: https://ecm.capitalone.com/WCM/digital/pdfs/patriot_2025.pdf
    - listitem [ref=e204]:
      - link "Wolfsberg Questionnaire" [ref=e205]:
        - /url: https://www.capitalone.com/digital/wolfsberg-questionnaire/
    - listitem [ref=e206]:
      - link "Subpoena Policy" [ref=e207]:
        - /url: https://www.capitalone.com/digital/subpoena-policy/
    - listitem [ref=e208]:
      - link "Additional Disclosures" [ref=e209]:
        - /url: https://www.capitalone.com/digital/disclosures/
  - heading "Support" [level=3] [ref=e212]
  - list [ref=e214]:
    - listitem [ref=e215]:
      - link "COVID-19" [ref=e216]:
        - /url: https://www.capitalone.com/updates/coronavirus/
    - listitem [ref=e217]:
      - link "Help Center" [ref=e218]:
        - /url: https://www.capitalone.com/help-center/
    - listitem [ref=e219]:
      - link "Learn & Grow" [ref=e220]:
        - /url: https://www.capitalone.com/learn-grow/
    - listitem [ref=e221]:
      - link "Resources for Military" [ref=e222]:
        - /url: https://www.capitalone.com/military/
    - listitem [ref=e223]:
      - link "Accessibility Assistance" [ref=e224]:
        - /url: https://www.capitalone.com/accessibility/
    - listitem [ref=e225]:
      - link "Reach @AskCapitalOne" [ref=e226]:
        - /url: https://x.com/askcapitalone
    - listitem [ref=e227]:
      - link "Security" [ref=e228]:
        - /url: https://www.capitalone.com/digital/identity-protection/
    - listitem [ref=e229]:
      - link "Contact Us" [ref=e230]:
        - /url: https://www.capitalone.com/help-center/contact-us/
  - button "Footnotes section content" [expanded] [ref=e240]:
    - paragraph [ref=e241]: Footnotes
  - paragraph [ref=e243]:
    - text: Learn more about
    - link "FDIC insurance coverage" [ref=e244]:
      - /url: https://www.capitalone.com/bank/fdic/
    - text: .
  - paragraph [ref=e247]:
    - text: Zelle® and the Zelle® related marks are wholly owned by Early Warning Services, LLC and are used herein under license.
    - text: To send or receive money with Zelle®, both parties must have an eligible checking or savings account.
  - paragraph [ref=e257]: ©2026 Capital One
  - link "Privacy" [ref=e260]:
    - /url: https://www.capitalone.com/privacy/
  - link "AdChoices" [ref=e262]:
    - /url: https://www.capitalone.com/privacy/online-privacy-policy#collecting-using-sharing-information-accordion-button-5
  - link "Your California Privacy Choices" [ref=e264]:
    - /url: https://www.capitalone.com/privacy/your-california-privacy-choices/
    - text: Your California Privacy Choices
    - img [ref=e265]
  - link "Terms & Conditions" [ref=e267]:
    - /url: https://www.capitalone.com/digital/terms-conditions/
  - 'link "Member: FDIC" [ref=e269]':
    - /url: https://www.fdic.gov/
  - img "Equal Housing Opportunity" [ref=e270]
```
is_error:No
snapshot_artifact:{
  "filename": "browser_snapshot_10c838c69773.txt",
  "s3_key": "session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_10c838c69773.txt",
  "s3_uri": "s3://dev-olivaw-iris-s3.us-east-1.cognitive/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_10c838c69773.txt",
  "local_path": "/tmp/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_10c838c69773.txt",
  "content_hash": "10c838c697737c1ad48261874d2d45e30395d627177edfb6f69e2c196cfd425e"
}
source_url:https://www.capitalone.com/search/

Input
ref:e61
text:Anti-Corruption and Bribery Policy
submit:Yes
element:Search capitalone.com textbox
Output
text:### Ran Playwright code
```js
await page.getByRole('textbox', { name: 'Search capitalone.com' }).fill('Anti-Corruption and Bribery Policy');
await page.getByRole('textbox', { name: 'Search capitalone.com' }).press('Enter');
```

### Page state
- Page URL: https://www.capitalone.com/search/?qt=Anti-Corruption%20and%20Bribery%20Policy&tab=all
- Page Title: Search capitalone.com | Capital One
- Page Snapshot:
```yaml
- banner "Capital One Main Navigation" [ref=e6]:
  - link "Skip to main content" [ref=e10]:
    - /url: "#default-id"
  - link "Capital One Home" [ref=e12]:
    - /url: https://www.capitalone.com/
    - img "Capital One Home" [ref=e13]
  - link "Credit Cards" [ref=e31]:
    - /url: https://www.capitalone.com/credit-cards/
    - generic [ref=e32]: Credit Cards
  - link "Checking & Savings" [ref=e33]:
    - /url: https://www.capitalone.com/bank/
    - generic [ref=e34]: Checking & Savings
  - link "Auto" [ref=e35]:
    - /url: https://www.capitalone.com/cars/auto-financing
    - generic [ref=e36]: Auto
  - link "Business" [ref=e37]:
    - /url: https://www.capitalone.com/small-business/homepage/
    - generic [ref=e38]: Business
  - link "Commercial" [ref=e39]:
    - /url: https://www.capitalone.com/commercial/
    - generic [ref=e40]: Commercial
  - link "Benefits & Tools" [ref=e41]:
    - /url: unsafe:javascript:void(0);
    - generic [ref=e42]: Benefits & Tools
  - button "Search" [ref=e45]
  - link "Help Center" [ref=e46]:
    - /url: https://www.capitalone.com/help-center/
  - link "locationsText" [ref=e47]:
    - /url: https://locations.capitalone.com/
  - link "Sign In" [ref=e48]:
    - /url: https://verified.capitalone.com/auth/signin
    - generic [ref=e49]: Sign In
- textbox "Search capitalone.com" [active] [ref=e61]: Anti-Corruption and Bribery Policy
- button "Submit Search" [ref=e62]
- button "Clear Search" [ref=e63]
- tablist [ref=e277]:
  - tab "All Results" [selected] [ref=e279]
  - tab "Help Topics" [ref=e280]
- tabpanel "All Results"
- alert [ref=e290]: 60 results for "Anti-Corruption and Bribery Policy"
- link "Supplier Third Party Code of Conduct" [ref=e295]:
  - /url: https://www.capitalone.com/about/corporate-information/code-of-conduct
  - generic [ref=e296]: Supplier Third Party Code of Conduct
- generic [ref=e297]: If you are considering registering your firm with Capital One, please read the Third Party Code of Business Conduct and Ethics prior to completing that process.
- link "Law enforcement investigation" [ref=e299]:
  - /url: https://www.capitalone.com/help-center/fraud-disputes/law-enforcement-investigation
  - generic [ref=e300]: Law enforcement investigation
- generic [ref=e301]: Find out who to contact at Capital One if you're a law enforcement officer seeking information on fraud investigation cases.
- link "Uniting Banks and Law Enforcement to Fight Financial Crime" [ref=e303]:
  - /url: https://www.capitalone.com/about/newsroom/uniting-banks-and-law-enforcement-to-fight-financial-crime
  - generic [ref=e304]: Uniting Banks and Law Enforcement to Fight Financial Crime
- generic [ref=e305]: Capital One’s Jamie Simmons discusses how real-time collaboration between banks and law enforcement helps prevent scams and disrupt financial crime.
- link "How to Identify Business Fraud" [ref=e307]:
  - /url: https://www.capitalone.com/learn-grow/business-resources/employee-fraud-awareness
  - generic [ref=e308]: How to Identify Business Fraud
- generic [ref=e309]: Is your business prepared for sophisticated fraud attacks? Explore best practices for helping employees identify business fraud.
- link "How Machine Learning Can Help Fight Money Laundering" [ref=e311]:
  - /url: https://www.capitalone.com/tech/machine-learning/how-machine-learning-can-help-fight-money-laundering
  - generic [ref=e312]: How Machine Learning Can Help Fight Money Laundering
- generic [ref=e313]: As financial criminals adopt sophisticated new money laundering techniques, traditional detection methods may become less effective. Putting the ML into AML represents a unique opportunity to take anti-money laundering efforts to the next level and help prevent this type of financial crime.
- link "Building a Safer Future Against Financial Crime" [ref=e315]:
  - /url: https://www.capitalone.com/about/newsroom/building-a-safer-future-against-financial-crime
  - generic [ref=e316]: Building a Safer Future Against Financial Crime
- generic [ref=e317]: Capital One is bringing together industries, government and law enforcement to protect consumers from financial crime
- link "Fighting Financial Scams During the COVID-19 Pandemic" [ref=e319]:
  - /url: https://www.capitalone.com/about/newsroom/financial-scams-aapi
  - generic [ref=e320]: Fighting Financial Scams During the COVID-19 Pandemic
- generic [ref=e321]: Capital One created resources to help people that don't primarily speak English avoid phishing schemes during the pandemic.
- link "Detecting Human Trafficking, Opioid Misuse, Elder Abuse" [ref=e323]:
  - /url: https://www.capitalone.com/about/newsroom/fight-against-opioid-misuse
  - generic [ref=e324]: Detecting Human Trafficking, Opioid Misuse, Elder Abuse
- generic [ref=e325]: Capital One is using real-time financial data to analyze potential anomalies associated with human trafficking, opioid misuse, and elder abuse.
- link "Advancing Greater Fairness and Explainability for AI and Machine Learning Across the Banking Industry" [ref=e327]:
  - /url: https://www.capitalone.com/tech/machine-learning/advancing-greater-fairness-and-explainability-for-ai-and-machine-learning-across-the-banking-industry
  - generic [ref=e328]: Advancing Greater Fairness and Explainability for AI and Machine Learning Across the Banking Industry
- generic [ref=e329]: How can developers prove that the AI/ML systems making these decisions are doing so ethically, fairly and in compliance with relevant laws?
- link "Fraud Prevention Tips for Your Business" [ref=e331]:
  - /url: https://www.capitalone.com/learn-grow/business-resources/avoiding-business-fraud
  - generic [ref=e332]: Fraud Prevention Tips for Your Business
- generic [ref=e333]: Some ways to prevent business fraud include training employees, setting up internal processes and investing in tools to detect and stop it early.
- button "Previous Page" [disabled] [ref=e336]
- button "Search Results Page Number 1" [disabled] [ref=e338]: "1"
- button "Search Results Page Number 2" [ref=e340]: "2"
- button "Search Results Page Number 3" [ref=e342]: "3"
- button "Search Results Page Number 4" [ref=e344]: "4"
- button "Search Results Page Number 5" [ref=e346]: "5"
- button "Search Results Page Number 6" [ref=e348]: "6"
- button "Next Page" [ref=e349]
- contentinfo [ref=e107]:
  - link "Capital One" [ref=e116]:
    - /url: https://www.capitalone.com/
  - navigation "Breadcrumb" [ref=e119]:
    - list [ref=e121]:
      - listitem [ref=e122]:
        - generic [ref=e123]: Search
  - link "Instagram" [ref=e127]:
    - /url: https://www.instagram.com/capitalone/
  - link "X" [ref=e128]:
    - /url: https://x.com/capitalone/
  - link "Facebook" [ref=e129]:
    - /url: https://www.facebook.com/capitalone/
  - link "LinkedIn" [ref=e130]:
    - /url: https://www.linkedin.com/company/capital-one/
  - link "YouTube" [ref=e131]:
    - /url: https://www.youtube.com/user/capitalone/
  - heading "Products" [level=3] [ref=e138]
  - list [ref=e140]:
    - listitem [ref=e141]:
      - link "Credit Cards" [ref=e142]:
        - /url: https://www.capitalone.com/credit-cards/
    - listitem [ref=e143]:
      - link "Checking & Savings" [ref=e144]:
        - /url: https://www.capitalone.com/bank/checking-accounts/
    - listitem [ref=e145]:
      - link "Auto" [ref=e146]:
        - /url: https://www.capitalone.com/cars/auto-financing
    - listitem [ref=e147]:
      - link "Business" [ref=e148]:
        - /url: https://www.capitalone.com/small-business/homepage/
    - listitem [ref=e149]:
      - link "Commercial" [ref=e150]:
        - /url: https://www.capitalone.com/commercial/
    - listitem [ref=e151]:
      - link "Capital One Shopping" [ref=e152]:
        - /url: https://smart.link/qpqs440wc5g68?cp_0=c1lp&cp_1=card_menu_control&cp_2=footer&cp_4=usa&creative_id=card_footer_control
    - listitem [ref=e153]:
      - link "CreditWise" [ref=e154]:
        - /url: https://www.capitalone.com/creditwise/?utm_source=Internal-Other&utm_channel=Homepage&utm_medium=Footer&utm_valprop=creditimprovement&utm_campaign=Products&utm_content=signup&utm_cadence=ongoing
  - heading "Get to Know Us" [level=3] [ref=e157]
  - list [ref=e159]:
    - listitem [ref=e160]:
      - link "About" [ref=e161]:
        - /url: https://www.capitalone.com/about/
    - listitem [ref=e162]:
      - link "Corporate Information" [ref=e163]:
        - /url: https://www.capitalone.com/about/corporate-information/
    - listitem [ref=e164]:
      - link "Newsroom" [ref=e165]:
        - /url: https://www.capitalone.com/about/newsroom/
    - listitem [ref=e166]:
      - link "Technology" [ref=e167]:
        - /url: https://www.capitalone.com/tech/
    - listitem [ref=e168]:
      - link "Investors" [ref=e169]:
        - /url: https://investor.capitalone.com/
    - listitem [ref=e170]:
      - link "Careers + Jobs" [ref=e171]:
        - /url: https://www.capitalonecareers.com/
    - listitem [ref=e172]:
      - link "Diversity & Inclusion" [ref=e173]:
        - /url: https://www.capitalone.com/diversity/
    - listitem [ref=e174]:
      - link "Canada" [ref=e175]:
        - /url: https://www.capitalone.ca/
    - listitem [ref=e176]:
      - link "UK" [ref=e177]:
        - /url: https://www.capitalone.co.uk/
  - heading "On the Go" [level=3] [ref=e180]
  - list [ref=e182]:
    - listitem [ref=e183]:
      - link "Locations & ATMs" [ref=e184]:
        - /url: https://locations.capitalone.com/
    - listitem [ref=e185]:
      - link "Capital One Cafés" [ref=e186]:
        - /url: https://www.capitalone.com/local/
    - listitem [ref=e187]:
      - link "Capital One Travel" [ref=e188]:
        - /url: https://capitalonetravel.com/?utm_source=navbau
    - listitem [ref=e189]:
      - link "Mobile App" [ref=e190]:
        - /url: https://www.capitalone.com/digital/tools/mobile/
    - listitem [ref=e191]:
      - link "Meet Eno" [ref=e192]:
        - /url: https://www.capitalone.com/digital/tools/eno/
    - listitem [ref=e193]:
      - link "Digital Tools" [ref=e194]:
        - /url: https://www.capitalone.com/digital/tools/
  - heading "Legal" [level=3] [ref=e197]
  - list [ref=e199]:
    - listitem [ref=e200]:
      - link "Privacy" [ref=e201]:
        - /url: https://www.capitalone.com/privacy/
    - listitem [ref=e202]:
      - link "Patriot Act Certification" [ref=e203]:
        - /url: https://ecm.capitalone.com/WCM/digital/pdfs/patriot_2025.pdf
    - listitem [ref=e204]:
      - link "Wolfsberg Questionnaire" [ref=e205]:
        - /url: https://www.capitalone.com/digital/wolfsberg-questionnaire/
    - listitem [ref=e206]:
      - link "Subpoena Policy" [ref=e207]:
        - /url: https://www.capitalone.com/digital/subpoena-policy/
    - listitem [ref=e208]:
      - link "Additional Disclosures" [ref=e209]:
        - /url: https://www.capitalone.com/digital/disclosures/
  - heading "Support" [level=3] [ref=e212]
  - list [ref=e214]:
    - listitem [ref=e215]:
      - link "COVID-19" [ref=e216]:
        - /url: https://www.capitalone.com/updates/coronavirus/
    - listitem [ref=e217]:
      - link "Help Center" [ref=e218]:
        - /url: https://www.capitalone.com/help-center/
    - listitem [ref=e219]:
      - link "Learn & Grow" [ref=e220]:
        - /url: https://www.capitalone.com/learn-grow/
    - listitem [ref=e221]:
      - link "Resources for Military" [ref=e222]:
        - /url: https://www.capitalone.com/military/
    - listitem [ref=e223]:
      - link "Accessibility Assistance" [ref=e224]:
        - /url: https://www.capitalone.com/accessibility/
    - listitem [ref=e225]:
      - link "Reach @AskCapitalOne" [ref=e226]:
        - /url: https://x.com/askcapitalone
    - listitem [ref=e227]:
      - link "Security" [ref=e228]:
        - /url: https://www.capitalone.com/digital/identity-protection/
    - listitem [ref=e229]:
      - link "Contact Us" [ref=e230]:
        - /url: https://www.capitalone.com/help-center/contact-us/
  - button "Footnotes section content" [expanded] [ref=e240]:
    - paragraph [ref=e241]: Footnotes
  - paragraph [ref=e243]:
    - text: Learn more about
    - link "FDIC insurance coverage" [ref=e244]:
      - /url: https://www.capitalone.com/bank/fdic/
    - text: .
  - paragraph [ref=e247]:
    - text: Zelle® and the Zelle® related marks are wholly owned by Early Warning Services, LLC and are used herein under license.
    - text: To send or receive money with Zelle®, both parties must have an eligible checking or savings account.
  - paragraph [ref=e257]: ©2026 Capital One
  - link "Privacy" [ref=e260]:
    - /url: https://www.capitalone.com/privacy/
  - link "AdChoices" [ref=e262]:
    - /url: https://www.capitalone.com/privacy/online-privacy-policy#collecting-using-sharing-information-accordion-button-5
  - link "Your California Privacy Choices" [ref=e264]:
    - /url: https://www.capitalone.com/privacy/your-california-privacy-choices/
    - text: Your California Privacy Choices
    - img [ref=e265]
  - link "Terms & Conditions" [ref=e267]:
    - /url: https://www.capitalone.com/digital/terms-conditions/
  - 'link "Member: FDIC" [ref=e269]':
    - /url: https://www.fdic.gov/
  - img "Equal Housing Opportunity" [ref=e270]
```
is_error:No
snapshot_artifact:{
  "filename": "browser_snapshot_4a1d78931d6a.txt",
  "s3_key": "session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_4a1d78931d6a.txt",
  "s3_uri": "s3://dev-olivaw-iris-s3.us-east-1.cognitive/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_4a1d78931d6a.txt",
  "local_path": "/tmp/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_4a1d78931d6a.txt",
  "content_hash": "4a1d78931d6ac86cee0cdc77ba4b1505b52e63e39b951bbfb315ed7520149b0f"
}

Input
ref:e61
text:anti-corruption bribery policy site:capitalone.com
submit:Yes
element:Search capitalone.com textbox
Output
text:### Ran Playwright code
```js
await page.getByRole('textbox', { name: 'Search capitalone.com' }).fill('anti-corruption bribery policy site:capitalone.com');
await page.getByRole('textbox', { name: 'Search capitalone.com' }).press('Enter');
```

### Page state
- Page URL: https://www.capitalone.com/search/?qt=anti-corruption%20bribery%20policy%20site:capitalone.com&tab=all
- Page Title: Search capitalone.com | Capital One
- Page Snapshot:
```yaml
- banner "Capital One Main Navigation" [ref=e6]:
  - link "Skip to main content" [ref=e10]:
    - /url: "#default-id"
  - link "Capital One Home" [ref=e12]:
    - /url: https://www.capitalone.com/
    - img "Capital One Home" [ref=e13]
  - link "Credit Cards" [ref=e31]:
    - /url: https://www.capitalone.com/credit-cards/
    - generic [ref=e32]: Credit Cards
  - link "Checking & Savings" [ref=e33]:
    - /url: https://www.capitalone.com/bank/
    - generic [ref=e34]: Checking & Savings
  - link "Auto" [ref=e35]:
    - /url: https://www.capitalone.com/cars/auto-financing
    - generic [ref=e36]: Auto
  - link "Business" [ref=e37]:
    - /url: https://www.capitalone.com/small-business/homepage/
    - generic [ref=e38]: Business
  - link "Commercial" [ref=e39]:
    - /url: https://www.capitalone.com/commercial/
    - generic [ref=e40]: Commercial
  - link "Benefits & Tools" [ref=e41]:
    - /url: unsafe:javascript:void(0);
    - generic [ref=e42]: Benefits & Tools
  - button "Search" [ref=e45]
  - link "Help Center" [ref=e46]:
    - /url: https://www.capitalone.com/help-center/
  - link "locationsText" [ref=e47]:
    - /url: https://locations.capitalone.com/
  - link "Sign In" [ref=e48]:
    - /url: https://verified.capitalone.com/auth/signin
    - generic [ref=e49]: Sign In
- textbox "Search capitalone.com" [active] [ref=e61]: anti-corruption bribery policy site:capitalone.com
- button "Submit Search" [ref=e62]
- button "Clear Search" [ref=e63]
- tablist [ref=e277]:
  - tab "All Results" [selected] [ref=e279]
  - tab "Help Topics" [ref=e280]
- tabpanel "All Results"
- alert [ref=e356]: 60 results for "anti-corruption bribery policy site:capitalone.com"
- link "Uniting Banks and Law Enforcement to Fight Financial Crime" [ref=e361]:
  - /url: https://www.capitalone.com/about/newsroom/uniting-banks-and-law-enforcement-to-fight-financial-crime
  - generic [ref=e362]: Uniting Banks and Law Enforcement to Fight Financial Crime
- generic [ref=e363]: Capital One’s Jamie Simmons discusses how real-time collaboration between banks and law enforcement helps prevent scams and disrupt financial crime.
- link "How to Identify Business Fraud" [ref=e365]:
  - /url: https://www.capitalone.com/learn-grow/business-resources/employee-fraud-awareness
  - generic [ref=e366]: How to Identify Business Fraud
- generic [ref=e367]: Is your business prepared for sophisticated fraud attacks? Explore best practices for helping employees identify business fraud.
- link "Law enforcement investigation" [ref=e369]:
  - /url: https://www.capitalone.com/help-center/fraud-disputes/law-enforcement-investigation
  - generic [ref=e370]: Law enforcement investigation
- generic [ref=e371]: Find out who to contact at Capital One if you're a law enforcement officer seeking information on fraud investigation cases.
- link "2019 Capital One Cyber Incident" [ref=e373]:
  - /url: https://www.capitalone.com/digital/facts2019
  - generic [ref=e374]: 2019 Capital One Cyber Incident
- generic [ref=e375]: Get more information about the Capital One cyber incident and its impact. Learn about what happened and what to do next.
- link "Supplier Third Party Code of Conduct" [ref=e377]:
  - /url: https://www.capitalone.com/about/corporate-information/code-of-conduct
  - generic [ref=e378]: Supplier Third Party Code of Conduct
- generic [ref=e379]: If you are considering registering your firm with Capital One, please read the Third Party Code of Business Conduct and Ethics prior to completing that process.
- link "How Machine Learning Can Help Fight Money Laundering" [ref=e381]:
  - /url: https://www.capitalone.com/tech/machine-learning/how-machine-learning-can-help-fight-money-laundering
  - generic [ref=e382]: How Machine Learning Can Help Fight Money Laundering
- generic [ref=e383]: As financial criminals adopt sophisticated new money laundering techniques, traditional detection methods may become less effective. Putting the ML into AML represents a unique opportunity to take anti-money laundering efforts to the next level and help prevent this type of financial crime.
- link "Building a Safer Future Against Financial Crime" [ref=e385]:
  - /url: https://www.capitalone.com/about/newsroom/building-a-safer-future-against-financial-crime
  - generic [ref=e386]: Building a Safer Future Against Financial Crime
- generic [ref=e387]: Capital One is bringing together industries, government and law enforcement to protect consumers from financial crime
- link "Search capitalone.com" [ref=e389]:
  - /url: https://www.capitalone.com/search
  - generic [ref=e390]: Search capitalone.com
- generic [ref=e391]: Use keywords to search the Capital One website and quickly find support for credit cards, bank accounts, small business & more.
- link "Fighting Financial Scams During the COVID-19 Pandemic" [ref=e393]:
  - /url: https://www.capitalone.com/about/newsroom/financial-scams-aapi
  - generic [ref=e394]: Fighting Financial Scams During the COVID-19 Pandemic
- generic [ref=e395]: Capital One created resources to help people that don't primarily speak English avoid phishing schemes during the pandemic.
- link "Capital One Fraud Protection Tools" [ref=e397]:
  - /url: https://www.capitalone.com/learn-grow/money-management/capital-one-fraud-protection
  - generic [ref=e398]: Capital One Fraud Protection Tools
- generic [ref=e399]: Fraud protection tools and alerts can help identify suspicious activity on your account. Learn more.
- button "Previous Page" [disabled] [ref=e402]
- button "Search Results Page Number 1" [disabled] [ref=e404]: "1"
- button "Search Results Page Number 2" [ref=e406]: "2"
- button "Search Results Page Number 3" [ref=e408]: "3"
- button "Search Results Page Number 4" [ref=e410]: "4"
- button "Search Results Page Number 5" [ref=e412]: "5"
- button "Search Results Page Number 6" [ref=e414]: "6"
- button "Next Page" [ref=e415]
- contentinfo [ref=e107]:
  - link "Capital One" [ref=e116]:
    - /url: https://www.capitalone.com/
  - navigation "Breadcrumb" [ref=e119]:
    - list [ref=e121]:
      - listitem [ref=e122]:
        - generic [ref=e123]: Search
  - link "Instagram" [ref=e127]:
    - /url: https://www.instagram.com/capitalone/
  - link "X" [ref=e128]:
    - /url: https://x.com/capitalone/
  - link "Facebook" [ref=e129]:
    - /url: https://www.facebook.com/capitalone/
  - link "LinkedIn" [ref=e130]:
    - /url: https://www.linkedin.com/company/capital-one/
  - link "YouTube" [ref=e131]:
    - /url: https://www.youtube.com/user/capitalone/
  - heading "Products" [level=3] [ref=e138]
  - list [ref=e140]:
    - listitem [ref=e141]:
      - link "Credit Cards" [ref=e142]:
        - /url: https://www.capitalone.com/credit-cards/
    - listitem [ref=e143]:
      - link "Checking & Savings" [ref=e144]:
        - /url: https://www.capitalone.com/bank/checking-accounts/
    - listitem [ref=e145]:
      - link "Auto" [ref=e146]:
        - /url: https://www.capitalone.com/cars/auto-financing
    - listitem [ref=e147]:
      - link "Business" [ref=e148]:
        - /url: https://www.capitalone.com/small-business/homepage/
    - listitem [ref=e149]:
      - link "Commercial" [ref=e150]:
        - /url: https://www.capitalone.com/commercial/
    - listitem [ref=e151]:
      - link "Capital One Shopping" [ref=e152]:
        - /url: https://smart.link/qpqs440wc5g68?cp_0=c1lp&cp_1=card_menu_control&cp_2=footer&cp_4=usa&creative_id=card_footer_control
    - listitem [ref=e153]:
      - link "CreditWise" [ref=e154]:
        - /url: https://www.capitalone.com/creditwise/?utm_source=Internal-Other&utm_channel=Homepage&utm_medium=Footer&utm_valprop=creditimprovement&utm_campaign=Products&utm_content=signup&utm_cadence=ongoing
  - heading "Get to Know Us" [level=3] [ref=e157]
  - list [ref=e159]:
    - listitem [ref=e160]:
      - link "About" [ref=e161]:
        - /url: https://www.capitalone.com/about/
    - listitem [ref=e162]:
      - link "Corporate Information" [ref=e163]:
        - /url: https://www.capitalone.com/about/corporate-information/
    - listitem [ref=e164]:
      - link "Newsroom" [ref=e165]:
        - /url: https://www.capitalone.com/about/newsroom/
    - listitem [ref=e166]:
      - link "Technology" [ref=e167]:
        - /url: https://www.capitalone.com/tech/
    - listitem [ref=e168]:
      - link "Investors" [ref=e169]:
        - /url: https://investor.capitalone.com/
    - listitem [ref=e170]:
      - link "Careers + Jobs" [ref=e171]:
        - /url: https://www.capitalonecareers.com/
    - listitem [ref=e172]:
      - link "Diversity & Inclusion" [ref=e173]:
        - /url: https://www.capitalone.com/diversity/
    - listitem [ref=e174]:
      - link "Canada" [ref=e175]:
        - /url: https://www.capitalone.ca/
    - listitem [ref=e176]:
      - link "UK" [ref=e177]:
        - /url: https://www.capitalone.co.uk/
  - heading "On the Go" [level=3] [ref=e180]
  - list [ref=e182]:
    - listitem [ref=e183]:
      - link "Locations & ATMs" [ref=e184]:
        - /url: https://locations.capitalone.com/
    - listitem [ref=e185]:
      - link "Capital One Cafés" [ref=e186]:
        - /url: https://www.capitalone.com/local/
    - listitem [ref=e187]:
      - link "Capital One Travel" [ref=e188]:
        - /url: https://capitalonetravel.com/?utm_source=navbau
    - listitem [ref=e189]:
      - link "Mobile App" [ref=e190]:
        - /url: https://www.capitalone.com/digital/tools/mobile/
    - listitem [ref=e191]:
      - link "Meet Eno" [ref=e192]:
        - /url: https://www.capitalone.com/digital/tools/eno/
    - listitem [ref=e193]:
      - link "Digital Tools" [ref=e194]:
        - /url: https://www.capitalone.com/digital/tools/
  - heading "Legal" [level=3] [ref=e197]
  - list [ref=e199]:
    - listitem [ref=e200]:
      - link "Privacy" [ref=e201]:
        - /url: https://www.capitalone.com/privacy/
    - listitem [ref=e202]:
      - link "Patriot Act Certification" [ref=e203]:
        - /url: https://ecm.capitalone.com/WCM/digital/pdfs/patriot_2025.pdf
    - listitem [ref=e204]:
      - link "Wolfsberg Questionnaire" [ref=e205]:
        - /url: https://www.capitalone.com/digital/wolfsberg-questionnaire/
    - listitem [ref=e206]:
      - link "Subpoena Policy" [ref=e207]:
        - /url: https://www.capitalone.com/digital/subpoena-policy/
    - listitem [ref=e208]:
      - link "Additional Disclosures" [ref=e209]:
        - /url: https://www.capitalone.com/digital/disclosures/
  - heading "Support" [level=3] [ref=e212]
  - list [ref=e214]:
    - listitem [ref=e215]:
      - link "COVID-19" [ref=e216]:
        - /url: https://www.capitalone.com/updates/coronavirus/
    - listitem [ref=e217]:
      - link "Help Center" [ref=e218]:
        - /url: https://www.capitalone.com/help-center/
    - listitem [ref=e219]:
      - link "Learn & Grow" [ref=e220]:
        - /url: https://www.capitalone.com/learn-grow/
    - listitem [ref=e221]:
      - link "Resources for Military" [ref=e222]:
        - /url: https://www.capitalone.com/military/
    - listitem [ref=e223]:
      - link "Accessibility Assistance" [ref=e224]:
        - /url: https://www.capitalone.com/accessibility/
    - listitem [ref=e225]:
      - link "Reach @AskCapitalOne" [ref=e226]:
        - /url: https://x.com/askcapitalone
    - listitem [ref=e227]:
      - link "Security" [ref=e228]:
        - /url: https://www.capitalone.com/digital/identity-protection/
    - listitem [ref=e229]:
      - link "Contact Us" [ref=e230]:
        - /url: https://www.capitalone.com/help-center/contact-us/
  - button "Footnotes section content" [expanded] [ref=e240]:
    - paragraph [ref=e241]: Footnotes
  - paragraph [ref=e243]:
    - text: Learn more about
    - link "FDIC insurance coverage" [ref=e244]:
      - /url: https://www.capitalone.com/bank/fdic/
    - text: .
  - paragraph [ref=e247]:
    - text: Zelle® and the Zelle® related marks are wholly owned by Early Warning Services, LLC and are used herein under license.
    - text: To send or receive money with Zelle®, both parties must have an eligible checking or savings account.
  - paragraph [ref=e257]: ©2026 Capital One
  - link "Privacy" [ref=e260]:
    - /url: https://www.capitalone.com/privacy/
  - link "AdChoices" [ref=e262]:
    - /url: https://www.capitalone.com/privacy/online-privacy-policy#collecting-using-sharing-information-accordion-button-5
  - link "Your California Privacy Choices" [ref=e264]:
    - /url: https://www.capitalone.com/privacy/your-california-privacy-choices/
    - text: Your California Privacy Choices
    - img [ref=e265]
  - link "Terms & Conditions" [ref=e267]:
    - /url: https://www.capitalone.com/digital/terms-conditions/
  - 'link "Member: FDIC" [ref=e269]':
    - /url: https://www.fdic.gov/
  - img "Equal Housing Opportunity" [ref=e270]
```
is_error:No
snapshot_artifact:{
  "filename": "browser_snapshot_5483dbe55159.txt",
  "s3_key": "session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_5483dbe55159.txt",
  "s3_uri": "s3://dev-olivaw-iris-s3.us-east-1.cognitive/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_5483dbe55159.txt",
  "local_path": "/tmp/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/browser_snapshot_5483dbe55159.txt",
  "content_hash": "5483dbe55159d97cde21640053c898a398cb5184cd543e03820c855a21a89f8e"
}

Input
query:Capital One "Anti-Corruption" "Bribery" policy site:capitalone.com
Output
result:Capital One maintains a zero-tolerance policy toward bribery and corruption, governed primarily by its **Enterprise Anti-Bribery and Anti-Corruption (ABAC) Standard** and its **Code of Business Conduct and Ethics**. The policy strictly prohibits offering, soliciting, or accepting anything of value to improperly influence business decisions, specifically adhering to the U.S. Foreign Corrupt Practices Act (FCPA) and the U.S. Bank Bribery Act (BBA).

### **Policy Evidence & Key Provisions**
*   **Core Prohibitions:** Associates, officers, and directors are prohibited from offering, providing, soliciting, or accepting bribes of any kind in connection with Capital One business. This includes "facilitation payments" (small payments to speed up routine government actions).
*   **Legal Compliance:**
    *   **Bank Bribery Act (BBA):** Prohibits corruptly soliciting or accepting offers from suppliers, service providers, or customers.
    *   **Foreign Corrupt Practices Act (FCPA):** Prohibits bribing foreign government officials (broadly defined to include employees of state-owned entities) to obtain a business advantage.
*   **Gifts and Entertainment:** The policy forbids seeking or accepting gifts, entertainment, or preferential treatment that could compromise—or appear to compromise—professional judgment. Any gift-related conduct involving financial institutions is treated with heightened scrutiny due to criminal statutes.
*   **Third-Party Requirements:** Capital One’s **Third Party Code of Business Conduct and Ethics** extends these anti-corruption requirements to vendors, suppliers, and contractors. Third parties must report if any Capital One associate seeks a gift or gratuity.
*   **Reporting Mechanisms:** Suspected violations must be reported immediately via:
    *   The **Ethics Line** (available 24/7, allows for anonymous reporting).
    *   The **Ethics Office** or a direct manager.
*   **Non-Retaliation:** Capital One strictly prohibits retaliation against any individual who reports a potential violation in good faith or cooperates in an investigation.

**Sources:**
[1] capitalone.com — https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf
[2] capitalone.com — https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-final.pdf
[3] corporate-ir.net — http://media.corporate-ir.net/media_files/irol/70/70667/corpgov/code_2007.pdf
[4] q4cdn.com — https://s206.q4cdn.com/737820215/files/doc_downloads/2025/09/Burford-Capital-Anti-Bribery-and-Anti-Corruption-Policy.pdf
[5] gcs-web.com — https://ir-capitalone.gcs-web.com/static-files/06fb4034-f0ae-4291-9e14-0731b749f013

```json citations
[
  {
    "ref": 1,
    "type": "web",
    "title": "capitalone.com",
    "url": "https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf"
  },
  {
    "ref": 2,
    "type": "web",
    "title": "capitalone.com",
    "url": "https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-final.pdf"
  },
  {
    "ref": 3,
    "type": "web",
    "title": "corporate-ir.net",
    "url": "http://media.corporate-ir.net/media_files/irol/70/70667/corpgov/code_2007.pdf"
  },
  {
    "ref": 4,
    "type": "web",
    "title": "q4cdn.com",
    "url": "https://s206.q4cdn.com/737820215/files/doc_downloads/2025/09/Burford-Capital-Anti-Bribery-and-Anti-Corruption-Policy.pdf"
  },
  {
    "ref": 5,
    "type": "web",
    "title": "gcs-web.com",
    "url": "https://ir-capitalone.gcs-web.com/static-files/06fb4034-f0ae-4291-9e14-0731b749f013"
  }
]
```

Input
content:Located Capital One sources that contain anti-bribery/anti-corruption requirements (Capital One Third Party Code of Conduct PDF on capitalone.com).
item_id:1
Output
status:ok
items:[
  {
    "id": "1",
    "content": "Located Capital One sources that contain anti-bribery/anti-corruption requirements (Capital One Third Party Code of Conduct PDF on capitalone.com).",
    "status": "done",
    "priority": "high"
  },
  {
    "id": "2",
    "content": "Extract key policy details (scope, prohibitions, gifts/hospitality, third parties, reporting) with citations",
    "status": "in_progress",
    "priority": "high"
  },
  {
    "id": "3",
    "content": "Deliver cited summary and direct link(s) to the policy",
    "status": "pending",
    "priority": "high"
  }
]
is_complete:No
reason:2 items still not done: "Extract key policy details (scope, prohibitions, g", "Deliver cited summary and direct link(s) to the po"
message:Marked todo item 1 as done.

Input
group_key:document_processing
Output
status:activated
group_key:document_processing
label:Document Processing
bundle:default
activation_entry:document_processing
available_tool_names:[
  "read_document",
  "index_document",
  "get_node_data",
  "get_page_content",
  "check_index_status"
]
contains_long_running_tools:Yes
additional_available_bundles:[]
remaining_inactive_groups:[
  "crawl4ai",
  "code_execution"
]
active_tool_groups:[
  "document_processing",
  "google_search",
  "playwright:core"
]
persisted:Yes
message:Tool group document_processing activated with bundle default. Continue now with: read_document, index_document, get_node_data, get_page_content, check_index_status

IMPORTANT — read_document vs index_document are TWO SEPARATE PATHS:
• read_document is the DEFAULT. Always try it FIRST for any document.
• index_document is a FALLBACK for PDFs only — use it ONLY when read_document failed or returned insufficient content, or the user explicitly asks for structural/TOC navigation.
Do NOT call both on the same document. Start with read_document.

Next: call load_tool_guide(guide_key='document-processing') before using this bundle's concrete tools.
after_tool_callback_injected:[
  "read_document",
  "index_document",
  "get_node_data",
  "get_page_content",
  "check_index_status"
]
agent_tool_names:[
  "file_write",
  "file_read",
  "file_read_lines",
  "file_list",
  "file_move_to_s3",
  "file_query_json",
  "file_search",
  "load_tool_guide",
  "todo_create",
  "todo_item_done",
  "todo_add_items",
  "todo_update_item",
  "todo_get",
  "list_tool_groups",
  "activate_tool_group",
  "mark_task_complete",
  "google_search",
  "browser_navigate",
  "browser_navigate_back",
  "browser_navigate_forward",
  "browser_snapshot",
  "browser_click",
  "browser_type",
  "browser_evaluate",
  "browser_wait_for",
  "browser_close",
  "browser_upload_to_s3",
  "browser_page_save_as_pdf",
  "browser_save_as_html",
  "read_document",
  "index_document",
  "get_node_data",
  "get_page_content",
  "check_index_status"
]

Input
guide_key:document-processing
Output
guide_key:document-processing
title:Document Processing
content:# Document Processing Guide

**Default:** Always start with `read_document`. Only escalate to `index_document` when `read_document` fails, the user explicitly asks for structural navigation, or you judge the task requires it.

## Default path — `read_document` (5 CALLS PER SESSION)

**Use for ALL documents by default**, regardless of size or format (PDF, DOCX, CSV, RTF, HTML, text, code, etc.).

**HARD LIMIT:** `read_document` may be called **at most 5 times per session**. It is an expensive LLM-powered extraction. Calls beyond the limit **will be rejected by the system**.

**PARALLEL CALLS FOR MULTIPLE DOCUMENTS:**
When you need to read N documents, call `read_document` N times **in the same turn** — one call per document. All calls are processed in parallel on the backend. Do NOT combine `read_document` with non-`read_document` tools in the same turn.

**QUERY STRATEGY — MAKE EACH CALL COUNT:**
Your `user_query` should be a comprehensive query covering all aspects of the user's request for that document:
- When in doubt, make the query **broader** rather than narrower — the tool returns up to 75K characters of relevant content.
- Use the same `user_query` for each document if the extraction goal is the same for all of them.

**Workflow:**
1. Read the user's full request carefully and synthesize a comprehensive `user_query`.
2. Call `read_document(path=..., user_query=...)` for each document — all in the **same turn**, one call per document. `path` accepts both S3 URIs and HTTP/HTTPS URLs.
3. If any call returns `status='queued'` or `next_step='wait'`, **STOP** — you will be auto-resumed when **all** background processing completes.
4. Answer the user's question directly from the returned text.

**IMPORTANT — `read_document` returns COMPLETE content:**
The returned text is the full query-relevant extraction (up to 75 K characters), not a preview. Answer the user's question directly from this text. A backup is saved to S3 (`_backup_file`) for later reference if the conversation compacts, but do NOT issue `file_search` or `file_query_json` on the backup to re-read the same content.

**AFTER ALL 5 CALLS ARE USED — follow-up questions:**
1. **First:** Re-examine the previous `read_document` results already in context. Most follow-up questions can be answered from content already extracted.
2. **If genuinely insufficient:** EXPLAIN to the user what information is missing and why the initial extraction did not cover it, then escalate to the `index_document` path.
3. **Do NOT attempt to call `read_document` again** — it will be rejected.

## Escalation path — `index_document` → `get_page_content`

**Escalate to this path ONLY when one of these conditions is met:**
1. **`read_document` failed or was insufficient** — it returned a guardrail error (file too large), or the extracted content did not contain the information needed to answer the query.
2. **User explicitly asks** for structural navigation, TOC browsing, or section-by-section analysis.
3. **You judge upfront that `read_document` will not be enough** — e.g. the task requires comparing many specific sections across a large report, extracting data from dozens of scattered pages, or building a comprehensive multi-facet analysis where a single query-focused extraction is unlikely to capture everything.
4. **PDF ONLY** — `index_document` does NOT work on DOCX, XLSX, CSV, HTML, or any non-PDF format. For non-PDF files, `read_document` is the only option; if it fails, report the limitation.

**Workflow (strictly sequential, stage-gated):**
1. `index_document(s3_path)` — call **alone**, then **STOP and WAIT** for auto-resume.
2. The result includes a `nodes` list (Table of Contents) with `node_id`, `title`, `start_page`, `end_page` for every section. **You already have the structure** — do NOT call `get_node_data(node_ids=None)`.
3. `get_page_content(s3_path, start_page, end_page)` — call **alone** to extract specific page ranges identified from the nodes list.
4. `get_node_data(s3_path, node_ids=[...])` — **only if** you need richer summaries for specific sections. Always pass explicit `node_ids`.

Each of these tools is asynchronous and MUST be called alone in its own turn.

## Decision quick-reference

| Condition | Use |
|---|---|
| Any document, any size — first attempt | `read_document` (5 calls max — batch per turn) |
| Non-PDF file (DOCX, XLSX, CSV, HTML, text, code…) | `read_document` — only option |
| Follow-up question after `read_document` | Answer from the already-extracted content |
| Already-extracted content genuinely insufficient | Explain to user, then escalate to `index_document` |
| Multiple documents to read | Call `read_document` N times in the **same turn** — one call per document |
| User explicitly asks for TOC / section hierarchy | `index_document` path |
| Multi-section deep analysis you judge needs structural nav | `index_document` path |

## Input rules — CRITICAL

`read_document` has a single **`path`** parameter that accepts both:
1. **HTTP/HTTPS URL** — pass the URL directly. The worker downloads it with retries. **Do NOT first upload to S3 with `file_move_to_s3` or `browser_upload_to_s3`** — that wastes a step. Just pass the URL.
2. **S3 URI** — a full `s3://<bucket>/...` URI for documents already in S3 (from file uploads, `browser_upload_to_s3`, etc.).

**When you have a URL, always pass it directly as `path`.**

**S3 path rules (when using S3 URIs):**
- The S3 URI must be the **exact, full URI** copied verbatim from a prior tool response (`browser_upload_to_s3` → `s3_uri`, file upload notification, or another document tool result).
- A valid URI looks like: `s3://<bucket>/uploads/<user>/<session>/<uuid>/<filename.ext>`
- **NEVER** shorten, truncate, or fabricate an S3 URI.
- **NEVER** pass just `s3://uploads`, `s3://uploads/<user>`, or `s3://bucket/uploads` — the path must include the full key down to the filename.
- If you don't have the exact URI, look back in the conversation for the `s3_uri` field returned by the upload or attachment tool.
- For `get_node_data` and `get_page_content`, always re-use the exact same `s3_path` that was passed to `index_document`.

## NOT FOR JSON FILES

Neither `read_document` nor `index_document` process JSON files. For JSON data in session files, use `file_query_json` or `file_search` instead.

## Rules

- **NEVER use code execution, file_read, file_query_json, or file_search to read or parse documents.** When a document is received (PDF, DOCX, XLSX, CSV, RTF, HTML, text, code, etc.), you MUST call `read_document` first. Do not upload the document to S3 and write code to extract data from it — `read_document` handles all formats with intelligent extraction.
- Always follow the `next_step` field returned by the tools.
- Do not combine document tools with unrelated tool calls in the same turn.
- If the tool response says to wait, stop and wait for resume.
- If indexing fails, use browser fallback for page inspection.

## Web-sourced document rule (HTML/HTM financial reports, filings, etc.)

When a web page IS itself a document (e.g. a financial report, SEC filing, annual report, or prospectus served as HTML/HTM) — as opposed to a page that merely contains a link to a document — use one of these workflows:

1. **Direct URL known:** `read_document(path=url, user_query=...)` — pass the URL directly, no upload step needed.
2. **Direct link to a downloadable file (PDF, DOCX, etc.):** `read_document(path=url, user_query=...)` — same as above.
3. **Page reached via navigation/auth (no direct URL):** `browser_save_as_html()` → `read_document(path=s3_uri, user_query=...)`

Do NOT use `crawl_url` or iterative `browser_snapshot`/`browser_evaluate` loops to read document-like HTML pages. The document tools handle HTML parsing with table preservation and intelligent extraction far better than manual scraping.

## Citation rules

- Every document tool result includes a `source_document` field with `filename` and `s3_path`. Use the `filename` value as the `file` field in your JSON citation.
- For `get_page_content` results, `source_document` also includes `page_range`. Include it as the `pages` field.
- For `read_document` results:
  - The returned PDF content contains **`[Page N]` markers** at the start of each page. Use these markers to determine the exact page numbers for your citations.
  - For large documents with intelligent extraction, chunks that don't start at a page boundary are annotated with **`[Pages N–M]`** markers showing their page range.
  - `source_document.page_range` gives the overall page range of the returned content. `source_document.selected_page_ranges` (when present) lists the individual page ranges that were selected by intelligent extraction.
  - When citing specific facts, use the page number from the nearest `[Page N]` or `[Pages N–M]` marker above the fact in the content — **not** the aggregate `page_range`.
- **Page-level specificity is mandatory.** Each citation must reference the specific page(s) where the cited fact appears. Create separate citation entries (`ref` numbers) for facts from different pages. Example: if a revenue figure is on page 8 and an AUM figure is on page 13, use `[1]` with `"pages": "8"` and `[2]` with `"pages": "13"` — NOT a single `[1]` with `"pages": "1-46"`.
- **Anti-pattern:** Never cite an entire document with a single citation covering all pages (e.g. `"pages": "1-46"`). This defeats the purpose of page-level citation. Always break citations down by the specific page(s) where each fact appears.
- Always provide a descriptive `title` for each document citation (e.g. the section name or topic).
- Use the `quote` field for short verbatim excerpts when relevant.
- Do not emit document excerpts as fenced code blocks or separate quote blocks when a matching document citation exists.
- When extracting multiple facts from different page ranges of the same document, use separate citation entries per page range.
group_key:document_processing
active:Yes
active_bundles:[
  "default"
]
already_loaded:No

Input
path:https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf
user_query:Extract all sections and statements related to anti-corruption, anti-bribery, gifts, entertainment, facilitation payments, interactions with government officials, third-party/vendor obligations, reporting (ethics line / hotline), non-retaliation, and consequences for violations. Provide page-numbere…
Output
important:This text is the COMPLETE query-relevant extraction, not a preview. Answer the user's question directly from this content. Do NOT call index_document until unless absolutely necessary — read_document already returned the full extraction. Do NOT call file_search or file_query_json on the backup — the…
success:Yes
status:completed
tool_name:read_document
next_step:use_content
message:Successfully read .PDF document: document-reader-url-d47xy_82.pdf

Source: Document: document-reader-url-d47xy_82.pdf
File size: 410.9 KB
Pages: 16
Text length: 31481 characters

Content:
[Page 1]
Third Party Code of Business
Conduct and Ethics
November 2023

[Page 2]
Letter From Capital One’s Chief…
metadata:{
  "fileName": "document-reader-url-d47xy_82.pdf",
  "extension": ".pdf",
  "source": "S3",
  "fileSize": "410.9 KB",
  "fileSizeBytes": 420793,
  "pages": 16,
  "pdfInfo": {
    "title": "2023 Capital One Third Party Code of Conduct",
    "author": "",
    "subject": "",
    "creator": "Adobe InDe…
s3_path:https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf
source_document:{
  "filename": "third-party-code-of-conduct-10.24.pdf",
  "s3_path": "https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf",
  "total_pages": 16,
  "page_range": "1–16"
}
_backup_file:read_document_output_1785399485432_e9136736.json
_backup_s3_key:session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/read_document_output_1785399485432_e9136736.json
_backup_s3_uri:s3://dev-olivaw-iris-s3.us-east-1.cognitive/session-files/35a6efca-099f-4bce-82d8-39aa03e4cde5/read_document_output_1785399485432_e9136736.json

Input
content:Extracted anti-corruption/anti-bribery-related requirements, gifts/entertainment restrictions, third-party scope, and reporting/non-retaliation mechanisms from Capital One's Third Party Code of Business Conduct and Ethics (PDF) with page-level citations.
item_id:2
Output
status:ok
items:[
  {
    "id": "1",
    "content": "Located Capital One sources that contain anti-bribery/anti-corruption requirements (Capital One Third Party Code of Conduct PDF on capitalone.com).",
    "status": "done",
    "priority": "high"
  },
  {
    "id": "2",
    "content": "Extracted anti-corruption/anti-bribery-related requirements, gifts/entertainment restrictions, third-party scope, and reporting/non-retaliation mechanisms from Capital One's Third Party Code of Business Conduct and Ethics (PDF) with page-level citations.",
    "status": "done",
    "priority": "high"
  },
  {
    "id": "3",
    "content": "Deliver cited summary and direct link(s) to the policy",
    "status": "in_progress",
    "priority": "high"
  }
]
is_complete:No
reason:1 items still not done: "Deliver cited summary and direct link(s) to the po"
message:Marked todo item 2 as done.

Input
content:Delivered direct link to Capital One’s anti-bribery/anti-corruption-related policy source and provided a cited summary of key provisions (gifts/gratuities, bribery/corruption, reporting & non-retaliation) from the company’s Third Party Code of Business Conduct and Ethics.
item_id:3
Output
status:ok
items:[
  {
    "id": "1",
    "content": "Located Capital One sources that contain anti-bribery/anti-corruption requirements (Capital One Third Party Code of Conduct PDF on capitalone.com).",
    "status": "done",
    "priority": "high"
  },
  {
    "id": "2",
    "content": "Extracted anti-corruption/anti-bribery-related requirements, gifts/entertainment restrictions, third-party scope, and reporting/non-retaliation mechanisms from Capital One's Third Party Code of Business Conduct and Ethics (PDF) with page-level citations.",
    "status": "done",
    "priority": "high"
  },
  {
    "id": "3",
    "content": "Delivered direct link to Capital One’s anti-bribery/anti-corruption-related policy source and provided a cited summary of key provisions (gifts/gratuities, bribery/corruption, reporting & non-retaliation) from the company’s Third Party Code of Business Conduct and Ethics.",
    "status": "done",
    "priority": "high"
  }
]
is_complete:Yes
reason:All items done.
message:Marked todo item 3 as done.

Input
outcome:success
summary:Located and summarized Capital One anti-bribery/anti-corruption-related requirements from Capital One’s Third Party Code of Business Conduct and Ethics PDF, including gifts/entertainment, FCPA, reporting and non-retaliation, and noted that a likely standalone policy URL returned 404.
final_response:Capital One’s anti-corruption / anti-bribery requirements appear to be addressed (at least publicly for vendors) in its **Third Party Code of Business Conduct and Ethics** (Nov 2023), rather than on a standalone capitalone.com page titled exactly “Anti‑Corruption and Bribery Policy.” The direct URL I attempted for a likely standalone page returned a “Page Not Found.” [1]

## Direct policy source (Capital One-hosted PDF)
- **Third Party Code of Business Conduct and Ethics (November 2023)**: https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf [2]

## Key anti-corruption / anti-bribery provisions (highlights)
### Scope / applicability
- The Third Party Code “documents key principles… and further sets out business conduct expectations of our third parties,” and third parties are expected to follow “the most restrictive applicable requirements.” [3]

### Gifts, gratuities, and entertainment (Bank Bribery Act)
- The document states the **Bank Bribery Act** “strictly prohibits” Capital One associates and representatives from corruptly soliciting or accepting offers from suppliers/service providers/customers, and it further states third parties **may not offer things of value** to Capital One associates to secure/retain business. [4]
- Third parties must inform Capital One if a Capital One associate seeks a gift or gratuity “by emailing the Ethics Office.” [4]

### Bribery and corruption (FCPA)
- The document describes the **Foreign Corrupt Practices Act (FCPA)** and notes that it (and other countries’ anti-corrupt practices laws) prohibits bribing government officials for business benefits/advantages; and it details prohibited “anything of value” transfers to specified foreign officials/parties/candidates (and family members) to influence decisions, induce unlawful acts, or secure improper advantage. [4]
- It emphasizes that third parties (and their employees/agents/subcontractors) must ensure compliance if representing Capital One or engaging others on Capital One’s behalf outside the U.S., and that they cannot avoid responsibility by acting through others or ignoring red flags. [4]

### Reporting, good-faith requirement, and non-retaliation
- The document states that third-party personnel can use the **Capital One Ethics Line** to report suspected unethical/illegal activities; reports must be made in good faith; and retaliation against anyone reporting suspected violations in good faith is prohibited (and may result in termination of the third-party employee’s engagement and the third-party relationship). [5]
- Ethics Line details: operated by an external company; staffed 24/7; reports can be anonymous and are not recorded; provides phone numbers by region and a web reporting option. [5]

## Note on the requested “Anti-Corruption and Bribery Policy” web page
- When I navigated to a plausible URL for a standalone policy page on capitalone.com, the site returned “Capital One - Page Not Found.” [1]

```json citations
[
  {
    "ref": 1,
    "type": "web",
    "title": "Capital One - Page Not Found",
    "url": "https://www.capitalone.com/page-not-found/"
  },
  {
    "ref": 2,
    "type": "web",
    "title": "third-party-code-of-conduct-10.24.pdf",
    "url": "https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf"
  },
  {
    "ref": 3,
    "type": "document",
    "file": "third-party-code-of-conduct-10.24.pdf",
    "pages": "5",
    "title": "Applicability (Third Party Code)"
  },
  {
    "ref": 4,
    "type": "document",
    "file": "third-party-code-of-conduct-10.24.pdf",
    "pages": "11",
    "title": "Gifts, Gratuities, and Entertainment; Bribery and Corruption (Bank Bribery Act and FCPA)"
  },
  {
    "ref": 5,
    "type": "document",
    "file": "third-party-code-of-conduct-10.24.pdf",
    "pages": "16",
    "title": "Contact Information and Escalation Procedures (Ethics Line; non-retaliation)"
  }
]
```
Output
status:complete
final_response:Capital One’s anti-corruption / anti-bribery requirements appear to be addressed (at least publicly for vendors) in its **Third Party Code of Business Conduct and Ethics** (Nov 2023), rather than on a standalone capitalone.com page titled exactly “Anti‑Corruption and Bribery Policy.” The direct URL I attempted for a likely standalone page returned a “Page Not Found.” [1]

## Direct policy source (Capital One-hosted PDF)
- **Third Party Code of Business Conduct and Ethics (November 2023)**: https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf [2]

## Key anti-corruption / anti-bribery provisions (highlights)
### Scope / applicability
- The Third Party Code “documents key principles… and further sets out business conduct expectations of our third parties,” and third parties are expected to follow “the most restrictive applicable requirements.” [3]

### Gifts, gratuities, and entertainment (Bank Bribery Act)
- The document states the **Bank Bribery Act** “strictly prohibits” Capital One associates and representatives from corruptly soliciting or accepting offers from suppliers/service providers/customers, and it further states third parties **may not offer things of value** to Capital One associates to secure/retain business. [4]
- Third parties must inform Capital One if a Capital One associate seeks a gift or gratuity “by emailing the Ethics Office.” [4]

### Bribery and corruption (FCPA)
- The document describes the **Foreign Corrupt Practices Act (FCPA)** and notes that it (and other countries’ anti-corrupt practices laws) prohibits bribing government officials for business benefits/advantages; and it details prohibited “anything of value” transfers to specified foreign officials/parties/candidates (and family members) to influence decisions, induce unlawful acts, or secure improper advantage. [4]
- It emphasizes that third parties (and their employees/agents/subcontractors) must ensure compliance if representing Capital One or engaging others on Capital One’s behalf outside the U.S., and that they cannot avoid responsibility by acting through others or ignoring red flags. [4]

### Reporting, good-faith requirement, and non-retaliation
- The document states that third-party personnel can use the **Capital One Ethics Line** to report suspected unethical/illegal activities; reports must be made in good faith; and retaliation against anyone reporting suspected violations in good faith is prohibited (and may result in termination of the third-party employee’s engagement and the third-party relationship). [5]
- Ethics Line details: operated by an external company; staffed 24/7; reports can be anonymous and are not recorded; provides phone numbers by region and a web reporting option. [5]

## Note on the requested “Anti-Corruption and Bribery Policy” web page
- When I navigated to a plausible URL for a standalone policy page on capitalone.com, the site returned “Capital One - Page Not Found.” [1]

```json citations
[
  {
    "ref": 1,
    "type": "web",
    "title": "Capital One - Page Not Found",
    "url": "https://www.capitalone.com/page-not-found/"
  },
  {
    "ref": 2,
    "type": "web",
    "title": "third-party-code-of-conduct-10.24.pdf",
    "url": "https://ecm.capitalone.com/WCM/stories/pdfs/third-party-code-of-conduct-10.24.pdf"
  },
  {
    "ref": 3,
    "type": "document",
    "file": "third-party-code-of-conduct-10.24.pdf",
    "pages": "5",
    "title": "Applicability (Third Party Code)"
  },
  {
    "ref": 4,
    "type": "document",
    "file": "third-party-code-of-conduct-10.24.pdf",
    "pages": "11",
    "title": "Gifts, Gratuities, and Entertainment; Bribery and Corruption (Bank Bribery Act and FCPA)"
  },
  {
    "ref": 5,
    "type": "document",
    "file": "third-party-code-of-conduct-10.24.pdf",
    "pages": "16",
    "title": "Contact Information and Escalation Procedures (Ethics Line; non-retaliation)"
  }
]
```
summary:Located and summarized Capital One anti-bribery/anti-corruption-related requirements from Capital One’s Third Party Code of Business Conduct and Ethics PDF, including gifts/entertainment, FCPA, reporting and non-retaliation, and noted that a likely standalone policy URL returned 404.
outcome:success