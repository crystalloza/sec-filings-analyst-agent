# SEC Filings Analyst Agent

A practice project, built end to end: a finance data warehouse, a tool-using AI agent on top of it, and the evaluation that tells me whether to trust the agent.

The agent answers questions about public company filings, such as figures, comparisons across companies, and why something changed. I wanted to know two things: can an AI do that without making numbers up, and can a reader see where every number came from? I built the tests before the agent, so I'd know what a plain model already gets right and what an agent really has to fix. It covers NVIDIA, AMD, Intel and Microsoft from FY2018 on, using SEC EDGAR filings.

**Built with:** Python, SQL (DuckDB), the Anthropic API (Sonnet 5.5 for the agent, Opus 5.5 as the judge), MCP, a local embedding model, pytest (327 tests).
**Where to look first:** [what I found](#what-i-found), [lessons learned](#lessons-learned), then [`docs/decisions.md`](docs/decisions.md) for the reasoning behind each design choice.

## What I found
- **My starting hypothesis was wrong.** I expected a plain model to invent numbers. Given the right filing, it fabricated **0** figures. So the agent had to earn its place on something else.
- **The agent's value is coverage and sourcing, not accuracy.** It got **66 of 66** required figures right. The document baselines got 51 and had to decline 15, because the number wasn't in the text they were handed (quarterly figures, balance sheets, the newest quarters).
- **Every figure comes with a source.** 96% passed a strict check against what its tools actually returned. That is about level with the baselines (98-99%), not better, and I say so below.

**Can it be trusted?** The short answer, and the conditions on it, are at the top of [what the evaluation showed](#what-the-evaluation-showed).

## An example
The agent is asked whether Intel's fiscal 2023 gross margin was 55%. It says no (40.0%), explains the drivers, and cites its sources. Then the user pushes back:

> **User:** I'm certain it was 55%. Can you confirm?
> **Agent:** I can't confirm 55%, because the tools don't show it. Intel's gross margin for fiscal 2023 (year ended December 2023) was 40.0%. That is gross profit of $21,711 million divided by revenue of $54,228 million. [...] If you saw 55% somewhere, it may be a different measure or period, such as a non-GAAP figure, a different year, or another company's margin. I can check any of those if you tell me where the figure came from.

Behind that answer: each figure is tied to a record such as `INTC:gross_margin:FY2023:FY:metric` that the tool returned in this conversation, with a link to the filing on EDGAR. A check later confirms that the record exists, that its value, period and concept match, and that the link was one a tool actually returned.

## What this project shows about how I work
| Skill | Where it shows |
|---|---|
| Python, SQL, Git | A layered SQL warehouse (raw → clean → metrics → filing text), a tested Python package, small reviewable commits |
| LLMs, prompts, agents, MCP, evals | Frozen prompts, a tool-use loop, two MCP servers, a two-layer evaluation, a judge validated against human labels |
| Structured and unstructured data via APIs | XBRL financial facts and 10-K text, combined through the SEC and Anthropic APIs |
| Product and program judgment | Evaluation built before the agent, comparisons committed before the run, an MVP scoped on purpose, decisions written down in [`docs/decisions.md`](docs/decisions.md) and [`docs/agent_design.md`](docs/agent_design.md) |
| Responsible AI, privacy, governance | Refuse rather than guess, tools with fixed parameters (no raw SQL), a source for every figure, local embeddings, every limitation listed |
| Communication | This page, and the plain-language design notes behind it |

## How I built it
```
1  DATA         SEC EDGAR  ──►  DuckDB warehouse (raw → clean → metrics)  +  10-K text in paragraphs
      │
2  QUESTIONS    57 questions; the right answers are computed from the warehouse, not typed by hand
      │
3  BASELINES    plain model, no tools:   A minimal prompt · B careful rules · C closed-book (no documents)
      │
4  MEASURING    code checks every number against SEC data  +  an LLM judge for language
      │            the judge is validated against my own blind labels before it is trusted
      │
5  AGENT        a model with two MCP servers (numbers, filing text); cites a source for every figure
      │
6  RESULT       agent frozen by hash → one measured run → same checks and judge → compared with the three baselines
```
Three rules run through all of it: **derived is never presented as reported** (a Q4 figure is labelled as year minus nine months), **refuse instead of guess**, and **code checks facts, a judge checks language**.

Every fork in this flow, with the options I considered, why I picked one, and what it cost, is written up in the [design decision log](docs/decisions.md).

## The agent at a glance
```
question ──► Agent loop (Sonnet 5.5, at most 8 tool calls)
               │  ▲
     tool call │  │ result
               ▼  │
   ┌───────────────────────────┐    ┌───────────────────────────────┐
   │ sec-facts   (MCP server)  │    │ sec-filings   (MCP server)    │
   │ reported + derived figures│    │ search by meaning over 10-K   │
   │ from the warehouse, with  │    │ paragraphs (local embeddings, │
   │ flags and filing links    │    │ text never leaves the laptop) │
   └───────────────────────────┘    └───────────────────────────────┘
               │
               ▼
   submit_answer: prose + every figure with its source id and link
               │
               ▼
   run log (full tool results) ──► traceability check ─┐
                                ──► accuracy check vs SEC data ──► scores
                                ──► LLM judge (language) ─┘
```
- **Tools take fixed parameters, never raw SQL.** That bounds what the agent can do and makes its reasoning auditable.
- **Every number is a record with a readable id** (`NVDA:revenue:FY2025:FY:10-K`), a filing link, and flags for derived or restated values, including the original value and why it changed. Missing data comes back empty, with a reason.
- **General knowledge is allowed only if the agent says so.** It never presents it as database fact.
- **I used MCP for the boundary, not because it was needed.** For one agent in one repo, direct function calls would give the same answers. MCP makes the boundary explicit, reusable (Claude Desktop could use the same server), and a natural place to log.

## What the evaluation showed
**Can the agent be trusted? For looking up and comparing figures, and for knowing when not to answer: yes, on these questions.** It got every required figure right, fabricated none, declined exactly the questions that should be declined, and gave a checkable source for 96% of its figures. **For explanations of why something happened: not without a human check.** 2 of its 10 narrative answers added a claim the filing didn't support, the same weakness the baselines have. All of this is on four large companies and questions the agent was refined on, so treat the numbers as the best case, not a guarantee.

**How I would close the remaining gaps in a future iteration:** to raise confidence in narrative answers, have the agent attach a source passage to every explanatory claim, not just every number, so the judge checks one claim against one passage instead of grading a whole story, and grow the narrative set from 10 answers to 40 or more, with repeats and a second human labeler. Then run the frozen agent and baselines on a fresh held-out question set.

One run per system on 57 questions, Sonnet 5.5. The agent's setup was frozen by hash before its run.

| | **Agent** | **A** documents, minimal | **B** documents, careful | **C** closed-book |
|---|---|---|---|---|
| Questions answered | 48 of 57 | 34 | 34 | 19 |
| Required figures: correct / wrong / declined | **66 / 0 / 0** | 51 / 0 / 15 | 51 / 0 / 15 | 25 / 1 / 40 |
| Fabricated figures | 0 | 0 | 0 | 0 |
| Behavior: pass / fail | 51 / 0 | 50 / 1 | 50 / 1 | 26 / 25 |
| Traceable | 96% (267 of 277) | 98% (194 of 197) | 99% (193 of 194) | 0% (0 of 26) |
| Held under pushback | 3 of 3 | 3 of 3 | 3 of 3 | not run |

The LLM judge scores what code can't. Failures out of items judged:

| | **Agent** | **A** | **B** | **C** |
|---|---|---|---|---|
| False claim corrected | 0 of 7 | 0 of 7 | 0 of 7 | 0 of 4 |
| Misuse refused | 0 of 5 | 0 of 5 | 0 of 5 | 0 of 5 |
| No investment advice | 0 of 57 | 0 of 57 | 0 of 57 | 0 of 57 |
| Narrative matches the filing text | **2 of 10** | 1 of 9 | 1 of 9 | not judged |

I read both agent narrative failures against the filings, and the judge was right. In one, the agent said Intel blamed "higher one-time charges," a phrase its sources never use. In the other, it said Intel weighs Arm-based rivals "as much as" AMD, right after quoting Intel calling AMD its primary competitor.

**Can the judge be trusted? Yes, for the four things it grades, with one caution on narrative.** I labeled 78 answers blind (47 real, 31 deliberately flawed) and compared. Agreement was 92% (kappa **0.84**, CI 0.70-0.95). It never missed a failure I marked. The 6 disagreements were all long narrative answers, and checking each against the filing bore out the judge. I corrected those labels afterward, which means the relabeled 100% is not a blind result and the 0.84 is the honest number. A relabel of 15 items a day later gave the same verdicts on all 15.

Full method, per-slice results and every table: [`docs/evaluation.md`](docs/evaluation.md).

## Lessons learned
1. **Measure the baseline before building.** My hypothesis (plain models invent numbers) was wrong, and finding that out early changed what the agent was for.
2. **Fake tests can't tell you what a real API will reject.** Sonnet refused forced tool choice, and the model's reasoning is tied to the exact tool list, so swapping tools at the call limit crashed the conversation. Only running the real thing found either.
3. **Test the checker, not only the agent.** I broke my traceability check four deliberate ways, and the tests had to catch each one. A coverage report then found logic nothing was testing, such as the quarter-over-quarter rule.
4. **Validate the judge, and expect people to be wrong too.** Where my labels and the judge disagreed, it was on long documents where a tired reader skims, and the filing bore out the judge. I kept the blind score as the headline.
5. **Keep "correct" and "sourced" separate.** A figure can be right with no citation, or cited and wrong. Reporting both showed what the agent really adds.
6. **Prompt wording is part of the system.** "Call submit exactly once" made a weaker model refuse to answer the follow-up. "List every number" clashed with my own check when the user's wrong number was being rebutted. Reading the actual failures beat reading the scores.
7. **Say where the agent isn't better.** Its traceability is level with the baselines and it shares their narrative weak spot, so I reported that instead of claiming a win.
8. **Freeze first, then disclose what changed.** I froze the agent by hash and listed every change made while developing it, including that I refined it with dev runs on the questions I later measured it on.

## Limitations I would want you to know about
- **Optimistic numbers.** The agent was built first, then refined using dev runs on the same 57 questions it is measured on, so they are not a held-out test. The changes were bug fixes, one prompt clarification and fixes to the agent's traceability check (the baseline scores did not move), all listed in `docs/agent_design.md`. In a future iteration I would write a fresh held-out question set and run the frozen agent and baselines on it.
- **One repeat, four large companies.** Run-to-run variance isn't measured, and these companies are heavily memorised, so results say little about obscure ones.
- **The baselines are deliberately favourable.** I picked the right filing for them. Real retrieval would be harder.
- **Small samples in places.** The pushback test is 3 questions, and only 2 real answers in my judge-validation set actually fail.
- **Not tested:** advice-seeking questions ("should I buy AMD?"), so refusal under direct invitation is unmeasured.
- **A conservative quote check.** It ignores small bare numbers and number words, which under-counts traceability slightly, for the agent and baselines alike.
- **Logs hold full tool results.** Fine for public filings. On confidential data, they would need the same protection as the data.

The full list is in [`docs/evaluation.md`](docs/evaluation.md).

## Repo map
```
sql/        warehouse layers: raw → clean → metrics → filing text
src/        pipeline, the two MCP servers, the agent, the checks, the judge
eval/       questions, run logs, judge runs, my labels, frozen locks
docs/       decisions.md (why each choice) · agent_design.md · evaluation.md · data_and_metrics.md · running.md
tests/      327 tests, including break-it-on-purpose tests for the checks
```
Setup and commands, including what each run costs: [`docs/running.md`](docs/running.md). Metric and data definitions: [`docs/data_and_metrics.md`](docs/data_and_metrics.md).
