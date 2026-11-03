"""Text normalization shared by the filing parser and the quote check.

One function, used on BOTH sides: when text is stored, and later when a quoted passage in an
answer is verified against the source. If the two sides normalized differently, correct quotes
would fail (curly vs straight quotes, non-breaking spaces and dash variants are all common in
10-K HTML). Keep this function small and change it deliberately.
"""

from __future__ import annotations

import re
import unicodedata

# Characters that look alike but compare unequal. Mapped to one plain form.
_REPLACEMENTS = {
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "–": "-", "—": "-", "‒": "-", "−": "-",
}
_TRANSLATION = str.maketrans(_REPLACEMENTS)
_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Unicode-normalize, unify quotes and dashes, and collapse all whitespace to single spaces."""
    text = unicodedata.normalize("NFKC", text)  # also turns non-breaking spaces into spaces
    text = text.translate(_TRANSLATION)
    return _WHITESPACE.sub(" ", text).strip()
