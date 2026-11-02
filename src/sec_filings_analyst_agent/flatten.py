"""Flatten the saved SEC companyfacts JSON into a flat Parquet table (the raw layer).

The SEC JSON is nested: taxonomy -> tag -> unit -> [fact entries]. This module
turns that into ONE ROW PER FACT ENTRY, carrying the parent keys down onto each
row. It does not clean, filter, dedupe, or reinterpret anything: restatements,
duplicate periods, every taxonomy and every tag are all preserved. Cleaning is
the job of the next layer (sql/clean.sql).

Output: data/raw/raw_facts_{TICKER}.parquet  (one file per company, gitignored)

Run with:  uv run python -m sec_filings_analyst_agent.flatten
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq

from sec_filings_analyst_agent.fetch import RAW_DIR, TICKERS

# Explicit schema = the raw table's contract. Nullable where the SEC omits the field.
SCHEMA = pa.schema(
    [
        ("cik", pa.int64()),
        ("entity_name", pa.string()),
        ("taxonomy", pa.string()),  # e.g. us-gaap, dei
        ("tag", pa.string()),  # e.g. Revenues
        ("label", pa.string()),  # tag metadata, repeated on every row of the tag
        ("description", pa.string()),
        ("unit", pa.string()),  # e.g. USD, shares, USD/shares
        ("start", pa.date32()),  # NULL for instant (balance-sheet-style) facts
        ("end", pa.date32()),
        ("val", pa.float64()),
        ("accn", pa.string()),  # accession number: which filing reported this fact
        ("fy", pa.int32()),  # NOTE: describes the FILING's fiscal period, not the fact's
        ("fp", pa.string()),
        ("form", pa.string()),
        ("filed", pa.date32()),
        ("frame", pa.string()),  # SEC's canonical calendar label; often NULL
        ("source_file", pa.string()),  # provenance: which saved JSON this came from
        ("fetched_at", pa.date32()),  # provenance: date the JSON was downloaded
    ]
)

# float64 represents integers exactly up to 2**53 (~9e15). Reported dollar values
# are far below that, but we fail loudly rather than silently lose precision.
MAX_EXACT_INT = 2**53


def latest_json_for(ticker: str) -> Path:
    """Most recent saved JSON for a ticker (filenames embed an ISO date, so sorting works)."""
    files = sorted(RAW_DIR.glob(f"{ticker}_*.json"))
    if not files:
        raise FileNotFoundError(f"No saved JSON for {ticker} in {RAW_DIR}. Run fetch first.")
    return files[-1]


def fetched_at_from_name(path: Path) -> date:
    match = re.search(r"_(\d{4}-\d{2}-\d{2})\.json$", path.name)
    if not match:
        raise ValueError(f"Cannot read a fetch date from filename {path.name!r}")
    return date.fromisoformat(match.group(1))


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _as_float(value: int | float) -> float:
    if isinstance(value, int) and abs(value) > MAX_EXACT_INT:
        raise ValueError(f"Value {value} is too large to store exactly as float64")
    return float(value)


def iter_fact_rows(doc: dict, source_file: str, fetched_at: date) -> Iterator[dict]:
    """Walk taxonomy -> tag -> unit -> entries and yield one flat row per entry."""
    for taxonomy, tags in doc["facts"].items():
        for tag, body in tags.items():
            for unit, entries in body["units"].items():
                for e in entries:
                    yield {
                        "cik": doc["cik"],
                        "entity_name": doc["entityName"],
                        "taxonomy": taxonomy,
                        "tag": tag,
                        "label": body.get("label"),
                        "description": body.get("description"),
                        "unit": unit,
                        "start": _as_date(e.get("start")),
                        "end": _as_date(e["end"]),
                        "val": _as_float(e["val"]),
                        "accn": e["accn"],
                        "fy": e["fy"],
                        "fp": e["fp"],
                        "form": e["form"],
                        "filed": _as_date(e["filed"]),
                        "frame": e.get("frame"),
                        "source_file": source_file,
                        "fetched_at": fetched_at,
                    }


def count_entries(doc: dict) -> int:
    """Independent count of fact entries in the JSON, used to verify nothing was dropped."""
    return sum(
        len(entries)
        for tags in doc["facts"].values()
        for body in tags.values()
        for entries in body["units"].values()
    )


def flatten_company(ticker: str) -> Path:
    path = latest_json_for(ticker)
    doc = json.loads(path.read_text())

    rows = list(iter_fact_rows(doc, path.name, fetched_at_from_name(path)))
    table = pa.Table.from_pylist(rows, schema=SCHEMA)

    out = RAW_DIR / f"raw_facts_{ticker}.parquet"
    pq.write_table(table, out)  # overwrite: this file is fully derived from the JSON

    # Conservation check: every JSON entry must be a row in the file we just wrote.
    expected = count_entries(doc)
    written = pq.read_metadata(out).num_rows
    if written != expected:
        raise RuntimeError(f"{ticker}: JSON has {expected} entries but Parquet has {written} rows")

    print(f"{ticker}: {written:,} rows -> {out.name}")
    return out


def flatten_all(tickers: list[str] = TICKERS) -> None:
    for ticker in tickers:
        flatten_company(ticker)


if __name__ == "__main__":
    flatten_all()
