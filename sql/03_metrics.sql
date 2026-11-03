-- Layer: METRICS
-- Derived ratios and growth rates, one row per company + period + metric ("long" format, so
-- an agent tool can ask for any metric of any period, and new metrics need no new columns).
--
-- Decisions encoded here:
--   * Periods: annual and quarterly only (no year-to-date). Balance-sheet items use their
--     Q1/Q2/Q3/year-end balances; a year-end balance counts as the Q4 balance.
--   * Ratios: gross margin = reported gross profit / revenue; operating margin; net margin;
--     R&D as a share of revenue. Growth starts at FY2019 (FY2018 is the base year).
--   * Growth: year-over-year for annual and quarterly periods (same period one year earlier),
--     plus quarter-over-quarter (sequential) for quarterly periods and balance-sheet items.
--   * If any input is missing or flagged, the value is NULL with a null_reason. It is never
--     computed from partial data. A prior value that is zero or negative gives NULL
--     ('prior_period_not_positive'), the finance "not meaningful" convention.
--   * Derived Q4 inputs are allowed, but uses_derived_input marks every metric that touches one.
--   * No percentile-vs-own-history column (dropped for the MVP).
-- Ratios are stored as fractions (0.75 = 75%).

-- The period cells metrics are built from: in-window reported facts, annual + quarterly
-- durations and all balance-sheet instants. seq_q puts quarters in order (1-4); the year-end
-- balance is the 4th quarter's balance. Annual durations have no quarter order (NULL).
CREATE TABLE metric_base AS
SELECT
    c.cik, c.ticker, c.concept, c.period_type, c.duration_class,
    c.fiscal_year, c.fiscal_period, c.period_end, c.val,
    c.exclude_from_questions AS flagged,
    c.is_derived,
    CASE
        WHEN c.period_type = 'instant' AND c.fiscal_period = 'FY' THEN 4
        WHEN c.period_type = 'instant'                            THEN CAST(SUBSTR(c.fiscal_period, 2, 1) AS INTEGER)
        WHEN c.duration_class = 'quarterly'                       THEN CAST(SUBSTR(c.fiscal_period, 2, 1) AS INTEGER)
    END AS seq_q
FROM clean_facts AS c
WHERE c.in_window
  AND c.role = 'reported'
  AND (c.period_type = 'instant' OR c.duration_class IN ('annual', 'quarterly'));

-- ---------------------------------------------------------------------------------------
-- Definitions as data, so the README can show them and the checks can re-implement them.
-- ---------------------------------------------------------------------------------------
CREATE TABLE ratio_definitions (
    metric      VARCHAR,
    numerator   VARCHAR,
    denominator VARCHAR
);

INSERT INTO ratio_definitions VALUES
    ('gross_margin',     'gross_profit',             'revenue'),
    ('operating_margin', 'operating_income',         'revenue'),
    ('net_margin',       'net_income',               'revenue'),
    ('rnd_pct_revenue',  'research_and_development', 'revenue');

CREATE TABLE growth_concepts (
    concept VARCHAR
);

INSERT INTO growth_concepts VALUES
    ('revenue'), ('cost_of_revenue'), ('gross_profit'), ('operating_income'),
    ('net_income'), ('research_and_development'), ('eps_diluted'),
    ('total_assets'), ('total_liabilities'), ('stockholders_equity'),
    ('cash_and_equivalents'), ('inventory');

-- ---------------------------------------------------------------------------------------
-- Ratios: anchored on revenue, so a ratio row exists for every period that has revenue.
-- ---------------------------------------------------------------------------------------
CREATE TABLE metrics_ratios AS
SELECT
    d.metric,
    r.cik, r.ticker, r.fiscal_year, r.fiscal_period, r.duration_class, r.period_end,
    CASE
        WHEN n.val IS NULL OR n.flagged OR r.flagged OR r.val <= 0 THEN NULL
        ELSE n.val / r.val
    END AS value,
    CASE
        WHEN n.val IS NULL         THEN 'numerator_missing'
        WHEN n.flagged OR r.flagged THEN 'input_flagged'
        WHEN r.val <= 0            THEN 'revenue_not_positive'
    END AS null_reason,
    (COALESCE(n.is_derived, FALSE) OR r.is_derived) AS uses_derived_input,
    d.numerator || ' / ' || d.denominator AS formula
FROM metric_base AS r
CROSS JOIN ratio_definitions AS d
LEFT JOIN metric_base AS n
    ON  n.ticker = r.ticker AND n.concept = d.numerator
    AND n.fiscal_year = r.fiscal_year AND n.fiscal_period = r.fiscal_period
    AND n.duration_class = r.duration_class
WHERE r.concept = 'revenue';

-- ---------------------------------------------------------------------------------------
-- Year-over-year growth: the same fiscal period one year earlier (annual vs prior annual,
-- Q2 vs prior Q2, a Q2 balance vs the prior Q2 balance). FY2019 onward.
-- ---------------------------------------------------------------------------------------
CREATE TABLE metrics_yoy AS
SELECT
    g.concept || '_yoy_growth' AS metric,
    c.cik, c.ticker, c.fiscal_year, c.fiscal_period, c.duration_class, c.period_end,
    CASE
        WHEN p.val IS NULL OR c.flagged OR p.flagged OR p.val <= 0 THEN NULL
        ELSE (c.val - p.val) / p.val
    END AS value,
    CASE
        WHEN p.val IS NULL           THEN 'prior_period_missing'
        WHEN c.flagged OR p.flagged  THEN 'input_flagged'
        WHEN p.val <= 0              THEN 'prior_period_not_positive'
    END AS null_reason,
    (c.is_derived OR COALESCE(p.is_derived, FALSE)) AS uses_derived_input,
    '(this period - same period prior year) / same period prior year' AS formula
FROM metric_base AS c
INNER JOIN growth_concepts AS g
    ON g.concept = c.concept
LEFT JOIN metric_base AS p
    ON  p.ticker = c.ticker AND p.concept = c.concept
    AND p.fiscal_period = c.fiscal_period
    AND p.fiscal_year = c.fiscal_year - 1
    AND p.duration_class IS NOT DISTINCT FROM c.duration_class
WHERE c.fiscal_year >= 2019;

-- ---------------------------------------------------------------------------------------
-- Quarter-over-quarter (sequential) growth: quarterly durations and balance-sheet items vs the
-- immediately preceding quarter (Q1 compares with the prior year's Q4). Annual periods have
-- no sequential comparison. Sequential growth includes seasonality (README note).
-- ---------------------------------------------------------------------------------------
CREATE TABLE metrics_qoq AS
SELECT
    g.concept || '_qoq_growth' AS metric,
    c.cik, c.ticker, c.fiscal_year, c.fiscal_period, c.duration_class, c.period_end,
    CASE
        WHEN p.val IS NULL OR c.flagged OR p.flagged OR p.val <= 0 THEN NULL
        ELSE (c.val - p.val) / p.val
    END AS value,
    CASE
        WHEN p.val IS NULL           THEN 'prior_period_missing'
        WHEN c.flagged OR p.flagged  THEN 'input_flagged'
        WHEN p.val <= 0              THEN 'prior_period_not_positive'
    END AS null_reason,
    (c.is_derived OR COALESCE(p.is_derived, FALSE)) AS uses_derived_input,
    '(this quarter - previous quarter) / previous quarter' AS formula
FROM metric_base AS c
INNER JOIN growth_concepts AS g
    ON g.concept = c.concept
LEFT JOIN metric_base AS p
    ON  p.ticker = c.ticker AND p.concept = c.concept
    AND p.duration_class IS NOT DISTINCT FROM c.duration_class
    AND p.seq_q = CASE WHEN c.seq_q = 1 THEN 4 ELSE c.seq_q - 1 END
    AND p.fiscal_year = CASE WHEN c.seq_q = 1 THEN c.fiscal_year - 1 ELSE c.fiscal_year END
WHERE c.fiscal_year >= 2019
  AND c.seq_q IS NOT NULL;

-- ---------------------------------------------------------------------------------------
-- The final table: one row per company + period + metric.
-- ---------------------------------------------------------------------------------------
CREATE TABLE metrics AS
SELECT metric, cik, ticker, fiscal_year, fiscal_period, duration_class, period_end,
       value, null_reason, uses_derived_input, formula
FROM metrics_ratios
UNION ALL
SELECT metric, cik, ticker, fiscal_year, fiscal_period, duration_class, period_end,
       value, null_reason, uses_derived_input, formula
FROM metrics_yoy
UNION ALL
SELECT metric, cik, ticker, fiscal_year, fiscal_period, duration_class, period_end,
       value, null_reason, uses_derived_input, formula
FROM metrics_qoq;
