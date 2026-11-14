"""Layer 2 never starts an agent by default: a dry run prints the plan and the estimate, and a live run
refuses without its confirmations and without a pilot first."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentsim import corpus, decision, instances, live, phase4
from agentsim.sandbox import TIMED_OUT, Completed, Sandbox
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
    confirm = live.usd(live._assumed_usage(live.CONFIRM_SESSION), prices)
    assert est["sessions"] == 5 + live.CONFIRMATIONS_ASSUMED
    assert est["usd"] == round(3 * answer + save + live.CONFIRMATIONS_ASSUMED * confirm + plain, 2)
    assert live.estimate(runs, "unknown-model")["usd"] is None


def test_a_live_run_without_confirmation_is_refused(no_process, runs, tmp_path):
    with pytest.raises(live.LiveRunRefused, match="--i-accept-the-cost"):
        live.execute(runs, tmp_path, confirmed=False, stage="pilot", pass_env=[])
    assert list(tmp_path.iterdir()) == []


def test_provisional_instances_never_run_beyond_the_pilot(no_process, runs, tmp_path):
    _pilot(tmp_path, [{"arm": arm, "start": GOOD_CALL, "save": GOOD_CALL} for arm in live.ARMS])
    with pytest.raises(live.LiveRunRefused, match="provisional"):
        live.execute(runs, tmp_path, confirmed=True, stage="mini", pass_env=[], provisional=True)


def test_a_full_run_without_a_pilot_is_refused(no_process, runs, tmp_path):
    with pytest.raises(live.LiveRunRefused, match="pilot"):
        live.execute(runs, tmp_path, confirmed=True, stage="mini", pass_env=[])
    assert list(tmp_path.iterdir()) == []


def test_the_command_line_writes_utf8_whatever_the_console_encoding(tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    done = subprocess.run(
        [sys.executable, str(TOOL), "live", "--agents", "claude", "--out", str(tmp_path / "live"),
         "--instances", str(instances.PROVISIONAL_FILE)],
        capture_output=True, env=env, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "`ANSWER: …`" in done.stdout.decode("utf-8")


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
GOOD_SAVE = GOOD_CALL | {"new_docs": 1, "final_message": True}


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
        ([_good("E", save=GOOD_CALL | {"new_docs": 1, "final_message": False})], "save ended with no final message"),
        ([_good("E", engmem_calls=0)], "no engmem search or read call"),
        ([], "no run"),
    ],
    ids=["start-failed", "no-usage", "save-missing", "save-failed", "other-model", "no-answer", "violation",
         "save-no-final-message", "no-engmem-call", "empty"],
)
def test_a_failed_pilot_does_not_unlock_the_full_run(no_process, runs, tmp_path, records, reason):
    _pilot(tmp_path, records)
    assert reason in live.pilot_problem(tmp_path / live.PILOT_FILE)
    with pytest.raises(live.LiveRunRefused, match="successful pilot"):
        live.execute(runs, tmp_path, confirmed=True, stage="mini", pass_env=[])


@pytest.fixture
def fake_runs():
    """`execute` with every run, the corpus and the CLI version faked; each run creates its root."""
    roots: list[Path] = []

    def one(run, root, generated, pass_env, version):
        root.mkdir(parents=True)
        roots.append(root)
        return _good(run.arm, run.agent.name) | {"task": run.task.id}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(live, "_execute_one", one)
        mp.setattr(live, "prepare_corpus", lambda root, size: root.mkdir(parents=True))
        mp.setattr(live, "_version", lambda agent: "test")
        mp.setattr(live, "_require_binaries", lambda runs: None)
        yield roots


def test_pilot_then_mini_in_one_out_dir_use_disjoint_sandboxes_and_results(fake_runs, runs, tmp_path):
    live.execute(runs, tmp_path, confirmed=True, stage="pilot", pass_env=[])
    pilot_roots = list(fake_runs)
    pilot_record = (tmp_path / live.PILOT_FILE).read_text(encoding="utf-8")
    live.execute(runs, tmp_path, confirmed=True, stage="mini", pass_env=[])
    mini_roots = fake_runs[len(pilot_roots):]
    assert len(mini_roots) == len(pilot_roots) == len(runs)
    assert not set(pilot_roots) & set(mini_roots)
    assert all(r.is_relative_to((tmp_path / "pilot").resolve()) for r in pilot_roots)
    assert all(r.is_relative_to((tmp_path / "mini").resolve()) for r in mini_roots)
    assert (tmp_path / live.PILOT_FILE).read_text(encoding="utf-8") == pilot_record
    assert json.loads((tmp_path / live.RESULTS_FILES["mini"]).read_text(encoding="utf-8"))
    assert len(set(live.RESULTS_FILES.values())) == len(live.STAGES)


@pytest.mark.parametrize("stage", live.STAGES)
def test_a_second_run_of_a_stage_into_the_same_out_dir_is_refused(fake_runs, runs, tmp_path, stage):
    if stage != "pilot":
        _pilot(tmp_path, [_good(arm, agent) for agent in ("claude", "codex") for arm in live.ARMS])
    live.execute(runs, tmp_path, confirmed=True, stage=stage, pass_env=[])
    results = (tmp_path / live.RESULTS_FILES[stage]).read_text(encoding="utf-8")
    with pytest.raises(live.LiveRunRefused, match="already exists"):
        live.execute(runs, tmp_path, confirmed=True, stage=stage, pass_env=[])
    assert len(fake_runs) == len(runs)
    assert (tmp_path / live.RESULTS_FILES[stage]).read_text(encoding="utf-8") == results


def test_a_stage_whose_sandboxes_exist_without_results_is_refused_before_any_run(fake_runs, runs, tmp_path):
    live.sandbox_roots(tmp_path, 1, stage="pilot")[0].mkdir(parents=True)
    with pytest.raises(live.LiveRunRefused, match=r"sandboxes would be reused; after checking it, remove it, or start "
                       r"a new --out \(beyond the pilot, holding a copy of the pilot's pilot\.json\)"):
        live.execute(runs, tmp_path, confirmed=True, stage="pilot", pass_env=[])
    assert fake_runs == [] and not (tmp_path / live.PILOT_FILE).exists()


def test_a_stage_whose_sandboxes_were_removed_still_never_overwrites_its_results(fake_runs, runs, tmp_path):
    (tmp_path / live.RESULTS_FILES["pilot"]).write_text("[]", encoding="utf-8")
    with pytest.raises(live.LiveRunRefused, match="pilot.json already exists"):
        live.execute(runs, tmp_path, confirmed=True, stage="pilot", pass_env=[])
    assert fake_runs == [] and not (tmp_path / "pilot").exists()


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
        ({"name": "Bash", "input": {"command": "sed -n '/Decision Log/,/Search Keywords/p' notes/a.md"}}, []),
        ({"name": "Bash", "input": {"command": "sed -n '/Decision Log/,/Search Keywords/p' {sibling}/a.md"}},
         ["outside the sandbox"]),
        ({"name": "Bash", "input": {"command": "awk -F / '/Decision Log/,/Search/' notes/a.md {sibling}/a.md"}},
         ["outside the sandbox"]),
        ({"name": "Bash", "input": {"command": "grep -n '/api/v1/' notes/a.md | rg -e '/x/' -f {sibling}/p"}},
         ["outside the sandbox"]),
        ({"name": "Bash", "input": {"command": "rg --files {sibling}"}}, ["outside the sandbox"]),
        ({"name": "Glob", "input": {"pattern": "{sibling}/**/*.md"}}, ["outside the sandbox"]),
        ({"name": "Glob", "input": {"pattern": "notes/**/*.md"}}, []),
        ({"name": "Bash", "input": {"command": "S={sibling}; ls"}}, ["outside the sandbox"]),
        ({"name": "Bash", "input": {"command": "ls {sandboxes}/1*/work/repo && ls {root}/wo?k"}}, []),
        ({"name": "Bash", "input": {"command": "ls notes/*.none"}}, []),
        ({"name": "Bash", "input": {"command": "ls {sandboxes}/*"}}, ["outside the sandbox"]),
        ({"name": "Bash", "input": {"command": "ls /*"}}, ["outside the sandbox"]),
        ({"name": "Bash", "input": {"command": "cat {sibling}*/none*/x"}}, ["outside the sandbox"]),
        ({"name": "Glob", "input": {"pattern": "../../../*"}}, ["outside the sandbox"]),
        ({"name": "Bash", "input": {"command": "cat no*/../../../x"}}, ["as far as the pattern can be bounded"]),
    ],
    ids=["inside", "engmem", "relative-up", "sibling-prefix", "absolute-store", "relative-store", "chained",
         "tool-path", "sed-script", "sed-file-outside", "awk-file-outside", "grep-pattern-and-file-option",
         "rg-files", "glob-outside", "glob-inside", "assignment", "own-root-wildcard", "unmatched-glob-inside",
         "sibling-wildcard", "root-wildcard", "unmatched-glob-outside", "glob-tool-up", "unmatched-glob-climbing"],
)
def test_each_way_out_of_the_sandbox_is_logged(box, call, expected):
    root, cwd, store = box
    sibling = (root.parent / "10").as_posix()
    given = {k: v.format(sibling=sibling, store=store.as_posix(), root=root.as_posix(), sandboxes=root.parent.as_posix())
             for k, v in call["input"].items()}
    found = live.read_violations([{"name": call["name"], "input": given}], root, cwd, store)
    assert [next(e for e in expected if e in f) for f in found] == expected


def test_a_glob_over_the_match_limit_is_logged_even_inside_the_sandbox(box):
    root, cwd, _ = box
    for i in range(4):
        (cwd / f"f{i}.md").write_text("x", encoding="utf-8")
    calls = [{"name": "Bash", "input": {"command": "cat ./*.md"}}]
    assert live.read_violations(calls, root, cwd, None) == []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(live, "GLOB_LIMIT", 3)
        assert ["as far as the pattern can be bounded" in v for v in live.read_violations(calls, root, cwd, None)] == [True]


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
        ("ANSWER: 64 connections, replacing the old 200 connections\nSOURCES: x", True),
        ("ANSWER: 64 connections, or 200 connections under load\nSOURCES: x", False),
        ("ANSWER: 200 connections\nSOURCES: x", False),
        ("64 connections but no answer line", False),
    ],
    ids=["right", "stale-marked-old", "stale-as-current", "stale", "no-answer-line"],
)
def test_a_live_answer_is_graded_by_the_instance_regexes(tasks, final, correct):
    task = next(t for t in tasks if t.id == "c04-identifier")
    assert live.grade_record(task, final)["correct"] is correct


def _audit_tasks(tasks, results):
    task = next(t for t in tasks if t.id == "c04-identifier")
    return {record["task"]: task for record in results}


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_audit_sample_is_a_seeded_tenth_with_a_blank_verdict(tasks, tmp_path):
    results = [_run(f"t{i}", "E", correct=True, index=i) for i in range(25)]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    rows = live.write_audit_sample(results, _audit_tasks(tasks, results), tmp_path / "a")
    again = live.write_audit_sample(results, _audit_tasks(tasks, results), tmp_path / "b")
    assert len(rows) == 3 and all(row["human_verdict"] is None for row in rows)
    assert [r["task"] for r in rows] == [r["task"] for r in again]
    assert len(_rows(tmp_path / "a" / live.AUDIT_FILE)) == len(_rows(tmp_path / "a" / live.AUDIT_KEY_FILE)) == 3


@pytest.mark.parametrize(
    ("final", "caveated"),
    [
        ("ANSWER: 64 connections, replacing the old 200 connections\nSOURCES: x", True),
        ("ANSWER: 64 connections\nSOURCES: x", False),
        ("ANSWER: 200 connections\nSOURCES: x", False),
    ],
)
def test_a_live_run_records_whether_a_caveat_changed_its_stale_verdict(tasks, final, caveated):
    task = next(t for t in tasks if t.id == "c04-identifier")
    assert live.grade_record(task, final)["stale_caveated"] is caveated


@pytest.mark.parametrize(
    ("final", "flagged"),
    [
        ("ANSWER: 64 connections. It was 200 connections until a commit changed it.\nSOURCES: x", True),
        ("ANSWER: 64 connections, replacing the old 200 connections\nSOURCES: x", True),
        ("ANSWER: 200 connections\nSOURCES: x", False),
        ("ANSWER: 64 connections\nSOURCES: x", False),
        ("no answer line, 64 connections and 200 connections", False),
        ("ANSWER: The pool cap changed; the 200 connections were replaced.\nSOURCES: x", True),
    ],
    ids=["both-uncaveated", "both-caveated", "stale-only", "current-only", "no-answer", "caveat-only"],
)
def test_a_run_naming_both_the_current_and_the_stale_value_needs_a_human(tasks, final, flagged):
    task = next(t for t in tasks if t.id == "c04-identifier")
    assert live.grade_record(task, final)["needs_human"] is flagged


def _run(task, arm, *, correct, needs_human=False, repeat=1, index=0, stage="mini"):
    return {"run_id": f"{stage}-{index:04d}", "task": task, "agent": "claude", "arm": arm, "repeat": repeat,
            "scenario_class": 4, "answer": f"answer {index}", "correct": correct, "needs_human": needs_human,
            "stale_avoided": True, "final_message": True, "start": {"usd": 0.1, "usage": {"input_tokens": 1}}}


def test_every_run_that_needs_a_human_joins_the_audit_sample_blind_to_arm_agent_and_grade(tasks, tmp_path):
    results = [_run(f"t{i}", "E", correct=True, needs_human=i in (4, 17), index=i) for i in range(25)]
    rows = live.write_audit_sample(results, _audit_tasks(tasks, results), tmp_path)
    key = {row["id"]: row["run_id"] for row in _rows(tmp_path / live.AUDIT_KEY_FILE)}
    assert {"mini-0004", "mini-0017"} <= set(key.values())
    assert len(set(key.values())) == len(rows) >= 3
    task = next(t for t in tasks if t.id == "c04-identifier")
    for row in _rows(tmp_path / live.AUDIT_FILE):
        assert set(row) == {"id", "task", "question", "required", "stale", "answer", "human_verdict"}
        assert row["question"] == task.description and row["required"] == list(task.required)
        assert row["stale"] == list(task.stale_patterns)
        assert row["answer"] == f"answer {key[row['id']].split('-')[1].lstrip('0') or '0'}"
        assert not any(name in row for name in ("agent", "arm", "run_id", "correct", "needs_human"))
    every = [_run(f"t{i}", "E", correct=True, needs_human=True, index=i) for i in range(25)]
    (tmp_path / "all").mkdir()
    live.write_audit_sample(every, _audit_tasks(tasks, every), tmp_path / "all")
    shuffled = [key_row["run_id"] for key_row in _rows(tmp_path / "all" / live.AUDIT_KEY_FILE)]
    assert len(shuffled) == 25 and shuffled[3:] != sorted(shuffled[3:])


def _adjudicate(directory, verdicts):
    """Fill in the human verdicts of an audit sample, by run id, through its key."""
    runs = {row["id"]: row["run_id"] for row in _rows(directory / live.AUDIT_KEY_FILE)}
    rows = [row | {"human_verdict": verdicts.get(runs[row["id"]])} for row in _rows(directory / live.AUDIT_FILE)]
    (directory / live.AUDIT_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_a_verdict_on_an_opaque_row_lands_on_its_run_and_only_flagged_runs_take_it(tasks, tmp_path):
    results = [_run("a", "E", correct=False, needs_human=True, index=0),
               _run("b", "G", correct=True, needs_human=True, index=1),
               _run("c", "N", correct=False, index=2)]
    live.write_audit_sample(results, _audit_tasks(tasks, results), tmp_path, share=1.0)
    _adjudicate(tmp_path, {"mini-0000": True, "mini-0002": True})
    applied, pending = live.apply_verdicts(results, live.human_verdicts(tmp_path / live.AUDIT_FILE))
    assert [r["correct"] for r in applied] == [True, True, False]
    assert applied[0]["graded_by"] == "human" and "graded_by" not in applied[2]
    assert [r["task"] for r in pending] == ["b"]
    rows = _rows(tmp_path / live.AUDIT_FILE)
    (tmp_path / live.AUDIT_FILE).write_text(json.dumps(rows[0] | {"human_verdict": "yes"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="true, false or null"):
        live.human_verdicts(tmp_path / live.AUDIT_FILE)
    (tmp_path / live.AUDIT_FILE).write_text(json.dumps(rows[0] | {"id": "forged"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not in audit-key"):
        live.human_verdicts(tmp_path / live.AUDIT_FILE)


def test_an_audit_sample_of_another_stage_is_refused(tasks, tmp_path):
    mini = [_run("a", "E", correct=False, needs_human=True, index=0, stage="mini")]
    confirmatory = [_run("a", "E", correct=False, needs_human=True, index=0, stage="confirmatory")]
    live.write_audit_sample(mini, _audit_tasks(tasks, mini), tmp_path)
    with pytest.raises(ValueError, match="do not hold: mini-0000"):
        live.apply_verdicts(confirmatory, live.human_verdicts(tmp_path / live.AUDIT_FILE))
    path = tmp_path / live.RESULTS_FILES["confirmatory"]
    path.write_text(json.dumps(confirmatory), encoding="utf-8")
    done = subprocess.run([sys.executable, str(TOOL), "analyse", "--results", str(path),
                           "--audit", str(tmp_path / live.AUDIT_FILE)], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 2 and "mini-0000" in done.stderr


def test_analyse_shows_no_decision_while_a_flagged_run_awaits_a_verdict(tasks, tmp_path):
    results = [_run("a", "E", correct=False, needs_human=True, index=0), _run("a", "G+", correct=True, index=1)]
    path = tmp_path / live.RESULTS_FILES["mini"]
    path.write_text(json.dumps(results), encoding="utf-8")
    assert live.audit_path(path) == tmp_path / "mini" / live.AUDIT_FILE
    command = [sys.executable, str(TOOL), "analyse", "--results", str(path)]
    pending = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    assert pending.returncode == 3
    assert f"1 run(s) naming both the current and the stale value await a human_verdict in {live.audit_path(path)}" \
        in pending.stdout
    for leak in ("## claude", "E − G+", "E - G+", "engmem better", "grep better", "inconclusive", "Tie", "per arm",
                 "General claims", "E: 1"):
        assert leak not in pending.stdout
    shown = subprocess.run([*command, "--provisional"], capture_output=True, text=True, encoding="utf-8")
    assert shown.returncode == 3
    assert "PROVISIONAL: 1 run(s) in all await a human verdict\n## claude" in shown.stdout
    assert "PROVISIONAL General claims" in shown.stdout and "E: 1" not in shown.stdout.split("## claude")[0]
    (tmp_path / "mini").mkdir()
    live.write_audit_sample(results, _audit_tasks(tasks, results), tmp_path / "mini")
    _adjudicate(tmp_path / "mini", {"mini-0000": True})
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0 and "PROVISIONAL" not in done.stdout and "## claude" in done.stdout


def test_every_live_result_carries_a_run_id_of_its_stage(fake_runs, runs, tmp_path):
    live.execute(runs, tmp_path, confirmed=True, stage="pilot", pass_env=[])
    records = json.loads((tmp_path / live.PILOT_FILE).read_text(encoding="utf-8"))
    assert [r["run_id"] for r in records] == [f"pilot-{i:04d}" for i in range(len(runs))]
    key = _rows(tmp_path / "pilot" / live.AUDIT_KEY_FILE)
    assert {row["run_id"] for row in key} <= {r["run_id"] for r in records}


PILOT_N_HANDOFF_ANSWER = (
    "This repo has no gateway client yet, so this is a general design. Generate one idempotency key per order "
    "before the first attempt and send the same key on every retry, with exponential backoff and jitter."
)


def test_the_handoff_is_graded_by_its_seeded_specifics_in_any_wording(tasks):
    task = next(t for t in tasks if t.saver)
    given = corpus.handoff_specifics()
    words = {94: "ninety-four"}
    assert given.minutes in words
    specific = (f"Send the token in the {given.header} header; intake keeps it {words[given.minutes]} minutes "
                f"in {given.table}.")
    assert live.grade_record(task, f"ANSWER: {specific}\nSOURCES: x")["correct"] is True
    assert live.grade_record(task, f"ANSWER: {PILOT_N_HANDOFF_ANSWER}\nSOURCES: x")["correct"] is False
    partial = f"Send the token in the {given.header} header for {given.minutes} minutes."
    assert live.grade_record(task, f"ANSWER: {partial}\nSOURCES: x")["correct"] is False


ACTIVE = "---\nid: 20261113-doc\nstatus: active\n---\n## Decision Log\n"
DRAFT = "---\nid: 20261113-doc\nstatus: draft\n---\n"
PREVIEW = "Here is the record:\n\n```markdown\n---\nid: 20261113-x\nstatus: active\n---\n## Decision Log\n```\n\nSave it?"
NO_DECISION = "This session made no decision; it is already stored in 20260312-ledger-sweeper-retry. Nothing saved."


class _AskingSandbox:
    """A headless agent whose save shows a preview and waits; it publishes only on the turns given."""

    def __init__(self, root: Path, publishes_on: tuple[str, ...], message: str = PREVIEW):
        self.store = root / "store"
        (self.store / "sessions").mkdir(parents=True)
        self.publishes_on = publishes_on
        self.message = message
        self.calls: list[list[str]] = []

    def run(self, argv, *, cwd=None, timeout=None, stdin=None):
        self.calls.append(argv)
        if argv[2] in self.publishes_on:
            (self.store / "sessions" / f"20261113-doc-{len(self.calls)}.md").write_text(ACTIVE, encoding="utf-8")
        event = {"type": "result", "subtype": "success", "is_error": False, "result": self.message,
                 "session_id": f"session-{len(self.calls)}", "usage": {"input_tokens": 1}}
        return Completed(json.dumps(event), "", 0, 0.1)


def _e_run(tasks) -> live.LiveRun:
    return live.LiveRun(tasks[0], live.AGENTS["claude"], "E", 1, live.DEFAULT_MODEL)


def test_a_save_that_waits_for_confirmation_publishes_after_the_simulated_yes(tasks, tmp_path):
    box = _AskingSandbox(tmp_path, publishes_on=(live.SIMULATED_USER_REPLY,))
    record = live._save(_e_run(tasks), box, tmp_path, "session-0")
    assert record["new_docs"] == 1 and len(record["confirmations"]) == 1
    save, reply = box.calls
    assert save[2] == "/engmem.save.quick" and save[save.index("--resume") + 1] == "session-0"
    assert reply[2] == live.SIMULATED_USER_REPLY and reply[reply.index("--resume") + 1] == "session-1"
    assert reply[reply.index("--allowedTools") + 1] == live.ENGMEM_TOOLS
    assert live._record_problem(_good("E", save=record)) is None


def test_a_save_that_publishes_at_once_gets_no_reply(tasks, tmp_path):
    box = _AskingSandbox(tmp_path, publishes_on=("/engmem.save.quick",))
    record = live._save(_e_run(tasks), box, tmp_path, "session-0")
    assert record["new_docs"] == 1 and record["confirmations"] == [] and len(box.calls) == 1


def test_an_earlier_save_that_still_writes_nothing_stops_after_the_bounded_replies_and_fails_the_gate(tasks, tmp_path):
    box = _AskingSandbox(tmp_path, publishes_on=())
    record = live._save(_e_run(tasks), box, tmp_path, "session-0")
    assert record["new_docs"] == 0 and len(record["confirmations"]) == live.CONFIRM_TURNS
    assert len(box.calls) == 1 + live.CONFIRM_TURNS and record["replies_stopped"] == "bound"
    problem = live._record_problem(_good("E", earlier={"start": GOOD_CALL, "save": record}))
    assert "earlier session saved no new document" in problem


def test_codex_replies_by_resuming_its_session():
    argv = live.AGENTS["codex"].reply("thread-1", live.SIMULATED_USER_REPLY, "gpt-x")
    assert argv[-3:] == ["resume", "thread-1", live.SIMULATED_USER_REPLY]


@pytest.mark.parametrize(
    ("earlier", "reason"),
    [
        ({"start": GOOD_CALL}, "no earlier save call"),
        ({"start": GOOD_CALL, "save": GOOD_CALL | {"new_docs": 0}}, "earlier session saved no new document"),
    ],
    ids=["no-save", "wrote-nothing"],
)
def test_a_handoff_whose_earlier_save_stored_nothing_fails_the_gate(earlier, reason):
    assert reason in live._record_problem(_good("E", earlier=earlier))
    assert live._record_problem(_good("E", earlier={"start": GOOD_CALL, "save": GOOD_SAVE})) is None
    assert live._record_problem(_good("GN", earlier={"start": GOOD_CALL})) is None


class _NoHistory:
    """A generated corpus with notes but no git history, enough to prepare a GN sandbox."""

    histories: dict = {}

    def __init__(self, notes: Path):
        self.notes = notes

    def store_for(self, task):
        return self.notes


def _gn_handoff(tasks) -> live.LiveRun:
    return live.LiveRun(next(t for t in tasks if t.saver), live.AGENTS["claude"], "GN", 1, live.DEFAULT_MODEL)


@pytest.mark.parametrize(
    "planted",
    ["home/.claude/projects/-work-intake-gateway/memory/MEMORY.md", "work/intake-gateway/CLAUDE.md",
     "home/.claude/CLAUDE.md"],
    ids=["auto-memory", "project-claude-md", "user-claude-md"],
)
def test_memory_the_earlier_session_left_refuses_the_finder_and_fails_the_gate(tasks, tmp_path, planted):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "20260101-a.md").write_text("# a\n", encoding="utf-8")
    root = live.sandbox_roots(tmp_path / "out", 1, stage="pilot")[0]

    def earlier(run, sandbox, checkout):
        target = sandbox.root / planted
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("X-Osprey-Twin, 94 minutes", encoding="utf-8")
        return {"start": GOOD_CALL}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(live, "_earlier_agent", earlier)
        record = live._execute_one(_gn_handoff(tasks), root, _NoHistory(notes), [], "test")
    assert "start" not in record and planted in record["refused"]
    assert record["earlier"]["memory_carried"] == [planted]
    assert "agent memory" in live._record_problem(record | {"model": live.DEFAULT_MODEL})
    assert "agent memory" in live.excluded(record)


def test_memory_present_before_the_earlier_session_is_not_counted_as_carried(tmp_path):
    sandbox = Sandbox.create(tmp_path / "box", inherit_env=False)
    checkout = sandbox.root / "work" / "repo"
    checkout.mkdir(parents=True)
    (checkout / "CLAUDE.md").write_text("engmem trigger rule\n", encoding="utf-8")
    before = live.memory_files(sandbox, checkout)
    assert live.carried_memory(sandbox, checkout, before) == []
    (checkout / "CLAUDE.md").write_text("engmem trigger rule\nheader X-Osprey-Twin\n", encoding="utf-8")
    assert live.carried_memory(sandbox, checkout, before) == ["work/repo/CLAUDE.md"]


def test_a_finder_reading_claude_project_memory_is_a_violation(box):
    root, cwd, _ = box
    memory = root / "home" / ".claude" / "projects" / "-x" / "memory" / "MEMORY.md"
    calls = [{"name": "Read", "input": {"file_path": memory.as_posix()}}]
    assert [v for v in live.read_violations(calls, root, cwd, None) if "agent memory" in v]



class _ThirdYesSandbox(_AskingSandbox):
    """A save that refuses a duplicate, shows a new preview, and publishes on the third reply."""

    def run(self, argv, *, cwd=None, timeout=None, stdin=None):
        done = super().run(argv, cwd=cwd, timeout=timeout, stdin=stdin)
        if len(self.calls) == 4:
            (self.store / "sessions" / "20261113-third.md").write_text(ACTIVE, encoding="utf-8")
        return done


def test_a_save_that_needs_three_confirmations_writes_its_document(tasks, tmp_path):
    box = _ThirdYesSandbox(tmp_path, publishes_on=())
    record = live._save(_e_run(tasks), box, tmp_path, "session-0")
    assert live.CONFIRM_TURNS == 3
    assert record["new_docs"] == 1 and len(record["confirmations"]) == 3


def test_only_the_earlier_handoff_save_must_write_a_document():
    declined = GOOD_CALL | {"new_docs": 0, "confirmations": [], "final_message": True}
    assert live._record_problem(_good("E", earlier={"start": GOOD_CALL, "save": GOOD_SAVE}, save=declined)) is None
    assert live._record_problem(_good("E", save=declined)) is None
    earlier_declined = _good("E", earlier={"start": GOOD_CALL, "save": GOOD_CALL | {"new_docs": 0}})
    assert "earlier session saved no new document" in live._record_problem(earlier_declined)


def test_a_lookup_save_that_declines_gets_no_reply_and_passes_the_gate(tasks, tmp_path):
    box = _AskingSandbox(tmp_path, publishes_on=(), message=NO_DECISION)
    record = live._save(_e_run(tasks), box, tmp_path, "session-0")
    assert len(box.calls) == 1 and record["confirmations"] == [] and record["replies_stopped"] == "no preview"
    assert record["new_docs"] == 0 and record["final_message"]
    assert live._record_problem(_good("E", save=record)) is None


@pytest.mark.parametrize(
    ("message", "preview"),
    [
        (PREVIEW, True),
        ("---\nid: 20261113-x\nstatus: active\n---\n## Decision Log\n- Decision: x\n\nSave it?", True),
        ("~~~\n---\nid: a\nstatus: draft\n---\n~~~", True),
        (NO_DECISION, False),
        ("```yaml\n---\nid: a\n---\n```", False),
        ("The record would have id: a and status: active.", False),
        ("Already stored.\n\n---\nid: a\nstatus: active\n---\nNothing saved.", False),
    ],
    ids=["fenced", "top", "tilde-fence", "no-decision", "no-status", "prose", "rule-not-front-matter"],
)
def test_a_preview_is_front_matter_with_id_and_status(message, preview):
    assert live.shows_preview(message) is preview


def _stream(calls: list[dict], answer: str) -> str:
    events = [{"type": "system", "model": live.DEFAULT_MODEL, "session_id": "s"}]
    events += [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": c["name"], "input": c["input"]}]}}
               for c in calls]
    events.append({"type": "result", "subtype": "success", "is_error": False, "result": f"ANSWER: {answer}\nSOURCES: none",
                   "session_id": "s", "usage": {"input_tokens": 1}})
    return "\n".join(json.dumps(e) for e in events)


def _fake_agent_reading_memory(mp):
    """Real git for the checkout; the agent itself only globs its own memory folder and answers."""
    real = Sandbox.run

    def run(self, argv, **kwargs):
        if argv[0] != "claude":
            return real(self, argv, **kwargs)
        memory = next(self.home.glob(".claude/projects/*/memory"), self.home / ".claude" / "projects" / "-w" / "memory")
        return Completed(_stream([{"name": "Glob", "input": {"pattern": "*.md", "path": memory.as_posix()}}], "none"),
                         "", 0, 0.1)

    mp.setattr(Sandbox, "run", run)


def test_a_single_session_reading_its_own_empty_memory_is_a_note_not_a_violation(tasks, tmp_path):
    run = live.LiveRun(tasks[0], live.AGENTS["claude"], "N", 1, live.DEFAULT_MODEL)
    root = live.sandbox_roots(tmp_path / "out", 1, stage="pilot")[0]
    with pytest.MonkeyPatch.context() as mp:
        _fake_agent_reading_memory(mp)
        record = live._execute_one(run, root, _NoHistory(tmp_path), [], "test")
    assert record["violations"] == [] and record["memory_reads"]
    assert live._record_problem(record) is None


def test_a_session_whose_memory_already_holds_files_may_not_read_it(tasks, tmp_path):
    run = live.LiveRun(tasks[0], live.AGENTS["claude"], "N", 1, live.DEFAULT_MODEL)
    root = live.sandbox_roots(tmp_path / "out", 1, stage="pilot")[0]
    old = root / "home" / ".claude" / "projects" / "-w" / "memory" / "MEMORY.md"
    old.parent.mkdir(parents=True)
    old.write_text("written before\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        _fake_agent_reading_memory(mp)
        record = live._execute_one(run, root, _NoHistory(tmp_path), [], "test")
    assert record["violations"] and "agent memory" in live._record_problem(record)


def test_a_handoff_finder_reading_agent_memory_is_a_violation(tasks, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    root = live.sandbox_roots(tmp_path / "out", 1, stage="pilot")[0]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(live, "_earlier_agent", lambda run, sandbox, checkout: {"start": GOOD_CALL})
        _fake_agent_reading_memory(mp)
        record = live._execute_one(_gn_handoff(tasks), root, _NoHistory(notes), [], "test")
    assert "refused" not in record
    assert record["violations"] and "agent memory" in live._record_problem(record)



HOME_READS = (
    "cat ~/.claude/projects/x/s.jsonl",
    "rg Osprey $HOME/.claude",
    "rg Osprey ~",
    "cat ~/.claude/history.jsonl",
    "cat ${HOME}/.claude.json",
)


@pytest.mark.parametrize("command", HOME_READS)
def test_a_home_path_written_with_a_tilde_or_home_variable_is_resolved_into_the_sandbox(box, command):
    root, cwd, _ = box
    found = live.read_violations([{"name": "Bash", "input": {"command": command}}], root, cwd, None)
    assert [v for v in found if live.MEMORY_READ in v] and not [v for v in found if "outside" in v]


def test_a_read_tool_path_with_a_tilde_is_resolved_too(box):
    root, cwd, _ = box
    calls = [{"name": "Read", "input": {"file_path": "~/.claude.json"}}]
    assert [v for v in live.read_violations(calls, root, cwd, None) if live.MEMORY_READ in v]


@pytest.mark.parametrize("command", ["cat ~/notes.md", "rg Osprey ~/work", "ls $HOME/.config"])
def test_other_home_paths_are_no_memory_read(box, command):
    root, cwd, _ = box
    found = live.read_violations([{"name": "Bash", "input": {"command": command}}], root, cwd, None)
    assert not [v for v in found if live.MEMORY_READ in v]


def _fake_agent_running(mp, commands):
    real = Sandbox.run

    def run(self, argv, **kwargs):
        if argv[0] != "claude":
            return real(self, argv, **kwargs)
        calls = [{"name": "Bash", "input": {"command": c}} for c in commands]
        return Completed(_stream(calls, "none"), "", 0, 0.1)

    mp.setattr(Sandbox, "run", run)


def test_each_home_read_of_a_handoff_finder_is_a_violation(tasks, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    root = live.sandbox_roots(tmp_path / "out", 1, stage="pilot")[0]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(live, "_earlier_agent", lambda run, sandbox, checkout: {"start": GOOD_CALL})
        _fake_agent_running(mp, HOME_READS)
        record = live._execute_one(_gn_handoff(tasks), root, _NoHistory(notes), [], "test")
    assert len([v for v in record["violations"] if live.MEMORY_READ in v]) == len(HOME_READS)
    assert "agent memory" in live._record_problem(record)


def test_a_single_session_tilde_read_of_its_own_empty_memory_is_a_note(tasks, tmp_path):
    run = live.LiveRun(tasks[0], live.AGENTS["claude"], "N", 1, live.DEFAULT_MODEL)
    root = live.sandbox_roots(tmp_path / "out", 1, stage="pilot")[0]
    with pytest.MonkeyPatch.context() as mp:
        _fake_agent_running(mp, ["ls ~/.claude/projects/-w/memory/"])
        record = live._execute_one(run, root, _NoHistory(tmp_path), [], "test")
    assert record["violations"] == [] and len(record["memory_reads"]) == 1
    assert live._record_problem(record) is None


@pytest.mark.parametrize(
    "path", ["~/.claude/commands/engmem.md", "~/.claude/skills/engmem/SKILL.md", "~/.claude/CLAUDE.md"],
)
def test_engmem_installed_copies_are_no_memory_read(box, path):
    root, cwd, _ = box
    calls = [{"name": "Read", "input": {"file_path": path}}]
    assert live.read_violations(calls, root, cwd, None) == []


def test_a_handoff_finder_reading_the_installed_command_gets_no_violation(tasks, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    root = live.sandbox_roots(tmp_path / "out", 1, stage="pilot")[0]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(live, "_earlier_agent", lambda run, sandbox, checkout: {"start": GOOD_CALL})
        _fake_agent_running(mp, ["cat ~/.claude/commands/engmem.md"])
        record = live._execute_one(_gn_handoff(tasks), root, _NoHistory(notes), [], "test")
    assert "refused" not in record and record["violations"] == []


@pytest.mark.parametrize("installed", ["home/.claude/commands/engmem.md", "home/.claude/skills/engmem/SKILL.md"])
def test_an_installed_copy_the_earlier_session_changed_refuses_the_finder(tasks, tmp_path, installed):
    notes = tmp_path / "notes"
    notes.mkdir()
    root = live.sandbox_roots(tmp_path / "out", 1, stage="pilot")[0]
    copy = root / installed
    copy.parent.mkdir(parents=True)
    copy.write_text("the installed template\n", encoding="utf-8")

    def earlier(run, sandbox, checkout):
        copy.write_text("the installed template\nheader X-Osprey-Twin\n", encoding="utf-8")
        return {"start": GOOD_CALL}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(live, "_earlier_agent", earlier)
        record = live._execute_one(_gn_handoff(tasks), root, _NoHistory(notes), [], "test")
    assert "start" not in record and record["earlier"]["memory_carried"] == [installed]


@pytest.mark.parametrize("arm", ["N", "G"])
def test_a_handoff_arm_with_no_earlier_session_reading_its_own_empty_memory_gets_a_note(tasks, tmp_path, arm):
    notes = tmp_path / "notes"
    notes.mkdir()
    run = live.LiveRun(next(t for t in tasks if t.saver), live.AGENTS["claude"], arm, 1, live.DEFAULT_MODEL)
    root = live.sandbox_roots(tmp_path / "out", 1, stage="pilot")[0]
    with pytest.MonkeyPatch.context() as mp:
        _fake_agent_running(mp, ["ls ~/.claude/projects/-w/memory/"])
        record = live._execute_one(run, root, _NoHistory(notes), [], "test")
    assert "earlier" not in record and record["violations"] == [] and len(record["memory_reads"]) == 1
    assert live._record_problem(record) is None



class _DraftFirstSandbox(_AskingSandbox):
    """`draft create` before the preview, `draft complete` after the first yes."""

    def run(self, argv, *, cwd=None, timeout=None, stdin=None):
        done = super().run(argv, cwd=cwd, timeout=timeout, stdin=stdin)
        draft = self.store / "sessions" / "20261113-doc.md"
        draft.write_text(DRAFT if len(self.calls) == 1 else ACTIVE, encoding="utf-8")
        return done


def test_a_draft_is_not_the_saved_document_and_the_replies_go_on(tasks, tmp_path):
    box = _DraftFirstSandbox(tmp_path, publishes_on=())
    record = live._save(_e_run(tasks), box, tmp_path, "session-0")
    assert len(record["confirmations"]) == 1 and record["new_docs"] == 1
    assert record["replies_stopped"] == "document written"


@pytest.mark.parametrize(
    ("text", "counted"),
    [(ACTIVE, True), (DRAFT, False), ("---\nid: x\nstatus: superseded\n---\n", False), ("no front matter\nstatus: active\n", False),
     ("---\nid: x\nstatus: draft\n---\n## Notes\nstatus: active\n---\n", False)],
    ids=["active", "draft", "superseded", "no-front-matter", "active-only-in-body"],
)
def test_only_an_active_document_counts(tmp_path, text, counted):
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "20261113-doc.md").write_text(text, encoding="utf-8")
    assert live._active_documents(tmp_path) == ({"20261113-doc"} if counted else set())
