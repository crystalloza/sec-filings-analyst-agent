"""The no-tools baseline: question + the right filing text -> one structured answer, logged.

For each question the harness assembles the documents (docs.py), builds a prompt (prompts.py,
variant A or B), makes ONE model call (llm.py) and appends one JSON line to eval/runs/.
It does not score anything: scoring is the job of the deterministic checks and the judge.

Two modes keep development and measurement apart:
  dev       Haiku, eval/questions_dev.jsonl, any prompt edits allowed.
  measured  Sonnet 5.5, eval/questions.jsonl, refuses to run unless the prompts match the hashes
            frozen in eval/prompts.lock.json (create it with --freeze once the prompts are final).

Examples:
  uv run python -m sec_filings_analyst_agent.baseline --mode dev --variant both
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


def run_one(con, question: dict, variant: str, repeat: int, mode: str, context, coverage: dict) -> dict:
    system = SYSTEMS[variant]
    user = render_user_prompt(format_documents(context.documents), question["question"])
    record = {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode, "question_id": question["id"], "variant": variant, "repeat": repeat,
        "prompt_version": PROMPT_VERSION, "prompt_hash": prompt_hash(variant),
        "documents": [d.ref() for d in context.documents], "context_notes": context.notes,
        "answer_in_docs": coverage,
    }
    try:
        result = complete("dev" if mode == "dev" else "measured", system, user, Answer)
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
    return record


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["dev", "measured"], default="dev")
    p.add_argument("--variant", choices=["A", "B", "both"], default="both")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--questions", type=Path, default=None)
    p.add_argument("--limit", type=int, default=None, help="only the first N questions")
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
    if args.limit:
        questions = questions[: args.limit]

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RUNS_DIR / f"{args.mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    con = duckdb.connect(str(DB_PATH), read_only=True)
    spent, calls, errors = 0.0, 0, 0
    with out_path.open("a") as out:
        for q in questions:
            context = load_context(con, q)
            coverage = answer_in_docs(con, q, format_documents(context.documents))
            for variant in variants:
                for repeat in range(args.repeats):
                    if spent >= args.max_cost:
                        print(f"Stopping: spend ${spent:.3f} reached --max-cost ${args.max_cost}")
                        print(f"Partial results in {out_path.relative_to(ROOT)}")
                        return
                    rec = run_one(con, q, variant, repeat, args.mode, context, coverage)
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    spent += rec["cost_usd"]
                    calls += 1
                    errors += rec["error"] is not None
                    status = rec["error"] or ("declined" if rec["parsed"] and rec["parsed"]["declined"]
                                              else f"{len(rec['parsed']['figures'])} figures")
                    print(f"{q['id']} {variant} r{repeat}: {status} "
                          f"(${rec['cost_usd']:.4f}, in-docs: {coverage['overall']})")
    print(f"\n{calls} calls, {errors} errors, ${spent:.4f} total -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
