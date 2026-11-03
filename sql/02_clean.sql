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

-- ---------------------------------------------------------------------------------------
-- Step 4: Classify each duration as quarterly, 6-month YTD, 9-month YTD, or annual.
-- A fiscal year produces overlapping spans (the year, three quarters, and 6- and 9-month
-- year-to-date totals). Mixing them in one aggregation would double- or triple-count, so every
-- duration gets a label. The label comes from the LENGTH of the span only: profiling showed
-- the lengths fall into four tight clusters (89-97, 181-188, 272-279, 363-370 days), and
-- fy/fp are not used because they describe the filing, not the fact's period.
-- Bands are deliberately wider than the observed clusters. Annual is NOT 365 days: companies
-- on 52/53-week years show 363 and 370 (and the difference is one less than the span, since
-- both the first and last day count).
-- Anything outside the bands is labelled 'other' and listed in unclassified_durations
-- (flag and continue), never dropped or guessed.
-- Instant facts get NULL here; where an instant falls in the fiscal calendar is step 6.
-- Q4 is not reported on its own (it sits inside the 10-K's full year) and is deliberately
-- NOT derived in this MVP: a derived figure must not be mistaken for a reported one.
-- Portability note: date_diff('day', ...) is DuckDB syntax (Databricks: datediff).
-- ---------------------------------------------------------------------------------------
CREATE TABLE duration_classes (
    duration_class VARCHAR,
    min_days       INTEGER,
    max_days       INTEGER,
    note           VARCHAR
);

INSERT INTO duration_classes VALUES
    ('quarterly', 80,  100, 'One fiscal quarter (about 3 months)'),
    ('ytd_6m',    170, 200, 'Six months year-to-date (Q1 + Q2)'),
    ('ytd_9m',    255, 290, 'Nine months year-to-date (Q1 + Q2 + Q3)'),
    ('annual',    350, 380, 'Full fiscal year, including 52/53-week years');

CREATE TABLE facts_classified AS
WITH measured AS (
    SELECT
        t.*,
        CASE WHEN t.period_type = 'duration'
             THEN date_diff('day', t."start", t."end") END AS duration_days
    FROM facts_typed AS t
)
SELECT
    m.*,
    CASE WHEN m.period_type = 'duration'
         THEN COALESCE(c.duration_class, 'other') END AS duration_class
FROM measured AS m
LEFT JOIN duration_classes AS c
    ON m.duration_days BETWEEN c.min_days AND c.max_days;

CREATE TABLE unclassified_durations AS
SELECT
    cik, entity_name, taxonomy, tag, unit, "start", "end", duration_days, val, accn, form
FROM facts_classified
WHERE duration_class = 'other';

-- ---------------------------------------------------------------------------------------
-- Step 5: Concept mapping -> clean_facts.
-- Until now rows were keyed by raw SEC tags. Here the project's own CONCEPTS replace them.
-- concept_map is the explicit allowlist: which tag(s) feed each concept, in what priority
-- order, and what period type (instant/duration) the concept must have. It is data, not
-- buried code, so the README can show it. Tags deliberately NOT mapped (different meaning):
--   SalesRevenueGoodsNet (goods only, not total revenue), CostsAndExpenses (total costs),
--   CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization (excludes D&A),
--   ProfitLoss (includes noncontrolling interest; net_income is the parent-company figure).
-- 'component' concepts are not user-facing metrics: they exist so a figure the company does
-- not report (e.g. total liabilities for AMD and INTC) can be DERIVED ON REQUEST and labelled
-- as derived: liabilities = liabilities_and_equity - stockholders_equity_incl_nci.
-- Per period, the highest-priority tag with a value wins. Where a lower-priority tag also has a
-- value and DISAGREES, the period is written to tag_disagreements (flag and continue).
-- Periods that are restated or have a tag disagreement are marked exclude_from_questions.
-- ---------------------------------------------------------------------------------------
CREATE TABLE concept_map (
    concept              VARCHAR,
    role                 VARCHAR,   -- 'reported' or 'component'
    taxonomy             VARCHAR,
    tag                  VARCHAR,
    unit                 VARCHAR,
    priority             INTEGER,   -- 1 = preferred
    expected_period_type VARCHAR,   -- 'instant' or 'duration'
    note                 VARCHAR
);

INSERT INTO concept_map VALUES
    ('revenue', 'reported', 'us-gaap', 'RevenueFromContractWithCustomerExcludingAssessedTax', 'USD', 1, 'duration', 'Standard tag since the new revenue standard'),
    ('revenue', 'reported', 'us-gaap', 'Revenues',                                            'USD', 2, 'duration', 'Used by NVIDIA; broader definition in principle, agrees in practice'),
    ('revenue', 'reported', 'us-gaap', 'SalesRevenueNet',                                     'USD', 3, 'duration', 'Deprecated 2018-01-31; legacy fallback'),
    ('cost_of_revenue', 'reported', 'us-gaap', 'CostOfRevenue',                               'USD', 1, 'duration', 'Used by NVIDIA'),
    ('cost_of_revenue', 'reported', 'us-gaap', 'CostOfGoodsAndServicesSold',                  'USD', 2, 'duration', 'Used by AMD, Intel, Microsoft'),
    ('gross_profit', 'reported', 'us-gaap', 'GrossProfit',                                    'USD', 1, 'duration', NULL),
    ('operating_income', 'reported', 'us-gaap', 'OperatingIncomeLoss',                        'USD', 1, 'duration', NULL),
    ('net_income', 'reported', 'us-gaap', 'NetIncomeLoss',                                    'USD', 1, 'duration', 'Attributable to parent (excludes noncontrolling interest)'),
    ('research_and_development', 'reported', 'us-gaap', 'ResearchAndDevelopmentExpense',     'USD', 1, 'duration', NULL),
    ('eps_diluted', 'reported', 'us-gaap', 'EarningsPerShareDiluted',                         'USD/shares', 1, 'duration', 'Latest filing wins, so values are split-adjusted, not as originally reported'),
    ('total_assets', 'reported', 'us-gaap', 'Assets',                                         'USD', 1, 'instant', NULL),
    ('total_liabilities', 'reported', 'us-gaap', 'Liabilities',                               'USD', 1, 'instant', 'Not reported by AMD or Intel: unavailable, derivable on request from components'),
    ('stockholders_equity', 'reported', 'us-gaap', 'StockholdersEquity',                      'USD', 1, 'instant', 'Parent-company equity'),
    ('cash_and_equivalents', 'reported', 'us-gaap', 'CashAndCashEquivalentsAtCarryingValue',  'USD', 1, 'instant', NULL),
    ('inventory', 'reported', 'us-gaap', 'InventoryNet',                                      'USD', 1, 'instant', NULL),
    ('liabilities_and_equity', 'component', 'us-gaap', 'LiabilitiesAndStockholdersEquity',    'USD', 1, 'instant', 'Component for deriving total_liabilities'),
    ('stockholders_equity_incl_nci', 'component', 'us-gaap', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest', 'USD', 1, 'instant', 'Component for deriving total_liabilities; needed for Intel (noncontrolling interest)');

-- Every fact whose tag is on the allowlist, with its concept and priority.
CREATE TABLE facts_mapped AS
SELECT
    m.concept, m.role, m.priority, m.expected_period_type,
    f.cik, f.entity_name, f.taxonomy, f.tag, f.unit,
    f."start", f."end", f.val, f.accn, f.fy, f.fp, f.form, f.filed, f.frame,
    f.period_type, f.duration_days, f.duration_class
FROM facts_classified AS f
INNER JOIN concept_map AS m
    ON  f.taxonomy = m.taxonomy
    AND f.tag      = m.tag
    AND f.unit     = m.unit;

-- Safety net from step 3: a mapped row whose period type contradicts the concept is flagged
-- and kept out of the candidates (never silently used).
CREATE TABLE concept_type_violations AS
SELECT * FROM facts_mapped WHERE period_type <> expected_period_type;

-- Candidates numbered by priority within each company + concept + period.
CREATE TABLE facts_candidates AS
SELECT
    c.*,
    ROW_NUMBER() OVER (
        PARTITION BY c.cik, c.concept, c."start", c."end"
        ORDER BY c.priority
    ) AS tag_rank
FROM facts_mapped AS c
WHERE c.period_type = c.expected_period_type;

-- The agreement check: where the winning tag and a lower-priority tag both have a value for
-- the same period and the values differ. comparison_basis says whether both values come from the
-- same filing (a real definitional difference) or different filings (restatement candidate).
CREATE TABLE tag_disagreements AS
SELECT
    w.cik, w.entity_name, w.concept, w."start", w."end",
    w.tag AS winning_tag, w.val AS winning_val, w.accn AS winning_accn,
    o.tag AS other_tag,   o.val AS other_val,   o.accn AS other_accn,
    w.val - o.val AS diff_abs,
    CASE WHEN o.val <> 0 THEN (w.val - o.val) / ABS(o.val) END AS diff_pct,
    CASE WHEN w.accn = o.accn THEN 'same_filing' ELSE 'cross_filing' END AS comparison_basis
FROM facts_candidates AS w
INNER JOIN facts_candidates AS o
    ON  o.cik = w.cik AND o.concept = w.concept AND o."end" = w."end"
    AND o."start" IS NOT DISTINCT FROM w."start"
WHERE w.tag_rank = 1 AND o.tag_rank > 1 AND w.val <> o.val;

-- clean_facts: one row per company + concept + period, with quality flags.
CREATE TABLE clean_facts AS
WITH winners AS (
    SELECT
        w.cik, w.entity_name, w.concept, w.role, w.tag AS source_tag, w.taxonomy, w.unit,
        w.period_type, w.duration_class, w.duration_days, w."start", w."end", w.val,
        w.accn, w.form, w.filed, w.fy, w.fp, w.frame,
        (d.flag IS NOT NULL) AS has_tag_disagreement,
        (r.flag IS NOT NULL) AS was_restated
    FROM facts_candidates AS w
    LEFT JOIN (
        SELECT DISTINCT cik, concept, "start", "end", 1 AS flag FROM tag_disagreements
    ) AS d
        ON d.cik = w.cik AND d.concept = w.concept AND d."end" = w."end"
        AND d."start" IS NOT DISTINCT FROM w."start"
    LEFT JOIN (
        SELECT DISTINCT cik, taxonomy, tag, unit, "start", "end", 1 AS flag FROM restated_facts
    ) AS r
        ON r.cik = w.cik AND r.taxonomy = w.taxonomy AND r.tag = w.tag AND r.unit = w.unit
        AND r."end" = w."end" AND r."start" IS NOT DISTINCT FROM w."start"
    WHERE w.tag_rank = 1
)
SELECT
    winners.*,
    (has_tag_disagreement OR was_restated) AS exclude_from_questions
FROM winners;
