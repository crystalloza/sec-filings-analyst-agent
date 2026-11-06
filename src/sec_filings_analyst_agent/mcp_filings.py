"""MCP server `sec-filings`: a thin wrapper over filing_search (all logic and tests live there).

  uv run python -m sec_filings_analyst_agent.mcp_filings      # speaks MCP over stdio

Fixed parameters only. Quote only text these tools return: that is what the traceability check verifies against.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from sec_filings_analyst_agent import filing_search

mcp = MCPServer("sec-filings")


@mcp.tool()
def search_filing(query: str, ticker: str | None = None, fiscal_year: int | None = None,
                  item: str | None = None, k: int = 5) -> dict:
    """Search the text of the 10-K filings (Items 1 Business, 1A Risk Factors, 7 MD&A) by meaning.

    query: what you are looking for, in plain words. Use ONE focused question per search and search again for
      other topics; only the first 2000 characters are used (the result says so if it was cut).
    ticker: AMD, INTC, MSFT or NVDA (optional). fiscal_year: the 10-K's fiscal year (optional).
    item: "1", "1A" or "7" (optional). k: results to return, 1-10 (default 5).
    Results are ranked by similarity in meaning, not by whether they answer the question: read the text. Text longer
    than ~1800 characters is cut and marked truncated; call get_paragraph for the full text. When you quote a filing,
    copy the words exactly from a result and cite its paragraph_id. The full financial statements are not
    included; use the sec-facts tools for reported figures."""
    return filing_search.search_filing(query, ticker, fiscal_year, item, k)


@mcp.tool()
def get_paragraph(paragraph_id: str, neighbors: int = 0) -> dict:
    """One paragraph of a 10-K in full, by the paragraph_id a search returned.

    neighbors: 0-2 extra paragraphs before and after it from the same section, to read around a hit."""
    return filing_search.get_paragraph(paragraph_id, neighbors)


if __name__ == "__main__":
    mcp.run()
