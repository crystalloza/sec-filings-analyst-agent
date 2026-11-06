"""The `sec-facts` tools: parameterized reads of the warehouse. No free SQL, ever.

Every call returns plain dicts (JSON-safe). A found fact is a RECORD with a readable id such as
`NVDA:revenue:FY2025:FY:10-K` (form type last; `derived` for values this project computed). Missing data
comes back as value None with a `missing_reason`, never a guess. The agent cites record ids, and the
traceability check later confirms the id was really returned in the conversation with the same value.

Tools: get_concept, compare_companies, get_metrics, list_available_periods.
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

from sec_filings_analyst_agent.reference import PERCENT_CONCEPTS, RATIO_FORMULAS, REPORTED, derived_total_liabilities

TICKERS = ("AMD", "INTC", "MSFT", "NVDA")
PERIODS = ("FY", "Q1", "Q2", "Q3", "Q4", "H1", "9M")
YTD = {"H1": "first six months, cumulative", "9M": "first nine months, cumulative"}
LABELS = {
    "revenue": "Revenue", "cost_of_revenue": "Cost of revenue", "gross_profit": "Gross profit",
    "operating_income": "Operating income", "net_income": "Net income",
    "research_and_development": "Research and development expense", "eps_diluted": "Diluted EPS",
    "total_assets": "Total assets", "total_liabilities": "Total liabilities",
    "stockholders_equity": "Stockholders' equity (parent)", "cash_and_equivalents": "Cash and cash equivalents",
    "inventory": "Inventory",
}
MAX_PERIODS = 12
TOTAL_LIABILITIES_RECIPE = (
    "DERIVED, not reported: liabilities and equity minus stockholders' equity including noncontrolling "
    "interest (parent equity where the company reports no noncontrolling interest)"
)
_ACCN = re.compile(r"accn (\d{10}-\d{2}-\d{6})")


def connect() -> duckdb.DuckDBPyConnection:
    from sec_filings_analyst_agent.build import DB_PATH
    return duckdb.connect(str(DB_PATH), read_only=True)


# ----------------------------------------------------------------------------- formatting
def filing_url(cik: int, accn: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{accn}-index.htm"


def display(value: float | None, unit: str | None, concept: str) -> str | None:
    if value is None:
        return None
    if concept in PERCENT_CONCEPTS or concept.endswith("_growth") or unit == "ratio":
        return f"{value * 100:.1f}%"
    if unit == "USD/shares":
        return f"${value:,.2f} per share"
    if abs(value) >= 1e6:
        millions = value / 1e6
        return f"${millions:,.0f} million" if abs(millions - round(millions)) < 1e-9 else f"${millions:,.3f} million"
    return f"${value:,.0f}"


def record_id(ticker: str, concept: str, fy: int, fp: str, form: str | None, derived: bool) -> str:
    return f"{ticker}:{concept}:FY{fy}:{fp}:{'derived' if derived or not form else form}"


def _check(ticker: str, concept: str | None, fp: str | None = None) -> dict | None:
    if ticker not in TICKERS:
        return {"error": f"unknown ticker {ticker!r}", "valid_tickers": list(TICKERS)}
    if concept is not None and concept not in REPORTED:
        return {"error": f"unknown concept {concept!r}", "valid_concepts": list(REPORTED)}
    if fp is not None and fp not in PERIODS:
        return {"error": f"unknown fiscal_period {fp!r}", "valid_periods": list(PERIODS)}
    return None


# ----------------------------------------------------------------------------- facts
def _restatement(con, row: dict) -> dict | None:
    r = con.execute(
        "SELECT superseded_val, superseded_form, superseded_filed, superseded_accn, latest_val, latest_form, "
        "latest_filed, change_abs, change_pct FROM restated_facts "
        "WHERE cik = ? AND tag = ? AND unit = ? AND \"end\" = ? AND \"start\" IS NOT DISTINCT FROM ? "
        "ORDER BY superseded_filed LIMIT 1",
        [row["cik"], row["source_tag"], row["unit"], row["period_end"], row["period_start"]],
    ).fetchone()
    if r is None:
        return None
    old, old_form, old_filed, old_accn, new, new_form, new_filed, d_abs, d_pct = r
    concept, unit = row["concept"], row["unit"]
    return {
        "original_value": old, "original_display": display(old, unit, concept), "original_filing": old_form,
        "original_filed": str(old_filed), "original_url": filing_url(row["cik"], old_accn) if old_accn else None,
        "change_abs": d_abs, "change_pct": d_pct,
        "explanation": (f"First reported as {display(old, unit, concept)} in the {old_form} filed {old_filed}; the "
                        f"{new_form} filed {new_filed} restated it to {display(new, unit, concept)}"
                        + (f" ({'+' if (d_abs or 0) >= 0 else ''}{d_pct * 100:.1f}%)" if d_pct is not None else "")
                        + ". The restated (latest) value is the one returned."),
    }


def _row_to_record(con, row: dict, derived_total: bool = False) -> dict:
    t, c, fy, fp = row["ticker"], row["concept"], row["fiscal_year"], row["fiscal_period"]
    derived = bool(row["is_derived"]) or derived_total
    flags = []
    if derived:
        flags.append("derived")
    if row.get("was_restated"):
        flags.append("restated")
    if row.get("has_tag_disagreement"):
        flags.append("tag_disagreement")
    if fp in YTD:
        flags.append("year_to_date")
    recipe = row.get("derivation") or (TOTAL_LIABILITIES_RECIPE if derived_total else None)
    accns = [row["accn"]] if row.get("accn") else _ACCN.findall(recipe or "")
    rec = {
        "id": record_id(t, c, fy, fp, row["form"], derived), "ticker": t, "company": row["entity_name"],
        "concept": c, "label": LABELS[c], "fiscal_year": fy, "fiscal_period": fp,
        "period_start": str(row["period_start"]) if row["period_start"] else None, "period_end": str(row["period_end"]),
        "value": row["val"], "unit": row["unit"], "display": display(row["val"], row["unit"], c),
        "form": None if derived else row["form"], "accession": None if derived else row["accn"],
        "source_urls": [filing_url(row["cik"], a) for a in accns],
        "is_derived": derived, "derivation": recipe, "flags": flags, "missing_reason": None,
        "source_tag": None if derived else row.get("source_tag"),
        "tag_priority": None if derived else row.get("tag_priority"),  # 2 = the company's fallback tag; informational, not a caveat
    }
    if fp in YTD:
        rec["note"] = f"{fp} = {YTD[fp]}, not a single quarter"
    if "restated" in flags:
        rec["restatement"] = _restatement(con, row)
    return rec


def _fetch_row(con, ticker: str, concept: str, fy: int, fp: str, roles: tuple = ("reported",)) -> dict | None:
    cur = con.execute(
        "SELECT * FROM clean_facts WHERE in_window AND ticker = ? AND concept = ? AND fiscal_year = ? "
        f"AND fiscal_period = ? AND role IN ({','.join('?' for _ in roles)}) ORDER BY is_derived, period_end DESC LIMIT 1",
        [ticker, concept, fy, fp, *roles])
    r = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], r)) if r else None


def _missing(con, ticker: str, concept: str, fy: int, fp: str) -> dict:
    if concept == "eps_diluted" and fp == "Q4":
        reason = "Diluted EPS for Q4 cannot be derived (share counts differ between periods); no reported Q4 EPS is available."
    elif concept == "total_liabilities":
        reason = ("Total liabilities is not reported by this company for this period and cannot be derived "
                  "(equity including noncontrolling interest is not available).")
    else:
        reason = f"No {LABELS[concept]} value for {ticker} FY{fy} {fp} in the warehouse."
    have = [r[0] for r in con.execute(
        "SELECT DISTINCT fiscal_period FROM clean_facts WHERE in_window AND ticker = ? AND concept = ? AND fiscal_year = ? "
        "AND role = 'reported' ORDER BY 1", [ticker, concept, fy]).fetchall()]
    return {"id": None, "ticker": ticker, "concept": concept, "label": LABELS[concept], "fiscal_year": fy,
            "fiscal_period": fp, "value": None, "display": None, "flags": [], "missing_reason": reason,
            "periods_available_for_that_year": have,
            "hint": "Call list_available_periods to see what exists for this company and concept."}


def get_fact(con, ticker: str, concept: str, fiscal_year: int, fiscal_period: str = "FY") -> dict:
    bad = _check(ticker, concept, fiscal_period)
    if bad:
        return bad
    row = _fetch_row(con, ticker, concept, fiscal_year, fiscal_period)
    if row is not None:
        return _row_to_record(con, row)
    if concept == "total_liabilities":
        value = derived_total_liabilities(con, ticker, fiscal_year, fiscal_period)
        if value is not None:
            base = _fetch_row(con, ticker, "liabilities_and_equity", fiscal_year, fiscal_period, ("reported", "component"))
            if base is not None:
                base = {**base, "concept": concept, "val": value, "form": None, "is_derived": True, "was_restated": False,
                        "has_tag_disagreement": False, "tag_priority": 1, "derivation": None}
                rec = _row_to_record(con, base, derived_total=True)
                equity = (_fetch_row(con, ticker, "stockholders_equity_incl_nci", fiscal_year, fiscal_period, ("reported", "component"))
                          or _fetch_row(con, ticker, "stockholders_equity", fiscal_year, fiscal_period))
                accns = [a for a in dict.fromkeys([base.get("accn"), equity.get("accn") if equity else None]) if a]
                rec["source_urls"] = [filing_url(base["cik"], a) for a in accns]
                return rec
    return _missing(con, ticker, concept, fiscal_year, fiscal_period)


def get_concept(ticker: str, concept: str, periods: list[dict], con=None) -> dict:
    """Facts for one company and concept over up to 12 periods; each period is {fiscal_year, fiscal_period}."""
    bad = _check(ticker, concept)
    if bad:
        return bad
    if not periods or len(periods) > MAX_PERIODS:
        return {"error": f"give between 1 and {MAX_PERIODS} periods"}
    for p in periods:
        if not isinstance(p, dict) or not isinstance(p.get("fiscal_year"), int) or isinstance(p.get("fiscal_year"), bool):
            return {"error": "each period must look like {\"fiscal_year\": 2025, \"fiscal_period\": \"FY\"}"}
    own = con is None
    con = con or connect()
    try:
        return {"records": [get_fact(con, ticker, concept, p["fiscal_year"], p.get("fiscal_period", "FY")) for p in periods]}
    finally:
        if own:
            con.close()


def compare_companies(tickers: list[str], concept: str, fiscal_year: int, fiscal_period: str = "FY", con=None) -> dict:
    """The same concept and fiscal period for 2-4 companies, with the arithmetic done in code for two."""
    if not 2 <= len(tickers) <= 4 or len(set(tickers)) != len(tickers):
        return {"error": "give between 2 and 4 distinct tickers"}
    for t in tickers:
        bad = _check(t, concept, fiscal_period)
        if bad:
            return bad
    own = con is None
    con = con or connect()
    try:
        records = []
        for t in tickers:
            records.append(get_fact(con, t, concept, fiscal_year, fiscal_period))
        out = {"records": records}
        ends = [r["period_end"] for r in records if r.get("period_end")]
        if len(ends) == len(records):
            from datetime import date
            days = (max(date.fromisoformat(e) for e in ends) - min(date.fromisoformat(e) for e in ends)).days
            if days > 31:
                out["calendar_warning"] = (f"Fiscal calendars differ: these periods end {days} days apart "
                                           f"({', '.join(f'{r['ticker']} {r['period_end']}' for r in records)}).")
        have = [r for r in records if r["value"] is not None]
        if len(records) == 2 and len(have) == 2:
            a, b = have
            diff = a["value"] - b["value"]
            out["comparison"] = {
                "larger": None if diff == 0 else (a["ticker"] if diff > 0 else b["ticker"]), "absolute_difference": abs(diff),
                "absolute_difference_display": display(abs(diff), a["unit"], concept),
                "calculation": f"{a['id']} minus {b['id']}",
                "ratio_larger_to_smaller": (max(a["value"], b["value"]) / min(a["value"], b["value"]))
                if min(a["value"], b["value"]) > 0 else None,
                "uses_records": [a["id"], b["id"]],
            }
        return out
    finally:
        if own:
            con.close()


# ----------------------------------------------------------------------------- metrics
def _prior(fy: int, fp: str, kind: str) -> tuple[int, str] | None:
    if kind == "yoy":
        return fy - 1, fp
    order = ["Q1", "Q2", "Q3", "Q4"]
    if fp not in order:
        return None
    return (fy - 1, "Q4") if fp == "Q1" else (fy, order[order.index(fp) - 1])


def _metric_inputs(con, ticker: str, metric: str, fy: int, fp: str) -> list[dict]:
    if metric in RATIO_FORMULAS:
        concepts, periods = list(RATIO_FORMULAS[metric]), [(fy, fp)]
    else:
        m = re.match(r"(.+)_(yoy|qoq)_growth$", metric)
        if not m:
            return []
        pr = _prior(fy, fp, m.group(2))
        concepts, periods = [m.group(1)], [(fy, fp)] + ([pr] if pr else [])
    recs = [get_fact(con, ticker, c, y, p) for c in concepts for (y, p) in periods]
    return [{"id": r["id"], "display": r["display"], "source_urls": r.get("source_urls", [])} for r in recs if r.get("id")]


def get_metrics(ticker: str, fiscal_year: int, fiscal_period: str = "FY", metrics: list[str] | None = None, con=None) -> dict:
    """Stored ratios and growth rates for one company and period, each with the records it was computed from."""
    bad = _check(ticker, None, fiscal_period)
    if bad:
        return bad
    own = con is None
    con = con or connect()
    try:
        sql = ("SELECT metric, value, null_reason, uses_derived_input, formula FROM metrics WHERE ticker = ? "
               "AND fiscal_year = ? AND fiscal_period = ?")
        args: list = [ticker, fiscal_year, fiscal_period]
        if metrics:
            sql += f" AND metric IN ({','.join('?' for _ in metrics)})"
            args += list(metrics)
        rows = con.execute(sql + " ORDER BY metric", args).fetchall()
        out = []
        for metric, value, null_reason, derived_in, formula in rows:
            out.append({
                "id": f"{ticker}:{metric}:FY{fiscal_year}:{fiscal_period}:metric", "ticker": ticker, "metric": metric,
                "fiscal_year": fiscal_year, "fiscal_period": fiscal_period, "value": value,
                "display": display(value, "ratio", metric), "formula": formula,
                "flags": ["derived_input"] if derived_in else [], "missing_reason": null_reason,
                "computed_from": _metric_inputs(con, ticker, metric, fiscal_year, fiscal_period) if value is not None else [],
            })
        if not out:
            return {"records": [], "missing_reason": f"No stored metrics for {ticker} FY{fiscal_year} {fiscal_period}"
                                                    + (f" matching {metrics}" if metrics else "") + ".",
                    "hint": "Call list_available_periods to see what exists."}
        return {"records": out}
    finally:
        if own:
            con.close()


# ----------------------------------------------------------------------------- periods
def list_available_periods(ticker: str, concept: str, con=None) -> dict:
    """Which fiscal years and periods exist for a company and concept (derived ones marked)."""
    bad = _check(ticker, concept)
    if bad:
        return bad
    own = con is None
    con = con or connect()
    try:
        rows = con.execute(
            "SELECT fiscal_year, fiscal_period, bool_and(is_derived) FROM clean_facts WHERE in_window AND ticker = ? "
            "AND concept = ? AND role = 'reported' GROUP BY 1, 2 ORDER BY 1, 2", [ticker, concept]).fetchall()
        years: dict[int, dict] = {}
        for fy, fp, derived in rows:
            y = years.setdefault(fy, {"fiscal_year": fy, "periods": [], "derived_periods": []})
            y["periods"].append(fp)
            if derived:
                y["derived_periods"].append(fp)
        out = {"ticker": ticker, "concept": concept, "label": LABELS[concept], "years": list(years.values())}
        if concept == "total_liabilities":
            out["note"] = ("Some periods are DERIVED on request (liabilities and equity minus equity including "
                           "noncontrolling interest) and are not listed here; call get_concept to try a period.")
        if not years:
            out["missing_reason"] = f"No {LABELS[concept]} data for {ticker}."
        return out
    finally:
        if own:
            con.close()
