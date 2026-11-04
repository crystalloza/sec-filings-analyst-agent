"""Assemble the documents a question gets, and annotate whether its answer is in them.

Document selection is by the question's own labels (companies, fiscal years, sections), so the
baseline always sees the right filing: any failure is then the model's, not a retrieval miss.

The coverage annotation (`answer_in_docs`) is a HEURISTIC: it looks for the expected figure in the
pasted text in a few written forms (millions with commas, billions with decimals, percentages).
It separates "the model invented a figure the text never contained" from "the model misread a
figure the text did contain". The deterministic checks will have their own, stricter extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import duckdb

SECTION_TITLES = {
    "7": "Item 7 - Management's Discussion and Analysis",
    "1A": "Item 1A - Risk Factors",
    "1": "Item 1 - Business",
}
DEFAULT_SECTIONS = ["7"]


class MissingDocumentError(Exception):
    """A question asks for a filing the corpus does not have (a data error for answerable questions)."""


@dataclass
class Document:
    ticker: str
    fiscal_year: int
    item: str
    accn: str
    report_date: str
    text: str
    words: int

    def ref(self) -> dict:
        return {"ticker": self.ticker, "fiscal_year": self.fiscal_year, "item": self.item,
                "accn": self.accn, "words": self.words}


@dataclass
class Context:
    documents: list[Document]
    notes: list[str] = field(default_factory=list)


def _section_text(con: duckdb.DuckDBPyConnection, accn: str, item: str) -> str:
    rows = con.execute(
        "SELECT heading, is_table, text FROM filing_paragraphs "
        "WHERE accn = ? AND item = ? ORDER BY paragraph_index",
        [accn, item],
    ).fetchall()
    parts, last_heading = [], None
    for heading, is_table, text in rows:
        if heading != last_heading:
            parts.append(f"## {heading}")
            last_heading = heading
        parts.append(f"[TABLE]\n{text}" if is_table else text)
    return "\n\n".join(parts)


def _find_filing(con, ticker: str, fiscal_year: int, item: str):
    return con.execute(
        "SELECT accn, report_date, words FROM filing_manifest "
        "WHERE ticker = ? AND fiscal_year = ? AND item = ? AND status = 'ok'",
        [ticker, fiscal_year, item],
    ).fetchone()


def _latest_fiscal_year(con, ticker: str, item: str) -> int | None:
    row = con.execute(
        "SELECT max(fiscal_year) FROM filing_manifest WHERE ticker = ? AND item = ? AND status = 'ok'",
        [ticker, item],
    ).fetchone()
    return row[0] if row else None


def load_context(con: duckdb.DuckDBPyConnection, question: dict) -> Context:
    """The filings a question gets: each named company x fiscal year x section.

    For an UNANSWERABLE question about a period with no filing (for example a future quarter), the
    company's latest filing is used instead and the substitution is noted, so the model sees
    realistic documents and must decide for itself that the answer is not there.
    """
    sections = question.get("sections", DEFAULT_SECTIONS)
    # `context_periods` overrides which filings are shown, for periods that have no 10-K of their own
    # (a quarter asked about is answered from the company's latest annual report, which cannot contain it).
    years = sorted({p["fiscal_year"] for p in question.get("context_periods", question["expected_periods"])})
    answerable = question.get("answerable", True)
    docs, notes = [], []
    for ticker in question["companies"]:
        for year in years:
            for item in sections:
                found = _find_filing(con, ticker, year, item)
                use_year = year
                if not found:
                    if answerable:
                        raise MissingDocumentError(
                            f"{question['id']}: no {ticker} FY{year} Item {item} in the corpus"
                        )
                    use_year = _latest_fiscal_year(con, ticker, item)
                    found = _find_filing(con, ticker, use_year, item) if use_year else None
                    if not found:
                        raise MissingDocumentError(f"{question['id']}: no {ticker} Item {item} at all")
                    notes.append(f"{ticker} FY{year} not in corpus; gave latest filing FY{use_year}")
                accn, report_date, words = found
                if any(d.accn == accn and d.item == item for d in docs):
                    continue
                docs.append(Document(ticker, use_year, item, accn, str(report_date),
                                     _section_text(con, accn, item), words))
    return Context(docs, notes)


def format_documents(docs: list[Document]) -> str:
    blocks = []
    for d in docs:
        header = (f'<document company="{d.ticker}" fiscal_year="{d.fiscal_year}" '
                  f'fiscal_year_end="{d.report_date}" form="10-K" '
                  f'section="{SECTION_TITLES.get(d.item, d.item)}">')
        blocks.append(f"{header}\n{d.text}\n</document>")
    return "<documents>\n" + "\n\n".join(blocks) + "\n</documents>"


# ----------------------------------------------------------------------- coverage annotation
def _reference_value(con, item: dict):
    """(value, kind) for an expected figure, from clean_facts (concept) or metrics (ratio)."""
    if "concept" in item:
        row = con.execute(
            "SELECT val, unit FROM clean_facts WHERE ticker = ? AND concept = ? AND fiscal_year = ? "
            "AND fiscal_period = ? AND role = 'reported' LIMIT 1",
            [item["company"], item["concept"], item["fiscal_year"], item["fiscal_period"]],
        ).fetchone()
        if not row:
            return None, None
        return row[0], ("usd_per_share" if row[1] == "USD/shares" else "usd")
    duration = "annual" if item["fiscal_period"] == "FY" else "quarterly"
    row = con.execute(
        "SELECT value FROM metrics WHERE ticker = ? AND metric = ? AND fiscal_year = ? "
        "AND fiscal_period = ? AND duration_class = ? LIMIT 1",
        [item["company"], item["metric"], item["fiscal_year"], item["fiscal_period"], duration],
    ).fetchone()
    return (row[0], "ratio") if row and row[0] is not None else (None, None)


def _variants(value: float, kind: str) -> list[str]:
    """Regexes for the ways a value is commonly written in filing text."""
    v = abs(value)
    forms: list[str] = []
    if kind == "usd":
        millions = v / 1e6
        if millions >= 100:
            forms += [re.escape(f"{round(millions):,}"), re.escape(f"{round(millions)}")]
        billions = v / 1e9
        if billions >= 1:  # "6.2" alone is too loose; require the word "billion" after it
            forms += [re.escape(f"{billions:.1f}") + r"\s*billion", re.escape(f"{billions:.2f}") + r"\s*billion"]
    elif kind == "usd_per_share":
        forms.append(re.escape(f"{v:.2f}"))
    elif kind == "ratio":
        pct = v * 100
        forms += [re.escape(f"{pct:.1f}") + r"\s?%", re.escape(f"{pct:.0f}") + r"\s?%"]
    return [r"(?<![\d.,])" + f + r"(?![\d])" for f in forms]


def answer_in_docs(con, question: dict, context_text: str) -> dict:
    """Is each expected figure present in the pasted text? Returns a per-figure and overall label."""
    # A hand-set label wins over the heuristic (use it when the heuristic is known to mislead,
    # e.g. a derived figure whose digits happen to match an unrelated number in the text).
    if question.get("answer_in_docs"):
        return {"overall": question["answer_in_docs"], "figures": [], "source": "manual"}
    expected = question.get("expected", [])
    if not expected:
        return {"overall": None, "figures": []}
    results = []
    for item in expected:
        value, kind = _reference_value(con, item)
        matched = None
        if value is not None:
            for rx in _variants(value, kind):
                if re.search(rx, context_text):
                    matched = rx
                    break
        results.append({"expected": item, "reference_value": value, "found_in_docs": matched is not None})
    found = [r["found_in_docs"] for r in results]
    overall = "all" if all(found) else ("none" if not any(found) else "partial")
    return {"overall": overall, "figures": results}
