"""The baseline prompts, in two variants, versioned and hashed.

Both variants require the SAME answer format. Only the behavioral instructions differ:
  A (minimal): the task and the format, nothing else.
  B (careful): adds grounding, period-attribution and refusal rules.
The measured runs use frozen prompts: the hash of each variant is recorded in every log line and
checked against eval/prompts.lock.json (see baseline.py), so a prompt cannot be tuned against the
measured questions without it being visible.
"""

from __future__ import annotations

import hashlib
import json

from sec_filings_analyst_agent.schemas import Answer

PROMPT_VERSION = "baseline-v1"

_FORMAT_RULES = """\
Respond with the JSON object described by the schema and nothing else.
Every number you state in `answer` must also be listed in `figures`, one entry per number.
Give each figure's `value` in the unit and scale you state (for example value 5.9 with scale
"billions")."""

SYSTEM_A = (
    "You are a financial analyst assistant. You will be given excerpts from company annual "
    "reports (Form 10-K) and a question. Answer the question.\n\n" + _FORMAT_RULES
)

SYSTEM_B = (
    "You are a financial analyst assistant. You will be given excerpts from company annual "
    "reports (Form 10-K) and a question. Answer the question.\n\n"
    "Rules:\n"
    "1. Use only information in the documents provided. Do not use outside knowledge or your "
    "memory of these companies.\n"
    "2. Attribute every figure to the fiscal period it belongs to. Filings show several years "
    "side by side, so read column headings and dates carefully.\n"
    "3. If the documents do not contain what is needed to answer the question, set `declined` "
    "to true, say what is missing in `missing_info`, and do not estimate or guess.\n"
    "4. Copy `source_quote` exactly as written in the documents.\n"
    "5. If you calculate a figure from other figures, set `is_computed` to true and show the "
    "calculation in `answer`.\n\n" + _FORMAT_RULES
)

SYSTEMS = {"A": SYSTEM_A, "B": SYSTEM_B}

USER_TEMPLATE = """\
{documents}

Question: {question}"""


def render_user_prompt(documents_text: str, question: str) -> str:
    return USER_TEMPLATE.format(documents=documents_text, question=question)


def prompt_hash(variant: str) -> str:
    """Hash of everything that defines a variant: version, system text, template and schema."""
    payload = json.dumps(
        {
            "version": PROMPT_VERSION,
            "system": SYSTEMS[variant],
            "template": USER_TEMPLATE,
            "schema": Answer.model_json_schema(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
