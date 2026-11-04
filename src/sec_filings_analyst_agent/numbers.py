"""Number rules for the checks: the rounding-consistency rule, scale diagnostics, prose scanning.

The rule (decision 1): a stated figure matches a reference if it equals the reference ROUNDED to the
precision the model stated, i.e. |value - reference| <= half a unit of the last decimal the model
wrote. "5.9 billion" matches 5.872 billion; "5.8" and "5.88" do not. The sign must match.
Everything is compared in the figure's OWN stated units (billions, percent, ...) so the tolerance
means what the model meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

SCALE_MULT = {"units": 1.0, "thousands": 1e3, "millions": 1e6, "billions": 1e9}
EPS = 1e-9
# A figure that fails can still match after moving the decimal point: record WHICH slip it was.
SCALE_SLIPS = (1e3, 1e6, 1e-3, 1e-6, 100.0, 0.01)
COARSE_RELATIVE_TOLERANCE = 0.01  # a tolerance above 1% of the reference marks the figure low-precision


def decimals_of(value: float) -> int:
    """Decimal places the model wrote. Integer-valued floats (5900.0) count as whole numbers."""
    d = Decimal(repr(float(value)))
    exponent = d.as_tuple().exponent
    decimals = -exponent if exponent < 0 else 0
    if decimals == 1 and repr(float(value)).endswith(".0"):
        decimals = 0
    return decimals


def half_unit(value: float) -> float:
    return 0.5 * 10 ** (-decimals_of(value))


def to_stated_space(canonical: float, unit: str, scale: str) -> float:
    """Express a canonical reference (USD, per-share dollars, or a fraction) in the figure's units."""
    if unit == "percent":
        return canonical * 100.0
    return canonical / SCALE_MULT[scale]


def to_canonical(value: float, unit: str, scale: str) -> float:
    if unit == "percent":
        return value / 100.0
    return value * SCALE_MULT[scale]


def rounds_to(value: float, reference_in_stated_space: float, factor: float = 1.0) -> bool:
    """The rounding-consistency rule, with an optional decimal-point slip `factor` applied to value."""
    shifted = value * factor
    if shifted != 0 and reference_in_stated_space != 0 and (shifted < 0) != (reference_in_stated_space < 0):
        return False
    tolerance = half_unit(value) * abs(factor) + EPS * max(1.0, abs(shifted))
    return abs(shifted - reference_in_stated_space) <= tolerance


def is_low_precision(value: float, reference_in_stated_space: float) -> bool:
    if reference_in_stated_space == 0:
        return False
    return half_unit(value) / abs(reference_in_stated_space) > COARSE_RELATIVE_TOLERANCE


def scale_slip(value: float, reference_in_stated_space: float) -> float | None:
    """If the value only matches after a decimal-point slip, return the slip factor."""
    for factor in SCALE_SLIPS:
        if rounds_to(value, reference_in_stated_space, factor):
            return factor
    return None


# ----------------------------------------------------------------------------- prose scanning
_NUMBER = re.compile(
    r"(?P<open>\()?(?P<cur>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<tail>\s?(?:%|percent\b|trillion\b|billion\b|million\b|thousand\b|bn\b))?",
    re.IGNORECASE,
)
_YEAR = re.compile(r"^(19|20)\d\d$")
_CONTEXT_BEFORE = re.compile(
    r"(fiscal(?: year)?|fy|q|item|note|page|section|form|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s*$",
    re.IGNORECASE,
)
_FORM_SUFFIX = re.compile(r"^-?[KQ]\b", re.IGNORECASE)


@dataclass
class ProseNumber:
    raw: str
    value: float  # as written (5.9, 5872, 68)
    decimals: int
    kind: str  # 'percent' | 'usd' | 'plain'
    scale_word: str | None


def prose_numbers(text: str) -> list[ProseNumber]:
    """Numbers stated in prose, ignoring years, fiscal labels (FY2023, Q2) and note references."""
    found = []
    for m in _NUMBER.finditer(text):
        num = m.group("num")
        tail = (m.group("tail") or "").strip().lower()
        has_marker = bool(m.group("cur") or tail or "," in num or "." in num)
        before = text[max(0, m.start() - 12): m.start()]
        if not has_marker:
            if _YEAR.match(num) or _CONTEXT_BEFORE.search(before):
                continue
            if m.start() > 0 and text[m.start() - 1].isalpha():  # Q2, FY23, H1
                continue
            if _FORM_SUFFIX.match(text[m.end(): m.end() + 3]):  # 10-K, 10-Q
                continue
            if tail == "" and int(float(num)) < 10 and "." not in num:
                continue  # tiny bare integers ("two", "3 filings") are counts, not figures
        value = float(num.replace(",", ""))  # prose sign is unreliable ("($2,709 million)" is parenthetical): compare sizes
        kind = "percent" if tail in ("%", "percent") else ("usd" if m.group("cur") else "plain")
        scale_word = tail if tail in ("trillion", "billion", "million", "thousand", "bn") else None
        decimals = len(num.split(".")[1]) if "." in num else 0
        found.append(ProseNumber(m.group(0).strip(), value, decimals, kind, scale_word))
    return found


_PROSE_SCALE = {"trillion": 1e12, "billion": 1e9, "bn": 1e9, "million": 1e6, "thousand": 1e3, None: 1.0}


def _token_candidates(token: ProseNumber, unit: str, scale: str) -> list[tuple[float, float]]:
    """Ways a token could express a figure: (value in the figure's stated units, token half-unit in those units).

    A token without a scale word ("rose to 16,434") could be at any scale, so each is tried.
    A percent figure may also be written without its % sign.
    """
    out: list[tuple[float, float]] = []
    if token.kind == "percent":
        if unit in ("percent", "other"):
            out.append((token.value, 0.5 * 10 ** (-token.decimals)))
        return out
    if unit == "percent":
        out.append((token.value, 0.5 * 10 ** (-token.decimals)))
    multipliers = (_PROSE_SCALE[token.scale_word],) if token.scale_word else (1.0, 1e3, 1e6, 1e9)
    for mult in multipliers:
        stated = to_stated_space(token.value * mult, unit, scale)
        out.append((stated, 0.5 * 10 ** (-token.decimals) * mult / SCALE_MULT[scale]))
    return out


def token_matches_figure(token: ProseNumber, value: float, unit: str, scale: str, lenient: bool = False) -> bool:
    """Does a number written in text correspond to a figure?

    STRICT (default): the figure must be a correct rounding of the token, judged at the FIGURE's
    precision. A token "5,872" supports a figure 5.9 billion, but a bare "6" does not support 5.872 billion.
    LENIENT (used when scanning the model's own prose against its own figures): either side may be the
    rounded one, so prose "$5.9 billion" corresponds to a listed figure 5.872. Signs are ignored (prose
    sign is unreliable); only sizes are compared.
    """
    magnitude = abs(value)
    for stated, token_half in _token_candidates(token, unit, scale):
        if rounds_to(magnitude, stated):
            return True
        if lenient and abs(magnitude - stated) <= token_half + EPS:
            return True
    return False
