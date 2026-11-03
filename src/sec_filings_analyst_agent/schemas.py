"""The structured answer format the baseline (and later the agent) must return.

Why structured: the deterministic checks need to see every figure with its unit, scale and fiscal
period without parsing free prose. The field descriptions below are sent to the model as part of
the schema, so both prompt variants see exactly the same definition of the format.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Figure(BaseModel):
    company: str = Field(description="Ticker of the company the figure belongs to, e.g. AMD.")
    metric: str = Field(description="What the figure is, e.g. 'research and development expense'.")
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
        description="A short passage copied from the documents that supports the figure. "
        "Empty string if the figure was calculated rather than quoted."
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
