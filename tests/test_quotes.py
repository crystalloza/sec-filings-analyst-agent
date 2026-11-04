from sec_filings_analyst_agent.quotes import canonicalize, check_quote

SOURCE = (
    "## Results\n\n[TABLE]\nResearch and development | 5,872 | 5,005\n\n"
    "Client net revenue of $4.7 billion in 2023 decreased by 25%, compared to net revenue of "
    "$6.2 billion in 2022, primarily due to lower sales of Ryzen mobile and desktop processors."
)
SRC = canonicalize(SOURCE)


def test_canonicalize_removes_our_storage_formatting():
    assert canonicalize("Research and development | 5,872 | 5,005") == "Research and development 5,872 5,005"
    assert "[TABLE]" not in SRC and "|" not in SRC and "##" not in SRC


def test_verbatim_quote_with_pipes_dropped_is_verified():
    assert check_quote("Research and development 5,872 5,005", SRC).status == "verified"
    assert check_quote("Research and development | 5,872 | 5,005", SRC).status == "verified"


def test_swapped_number_is_neither_verified_nor_near():
    swapped = ("Client net revenue of $5.1 billion in 2023 decreased by 25%, compared to net revenue of "
               "$6.2 billion in 2022, primarily due to lower sales of Ryzen mobile and desktop processors.")
    result = check_quote(swapped, SRC)
    assert result.status == "unverified"
    assert "number differs" in result.detail


def test_word_changes_with_exact_numbers_are_near_not_verified():
    tweaked = ("Client net revenue of $4.7 billion in 2023 decreased by 25%, versus net revenue of "
               "$6.2 billion in 2022, mainly due to lower sales of Ryzen mobile and desktop processors.")
    assert check_quote(tweaked, SRC).status == "near"


def test_bare_number_is_too_short():
    assert check_quote("5,872", SRC).status == "too_short"
    assert check_quote("", SRC).status == "empty"


def test_ellipsis_fragments_must_appear_in_order():
    assert check_quote("Client net revenue of $4.7 billion in 2023 ... lower sales of Ryzen mobile", SRC).status == "verified"
    assert check_quote("lower sales of Ryzen mobile ... Client net revenue of $4.7 billion in 2023", SRC).status != "verified"


def test_quote_cut_in_the_middle_of_a_number_is_not_verified():
    assert check_quote("Research and development 5,87", SRC).status != "verified"
    assert check_quote("Research and development 5,872 5,00", SRC).status != "verified"
    assert check_quote("development 5,872 5,005 Client net revenue", SRC).status == "verified"   # starting at a word is fine


def test_a_table_row_with_a_short_label_and_several_numbers_is_a_valid_quote():
    row = "Research and development | 5,872 | 5,005"
    assert check_quote(row, SRC).status == "verified"
    assert check_quote("development 5,872 5,005", SRC).status == "verified"      # one label word, three tokens
    assert check_quote("development 5,872", SRC).status == "too_short"           # label plus a single number: too thin
    assert check_quote("5,872 5,005", SRC).status == "too_short"                 # numbers alone prove nothing
