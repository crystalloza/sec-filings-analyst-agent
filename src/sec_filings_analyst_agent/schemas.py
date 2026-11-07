"""The structured answer format the baseline (and later the agent) must return.

Why structured: the deterministic checks need to see every figure with its unit, scale and fiscal
period without parsing free prose. The field descriptions below are sent to the model as part of
the schema, so both prompt variants see exactly the same definition of the format.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# The fixed vocabulary the checks use to know what a figure claims to be (decision: model self-labels).
Concept = Literal[
    "revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income",
    "research_and_development", "eps_diluted", "total_assets", "total_liabilities",
    "stockholders_equity", "cash_and_equivalents", "inventory",
    "gross_margin", "operating_margin", "net_margin", "rnd_pct_revenue", "revenue_growth",
    "change_amount", "change_percent", "other",
]


class Figure(BaseModel):
    company: str = Field(description="Ticker of the company the figure belongs to, e.g. AMD.")
    metric: str = Field(description="What the figure is, e.g. 'research and development expense'.")
    concept: Concept = Field(
        description="The closest concept from the list. Margins and ratios are percentages (unit "
        "percent): gross_margin is the PERCENTAGE, while gross profit in dollars is gross_profit. "
        "Use change_amount or change_percent for differences you calculated, revenue_growth for "
        "year-over-year revenue growth, and other for anything else (segment figures, drivers, etc.)."
    )
    scope: Literal["company_total", "segment_or_product", "non_gaap", "other"] = Field(
        description="What the figure measures. company_total: a GAAP figure for the whole company. "
        "segment_or_product: a business segment, product line, region or other part of the company "
        "(for example Intelligent Cloud revenue or Azure growth). non_gaap: any adjusted or non-GAAP "
        "measure. other: anything else (counts, dates, drivers). The concept field names the closest "
        "concept either way."
    )
    value: float = Field(
        description="The number exactly as stated in the unit and scale given below "
        "(for example 5.9 with scale 'billions', or 75.0 with unit 'percent')."
    )
    unit: Literal["USD", "USD_per_share", "percent", "shares", "other"]
    scale: Literal["units", "thousands", "millions", "billions"] = Field(
        description="The scale of `value`. Use 'units' for percentages and per-share amounts."
    )
    fiscal_year: int = Field(description="The company's fiscal year the figure belongs to.")
    fiscal_period: Literal["FY", "Q1", "Q2", "Q3", "Q4", "other"] = Field(
        description="FY for a full fiscal year, Q1-Q4 for a single quarter, other if unclear."
    )
    source_quote: str = Field(
        description="A short passage copied exactly from the documents that supports the figure, "
        "and that includes the figure's own number as written there (for example the full table row "
        "or the full sentence). Empty string if the figure was calculated rather than quoted."
    )
    is_computed: bool = Field(
        description="True if you calculated this figure from other figures instead of reading it."
    )


class Answer(BaseModel):
    answer: str = Field(description="The written answer to the question.")
    figures: list[Figure] = Field(
        description="Every number stated in `answer`, one entry each. Empty if none."
    )
    declined: bool = Field(
        description="True if the documents do not contain what is needed to answer."
    )
    missing_info: Optional[str] = Field(
        description="If declined, what is missing from the documents. Otherwise null."
    )


# ----------------------------------------------------------------------------- the agent's answer format
# Separate classes: the frozen baseline schema above (and its prompt hashes) must not change.
class AgentFigure(Figure):
    source_quote: str = Field(
        description="For source_type quote: a short passage copied exactly from a sec-filings result, including the "
        "figure's own number as written there. Empty string otherwise."
    )
    source_type: Literal["quote", "record", "computed", "background"] = Field(
        description="Where the figure comes from. record: a fact record returned by a sec-facts tool (cite its id). "
        "quote: copied word for word from a sec-filings result (cite its paragraph_id and give source_quote). "
        "computed: calculated by you from records (list every record id used). "
        "background: from your general knowledge, NOT from the tools (say so in the answer)."
    )
    source_ids: list[str] = Field(
        description="Record ids (like NVDA:revenue:FY2025:FY:10-K) and/or paragraph ids returned by the tools that "
        "support this figure. Empty only for background figures."
    )
    source_url: Optional[str] = Field(
        description="The EDGAR filing link for the figure, copied exactly: one entry of the record's source_urls, or "
        "the paragraph's source_url. Null for background figures."
    )


class AgentAnswer(BaseModel):
    answer: str = Field(description="The written answer to the question.")
    figures: list[AgentFigure] = Field(
        default_factory=list,
        description="Every number stated in `answer`, one entry each. Empty if none."
    )
    declined: bool = Field(description="True if the tools do not provide what is needed to answer.")
    missing_info: Optional[str] = Field(
        default=None,
        description="If declined, or if part of the question could not be answered, what is missing. Otherwise null."
    )
    uses_background_knowledge: bool = Field(
        description="True if any part of the answer comes from general knowledge instead of the tools. "
        "If true, the answer text must say so explicitly."
    )
