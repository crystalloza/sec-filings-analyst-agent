"""The `sec-filings` tools: meaning-based search over the 10-K paragraphs, and paragraph reading.

  uv run python -m sec_filings_analyst_agent.filing_search --build     # embed every paragraph once (a few minutes)

Embeddings come from a LOCAL open-source model (BGE-small, run through fastembed/ONNX), so there is no extra API
key. The paragraphs are embedded once and stored in data/index/, so retrieval is repeatable: same query, same
filters, same results. The text a call returns is exactly what the agent "was shown", which is what the quote
check later verifies against.

Tools: search_filing, get_paragraph. Both read-only, fixed parameters, no free SQL.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

import duckdb
import numpy as np

from sec_filings_analyst_agent.facts_tools import TICKERS, filing_url

MODEL_NAME = "BAAI/bge-small-en-v1.5"
INDEX_DIR = Path(__file__).resolve().parents[2] / "data" / "index"
ITEMS = {"1": "Business", "1A": "Risk Factors", "7": "Management's Discussion and Analysis"}
DEFAULT_K, MAX_K = 5, 10
MAX_CHARS = 1800            # a search hit longer than this is cut, and says so; get_paragraph returns the full text
MAX_NEIGHBORS = 2
MAX_QUERY_CHARS = 2000   # about what the embedding model reads (512 tokens); the rest would be ignored silently
_PARAGRAPH_ID = re.compile(r"^\d{10}-\d{2}-\d{6}:(1A|1|7):\d{4}$")


def connect() -> duckdb.DuckDBPyConnection:
    from sec_filings_analyst_agent.build import DB_PATH
    return duckdb.connect(str(DB_PATH), read_only=True)


def _model():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODEL_NAME)


def passage_text(heading: str | None, text: str) -> str:
    """What gets embedded: the nearest heading gives a short paragraph its context."""
    return f"{heading}. {text}" if heading else text


# ----------------------------------------------------------------------------- index
def corpus_hash(rows) -> str:
    """Fingerprint of every paragraph's id and embedded text: a changed paragraph makes a stored index stale."""
    h = hashlib.sha256()
    for pid, heading, text in rows:
        h.update(f"{pid}\t{passage_text(heading, text)}\n".encode())
    return h.hexdigest()


def _corpus_rows(con):
    return con.execute("SELECT paragraph_id, heading, text FROM filing_paragraphs ORDER BY paragraph_id").fetchall()


def build_index() -> None:
    con = connect()
    rows = _corpus_rows(con)
    con.close()
    model = _model()
    texts = [passage_text(h, t) for _, h, t in rows]
    print(f"embedding {len(texts)} paragraphs with {MODEL_NAME} ...")
    vecs = np.vstack([np.asarray(v, dtype=np.float32) for v in model.embed(texts, batch_size=64)])
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.save(INDEX_DIR / "embeddings.npy", vecs)
    (INDEX_DIR / "ids.txt").write_text("\n".join(r[0] for r in rows))
    (INDEX_DIR / "model.txt").write_text(MODEL_NAME)
    (INDEX_DIR / "corpus.sha").write_text(corpus_hash(rows))
    print(f"saved {vecs.shape} to {INDEX_DIR}")


class Index:
    """Loaded once per process: normalized vectors plus each paragraph's filter columns."""

    def __init__(self, con) -> None:
        ids_path, vec_path = INDEX_DIR / "ids.txt", INDEX_DIR / "embeddings.npy"
        if not ids_path.exists() or not vec_path.exists():
            raise FileNotFoundError("no search index; run: uv run python -m sec_filings_analyst_agent.filing_search --build")
        if (INDEX_DIR / "model.txt").read_text().strip() != MODEL_NAME:
            raise RuntimeError("the stored index was built with a different model; rebuild it")
        sha_path = INDEX_DIR / "corpus.sha"
        if not sha_path.exists() or sha_path.read_text().strip() != corpus_hash(_corpus_rows(con)):
            raise RuntimeError("the stored index was built from different paragraph text; rebuild it")
        self.ids = ids_path.read_text().split("\n")
        self.vecs = np.load(vec_path)
        meta = {r[0]: r[1:] for r in con.execute(
            "SELECT paragraph_id, ticker, fiscal_year, item FROM filing_paragraphs").fetchall()}
        if set(meta) != set(self.ids):
            raise RuntimeError("the stored index does not match the paragraphs in the warehouse; rebuild it")
        self.ticker = np.array([meta[i][0] for i in self.ids])
        self.year = np.array([meta[i][1] for i in self.ids])
        self.item = np.array([meta[i][2] for i in self.ids])
        self._model = None

    def embed_query(self, query: str) -> np.ndarray:
        if self._model is None:
            self._model = _model()
        v = np.asarray(next(iter(self._model.query_embed([query]))), dtype=np.float32)
        return v / np.linalg.norm(v)


_INDEX: Index | None = None


def _index(con) -> Index:
    global _INDEX
    if _INDEX is None:
        _INDEX = Index(con)
    return _INDEX


# ----------------------------------------------------------------------------- records
_CIKS: dict[str, int] = {}


def _cik(con, ticker: str) -> int:
    if not _CIKS:
        _CIKS.update(dict(con.execute("SELECT DISTINCT ticker, cik FROM clean_facts").fetchall()))
    return _CIKS[ticker]


def _record(con, row: tuple, cut: bool, score: float | None = None) -> dict:
    pid, ticker, fy, accn, item, idx, heading, is_table, text = row
    full = len(text)
    shown = text[:MAX_CHARS] if cut else text
    rec = {
        "paragraph_id": pid, "ticker": ticker, "fiscal_year": fy, "item": item, "section": ITEMS.get(item),
        "heading": heading, "is_table": bool(is_table), "text": shown, "truncated": len(shown) < full,
        "source_url": filing_url(_cik(con, ticker), accn),
        "form": "10-K",
    }
    if rec["truncated"]:
        rec["full_length_chars"] = full
        rec["hint"] = "Text was cut. Call get_paragraph for the full paragraph before quoting from the end of it."
    if score is not None:
        rec["score"] = round(float(score), 4)
    return rec


_COLS = "paragraph_id, ticker, fiscal_year, accn, item, paragraph_index, heading, is_table, text"


def search_filing(query: str, ticker: str | None = None, fiscal_year: int | None = None, item: str | None = None,
                  k: int = DEFAULT_K, con=None) -> dict:
    """Paragraphs of the 10-Ks most similar in meaning to `query`, best first."""
    if not isinstance(query, str) or not query.strip():
        return {"error": "give a non-empty query"}
    query_truncated = len(query) > MAX_QUERY_CHARS
    query = query[:MAX_QUERY_CHARS]
    if ticker is not None and ticker not in TICKERS:
        return {"error": f"unknown ticker {ticker!r}", "valid_tickers": list(TICKERS)}
    if item is not None and item not in ITEMS:
        return {"error": f"unknown item {item!r}", "valid_items": ITEMS}
    if fiscal_year is not None and (not isinstance(fiscal_year, int) or isinstance(fiscal_year, bool)):
        return {"error": "fiscal_year must be an integer"}
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= MAX_K:
        return {"error": f"k must be between 1 and {MAX_K}"}
    own = con is None
    con = con or connect()
    try:
        ix = _index(con)
        mask = np.ones(len(ix.ids), dtype=bool)
        if ticker:
            mask &= ix.ticker == ticker
        if fiscal_year is not None:
            mask &= ix.year == fiscal_year
        if item:
            mask &= ix.item == item
        pool = np.flatnonzero(mask)
        if len(pool) == 0:
            return {"results": [], "missing_reason": "No 10-K paragraphs match those filters (filings cover FY2018 on, "
                                                     "Items 1, 1A and 7 of AMD, INTC, MSFT and NVDA)."}
        scores = ix.vecs[pool] @ ix.embed_query(query)
        # stable order: highest score first, ties broken by paragraph id so results are repeatable
        order = sorted(range(len(pool)), key=lambda j: (-float(scores[j]), ix.ids[pool[j]]))[:k]
        out = []
        for j in order:
            r = con.execute(f"SELECT {_COLS} FROM filing_paragraphs WHERE paragraph_id = ?", [ix.ids[pool[j]]]).fetchone()
            out.append(_record(con, r, cut=True, score=scores[j]))
        res = {"results": out, "searched_paragraphs": int(len(pool)),
               "note": "Ranked by similarity in meaning, not by whether the paragraph answers the question. Read the text."}
        if query_truncated:
            res["query_truncated"] = True
            res["query_note"] = (f"Only the first {MAX_QUERY_CHARS} characters of the query were used. Use one focused "
                                 "question per search, and search again for other topics.")
        return res
    finally:
        if own:
            con.close()


def get_paragraph(paragraph_id: str, neighbors: int = 0, con=None) -> dict:
    """One paragraph in full, optionally with up to 2 paragraphs before and after it from the same section."""
    if not isinstance(paragraph_id, str) or not _PARAGRAPH_ID.match(paragraph_id):
        return {"error": "paragraph_id looks like '0001045810-26-000021:7:0042' (use an id returned by search_filing)"}
    if not isinstance(neighbors, int) or isinstance(neighbors, bool) or not 0 <= neighbors <= MAX_NEIGHBORS:
        return {"error": f"neighbors must be between 0 and {MAX_NEIGHBORS}"}
    own = con is None
    con = con or connect()
    try:
        r = con.execute(f"SELECT {_COLS} FROM filing_paragraphs WHERE paragraph_id = ?", [paragraph_id]).fetchone()
        if r is None:
            return {"error": f"no paragraph with id {paragraph_id!r}"}
        out = {"paragraph": _record(con, r, cut=False)}
        if neighbors:
            accn, item, idx = r[3], r[4], r[5]
            rows = con.execute(
                f"SELECT {_COLS} FROM filing_paragraphs WHERE accn = ? AND item = ? AND paragraph_index BETWEEN ? AND ? "
                "AND paragraph_id <> ? ORDER BY paragraph_index", [accn, item, idx - neighbors, idx + neighbors, paragraph_id]).fetchall()
            out["neighbors"] = [_record(con, x, cut=False) for x in rows]
        return out
    finally:
        if own:
            con.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", action="store_true")
    if not p.parse_args().build:
        p.error("pass --build")
    build_index()


if __name__ == "__main__":
    main()
