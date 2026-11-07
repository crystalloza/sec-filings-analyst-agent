"""The agent's traceability check, tested by breaking it on purpose.

Every test starts from REAL tool output (the actual sec-facts and sec-filings functions), builds a correct figure, and
then corrupts exactly one thing. The check must pass the correct figure and catch every corruption.
"""

import json

import pytest

from sec_filings_analyst_agent import checks, facts_tools as f, filing_search as fs
from sec_filings_analyst_agent.traceability import build_evidence, check_agent_source, parse_display


def call(tool, result):
    return {"tool": tool, "arguments": {}, "result_text": json.dumps(result), "is_error": False, "rejected": None}


@pytest.fixture(scope="module")
def con():
    c = f.connect()
    yield c
    c.close()


@pytest.fixture(scope="module")
def calls(con):
    periods = [{"fiscal_year": 2025, "fiscal_period": "FY"}, {"fiscal_year": 2024, "fiscal_period": "FY"}]
    return [
        call("get_concept", f.get_concept("NVDA", "revenue", periods, con=con)),
        call("get_concept", f.get_concept("NVDA", "gross_profit", periods[:1], con=con)),
        call("get_concept", f.get_concept("AMD", "revenue", periods[:1], con=con)),
        call("get_metrics", f.get_metrics("NVDA", 2025, "FY", ["gross_margin", "operating_margin"], con=con)),
        call("get_metrics", f.get_metrics("NVDA", 2024, "FY", ["gross_margin"], con=con)),
        call("search_filing", fs.search_filing("export license requirements China", ticker="NVDA", fiscal_year=2025, k=4, con=con)),
    ]


@pytest.fixture(scope="module")
def ev(calls):
    return build_evidence(calls)


REV25, REV24 = "NVDA:revenue:FY2025:FY:10-K", "NVDA:revenue:FY2024:FY:10-K"


def fig(**kw):
    base = {"company": "NVDA", "metric": "revenue", "concept": "revenue", "scope": "company_total", "value": 130.497,
            "unit": "USD", "scale": "billions", "fiscal_year": 2025, "fiscal_period": "FY", "source_quote": "",
            "is_computed": False, "source_type": "record", "source_ids": [REV25], "source_url": None}
    base.update(kw)
    return base


def trace(ev, **kw):
    return check_agent_source(fig(**kw), ev, "NVDA")


# ----------------------------------------------------------------------------- records
def test_a_correct_record_figure_is_traceable(ev):
    t = trace(ev)
    assert t["traceable"] is True and t["status"] == "verified" and t["url"] == "missing"


def test_wrong_value_on_a_real_id_fails(ev):
    assert trace(ev, value=125.0)["status"] == "value_mismatch"
    assert trace(ev, value=130.497, scale="millions")["status"] == "value_mismatch"       # scale slip


def test_an_id_the_tools_never_returned_fails(ev):
    t = trace(ev, source_ids=["NVDA:revenue:FY2025:FY:10-Q"])
    assert t["traceable"] is False and t["status"] == "id_not_returned"


def test_another_companys_record_fails(ev):
    t = trace(ev, source_ids=["AMD:revenue:FY2025:FY:10-K"])          # real and returned, but not NVDA's
    assert t["status"] == "company_mismatch"


def test_wrong_period_record_fails(ev):
    assert trace(ev, source_ids=[REV24])["status"] == "period_mismatch"
    assert trace(ev, fiscal_year=2024, source_ids=[REV25])["status"] == "period_mismatch"


def test_wrong_concept_record_fails(ev):
    assert trace(ev, source_ids=["NVDA:gross_profit:FY2025:FY:10-K"])["status"] == "concept_mismatch"


def test_a_figure_with_no_source_id_or_type_fails(ev):
    assert trace(ev, source_ids=[])["status"] == "no_source_id"
    assert trace(ev, source_type="vibes")["status"] == "no_source_type"


def test_an_invented_url_fails_and_a_missing_url_is_only_reported(ev):
    good = ev.records[REV25]
    url = next(iter(u for u in ev.urls if "1045810" in u))
    assert trace(ev, source_url=url)["traceable"] is True and trace(ev, source_url=url)["url"] == "ok"
    bad = trace(ev, source_url="https://www.sec.gov/Archives/edgar/data/1/000000000000000000/fake-index.htm")
    assert bad["traceable"] is False and bad["status"] == "url_not_returned"
    assert good["value"] == 130_497_000_000


def test_stored_metric_matches_percent_to_fraction_and_rejects_the_wrong_one(ev):
    ok = trace(ev, metric="gross margin", concept="gross_margin", value=75.0, unit="percent", scale="units",
               source_ids=["NVDA:gross_margin:FY2025:FY:metric"])
    assert ok["traceable"] is True
    assert trace(ev, metric="gross margin", concept="gross_margin", value=74.0, unit="percent", scale="units",
                 source_ids=["NVDA:gross_margin:FY2025:FY:metric"])["status"] == "value_mismatch"
    assert trace(ev, metric="operating margin", concept="operating_margin", value=75.0, unit="percent", scale="units",
                 source_ids=["NVDA:gross_margin:FY2025:FY:metric"])["status"] == "concept_mismatch"


def test_a_stored_percent_cannot_support_a_dollar_concept(ev):
    """Real case from the dev pass: a -19.2% growth rate labelled concept gross_profit."""
    t = trace(ev, concept="gross_profit", value=75.0, unit="percent", scale="units",
              source_ids=["NVDA:gross_margin:FY2025:FY:metric"])
    assert t["traceable"] is False and t["status"] == "concept_mismatch"
    assert trace(ev, concept="other", value=75.0, unit="percent", scale="units",
                 source_ids=["NVDA:gross_margin:FY2025:FY:metric"])["traceable"] is True       # unlabelled ratio: allowed


def test_a_record_seen_only_as_a_displayed_input_is_still_checked_against_its_display(calls):
    only_metrics = build_evidence([calls[3]])                          # get_metrics lists its inputs, with displays only
    assert REV25 in only_metrics.records and only_metrics.records[REV25]["stub"]
    assert trace(only_metrics)["traceable"] is True                    # $130,497 million, within the displayed rounding
    assert trace(only_metrics, value=131.0)["status"] == "value_mismatch"


# ----------------------------------------------------------------------------- quotes
@pytest.fixture(scope="module")
def para(ev):
    """A returned paragraph, a verbatim window around a real figure-like number in it, and that number."""
    from sec_filings_analyst_agent.numbers import prose_numbers
    for pid, text in ev.paragraphs.items():
        nums = prose_numbers(text)
        words = text.split()
        for n in nums:
            for i, w in enumerate(words):
                if w.strip(".,;:()") == n.raw and 6 < i < len(words) - 6:
                    return pid, " ".join(words[i - 5:i + 5]), n.value
    pytest.skip("no suitable paragraph")


def qfig(pid, quote, value, **kw):
    return dict(source_type="quote", source_ids=[pid], source_quote=quote, value=float(value), unit="other", scale="units",
                concept="other", metric="year", **kw)


def test_a_verbatim_quote_with_the_number_is_traceable(ev, para):
    pid, quote, n = para
    assert trace(ev, **qfig(pid, quote, n))["traceable"] is True


def test_an_altered_quote_fails(ev, para):
    pid, quote, n = para
    words = quote.split(); words[0] = "Zebra"
    assert trace(ev, **qfig(pid, " ".join(words), n))["status"] == "quote_not_verbatim"


def test_a_quote_without_the_figures_number_fails(ev, para):
    pid, quote, n = para
    assert trace(ev, **qfig(pid, quote, n + 7))["status"] == "number_not_in_quote"


def test_a_paragraph_the_tools_never_returned_fails(ev, para):
    _, quote, n = para
    assert trace(ev, **qfig("0000000000-00-000000:7:0001", quote, n))["status"] == "paragraph_not_returned"


def test_quote_from_a_different_paragraph_than_cited_fails(ev, para):
    pid, quote, n = para
    other = next(p for p in ev.paragraphs if p != pid and quote not in ev.paragraphs[p])
    assert trace(ev, **qfig(other, quote, n))["status"] == "quote_not_verbatim"


# ----------------------------------------------------------------------------- computed figures
def cfig(value, ids, unit="USD", scale="billions", concept="change_amount", **kw):
    return dict(source_type="computed", source_ids=ids, value=value, unit=unit, scale=scale, concept=concept,
                metric="calc", is_computed=True, **kw)


def test_correct_arithmetic_is_verified(ev):
    assert trace(ev, **cfig(69.575, [REV25, REV24]))["traceable"] is True                         # difference
    assert trace(ev, **cfig(114.2, [REV25, REV24], unit="percent", scale="units", concept="change_percent"))["traceable"] is True
    assert trace(ev, **cfig(75.0, ["NVDA:gross_profit:FY2025:FY:10-K", REV25], unit="percent", scale="units",
                            concept="gross_margin"))["traceable"] is True                         # margin from two records
    assert trace(ev, **cfig(130.497, [REV25]))["traceable"] is True                               # a unit conversion


def test_change_in_a_ratio_from_two_metric_records_is_verified(ev):
    ids = ["NVDA:gross_margin:FY2025:FY:metric", "NVDA:gross_margin:FY2024:FY:metric"]
    pp = (ev.records[ids[0]]["value"] - ev.records[ids[1]]["value"]) * 100
    assert trace(ev, **cfig(round(pp, 1), ids, unit="percent", scale="units", concept="change_amount"))["traceable"] is True


def test_wrong_arithmetic_on_real_ids_fails(ev):
    t = trace(ev, **cfig(70.5, [REV25, REV24]))
    assert t["traceable"] is False and t["status"] == "arithmetic_not_reproduced"
    assert trace(ev, **cfig(100.0, [REV25, REV24], unit="percent", scale="units", concept="change_percent"))["traceable"] is False


def test_computed_from_an_id_never_returned_fails(ev):
    assert trace(ev, **cfig(69.575, [REV25, "NVDA:revenue:FY2023:FY:10-K"]))["status"] == "id_not_returned"


def test_a_calculation_the_check_cannot_redo_is_unverifiable_not_failed(ev, para):
    t = trace(ev, **cfig(42.0, [REV25, REV24], concept="other"))
    assert t["traceable"] is None and t["status"] == "unverifiable"
    pid = para[0]
    assert trace(ev, **cfig(3.0, [pid]))["status"] == "unverifiable"                               # text paragraphs cannot be recomputed


# ----------------------------------------------------------------------------- background, evidence, scoring
def test_background_is_its_own_line_no_credit_and_never_fabricated(ev, con):
    t = trace(ev, source_type="background", source_ids=[], source_url=None)
    assert t["traceable"] is None and t["type"] == "background"
    row = {"figures": [checks.check_agent_figure(con, fig(source_type="background", source_ids=[], value=999.0), checks.DocsText.from_text(ev.shown_text), ev)],
           "required": [], "declined": False, "behavior": {"docs_adjusted": "na"}, "prose_unlisted": []}
    h = checks.headline([row])
    assert h["traceable_n"] == 0 and h["traceability"] is None                     # excluded from the denominator
    assert checks.agent_traceability([row])["background"] == 1


def test_headline_is_strict_unverifiable_counts_as_not_traceable(ev, con):
    docs = checks.DocsText.from_text(ev.shown_text)
    figs = [checks.check_agent_figure(con, fig(), docs, ev),
            checks.check_agent_figure(con, fig(value=125.0), docs, ev),
            checks.check_agent_figure(con, fig(**cfig(42.0, [REV25, REV24], concept="other")), docs, ev)]
    row = {"figures": figs, "required": [], "declined": False, "behavior": {"docs_adjusted": "na"}, "prose_unlisted": []}
    h = checks.headline([row])
    assert (h["traceable_n"], h["traceability"]) == (3, pytest.approx(1 / 3))


def test_evidence_ignores_errors_rejected_calls_and_garbage(calls):
    junk = [{"result_text": "not json", "is_error": False}, {"result_text": None, "is_error": True, "rejected": "x"},
            {"result_text": json.dumps({"error": "unknown ticker"}), "is_error": False},
            {"result_text": json.dumps({"records": [{"id": "NVDA:revenue:FY2025:FY:10-K", "value": 1}]}), "is_error": True}]
    assert build_evidence(junk).records == {}
    assert REV25 in build_evidence(junk + calls).records


def test_a_full_paragraph_replaces_a_cut_search_hit_and_a_stub_never_replaces_a_full_record(ev):
    short = {"result_text": json.dumps({"results": [{"paragraph_id": "p", "text": "short"}]}), "is_error": False}
    full = {"result_text": json.dumps({"paragraph": {"paragraph_id": "p", "text": "short and then the full text"}}), "is_error": False}
    assert build_evidence([short, full]).paragraphs["p"].endswith("full text")
    assert build_evidence([full, short]).paragraphs["p"].endswith("full text")


def test_evidence_spans_the_whole_conversation_when_scored(tmp_path, calls):
    """Turn 2 may cite records fetched in turn 1."""
    def rec(turn, tool_calls, conv="c1"):
        return {"run_id": f"r{turn}", "question_id": "q28", "variant": "AGENT", "repeat": 0, "turn": turn, "conversation_id": conv,
                "mode": "dev", "parsed": {"answer": "x", "declined": False, "missing_info": None, "uses_background_knowledge": False,
                                          "figures": [fig(company="NVDA")]}, "tool_calls": tool_calls, "error": None}
    path = tmp_path / "a.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (rec(1, calls), rec(2, []))) + "\n")
    res, _ = checks.score_runs(path)
    t2 = [r for r in res if r["turn"] == 2][0]
    assert t2["figures"][0]["traceable"] is True                         # cites turn 1's record
    path.write_text("\n".join(json.dumps(r) for r in (rec(1, calls, conv=None), rec(2, [], conv=None))) + "\n")
    res, _ = checks.score_runs(path)
    assert [r for r in res if r["turn"] == 2][0]["figures"][0]["traceable"] is False   # no shared conversation, no evidence


def test_parse_display_formats():
    assert parse_display("$130,497 million") == (130_497e6, "USD", 0.5e6)
    assert parse_display("$2.94 per share")[:2] == (2.94, "USD/shares")
    assert parse_display("75.0%")[0] == pytest.approx(0.75) and parse_display("garbage") is None


# ----------------------------------------------------------------------------- the warehouse side stays separate
def test_a_figure_from_a_derived_record_satisfies_derive_with_label(con):
    calls = [call("get_concept", f.get_concept("AMD", "total_liabilities", [{"fiscal_year": 2024, "fiscal_period": "FY"}], con=con))]
    ev = build_evidence(calls)
    rid = "AMD:total_liabilities:FY2024:FY:derived"
    value = ev.records[rid]["value"] / 1e6
    fg = fig(company="AMD", metric="total liabilities", concept="total_liabilities", value=value, scale="millions", fiscal_year=2024,
             source_ids=[rid])
    res = checks.check_agent_figure(con, fg, checks.DocsText.from_text(ev.shown_text), ev)
    assert res["status"] == "verified_warehouse" and res["traceable"] is True and res["from_derived_record"]
    verdict = checks.behavior_verdict({"expected_behavior": "derive_with_label"},
                                      {"declined": False, "answer": "Derived: liabilities and equity minus equity."}, [res], [])
    assert verdict["verdict"] == "pass"


def test_stored_growth_metric_without_an_independent_reference_gets_its_own_tier(con, calls):
    c = call("get_metrics", f.get_metrics("NVDA", 2025, "FY", ["cost_of_revenue_yoy_growth"], con=con))
    ev = build_evidence([c])
    rid = "NVDA:cost_of_revenue_yoy_growth:FY2025:FY:metric"
    val = ev.records[rid]["value"] * 100
    fg = fig(metric="cost of revenue growth", concept="other", value=round(val, 1), unit="percent", scale="units", source_ids=[rid])
    res = checks.check_agent_figure(con, fg, checks.DocsText.from_text(ev.shown_text), ev)
    assert res["status"] == "verified_record" and "record_only" in res["flags"]


def test_prose_citation_ids_are_not_counted_as_unlisted_numbers():
    text = "Revenue was $130.5 billion (NVDA:revenue:FY2025:FY:10-K) per 0001045810-26-000021:7:0049 https://www.sec.gov/x/0001045810-26-000021-index.htm"
    assert checks.prose_unlisted(text, [], strip_citations=True) == ["$130.5 billion"]
    assert len(checks.prose_unlisted(text, [], strip_citations=False)) > 1


def test_a_wrong_figure_with_a_real_cited_record_is_caught_on_both_axes(con, ev):
    res = checks.check_agent_figure(con, fig(value=120.0), checks.DocsText.from_text(ev.shown_text), ev)
    assert res["traceable"] is False and res["status"] in checks.BAD            # not traceable AND not correct
