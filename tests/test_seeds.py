from collections import Counter

from sec_filings_analyst_agent import seeds


def test_every_spec_uses_a_defined_mode_and_ids_are_unique():
    assert all(s[4] in seeds.MODES for s in seeds.SPECS)
    ids = [seeds.seed_id(s) for s in seeds.SPECS]
    assert len(set(ids)) == len(ids)


def test_eight_seeds_per_criterion_graded_obvious_subtle_near_miss():
    for crit in ("premise", "misuse", "advice", "narrative"):
        specs = [s for s in seeds.SPECS if s[0] == crit]
        assert len(specs) == 8
        assert Counter(s[5] for s in specs) == {"obvious": 3, "subtle": 3, "near_miss": 2}


def test_no_seed_is_written_for_the_evasion_question():
    assert not any(s[1] == "q46" for s in seeds.SPECS)


def test_the_generator_prompt_carries_the_rubric_and_hides_nothing_it_should_show():
    item = {"criterion": "advice", "reference": None,
            "conversation": [{"role": "user", "content": "Should I buy?"}, {"role": "assistant", "content": "GOODMSG"}]}
    prompt = seeds.generator_prompt(item, "recommend", None)
    assert "No personal investment advice" in prompt and "GOODMSG" in prompt and "recommend" in prompt
    assert "Should I buy?" in prompt


def test_generator_hash_changes_when_the_specs_change(monkeypatch):
    before = seeds.generator_hash()
    monkeypatch.setattr(seeds, "SPECS", seeds.SPECS[:-1])
    assert seeds.generator_hash() != before
