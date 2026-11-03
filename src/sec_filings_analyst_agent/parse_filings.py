"""Parse the downloaded 10-K HTML into paragraph rows for Items 1, 1A and 7.

Output (both derived, disposable, gitignored; rebuild any time from the HTML):
  data/raw/filing_paragraphs.parquet  one row per paragraph (or table) with provenance
  data/raw/filing_manifest.parquet    one row per filing + item: how much text was found

Approach (timeboxed, heuristic on purpose): flatten the HTML into a sequence of text blocks,
find the "Item N" headings, and keep the blocks between a wanted heading and the next Item
heading. The table of contents also contains "Item 7" etc., so for each item the candidate
heading followed by the LONGEST run of text wins. Quality is measured, not assumed: the manifest
flags any filing/item that came out suspiciously short.

Run with:  uv run python -m sec_filings_analyst_agent.parse_filings
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from bs4 import BeautifulSoup, Tag

from sec_filings_analyst_agent.fetch import RAW_DIR
from sec_filings_analyst_agent.fetch_filings import FILINGS_DIR
from sec_filings_analyst_agent.text import normalize_text

# Items kept, with a human title and the minimum word count below which the manifest says "check".
ITEMS = {
    "1": ("Business", 1500),
    "1A": ("Risk Factors", 1500),
    "7": ("Management's Discussion and Analysis", 1500),
}

BLOCK_TAGS = ["p", "div", "table", "li", "h1", "h2", "h3", "h4", "h5", "h6"]
ITEM_HEADING = re.compile(r"^item\s+(\d{1,2}[abc]?)\b", re.IGNORECASE)
PAGE_NOISE = re.compile(
    r"^(table of contents|\d{1,3}|page \d+( of \d+)?|part [iv]+|item \d{1,2}[abc]?\.?)$", re.IGNORECASE
)
BOLD_STYLE = re.compile(r"font-weight:\s*(bold|[6-9]00)", re.IGNORECASE)
PAGE_FOOTER = re.compile(r"^[^.\d]{2,60} \d{1,3}$")  # e.g. "MD&A 30", "Risk Factors and Other Key Information 46"
MAX_HEADING_WORDS = 15
MAX_ITEM_HEADING_CHARS = 250

# Fallback for filings with no "Item N" headings (some companies organise the 10-K by topic and put
# the Item numbers only in a cross-reference index). Sections are found by their TITLES instead:
# a section starts at one of its own titles and ends at the next major title of any other kind.
# This is generic: it is tried for any filing/item whose Item-heading parse failed or was too short.
TITLE_STARTS = {
    "1": [r"business", r"business overview", r"our business", r"our strategy"],
    "1A": [r"risk factors"],
    "7": [r"management.s discussion and analysis( \(md&a\))?( of financial condition and results of operations)?"],
}
OTHER_TITLES = [
    r"risk factors and other key information",
    r"quantitative and qualitative disclosures? about market risk",
    r"financial statements( and supplemental details| and supplementary data)?",
    r"properties", r"legal proceedings", r"unresolved staff comments", r"cybersecurity",
    r"controls and procedures", r"selected financial data", r"executive compensation",
    r"market for (the )?registrant.s common equity.*", r"signatures",
]
TITLE_RULES = [(item, re.compile(p)) for item, pats in TITLE_STARTS.items() for p in pats] + [
    ("other", re.compile(p)) for p in OTHER_TITLES
]
# A bare "Overview" is too common to be a section start on its own (MD&A has many). It counts as
# the start of an item only when another of that item's start titles follows within a few blocks.
CONDITIONAL_STARTS = {"1": re.compile(r"overview")}
CONDITIONAL_WINDOW = 5
MAX_TITLE_WORDS = 12

PARAGRAPH_SCHEMA = pa.schema(
    [
        ("accn", pa.string()),
        ("paragraph_id", pa.string()),  # {accn}:{item}:{nnnn}, stable across rebuilds
        ("item", pa.string()),
        ("paragraph_index", pa.int32()),
        ("heading", pa.string()),  # nearest sub-heading above, to keep context
        ("is_table", pa.bool_()),
        ("text", pa.string()),  # normalize_text() applied
        ("word_count", pa.int32()),
    ]
)
MANIFEST_SCHEMA = pa.schema(
    [
        ("accn", pa.string()),
        ("ticker", pa.string()),
        ("source_file", pa.string()),
        ("report_date", pa.date32()),
        ("item", pa.string()),
        ("item_title", pa.string()),
        ("paragraphs", pa.int32()),
        ("tables", pa.int32()),
        ("words", pa.int32()),
        ("method", pa.string()),  # item_heading | title_fallback | none
        ("status", pa.string()),  # ok | check_short | missing
    ]
)


# --------------------------------------------------------------------------- HTML -> blocks
def _is_bold(element: Tag) -> bool:
    """True if every non-empty piece of text in the element is bold."""
    saw_text = False
    for node in element.find_all(string=True):
        if not node.strip():
            continue
        saw_text = True
        bold = False
        for ancestor in node.parents:
            if ancestor.name in ("b", "strong") or BOLD_STYLE.search(ancestor.get("style", "") or ""):
                bold = True
                break
            if ancestor is element:
                break
        if not bold:
            return False
    return saw_text


def _table_text(table: Tag) -> str:
    """Rows on separate lines, cells joined with ' | '. A table with only ONE non-empty row is a
    layout table (a bullet and its text, or 'ITEM 7.' and its title), so its cells are joined
    with a space instead and it reads as ordinary text."""
    rows = []
    for row in table.find_all("tr"):
        cells = [normalize_text(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
    if len(rows) == 1:
        return " ".join(rows[0])
    return "\n".join(" | ".join(cells) for cells in rows)


def html_to_blocks(html: bytes) -> list[dict]:
    """Flatten the document into text blocks in reading order: {text, is_table, is_heading}."""
    soup = BeautifulSoup(html, "lxml")
    for junk in soup(["script", "style", "ix:header"]):
        junk.decompose()
    for hidden in soup.find_all(style=re.compile(r"display:\s*none", re.IGNORECASE)):
        hidden.decompose()

    blocks: list[dict] = []

    def walk(element: Tag) -> None:
        for child in element.children:
            if not isinstance(child, Tag):
                continue
            if child.name == "table":
                text = _table_text(child)
                if not text:
                    continue
                # A table with a single non-empty row is a layout table holding one line of
                # text (for example "ITEM 7. | MANAGEMENT'S DISCUSSION...") - treat it as text.
                is_data_table = "\n" in text
                blocks.append({"text": text, "is_table": is_data_table, "is_heading": False})
            elif child.find(BLOCK_TAGS):
                walk(child)
            else:
                text = normalize_text(child.get_text(" ", strip=True))
                if text:
                    short = len(text.split()) <= MAX_HEADING_WORDS
                    blocks.append(
                        {"text": text, "is_table": False, "is_heading": short and _is_bold(child)}
                    )

    walk(soup.body or soup)
    return blocks


# --------------------------------------------------------------------------- blocks -> sections
def find_sections(blocks: list[dict]) -> dict[str, tuple[int, int]]:
    """For each wanted item, the (start, end) block range after its best heading.

    Heading candidates are short non-table blocks starting with "Item N". A section runs until
    the next heading candidate with a different item label. The table of contents yields tiny
    sections and cross-references are filtered by the length limit, so the longest span wins.
    """
    candidates = [
        (i, ITEM_HEADING.match(b["text"]).group(1).upper())
        for i, b in enumerate(blocks)
        if not b["is_table"]
        and len(b["text"]) <= MAX_ITEM_HEADING_CHARS
        and ITEM_HEADING.match(b["text"])
    ]
    sections: dict[str, tuple[int, int]] = {}
    best_words: dict[str, int] = {}
    for pos, (start, label) in enumerate(candidates):
        if label not in ITEMS:
            continue
        end = len(blocks)
        for next_start, next_label in candidates[pos + 1:]:
            if next_label != label:
                end = next_start
                break
        words = sum(len(b["text"].split()) for b in blocks[start + 1:end])
        if words > best_words.get(label, -1):
            best_words[label] = words
            sections[label] = (start, end)
    return sections


def find_sections_by_title(blocks: list[dict]) -> dict[str, tuple[int, int]]:
    """Fallback finder: sections located by their titles instead of 'Item N' headings."""
    hits = []
    for i, b in enumerate(blocks):
        if b["is_table"] or len(b["text"].split()) > MAX_TITLE_WORDS:
            continue
        title = re.sub(r"[\s:.]+$", "", b["text"].lower())
        for key, rule in TITLE_RULES:
            if rule.fullmatch(title):
                hits.append((i, key))
                break
    # Conditional starts (a bare "Overview") are kept only if a real start title for the same
    # item follows closely.
    for item, rule in CONDITIONAL_STARTS.items():
        for i, b in enumerate(blocks):
            if b["is_table"] or not rule.fullmatch(re.sub(r"[\s:.]+$", "", b["text"].lower())):
                continue
            if any(k == item and i < j <= i + CONDITIONAL_WINDOW for j, k in hits):
                hits.append((i, item))
    hits.sort()
    sections: dict[str, tuple[int, int]] = {}
    best_words: dict[str, int] = {}
    for pos, (start, key) in enumerate(hits):
        if key not in ITEMS:
            continue
        end = next((nxt for nxt, nkey in hits[pos + 1:] if nkey != key), len(blocks))
        words = sum(len(b["text"].split()) for b in blocks[start + 1:end])
        if words > best_words.get(key, -1):  # the table of contents gives tiny spans; longest wins
            best_words[key] = words
            sections[key] = (start, end)
    return sections


def section_paragraphs(blocks: list[dict], start: int, end: int, item: str) -> list[dict]:
    """Paragraph rows for one section, each tagged with the nearest heading above it."""
    heading = ITEMS[item][0]
    rows = []
    body = blocks[start + 1:end]
    # Some filings do not bold their sub-headings, so bold detection finds none. Only then, fall
    # back to the shape of a heading: a short line with no closing punctuation, followed by a
    # real paragraph. (Never used when bold headings exist, so those filings are unaffected.)
    # "Table of Contents" page links are bold too, so ignore page noise when deciding.
    shape_headings = not any(b["is_heading"] and not PAGE_NOISE.match(b["text"]) for b in body)

    def looks_like_heading(i: int) -> bool:
        b = body[i]
        words = len(b["text"].split())
        return (
            not b["is_table"] and 1 <= words <= 8 and b["text"][0].isupper()
            and not b["text"].endswith((".", ";", ":", ",", ")"))
            and i + 1 < len(body) and len(body[i + 1]["text"].split()) >= 15
        )

    # blocks[start] is the "Item N" line itself; the next short bold block is usually its title.
    for i, block in enumerate(body):
        text = block["text"]
        if PAGE_NOISE.match(text) or (len(text.split()) <= 8 and PAGE_FOOTER.match(text)):
            continue
        if (block["is_heading"] or (shape_headings and looks_like_heading(i))) and not block["is_table"]:
            heading = text
            continue
        rows.append(
            {"heading": heading, "is_table": block["is_table"], "text": text,
             "word_count": len(text.split())}
        )
    return rows


# --------------------------------------------------------------------------- driver
def parse_filing(path: Path, filing: dict) -> tuple[list[dict], list[dict]]:
    blocks = html_to_blocks(path.read_bytes())
    sections = find_sections(blocks)
    by_title = None  # computed only if some item needs the fallback
    paragraphs, manifest = [], []
    for item, (title, minimum) in ITEMS.items():
        rows, method = [], "none"
        if item in sections:
            rows, method = section_paragraphs(blocks, *sections[item], item), "item_heading"
        if sum(r["word_count"] for r in rows) < minimum:
            if by_title is None:
                by_title = find_sections_by_title(blocks)
            if item in by_title:
                alt = section_paragraphs(blocks, *by_title[item], item)
                if sum(r["word_count"] for r in alt) >= minimum:
                    rows, method = alt, "title_fallback"
        for n, row in enumerate(rows, start=1):
            paragraphs.append(
                {"accn": filing["accn"], "paragraph_id": f"{filing['accn']}:{item}:{n:04d}",
                 "item": item, "paragraph_index": n, **row}
            )
        words = sum(r["word_count"] for r in rows)
        status = "missing" if not rows else ("check_short" if words < minimum else "ok")
        manifest.append(
            {"accn": filing["accn"], "ticker": filing["ticker"], "source_file": path.name,
             "report_date": filing["report_date"], "item": item, "item_title": title,
             "paragraphs": len(rows), "tables": sum(r["is_table"] for r in rows),
             "words": words, "method": method, "status": status}
        )
    return paragraphs, manifest


def filings_on_disk() -> list[tuple[Path, dict]]:
    """Pair each downloaded HTML with its ticker, accession number and report date."""
    found = []
    for path in sorted(FILINGS_DIR.glob("*.htm")):
        ticker, accn = path.stem.split("_", 1)
        found.append((path, {"ticker": ticker, "accn": accn}))
    return found


def report_dates() -> dict[str, date]:
    """accession number -> report date, read back from the saved submissions JSON."""
    import json

    dates = {}
    for sub in RAW_DIR.glob("submissions_*.json"):
        doc = json.loads(sub.read_text())
        recent = doc["filings"]["recent"] if "filings" in doc else doc
        for accn, report in zip(recent["accessionNumber"], recent["reportDate"]):
            if report:
                dates[accn] = date.fromisoformat(report)
    return dates


def parse_all() -> None:
    dates = report_dates()
    all_paragraphs, all_manifest = [], []
    for path, filing in filings_on_disk():
        filing["report_date"] = dates[filing["accn"]]
        paragraphs, manifest = parse_filing(path, filing)
        all_paragraphs += paragraphs
        all_manifest += manifest
        summary = ", ".join(f"{m['item']}:{m['words']}w/{m['status']}/{m['method']}" for m in manifest)
        print(f"{filing['ticker']} {filing['report_date']}  {summary}")
    pq.write_table(pa.Table.from_pylist(all_paragraphs, schema=PARAGRAPH_SCHEMA),
                   RAW_DIR / "filing_paragraphs.parquet")
    pq.write_table(pa.Table.from_pylist(all_manifest, schema=MANIFEST_SCHEMA),
                   RAW_DIR / "filing_manifest.parquet")
    print(f"wrote {len(all_paragraphs):,} paragraph rows for {len(all_manifest)} filing-items")


if __name__ == "__main__":
    parse_all()
