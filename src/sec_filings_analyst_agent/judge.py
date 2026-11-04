"""Run the LLM judge over logged answers.

  uv run python -m sec_filings_analyst_agent.judge --dev                      # tune on the hand-written dev set
  uv run python -m sec_filings_analyst_agent.judge --plan --runs F --pressure P   # counts and cost, no API calls
  uv run python -m sec_filings_analyst_agent.judge --freeze                   # lock the judge prompts
  uv run python -m sec_filings_analyst_agent.judge --runs F --pressure P      # measured judging (needs the lock)
  uv run python -m sec_filings_analyst_agent.judge --summarize JUDGE_FILE

One call per (answer, criterion, run). The judge sees only the conversation, plus a reference line
(criteria 1-2) or the filing text (criterion 4). It never sees the variant, the figure list, or any
code-check result. Every raw judgment is logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sec_filings_analyst_agent.judge_prompts import (
    CRITERIA, JUDGE_VERSION, OUTPUT_MODELS, SYSTEMS, prompt_hash, render_user_prompt,
)
from sec_filings_analyst_agent.llm import EFFORT, MODELS, PRICES, complete

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval"
JUDGE_RUNS = EVAL_DIR / "judge_runs"
LOCK_PATH = EVAL_DIR / "judge.lock.json"
CONTEXT_PATH = EVAL_DIR / "judge_context.json"
DEV_PATH = EVAL_DIR / "judge_dev.jsonl"
MISUSE_BEHAVIORS = {"refuse_misuse", "refuse_deceive", "resist_override"}
NARRATIVE_TYPES = {"narrative", "narrative_plus_number"}


# ----------------------------------------------------------------------------- what gets judged
def render_answer(parsed: dict) -> str:
    """The answer as a reader sees it: the prose, and what the assistant said was missing if it declined.
    The structured figure list is deliberately NOT shown to the judge."""
    text = parsed.get("answer", "") or ""
    if parsed.get("declined") and parsed.get("missing_info"):
        text += f"\n(The assistant said this information is missing: {parsed['missing_info']})"
    return text


def applicable_criteria(question: dict, variant: str) -> list[str]:
    crit = ["advice"]
    if question.get("expected_behavior") == "correct_premise":
        crit.append("premise")
    if question.get("expected_behavior") in MISUSE_BEHAVIORS:
        crit.append("misuse")
    if question.get("type") in NARRATIVE_TYPES and variant != "C":  # closed-book saw no filing text to reconcile with
        crit.append("narrative")
    return crit


def item_id(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


def _last_successful(records: list[dict]) -> list[dict]:
    keep: dict[tuple, dict] = {}
    for r in records:
        key = (r["question_id"], r["variant"], r["repeat"], r.get("turn") or 1, r.get("conversation_id"))
        if r.get("parsed") is not None or key not in keep:
            keep[key] = r
    return [r for r in keep.values() if r.get("parsed") is not None]


def build_items(main_records: list[dict], pressure_records: list[dict], questions: dict[str, dict],
                reference: dict[str, str]) -> list[dict]:
    """One item per (answer, applicable criterion). Items carry no variant-revealing text."""
    items = []
    for r in _last_successful(main_records):
        q = questions.get(r["question_id"])
        if q is None or (r.get("turn") or 1) != 1:
            continue
        p = r["parsed"]
        convo = [{"role": "user", "content": q["question"]}, {"role": "assistant", "content": render_answer(p)}]
        for crit in applicable_criteria(q, r["variant"]):
            if crit == "narrative" and p.get("declined"):
                continue  # a decline has no explanation to reconcile
            if crit in ("premise", "misuse") and q["id"] not in reference:
                raise KeyError(f"{q['id']} needs a reference line in judge_context.json for criterion {crit}")
            items.append({"item_id": item_id("main", q["id"], r["variant"], r["repeat"], crit), "criterion": crit,
                          "source": "main", "question_id": q["id"], "variant": r["variant"], "repeat": r["repeat"],
                          "conversation": convo, "reference": reference.get(q["id"]) if crit in ("premise", "misuse") else None,
                          "documents_ref": r.get("documents")})
    by_conv = defaultdict(dict)
    for r in _last_successful(pressure_records):
        by_conv[r["conversation_id"]][r.get("turn") or 1] = r
    for conv_id, turns in by_conv.items():
        if 1 not in turns or 2 not in turns:
            continue
        r1, r2 = turns[1], turns[2]
        q = questions[r1["question_id"]]
        convo = [{"role": "user", "content": q["question"]}, {"role": "assistant", "content": render_answer(r1["parsed"])},
                 {"role": "user", "content": r2["pushback"]}, {"role": "assistant", "content": render_answer(r2["parsed"])}]
        items.append({"item_id": item_id("pressure", conv_id, "premise"), "criterion": "premise", "source": "pressure",
                      "question_id": q["id"], "variant": r1["variant"], "repeat": r1["repeat"], "conversation": convo,
                      "reference": reference[q["id"]]})
    return items


TRIAL_QUOTA = {"premise": 6, "misuse": 4, "advice": 5, "narrative": 5}


def items_from_key(path: Path) -> list[dict]:
    """The labeled set (real answers + seeded failures) as judge items. Only the conversation, the reference
    line and the document pointer are carried over; origin and seed details stay out of what the judge sees.
    Real items keep their original item_id, so earlier judgments of them can be reused."""
    key = json.loads(path.read_text())
    return [{"item_id": k["item_id"], "criterion": k["criterion"], "source": k["origin"], "question_id": k["question_id"],
             "variant": None if k["origin"] == "seed" else k["variant"], "conversation": k["conversation"],
             "reference": k["reference"] if k["criterion"] in ("premise", "misuse") else None,
             "documents_ref": k.get("documents_ref")} for k in key["items"]]


def trial_sample(items: list[dict], questions: dict[str, dict], seed: int = 7) -> list[dict]:
    """A small mixed sample of REAL answers for reading the judge's reasoning before the prompts are frozen.
    Advice is drawn first from questions that invite advice, since most answers are plain lookups."""
    import random
    rng = random.Random(seed)
    chosen = []
    for crit, quota in TRIAL_QUOTA.items():
        pool = [i for i in items if i["criterion"] == crit]
        rng.shuffle(pool)
        if crit == "advice":
            pool.sort(key=lambda i: questions[i["question_id"]].get("expected_behavior") != "decline_advice")
        if crit == "premise":
            pool.sort(key=lambda i: i["source"] != "pressure")  # include the pressure conversations
        chosen += pool[:quota]
    return chosen


def load_dev_items() -> list[dict]:
    return [json.loads(l) for l in DEV_PATH.read_text().splitlines() if l.strip()]


# ----------------------------------------------------------------------------- lock
def judge_config() -> dict:
    return {"models": {"judge": MODELS["judge"]}, "effort": {"judge": EFFORT["judge"]}}


def freeze() -> None:
    lock = {"version": JUDGE_VERSION, "frozen_at": datetime.now(timezone.utc).isoformat(),
            "hashes": {c: prompt_hash(c) for c in CRITERIA}, "config": judge_config()}
    LOCK_PATH.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"Froze judge prompts {lock['hashes']} -> {LOCK_PATH.relative_to(ROOT)}")


def check_frozen() -> None:
    if not LOCK_PATH.exists():
        sys.exit("No judge lock. Tune on the dev set, then run with --freeze first.")
    lock = json.loads(LOCK_PATH.read_text())
    if lock.get("config") != judge_config():
        sys.exit("The judge model or effort no longer matches the frozen one. Re-freeze if deliberate.")
    for c in CRITERIA:
        if lock["hashes"].get(c) != prompt_hash(c):
            sys.exit(f"Judge criterion {c!r} no longer matches the frozen prompt. A measured run must use the frozen prompts.")


# ----------------------------------------------------------------------------- aggregation
def majority(verdicts: list[str]) -> str:
    """Majority vote; a tie (or a 3-way split) goes to fail, the safe direction."""
    counts = Counter(verdicts).most_common()
    if len(counts) == 1 or counts[0][1] > counts[1][1]:
        return counts[0][0]
    return "fail"


def analysis_verdict(criterion: str, verdict: str) -> str:
    """The verdict as used for agreement statistics. For the advice criterion, pass and not_applicable are one
    outcome ("no advice given"): the judge flips between them on near-identical answers, and the question that
    matters is only whether advice was given. The raw verdict stays in the logs."""
    if criterion == "advice" and verdict == "not_applicable":
        return "pass"
    return verdict


def aggregate(records: list[dict]) -> list[dict]:
    """One result per (item, criterion) from its runs (errored runs are ignored)."""
    groups = defaultdict(list)
    for r in records:
        if r.get("parsed"):
            groups[(r["item_id"], r["criterion"])].append(r)
    out = []
    for (iid, crit), runs in groups.items():
        verdicts = [x["parsed"]["verdict"] for x in runs]
        first = runs[0]
        out.append({"item_id": iid, "criterion": crit, "source": first.get("source"), "question_id": first.get("question_id"),
                    "variant": first.get("variant"), "verdict": majority(verdicts), "n_runs": len(runs),
                    "agreement": "unanimous" if len(set(verdicts)) == 1 else "split", "verdicts": verdicts,
                    "confidence": "borderline" if any(x["parsed"]["confidence"] == "borderline" for x in runs) else "clear"})
    return out


# ----------------------------------------------------------------------------- running
def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_p, out_p = PRICES[model]["standard"]
    return input_tokens * in_p / 1e6 + output_tokens * out_p / 1e6


def user_prompt(item: dict, documents: str | None) -> str:
    return render_user_prompt(item["criterion"], item["conversation"], item.get("reference"),
                              documents if item["criterion"] == "narrative" else None)


def run_judge(items: list[dict], n: int, out_path: Path, max_cost: float, docs_for, workers: int = 6,
              done: set | None = None) -> float:
    lock, state = threading.Lock(), {"spent": 0.0, "stopped": False}
    done = done or set()
    tasks = [(it, k) for it in items for k in range(n) if (it["item_id"], it["criterion"], k) not in done]
    print(f"{len(tasks)} judge calls ({len(items)} items x {n} runs, {len(done)} already done)")

    def one(task):
        item, k = task
        if state["stopped"]:
            return
        rec = {"run_id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(), "item_id": item["item_id"],
               "criterion": item["criterion"], "run": k, "source": item.get("source"), "question_id": item.get("question_id"),
               "variant": item.get("variant"), "judge_version": JUDGE_VERSION, "prompt_hash": prompt_hash(item["criterion"])}
        try:
            docs = docs_for(item) if item["criterion"] == "narrative" else None
            res = complete("judge", SYSTEMS[item["criterion"]], user_prompt(item, docs), OUTPUT_MODELS[item["criterion"]])
            rec.update({"model": res.model, "effort": res.effort, "request_id": res.request_id, "stop_reason": res.stop_reason,
                        "input_tokens": res.input_tokens, "output_tokens": res.output_tokens, "cost_usd": round(res.cost_usd, 5),
                        "latency_s": res.latency_s, "raw_text": res.raw_text,
                        "parsed": res.parsed.model_dump() if res.parsed else None, "error": None})
        except Exception as e:  # logged and retried on --resume; one bad call must not lose the rest
            rec.update({"model": None, "cost_usd": 0.0, "parsed": None, "raw_text": None, "error": f"{type(e).__name__}: {e}"[:500]})
        with lock:
            state["spent"] += rec["cost_usd"]
            with out_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            if state["spent"] > max_cost:
                state["stopped"] = True
                print(f"STOP: spent ${state['spent']:.2f} > --max-cost ${max_cost}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, tasks))
    return state["spent"]


def plan(items: list[dict], n: int, docs_for) -> None:
    """Counts and a rough cost, without calling the API (token counts are chars/4 x 1.4, as before)."""
    price_in, price_out = PRICES[MODELS["judge"]["anthropic"]]["standard"]
    by_crit, tokens_in = Counter(), 0
    for it in items:
        docs = docs_for(it) if it["criterion"] == "narrative" else None
        chars = len(SYSTEMS[it["criterion"]]) + len(user_prompt(it, docs))
        tokens_in += int(chars / 4 * 1.4)
        by_crit[(it["criterion"], it["source"])] += 1
    for (crit, src), c in sorted(by_crit.items()):
        print(f"  {crit:10} {src:9} {c:4} items")
    out_tokens = len(items) * 500
    cost = (tokens_in * price_in + out_tokens * price_out) / 1e6 * n
    print(f"{len(items)} items x {n} runs = {len(items) * n} calls; ~{tokens_in * n / 1e6:.1f}M input tokens; rough cost ${cost:.2f}")


def summarize(path: Path) -> None:
    records = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    agg = aggregate(records)
    errors = sum(1 for r in records if r.get("error"))
    print(f"{len(records)} judgments, {errors} errors, ${sum(r.get('cost_usd') or 0 for r in records):.2f}; {len(agg)} items judged")
    print(f"{'criterion':10} {'source':9} {'variant':8} {'n':>4} {'pass':>5} {'fail':>5} {'n/a':>4} {'borderline':>10} {'split':>6}")
    groups = defaultdict(list)
    for a in agg:
        groups[(a["criterion"], a["source"], a["variant"] if a["source"] != "dev" else "-")].append(a)
    for (crit, src, var), rows in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        c = Counter(r["verdict"] for r in rows)
        print(f"{crit:10} {src:9} {str(var):8} {len(rows):>4} {c['pass']:>5} {c['fail']:>5} {c['not_applicable']:>4} "
              f"{sum(r['confidence'] == 'borderline' for r in rows):>10} {sum(r['agreement'] == 'split' for r in rows):>6}")


def show_dev(agg: list[dict], items: list[dict]) -> None:
    expected = {i["item_id"]: i for i in items}
    hits = 0
    print(f"{'item':8} {'criterion':10} {'expected':14} {'judge':14} {'conf':10} {'runs':10} {'match':6} note")
    for a in sorted(agg, key=lambda x: x["item_id"]):
        e = expected[a["item_id"]]
        ok = a["verdict"] == e["expected_verdict"]
        hits += ok
        print(f"{a['item_id']:8} {a['criterion']:10} {e['expected_verdict']:14} {a['verdict']:14} {a['confidence']:10} "
              f"{a['agreement']:10} {'OK' if ok else 'MISS':6} {e['note']}")
    print(f"\n{hits}/{len(agg)} match the author's expected verdicts (dev set; the author's labels, not yours)")


def show_trial(records: list[dict], items: list[dict]) -> None:
    by_id = {(i["item_id"], i["criterion"]): i for i in items}
    runs = defaultdict(list)
    for r in records:
        if r.get("parsed"):
            runs[(r["item_id"], r["criterion"])].append(r)
    for key, rs in sorted(runs.items(), key=lambda kv: kv[0][1]):
        it = by_id[key]
        rs = sorted(rs, key=lambda x: x["run"])
        verdicts = [x["parsed"]["verdict"] + "/" + x["parsed"]["confidence"][0] for x in rs]
        print("\n" + "-" * 100)
        print(f"{it['criterion'].upper()} | {it['question_id']} | {it['source']} | runs: {verdicts}")
        print("  Q:", it["conversation"][0]["content"][:160].replace("\n", " "))
        if len(it["conversation"]) > 2:
            print("  PUSHBACK:", it["conversation"][2]["content"][:120])
        print("  A:", it["conversation"][-1]["content"][:420].replace("\n", " "))
        first = rs[0]["parsed"]
        for c in (first.get("claims") or [])[:6]:
            print(f"    claim [{c['label']}]: {c['claim'][:110]}")
        print("  JUDGE:", first["reasoning"][:520].replace("\n", " "))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dev", action="store_true", help="judge the hand-written dev set")
    p.add_argument("--plan", action="store_true", help="count items and estimate cost; no API calls")
    p.add_argument("--freeze", action="store_true")
    p.add_argument("--summarize", type=Path)
    p.add_argument("--runs", type=Path, help="baseline runs file (main)")
    p.add_argument("--pressure", type=Path, help="baseline pressure runs file")
    p.add_argument("--trial", action="store_true", help="judge a small sample of real answers (not frozen, not committed) to read the reasoning")
    p.add_argument("--n", type=int, default=3, help="runs per judgment (majority vote)")
    p.add_argument("--max-cost", type=float, default=15.0)
    p.add_argument("--resume", type=Path, help="continue an interrupted judge file")
    p.add_argument("--items", type=Path, help="judge the labeled set from a labeling key (real + seeded items)")
    p.add_argument("--reuse", type=Path, help="with --items: copy existing judgments of the same item ids instead of re-running them")
    args = p.parse_args()

    if args.freeze:
        return freeze()
    if args.summarize:
        return summarize(args.summarize)

    JUDGE_RUNS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.dev:
        items = load_dev_items()
        out = JUDGE_RUNS / f"dev_{stamp}.jsonl"
        spent = run_judge(items, args.n, out, args.max_cost, docs_for=lambda it: it["documents_text"])
        recs = [json.loads(l) for l in out.read_text().splitlines()]
        print(f"errors: {sum(1 for r in recs if r.get('error'))}; spent ${spent:.2f} -> {out.relative_to(ROOT)}")
        show_dev(aggregate(recs), items)
        return

    import duckdb
    from sec_filings_analyst_agent.build import DB_PATH
    from sec_filings_analyst_agent.docs import format_documents, load_context
    if not args.runs and not args.items:
        sys.exit("--runs is required (a baseline runs file)")
    questions = {json.loads(l)["id"]: json.loads(l) for l in (EVAL_DIR / "questions.jsonl").read_text().splitlines() if l.strip()}
    reference = json.loads(CONTEXT_PATH.read_text())
    read = lambda path: [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if args.items:
        items = items_from_key(args.items)
    else:
        items = build_items(read(args.runs), read(args.pressure) if args.pressure else [], questions, reference)
    con = duckdb.connect(str(DB_PATH), read_only=True)
    if args.trial:
        items = trial_sample(items, questions)
    # Load the filing text ONCE, here in the main thread (a DuckDB connection must not be shared across worker
    # threads), and confirm the judge is shown the same documents the answerer was.
    cache: dict[str, str] = {}
    for it in items:
        if it["criterion"] == "narrative" and it["question_id"] not in cache:
            ctx = load_context(con, questions[it["question_id"]])
            if [d.ref() for d in ctx.documents] != it["documents_ref"]:
                sys.exit(f"{it['question_id']}: the documents now loaded differ from the ones the answerer was shown")
            cache[it["question_id"]] = format_documents(ctx.documents)
    docs_for = lambda it: cache[it["question_id"]]
    if args.plan:
        return plan(items, args.n, docs_for)
    if not args.trial:
        check_frozen()
    out = args.resume or JUDGE_RUNS / (f"dev_trial_{stamp}.jsonl" if args.trial else f"judged_labeled_{stamp}.jsonl" if args.items else f"judge_{stamp}.jsonl")
    done = set()
    if args.items and args.reuse and not args.resume:
        wanted = {(i["item_id"], i["criterion"]) for i in items if i["source"] != "seed"}
        with out.open("w") as f:
            for r in read(args.reuse):
                if r.get("parsed") and (r["item_id"], r["criterion"]) in wanted:
                    f.write(json.dumps(r) + "\n")
    if args.items and out.exists():
        for r in read(out):
            if r.get("parsed"):
                done.add((r["item_id"], r["criterion"], r["run"]))
    if args.resume:
        for r in read(args.resume):
            if r.get("parsed"):
                done.add((r["item_id"], r["criterion"], r["run"]))
    spent = run_judge(items, args.n, out, args.max_cost, docs_for, done=done)
    print(f"spent ${spent:.2f} -> {out.resolve().relative_to(ROOT)}")
    summarize(out)
    if args.trial:
        show_trial(read(out), items)


if __name__ == "__main__":
    main()
