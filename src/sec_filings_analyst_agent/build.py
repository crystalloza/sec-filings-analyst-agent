"""Rebuild the DuckDB warehouse from scratch by running sql/*.sql in order.

The database is disposable: everything in it is derived from the Parquet files and the SQL.
Change the SQL, rerun this, get a fresh database. Never edit the database by hand.

Safety: the build happens in a temporary file that replaces data/warehouse.duckdb only if
every step succeeds, so a failed build never destroys the last good database.

Run with:  uv run python -m sec_filings_analyst_agent.build
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = ROOT / "sql"
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "warehouse.duckdb"


def sql_files() -> list[Path]:
    """SQL steps run in filename order, so 01_raw.sql runs before 02_clean.sql."""
    files = sorted(SQL_DIR.glob("[0-9][0-9]_*.sql"))
    if not files:
        raise FileNotFoundError(f"No numbered SQL files (e.g. 01_raw.sql) found in {SQL_DIR}")
    return files


def check_raw_row_count(con: duckdb.DuckDBPyConnection) -> None:
    """Reconciliation: raw_facts must have exactly as many rows as the Parquet files hold."""
    expected = sum(pq.read_metadata(p).num_rows for p in RAW_DIR.glob("raw_facts_*.parquet"))
    actual = con.execute("SELECT count(*) FROM raw_facts").fetchone()[0]
    if actual != expected:
        raise RuntimeError(f"raw_facts has {actual} rows but the Parquet files hold {expected}")
    print(f"check: raw_facts row count matches Parquet ({actual:,})")


def check_filing_row_count(con: duckdb.DuckDBPyConnection) -> None:
    """Reconciliation: every parsed paragraph must reach filing_paragraphs (none lost in the join)."""
    expected = pq.read_metadata(RAW_DIR / "filing_paragraphs.parquet").num_rows
    actual = con.execute("SELECT count(*) FROM filing_paragraphs").fetchone()[0]
    if actual != expected:
        raise RuntimeError(f"filing_paragraphs has {actual} rows but the Parquet file holds {expected}")
    print(f"check: filing_paragraphs row count matches Parquet ({actual:,})")


def build() -> Path:
    tmp_path = DB_PATH.with_name(DB_PATH.name + ".tmp")
    tmp_path.unlink(missing_ok=True)

    con = duckdb.connect(str(tmp_path))
    try:
        # SQL files use paths relative to the repo root (e.g. data/raw/...).
        con.execute(f"SET file_search_path = '{ROOT}'")
        for path in sql_files():
            con.execute(path.read_text())
            print(f"ran {path.name}")
        check_raw_row_count(con)
        check_filing_row_count(con)
    except BaseException:
        con.close()
        tmp_path.unlink(missing_ok=True)
        raise
    con.close()

    os.replace(tmp_path, DB_PATH)  # swap in only after everything succeeded
    print(f"built {DB_PATH.relative_to(ROOT)}")
    return DB_PATH


if __name__ == "__main__":
    build()
