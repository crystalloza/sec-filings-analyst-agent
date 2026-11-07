"""Traceability for the agent: could a reader follow each figure's cited source to this exact number?

The evidence is REBUILT FROM THE LOG: only what the agent's tools actually returned in the conversation counts
(an invented record id, a quote not in any returned paragraph, or a URL no tool gave all fail). Rules, per figure:

  record     every cited id was returned; at least one cited record matches the figure's company, concept, period and
             value (to the figure's own rounding). A stored metric holds a fraction; a figure states a percent.
  quote      the cited paragraph was returned, the quote is verbatim in it, and the quote contains the figure's number.
  computed   every cited id was returned, and the figure is reproduced from the cited records by a known pattern
             (difference, sum, percent change, ratio or margin, change in two ratios). Not reproduced: `failed` if it is a
             kind of figure that should follow from those records, else `unverifiable`.
  background from general knowledge: no traceability credit, and not counted as fabricated (own line).
  URL        a stated URL must be one a tool returned; a missing URL is reported, not failed.

Traceable and correct are separate: this module never looks at the warehouse; checks.py does that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from itertools import permutations

from sec_filings_analyst_agent.numbers import SCALE_MULT, half_unit, prose_numbers, to_canonical, token_matches_figure
from sec_filings_analyst_agent.quotes import canonicalize, check_quote

RATIO_CONCEPTS = {"gross_margin", "operating_margin", "net_margin", "rnd_pct_revenue"}
KNOWN_ARITHMETIC = {"change_amount", "change_percent", "revenue_growth"} | RATIO_CONCEPTS
METRIC_TO_CONCEPT = {"revenue_yoy_growth": "revenue_growth"}
FIG_UNIT_TO_RECORD_UNIT = {"USD": "USD", "USD_per_share": "USD/shares", "percent": "ratio"}
EPS = 1e-9
_DISPLAY = [  # (pattern, unit, multiplier, decimals source)
    (re.compile(r"^\$(-?[\d,]+(?:\.\d+)?) million$"), "USD", 1e6),
    (re.compile(r"^\$(-?[\d,]+(?:\.\d+)?) per share$"), "USD/shares", 1.0),
    (re.compile(r"^\$(-?[\d,]+)$"), "USD", 1.0),
    (re.compile(r"^(-?[\d,]+(?:\.\d+)?)%$"), "ratio", 0.01),
]


@dataclass
class Evidence:
    paragraphs: dict = field(default_factory=dict)    # paragraph_id -> text exactly as the agent saw it
    records: dict = field(default_factory=dict)       # record id -> {value, unit, slack, ticker, concept, ...}
    urls: set = field(default_factory=set)
    shown_text: str = ""                              # every paragraph and record display, for number diagnostics


# ----------------------------------------------------------------------------- building the evidence
def parse_display(display: str | None):
    """(canonical value, unit, rounding slack) from a display string such as '$130,497 million', or None."""
    for pattern, unit, mult in _DISPLAY:
        m = pattern.match((display or "").strip())
        if m:
            raw = m.group(1).replace(",", "")
            decimals = len(raw.split(".")[1]) if "." in raw else 0
            return float(raw) * mult, unit, 0.5 * 10 ** (-decimals) * mult
    return None


def _id_parts(rid: str) -> dict:
    """ticker:concept:FYyyyy:period:form  (the id format of sec-facts)."""
    p = rid.split(":")
    if len(p) != 5 or not p[2].startswith("FY") or not p[2][2:].isdigit():
        return {}
    return {"ticker": p[0], "concept": p[1], "fiscal_year": int(p[2][2:]), "fiscal_period": p[3], "form": p[4]}


def _add_record(ev: Evidence, rec: dict, stub: bool = False) -> None:
    rid = rec.get("id")
    if not isinstance(rid, str):
        return
    parts = _id_parts(rid)
    if not parts:
        return
    is_metric = parts["form"] == "metric"
    if stub:
        parsed = parse_display(rec.get("display"))
        value, unit, slack = parsed if parsed else (None, None, 0.0)
        if rid in ev.records:                           # never let a stub overwrite a full record
            return
    else:
        value = rec.get("value")
        unit = "ratio" if is_metric else rec.get("unit")
        slack = 0.0
    ev.records[rid] = {"id": rid, "ticker": rec.get("ticker", parts["ticker"]), "concept": parts["concept"],
                       "fiscal_year": rec.get("fiscal_year", parts["fiscal_year"]),
                       "fiscal_period": rec.get("fiscal_period", parts["fiscal_period"]),
                       "is_metric": is_metric, "value": value, "unit": unit, "slack": slack, "stub": stub}
    for u in rec.get("source_urls") or []:
        ev.urls.add(u)
    if (rec.get("restatement") or {}).get("original_url"):
        ev.urls.add(rec["restatement"]["original_url"])
    if rec.get("display"):
        ev.shown_text += f"\n{rec['display']}"
    for c in rec.get("computed_from") or []:
        _add_record(ev, c, stub=True)


def _add_paragraph(ev: Evidence, r: dict) -> None:
    pid, text = r.get("paragraph_id"), r.get("text")
    if not isinstance(pid, str) or not isinstance(text, str):
        return
    if len(text) >= len(ev.paragraphs.get(pid, "")):   # a full read replaces a cut search hit
        ev.paragraphs[pid] = text
    if r.get("source_url"):
        ev.urls.add(r["source_url"])


def build_evidence(tool_calls: list[dict]) -> Evidence:
    """Everything the tools returned, from the run log. Errors, rejected calls and unparseable results add nothing."""
    ev = Evidence()
    for tc in tool_calls:
        text = tc.get("result_text")
        if not text or tc.get("is_error") or tc.get("rejected"):
            continue
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for rec in data.get("records") or []:
            if isinstance(rec, dict):
                _add_record(ev, rec)
        for r in (data.get("results") or []) + (data.get("neighbors") or []) + ([data["paragraph"]] if "paragraph" in data else []):
            if isinstance(r, dict):
                _add_paragraph(ev, r)
    ev.shown_text += "\n" + "\n".join(ev.paragraphs.values())
    return ev


# ----------------------------------------------------------------------------- one figure
def _tolerance(fig: dict) -> float:
    return half_unit(fig["value"]) * (0.01 if fig["unit"] == "percent" else SCALE_MULT[fig["scale"]])


def record_problem(fig: dict, rec: dict, company: str) -> str | None:
    """Why this cited record does not support the figure, or None if it does."""
    if rec["value"] is None:
        return "record_has_no_value"
    if rec["ticker"] != company:
        return "company_mismatch"
    concept = rec["concept"]
    if rec["is_metric"]:
        concept = METRIC_TO_CONCEPT.get(concept, concept)
        # A stored ratio or growth rate can only support a ratio-type figure. A dollar concept (revenue, gross_profit...)
        # is a different thing, and a margin or revenue-growth figure must name the same metric.
        if fig.get("concept") not in (RATIO_CONCEPTS | {"revenue_growth", "change_percent", "change_amount", "other"}):
            return "concept_mismatch"
        if fig.get("concept") in (RATIO_CONCEPTS | {"revenue_growth"}) and fig["concept"] != concept:
            return "concept_mismatch"
    elif fig.get("concept") != concept:
        return "concept_mismatch"
    if rec["fiscal_year"] != fig["fiscal_year"]:
        return "period_mismatch"
    if fig["fiscal_period"] != "other" and rec["fiscal_period"] != fig["fiscal_period"]:
        return "period_mismatch"
    if FIG_UNIT_TO_RECORD_UNIT.get(fig["unit"]) != rec["unit"]:
        return "unit_mismatch"
    canon = to_canonical(fig["value"], fig["unit"], fig["scale"])
    if canon != 0 and rec["value"] != 0 and (canon < 0) != (rec["value"] < 0):
        return "value_mismatch"
    if abs(canon - rec["value"]) > _tolerance(fig) + rec["slack"] + EPS * max(1.0, abs(rec["value"])):
        return "value_mismatch"
    return None


def computed_outcome(fig: dict, recs: list[dict]) -> str:
    """verified / failed / unverifiable: is the figure reproduced from the cited records by a known pattern?"""
    c = abs(to_canonical(fig["value"], fig["unit"], fig["scale"]))
    tol = _tolerance(fig)
    pct = fig["unit"] == "percent"

    def close(x: float, extra: float = 0.0) -> bool:
        return abs(c - abs(x)) <= tol + extra + EPS * max(1.0, abs(x))

    for a in recs:                                             # a unit conversion of one record
        if FIG_UNIT_TO_RECORD_UNIT.get(fig["unit"]) in (a["unit"], None) and close(a["value"], a["slack"]):
            return "verified"
    for a, b in permutations(recs, 2):
        if a["unit"] != b["unit"]:
            continue
        if FIG_UNIT_TO_RECORD_UNIT.get(fig["unit"]) in (a["unit"],) and (
                close(a["value"] - b["value"], a["slack"] + b["slack"]) or close(a["value"] + b["value"], a["slack"] + b["slack"])):
            return "verified"
        if b["value"]:
            ratio, change = a["value"] / b["value"], (a["value"] - b["value"]) / abs(b["value"])
            rel = (a["slack"] + abs(ratio) * b["slack"]) / abs(b["value"])
            if pct and (close(ratio, rel) or close(change, rel)):
                return "verified"
            if not pct and fig["unit"] == "other" and close(ratio, rel):
                return "verified"
    if len(recs) == 4 and pct:                                 # change in a ratio: a/b - c/d
        for a, b, c2, d in permutations(recs, 4):
            if b["value"] and d["value"] and close(a["value"] / b["value"] - c2["value"] / d["value"], 0.002):
                return "verified"
    return "failed" if fig.get("concept") in KNOWN_ARITHMETIC else "unverifiable"


def check_agent_source(fig: dict, ev: Evidence, company: str) -> dict:
    """The traceability verdict for one figure. traceable is True, False, or None (background / unverifiable)."""
    st = fig.get("source_type") or "missing"
    ids = list(fig.get("source_ids") or [])
    url = fig.get("source_url")
    out = {"type": st, "status": None, "detail": "", "traceable": False,
           "url": ("ok" if url in ev.urls else "not_returned") if url else ("missing" if st != "background" else "none")}

    def done(status: str, traceable, detail: str = "") -> dict:
        out.update(status=status, traceable=traceable, detail=detail)
        if traceable is True and out["url"] == "not_returned":     # a link no tool gave is an invented source
            out.update(status="url_not_returned", traceable=False, detail="the stated URL was not returned by any tool")
        return out

    if st == "background":
        return done("background", None)
    if st not in ("record", "quote", "computed"):
        return done("no_source_type", False)
    if not ids:
        return done("no_source_id", False, "no source id cited")
    if st == "record":
        missing = [i for i in ids if i not in ev.records]
        if missing:
            return done("id_not_returned", False, f"never returned: {missing[0]}")
        problems = [record_problem(fig, ev.records[i], company) for i in ids]
        if any(p is None for p in problems):
            return done("verified", True)
        return done(problems[0], False, f"cited record does not match the figure ({problems[0]})")
    if st == "quote":
        missing = [i for i in ids if i not in ev.paragraphs]
        if missing:
            return done("paragraph_not_returned", False, f"never returned: {missing[0]}")
        text = canonicalize(" ".join(ev.paragraphs[i] for i in ids))
        quote = fig.get("source_quote") or ""
        qr = check_quote(quote, text, text.split())
        if qr.status != "verified":
            return done("quote_not_verbatim", False, f"quote is {qr.status} in the cited paragraph")
        if not any(token_matches_figure(t, fig["value"], fig["unit"], fig["scale"]) for t in prose_numbers(quote)):
            return done("number_not_in_quote", False, "the quote does not contain the figure's number")
        return done("verified", True)
    # computed
    missing = [i for i in ids if i not in ev.records and i not in ev.paragraphs]
    if missing:
        return done("id_not_returned", False, f"never returned: {missing[0]}")
    recs = [ev.records[i] for i in ids if i in ev.records]
    if len(recs) < len(ids) or any(r["value"] is None for r in recs):
        return done("unverifiable", None, "cites text or a record without a value; cannot be recomputed")
    outcome = computed_outcome(fig, recs)
    if outcome == "verified":
        return done("verified", True)
    if outcome == "unverifiable":
        return done("unverifiable", None, "a calculation this check does not recompute")
    return done("arithmetic_not_reproduced", False, "the figure does not follow from the cited records")
