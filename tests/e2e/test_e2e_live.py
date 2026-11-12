"""Layer 2 never starts an agent by default: a dry run prints the plan and the estimate, and a live run
refuses without its confirmations and without a pilot first."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentsim import corpus, decision, instances, live, phase4
from agentsim.sandbox import TIMED_OUT
from support.paths import REPO_ROOT

pytestmark = pytest.mark.e2e

TOOL = REPO_ROOT / "tools" / "e2e.py"


def _refuse(*args, **kwargs):
    raise AssertionError("a dry run or a refused run must not start a process")


@pytest.fixture
def no_process():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "run", _refuse)
        mp.setattr(subprocess, "Popen", _refuse)
        yield


@pytest.fixture
def tasks():
    instance_set, facts = phase4.load(instances.PROVISIONAL_FILE)
    return instances.tasks_of(instance_set, facts) + [t for t in corpus.tasks() if t.saver]


@pytest.fixture
def runs(tasks):
    return live.plan(tasks[:2], ["claude", "codex"], repeats=1)


def test_the_plan_crosses_tasks_agents_and_arms(runs):
    assert len(runs) == 2 * 2 * len(live.ARMS)
    engmem_run = next(r for r in runs if r.arm == "E" and r.agent.name == "claude")
    start = engmem_run.commands()[0]
    assert start[2].startswith("/engmem ") and start[2].endswith(live.ANSWER_SUFFIX)
    assert len(engmem_run.commands()) == 2
    grep_run = next(r for r in runs if r.arm == "G")
    assert live.NOTES_HINT in " ".join(grep_run.commands()[0])
    assert len(grep_run.commands()) == 1


def test_claude_sessions_capture_tool_calls_with_a_pinned_model_and_turn_cap(runs):
    argv = next(r for r in runs if r.agent.name == "claude").commands()[0]
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert argv[argv.index("--max-turns") + 1] == str(live.MAX_TURNS)
    assert argv[argv.index("--model") + 1] == live.DEFAULT_MODEL


def test_only_the_guided_arm_gets_the_advice_and_only_e_names_engmem(runs):
    for run in runs:
        prompt = run.commands()[-2 if run.arm == "E" else -1][2] if run.agent.name == "claude" else None
        if prompt is None:
            continue
        assert (live.GUIDANCE in prompt) is (run.arm == "G+")
        assert ("engmem" in prompt) is (run.arm == "E")
        assert ("notes" in prompt) is (run.arm in ("G", "G+"))


def test_every_block_holds_each_arm_once_in_a_seeded_shuffled_order(tasks):
    first = live.plan(tasks, ["claude"], repeats=2)
    again = live.plan(tasks, ["claude"], repeats=2)
    assert [(r.task.id, r.arm, r.repeat) for r in first] == [(r.task.id, r.arm, r.repeat) for r in again]
    blocks: dict = {}
    for run in first:
        blocks.setdefault((run.task.id, run.repeat), []).append(run.arm)
    for (task_id, _), arms in blocks.items():
        expected = live.HANDOFF_ARMS if task_id == "multi-agent" else live.ARMS
        assert sorted(arms) == sorted(expected)
    orders = {tuple(arms) for arms in blocks.values() if len(arms) == len(live.ARMS)}
    assert len(orders) > 1


def test_the_handoff_runs_an_earlier_session_for_e_and_gn_only(tasks):
    handoff = [t for t in tasks if t.saver]
    by_arm = {r.arm: r for r in live.plan(handoff, ["claude"], repeats=1)}
    assert set(by_arm) == set(live.HANDOFF_ARMS)
    assert len(by_arm["E"].commands()) == 4 and len(by_arm["GN"].commands()) == 2
    assert live.NOTES_INSTRUCTION in by_arm["GN"].commands()[0][2]
    assert "Write" in by_arm["GN"].commands()[0][-1] and "Write" not in by_arm["GN"].commands()[1][-1]
    assert len(by_arm["G"].commands()) == 1 and len(by_arm["N"].commands()) == 1


def test_a_dry_run_prints_every_stage_and_starts_nothing(no_process, tasks, tmp_path):
    text = live.dry_run(tasks, ["claude"], live.DEFAULT_MODEL, tmp_path / "out")
    assert "DRY RUN: no agent is started" in text
    for stage in live.STAGES:
        assert f"| {stage} |" in text
    assert f"confirmatory at {live.PLANNED_PER_CLASS} per class" in text
    assert not (tmp_path / "out").exists()


def test_the_estimate_prices_every_session_from_the_stated_assumptions(tasks):
    runs = live.plan(tasks[:1], ["claude"], repeats=1)
    est = live.estimate(runs, live.DEFAULT_MODEL)
    prices = live.PRICES[live.DEFAULT_MODEL]
    answer = live.usd(live._assumed_usage(live.ANSWER_SESSION), prices)
    save = live.usd(live._assumed_usage(live.SAVE_SESSION), prices)
    plain = live.usd(live._assumed_usage(live.NO_HISTORY_SESSION), prices)
    assert est["sessions"] == 5
    assert est["usd"] == round(3 * answer + save + plain, 2)
    assert live.estimate(runs, "unknown-model")["usd"] is None


def test_a_live_run_without_confirmation_is_refused(no_process, runs, tmp_path):
    with pytest.raises(live.LiveRunRefused, match="--i-accept-the-cost"):
        live.execute(runs, tmp_path, confirmed=False, pilot=True, pass_env=[])
    assert list(tmp_path.iterdir()) == []


def test_provisional_instances_never_run_beyond_the_pilot(no_process, runs, tmp_path):
    _pilot(tmp_path, [{"arm": arm, "start": GOOD_CALL, "save": GOOD_CALL} for arm in live.ARMS])
    with pytest.raises(live.LiveRunRefused, match="provisional"):
        live.execute(runs, tmp_path, confirmed=True, pilot=False, pass_env=[], provisional=True)


def test_a_full_run_without_a_pilot_is_refused(no_process, runs, tmp_path):
    with pytest.raises(live.LiveRunRefused, match="pilot"):
        live.execute(runs, tmp_path, confirmed=True, pilot=False, pass_env=[])
    assert list(tmp_path.iterdir()) == []


def test_the_command_line_defaults_to_a_dry_run(tmp_path):
    out = tmp_path / "live"
    done = subprocess.run(
        [sys.executable, str(TOOL), "live", "--agents", "claude", "--out", str(out),
         "--instances", str(instances.PROVISIONAL_FILE)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "DRY RUN" in done.stdout and "dry run only" in done.stdout
    assert not out.exists()


def test_claude_usage_is_read_from_its_json_result():
    text = json.dumps({
        "type": "result",
        "session_id": "s-1",
        "duration_ms": 1200,
        "num_turns": 3,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
    })
    usage = live.claude_usage(text)
    assert usage["input_tokens"] == 10 and usage["cache_read_input_tokens"] == 100
    assert usage["duration_ms"] == 1200 and usage["num_turns"] == 3
    assert live.claude_session(text) == "s-1"


def test_codex_usage_sums_each_turn_and_ignores_other_lines():
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t-1"}),
        "not json",
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 2}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "cached_input_tokens": 4}}),
    ]
    usage = live.codex_usage("\n".join(lines))
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 2 and usage["cached_input_tokens"] == 4
    assert live.codex_session("\n".join(lines)) == "t-1"


def test_no_report_means_no_numbers():
    assert live.claude_usage("") == {}
    assert live.codex_usage("plain text") == {}


def _pilot(tmp_path, records) -> Path:
    path = tmp_path / live.PILOT_FILE
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


GOOD_CALL = {"returncode": 0, "usage": {"input_tokens": 1}}
GOOD_SAVE = GOOD_CALL | {"new_docs": 1}


def _good(arm: str, agent: str = "claude", **changes) -> dict:
    record = {"agent": agent, "arm": arm, "start": GOOD_CALL, "model": live.DEFAULT_MODEL,
              "model_reported": live.DEFAULT_MODEL, "answer": "64 connections", "violations": [],
              "engmem_calls": 2 if arm == "E" else 0}
    if arm == "E":
        record["save"] = GOOD_SAVE
    return record | changes


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        ([_good("N", start={"returncode": 1, "usage": {"input_tokens": 1}})], "exited 1"),
        ([_good("N", start={"returncode": 0, "usage": {}})], "no usage"),
        ([_good("E", save=None)], "no save call"),
        ([_good("E", save={"returncode": 2, "usage": {"input_tokens": 1}})], "exited 2"),
        ([_good("G", model_reported="claude-other")], "not the pinned"),
        ([_good("G", answer="")], "no ANSWER"),
        ([_good("G", violations=["Read outside the sandbox: /etc/passwd"])], "outside its bounds"),
        ([_good("E", save=GOOD_CALL | {"new_docs": 0})], "saved no new document"),
        ([_good("E", engmem_calls=0)], "no engmem search or read call"),
        ([], "no run"),
    ],
    ids=["start-failed", "no-usage", "save-missing", "save-failed", "other-model", "no-answer", "violation",
         "save-wrote-nothing", "no-engmem-call", "empty"],
)
def test_a_failed_pilot_does_not_unlock_the_full_run(no_process, runs, tmp_path, records, reason):
    _pilot(tmp_path, records)
    assert reason in live.pilot_problem(tmp_path / live.PILOT_FILE)
    with pytest.raises(live.LiveRunRefused, match="successful pilot"):
        live.execute(runs, tmp_path, confirmed=True, pilot=False, pass_env=[])


def test_a_pilot_unlocks_only_the_agent_and_arms_it_ran(tmp_path):
    _pilot(tmp_path, [_good("E"), _good("G")])
    assert live.pilot_problem(tmp_path / live.PILOT_FILE, {("claude", "E"), ("claude", "G")}) is None
    assert "no claude/G+ run" in live.pilot_problem(tmp_path / live.PILOT_FILE, {("claude", "G+")})
    assert "no codex/E run" in live.pilot_problem(tmp_path / live.PILOT_FILE, {("codex", "E")})


def test_stream_json_yields_the_tool_calls_the_model_and_the_final_answer():
    events = [
        {"type": "system", "subtype": "init", "model": "claude-x", "session_id": "s-2"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/passwd"}},
        ]}},
        {"type": "result", "subtype": "success", "result": "ANSWER: 64 connections\nSOURCES: a",
         "usage": {"input_tokens": 3}},
    ]
    text = "\n".join(json.dumps(e) for e in events)
    assert live.claude_model(text) == "claude-x"
    assert live.claude_tool_calls(text) == [{"name": "Read", "input": {"file_path": "/etc/passwd"}}]
    assert live.claude_final_text(text).startswith("ANSWER: 64")
    assert live.claude_usage(text)["input_tokens"] == 3


def test_a_session_cut_at_max_turns_has_no_final_message_and_is_never_graded_from_its_transcript(tasks):
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ANSWER: 64 connections"}]}},
        {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "ANSWER: 64 connections",
         "usage": {"input_tokens": 9}},
    ]
    text = "\n".join(json.dumps(e) for e in events)
    assert live.claude_final_text(text) == ""
    task = next(t for t in tasks if t.id == "c04-identifier")
    graded = live.grade_record(task, live.claude_final_text(text))
    assert graded["correct"] is False and graded["answer"] == "" and graded["stale_avoided"] is None


def test_codex_events_yield_the_last_agent_message_its_commands_and_its_model():
    events = [
        {"type": "thread.started", "thread_id": "t-9", "model": "codex-y"},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "rg pool notes"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "thinking"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ANSWER: 64 connections\nSOURCES: x"}},
        {"type": "turn.completed", "usage": {"input_tokens": 4}},
    ]
    text = "\n".join(json.dumps(e) for e in events)
    assert live.codex_final_text(text).startswith("ANSWER: 64")
    assert live.codex_tool_calls(text) == [{"name": "Bash", "input": {"command": "rg pool notes"}}]
    assert live.codex_model(text) == "codex-y"
    assert live.codex_final_text(json.dumps({"type": "turn.completed"})) == ""


@pytest.mark.parametrize(
    ("call", "count"),
    [
        ({"name": "Bash", "input": {"command": "engmem search pool size"}}, 1),
        ({"name": "Bash", "input": {"command": "engmem mode show && engmem read x --role primer"}}, 1),
        ({"name": "Bash", "input": {"command": "rg pool notes"}}, 0),
        ({"name": "mcp__engmem__engmem_search", "input": {"query": "pool"}}, 1),
        ({"name": "engmem_read", "input": {"id": "x"}}, 1),
        ({"name": "Read", "input": {"file_path": "store/sessions/x.md"}}, 0),
    ],
    ids=["cli-search", "chained-read", "grep", "mcp-prefixed", "mcp", "file-read"],
)
def test_engmem_calls_count_cli_and_mcp_searches_and_reads_only(call, count):
    assert live.engmem_calls([call]) == count


def test_codex_runs_without_a_tool_restriction_and_the_dry_run_says_so(tasks, tmp_path):
    assert live.AGENTS["claude"].tools_restricted and not live.AGENTS["codex"].tools_restricted
    text = live.dry_run(tasks, ["claude", "codex"], live.DEFAULT_MODEL, tmp_path / "out")
    assert "WARNING: codex runs without a tool restriction" in text
    assert "WARNING: claude" not in text


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "sandboxes" / "1"
    cwd = root / "work" / "repo"
    store = root / "store"
    (store / "sessions").mkdir(parents=True)
    cwd.mkdir(parents=True)
    (tmp_path / "sandboxes" / "10").mkdir()
    return root, cwd, store


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ({"name": "Read", "input": {"file_path": "README.md"}}, []),
        ({"name": "Bash", "input": {"command": "engmem read x --role primer"}}, []),
        ({"name": "Bash", "input": {"command": "git grep --no-index pool ../../../corpus"}}, ["outside the sandbox"]),
        ({"name": "Read", "input": {"file_path": "{sibling}/a.md"}}, ["outside the sandbox"]),
        ({"name": "Bash", "input": {"command": "cat {store}/sessions/x.md"}}, ["read the store directly"]),
        ({"name": "Bash", "input": {"command": "grep -r pool ../../store/sessions/"}}, ["read the store directly"]),
        ({"name": "Bash", "input": {"command": "engmem search pool && cat ../../store/sessions/x.md"}},
         ["read the store directly"]),
        ({"name": "Grep", "input": {"pattern": "pool", "path": "../../.."}}, ["outside the sandbox"]),
    ],
    ids=["inside", "engmem", "relative-up", "sibling-prefix", "absolute-store", "relative-store", "chained",
         "tool-path"],
)
def test_each_way_out_of_the_sandbox_is_logged(box, call, expected):
    root, cwd, store = box
    sibling = (root.parent / "10").as_posix()
    given = {k: v.format(sibling=sibling, store=store.as_posix()) for k, v in call["input"].items()}
    found = live.read_violations([{"name": call["name"], "input": given}], root, cwd, store)
    assert [next(e for e in expected if e in f) for f in found] == expected


def test_a_non_e_arm_reading_the_notes_is_not_a_store_read(box):
    root, cwd, _ = box
    calls = [{"name": "Bash", "input": {"command": "cat ../../store/sessions/x.md"}}]
    assert live.read_violations(calls, root, cwd, None) == []


def test_the_live_corpus_keeps_no_answer_key_and_the_default_out_is_outside_the_repository(tmp_path):
    live.prepare_corpus(tmp_path / "corpus", "4kb")
    assert not (tmp_path / "corpus" / "tasks.json").exists() and not (tmp_path / "corpus" / "facts.json").exists()
    assert any((tmp_path / "corpus" / "base" / "sessions").glob("*.md"))
    assert not live.default_out("x").is_relative_to(REPO_ROOT.resolve())


def _live_record(task: str, arm: str, *, correct: bool, final: bool = True, usd: float | None = 0.4,
                 usage: dict | None = None, started: bool = True) -> dict:
    record = {"task": task, "scenario_class": 4, "arm": arm, "agent": "claude", "correct": correct,
              "final_message": final}
    if started:
        record["start"] = {"returncode": 0 if final else 1, "usd": usd,
                           "usage": {"input_tokens": 5} if usage is None else usage}
    return record


def test_a_run_without_a_final_message_counts_as_incorrect_at_the_usd_it_reported():
    runs = [
        _live_record("a", "E", correct=True), _live_record("b", "E", correct=False, final=False, usd=0.9),
        _live_record("a", "G+", correct=True), _live_record("b", "G+", correct=True),
    ]
    rows, left_out = live.analysis_records(runs)
    e_rows = [r for r in rows if r["arm"] == "E"]
    assert [(r["instance"], r["correct"], r["usd"]) for r in e_rows] == [("a", True, 0.4), ("b", False, 0.9)]
    assert left_out == []
    (family,) = decision.analyse(rows)
    assert family.diff == -50.0


def test_only_a_run_that_never_started_leaves_the_analysis_and_an_unpriced_one_leaves_only_the_cost():
    runs = [
        _live_record("a", "E", correct=True),
        _live_record("b", "E", correct=False, started=False),
        _live_record("c", "G+", correct=False, final=False, usd=None, usage={}),
        {**_live_record("d", "E", correct=True), "scenario_class": 8},
    ]
    rows, left_out = live.analysis_records(runs)
    assert [(r["instance"], r["arm"], r["correct"], r["usd"]) for r in rows] == [
        ("a", "E", True, 0.4), ("c", "G+", False, None),
    ]
    assert [(e["instance"], e["reason"], e["scope"]) for e in left_out] == [
        ("b", "never started", "analysis"), ("c", "no usage reported", "cost"),
    ]
    assert live.excluded_per_arm(left_out, "analysis") == {"E": 1}
    assert live.excluded_per_arm(left_out, "cost") == {"G+": 1}


def _timed_out(task: str, arm: str, usage: dict) -> dict:
    record = _live_record(task, arm, correct=False, final=False, usd=None, usage=usage)
    record["start"] |= {"returncode": TIMED_OUT, "timed_out": True}
    return record


def test_a_timed_out_e_run_lowers_e_in_its_family_and_is_not_excluded_from_correctness():
    runs = [
        _live_record("a", "E", correct=True), _timed_out("b", "E", usage={}),
        _live_record("a", "G+", correct=True), _live_record("b", "G+", correct=True),
    ]
    rows, left_out = live.analysis_records(runs)
    assert [(r["instance"], r["correct"]) for r in rows if r["arm"] == "E"] == [("a", True), ("b", False)]
    assert live.excluded_per_arm(left_out, "analysis") == {}
    assert [(e["instance"], e["reason"]) for e in left_out] == [("b", "timed out with no usage")]
    (family,) = decision.analyse(rows)
    assert family.diff == -50.0 and family.ratio == 1.0


def test_a_timed_out_run_keeps_the_usage_its_partial_output_reported():
    runs = [_timed_out("b", "E", usage={"input_tokens": 9})]
    runs[0]["start"]["usd"] = 0.7
    rows, left_out = live.analysis_records(runs)
    assert rows == [{"family": "stale", "instance": "b", "arm": "E", "correct": False, "usd": 0.7}]
    assert left_out == []


def test_families_are_pooled_by_class_and_their_detectable_effect_shrinks_with_instances(tasks):
    assert [live.family_of(c) for c in (1, 2, 6, 7, 4, 10, 11, 12, 3, 13, 5, 8, 9)] == [
        "retrieval"] * 4 + ["stale"] * 4 + ["section"] * 2 + [None] * 3
    assert live.minimum_detectable_effect(40, 0.5) < live.minimum_detectable_effect(20, 0.5)
    assert 25 < live.minimum_detectable_effect(40, 0.5) < 27
    assert live.family_sizes(tasks) == {"retrieval": 8, "stale": 8, "section": 4}


@pytest.mark.parametrize(
    ("final", "correct"),
    [
        ("Plan...\nANSWER: cap it at 64 connections per instance\nSOURCES: notes/a.md", True),
        ("ANSWER: 64 connections, not the old 200 connections\nSOURCES: x", False),
        ("ANSWER: 200 connections\nSOURCES: x", False),
        ("64 connections but no answer line", False),
    ],
    ids=["right", "mentions-stale", "stale", "no-answer-line"],
)
def test_a_live_answer_is_graded_by_the_instance_regexes(tasks, final, correct):
    task = next(t for t in tasks if t.id == "c04-identifier")
    assert live.grade_record(task, final)["correct"] is correct


def test_the_audit_sample_is_a_seeded_tenth_with_a_blank_verdict(tmp_path):
    results = [{"task": f"t{i}", "arm": "E", "answer": "a", "correct": True} for i in range(25)]
    rows = live.write_audit_sample(results, tmp_path / "audit.jsonl")
    assert len(rows) == 3 and all(row["human_verdict"] is None for row in rows)
    assert rows == live.write_audit_sample(results, tmp_path / "again.jsonl")
    assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 3
