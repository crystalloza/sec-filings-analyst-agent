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
