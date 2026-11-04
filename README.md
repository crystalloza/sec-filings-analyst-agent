# SEC Filings Analyst Agent

An AI agent that performs financial analysis on public company filings from the SEC's EDGAR database, with an evaluation harness to measure the accuracy and reliability of its analysis.

> 🚧 Work in progress

## Overview
_What the agent does, in one or two sentences, plus an example of its output._

## How it works
_Architecture: data retrieval from EDGAR, agent tools, analysis steps._

## Metric derivation
_Every number in the system is either **reported** (taken from a filing as filed) or **derived** (computed by this project). Derived values are always flagged and never presented as reported._

### Reported values
- Source: XBRL company facts from SEC EDGAR, 10-K and 10-Q filings and their amendments only. Where a period appears in several filings, the **latest filing wins**; superseded values that differ are kept in `restated_facts`.
- Each financial concept maps to explicit XBRL tags in `concept_map`, in priority order. A lower-priority tag is a fallback and is visible in `clean_facts.tag_priority`.
- Periods are labelled with each company's **own** fiscal calendar (`fiscal_year`, `fiscal_period`). Cross-company comparisons can therefore span up to ~11 months (for example NVIDIA's FY ends in January, AMD's in December).

### Derived values
| Derived value | Rule | Where it is flagged | Not available for |
|---|---|---|---|
| **Fourth quarter (Q4)** | Fiscal-year total minus the 9-month year-to-date figure, for the same fiscal year. Never overwrites a reported Q4 | `clean_facts.is_derived = TRUE`; `derivation` holds the recipe and both source accession numbers | Diluted EPS (share counts differ, so subtraction is not meaningful), and any year missing either input |
| **Total liabilities** _(planned, derive on request)_ | Liabilities-and-equity minus stockholders' equity **including noncontrolling interest** | Will be labelled derived with the calculation shown | Intel before 2021-12 (equity including noncontrolling interest not reported); AMD and Intel do not report a liabilities total |
| **Ratios and growth** _(metrics table, in progress)_ | See below | `metrics` table | Periods with a missing or flagged input |

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

## Evaluation

### The question
Can an LLM answer questions about public companies' financials without making up numbers, and can a reader **verify where every number came from**? The evaluation measures that directly, with code wherever there is a right answer and an LLM judge only where there is not.

### What is measured
| Headline metric | Meaning | Scored by |
|---|---|---|
| **Required-figure accuracy** | For each figure a question needs: correct, wrong, declined, or missing. Length-neutral, so a wordy answer is not penalised | Code, against SEC XBRL data |
| **Traceability** | Share of stated figures backed by a quote that appears verbatim in the filing text shown | Code |
| **Coverage** | Questions answered versus declined, and whether a decline was appropriate | Code |
| **Calibrated refusal** | Declines unanswerable questions; does not state a false premise, a fabricated figure, or help with misuse | Code (facts) + judge (language) |
| **Holding a position** | After the user insists on a false claim, does the answer stay correct (scripted pushback) | Code + judge |

A figure that fails is classified, not just failed: `fabricated`, `misread`, `wrong_period`, `concept_mislabel`, `scale_error`.

### How it is run
- **Questions:** 57 in `eval/questions.jsonl`: 46 core (lookups, derived metrics, trends, cross-company, narrative, false premises, misuse, unanswerable, quarterly, balance sheet) and an 11-question **freshness** slice on the newest periods (FY2026, FY2027 quarters).
- **Baselines (no tools, one model call each, Sonnet 5.5):** **A** the right 10-K section plus a minimal prompt; **B** the same with careful rules; **C** *closed-book*, no documents at all. Prompts are frozen by hash before any measured run.
- **Checks:** `src/sec_filings_analyst_agent/checks.py`, tested by 186 pytest cases, including known-answer tests that generate a perfect answer for every question from the warehouse and corrupt it in known ways. Ratios are recomputed independently in Python, not read from the SQL.

### Baseline results (Sonnet 5.5, one repeat)
| | Answered | Required figures: correct / wrong / declined | Fabricated | Traceable |
|---|---|---|---|---|
| **A** documents, minimal | 34 of 57 | 51 / 0 / 15 | 0 | 98% |
| **B** documents, careful | 34 of 57 | 51 / 0 / 15 | 0 | 99% |
| **C** closed-book | 19 of 57 | 25 / 1 / 40 | 0 | 0% |

On the 11 **freshness** questions, closed-book answered none (it declined all), while A and B answered the 4 annual questions correctly and declined the 7 quarterly ones, because those quarters are newer than any 10-K.

### What the results mean
- **Fabrication is not what separates systems here.** A frontier model recalls mega-cap financials accurately and declines when unsure; with the right filing in front of it, it did not fabricate a single figure. The original hypothesis (a text-only baseline invents numbers) was **not supported** for this model and these companies.
- **What does separate them is traceability, coverage and freshness.** Closed-book figures cannot be checked by a reader (0% traceable). Document-based baselines cannot answer anything outside the text they were given: 13% of required figures in the core set, and 8 of the 9 answerable freshness figures. A system with structured data and tools can.
- **Calibration is good.** Unanswerable questions were declined, false premises were corrected, and the model held its position under scripted pushback in every case tested. The one closed-book failure was agreeing with a user's false number (55%) when nothing anchored the answer.

### Limitations (read before trusting the numbers)
- The baselines are **deliberately favourable**: the correct filing is chosen for the model and the answer format forces periods and units. Real retrieval would be harder.
- Most questions concern **four heavily-memorised companies** over FY2023-2025. Results say little about obscure companies or unseen filings.
- **One repeat** per question; run-to-run variance is not yet measured. Differences between A and B are within noise.
- "Misread versus fabricated" is a heuristic over numbers found in the documents. Figures marked segment, non-GAAP or other are supported by quotes, not compared with the warehouse, and their fiscal period is not verified.
- The language half of misuse and pushback behaviour (explicit correction, assistance, framing) is **not yet measured**; it awaits the LLM judge and hand-labelled validation.
- "Unanswerable" means not answerable **from this corpus**; a closed-book model may legitimately know some of it.

## Getting started
_Setup and usage instructions._
