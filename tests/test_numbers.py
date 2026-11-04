from sec_filings_analyst_agent.numbers import (
    ProseNumber, is_low_precision, prose_numbers, rounds_to, scale_slip, token_matches_figure,
)


def test_rounding_rule_accepts_correct_rounding_only():
    reference = 5.872  # billions
    assert rounds_to(5.9, reference)       # 5.872 rounds to 5.9
    assert rounds_to(5.87, reference)      # and to 5.87
    assert not rounds_to(5.8, reference)   # 5.8 is not a rounding of 5.872
    assert not rounds_to(5.88, reference)  # nor is 5.88
    assert rounds_to(5.872, reference)


def test_sign_must_match():
    assert not rounds_to(-22.0, 22.0)
    assert rounds_to(-22.0, -21.99)


def test_integer_valued_figure_is_whole_number_precision():
    assert rounds_to(6.0, 5.872)           # "about 6 billion" rounds correctly
    assert is_low_precision(6.0, 5.872)    # but it is coarse and gets flagged
    assert not is_low_precision(5.872, 5.872)


def test_scale_slip_is_detected():
    assert scale_slip(5.872, 5872.0) == 1e3     # wrote millions where billions was meant
    assert scale_slip(0.727, 72.7) == 100.0     # fraction written as percent
    assert scale_slip(5.9, 12.0) is None


def test_prose_numbers_ignore_years_and_fiscal_labels():
    tokens = prose_numbers("In fiscal 2023 (FY2024, Q2) revenue was $5.9 billion, up 68% from 16,434 million.")
    raws = [t.raw for t in tokens]
    assert "$5.9 billion" in raws and "68%" in raws
    assert not any(r.strip() in ("2023", "2024", "2") for r in raws)


def test_parentheses_in_prose_are_not_a_sign():
    tokens = prose_numbers("lower server revenue ($2,709 million) drove it")
    assert tokens and tokens[0].value == 2709.0
    assert token_matches_figure(tokens[0], 2.709, "USD", "billions", lenient=True)


def test_strict_matching_is_at_the_figures_precision():
    assert token_matches_figure(prose_numbers("$5,872 million")[0], 5.9, "USD", "billions")      # 5.9 rounds 5,872
    assert token_matches_figure(prose_numbers("rose to 16,434")[0], 16.434, "USD", "billions")   # scale word dropped
    assert token_matches_figure(prose_numbers("grew 68%")[0], 68.0, "percent", "units")
    assert not token_matches_figure(prose_numbers("$5.9 billion")[0], 5.872, "USD", "billions")  # strict: figure claims more precision
    assert not token_matches_figure(prose_numbers("$5.9 billion")[0], 4.2, "USD", "billions")


def test_lenient_matching_lets_prose_round_a_listed_figure():
    assert token_matches_figure(prose_numbers("$5.9 billion")[0], 5.872, "USD", "billions", lenient=True)
    assert not token_matches_figure(prose_numbers("$4.2 billion")[0], 5.872, "USD", "billions", lenient=True)


def test_negative_figure_matches_its_prose_mention():
    assert token_matches_figure(prose_numbers("a margin of 22.0%")[0], -22.0, "percent", "units", lenient=True)


def test_stray_small_numbers_do_not_support_a_figure():
    stray = ProseNumber("6", 6.0, 0, "plain", None)
    assert not token_matches_figure(stray, 5.872, "USD", "billions")      # strict: a bare 6 is not 5.872 billion
    assert not token_matches_figure(ProseNumber("10", 10.0, 0, "plain", None), 10.08, "USD", "billions")


def test_dates_and_form_names_are_not_figures():
    text = "For the year ended January 26, 2025, in its Form 10-K filed on February 26, revenue rose."
    assert prose_numbers(text) == []


def test_random_rounding_cases_agree_with_round():
    import random
    random.seed(7)
    for _ in range(3000):
        ref, d = random.uniform(0.1, 900), random.randint(0, 3)
        v, step = round(ref, d), 10 ** -d
        assert rounds_to(v, ref)
        assert not rounds_to(v + 1.01 * step, ref) and not rounds_to(v - 1.01 * step, ref)


def test_bare_numbers_after_a_word_are_found_but_labels_glued_to_letters_are_not():
    assert [t.value for t in prose_numbers("China and over 150 countries were designated")] == [150.0]
    assert [t.value for t in prose_numbers("a 120-day compliance period")] == [120.0]
    assert prose_numbers("in Q2 and FY23 and H1") == []
    assert prose_numbers("see Item 10 and Note 16 and fiscal 2024") == []
