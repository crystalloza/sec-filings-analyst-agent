import json

from sec_filings_analyst_agent import labeling

REAL = [{"item_id": f"r{i}", "criterion": "premise", "source": "main", "question_id": "q1", "variant": "A",
         "conversation": [{"role": "user", "content": "Q"}, {"role": "assistant", "content": f"answer {i}"}],
         "reference": "claim", "documents_ref": None} for i in range(6)]
SEEDS = [{"seed_id": f"s{i}", "criterion": "premise", "question_id": "q1", "base_item_id": f"r{i}", "mode": "agree",
          "difficulty": "obvious", "change_made": "x", "reference": "claim", "documents_ref": None,
          "conversation": [{"role": "user", "content": "Q"}, {"role": "assistant", "content": f"bad {i}"}]} for i in range(2)]


def judged(fail_id=None):
    return [{"item_id": i["item_id"], "criterion": "premise", "verdict": "fail" if i["item_id"] == fail_id else "pass",
             "confidence": "clear", "agreement": "unanimous"} for i in REAL]


def test_seed_bases_are_excluded_from_the_real_pool():
    chosen, log = labeling.select(REAL, SEEDS, judged())
    real_ids = {i["item_id"] for i in chosen if i["origin"] == "real"}
    assert not real_ids & {"r0", "r1"} and real_ids == {"r2", "r3", "r4", "r5"}
    assert sum(1 for i in chosen if i["origin"] == "seed") == 2


def test_a_seed_built_on_a_real_judge_failure_is_dropped_and_the_real_answer_stays():
    chosen, log = labeling.select(REAL, SEEDS, judged(fail_id="r0"))
    assert log["dropped_seeds"] == ["s0"]
    real_ids = {i["item_id"] for i in chosen if i["origin"] == "real"}
    assert "r0" in real_ids and "r1" not in real_ids


def test_ids_are_sequential_and_deterministic():
    a = labeling.assign_ids(labeling.select(REAL, SEEDS, judged())[0])
    b = labeling.assign_ids(labeling.select(REAL, SEEDS, judged())[0])
    assert [i["label_id"] for i in a] == [f"L{n:03d}" for n in range(1, len(a) + 1)]
    assert [i["item_id"] for i in a] == [i["item_id"] for i in b]


def test_the_page_payload_hides_everything_that_would_reveal_the_answer():
    items = labeling.assign_ids(labeling.select(REAL, SEEDS, judged())[0])
    text = json.dumps(labeling.page_payload(items, {}))
    for secret in ("seed", "origin", "variant", "difficulty", "mode", "base_item_id", "change_made", "r2", "s0", "main", "confidence", "agreement"):
        assert f'"{secret}"' not in text and f'"{secret}' not in text.replace("rubric", "")
    shown = {k for item in labeling.page_payload(items, {})["items"] for k in item}
    assert shown == {"id", "criterion", "conversation", "reference", "docs_key"}


def test_page_escapes_script_endings_and_embeds_every_item():
    items = labeling.assign_ids(labeling.select(REAL, SEEDS, judged())[0])
    items[0]["conversation"][-1]["content"] = "evil </script><b>x</b>"
    page = labeling.render_page(labeling.page_payload(items, {}))
    assert page.count("</script>") == 1  # only the page's own closing tag
    assert all(i["label_id"] in page for i in items)


def test_narrative_items_point_at_their_filing_text():
    item = {"item_id": "n1", "criterion": "narrative", "source": "main", "question_id": "q9", "variant": "A", "origin": "real",
            "conversation": [{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A"}], "reference": None,
            "documents_ref": [{"x": 1}], "label_id": "L001"}
    payload = labeling.page_payload([item], {"q9": "FILING TEXT"})
    assert payload["items"][0]["docs_key"] == "q9" and payload["docs"]["q9"] == "FILING TEXT"
