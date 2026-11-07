# Data pipeline and metric definitions

The detail behind the README: how the SEC data becomes a warehouse the agent can query, and exactly how every number is defined.

## The pipeline
```
EDGAR financial data ──► raw ──► clean ──► metrics ──┐
EDGAR 10-K documents ──► filing text ────────────────┴──► agent tools (MCP servers)
```
Everything lives in one DuckDB file that I can delete and rebuild from the SQL.

- **raw:** the SEC's reported numbers, saved untouched.
- **clean:** one row per company, concept and period. The latest filing wins, restated values are kept aside, and anything I had to fall back or derive is flagged.
- **metrics:** margins and growth rates. If any input is missing, the value is empty with a reason. It is never computed from partial data.
- **filing text:** the MD&A, business and risk sections of each 10-K, split into paragraphs with stable ids.

Three rules run through all of it:
1. **Derived is never presented as reported.** Q4 numbers, for example, are the year minus nine months, and they are labelled that way.
2. **Refuse instead of guess.** If the data isn't there, the answer is "I don't have that."
3. **Code checks facts, a judge checks language.** Numbers are verified against the SEC data in code. The LLM judge only scores what code can't, like whether a false claim was corrected. The judge itself is validated against hand labels.

**The agent:** a model (Sonnet 5.5) that calls two small MCP servers. The tools take set parameters like ticker and period, never raw SQL.
- **`sec-facts`** (numbers): every value comes back as a record with a readable id like `NVDA:revenue:FY2025:FY:10-K`, a link to the filing on EDGAR, and flags for derived or restated values (with the original value and why it changed). Missing data comes back empty with a reason.
- **`sec-filings`** (text): search by meaning over the 10-K paragraphs, using a small embedding model that runs on the laptop, so the text never leaves the machine.

It gets at most 8 tool calls per question, must cite a record id or quote for every figure, and may use general knowledge only if it says so. It is scored on the same questions, checks and judge as the baselines. The design decisions are in [`docs/agent_design.md`](docs/agent_design.md).

How the testing works, and what it found, is in the Evaluation section below.

## Metric derivation
_Every number in the system is either **reported** (taken from a filing as filed) or **derived** (computed by this project). Derived values are always flagged and never presented as reported._

### Reported values
- Source: XBRL company facts from SEC EDGAR, 10-K and 10-Q filings and their amendments only. Where a period appears in several filings, the **latest filing wins**; superseded values that differ are kept in `restated_facts`.
- Each financial concept maps to explicit XBRL tags in `concept_map`, in priority order. A lower-priority tag is a fallback and is visible in `clean_facts.tag_priority`.
- Periods are labelled with each company's **own** fiscal calendar (`fiscal_year`, `fiscal_period`). Cross-company comparisons can therefore span up to ~11 months (for example NVIDIA's FY ends in January, AMD's in December).

### Derived values
- **Fourth quarter (Q4):** fiscal-year total minus the 9-month figure. It never overwrites a reported Q4, and it isn't available for diluted EPS (share counts differ) or any year missing an input.
- **Total liabilities:** AMD and Intel don't report it, so it is liabilities-and-equity minus equity including noncontrolling interest (parent equity where there is none). It is labelled derived. Where Intel's equity including noncontrolling interest isn't available (FY2018 and the quarters before 2022), it is refused, not guessed.
- **Ratios and growth:** see below.

Derived rows are flagged `is_derived` with the recipe and source filings.

**Why Q4 is derived:** companies do not file a fourth quarter on its own; the 10-K covers the whole year. Stand-alone Q4 facts exist only through FY2020 for AMD, Microsoft and NVIDIA (and never for Intel), because 10-Ks used to include a quarterly-results table that is no longer required. After FY2020, Q4 is therefore available only as a derived value.

**How the Q4 derivation was validated:** for the 134 company-concept-years that have both a reported Q4 and the two inputs, annual minus 9-month reproduces the reported Q4 exactly in 115 cases. The 19 differences are restatement effects before the FY2018 window (flagged) and five NVIDIA net-income values off by $1 million because the company's own quarterly figures are rounded in millions and do not sum exactly to the year. Derived Q4 values can therefore differ from a company's own Q4 by rounding.

**Flag inheritance:** a derived row is excluded from the question set if either input is restated or has a tag disagreement.

### Metric definitions (decided so far)
- **Gross margin** = reported gross profit ÷ revenue. (Revenue − cost of revenue equals reported gross profit exactly in all 150 period cells checked, kept as a data-quality check.)
- **Operating margin** = operating income ÷ revenue. **Net margin** = net income ÷ revenue. **R&D intensity** = R&D expense ÷ revenue.
- Metrics are computed for **annual and quarterly periods only** (no year-to-date), keyed by company, fiscal year and fiscal period.
- If any input is missing or flagged, the metric is **empty with a `null_reason`**; it is never computed from partial data.
- **Year-over-year growth** (annual and quarterly): this period versus the same fiscal period one year earlier. **Quarter-over-quarter growth** (quarterly periods and balance-sheet items): versus the immediately preceding quarter (Q1 versus the prior year's Q4), which includes seasonality. Growth starts at FY2019; FY2018 is the base year.
- Growth is computed for 12 line items: revenue, cost of revenue, gross profit, operating income, net income, R&D, diluted EPS, total assets, total liabilities, stockholders' equity, cash and inventory.
- **Not meaningful (empty with `prior_period_not_positive`)** when the prior value is zero or negative, the standard finance convention. A swing from profit to loss therefore has no stored growth rate; the underlying values show it.
- Every metric row carries `uses_derived_input` so a ratio or growth rate built on a derived Q4 stays visibly derived. Diluted EPS has no Q4 after FY2020, so Q1 quarter-over-quarter EPS growth is empty (`prior_period_missing`) for those years.
- Metrics are stored as one row per company, period and metric (`metrics` table). A percentile-versus-own-history column was considered and dropped for the MVP.
