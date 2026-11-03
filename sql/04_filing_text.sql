-- Layer: FILING TEXT
-- Paragraph-level text of the 10-K sections the agent can read (Items 1, 1A and 7), parsed by
-- parse_filings.py from the saved HTML. One row per paragraph (or table, is_table = TRUE), with a
-- stable paragraph_id, the nearest heading above it, and the filing it came from. Text was
-- normalized with the same normalize_text() the quote check will use.
--
-- filing_manifest says how much text each filing/item produced and by which method
-- (item_heading, or the generic title_fallback for filings without "Item N" headings).
-- A row with status 'missing' means no text for that item (for example Intel's Item 1),
-- which the agent must treat as "not available", never as "empty".

-- Fiscal year of a filing = fiscal year of its report date, using the same calendar and
-- 7-day-shift rule as clean_facts.
CREATE TABLE filing_manifest AS
SELECT
    m.accn, m.ticker, m.source_file, m.report_date,
    CASE WHEN MONTH(CAST(m.report_date AS DATE) - INTERVAL '7' DAY) <= c.fiscal_year_end_month
         THEN YEAR(CAST(m.report_date AS DATE) - INTERVAL '7' DAY)
         ELSE YEAR(CAST(m.report_date AS DATE) - INTERVAL '7' DAY) + 1 END AS fiscal_year,
    m.item, m.item_title, m.paragraphs, m.tables, m.words, m.method, m.status
FROM read_parquet('data/raw/filing_manifest.parquet') AS m
LEFT JOIN fiscal_calendar AS c
    ON c.ticker = m.ticker;

CREATE TABLE filing_paragraphs AS
SELECT
    f.ticker, f.fiscal_year, p.accn, p.paragraph_id, p.item, p.paragraph_index,
    p.heading, p.is_table, p.text, p.word_count
FROM read_parquet('data/raw/filing_paragraphs.parquet') AS p
INNER JOIN (SELECT DISTINCT accn, ticker, fiscal_year FROM filing_manifest) AS f
    ON f.accn = p.accn;
