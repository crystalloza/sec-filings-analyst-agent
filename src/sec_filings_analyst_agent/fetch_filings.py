"""Download each company's annual reports (10-K, as HTML) from SEC EDGAR.

Same rules as fetch.py: save the SEC's response untouched to data/raw/ (gitignored), skip what
already exists, send the required User-Agent, pause between requests, fail loudly.

Which filings: every 10-K whose report date (the fiscal year-end) is on or after 2018-01-01.
For these four companies that is exactly fiscal years 2018 onward (NVIDIA's FY2018 ended
2018-01-28; AMD's and Intel's FY2017 ended 2017-12-30 and are excluded). Fiscal-year labels
themselves are assigned later, in SQL, from the fiscal_calendar table.

Run with:  uv run python -m sec_filings_analyst_agent.fetch_filings
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import requests

from sec_filings_analyst_agent.fetch import (
    PAUSE_SECONDS,
    RAW_DIR,
    REQUEST_TIMEOUT_SECONDS,
    TICKERS,
    load_headers,
    load_ticker_map,
    lookup_cik,
)

FILINGS_DIR = RAW_DIR / "filings"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodash}/{document}"
EARLIEST_REPORT_DATE = "2018-01-01"


def _get_json(url: str, headers: dict[str, str], cache_path: Path) -> dict:
    """Return a JSON document, saving the raw response the first time and reusing it afterwards."""
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    cache_path.write_bytes(response.content)  # bytes, exactly as received
    time.sleep(PAUSE_SECONDS)
    return response.json()


def _rows(block: dict) -> list[dict]:
    """Turn the SEC's column-oriented filing lists into one dict per filing."""
    keys = ["accessionNumber", "form", "filingDate", "reportDate", "primaryDocument"]
    return [dict(zip(keys, values)) for values in zip(*(block[k] for k in keys))]


def list_filings(ticker: str, cik: int, headers: dict[str, str]) -> list[dict]:
    """All filings for a company: the 'recent' list plus any older pages the SEC splits off."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    main_name = f"CIK{cik:010d}.json"
    main = _get_json(
        SUBMISSIONS_URL.format(name=main_name), headers,
        RAW_DIR / f"submissions_{ticker}_{today}.json",
    )
    filings = _rows(main["filings"]["recent"])
    for page in main["filings"].get("files", []):
        older = _get_json(
            SUBMISSIONS_URL.format(name=page["name"]), headers,
            RAW_DIR / f"submissions_{ticker}_{page['name']}",
        )
        filings += _rows(older)
    return filings


def annual_reports(filings: list[dict]) -> list[dict]:
    """10-Ks (not amendments) for the fiscal years in scope."""
    wanted = [f for f in filings if f["form"] == "10-K" and f["reportDate"] >= EARLIEST_REPORT_DATE]
    return sorted(wanted, key=lambda f: f["reportDate"])


def download_filing(ticker: str, cik: int, filing: dict, headers: dict[str, str]) -> Path:
    """Save one filing's main HTML document. Skips if already downloaded."""
    FILINGS_DIR.mkdir(parents=True, exist_ok=True)
    accn = filing["accessionNumber"]
    path = FILINGS_DIR / f"{ticker}_{accn}.htm"
    if path.exists():
        return path
    url = DOCUMENT_URL.format(
        cik=cik, accn_nodash=accn.replace("-", ""), document=filing["primaryDocument"]
    )
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    path.write_bytes(response.content)  # bytes, exactly as received
    print(f"{ticker}: saved {path.name} (report date {filing['reportDate']}, {len(response.content) / 1e6:.1f} MB)")
    time.sleep(PAUSE_SECONDS)
    return path


def fetch_all(tickers: list[str] = TICKERS) -> None:
    headers = load_headers()
    ticker_map = load_ticker_map(headers)
    for ticker in tickers:
        cik = lookup_cik(ticker_map, ticker)
        reports = annual_reports(list_filings(ticker, cik, headers))
        print(f"{ticker}: {len(reports)} annual reports in scope")
        for filing in reports:
            download_filing(ticker, cik, filing, headers)


if __name__ == "__main__":
    fetch_all()
