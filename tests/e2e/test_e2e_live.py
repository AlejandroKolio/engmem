"""Layer 2 never starts an agent by default: a dry run prints the plan and the estimate, and a live run
refuses without its confirmations and without a pilot first."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentsim import corpus, live
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
def runs():
    tasks = [t for t in corpus.tasks() if t.id in ("keyword", "stale")]
    return live.plan(tasks, ["claude", "codex"], list(live.CONDITIONS))


def test_the_plan_crosses_tasks_agents_and_conditions(runs):
    assert len(runs) == 2 * 2 * 3
    engmem_run = next(r for r in runs if r.condition == "engmem" and r.agent.name == "claude")
    assert engmem_run.commands()[0][2].startswith("/engmem ")
    assert len(engmem_run.commands()) == 2
    grep_run = next(r for r in runs if r.condition == "grep")
    assert live.NOTES_HINT in " ".join(grep_run.commands()[0])
    assert len(grep_run.commands()) == 1


def test_a_dry_run_prints_the_estimate_and_starts_nothing(no_process, runs, tmp_path):
    est = live.estimate(runs, seconds_per_run=100, tokens_per_run=10_000, usd_per_mtok=None)
    text = live.dry_run(runs, est, tmp_path / "out")
    assert "DRY RUN: no agent is started" in text
    assert "runs planned: 12" in text
    assert "estimated time: 20.0 min" in text
    assert "not computed" in text
    assert not (tmp_path / "out").exists()


def test_the_estimate_prices_only_when_given_a_price(runs):
    est = live.estimate(runs, seconds_per_run=60, tokens_per_run=1_000_000, usd_per_mtok=2.5)
    assert est["tokens"] == 12_000_000 and est["usd"] == 30.0


def test_a_live_run_without_confirmation_is_refused(no_process, runs, tmp_path):
    with pytest.raises(live.LiveRunRefused, match="--i-accept-the-cost"):
        live.execute(runs, tmp_path, confirmed=False, pilot=True, pass_env=[])
    assert list(tmp_path.iterdir()) == []


def test_a_full_run_without_a_pilot_is_refused(no_process, runs, tmp_path):
    with pytest.raises(live.LiveRunRefused, match="pilot"):
        live.execute(runs, tmp_path, confirmed=True, pilot=False, pass_env=[])
    assert list(tmp_path.iterdir()) == []


def test_the_command_line_defaults_to_a_dry_run(tmp_path):
    out = tmp_path / "live"
    done = subprocess.run(
        [sys.executable, str(TOOL), "live", "--tasks", "keyword", "--agents", "claude", "--out", str(out)],
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


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        ([{"condition": "none", "start": {"returncode": 1, "usage": {"input_tokens": 1}}}], "exited 1"),
        ([{"condition": "none", "start": {"returncode": 0, "usage": {}}}], "no usage"),
        ([{"condition": "engmem", "start": GOOD_CALL}], "no save call"),
        ([{"condition": "engmem", "start": GOOD_CALL, "save": {"returncode": 2, "usage": {"input_tokens": 1}}}], "exited 2"),
        ([], "no run"),
    ],
    ids=["start-failed", "no-usage", "save-missing", "save-failed", "empty"],
)
def test_a_failed_pilot_does_not_unlock_the_full_run(no_process, runs, tmp_path, records, reason):
    _pilot(tmp_path, records)
    assert reason in live.pilot_problem(tmp_path / live.PILOT_FILE)
    with pytest.raises(live.LiveRunRefused, match="successful pilot"):
        live.execute(runs, tmp_path, confirmed=True, pilot=False, pass_env=[])


def test_a_successful_pilot_is_accepted(tmp_path):
    _pilot(tmp_path, [{"condition": "engmem", "start": GOOD_CALL, "save": GOOD_CALL}])
    assert live.pilot_problem(tmp_path / live.PILOT_FILE) is None
