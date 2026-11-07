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

- **Fact record:** value, unit, scale; fiscal period labels and dates; a record id; the source filing number and an EDGAR URL; flags for derived (with the recipe), restated, tag disagreement and year-to-date. The XBRL tag used is a plain field, not a flag, since about 10% of rows use a fallback tag and it isn't a caveat. Missing data comes back empty with a reason, never a guess.
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

## How the agent was developed, and what that means for the results
The agent was developed with dev runs on the same 57 questions the measured run uses, and then frozen by hash before the measured run. The questions were written before the agent existed, and the baselines were frozen before any agent work, but the agent's setup was adjusted while looking at how it behaved on these questions.

Changes made after seeing dev results:
1. The tool list stays identical on every turn. A bug fix: the API rejects a conversation whose reasoning blocks were written under a different tool list, and the first design swapped the list at the 8-call limit.
2. "Call submit_answer once" became "once per user message." A bug fix: on the pushback turn the model refused to submit a second time.
3. Logging of rejected submissions, the text of turns that never submit, and block types per turn.
4. `figures` and `missing_info` became optional in the answer format.
5. A prompt exception for a number that appears only because the user claimed it and the agent is correcting it (it no longer lists that number as a figure). This fixed a conflict between the prompt ("list every number") and the check (listing a rebutted claim counted as stating it).

None of these changed an accuracy or refusal rule to chase a score. They are bug fixes and one clarification.

**Caveat.** A fully clean evaluation would run the frozen agent on a blind set of questions it was never developed against. This version keeps the same 57 questions because it is an MVP, so the agent's numbers are likely somewhat optimistic compared with unseen questions. The comparison with the baselines is still fair on the setup (same questions, same checks, same judge), but the absolute numbers should be read with that in mind. What I would do differently in a future iteration: write a fresh held-out question set and run the frozen agent and the baselines on it. I am not doing that here because this is a practice project.
