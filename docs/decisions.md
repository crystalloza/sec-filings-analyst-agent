# Design decision log

The choices that shaped this project, with the reasoning and what each one cost. Each entry: the decision, why, and the road not taken. Where a decision was later corrected or reversed, that is included, because those are the ones I learned most from.

## Scope and data

**1. Evaluation first, on a starting set of four companies.**
Why: I wanted to practice the whole lifecycle (data, tests, agent, results) quickly, so I scoped a thin slice. NVIDIA, AMD, Intel and Microsoft were chosen because their fiscal years end in different months (January, December, June) and their XBRL tags behave differently.
Cost: the companies are heavily memorised, so the results say little about obscure ones.

**2. A local DuckDB database with plain SQL files, not a cloud platform.**
Why: it rebuilds from scratch with one command, I can read every layer, and the SQL stays portable (raw → clean → metrics, the same idea as bronze/silver/gold).
Cost: it runs on one machine. I kept the SQL portable on purpose, so it could move to a cloud platform.

**3. Latest filing wins; restated values are kept aside and kept out of the questions.**
Why: when a company restates a figure, "the" right answer is ambiguous. An evaluation can't catch errors in its own answer key, so I handled ground truth upstream: 265 of 5,027 fact rows are flagged and excluded from the question set.
Cost: "as originally reported" views are out of scope, and EPS is split-adjusted.

**4. An explicit allowlist maps each concept to XBRL tags, not a search for tags by name.**
Why: a company's revenue tag changes over time, and a name search pulls in pro-forma and deferred revenue. The mapping table makes the judgment call visible, and a lower-priority tag shows as a fallback.
Cost: a manual table. The Intel cash fallback is also unverified in the years where only that tag exists, which is a stated limitation.

**5. Q4 is derived (year minus nine months) and always labelled derived.**
Why: companies don't file a stand-alone Q4, so refusing every Q4 question would throw away answerable ones. The recipe reproduced reported Q4 exactly in 115 of 134 cases, and the misses are explained (restatements and company rounding). Diluted EPS is excluded because subtraction doesn't work for it.
Cost: derived Q4 can differ from a company's own Q4 by rounding.

**6. Ratios and growth live in a stored table, not in the agent's head.**
Why: a model shouldn't do arithmetic on a finance question, and one governed definition per metric beats several prompt-time versions. The checks recompute the same ratios independently in Python, so a bug in the SQL can't pass both the agent and the check. If any input is missing, the metric is empty with a reason, never computed from partial data.
Cost: more up-front modeling.

**7. Total liabilities is derived for AMD and Intel, and refused where the inputs aren't there. This one was corrected later.**
Why: neither reports it. Using parent-only equity would overstate Intel's liabilities by its noncontrolling interest, a plausible-looking wrong number, so the derivation uses equity including it.
Correction: I had marked Intel FY2019 as "must decline." Building the agent's tools showed later filings supply that equity figure, so it is derivable. I relabeled the question and re-scored the baselines (no new model calls).

## Evaluation

**8. Build the tests before the agent, with three no-tools baselines on the same model.**
Why: using the agent's model for the baselines isolates what the tools add. A is a minimal prompt, B has careful rules, and C is closed-book (no documents), which acts as a control.
Cost: I gave the baselines the right filing, which flatters them. That's disclosed.

**9. Code checks facts, a judge checks language.**
Why: numbers have a right answer in the database, so code should score them. The judge only handles what code can't, such as whether a false claim was corrected, which shrinks the part of the evaluation that is itself unreliable.
Cost: two scoring systems to build and keep consistent.

**10. When the first result contradicted my hypothesis, I changed the thesis, not the data.**
Why: I expected plain models to fabricate numbers. They fabricated none. Coverage and checkability turned out to be the real gaps, so traceability became a headline metric and the agent was built for that.

**11. Freeze the setup by hash before any measured run, and say what changed during development.**
Why: it makes tuning to the test visible. I froze the baseline prompts and, later, the agent (prompt, answer format, tool definitions, model, effort).
Cost: I still refined the agent with dev runs on the same 57 questions, so they are not a held-out test, and I wrote that into the README as a limitation. A held-out set is what I'd do differently next time.

## The judge

**12. Four binary criteria, one call each, with a short reason, a confidence flag, and a three-vote majority. Opus judges Sonnet.**
Why: binary is easier to label and to score with kappa than a 1-5 scale. Separate calls stop one criterion bleeding into another, and ties fail, the safe direction for a safety check. The judge is a different, stronger model than the one it grades, because its quality caps how far every other result can be trusted.
Cost: three calls per item on the strongest model.

**13. The judge was validated against my blind labels before I trusted it, using planted failures.**
Why: a judge can be too lenient, too harsh, or fooled by confident wording. Real failures were rare, so I had Claude write 31 deliberately flawed answers to test that the judge catches them, and I reported kappa with a confidence interval, not raw agreement.
Cost: the seeded answers are synthetic, so agreement overstates how it would do on a natural mix.

## The agent

**14. Two MCP servers with fixed parameters and no raw SQL, used for the boundary and not for accuracy.**
Why: fixed parameters bound what the agent can do and make its reasoning auditable, which is the governance point. I said plainly that direct function calls would have given the same answers. MCP earns its place through the explicit boundary, reuse, and one clean place to log.

**15. Every figure is a record with a readable id and a filing link, and traceability is strict.**
Why: a reader should be able to follow any number to its source. A figure counts only if its cited record or quote was actually returned by a tool, with the right value, period and concept. A calculation the check can't redo counts as not traceable. "Correct" and "traceable" are scored separately, because a number can be right and unsourced, or cited and wrong.
Cost: the agent's traceability (96%) is level with the baselines, not better.

**16. General knowledge is allowed only if the agent says so, and earns no credit.**
Why: refusing outright would make it less useful, but unlabelled memory would undermine the whole point. Labelled background figures aren't counted as correct or as fabricated. They get their own line.

**17. A local embedding model for text search.**
Why: running the embedding model locally means the filing text never leaves the machine, which matches the confidential-data use case I'm pointing at, and needs no extra API key. I chose meaning-based search over simple keyword search because it finds paraphrases that keyword search misses.
Cost: a heavier dependency, and retrieval that is fuzzier and harder to test than keyword matching.

**18. Eight tool calls maximum, and the tool list never changes mid-conversation.**
Why: 8 came from the design and held up in the data (the most any Sonnet run used was 6). The "never change the tool list" part came from a bug: the model's reasoning is bound to the exact tools it was given, and swapping the list at the limit made the API reject the conversation. The limit is enforced by rejecting extra calls instead.

**19. I kept the quote check conservative and unchanged.**
Why: it ignores small bare numbers and number words. Fixing it would have moved the published baseline numbers. Parity between the agent and the baselines mattered more than a slightly higher agent score.
