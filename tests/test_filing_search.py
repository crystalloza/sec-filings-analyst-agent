"""sec-filings tools against the real warehouse and the stored embedding index (build both first)."""

import pytest

from sec_filings_analyst_agent import filing_search as fs
from sec_filings_analyst_agent.quotes import canonicalize, check_quote

pytestmark = pytest.mark.skipif(not (fs.INDEX_DIR / "embeddings.npy").exists(),
                                reason="search index not built (filing_search --build)")


@pytest.fixture(scope="module")
def con():
    c = fs.connect()
    yield c
    c.close()


def test_every_paragraph_id_matches_the_documented_format(con):
    ids = [r[0] for r in con.execute("SELECT paragraph_id FROM filing_paragraphs").fetchall()]
    assert ids and all(fs._PARAGRAPH_ID.match(i) for i in ids)


def test_index_matches_the_warehouse(con):
    ix = fs._index(con)
    assert len(ix.ids) == con.execute("SELECT count(*) FROM filing_paragraphs").fetchone()[0]
    assert ix.vecs.shape[0] == len(ix.ids)


def test_search_is_repeatable_and_ranked(con):
    a = fs.search_filing("export controls on China", ticker="NVDA", con=con)
    b = fs.search_filing("export controls on China", ticker="NVDA", con=con)
    assert [r["paragraph_id"] for r in a["results"]] == [r["paragraph_id"] for r in b["results"]]
    scores = [r["score"] for r in a["results"]]
    assert scores == sorted(scores, reverse=True) and len(a["results"]) == fs.DEFAULT_K


def test_search_finds_the_right_topic(con):
    out = fs.search_filing("export license requirements for sales to China", ticker="NVDA", fiscal_year=2025, con=con)
    assert any("export" in r["text"].lower() for r in out["results"][:3])


def test_filters_are_respected(con):
    out = fs.search_filing("supply chain dependence on foundries", ticker="AMD", fiscal_year=2023, item="1A", k=10, con=con)
    assert len(out["results"]) == 10
    assert all(r["ticker"] == "AMD" and r["fiscal_year"] == 2023 and r["item"] == "1A" for r in out["results"])


def test_no_match_gives_a_reason_not_an_error(con):
    out = fs.search_filing("revenue", ticker="AMD", fiscal_year=2010, con=con)
    assert out["results"] == [] and out["missing_reason"]


def test_bad_inputs_return_errors(con):
    for kwargs in ({"query": ""}, {"query": "x", "ticker": "TSLA"}, {"query": "x", "item": "9"},
                   {"query": "x", "k": 0}, {"query": "x", "k": 11}, {"query": "x", "fiscal_year": "2025"}):
        assert "error" in fs.search_filing(con=con, **kwargs), kwargs


def test_long_hits_are_cut_and_say_so_and_get_paragraph_returns_all(con):
    row = con.execute("SELECT paragraph_id, length(text) FROM filing_paragraphs WHERE length(text) > ? "
                      "ORDER BY paragraph_id LIMIT 1", [fs.MAX_CHARS + 50]).fetchone()
    full = fs.get_paragraph(row[0], con=con)["paragraph"]
    assert len(full["text"]) == row[1] and not full["truncated"]
    rec = fs._record(con, con.execute(f"SELECT {fs._COLS} FROM filing_paragraphs WHERE paragraph_id = ?", [row[0]]).fetchone(), cut=True)
    assert rec["truncated"] and len(rec["text"]) == fs.MAX_CHARS and rec["full_length_chars"] == row[1] and "hint" in rec


def test_neighbors_come_from_the_same_section_and_are_adjacent(con):
    pid = con.execute("SELECT paragraph_id FROM filing_paragraphs WHERE item = '7' AND paragraph_index = 10 LIMIT 1").fetchone()[0]
    out = fs.get_paragraph(pid, neighbors=2, con=con)
    assert {n["item"] for n in out["neighbors"]} == {"7"} and len(out["neighbors"]) == 4
    assert pid not in {n["paragraph_id"] for n in out["neighbors"]}


def test_get_paragraph_rejects_bad_ids_and_unknown_ids(con):
    assert "error" in fs.get_paragraph("nonsense", con=con)
    assert "error" in fs.get_paragraph("0000000000-00-000000:7:0001", con=con)
    pid = con.execute("SELECT paragraph_id FROM filing_paragraphs LIMIT 1").fetchone()[0]
    assert "error" in fs.get_paragraph(pid, neighbors=3, con=con)


def test_returned_text_passes_the_quote_check(con):
    """The agent can only be traced to what a tool returned: a sentence copied from a result must verify."""
    out = fs.search_filing("gross margin decline reasons", ticker="INTC", item="7", con=con)
    hit = out["results"][0]
    sentence = " ".join(hit["text"].split()[:12])
    shown = canonicalize(" ".join(r["text"] for r in out["results"]))
    assert check_quote(sentence, shown).status in ("verified", "too_short")


def test_index_is_tied_to_the_paragraph_text(con):
    rows = fs._corpus_rows(con)
    same = fs.corpus_hash(rows)
    assert same == (fs.INDEX_DIR / "corpus.sha").read_text().strip()
    changed = [(rows[0][0], rows[0][1], rows[0][2] + " edited")] + rows[1:]
    assert fs.corpus_hash(changed) != same


def test_over_long_query_is_truncated_and_says_so(con):
    long_q = "export license requirements for China. " * 100        # about 3,900 characters
    out = fs.search_filing(long_q, ticker="NVDA", con=con)
    assert out["query_truncated"] is True and "2000" in out["query_note"] and out["results"]
    same = fs.search_filing(long_q[:fs.MAX_QUERY_CHARS], ticker="NVDA", con=con)
    assert [r["paragraph_id"] for r in out["results"]] == [r["paragraph_id"] for r in same["results"]]
    assert "query_truncated" not in same
