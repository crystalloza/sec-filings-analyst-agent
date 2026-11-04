"""Integration test: the checks' independent Python recomputation must agree with the SQL metrics table."""
from pathlib import Path

import duckdb
import pytest

from sec_filings_analyst_agent.build import DB_PATH
from sec_filings_analyst_agent.reference import RATIO_FORMULAS, reference_value

pytestmark = pytest.mark.skipif(not Path(DB_PATH).exists(), reason="warehouse not built")


def test_python_ratios_match_sql_metrics():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute(
        "SELECT ticker, metric, fiscal_year, fiscal_period, value FROM metrics "
        "WHERE value IS NOT NULL AND (metric IN ('gross_margin','operating_margin','net_margin','rnd_pct_revenue') "
        "OR metric = 'revenue_yoy_growth') AND duration_class IN ('annual','quarterly')"
    ).fetchall()
    assert len(rows) > 500
    mismatches = []
    for ticker, metric, fy, fp, sql_value in rows:
        concept = "revenue_growth" if metric == "revenue_yoy_growth" else metric
        py_value, _ = reference_value(con, ticker, concept, fy, fp)
        if py_value is None or abs(py_value - sql_value) > 1e-9:
            mismatches.append((ticker, metric, fy, fp, sql_value, py_value))
    assert not mismatches, mismatches[:5]
