"""The one place that talks to a model provider.

Everything else in the project calls complete() and gets back a plain LLMResult. Swapping the
provider (direct API, Bedrock, Vertex) or the SDK version should only touch this file.

Today only the direct Anthropic API is implemented, because it is the one route that can be
exercised here. The seam is the MODELS table and make_client(): another route is one more branch.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()  # reads ANTHROPIC_API_KEY from the gitignored .env; never printed or logged

# Our names for models -> each provider's model ID.
MODELS = {
    "dev": {"anthropic": "claude-haiku-5-5"},        # cheap, for prompt iteration
    "measured": {"anthropic": "claude-sonnet-5-5"},  # the model reported in results
    "judge": {"anthropic": "claude-opus-5-5"},       # grades answers; a different tier from the answerer
}

# Reasoning effort per alias. These are the models' own defaults, written down so runs are reproducible
# and the same setting can be reused for the agent.
EFFORT = {"dev": "medium", "measured": "high", "judge": "high"}

# (input, output) USD per million tokens. Haiku's price rises for prompts over 100K tokens.
PRICES = {
    "claude-haiku-5-5": {"standard": (0.10, 0.50), "long": (0.50, 2.50)},
    "claude-sonnet-5-5": {"standard": (2.00, 10.00)},
    "claude-opus-5-5": {"standard": (4.00, 20.00)},
}
LONG_PROMPT_TOKENS = 100_000

# Models that support the server-side refusal fallback on the direct API.
SERVER_FALLBACK_MODELS = {"claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class LLMResult:
    parsed: Optional[BaseModel]  # the validated structured answer
    raw_text: str
    stop_reason: str
    model: str  # the model that actually served the request
    requested_model: str
    provider: str
    effort: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    request_id: str


@lru_cache(maxsize=None)
def make_client(provider: str):
    if provider == "anthropic":
        return anthropic.Anthropic()  # credentials come from the environment
    raise NotImplementedError(
        f"Provider {provider!r} is not implemented. Add a branch here (and entries in MODELS)."
    )


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = PRICES[model]
    tier = "long" if ("long" in prices and input_tokens > LONG_PROMPT_TOKENS) else "standard"
    in_price, out_price = prices[tier]
    return input_tokens * in_price / 1e6 + output_tokens * out_price / 1e6


def complete(
    alias: str,
    system: str,
    user: Optional[str],
    output_model: type[BaseModel],
    max_tokens: int = 16000,
    messages: Optional[list[dict]] = None,
) -> LLMResult:
    """One model call that must return `output_model`. `alias` is 'dev', 'measured' or 'judge'.

    Pass either `user` (a single user turn) or `messages` (a full conversation; the API is
    stateless, so a multi-turn exchange resends the whole history every call).
    """
    if messages is None:
        messages = [{"role": "user", "content": user}]
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    model = MODELS[alias][provider]
    client = make_client(provider)

    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        output_format=output_model,
        output_config={"effort": EFFORT[alias]},
    )
    start = time.time()
    if provider == "anthropic" and model in SERVER_FALLBACK_MODELS:
        response = client.beta.messages.parse(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
    else:
        response = client.messages.parse(**kwargs)
    latency = time.time() - start

    raw_text = next((b.text for b in response.content if b.type == "text"), "")
    usage = response.usage
    return LLMResult(
        parsed=response.parsed_output,
        raw_text=raw_text,
        stop_reason=response.stop_reason,
        model=response.model,
        requested_model=model,
        provider=provider,
        effort=EFFORT[alias],
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=estimate_cost(model, usage.input_tokens, usage.output_tokens),
        latency_s=round(latency, 2),
        request_id=response._request_id,
    )
