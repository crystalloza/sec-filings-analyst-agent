"""sec-facts tools against the real warehouse (build it first). Checks ids, flags, missing-data behavior, no guessing."""

import pytest

from sec_filings_analyst_agent import facts_tools as f
from pathlib import Path

from sec_filings_analyst_agent.build import DB_PATH

pytestmark = pytest.mark.skipif(not Path(DB_PATH).exists(), reason="warehouse not built (see docs/running.md)")


@pytest.fixture(scope="module")
def con():
    c = f.connect()
    yield c
    c.close()


def test_reported_fact_has_readable_id_value_and_url(con):
    r = f.get_fact(con, "NVDA", "revenue", 2025, "FY")
    assert r["id"] == "NVDA:revenue:FY2025:FY:10-K"
    assert r["value"] == 130_497_000_000 and r["display"] == "$130,497 million"
    assert r["period_start"] == "2024-01-29" and r["period_end"] == "2025-01-26"
    assert r["source_urls"][0].startswith("https://www.sec.gov/Archives/edgar/data/1045810/")
    assert r["missing_reason"] is None and "derived" not in r["flags"]


def test_derived_q4_is_flagged_with_recipe_and_two_sources(con):
    r = f.get_fact(con, "AMD", "cost_of_revenue", 2024, "Q4")
    assert r["id"].endswith(":derived") and r["is_derived"] and "derived" in r["flags"]
    assert "minus" in r["derivation"] and len(r["source_urls"]) == 2 and r["form"] is None


def test_derived_total_liabilities_matches_reference_and_is_labelled(con):
    from sec_filings_analyst_agent.reference import derived_total_liabilities
    r = f.get_fact(con, "AMD", "total_liabilities", 2024, "FY")
    assert r["is_derived"] and r["id"].endswith(":derived") and "derived" in r["flags"]
    assert r["value"] == derived_total_liabilities(con, "AMD", 2024, "FY")
    assert r["derivation"] and r["source_urls"]


def test_reported_total_liabilities_is_not_marked_derived(con):
    r = f.get_fact(con, "MSFT", "total_liabilities", 2024, "FY")
    assert not r["is_derived"] and r["id"].endswith(":10-K")


def test_missing_data_is_empty_with_reason_never_a_value(con):
    r = f.get_fact(con, "AMD", "eps_diluted", 2024, "Q4")
    assert r["value"] is None and r["id"] is None and "Q4" in r["missing_reason"]
    r = f.get_fact(con, "NVDA", "revenue", 2040, "FY")
    assert r["value"] is None and r["id"] is None and r["missing_reason"]


def test_restated_value_returns_original_and_explanation(con):
    r = f.get_fact(con, "INTC", "revenue", 2022, "H1")
    assert "restated" in r["flags"]
    rs = r["restatement"]
    assert rs["original_value"] != r["value"] and "restated" in rs["explanation"]


def test_year_to_date_periods_say_so(con):
    r = f.get_fact(con, "NVDA", "revenue", 2025, "9M")
    assert "year_to_date" in r["flags"] and "cumulative" in r["note"]


def test_bad_inputs_return_errors_not_exceptions(con):
    assert "error" in f.get_concept("TSLA", "revenue", [{"fiscal_year": 2025}], con=con)
    assert "error" in f.get_concept("NVDA", "ebitda", [{"fiscal_year": 2025}], con=con)
    assert "error" in f.get_concept("NVDA", "revenue", [{"fiscal_year": 2025, "fiscal_period": "Q9"}], con=con)["records"][0]
    assert "error" in f.get_concept("NVDA", "revenue", [], con=con)
    assert "error" in f.get_concept("NVDA", "revenue", [{"fiscal_year": y} for y in range(2010, 2024)], con=con)
    assert "error" in f.compare_companies(["NVDA"], "revenue", 2025, con=con)


def test_bad_period_is_rejected(con):
    out = f.get_fact(con, "NVDA", "revenue", 2025, "Q9")
    assert "error" in out


def test_compare_companies_warns_on_calendar_gap_and_does_the_arithmetic(con):
    out = f.compare_companies(["NVDA", "AMD"], "inventory", 2025, "FY", con=con)
    assert "calendar_warning" in out
    a, b = out["records"]
    assert out["comparison"]["absolute_difference"] == pytest.approx(abs(a["value"] - b["value"]))
    assert set(out["comparison"]["uses_records"]) == {a["id"], b["id"]}


def test_compare_with_one_missing_does_no_arithmetic(con):
    out = f.compare_companies(["NVDA", "AMD"], "revenue", 2040, "FY", con=con)
    assert "comparison" not in out


def test_metrics_carry_inputs_and_match_stored_values(con):
    out = f.get_metrics("NVDA", 2025, "FY", ["gross_margin", "revenue_yoy_growth"], con=con)
    gm = {r["metric"]: r for r in out["records"]}["gross_margin"]
    assert gm["id"] == "NVDA:gross_margin:FY2025:FY:metric" and gm["display"] == "75.0%"
    assert {i["id"] for i in gm["computed_from"]} == {"NVDA:gross_profit:FY2025:FY:10-K", "NVDA:revenue:FY2025:FY:10-K"}
    yoy = {r["metric"]: r for r in out["records"]}["revenue_yoy_growth"]
    assert len(yoy["computed_from"]) == 2


def test_empty_metric_keeps_its_reason(con):
    row = con.execute("SELECT ticker, fiscal_year, fiscal_period, metric, null_reason FROM metrics "
                      "WHERE value IS NULL AND ticker IN ('NVDA','AMD','INTC','MSFT') LIMIT 1").fetchone()
    out = f.get_metrics(row[0], row[1], row[2], [row[3]], con=con)
    assert out["records"][0]["value"] is None and out["records"][0]["missing_reason"] == row[4]


def test_list_available_periods_marks_derived(con):
    out = f.list_available_periods("AMD", "cost_of_revenue", con=con)
    y = {x["fiscal_year"]: x for x in out["years"]}
    assert "Q4" in y[2024]["derived_periods"] and "FY" in y[2024]["periods"]


def test_every_record_id_is_unique_per_period(con):
    out = f.get_concept("NVDA", "revenue", [{"fiscal_year": y, "fiscal_period": p} for y in (2024, 2025) for p in ("Q1", "FY")], con=con)
    ids = [r["id"] for r in out["records"]]
    assert len(ids) == len(set(ids)) == 4


def test_intel_total_liabilities_refused_where_equity_incl_nci_is_missing(con):
    """No silent fallback to parent equity: that would overstate liabilities by the noncontrolling interest."""
    r = f.get_fact(con, "INTC", "total_liabilities", 2020, "Q1")
    assert r["value"] is None and r["id"] is None and r["missing_reason"]
    r = f.get_fact(con, "INTC", "total_liabilities", 2018, "FY")
    assert r["value"] is None


def test_intel_fy2019_total_liabilities_is_derived_from_both_filings(con):
    r = f.get_fact(con, "INTC", "total_liabilities", 2019, "FY")
    assert r["value"] == pytest.approx(59_020_000_000) and r["is_derived"]
    assert len(r["source_urls"]) == 2   # liabilities-and-equity and the equity row come from different filings


def test_malformed_periods_return_errors_not_exceptions(con):
    assert "error" in f.get_concept("NVDA", "revenue", [{"fiscal_period": "FY"}], con=con)
    assert "error" in f.get_concept("NVDA", "revenue", [{"fiscal_year": "2025"}], con=con)
    assert "error" in f.get_concept("NVDA", "revenue", ["2025"], con=con)


def test_compare_rejects_bad_ticker_concept_and_period(con):
    assert "error" in f.compare_companies(["NVDA", "TSLA"], "revenue", 2025, con=con)
    assert "error" in f.compare_companies(["NVDA", "AMD"], "ebitda", 2025, con=con)
    assert "error" in f.compare_companies(["NVDA", "AMD"], "revenue", 2025, "Q9", con=con)
    assert "error" in f.compare_companies(["NVDA", "NVDA"], "revenue", 2025, con=con)


def test_every_restated_row_in_the_window_builds_an_explanation(con):
    rows = con.execute("SELECT ticker, concept, fiscal_year, fiscal_period FROM clean_facts WHERE in_window AND was_restated "
                       "AND role = 'reported' AND concept IN (%s)" % ",".join(f"'{c}'" for c in f.REPORTED)).fetchall()
    assert rows
    for t, c, y, p in rows:
        r = f.get_fact(con, t, c, y, p)
        assert "restated" in r["flags"] and r["restatement"] and "restated" in r["restatement"]["explanation"], (t, c, y, p)


def test_every_in_window_fact_yields_a_unique_valid_record(con):
    rows = con.execute("SELECT DISTINCT ticker, concept, fiscal_year, fiscal_period FROM clean_facts WHERE in_window "
                       "AND role = 'reported' AND concept IN (%s)" % ",".join(f"'{c}'" for c in f.REPORTED)).fetchall()
    ids = set()
    for t, c, y, p in rows:
        r = f.get_fact(con, t, c, y, p)
        assert r["id"] and r["value"] is not None and r["source_urls"], (t, c, y, p)
        assert r["id"] not in ids, r["id"]
        ids.add(r["id"])


def test_derived_records_carry_no_form_or_accession_of_their_own(con):
    for args in (("AMD", "cost_of_revenue", 2024, "Q4"), ("AMD", "total_liabilities", 2024, "FY")):
        r = f.get_fact(con, *args)
        assert r["is_derived"] and r["form"] is None and r["accession"] is None and r["source_urls"]


def test_mcp_tool_text_lists_exactly_the_real_concepts():
    import inspect
    from sec_filings_analyst_agent import mcp_facts
    doc = inspect.getdoc(mcp_facts.get_concept)
    for c in f.REPORTED:
        assert c in doc


# ----------------------------------------------------------------------------- gaps found by the coverage report
def test_prior_period_rules():
    assert f._prior(2025, "Q1", "qoq") == (2024, "Q4")        # Q1 compares with the previous fiscal year's Q4
    assert f._prior(2025, "Q3", "qoq") == (2025, "Q2")
    assert f._prior(2025, "FY", "qoq") is None
    assert f._prior(2025, "Q2", "yoy") == (2024, "Q2")


def test_qoq_metric_cites_the_prior_quarter_records(con):
    out = f.get_metrics("NVDA", 2025, "Q1", ["revenue_qoq_growth"], con=con)
    rec = out["records"][0]
    ids = [i["id"] for i in rec["computed_from"]]
    assert rec["value"] is not None and len(ids) == 2
    assert ids[0].startswith("NVDA:revenue:FY2025:Q1:") and ids[1].startswith("NVDA:revenue:FY2024:Q4:")


def test_tools_work_without_a_supplied_connection():
    """The MCP servers call the tools this way: each call opens and closes its own connection."""
    assert f.get_concept("NVDA", "revenue", [{"fiscal_year": 2025, "fiscal_period": "FY"}])["records"][0]["value"] == 130_497_000_000
    assert f.compare_companies(["NVDA", "AMD"], "revenue", 2025)["records"][0]["id"]
    assert f.get_metrics("NVDA", 2025, "FY", ["gross_margin"])["records"][0]["display"] == "75.0%"
    assert f.list_available_periods("NVDA", "revenue")["years"]


def test_tag_disagreement_is_flagged(con):
    row = con.execute("SELECT * FROM clean_facts WHERE role = 'reported' AND NOT is_derived LIMIT 1")
    d = dict(zip([c[0] for c in row.description], row.fetchone()))
    assert "tag_disagreement" not in f._row_to_record(con, d)["flags"]
    assert "tag_disagreement" in f._row_to_record(con, {**d, "has_tag_disagreement": True})["flags"]


def test_metrics_and_periods_reject_bad_inputs_and_report_empty_results(con):
    assert "error" in f.get_metrics("TSLA", 2025, "FY", con=con)
    assert "error" in f.get_metrics("NVDA", 2025, "Q9", con=con)
    out = f.get_metrics("NVDA", 2025, "FY", ["no_such_metric"], con=con)
    assert out["records"] == [] and out["missing_reason"]
    assert "error" in f.list_available_periods("TSLA", "revenue", con=con)
    assert "error" in f.list_available_periods("NVDA", "ebitda", con=con)


def test_periods_for_a_concept_a_company_does_not_report_says_so_and_mentions_derivation(con):
    out = f.list_available_periods("AMD", "total_liabilities", con=con)
    assert out["years"] == [] and out["missing_reason"] and "DERIVED" in out["note"]


def test_display_formats():
    assert f.display(None, "USD", "revenue") is None
    assert f.display(750_000, "USD", "revenue") == "$750,000"
    assert f.display(1_500_000, "USD", "revenue") == "$1.500 million"
    assert f.display(2.94, "USD/shares", "eps_diluted") == "$2.94 per share"
    assert f.display(0.7499, "ratio", "gross_margin") == "75.0%"
    assert f.display(-0.052, "ratio", "revenue_yoy_growth") == "-5.2%"
