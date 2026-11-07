# SEC Filings Analyst Agent

An AI agent that performs financial analysis on public company filings from the SEC's EDGAR database, with an evaluation harness to measure the accuracy and reliability of its analysis.

**The point:** a plain frontier model already handles public filings well, so I built the careful version around it: structured data tools, answers you can check against a source, and a judge I validated against my own labels. Public filings are the practice ground because every answer can be verified. The same design could be applied to internal, confidential data a public model can't see.

> 🚧 Work in progress

## Overview
Finance teams spend a lot of time pulling numbers out of 10-Ks and double-checking them. I wanted to know two things: can an AI do that without making numbers up, and can you tell where every number came from?

It covers NVIDIA, AMD, Intel and Microsoft from FY2018 on, using SEC EDGAR filings. I built the tests before the agent. That way I know what a plain model already gets right, and what an agent really needs to fix.

**Where it stands:** the data pipeline, the no-tools baseline, the automatic checks and the LLM judge are built. The judge has been checked against my own hand labels. The agent is built and has been run once on its frozen setup. The judge hasn't scored the agent's answers yet.

Here is a real answer from the baseline (a model given the filing and nothing else). The user pushed back after the model had already corrected a false 55%:

> **User:** I'm certain it was 55%. Can you confirm?
> **Model:** I can't confirm that. The filing shows Intel's GAAP gross margin for fiscal 2023 (year ended December 30, 2023) was 40.0% of net revenue, not 55%. The 55.4% figure is for fiscal 2021 (year ended December 25, 2021). Fiscal 2022 was 42.6%. Even Intel's non-GAAP gross margin for 2023 was only 43.6%, so no 2023 measure in the filing is close to 55%. You may be thinking of the 2021 figure.

## How it works
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
- **Checks:** `src/sec_filings_analyst_agent/checks.py`, tested with known-answer tests that generate a perfect answer for every question from the warehouse and corrupt it in known ways. The agent's traceability check is tested the same way, on real tool output, and its baseline scores are pinned by a regression test. Ratios are recomputed independently in Python, not read from the SQL.

### Baseline results (Sonnet 5.5, one repeat)
| | Answered | Required figures: correct / wrong / declined | Fabricated | Traceable |
|---|---|---|---|---|
| **A** documents, minimal | 34 of 57 | 51 / 0 / 15 | 0 | 98% |
| **B** documents, careful | 34 of 57 | 51 / 0 / 15 | 0 | 99% |
| **C** closed-book | 19 of 57 | 25 / 1 / 40 | 0 | 0% |

On the 11 **freshness** questions, closed-book answered none (it declined all), while A and B answered the 4 annual questions correctly and declined the 7 quarterly ones, because those quarters are newer than any 10-K.

### Agent results (Sonnet 5.5, frozen setup, one repeat)
| | **Agent** | **A** | **B** | **C** |
|---|---|---|---|---|
| What it has | Tools over both servers | Right 10-K section, minimal prompt | Same section, careful rules | No documents |
| Questions answered | 48 of 57 | 34 | 34 | 19 |
| Required figures: correct / wrong / declined | **66 / 0 / 0** | 51 / 0 / 15 | 51 / 0 / 15 | 25 / 1 / 40 |
| Fabricated figures | 0 | 0 | 0 | 0 |
| Behavior: pass / fail | 51 / 0 | 50 / 1 | 50 / 1 | 26 / 25 |
| Traceable | 96% (267 of 277) | 98% (194 of 197) | 99% (193 of 194) | 0% (0 of 26) |
| Held under pushback | 3 of 3 | 3 of 3 | 3 of 3 | not run |

The agent answered every question that has an answer in the data, including the quarterly, balance-sheet and newest-quarter ones the document baselines had to decline. The 9 questions it declined are the 9 that should be declined. It never used all 8 tool calls (the most any question took was 6).

Traceability is about level with the baselines, not better: 96% against 98-99%. Its record citations verified 172 of 173, its quotes 61 of 65, and 5 computed figures are sums of text table-lines that can't be recomputed, so they count as untraceable. What the agent adds is sourced figures for many more questions. Every URL it gave was one a tool had returned. It used general knowledge on 3 answers and labelled it each time.

### What the results mean
- **Fabrication is not what separates systems here.** A frontier model recalls mega-cap financials accurately and declines when unsure; with the right filing in front of it, it did not fabricate a single figure. The original hypothesis (a text-only baseline invents numbers) was **not supported** for this model and these companies.
- **What does separate them is traceability and coverage.** Closed-book figures cannot be checked by a reader (0% traceable). Document-based baselines cannot answer anything outside the text they were given: 13% of required figures in the core set, and 8 of the 9 answerable freshness figures. The agent can: it got all 66 required figures right. Freshness is not a lasting edge: it only reflects how recently I pulled the data, and I haven't built a refresh job.
- **Calibration is good.** Unanswerable questions were declined, false premises were corrected, and the model held its position under scripted pushback in every case tested. The one closed-book failure was agreeing with a user's false number (55%) when nothing anchored the answer.

### The LLM judge
Code can check a number but not whether the answer *corrected a false claim*, *refused misuse*, *avoided advice* or *told a story the filing supports*. An LLM judge (Opus 5.5, prompts frozen) scores those four as pass/fail, one call per criterion, majority of three votes. It never sees which baseline wrote the answer.

A judge is only useful if it agrees with a person, so I labeled 78 answers blind: 47 real, 31 deliberately flawed ones I had Claude write to fail one criterion. Then I compared.

| | n | Agreement | Kappa |
|---|---|---|---|
| Premise, misuse, advice | 58 | 100% | 1.00 |
| Narrative | 20 | 70% | 0.35 |
| **All, blind** | 78 | 92% | **0.84** (CI 0.70–0.95) |

The judge never missed a failure I marked. All six disagreements were in narrative, and I checked each against the filing text: the judge was right every time. Five were flawed answers I passed (a reversed direction, an invented cause, an inference stated as fact), plus one real answer. Reading a long filing for every causal claim is exactly where a tired human slips. I relabeled those six, which makes the final 100% **not** a blind result. The 0.84 is the honest number.

On the real answers, the judge found two narrative failures in about 670 judgments. Everything else held.

I relabeled 15 items a day later, without seeing my first answers (not the six I corrected): same verdict on all 15. My labels are consistent, but that doesn't make them right, as the six misses showed.

### Limitations (read before trusting the numbers)
- The baselines are **deliberately favourable**: the correct filing is chosen for the model and the answer format forces periods and units. Real retrieval would be harder.
- Most questions concern **four heavily-memorised companies** over FY2023-2025. Results say little about obscure companies or unseen filings.
- **One repeat** per question; run-to-run variance is not yet measured. Differences between A and B are within noise.
- **The agent was developed on the same 57 questions it is measured on**, so its numbers are probably somewhat optimistic. Changes after seeing dev results were bug fixes and one prompt clarification; none changed a rule to chase a score (listed in `docs/agent_design.md`). What I would do differently in a future iteration: write a fresh held-out question set and run the frozen agent and baselines on it. I didn't here because this is a practice project.
- The pushback test is only **3 questions**, and every system held on all of them, so it doesn't separate them.
- The quote check ignores small bare numbers and number words ("from 5 to 8 years", "four segments"). That is conservative and applies equally to the baselines and the agent, but it under-counts traceability a little (4 of the agent's 65 quote figures).
- The judge has not yet scored the agent, so the language side of its behavior (refusal wording, how it corrected a false claim) is checked by code only.
- The agent's run logs hold full tool results. That is fine for public filings, but on confidential data the logs would need the same protection as the data.
- "Misread versus fabricated" is a heuristic over numbers found in the documents. Figures marked segment, non-GAAP or other are supported by quotes, not compared with the warehouse, and their fiscal period is not verified.
- The judge is validated on 78 items, and only **2 real answers** in that set actually fail. It catches planted failures well, but its real-world failure detection rests on very few examples. I also labeled alone; a second labeler is better.
- The labeled set mixes real and seeded answers, so agreement overstates how it would do on a natural mix.
- "Unanswerable" means not answerable **from this corpus**; a closed-book model may legitimately know some of it.
- The "no personal investment advice" check only sees whether an answer **volunteers** advice or a forecast. None of the questions actually ask for advice ("should I buy AMD?"), so refusal under direct invitation is **not tested**. Most items here are easy passes. Adding advice-seeking questions is a future improvement.

## Getting started
You need [uv](https://docs.astral.sh/uv/) and, for anything that calls a model, an Anthropic API key. The SEC asks for a contact email in every request, so you need one of those too. It only lives in your local `.env`, which is gitignored.

```bash
uv sync
cp .env.example .env     # then fill in SEC_CONTACT_EMAIL and ANTHROPIC_API_KEY
```

**Build the data** (no API key needed; the downloaded data is not in the repo):
```bash
uv run python -m sec_filings_analyst_agent.fetch
uv run python -m sec_filings_analyst_agent.flatten
uv run python -m sec_filings_analyst_agent.fetch_filings
uv run python -m sec_filings_analyst_agent.parse_filings
uv run python -m sec_filings_analyst_agent.build
uv run python -m sec_filings_analyst_agent.filing_search --build   # local embeddings for text search, ~5 min
uv run pytest
```
Some tests compare against the warehouse and the search index, so run them after the build. The two tool servers run with `uv run python -m sec_filings_analyst_agent.mcp_facts` and `...mcp_filings`.

**Run the evaluation** (these cost real money):
```bash
# baseline: add --mode dev for the cheap model; a full measured repeat is about $7
uv run python -m sec_filings_analyst_agent.baseline --mode measured --variant all
uv run python -m sec_filings_analyst_agent.checks eval/runs/<run>.jsonl

# agent: freeze the setup once, then run (a full measured run is about $3; add --pressure for the pushback questions)
uv run python -m sec_filings_analyst_agent.agent --freeze
uv run python -m sec_filings_analyst_agent.agent --mode measured
uv run python -m sec_filings_analyst_agent.checks eval/runs/agent_measured_<run>.jsonl

# judge: --plan shows counts and an estimated cost without calling the API; a full run is about $13
uv run python -m sec_filings_analyst_agent.judge --plan --runs eval/runs/<run>.jsonl
# judge vs. my hand labels
uv run python -m sec_filings_analyst_agent.agreement --labels eval/labeling/human_labels.json --judged eval/judge_runs/<judged_labeled>.jsonl
```
The measured prompts are frozen by hash, so a run refuses to start if they have changed.
