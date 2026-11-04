from sec_filings_analyst_agent.prompts import SYSTEMS, prompt_hash
from sec_filings_analyst_agent.schemas import Answer


def test_quote_description_tells_the_model_to_include_the_number():
    description = Answer.model_json_schema()["$defs"]["Figure"]["properties"]["source_quote"]["description"]
    assert "figure's own number" in description


def test_variants_share_the_same_format_rules_and_differ_only_in_behavior_rules():
    a, b = SYSTEMS["A"], SYSTEMS["B"]
    format_tail = a[a.index("Respond with the JSON object"):]
    assert b.endswith(format_tail)
    assert "Rules:" in b and "Rules:" not in a


def test_hashes_are_stable_and_distinct():
    assert prompt_hash("A") == prompt_hash("A")
    assert prompt_hash("A") != prompt_hash("B")
