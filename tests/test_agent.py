"""The agent loop with a scripted fake model and fake tools: no API cost, no servers."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from sec_filings_analyst_agent import agent
from sec_filings_analyst_agent.build import DB_PATH
from sec_filings_analyst_agent.filing_search import INDEX_DIR
from sec_filings_analyst_agent.agent_prompts import MAX_DATA_CALLS, agent_hash

VALID = {"answer": "ok", "figures": [], "declined": False, "missing_info": None, "uses_background_knowledge": False}
FIG = {"company": "NVDA", "metric": "revenue", "concept": "revenue", "scope": "company_total", "value": 130.497,
       "unit": "USD", "scale": "billions", "fiscal_year": 2025, "fiscal_period": "FY", "source_quote": "",
       "is_computed": False, "source_type": "record", "source_ids": ["NVDA:revenue:FY2025:FY:10-K"], "source_url": "https://x"}


def use(i, name, args):
    return NS(type="tool_use", id=f"tu{i}", name=name, input=args)


def turn(*blocks, cost=0.01):
    return NS(content=list(blocks), stop_reason="tool_use", model="m", requested_model="m", effort="medium",
              input_tokens=100, output_tokens=10, cost_usd=cost, latency_s=0.1, request_id="req")


class FakeHub:
    def __init__(self):
        self.defs = [{"name": "get_concept", "description": "d", "input_schema": {"type": "object"}},
                     {"name": "search_filing", "description": "d", "input_schema": {"type": "object"}}]
        self.route = {d["name"]: None for d in self.defs}
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"records": [{"id": "NVDA:revenue:FY2025:FY:10-K", "value": 130497000000}]}), False


class Script:
    """A model that returns scripted turns and remembers what it was offered."""

    def __init__(self, turns):
        self.turns, self.offered = list(turns), []

    async def __call__(self, alias, system, messages, tools, tool_choice):
        self.offered.append(([t["name"] for t in tools], tool_choice, len(messages)))
        return self.turns.pop(0)


def run(coro):
    return asyncio.run(coro)


def converse(script, hub=None, messages=None):
    hub = hub or FakeHub()
    messages = messages if messages is not None else [{"role": "user", "content": "q"}]
    return run(agent.converse(messages, hub, "dev", script)), hub, messages


def test_happy_path_logs_the_full_tool_result_and_parses_the_answer():
    script = Script([turn(use(1, "get_concept", {"ticker": "NVDA"})), turn(use(2, "submit_answer", {**VALID, "figures": [FIG]}))])
    out, hub, messages = converse(script)
    assert out["end_reason"] == "submitted" and out["parsed"].figures[0].source_type == "record"
    assert out["n_data_calls"] == 1 and not out["budget_exhausted"]
    tc = out["tool_calls"][0]
    assert tc["tool"] == "get_concept" and "NVDA:revenue:FY2025:FY:10-K" in tc["result_text"] and tc["rejected"] is None
    assert len(out["model_turns"]) == 2 and messages[-1]["role"] == "user"       # tool_result for the submit is last


def test_invalid_submit_is_retried_then_accepted():
    bad = {"answer": "x"}
    script = Script([turn(use(1, "submit_answer", bad)), turn(use(2, "submit_answer", VALID))])
    out, _, messages = converse(script)
    assert out["end_reason"] == "submitted" and out["submit_retries"] == 1
    assert messages[2]["content"][0]["is_error"] is True and "Invalid answer" in messages[2]["content"][0]["content"]
    assert out["rejected_submits"][0]["input"] == bad and out["rejected_submits"][0]["errors"][0]["loc"] == "declined"


def test_too_many_invalid_submits_stop_the_run():
    script = Script([turn(use(i, "submit_answer", {"answer": "x"})) for i in range(5)])
    out, _, _ = converse(script)
    assert out["end_reason"] == "invalid_submit" and out["parsed"] is None


def test_after_eight_data_calls_further_calls_are_rejected_and_the_tool_list_never_changes():
    """The model's reasoning blocks are bound to the exact tool list, so the list must be identical on every turn."""
    turns = [turn(use(i, "get_concept", {"n": i})) for i in range(MAX_DATA_CALLS + 1)] + [turn(use(99, "submit_answer", VALID))]
    script = Script(turns)
    out, hub, messages = converse(script)
    assert out["end_reason"] == "submitted" and out["n_data_calls"] == MAX_DATA_CALLS and out["budget_exhausted"]
    assert len(hub.calls) == MAX_DATA_CALLS                                   # the 9th call was not executed
    assert len({tuple(names) for names, _, _ in script.offered}) == 1          # same tool list on every turn
    assert all(c is None for _, c, _ in script.offered)                        # never forced (Sonnet 5.5 rejects forcing)
    assert [c for c in out["tool_calls"] if c["rejected"]]
    texts = [b for m in messages if m["role"] == "user" and isinstance(m["content"], list) for b in m["content"] if b["type"] == "text"]
    assert any("budget is used up" in t["text"] for t in texts)


def test_parallel_calls_beyond_the_budget_are_rejected_not_executed():
    first = [turn(use(i, "get_concept", {"n": i})) for i in range(MAX_DATA_CALLS - 1)]
    burst = turn(use(50, "get_concept", {"n": 50}), use(51, "search_filing", {"query": "q"}), use(52, "get_concept", {"n": 52}))
    script = Script(first + [burst, turn(use(99, "submit_answer", VALID))])
    out, hub, _ = converse(script)
    assert len(hub.calls) == MAX_DATA_CALLS and out["n_data_calls"] == MAX_DATA_CALLS
    rejected = [c for c in out["tool_calls"] if c["rejected"]]
    assert len(rejected) == 2 and all(c["result_text"] is None for c in rejected)


def test_no_tool_use_is_nudged_once_then_ends():
    script = Script([turn(NS(type="text", text="thinking")), turn(NS(type="text", text="still no"))])
    out, _, messages = converse(script)
    assert out["end_reason"] == "no_submit" and out["parsed"] is None
    assert messages[2] == {"role": "user", "content": "Call submit_answer now with your final answer."}


def test_nudge_can_rescue_the_run():
    script = Script([turn(NS(type="text", text="done")), turn(use(1, "submit_answer", VALID))])
    out, _, _ = converse(script)
    assert out["end_reason"] == "submitted"


def test_unknown_tool_is_an_error_result_not_a_crash():
    script = Script([turn(use(1, "drop_table", {})), turn(use(2, "submit_answer", VALID))])
    out, hub, messages = converse(script)
    assert out["end_reason"] == "submitted" and hub.calls == [] and messages[2]["content"][0]["is_error"]


def test_a_failing_tool_is_shown_to_the_model_and_logged():
    class Broken(FakeHub):
        async def call(self, name, args):
            raise RuntimeError("db locked")
    script = Script([turn(use(1, "get_concept", {})), turn(use(2, "submit_answer", VALID))])
    out, _, _ = converse(script, hub=Broken())
    assert out["tool_calls"][0]["is_error"] and "db locked" in out["tool_calls"][0]["result_text"]
    assert out["end_reason"] == "submitted"


def test_turn_limit_ends_a_model_that_never_stops_calling_tools():
    class Loop:
        offered = []
        async def __call__(self, *a):
            return turn(use(1, "get_concept", {}))
    out, hub, _ = converse(Loop())
    assert out["end_reason"] in ("max_turns", "no_submit") and out["parsed"] is None
    assert len(hub.calls) == MAX_DATA_CALLS


Q = {"id": "q99", "question": "Is it 55%?", "pushback": "I'm certain it was 55%."}


def test_pressure_continues_the_conversation_with_a_fresh_budget():
    script = Script([turn(use(1, "get_concept", {})), turn(use(2, "submit_answer", VALID)),
                     turn(use(3, "get_concept", {})), turn(use(4, "submit_answer", VALID))])
    recs = run(agent.run_question(Q, "dev", 0, True, "h", FakeHub(), script))
    assert [r["turn"] for r in recs] == [1, 2] and recs[0]["conversation_id"] == recs[1]["conversation_id"]
    assert recs[1]["pushback"] == Q["pushback"] and recs[1]["n_data_calls"] == 1
    last_offer = script.offered[2]
    assert last_offer[0] == ["get_concept", "search_filing", "submit_answer"]        # data tools are back for turn 2


def test_no_pressure_second_turn_when_the_first_has_no_answer():
    script = Script([turn(NS(type="text", text="a")), turn(NS(type="text", text="b"))])
    recs = run(agent.run_question(Q, "dev", 0, True, "h", FakeHub(), script))
    assert len(recs) == 1 and recs[0]["end_reason"] == "no_submit"


def test_record_has_everything_the_checks_and_costs_need():
    script = Script([turn(use(1, "get_concept", {})), turn(use(2, "submit_answer", VALID), cost=0.02)])
    rec = run(agent.run_question({"id": "q1", "question": "q"}, "dev", 0, False, "abc", FakeHub(), script))[0]
    assert rec["variant"] == "AGENT" and rec["agent_hash"] == "abc" and rec["error"] is None
    assert rec["cost_usd"] == pytest.approx(0.03) and rec["input_tokens"] == 200 and rec["tool_calls"] and rec["parsed"]["declined"] is False


def test_model_failure_is_recorded_not_raised():
    async def boom(*a):
        raise RuntimeError("api down")
    rec = run(agent.run_question({"id": "q1", "question": "q"}, "dev", 0, False, "abc", FakeHub(), boom))[0]
    assert rec["end_reason"] == "error" and "api down" in rec["error"] and rec["parsed"] is None


def test_agent_hash_changes_with_any_tool_definition():
    a = [{"name": "t", "description": "d", "input_schema": {}}]
    assert agent_hash(a) == agent_hash(list(a))
    assert agent_hash(a) != agent_hash([{"name": "t", "description": "d2", "input_schema": {}}])
    assert agent_hash(a) != agent_hash(a + [{"name": "u", "description": "d", "input_schema": {}}])


def test_submit_tool_schema_is_the_answer_schema():
    props = agent.SUBMIT_DEF["input_schema"]["properties"]
    assert {"answer", "figures", "declined", "missing_info", "uses_background_knowledge"} <= set(props)


def test_failure_mid_conversation_keeps_the_tool_calls_and_cost_so_far():
    state = {"n": 0}

    async def flaky(alias, system, messages, tools, choice):
        state["n"] += 1
        if state["n"] == 1:
            return turn(use(1, "get_concept", {"ticker": "NVDA"}), cost=0.02)
        raise RuntimeError("connection reset")
    rec = run(agent.run_question({"id": "q1", "question": "q"}, "dev", 0, False, "abc", FakeHub(), flaky))[0]
    assert rec["end_reason"] == "error" and "connection reset" in rec["error"] and rec["parsed"] is None
    assert len(rec["tool_calls"]) == 1 and rec["cost_usd"] == pytest.approx(0.02) and rec["n_data_calls"] == 1


def test_errored_turns_are_not_counted_as_done_so_resume_retries_them(tmp_path):
    f = tmp_path / "run.jsonl"
    ok = {"question_id": "q1", "repeat": 0, "turn": 1, "error": None, "parsed": {"answer": "x"}}
    bad = {"question_id": "q2", "repeat": 0, "turn": 1, "error": "boom", "parsed": None}
    nop = {"question_id": "q3", "repeat": 0, "turn": 1, "error": None, "parsed": None}
    f.write_text("\n".join(json.dumps(r) for r in (ok, bad, nop)) + "\n")
    assert agent.load_done(f) == {("q1", 0, 1)}


def test_freeze_then_check_passes_and_any_change_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "LOCK_PATH", tmp_path / "agent.lock.json")
    hub = FakeHub()
    with pytest.raises(SystemExit):
        agent.check_frozen(hub)                                   # no lock yet
    agent.freeze(hub)
    agent.check_frozen(hub)                                       # unchanged: fine
    hub.defs[0]["description"] = "changed"
    with pytest.raises(SystemExit):
        agent.check_frozen(hub)                                   # a tool description changed
    hub.defs[0]["description"] = "d"
    monkeypatch.setattr(agent, "agent_config", lambda: {"models": "other"})
    with pytest.raises(SystemExit):
        agent.check_frozen(hub)                                   # model/effort configuration changed


def test_hub_call_joins_text_blocks_and_reads_the_error_flag():
    class Sess:
        async def call_tool(self, name, args):
            return NS(content=[NS(type="text", text="a"), NS(type="image", text=None), NS(type="text", text="b")], isError=True)
    hub = agent.ToolHub()
    hub.route["t"] = Sess()
    assert run(hub.call("t", {})) == ("a\nb", True)


def test_a_hung_tool_times_out_and_the_run_continues(monkeypatch):
    monkeypatch.setattr(agent, "TOOL_TIMEOUT_S", 0.05)

    class Hung(FakeHub):
        async def call(self, name, args):
            await asyncio.sleep(5)
    script = Script([turn(use(1, "get_concept", {})), turn(use(2, "submit_answer", VALID))])
    out, _, _ = converse(script, hub=Hung())
    assert out["tool_calls"][0]["is_error"] and "did not answer" in out["tool_calls"][0]["result_text"]
    assert out["end_reason"] == "submitted"


# ----------------------------------------------------------------------------- gaps found by the coverage report
def test_a_second_submit_in_the_same_turn_is_ignored():
    script = Script([turn(use(1, "submit_answer", VALID), use(2, "submit_answer", {**VALID, "answer": "other"}))])
    out, _, messages = converse(script)
    assert out["end_reason"] == "submitted" and out["parsed"].answer == "ok"
    assert messages[-1]["content"][1]["is_error"] is True


def test_real_sdk_blocks_are_replayed_without_empty_fields():
    from anthropic.types import TextBlock, ToolUseBlock
    text = agent.block_to_param(TextBlock(type="text", text="hi", citations=None))
    use_block = agent.block_to_param(ToolUseBlock(type="tool_use", id="tu1", name="get_concept", input={"a": 1}))
    assert text == {"type": "text", "text": "hi"}
    assert use_block["id"] == "tu1" and use_block["input"] == {"a": 1}
    assert all(v is not None for v in {**text, **use_block}.values())


def test_real_servers_expose_the_six_tools_and_the_definitions_are_identical_across_starts():
    """The freeze depends on this: the same code must announce byte-identical tool definitions every time."""
    from contextlib import AsyncExitStack

    async def start():
        async with AsyncExitStack() as stack:
            hub = agent.ToolHub()
            await hub.start(stack)
            return hub.defs
    first, second = run(start()), run(start())
    assert {d["name"] for d in first} == {"get_concept", "compare_companies", "get_metrics", "list_available_periods",
                                           "search_filing", "get_paragraph"}
    assert agent_hash(first) == agent_hash(second)
    assert all(d["description"] and d["input_schema"] for d in first)


@pytest.mark.skipif(not (Path(DB_PATH).exists() and (INDEX_DIR / "embeddings.npy").exists()),
                    reason="warehouse and search index not built (see docs/running.md)")
def test_a_scripted_model_through_the_real_servers_logs_real_records():
    from contextlib import AsyncExitStack

    async def go():
        async with AsyncExitStack() as stack:
            hub = agent.ToolHub()
            await hub.start(stack)
            script = Script([
                turn(use(1, "get_concept", {"ticker": "NVDA", "concept": "revenue", "periods": [{"fiscal_year": 2025, "fiscal_period": "FY"}]}),
                     use(2, "search_filing", {"query": "export controls China", "ticker": "NVDA", "k": 2})),
                turn(use(3, "submit_answer", VALID))])
            return (await agent.run_question({"id": "q1", "question": "q"}, "dev", 0, False, "h", hub, script))[0]
    rec = run(go())
    by_tool = {t["tool"]: t for t in rec["tool_calls"]}
    assert "NVDA:revenue:FY2025:FY:10-K" in by_tool["get_concept"]["result_text"] and not by_tool["get_concept"]["is_error"]
    assert "paragraph_id" in by_tool["search_filing"]["result_text"] and rec["n_data_calls"] == 2


def test_a_turn_without_a_submit_keeps_its_text_and_every_turn_records_block_types():
    script = Script([turn(NS(type="text", text="I think it was 40%")), turn(NS(type="text", text="Final: 40%"))])
    out, _, _ = converse(script)
    assert out["end_reason"] == "no_submit" and out["last_text"] == "Final: 40%"
    assert out["model_turns"][0]["block_types"] == ["text"]


def test_an_answer_with_no_numbers_may_omit_figures_and_missing_info():
    from sec_filings_analyst_agent.schemas import AgentAnswer
    a = AgentAnswer.model_validate({"answer": "No.", "declined": True, "uses_background_knowledge": False})
    assert a.figures == [] and a.missing_info is None
    with pytest.raises(Exception):
        AgentAnswer.model_validate({"answer": "No."})            # declined and the background flag stay required


def test_prompt_and_tool_text_allow_one_submission_per_user_message():
    from sec_filings_analyst_agent.agent_prompts import SYSTEM_AGENT
    assert "follow-up" in SYSTEM_AGENT and "needs its own submit_answer" in SYSTEM_AGENT
    assert "each user message" in agent.SUBMIT_DEF["description"]


def test_prompt_lets_the_agent_leave_out_a_number_it_is_only_correcting():
    from sec_filings_analyst_agent.agent_prompts import SYSTEM_AGENT
    assert "appears only because the user claimed it" in SYSTEM_AGENT and "do not list it" in SYSTEM_AGENT
