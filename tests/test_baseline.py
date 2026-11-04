import json

from sec_filings_analyst_agent.baseline import current_config, load_done
from sec_filings_analyst_agent.checks import dedupe_resumed


def rec(qid, variant="A", repeat=0, parsed=True, error=None, turn=None, conv=None):
    return {"question_id": qid, "variant": variant, "repeat": repeat, "turn": turn, "conversation_id": conv,
            "parsed": {"declined": False} if parsed else None, "error": error}


def test_resume_skips_successes_and_retries_failures(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in [
        rec("q01"), rec("q02", parsed=False, error="APIError: overloaded"), rec("q03", variant="B")]))
    assert load_done(path) == {("q01", "A", 0, 1), ("q03", "B", 0, 1)}


def test_resumed_runs_keep_the_successful_retry():
    first = rec("q02", parsed=False, error="APIError: overloaded")
    retry = rec("q02")
    kept = dedupe_resumed([first, rec("q01"), retry])
    assert len(kept) == 2 and [r for r in kept if r["question_id"] == "q02"][0]["parsed"] is not None


def test_configuration_names_models_and_effort():
    config = current_config()
    assert config["models"]["measured"]["anthropic"] == "claude-sonnet-5-5"
    assert config["effort"]["measured"] in ("low", "medium", "high", "xhigh", "max")
