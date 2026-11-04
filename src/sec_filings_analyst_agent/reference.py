"""Ground truth for the checks, read from the warehouse.

IMPORTANT: ratios and growth are RECOMPUTED here in plain Python from the underlying clean_facts
values, not read from the SQL `metrics` table. The checks and the SQL are two independent
implementations of the same definitions: a bug in one cannot silently pass the other. A test
(tests/test_reference.py) asserts they agree on the real warehouse.
"""

from __future__ import annotations

import duckdb

REPORTED = (
    "revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income",
    "research_and_development", "eps_diluted", "total_assets", "total_liabilities",
    "stockholders_equity", "cash_and_equivalents", "inventory",
)
RATIO_FORMULAS = {  # metric -> (numerator concept, denominator concept)
    "gross_margin": ("gross_profit", "revenue"),
    "operating_margin": ("operating_income", "revenue"),
    "net_margin": ("net_income", "revenue"),
    "rnd_pct_revenue": ("research_and_development", "revenue"),
}
PERCENT_CONCEPTS = set(RATIO_FORMULAS) | {"revenue_growth"}
MAPPED = set(REPORTED) | PERCENT_CONCEPTS


def unit_kind(concept: str) -> str | None:
    """The unit a figure of this concept must have: percent, USD_per_share or USD."""
    if concept in PERCENT_CONCEPTS:
        return "percent"
    if concept == "eps_diluted":
        return "USD_per_share"
    if concept in REPORTED:
        return "USD"
    return None


def _fact(con: duckdb.DuckDBPyConnection, company: str, concept: str, fy: int, fp: str,
          roles: tuple[str, ...] = ("reported",)) -> float | None:
    marks = ",".join("?" for _ in roles)
    row = con.execute(
        f"SELECT val FROM clean_facts WHERE ticker = ? AND concept = ? AND fiscal_year = ? "
        f"AND fiscal_period = ? AND role IN ({marks}) ORDER BY is_derived, period_end DESC LIMIT 1",
        [company, concept, fy, fp, *roles],
    ).fetchone()
    return None if row is None else row[0]


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den <= 0:
        return None
    return num / den


def derived_total_liabilities(con, company: str, fy: int, fp: str) -> float | None:
    """Total liabilities = liabilities and equity - equity INCLUDING noncontrolling interest.

    Where the company reports no noncontrolling-interest equity at all (AMD), parent equity is used.
    Where the company has it in some periods but not this one (Intel before 2021), it is NOT derivable.
    """
    both = ("reported", "component")
    lse = _fact(con, company, "liabilities_and_equity", fy, fp, both)
    if lse is None:
        return None
    equity_total = _fact(con, company, "stockholders_equity_incl_nci", fy, fp, both)
    if equity_total is not None:
        return lse - equity_total
    has_nci_anywhere = con.execute(
        "SELECT count(*) FROM clean_facts WHERE ticker = ? AND concept = 'stockholders_equity_incl_nci'",
        [company],
    ).fetchone()[0]
    if has_nci_anywhere:
        return None
    parent = _fact(con, company, "stockholders_equity", fy, fp)
    return None if parent is None else lse - parent


def reference_value(con, company: str, concept: str, fy: int, fp: str) -> tuple[float | None, dict]:
    """Canonical reference (USD, per-share dollars, or a fraction) for a figure's claim, plus notes."""
    info = {"derived": False}
    if concept in RATIO_FORMULAS:
        num, den = RATIO_FORMULAS[concept]
        return _ratio(_fact(con, company, num, fy, fp), _fact(con, company, den, fy, fp)), info
    if concept == "revenue_growth":
        now, prev = _fact(con, company, "revenue", fy, fp), _fact(con, company, "revenue", fy - 1, fp)
        if now is None or prev is None or prev <= 0:
            return None, info
        return (now - prev) / prev, info
    if concept in REPORTED:
        value = _fact(con, company, concept, fy, fp)
        if value is None and concept == "total_liabilities":
            value = derived_total_liabilities(con, company, fy, fp)
            info["derived"] = value is not None
        return value, info
    return None, info


def values_at_period(con, company: str, fy: int, fp: str) -> dict[str, float]:
    """Every concept's reference at one period (used to spot a figure labelled as the wrong concept)."""
    out = {}
    for concept in sorted(MAPPED):
        value, _ = reference_value(con, company, concept, fy, fp)
        if value is not None:
            out[concept] = value
    return out


def all_period_values(con, company: str, concept: str) -> dict[tuple[int, str], float]:
    """A concept's reference at every period (used to spot a real value attributed to the wrong period)."""
    needed = {concept}
    if concept in RATIO_FORMULAS:
        needed = set(RATIO_FORMULAS[concept])
    elif concept == "revenue_growth":
        needed = {"revenue"}
    if not needed <= set(REPORTED):
        return {}
    marks = ",".join("?" for _ in needed)
    rows = con.execute(
        f"SELECT fiscal_year, fiscal_period, concept, val FROM clean_facts WHERE ticker = ? "
        f"AND role = 'reported' AND concept IN ({marks}) ORDER BY is_derived DESC, period_end",
        [company, *sorted(needed)],
    ).fetchall()
    by_period: dict[tuple[int, str], dict[str, float]] = {}
    for fy, fp, c, val in rows:
        by_period.setdefault((fy, fp), {})[c] = val  # non-derived rows are written last and win
    out = {}
    for (fy, fp), vals in by_period.items():
        if concept in RATIO_FORMULAS:
            num, den = RATIO_FORMULAS[concept]
            v = _ratio(vals.get(num), vals.get(den))
        elif concept == "revenue_growth":
            prev = by_period.get((fy - 1, fp), {}).get("revenue")
            now = vals.get("revenue")
            v = None if now is None or prev is None or prev <= 0 else (now - prev) / prev
        else:
            v = vals.get(concept)
        if v is not None:
            out[(fy, fp)] = v
    return out
