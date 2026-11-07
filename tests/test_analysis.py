"""The agreement statistics, the retest selection, and the labeled-set loader."""

import json

import pytest

from sec_filings_analyst_agent import judge, retest
from sec_filings_analyst_agent.agreement import kappa, stats


def test_kappa_known_values():
    assert kappa([("pass", "pass"), ("fail", "fail"), ("pass", "pass"), ("fail", "fail")]) == pytest.approx(1.0)
    # hand-computed: observed 0.75, expected by chance 0.5 -> 0.5
    assert kappa([("pass", "pass"), ("pass", "fail"), ("fail", "fail"), ("fail", "fail")]) == pytest.approx(0.5)
    # no better than chance
    assert kappa([("pass", "pass"), ("pass", "fail"), ("fail", "pass"), ("fail", "fail")]) == pytest.approx(0.0)
    assert kappa([("pass", "pass"), ("pass", "pass")]) is None       # one class only: undefined
    assert kappa([]) is None


def test_stats_counts_recalls_and_borderline_recoding():
    rows = [{"human": "fail", "judge": "fail", "borderline": False}, {"human": "fail", "judge": "pass", "borderline": False},
            {"human": "pass", "judge": "pass", "borderline": False}, {"human": "pass", "judge": "fail", "borderline": True}]
    s = stats(rows)
    assert (s["tp"], s["fn"], s["tn"], s["fp"]) == (1, 1, 1, 1) and s["fail_recall"] == "1/2" and s["pass_recall"] == "1/2"
    as_fail = stats(rows, border="fail")                       # the borderline human-pass is recoded as fail
    assert (as_fail["tp"], as_fail["fp"]) == (2, 0)
    as_pass = stats(rows, border="pass")
    assert (as_pass["tp"], as_pass["fp"]) == (1, 1)


def test_retest_selection_is_stratified_repeatable_and_excludes_corrected_labels():
    key = json.loads(retest.KEY_PATH.read_text())["items"]
    labels = json.loads(retest.LABELS_PATH.read_text())["labels"]
    a, b = retest.choose(key, labels), retest.choose(key, labels)
    assert [k["label_id"] for k in a] == [k["label_id"] for k in b] and len(a) == 15
    assert not any("relabeled_from" in labels[k["label_id"]] for k in a)
    for crit, (n_real, n_seed) in retest.QUOTA.items():
        got = [k for k in a if k["criterion"] == crit]
        assert sum(k["origin"] == "real" for k in got) == n_real and sum(k["origin"] == "seed" for k in got) == n_seed


def test_labeled_set_becomes_judge_items_without_leaking_the_source():
    items = judge.items_from_key(retest.KEY_PATH)
    assert len(items) == 78
    for it in items:
        assert it["criterion"] in ("premise", "misuse", "advice", "narrative")
        assert (it["reference"] is not None) == (it["criterion"] in ("premise", "misuse"))
        assert set(it) == {"item_id", "criterion", "source", "question_id", "variant", "conversation", "reference", "documents_ref"}
        if it["source"] == "seed":
            assert it["variant"] is None
