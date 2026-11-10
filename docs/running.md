# Running it

Setup and commands. Nothing here is needed to read the results; the README and `docs/evaluation.md` have those.

## Setup
You need [uv](https://docs.astral.sh/uv/) and, for anything that calls a model, an Anthropic API key. The SEC asks for a contact email in every request, so you need one of those too. It only lives in your local `.env`, which is gitignored.

```bash
uv sync
cp .env.example .env     # then fill in SEC_CONTACT_EMAIL and ANTHROPIC_API_KEY
```

**Build the data** (no API key needed; the downloaded data is not in the repo). Everything except the embeddings step takes under a minute. Tests that need the database or the search index skip with a clear reason until you have built them.
```bash
uv run python -m sec_filings_analyst_agent.fetch
uv run python -m sec_filings_analyst_agent.flatten
uv run python -m sec_filings_analyst_agent.fetch_filings
uv run python -m sec_filings_analyst_agent.parse_filings
uv run python -m sec_filings_analyst_agent.build
uv run python -m sec_filings_analyst_agent.filing_search --build   # local embeddings for text search, 5-15 min depending on the machine
uv run pytest
```
Some tests compare against the database and the search index, so run them after the build. The two tool servers run with `uv run python -m sec_filings_analyst_agent.mcp_facts` and `...mcp_filings`.

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

**A note on reproducing the results.** The data is downloaded live from the SEC, so a build far in the future can differ from the one behind these results (new filings, restatements). The committed run logs and their scored output (`eval/runs/`, `eval/judge_runs/`) are the record of what I measured. Rescoring them with `checks` against a fresh build reproduces the numbers in the README, which I checked from a clean clone.
