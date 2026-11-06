"""MCP server `sec-facts`: a thin wrapper over facts_tools (all logic and tests live there).

  uv run python -m sec_filings_analyst_agent.mcp_facts      # speaks MCP over stdio

Tools take fixed parameters only. There is no way to send SQL.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from sec_filings_analyst_agent import facts_tools

mcp = MCPServer("sec-facts")

@mcp.tool()
def get_concept(ticker: str, concept: str, periods: list[dict]) -> dict:
    """Reported or derived value of one financial concept for one company over up to 12 periods.

    ticker: AMD, INTC, MSFT or NVDA.
    concept: revenue, cost_of_revenue, gross_profit, operating_income, net_income, research_and_development,
      eps_diluted, total_assets, total_liabilities, stockholders_equity, cash_and_equivalents, inventory.
    periods: a list like [{"fiscal_year": 2025, "fiscal_period": "FY"}]. fiscal_period is FY, Q1-Q4, H1
      (six months, cumulative) or 9M (nine months, cumulative), in the COMPANY'S OWN fiscal calendar.
    Q4 after FY2020 and some total liabilities are derived and flagged. Cite the returned record `id`.
    Missing data comes back with value null and a missing_reason; never estimate it."""
    return facts_tools.get_concept(ticker, concept, periods)


@mcp.tool()
def compare_companies(tickers: list[str], concept: str, fiscal_year: int, fiscal_period: str = "FY") -> dict:
    """The same concept and fiscal period for 2-4 companies. For two companies the difference is computed
    in code. Fiscal calendars differ across companies; a calendar_warning says how far apart the periods end."""
    return facts_tools.compare_companies(tickers, concept, fiscal_year, fiscal_period)


@mcp.tool()
def get_metrics(ticker: str, fiscal_year: int, fiscal_period: str = "FY", metrics: list[str] | None = None) -> dict:
    """Stored ratios (gross_margin, operating_margin, net_margin, rnd_pct_revenue) and growth rates
    (<concept>_yoy_growth, <concept>_qoq_growth) for one company and period, each with the records it was computed from.
    Values are fractions (0.75 = 75%); use `display`. Empty metrics carry a missing_reason."""
    return facts_tools.get_metrics(ticker, fiscal_year, fiscal_period, metrics)


@mcp.tool()
def list_available_periods(ticker: str, concept: str) -> dict:
    """Which fiscal years and periods exist for a company and concept, with derived periods marked.
    Use this before concluding that data is missing."""
    return facts_tools.list_available_periods(ticker, concept)


if __name__ == "__main__":
    mcp.run()
