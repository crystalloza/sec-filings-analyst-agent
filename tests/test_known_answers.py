"""Known-answer tests on the real warehouse and the real question set.

For every question with expected figures we build a PERFECT answer straight from the warehouse and the
checks must score it perfectly. Then we corrupt it in known ways and the checks must notice each time.
This is the test that catches mapping mistakes (metric names, periods, derived items) that unit tests miss.
"""
import json
from pathlib import Path

import duckdb
import pytest

from sec_filings_analyst_agent.build import DB_PATH, ROOT
from sec_filings_analyst_agent.checks import DocsText, expected_concept, score_record
from sec_filings_analyst_agent.reference import reference_value, unit_kind

pytestmark = pytest.mark.skipif(not Path(DB_PATH).exists(), reason="warehouse not built")
QUESTIONS = [json.loads(l) for l in (ROOT / "eval" / "questions.jsonl").read_text().splitlines() if l.strip()]
WITH_EXPECTED = [q for q in QUESTIONS if q.get("expected")]
EMPTY_DOCS = DocsText.from_text("nothing relevant here")


@pytest.fixture(scope="module")
def con():
    return duckdb.connect(str(DB_PATH), read_only=True)


def perfect_figure(con, item):
    concept = expected_concept(item)
    value, _ = reference_value(con, item["company"], concept, item["fiscal_year"], item["fiscal_period"])
    assert value is not None, f"no reference for {item}"
    kind = unit_kind(concept)
    if kind == "percent":
        stated, unit, scale = round(value * 100, 1), "percent", "units"
    elif kind == "USD_per_share":
        stated, unit, scale = round(value, 2), "USD_per_share", "units"
    elif abs(value) >= 1e9:
        stated, unit, scale = round(value / 1e9, 3), "USD", "billions"
    else:
        stated, unit, scale = round(value / 1e6, 1), "USD", "millions"
    return {"company": item["company"], "metric": concept, "concept": concept, "value": stated, "unit": unit,
            "scale": scale, "fiscal_year": item["fiscal_year"], "fiscal_period": item["fiscal_period"],
            "source_quote": "", "is_computed": False}


def record_for(figures, declined=False):
    return {"run_id": "t", "question_id": "t", "variant": "A", "repeat": 0, "turn": None, "conversation_id": None,
            "mode": "dev", "parsed": {"answer": "answer", "figures": figures, "declined": declined, "missing_info": None}}


@pytest.mark.parametrize("q", WITH_EXPECTED, ids=lambda q: q["id"])
def test_perfect_answer_scores_perfectly(con, q):
    figures = [perfect_figure(con, item) for item in q["expected"]]
    result = score_record(con, q, record_for(figures), EMPTY_DOCS)
    assert [x["status"] for x in result["required"]] == ["correct"] * len(q["expected"]), result["required"]


@pytest.mark.parametrize("q", WITH_EXPECTED, ids=lambda q: q["id"])
def test_a_wrong_period_is_never_scored_correct(con, q):
    figures = [perfect_figure(con, item) for item in q["expected"]]
    for f in figures:
        f["fiscal_year"] -= 1  # same value, attributed to the previous year
    result = score_record(con, q, record_for(figures), EMPTY_DOCS)
    assert all(x["status"] != "correct" for x in result["required"])


@pytest.mark.parametrize("q", WITH_EXPECTED, ids=lambda q: q["id"])
def test_a_three_percent_error_is_never_scored_correct(con, q):
    figures = [perfect_figure(con, item) for item in q["expected"]]
    for f in figures:
        f["value"] = round(f["value"] * 1.03, 4)
    result = score_record(con, q, record_for(figures), EMPTY_DOCS)
    assert all(x["status"] != "correct" for x in result["required"])


def test_a_wrong_scale_is_called_a_scale_error(con):
    usd_questions = [q for q in WITH_EXPECTED if all(expected_concept(i) in
                     ("revenue", "net_income", "research_and_development", "inventory", "total_liabilities") for i in q["expected"])]
    assert usd_questions
    for q in usd_questions:
        figures = [perfect_figure(con, item) for item in q["expected"]]
        for f in figures:
            f["scale"] = {"billions": "millions", "millions": "billions"}[f["scale"]]
        result = score_record(con, q, record_for(figures), EMPTY_DOCS)
        assert all(f["status"] == "scale_error" for f in result["figures"]), (q["id"], [f["status"] for f in result["figures"]])


def test_api_safety_refusal_counts_as_a_decline():
    misuse = next(q for q in QUESTIONS if q["id"] == "q45")
    refused = {"run_id": "t", "question_id": "q45", "variant": "A", "repeat": 0, "turn": None, "conversation_id": None,
               "mode": "measured", "parsed": None, "error": None, "stop_reason": "refusal"}
    result = score_record(None, misuse, refused, EMPTY_DOCS)
    assert result["api_refusal"] and result["behavior"]["verdict"] == "pass"


def test_a_malformed_figure_does_not_crash_scoring(con):
    bad = {"company": "AMD", "concept": "revenue", "metric": "x", "value": float("nan"), "unit": "USD", "scale": "billions",
           "fiscal_year": 2023, "fiscal_period": "FY", "source_quote": "", "is_computed": False}
    result = score_record(con, QUESTIONS[0], record_for([bad]), EMPTY_DOCS)
    assert result["figures"][0]["status"] in ("check_error", "fabricated", "misread", "unsupported", "scale_error", "wrong_period")
