-- Layer: RAW
-- Load the flattened Parquet files (one per company, written by flatten.py) into a single
-- table. Nothing is cleaned, filtered, or deduplicated here: restatements, duplicate periods,
-- every taxonomy and every tag are kept exactly as the SEC reported them.
--
-- Column notes (kept as the SEC named them):
--   * "start" / "end" are the fact's period. "start" is NULL for instant facts.
--     "end" is a reserved word in SQL, so queries must quote it as "end".
--   * fy / fp describe the FILING's fiscal period, not the period the fact covers.
--   * source_file / fetched_at are provenance for the raw JSON each row came from.
--
-- Portability note: read_parquet() is DuckDB syntax. On Databricks this one statement is the
-- only thing that changes (read the same Parquet files into a table).
CREATE TABLE raw_facts AS
SELECT *
FROM read_parquet('data/raw/raw_facts_*.parquet');
