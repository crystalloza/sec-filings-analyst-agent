import duckdb
import pytest

from sec_filings_analyst_agent.checks import DocsText, check_figure


@pytest.fixture()
def con():
    c = duckdb.connect()
    c.execute("""CREATE TABLE clean_facts (ticker VARCHAR, concept VARCHAR, role VARCHAR, fiscal_year INTEGER,
                 fiscal_period VARCHAR, val DOUBLE, is_derived BOOLEAN, period_end DATE)""")
    c.execute("INSERT INTO clean_facts VALUES ('AMD','revenue','reported',2023,'FY',22.68e9,FALSE,DATE '2024-01-01')")
    return c


DOCS = DocsText.from_text("Net revenue $ 22,680 $ 23,601 (4)% in fiscal 2023")


def fig(**kw):
    base = dict(company="AMD", concept="revenue", metric="revenue", scope="company_total", value=22.68, unit="USD",
                scale="billions", fiscal_year=2023, fiscal_period="FY", source_quote="", is_computed=False)
    return {**base, **kw}


def test_a_correct_figure_without_a_quote_is_right_but_not_traceable(con):
    r = check_figure(con, fig(), DOCS)
    assert r["status"] == "verified_warehouse" and r["traceable"] is False


def test_a_correct_figure_with_a_verified_quote_is_traceable(con):
    r = check_figure(con, fig(source_quote="Net revenue $ 22,680 $ 23,601"), DOCS)
    assert r["status"] == "verified_warehouse" and r["traceable"] is True


def test_a_quote_without_the_figures_number_is_not_traceable(con):
    r = check_figure(con, fig(source_quote="Net revenue $ 23,601 (4)% in fiscal"), DOCS)
    assert r["traceable"] is False


def test_computed_figures_have_no_traceability(con):
    assert check_figure(con, fig(is_computed=True), DOCS)["traceable"] is None
