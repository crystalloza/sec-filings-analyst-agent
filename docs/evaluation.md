# Evaluation in detail

The full methodology, every results table, the judge validation and the complete list of limitations. The README has the short version.

## The question
Can an LLM answer questions about public companies' financials without making up numbers, and can a reader **verify where every number came from**? The evaluation measures that directly, with code wherever there is a right answer and an LLM judge only where there is not.

## What is measured
| Headline metric | Meaning | Scored by |
|---|---|---|
| **Required-figure accuracy** | For each figure a question needs: correct, wrong, declined, or missing. Length-neutral, so a wordy answer is not penalised | Code, against SEC XBRL data |
| **Traceability** | Share of stated figures backed by a source a reader can follow. Baselines: a quote that appears verbatim in the filing text they were shown. Agent: a record id or quote that its tools actually returned, with matching value, concept and period (computed figures are recomputed from the cited records). Strict: a figure that can't be recomputed counts as not traceable | Code |
| **Coverage** | Questions answered versus declined, and whether a decline was appropriate | Code |
| **Calibrated refusal** | Declines unanswerable questions; does not state a false premise, a fabricated figure, or help with misuse | Code (facts) + judge (language) |
| **Holding a position** | After the user insists on a false claim, does the answer stay correct (scripted pushback) | Code + judge |

A figure that fails is classified, not just failed: `fabricated`, `misread`, `wrong_period`, `concept_mislabel`, `scale_error`.

## How it is run
- **Questions:** 57 in `eval/questions.jsonl`: 46 core (lookups, derived metrics, trends, cross-company, narrative, false premises, misuse, unanswerable, quarterly, balance sheet) and an 11-question **freshness** slice on the newest periods (FY2026, FY2027 quarters).
- **Baselines (no tools, one model call each, Sonnet 5.5):** **A** the right 10-K section plus a minimal prompt; **B** the same with careful rules; **C** *closed-book*, no documents at all. Prompts are frozen by hash before any measured run.
- **Agent (Sonnet 5.5, tools over two MCP servers):** at most 8 tool calls per question; setup frozen by hash (prompt, answer format, tool definitions, model, effort) before the measured run. Design and the list of changes made during development are in `docs/agent_design.md`.
- **Checks:** `src/sec_filings_analyst_agent/checks.py`, tested with known-answer tests that generate a perfect answer for every question from the warehouse and corrupt it in known ways. The agent's traceability check is tested the same way, on real tool output, and its baseline scores are pinned by a regression test. Ratios are recomputed independently in Python, not read from the SQL.

## Baseline results (Sonnet 5.5, one repeat)
| | Answered | Required figures: correct / wrong / declined | Fabricated | Traceable |
|---|---|---|---|---|
| **A** documents, minimal | 34 of 57 | 51 / 0 / 15 | 0 | 98% |
| **B** documents, careful | 34 of 57 | 51 / 0 / 15 | 0 | 99% |
| **C** closed-book | 19 of 57 | 25 / 1 / 40 | 0 | 0% |

On the 11 **freshness** questions, closed-book answered none (it declined all), while A and B answered the 4 annual questions correctly and declined the 7 quarterly ones, because those quarters are newer than any 10-K.

## Agent results (Sonnet 5.5, frozen setup, one repeat)
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

## What the results mean
- **Fabrication is not what separates systems here.** A frontier model recalls mega-cap financials accurately and declines when unsure; with the right filing in front of it, it did not fabricate a single figure. The original hypothesis (a text-only baseline invents numbers) was **not supported** for this model and these companies.
- **What does separate them is traceability and coverage.** Closed-book figures cannot be checked by a reader (0% traceable). Document-based baselines cannot answer anything outside the text they were given: 13% of required figures in the core set, and 8 of the 9 answerable freshness figures. The agent can: it got all 66 required figures right. Freshness is not a lasting edge: it only reflects how recently I pulled the data, and I haven't built a refresh job.
- **Calibration is good.** Unanswerable questions were declined and false premises were corrected, including by the closed-book model, which said it was answering from memory. The document baselines held their position under scripted pushback in every case tested (closed-book was not run in that test). Closed-book's single "wrong" required figure (q28) is an answer that corrected the false 55% but also listed it among its figures, which the check reads as a conflicting value, not as agreement.

## The LLM judge
Code can check a number but not whether the answer *corrected a false claim*, *refused misuse*, *avoided advice* or *told a story the filing supports*. An LLM judge (Opus 5.5, prompts frozen) scores those four as pass/fail, one call per criterion, majority of three votes. It never sees which baseline wrote the answer.

A judge is only useful if it agrees with a person, so I labeled 78 answers blind: 47 real, 31 deliberately flawed ones I had Claude write to fail one criterion. Then I compared.

| | n | Agreement | Kappa |
|---|---|---|---|
| Premise, misuse, advice | 58 | 100% | 1.00 |
| Narrative | 20 | 70% | 0.35 |
| **All, blind** | 78 | 92% | **0.84** (CI 0.70–0.95) |

The judge never missed a failure I marked. All six disagreements were in narrative, and I checked each against the filing text: the judge was right every time. Five were flawed answers I passed (a reversed direction, an invented cause, an inference stated as fact), plus one real answer. Reading a long filing for every causal claim is exactly where a tired human slips. I relabeled those six, which makes the final 100% **not** a blind result. The 0.84 is the honest number.

On the real baseline answers, the judge found two narrative failures in about 670 judgments. Everything else held.

**Judge results, all four systems** (failures out of items judged; the judge never sees which system wrote an answer):

| | **Agent** | **A** | **B** | **C** |
|---|---|---|---|---|
| False claim corrected (incl. pushback turns) | 0 of 7 | 0 of 7 | 0 of 7 | 0 of 4 |
| Misuse refused | 0 of 5 | 0 of 5 | 0 of 5 | 0 of 5 |
| No investment advice | 0 of 57 | 0 of 57 | 0 of 57 | 0 of 57 |
| Narrative matches the filing text | **2 of 10** | 1 of 9 | 1 of 9 | not judged (no text) |

I read both agent narrative failures against the filings and the judge was right. In q21 the agent wrote that Intel attributed the decline to "higher one-time charges," a phrase the text it was shown never uses, and said volume was the largest driver while its own caveat said the data can't separate price from volume. In q54 it wrote that Intel weighs Arm-based rivals "as much as" AMD, right after quoting Intel calling AMD its primary competitor. The baselines' two failures were the same kind. So the one soft spot for every system is adding an interpretive claim the text doesn't support, and the agent is no better at that. These are small numbers (10 narrative answers), so I wouldn't rank the systems on them. For the agent, "the text" is the paragraphs and data records its tools returned.

I relabeled 15 items a day later, without seeing my first answers (not the six I corrected): same verdict on all 15. My labels are consistent, but that doesn't make them right, as the six misses showed.

## Limitations (read before trusting the numbers)
- The baselines are **deliberately favourable**: the correct filing is chosen for the model and the answer format forces periods and units. Real retrieval would be harder.
- Most questions concern **four heavily-memorised companies** over FY2023-2025. Results say little about obscure companies or unseen filings.
- **One repeat** per question; run-to-run variance is not yet measured. Differences between A and B are within noise.
- **The agent was built first, then refined using dev runs on the same 57 questions it is measured on**, so they are not a held-out test and its numbers are probably somewhat optimistic. Changes after seeing dev results were bug fixes, one prompt clarification and fixes to the agent's traceability check; none changed a rule to chase a score (listed in `docs/agent_design.md`). What I would do differently in a future iteration: write a fresh held-out question set and run the frozen agent and baselines on it. I didn't here because this is a practice project.
- The pushback test is only **3 questions**, and every system held on all of them, so it doesn't separate them.
- The quote check ignores small bare numbers and number words ("from 5 to 8 years", "four segments"). That is conservative and applies equally to the baselines and the agent, but it under-counts traceability a little (4 of the agent's 65 quote figures).
- The narrative check judges the agent only against what its tools returned, so an answer can fail because the agent fetched too little, not only because it over-claimed.
- The agent's run logs hold full tool results. That is fine for public filings, but on confidential data the logs would need the same protection as the data.
- "Misread versus fabricated" is a heuristic over numbers found in the documents. Figures marked segment, non-GAAP or other are supported by quotes, not compared with the warehouse, and their fiscal period is not verified.
- The judge is validated on 78 items, and only **2 real answers** in that set actually fail. It catches planted failures well, but its real-world failure detection rests on very few examples. I also labeled alone; a second labeler is better.
- The labeled set mixes real and seeded answers, so agreement overstates how it would do on a natural mix.
- "Unanswerable" means not answerable **from this corpus**; a closed-book model may legitimately know some of it.
- The "no personal investment advice" check only sees whether an answer **volunteers** advice or a forecast. None of the questions actually ask for advice ("should I buy AMD?"), so refusal under direct invitation is **not tested**. Most items here are easy passes. Adding advice-seeking questions is a future improvement.

**How I would close the remaining gaps in a future iteration:** to raise confidence in narrative answers, have the agent attach a source passage to every explanatory claim, not just every number, so the judge checks one claim against one passage instead of grading a whole story, and grow the narrative set from 10 answers to 40 or more, with repeats and a second human labeler. I would also measure retrieval on its own (does the right paragraph appear in the top 5?), to tell a retrieval miss from over-claiming. Then run the frozen agent and baselines on a fresh held-out question set.
