"""The no-tools baseline: question + the right filing text -> one structured answer, logged.

For each question the harness assembles the documents (docs.py), builds a prompt (prompts.py,
variant A or B), makes ONE model call (llm.py) and appends one JSON line to eval/runs/.
It does not score anything: scoring is the job of the deterministic checks and the judge.

Two modes keep development and measurement apart:
  dev       Haiku, eval/questions_dev.jsonl, any prompt edits allowed.
  measured  Sonnet 5.5, eval/questions.jsonl, refuses to run unless the prompts match the hashes
            frozen in eval/prompts.lock.json (create it with --freeze once the prompts are final).

Pressure test (--pressure): for questions that carry a scripted `pushback` message, the model first
answers normally, then the user insists on the false claim and the model answers again. This
measures whether it holds its position or caves. Each turn is logged as its own record, linked by
`conversation_id`. The pushback text is scripted and identical for every model and run.

Examples:
  uv run python -m sec_filings_analyst_agent.baseline --mode dev --variant both
  uv run python -m sec_filings_analyst_agent.baseline --mode dev --pressure
  uv run python -m sec_filings_analyst_agent.baseline --freeze
  uv run python -m sec_filings_analyst_agent.baseline --mode measured --variant both --repeats 3
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import duckdb
from pydantic import ValidationError

from sec_filings_analyst_agent.build import DB_PATH, ROOT
from sec_filings_analyst_agent.docs import answer_in_docs, format_documents, load_context
from sec_filings_analyst_agent.llm import complete
from sec_filings_analyst_agent.prompts import PROMPT_VERSION, SYSTEMS, prompt_hash, render_user_prompt
from sec_filings_analyst_agent.schemas import Answer

EVAL_DIR = ROOT / "eval"
RUNS_DIR = EVAL_DIR / "runs"
LOCK_PATH = EVAL_DIR / "prompts.lock.json"
QUESTION_FILES = {"dev": EVAL_DIR / "questions_dev.jsonl", "measured": EVAL_DIR / "questions.jsonl"}


def load_questions(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"Question file not found: {path}")
    questions = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for q in questions:
        for key in ("id", "question", "companies", "expected_periods"):
            if key not in q:
                sys.exit(f"Question {q.get('id', '?')} is missing required field {key!r}")
    return questions


def freeze_prompts() -> None:
    lock = {"version": PROMPT_VERSION, "frozen_at": datetime.now(timezone.utc).isoformat(),
            "hashes": {v: prompt_hash(v) for v in SYSTEMS}}
    LOCK_PATH.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"Froze prompts {lock['hashes']} -> {LOCK_PATH.relative_to(ROOT)}")


def check_prompts_frozen(variants: list[str]) -> None:
    if not LOCK_PATH.exists():
        sys.exit("No prompt lock. Finish developing the prompts, then run with --freeze first.")
    lock = json.loads(LOCK_PATH.read_text())
    for v in variants:
        if lock["hashes"].get(v) != prompt_hash(v):
            sys.exit(f"Variant {v} no longer matches the frozen prompt. A measured run must use "
                     f"the frozen prompts; if the change is deliberate, re-freeze and say why.")


def run_turn(question: dict, variant: str, repeat: int, mode: str, context, coverage: dict,
             messages: list[dict], turn: int, conversation_id: str | None = None,
             pushback: str | None = None):
    """One model call over `messages`. Returns (log record, raw assistant text or None)."""
    record = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode, "question_id": question["id"], "variant": variant, "repeat": repeat,
        "turn": turn, "conversation_id": conversation_id, "pushback": pushback,
        "prompt_version": PROMPT_VERSION, "prompt_hash": prompt_hash(variant),
        "documents": [d.ref() for d in context.documents], "context_notes": context.notes,
        "answer_in_docs": coverage,
    }
    raw_text = None
    try:
        result = complete("dev" if mode == "dev" else "measured", SYSTEMS[variant], None, Answer,
                          messages=messages)
        raw_text = result.raw_text
        record.update({
            "model": result.model, "requested_model": result.requested_model,
            "provider": result.provider, "request_id": result.request_id,
            "stop_reason": result.stop_reason, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "cost_usd": round(result.cost_usd, 5),
            "latency_s": result.latency_s, "raw_text": result.raw_text,
            "parsed": result.parsed.model_dump() if result.parsed else None, "error": None,
        })
    except (anthropic.APIError, ValidationError) as e:
        record.update({"model": None, "cost_usd": 0.0, "parsed": None, "raw_text": None,
                       "error": f"{type(e).__name__}: {e}"[:500]})
    return record, raw_text


def describe(rec: dict) -> str:
    if rec["error"]:
        return rec["error"]
    p = rec["parsed"]
    return "declined" if p and p["declined"] else f"{len(p['figures'])} figures"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["dev", "measured"], default="dev")
    p.add_argument("--variant", choices=["A", "B", "both"], default="both")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--questions", type=Path, default=None)
    p.add_argument("--limit", type=int, default=None, help="only the first N questions")
    p.add_argument("--pressure", action="store_true",
                   help="multi-turn pressure test: only questions with a scripted `pushback`")
    p.add_argument("--max-cost", type=float, default=3.0, help="stop if spend would exceed this (USD)")
    p.add_argument("--freeze", action="store_true", help="record the current prompt hashes and exit")
    args = p.parse_args()

    if args.freeze:
        freeze_prompts()
        return

    variants = ["A", "B"] if args.variant == "both" else [args.variant]
    if args.mode == "measured":
        check_prompts_frozen(variants)
    questions = load_questions(args.questions or QUESTION_FILES[args.mode])
    if args.pressure:
        questions = [q for q in questions if q.get("pushback")]
        if not questions:
            sys.exit("No questions with a `pushback` field in this question file.")
    if args.limit:
        questions = questions[: args.limit]

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    kind = f"{args.mode}_pressure" if args.pressure else args.mode
    out_path = RUNS_DIR / f"{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    con = duckdb.connect(str(DB_PATH), read_only=True)
    spent, calls, errors = 0.0, 0, 0
    with out_path.open("a") as out:
        for q in questions:
            context = load_context(con, q)
            documents_text = format_documents(context.documents)
            coverage = answer_in_docs(con, q, documents_text)
            first_user = {"role": "user", "content": render_user_prompt(documents_text, q["question"])}
            for variant in variants:
                for repeat in range(args.repeats):
                    if spent >= args.max_cost:
                        print(f"Stopping: spend ${spent:.3f} reached --max-cost ${args.max_cost}")
                        print(f"Partial results in {out_path.relative_to(ROOT)}")
                        return
                    conv_id = str(uuid.uuid4()) if args.pressure else None
                    rec1, raw1 = run_turn(q, variant, repeat, args.mode, context, coverage,
                                          [first_user], turn=1, conversation_id=conv_id)
                    out.write(json.dumps(rec1) + "\n"); out.flush()
                    spent += rec1["cost_usd"]; calls += 1; errors += rec1["error"] is not None
                    line = f"{q['id']} {variant} r{repeat} t1: {describe(rec1)} (${rec1['cost_usd']:.4f})"
                    if args.pressure and raw1 is not None:
                        history = [first_user, {"role": "assistant", "content": raw1},
                                   {"role": "user", "content": q["pushback"]}]
                        rec2, _ = run_turn(q, variant, repeat, args.mode, context, coverage,
                                           history, turn=2, conversation_id=conv_id, pushback=q["pushback"])
                        out.write(json.dumps(rec2) + "\n"); out.flush()
                        spent += rec2["cost_usd"]; calls += 1; errors += rec2["error"] is not None
                        line += f" | t2: {describe(rec2)} (${rec2['cost_usd']:.4f})"
                    print(line + f" | in-docs: {coverage['overall']}")
    print(f"\n{calls} calls, {errors} errors, ${spent:.4f} total -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
