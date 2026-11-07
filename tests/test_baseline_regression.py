"""The baseline scores must never move when the checks are extended for the agent.

golden_baseline_headlines.json is a snapshot of the committed baseline runs scored BEFORE the agent's traceability
was added (with q39 already relabelled). If this test fails, a change to checks.py altered how the baselines are scored.
"""

import collections
import json
import shutil
from pathlib import Path

import pytest

from sec_filings_analyst_agent import checks

RUNS = Path(__file__).resolve().parents[1] / "eval" / "runs"
GOLDEN = json.loads((Path(__file__).parent / "golden_baseline_headlines.json").read_text())


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_baseline_headlines_are_unchanged(name, tmp_path):
    src = RUNS / f"{name}.jsonl"
    if not src.exists():
        pytest.skip("baseline run file not present")
    copy = tmp_path / src.name                              # score_runs writes beside its input
    shutil.copy(src, copy)
    results, _ = checks.score_runs(copy)
    ok = [r for r in results if not r["error"]]
    for variant, expected in GOLDEN[name].items():
        if variant == "pressure":
            now = dict(collections.Counter(r["pressure"]["verdict"] for r in results if r.get("pressure")))
        else:
            now = checks.headline([r for r in ok if r["variant"] == variant])
        assert now == expected, f"{name} variant {variant} changed"
