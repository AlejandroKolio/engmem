"""Layer 1 end to end: scripted agents drive engmem as shipped on every scenario class. The smoke plan
runs by default; ENGMEM_E2E_FULL=1 runs every profile on every class (contracts/e2e.md)."""

from __future__ import annotations

import json
import os

import pytest

from agentsim import harness, report
from agentsim.profiles import BY_NAME, PROFILES
from agentsim.trace import TOKEN_METHOD
from support.platform import on_a_shared_runner

pytestmark = pytest.mark.e2e

SMOKE_BUDGET_SECONDS = 60
FULL = bool(os.environ.get("ENGMEM_E2E_FULL"))


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e")
    with pytest.MonkeyPatch.context() as mp:
        for name in ("HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            mp.setenv(name, str(root / "harness-process-home"))
        mp.delenv("ENGMEM_HOME", raising=False)
        mp.delenv("ENGMEM_STORE", raising=False)
        return harness.run(root / "out", harness.Plan(smoke=not FULL))


def _runs(results, *, cls=None, profile=None, condition=None):
    return [
        r for r in results["runs"]
        if (cls is None or r["scenario_class"] == cls)
        and (profile is None or r["profile"] == profile)
        and (condition is None or r["condition"] == condition)
    ]


def _one(results, cls, profile):
    (run,) = _runs(results, cls=cls, profile=profile)
    return run


def test_every_profile_runs_class_1_and_every_class_runs_under_an_engmem_profile_and_grep(results):
    assert {r["profile"] for r in _runs(results, cls=1)} == {p.name for p in PROFILES}
    for name in ("mcp-only", "grep", "no-history"):
        assert {r["scenario_class"] for r in _runs(results, profile=name)} == set(harness.ALL_CLASSES)
        assert any(r["variant"] for r in _runs(results, cls=2, profile=name))


def test_no_history_makes_no_call_and_is_right_only_where_nothing_was_known(results):
    for run in _runs(results, profile="no-history"):
        assert run["cost"]["calls"] == 0
        assert run["outcome"]["correct"] is (run["scenario_class"] in (5, 9))


@pytest.mark.parametrize("profile", [p.name for p in PROFILES if p.condition != "none"])
def test_a_keyword_findable_answer_is_reached_by_every_profile_that_searches(results, profile):
    run = _one(results, 1, profile)
    assert run["outcome"]["correct"], run["steps"]


def test_every_saving_profile_publishes_with_a_valid_citation(results):
    for run in _runs(results, condition="engmem"):
        if run["outcome"]["skipped"]:
            continue
        if not BY_NAME[run["profile"]].saves:
            assert run["outcome"]["saved"] is None and run["save_note"]
            continue
        assert run["outcome"]["saved"], (run["task"], run["profile"])
        assert run["outcome"]["save_refusals"] == 0
        if run["outcome"]["quoted"]:
            assert run["outcome"]["related_names_answer"], (run["task"], run["profile"])


def test_the_stale_trap_is_avoided_through_the_successor(results):
    for name in ("shell-claude", "grep"):
        outcome = _one(results, 4, name)["outcome"]
        assert outcome["stale_avoided"] and outcome["correct"], name


def test_engmem_says_none_found_when_nothing_is_relevant(results):
    for run in _runs(results, cls=5, condition="engmem"):
        assert run["outcome"]["none_found_said"], run["profile"]
        assert run["cost"]["retrieval_calls"] <= 2


def test_a_saved_decision_is_found_and_cited_by_another_agent_kind(results):
    for name in ("shell-claude", "mcp-only"):
        run = _one(results, 8, name)
        assert run["saver"]["saved"] and run["saver"]["profile"] != name
        assert run["outcome"]["related_names_answer"]
    assert not _one(results, 8, "grep")["outcome"]["correct"]


def test_every_rendered_copy_names_the_steps_its_profile_ran(results):
    failed = [
        (r["profile"], check) for r in results["runs"] for check, passed in r["template_checks"].items() if not passed
    ]
    assert failed == []


def test_engmem_ledger_matches_what_the_mcp_agent_received(results):
    for run in _runs(results, condition="engmem"):
        if run["engmem_observed_bytes"] is None or "mcp" not in run["profile"]:
            continue
        received = sum(
            s["bytes"] for s in run["steps"]
            if s["action"] in report.ENGMEM_RETRIEVAL_ACTIONS and s["phase"] in ("search", "load")
        )
        assert received == run["engmem_observed_bytes"], (run["task"], run["profile"])


def test_the_report_names_its_token_method_and_both_sides(results, tmp_path):
    text = report.render(results)
    assert TOKEN_METHOD in text
    assert "## Strong sides" in text and "## Weak sides" in text
    for cls in harness.ALL_CLASSES:
        assert f"## Class {cls}:" in text
    strong, weak, _ = report.sides(results["runs"])
    assert strong and weak
    assert any("saving costs" in line for line in weak)
    path = tmp_path / "results.json"
    harness.write_results(results, path)
    assert json.loads(path.read_text(encoding="utf-8"))["meta"]["token_method"] == TOKEN_METHOD


@on_a_shared_runner
def test_the_smoke_plan_stays_within_its_budget(results):
    if FULL:
        pytest.skip("the budget is for the smoke plan")
    assert results["meta"]["harness_seconds"] < SMOKE_BUDGET_SECONDS


def test_the_second_search_variant_runs_both_searches_for_engmem_and_grep(results):
    for name in ("mcp-only", "grep"):
        (run,) = [r for r in _runs(results, cls=2, profile=name) if r["variant"]]
        assert sum(1 for s in run["steps"] if s["phase"] == "search") == 2, name


def test_shell_profiles_read_the_mode_before_anything_else_as_step_0_says(results):
    run = _one(results, 1, "shell-claude")
    calls = [s["action"] for s in run["steps"] if s["kind"] != "template"]
    assert calls[:2] == ["engmem mode show", "engmem search"]


def test_a_daily_start_writes_nothing_and_its_save_takes_three_calls(results):
    """The daily contract: no draft before the save; one git call, create and complete."""
    for run in _runs(results, condition="engmem"):
        if run["outcome"]["skipped"]:
            continue
        before_save = [s["action"] for s in run["steps"] if s["phase"] != "save"]
        assert not [a for a in before_save if "draft" in a or a == "engmem store show"], run["profile"]
        assert run["drafts_left"] == 0, (run["task"], run["profile"])
        if BY_NAME[run["profile"]].saves:
            assert run["cost"]["save_calls"] <= 3, (run["task"], run["profile"])


def test_a_non_engineering_task_leaves_no_draft_no_search_row_and_no_store_write(results):
    """The declared expectation of class 9: every engmem profile stops at the scope gate."""
    runs = _runs(results, cls=9, condition="engmem")
    assert runs
    for run in runs:
        assert run["outcome"]["skipped"] and run["outcome"]["correct"], run["profile"]
        assert run["store_written"] is False, run["profile"]
        assert not [s for s in run["steps"] if s["phase"] in ("search", "load", "save")], run["profile"]
        assert not [s for s in run["steps"] if s["kind"] != "template"], run["profile"]
    assert _one(results, 9, "grep")["cost"]["calls"] == 0


def test_an_engineering_task_is_not_skipped_and_writes_its_draft(results):
    run = _one(results, 1, "shell-claude")
    assert run["outcome"]["skipped"] is False and run["store_written"] is True
