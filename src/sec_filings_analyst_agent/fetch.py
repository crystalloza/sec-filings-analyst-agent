"""Fetch SEC EDGAR data and save it, untouched, to data/raw/.

This is the "raw" layer's first step: we store exactly what the SEC returned
so the pipeline can be re-run without the network and we can always show
what the source said on the day we pulled it.

Run with:  uv run python -m sec_filings_analyst_agent.fetch
"""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

TICKERS = ["NVDA", "AMD", "INTC", "MSFT"]

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# SEC allows 10 requests/second. 0.15s between calls keeps us comfortably under.
PAUSE_SECONDS = 0.15
REQUEST_TIMEOUT_SECONDS = 30

# src/sec_filings_analyst_agent/fetch.py -> parents[2] is the repo root.
RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

PLACEHOLDER_EMAIL = "your.email@example.com"


def load_headers() -> dict[str, str]:
    """Build the User-Agent header the SEC requires, from the .env file.

    Fails loudly if the email is missing or still the placeholder. The email
    is never printed or logged.
    """
    load_dotenv()
    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if not email or email == PLACEHOLDER_EMAIL:
        raise RuntimeError(
            "SEC_CONTACT_EMAIL is not set. Put your real contact email in .env "
            "(see .env.example). The SEC rejects requests without a descriptive User-Agent."
        )
    return {"User-Agent": f"sec-filings-analyst-agent {email}"}


def load_ticker_map(headers: dict[str, str]) -> dict:
    """Return the SEC ticker file, saving it to data/raw/ the first time only."""
    path = RAW_DIR / "company_tickers.json"
    if path.exists():
        return json.loads(path.read_text())

    response = requests.get(TICKERS_URL, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)  # bytes, exactly as received
    time.sleep(PAUSE_SECONDS)
    return response.json()


def lookup_cik(ticker_map: dict, ticker: str) -> int:
    """Find a ticker's CIK. The file is a dict keyed "0", "1", ... so we scan its values."""
    for entry in ticker_map.values():
        if entry["ticker"] == ticker:
            return int(entry["cik_str"])
    raise KeyError(f"Ticker {ticker!r} not found in the SEC company_tickers.json file")


def fetch_company_facts(ticker: str, cik: int, headers: dict[str, str]) -> Path | None:
    """Download one company's full XBRL facts. Skips if any file for the ticker exists.

    Returns the path written, or None if skipped.
    """
    existing = sorted(RAW_DIR.glob(f"{ticker}_*.json"))
    if existing:
        print(f"{ticker}: already have {existing[0].name}, skipping")
        return None

    response = requests.get(
        COMPANY_FACTS_URL.format(cik=cik),
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()  # fail loudly on 403/404/5xx

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"{ticker}_{date.today().isoformat()}.json"
    path.write_bytes(response.content)  # bytes, exactly as received
    print(f"{ticker}: saved {path.name} ({len(response.content) / 1e6:.1f} MB)")
    time.sleep(PAUSE_SECONDS)
    return path


def fetch_all(tickers: list[str] = TICKERS) -> None:
    headers = load_headers()
    ticker_map = load_ticker_map(headers)
    for ticker in tickers:
        cik = lookup_cik(ticker_map, ticker)
        fetch_company_facts(ticker, cik, headers)


if __name__ == "__main__":
    fetch_all()
