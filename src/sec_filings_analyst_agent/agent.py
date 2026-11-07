"""The agent: a model that answers by calling the two MCP servers (sec-facts, sec-filings).

  uv run python -m sec_filings_analyst_agent.agent --mode dev --limit 3        # cheap model, quick look
  uv run python -m sec_filings_analyst_agent.agent --mode dev --ids q01,q35    # chosen questions
  uv run python -m sec_filings_analyst_agent.agent --freeze                    # lock prompt + tools + config
  uv run python -m sec_filings_analyst_agent.agent --mode measured             # the measured run (needs the lock)

How a question runs (decisions in docs/agent_design.md):
  * Both servers are started once and spoken to over MCP; their tool menus are shown to the model.
  * At most 8 DATA tool calls per question. After the 8th, only submit_answer is offered and the model is told to
    submit. (Forcing the choice is not possible: Sonnet 5.5 rejects tool_choice "tool"/"any".)
  * The model ends by calling submit_answer, whose input is the structured AgentAnswer.
  * Every tool call is logged with its FULL result text: that is what the traceability check later verifies against.
Pressure mode (--pressure) continues the same conversation with the question's scripted pushback and a fresh budget.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from sec_filings_analyst_agent.agent_prompts import AGENT_VERSION, MAX_DATA_CALLS, SYSTEM_AGENT, agent_hash
from sec_filings_analyst_agent.baseline import current_config
from sec_filings_analyst_agent.llm import create_with_tools
from sec_filings_analyst_agent.schemas import AgentAnswer

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval"
RUNS_DIR = EVAL_DIR / "runs"
LOCK_PATH = EVAL_DIR / "agent.lock.json"
SERVERS = {"sec-facts": "mcp_facts", "sec-filings": "mcp_filings"}
SUBMIT = "submit_answer"
MAX_SUBMIT_RETRIES = 2
TOOL_TIMEOUT_S = 60.0                 # a hung server must not hang the run; the model sees a timeout error
MAX_TURNS = MAX_DATA_CALLS + 8        # data calls plus room for a nudge, retries and the final submit
VARIANT = "AGENT"

SUBMIT_DEF = {
    "name": SUBMIT,
    "description": "Submit your final answer. Call this exactly once, when you are done. "
                   "Every number in the answer text must also be listed in figures.",
    "input_schema": AgentAnswer.model_json_schema(),
}


# ----------------------------------------------------------------------------- tools
class ToolHub:
    """Both MCP servers behind one call(); tool definitions in the model API's format."""

    def __init__(self) -> None:
        self.route: dict = {}
        self.defs: list[dict] = []

    async def start(self, stack: AsyncExitStack) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        for module in SERVERS.values():
            params = StdioServerParameters(command=sys.executable, args=["-m", f"sec_filings_analyst_agent.{module}"],
                                           cwd=str(ROOT))
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            for t in (await session.list_tools()).tools:
                self.route[t.name] = session
                self.defs.append({"name": t.name, "description": t.description or "",
                                  "input_schema": getattr(t, "inputSchema", None) or t.input_schema})

    async def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        res = await self.route[name].call_tool(name, arguments)
        text = "\n".join(c.text for c in res.content if getattr(c, "type", None) == "text")
        return text, bool(getattr(res, "isError", getattr(res, "is_error", False)))


# ----------------------------------------------------------------------------- the loop
def block_to_param(b) -> dict:
    """An assistant content block as the dict the API expects back in the next request."""
    if hasattr(b, "model_dump"):
        return b.model_dump(exclude_none=True)
    if b.type == "text":
        return {"type": "text", "text": b.text}
    return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}


def _result(tool_use_id: str, text: str, is_error: bool = False) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": text, **({"is_error": True} if is_error else {})}


async def converse(messages: list[dict], hub, alias: str, model_call=create_with_tools) -> dict:
    """Run the tool loop on `messages` (which must end with a user message) until the model submits.

    Appends every assistant turn and tool-result message to `messages`. When the answer is accepted, the last
    message is the user message holding its tool_result, so a caller can add a follow-up block to it.
    """
    tool_calls, turns = [], []
    data_calls, retries, nudged, parsed, end_reason, error = 0, 0, False, None, "max_turns", None
    for i in range(MAX_TURNS):
        exhausted = data_calls >= MAX_DATA_CALLS
        tools = [SUBMIT_DEF] if exhausted else hub.defs + [SUBMIT_DEF]
        try:
            turn = await model_call(alias, SYSTEM_AGENT, messages, tools, None)
        except Exception as e:  # keep what the run found so far (tool calls, cost); --resume retries it
            end_reason, error = "error", f"{type(e).__name__}: {e}"[:500]
            break
        uses = [b for b in turn.content if b.type == "tool_use"]
        turns.append({"request_id": turn.request_id, "stop_reason": turn.stop_reason, "model": turn.model,
                      "effort": turn.effort, "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens,
                      "cost_usd": round(turn.cost_usd, 5), "latency_s": turn.latency_s, "tool_uses": [u.name for u in uses]})
        messages.append({"role": "assistant", "content": [block_to_param(b) for b in turn.content]})
        if not uses:
            if nudged:
                end_reason = "no_submit"
                break
            nudged = True
            messages.append({"role": "user", "content": "Call submit_answer now with your final answer."})
            continue
        results, accepted = [], False
        for b in uses:
            if b.name == SUBMIT:
                if accepted:
                    results.append(_result(b.id, "Ignored: an answer was already submitted.", True))
                    continue
                try:
                    parsed = AgentAnswer.model_validate(b.input)
                    accepted = True
                    results.append(_result(b.id, "Answer recorded."))
                except ValidationError as e:
                    retries += 1
                    detail = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5])
                    results.append(_result(b.id, f"Invalid answer, fix and call submit_answer again: {detail}", True))
            elif b.name not in hub.route:
                results.append(_result(b.id, f"Unknown tool {b.name!r}.", True))
            elif data_calls >= MAX_DATA_CALLS:
                tool_calls.append({"turn": i, "tool": b.name, "arguments": b.input, "result_text": None,
                                   "is_error": True, "rejected": "tool budget used up", "latency_s": 0.0})
                results.append(_result(b.id, "Tool budget used up. Submit your answer.", True))
            else:
                data_calls += 1
                start = time.time()
                try:
                    text, is_error = await asyncio.wait_for(hub.call(b.name, b.input), TOOL_TIMEOUT_S)
                except asyncio.TimeoutError:
                    text, is_error = f"The tool did not answer within {TOOL_TIMEOUT_S:.0f} seconds.", True
                except Exception as e:  # a failing tool must not end the run; the model sees the error
                    text, is_error = f"{type(e).__name__}: {e}", True
                tool_calls.append({"turn": i, "tool": b.name, "arguments": b.input, "result_text": text,
                                   "is_error": is_error, "rejected": None, "latency_s": round(time.time() - start, 3)})
                results.append(_result(b.id, text, is_error))
        user_content = results
        if accepted:
            messages.append({"role": "user", "content": user_content})
            end_reason = "submitted"
            break
        if retries > MAX_SUBMIT_RETRIES:
            messages.append({"role": "user", "content": user_content})
            end_reason = "invalid_submit"
            break
        if data_calls >= MAX_DATA_CALLS:
            user_content = results + [{"type": "text", "text": (
                "Your tool budget is used up. Submit your answer now from what you have found; if it is not "
                "enough, decline and say what is missing.")}]
        messages.append({"role": "user", "content": user_content})
    return {"parsed": parsed, "end_reason": end_reason, "tool_calls": tool_calls, "model_turns": turns,
            "n_data_calls": data_calls, "budget_exhausted": data_calls >= MAX_DATA_CALLS, "submit_retries": retries,
            "error": error}


# ----------------------------------------------------------------------------- records
def _record(q: dict, mode: str, repeat: int, turn: int, conv_id: str | None, pushback: str | None, ahash: str) -> dict:
    return {"run_id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(), "mode": mode,
            "question_id": q["id"], "variant": VARIANT, "repeat": repeat, "turn": turn, "conversation_id": conv_id,
            "pushback": pushback, "agent_version": AGENT_VERSION, "agent_hash": ahash, "documents": [],
            "answer_in_docs": None}


async def run_turn(q, mode, repeat, turn, conv_id, pushback, ahash, messages, hub, model_call) -> dict:
    alias = "dev" if mode == "dev" else "measured"
    rec = _record(q, mode, repeat, turn, conv_id, pushback, ahash)
    try:
        out = await converse(messages, hub, alias, model_call)
        turns = out["model_turns"]
        rec.update({"model": turns[0]["model"] if turns else None, "effort": turns[0]["effort"] if turns else None,
                    "parsed": out["parsed"].model_dump() if out["parsed"] else None, "end_reason": out["end_reason"],
                    "tool_calls": out["tool_calls"], "model_turns": turns, "n_data_calls": out["n_data_calls"],
                    "budget_exhausted": out["budget_exhausted"], "submit_retries": out["submit_retries"],
                    "cost_usd": round(sum(t["cost_usd"] for t in turns), 5),
                    "input_tokens": sum(t["input_tokens"] for t in turns), "output_tokens": sum(t["output_tokens"] for t in turns),
                    "latency_s": round(sum(t["latency_s"] for t in turns), 2), "error": out["error"]})
    except Exception as e:  # logged; --resume retries. One failed question must not lose the run.
        rec.update({"model": None, "parsed": None, "end_reason": "error", "tool_calls": [], "model_turns": [],
                    "n_data_calls": 0, "cost_usd": 0.0, "error": f"{type(e).__name__}: {e}"[:500]})
    return rec


async def run_question(q, mode, repeat, pressure, ahash, hub, model_call=create_with_tools) -> list[dict]:
    conv_id = str(uuid.uuid4()) if pressure else None
    messages = [{"role": "user", "content": q["question"]}]
    r1 = await run_turn(q, mode, repeat, 1, conv_id, None, ahash, messages, hub, model_call)
    records = [r1]
    if pressure and r1["parsed"] is not None:
        # the accepted answer's tool_result is the last message; add the pushback beside it, then a fresh budget
        messages[-1]["content"].append({"type": "text", "text": q["pushback"]})
        records.append(await run_turn(q, mode, repeat, 2, conv_id, q["pushback"], ahash, messages, hub, model_call))
    return records


# ----------------------------------------------------------------------------- lock + CLI
def agent_config() -> dict:
    return current_config()


def freeze(hub) -> None:
    lock = {"version": AGENT_VERSION, "frozen_at": datetime.now(timezone.utc).isoformat(),
            "hash": agent_hash(hub.defs), "config": agent_config(), "max_data_calls": MAX_DATA_CALLS}
    LOCK_PATH.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"Froze agent {lock['hash']} -> {LOCK_PATH.name}")


def check_frozen(hub) -> None:
    if not LOCK_PATH.exists():
        sys.exit("No agent lock. Finish developing, then run with --freeze first.")
    lock = json.loads(LOCK_PATH.read_text())
    if lock.get("config") != agent_config():
        sys.exit("The model or effort configuration no longer matches the frozen one. Re-freeze if deliberate.")
    if lock.get("hash") != agent_hash(hub.defs):
        sys.exit("The agent prompt, answer schema or tool definitions no longer match the frozen setup. "
                 "A measured run must use the frozen agent; if the change is deliberate, re-freeze and say why.")


def load_done(path: Path) -> set[tuple]:
    done = set()
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if not r.get("error") and r.get("parsed") is not None:
                done.add((r["question_id"], r["repeat"], r.get("turn") or 1))
    return done


async def amain(args) -> None:
    from sec_filings_analyst_agent.baseline import QUESTION_FILES, load_questions
    async with AsyncExitStack() as stack:
        hub = ToolHub()
        await hub.start(stack)
        if args.freeze:
            return freeze(hub)
        if args.mode == "measured":
            check_frozen(hub)
        ahash = agent_hash(hub.defs)
        questions = load_questions(args.questions or QUESTION_FILES[args.mode])
        if args.ids:
            wanted = set(args.ids.split(","))
            questions = [q for q in questions if q["id"] in wanted]
        if args.pressure:
            questions = [q for q in questions if q.get("pushback")]
            if not questions:
                sys.exit("No questions with a `pushback` field in this question file.")
        if args.limit:
            questions = questions[: args.limit]
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        kind = f"agent_{args.mode}_pressure" if args.pressure else f"agent_{args.mode}"
        out_path = args.resume or RUNS_DIR / f"{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        done = load_done(args.resume) if args.resume else set()
        if args.resume:
            out_path = args.resume.resolve()
            print(f"Resuming {out_path.name}: {len(done)} turns already done")
        state = {"spent": 0.0, "stopped": False, "calls": 0, "errors": 0}
        sem = asyncio.Semaphore(args.workers)

        async def unit(q, repeat):
            if (q["id"], repeat, 1) in done and not args.pressure:
                return
            async with sem:
                if state["stopped"] or state["spent"] >= args.max_cost:
                    state["stopped"] = True
                    return
                recs = await run_question(q, args.mode, repeat, args.pressure, ahash, hub)
            with out_path.open("a") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
                    state["spent"] += r["cost_usd"]; state["calls"] += 1; state["errors"] += r["error"] is not None
            r1 = recs[0]
            p = r1["parsed"]
            if r1["error"]:
                what = r1["error"]
            elif p is None:
                what = "no answer"
            else:
                what = "declined" if p["declined"] else f"{len(p['figures'])} figures"
            print(f"{q['id']} r{repeat}: {what}, {r1['n_data_calls']} tool calls, {r1['end_reason']} (${sum(r['cost_usd'] for r in recs):.4f})")

        await asyncio.gather(*(unit(q, rep) for q in questions for rep in range(args.repeats)))
        if state["stopped"]:
            print(f"Stopped at --max-cost ${args.max_cost}; resume with --resume {out_path.resolve()}")
        print(f"\n{state['calls']} records, {state['errors']} errors, ${state['spent']:.4f} -> {out_path.resolve()}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["dev", "measured"], default="dev")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--questions", type=Path, default=None)
    p.add_argument("--ids", type=str, default=None, help="comma-separated question ids")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--pressure", action="store_true")
    p.add_argument("--workers", type=int, default=3, help="questions run at the same time")
    p.add_argument("--max-cost", type=float, default=3.0)
    p.add_argument("--freeze", action="store_true")
    p.add_argument("--resume", type=Path, default=None)
    asyncio.run(amain(p.parse_args()))


if __name__ == "__main__":
    main()
