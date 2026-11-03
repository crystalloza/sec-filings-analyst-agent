-- Layer: CLEAN
-- Built one cleaning problem at a time. Each step reads the previous step's table, so a
-- number that looks wrong can be traced back to the step that changed it. The final table
-- of this file will be clean_facts.

-- ---------------------------------------------------------------------------------------
-- Step 1: Which filings count as a source of figures?
-- Decision: periodic financial reports and their amendments only. Event notices (8-K) and
-- proxy statements (DEF 14A) are excluded. Profiling showed they add almost nothing new
-- (4 company-tag-periods across the 13 concepts, none conflicting), have no fiscal-year
-- labels (DEF 14A), and are filed after the 10-K, so a "latest filing wins" rule would
-- otherwise pick a proxy statement over the 10-K as the cited source.
-- The decision lives in this reference table so it is visible and easy to change.
-- ---------------------------------------------------------------------------------------
CREATE TABLE allowed_forms (
    form   VARCHAR,
    reason VARCHAR
);

INSERT INTO allowed_forms VALUES
    ('10-K',   'Annual report (audited)'),
    ('10-K/A', 'Amendment to an annual report'),
    ('10-Q',   'Quarterly report (reviewed, not audited)'),
    ('10-Q/A', 'Amendment to a quarterly report');

-- Still every tag and every taxonomy: only the form is filtered at this step.
CREATE TABLE facts_in_form_scope AS
SELECT r.*
FROM raw_facts AS r
INNER JOIN allowed_forms AS f
    ON r.form = f.form;

-- ---------------------------------------------------------------------------------------
-- Step 2: Restatements - one answer per period.
-- The same period is reported in several filings (comparatives, corrections). Rules:
--   * Period identity = company + taxonomy + tag + unit + start + end. (start is NULL for
--     instant facts; window PARTITION BY treats NULLs as equal, so they group correctly.)
--   * The LATEST filed copy wins. Ties on the filed date are broken by the higher accession
--     number (the filer's sequence number for that year).
--   * Superseded copies are not silently dropped. Where a superseded copy's VALUE differs
--     from the winner, it is recorded in restated_facts with the old value, new value, and
--     size of the change. Identical repeats (the common case) are not recorded.
-- Decided for an MVP: "as originally reported" views are out of scope (see README limits).
-- ---------------------------------------------------------------------------------------

-- Intermediate: every in-scope row numbered newest-first within its period.
CREATE TABLE facts_ranked AS
SELECT
    f.*,
    ROW_NUMBER() OVER (
        PARTITION BY f.taxonomy, f.tag, f.unit, f.cik, f."start", f."end"
        ORDER BY f.filed DESC, f.accn DESC
    ) AS version_rank
FROM facts_in_form_scope AS f;

-- The winners: exactly one row per period. Columns listed explicitly (not SELECT * EXCEPT)
-- to keep the SQL portable.
CREATE TABLE facts_latest AS
SELECT
    cik, entity_name, taxonomy, tag, label, description, unit,
    "start", "end", val, accn, fy, fp, form, filed, frame, source_file, fetched_at
FROM facts_ranked
WHERE version_rank = 1;

-- The audit trail: periods whose value changed between filings.
CREATE TABLE restated_facts AS
SELECT
    o.cik, o.entity_name, o.taxonomy, o.tag, o.unit, o."start", o."end",
    o.val   AS superseded_val,   o.accn AS superseded_accn,
    o.form  AS superseded_form,  o.filed AS superseded_filed,
    n.val   AS latest_val,       n.accn AS latest_accn,
    n.form  AS latest_form,      n.filed AS latest_filed,
    n.val - o.val AS change_abs,
    CASE WHEN o.val <> 0 THEN (n.val - o.val) / ABS(o.val) END AS change_pct
FROM facts_ranked AS o
INNER JOIN facts_ranked AS n
    ON  n.version_rank = 1
    AND n.taxonomy = o.taxonomy AND n.tag = o.tag AND n.unit = o.unit AND n.cik = o.cik
    AND n."start" IS NOT DISTINCT FROM o."start"
    AND n."end" = o."end"
WHERE o.version_rank > 1
  AND o.val <> n.val;

-- ---------------------------------------------------------------------------------------
-- Step 3: Duration vs instant facts.
-- In XBRL, whether a concept is an instant (a balance on one date, e.g. Assets) or a duration
-- (a flow over a span, e.g. Revenues) is fixed by the concept's definition. So a missing
-- "start" is not a judgment call: it means "instant". period_type records that explicitly so
-- later steps filter on a word instead of repeating a NULL check.
-- Safety net: a tag that appears as BOTH types would suggest a filing error. Such tags are
-- flagged in period_type_conflicts (flag and continue); profiling found 13, none among the
-- 13 concepts this project uses. Step 5 adds an expected type per mapped concept.
-- ---------------------------------------------------------------------------------------
CREATE TABLE facts_typed AS
SELECT
    f.*,
    CASE WHEN f."start" IS NULL THEN 'instant' ELSE 'duration' END AS period_type
FROM facts_latest AS f;

CREATE TABLE period_type_conflicts AS
SELECT
    taxonomy,
    tag,
    SUM(CASE WHEN period_type = 'instant'  THEN 1 ELSE 0 END) AS instant_rows,
    SUM(CASE WHEN period_type = 'duration' THEN 1 ELSE 0 END) AS duration_rows
FROM facts_typed
GROUP BY taxonomy, tag
HAVING SUM(CASE WHEN period_type = 'instant'  THEN 1 ELSE 0 END) > 0
   AND SUM(CASE WHEN period_type = 'duration' THEN 1 ELSE 0 END) > 0;
