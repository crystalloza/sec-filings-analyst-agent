"""Quote verification (decision 4): asymmetric, strict where a mistake would hide a fabrication.

VERIFIED tier: the quote is an exact substring of the documents the model was shown, after cleaning
our own storage formatting (table pipes, bullets, heading markers) from both sides, and it holds at
least three words of context besides numbers (a bare number proves nothing).
NEAR tier (reported separately, never counted as verified): the words are about 90% similar to a
passage in the source, but EVERY number in the quote appears exactly in that passage. That closes
the loophole where a real sentence is quoted with one swapped number.
Both sides use the shared normalize_text().
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from sec_filings_analyst_agent.text import normalize_text

MIN_CONTEXT_WORDS = 3
NEAR_COVERAGE = 0.90
_NUMBER_TOKEN = re.compile(r"^\$?\(?-?\d[\d,]*(\.\d+)?%?\)?$")


def canonicalize(text: str) -> str:
    """normalize_text plus removal of the formatting our own storage adds."""
    t = normalize_text(text)
    t = t.replace("[TABLE]", " ")
    t = re.sub(r"(^|\s)#{1,6}\s", " ", t)   # markdown heading markers
    t = t.replace("|", " ").replace("•", " ")
    return re.sub(r"\s+", " ", t).strip()


def _number_value(token: str) -> str | None:
    """Normalized text of a numeric token ('$5,872' -> '5872'), or None if it is not numeric."""
    t = token.strip(".,;:()")
    if not _NUMBER_TOKEN.match(token.strip(".,;:")) and not re.match(r"^\$?\d", t):
        return None
    cleaned = re.sub(r"[$,%()]", "", t)
    return cleaned if re.match(r"^-?\d+(\.\d+)?$", cleaned) else None


def numbers_in(text: str) -> list[str]:
    return [n for n in (_number_value(tok) for tok in text.split()) if n is not None]


def context_words(text: str) -> int:
    """Words that are not numbers."""
    return sum(1 for tok in text.split() if re.search(r"[A-Za-z]", tok))


@dataclass
class QuoteResult:
    status: str  # verified | near | unverified | too_short | empty
    similarity: float  # best token coverage found (1.0 for a verbatim match)
    detail: str = ""


def _fragments(quote_canon: str) -> list[str]:
    """Quotes sometimes skip text with an ellipsis; each fragment must appear, in order."""
    parts = re.split(r"\s*(?:\.\.\.|…)\s*", quote_canon)
    return [p.strip() for p in parts if p.strip()]


def _find_bounded(fragment: str, source: str, start: int) -> int:
    """Index of `fragment` in `source` that does not cut a word or a number in half, else -1.

    "5,87" must not match inside "5,872", and "872" must not match inside "5,872".
    """
    i = source.find(fragment, start)
    while i >= 0:
        end = i + len(fragment)
        before = source[i - 1] if i > 0 else " "
        after = source[end] if end < len(source) else " "
        before_before = source[i - 2] if i > 1 else " "
        after_after = source[end + 1] if end + 1 < len(source) else " "
        cuts_left = fragment[0].isalnum() and (before.isalnum() or (before in ",." and before_before.isdigit()))
        cuts_right = fragment[-1].isalnum() and (after.isalnum() or (after in ",." and after_after.isdigit()))
        if not cuts_left and not cuts_right:
            return i
        i = source.find(fragment, i + 1)
    return -1


def _verbatim_in_order(fragments: list[str], source: str) -> bool:
    position = 0
    for frag in fragments:
        found = _find_bounded(frag, source, position)
        if found < 0:
            return False
        position = found + len(frag)
    return True


def _best_coverage(quote_tokens: list[str], source_tokens: list[str]) -> tuple[float, bool]:
    """Best fraction of the quote's tokens found in order in a nearby source window, and whether every
    number of the quote is present in that window.

    Every number in the quote is tried as an anchor, so a quote with one swapped number is still
    located by its other numbers (and reported as "similar wording but a number differs").
    """
    quote_numbers = [n for n in (_number_value(t) for t in quote_tokens) if n is not None]
    if not quote_numbers:
        return 0.0, False
    span = len(quote_tokens)
    best, best_numbers_ok = 0.0, False
    for anchor in dict.fromkeys(quote_numbers):  # unique, in order
        positions = [i for i, t in enumerate(source_tokens) if _number_value(t) == anchor]
        for pos in positions[:50]:
            lo, hi = max(0, pos - span), min(len(source_tokens), pos + span + 1)
            window = source_tokens[lo:hi]
            matcher = SequenceMatcher(None, quote_tokens, window, autojunk=False)
            covered = sum(block.size for block in matcher.get_matching_blocks()) / len(quote_tokens)
            window_numbers = [n for n in (_number_value(t) for t in window) if n is not None]
            numbers_ok = all(n in window_numbers for n in quote_numbers)
            if covered > best or (covered == best and numbers_ok and not best_numbers_ok):
                best, best_numbers_ok = covered, numbers_ok
    return best, best_numbers_ok


def check_quote(quote: str, source_canonical: str, source_tokens: list[str] | None = None) -> QuoteResult:
    """Classify a source_quote against the canonicalized documents the model was shown."""
    if not quote or not quote.strip():
        return QuoteResult("empty", 0.0)
    q = canonicalize(quote)
    if context_words(q) < MIN_CONTEXT_WORDS:
        return QuoteResult("too_short", 0.0, "fewer than 3 words of context besides numbers")
    if _verbatim_in_order(_fragments(q), source_canonical):
        return QuoteResult("verified", 1.0)
    tokens = source_tokens if source_tokens is not None else source_canonical.split()
    coverage, numbers_ok = _best_coverage(q.split(), tokens)
    if coverage >= NEAR_COVERAGE and numbers_ok:
        return QuoteResult("near", round(coverage, 3), "words differ slightly; all numbers match exactly")
    detail = "similar wording but a number differs" if coverage >= NEAR_COVERAGE else ""
    return QuoteResult("unverified", round(coverage, 3), detail)
