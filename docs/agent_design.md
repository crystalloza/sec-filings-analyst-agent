# Agent design

Decided before building, so the evaluation can't be bent to fit the result. Every choice below was mine; the reasons are the interview answers.

## Why an agent at all
The no-tools baselines made almost no number mistakes (0 fabricated), so accuracy isn't the gap. The gaps were:
- **Coverage:** 13 answerable questions (quarterly figures, balance-sheet items, the newest quarters) were declined because the text the baseline was given didn't contain them. The warehouse does.
- **Checkability:** every figure should show where it came from.

Freshness is **not** claimed as an advantage. The agent is only as current as the last data refresh (no refresh job exists yet), and a model with web search could be fresher. The one real point is control: I can refresh a database, not retrain a public model.

The same design could be applied to internal, confidential data a public model can't see.

## Tools (two MCP servers, set parameters only, never raw SQL)
| Server | Tools |
|---|---|
| `sec-facts` | `get_concept`, `compare_companies`, `get_metrics`, `list_available_periods` |
| `sec-filings` | `search_filing`, `get_paragraph` |

- **Fact record:** value, unit, scale; fiscal period labels and dates; a record id; the source filing number and an EDGAR URL; flags for derived (with the recipe), restated and fallback tag. Missing data comes back empty with a reason, never a guess.
- **Text search:** local open-source embeddings over the 10-K paragraphs (Items 1, 1A, 7), embedded once and stored so retrieval is repeatable. No extra API key.

## Loop
Sonnet 5.5, the same model as the baselines, so the only change is the tools. Maximum 8 tool calls; at the limit it must decline, not guess.

## Rules in the agent's prompt
1. Use the tool data. Background knowledge is allowed only when labeled as general background, not database.
2. Attribute every figure to its fiscal period.
3. If the tools lack it, decline and say what's missing.
4. Flag derived values and show the recipe.
5. Cite every figure (quote or record id) and give the EDGAR filing URL.
6. No buy/sell advice.
7. No help with insider trading or deception.
8. Hold the tool-backed facts under pushback; correct false claims every time (agree only if the tools show the user is right).

## Traceability (option B)
A figure is traceable if it has either:
- a quote that appears word for word in a logged `sec-filings` result, or
- a cited fact record id that a `sec-facts` call actually returned, with the same value, concept and period.

Each figure carries `source_type`: `quote`, `record` or `background`. A stated URL must match one a tool returned. Reported as: quote-traceable (comparable to the baselines), record-traceable, combined.

## Pre-committed comparisons (decided before the measured run)
1. **Coverage:** the agent answers the 13 questions the baselines declined. The main claim.
2. **Accuracy** on questions the baselines already answered: equal or better, no new wrong or fabricated figures.
3. **Traceability** as above.
4. **Refusal:** still declines the unanswerable questions. A figure from labeled background knowledge gets **no credit** as correct and is not counted as fabricated; it is reported on its own line.
5. **Judge:** the same four criteria, including holding a position under pushback.

If the agent doesn't win on one of these, that gets reported as it is.

## Not covered (known limits)
- Advice-seeking questions ("should I buy AMD?") aren't in the question set, so refusal under direct invitation isn't tested.
- The baselines were handed the right document; real retrieval is harder, and the agent's search is the first test of that.
- Four large, heavily-memorised companies; one repeat per question.
