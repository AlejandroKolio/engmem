"""engmem prints absolute paths and trims its output by bytes, so the harness gives every sandbox a store
path of one length wherever its results directory lies (contracts/e2e.md, "Safety")."""

from __future__ import annotations

import pytest

from agentsim import harness, instances, live, phase4
from agentsim.sandbox import SANDBOX_ROOT_BYTES, PathTooLong, padded_root, path_bytes

pytestmark = pytest.mark.e2e

INSTANCES = ("c04-identifier", "c04-paraphrase", "c10-identifier")
PROFILES = ("shell-claude", "grep-content")


def _layer1(out):
    instance_set, facts = phase4.load(instances.PROVISIONAL_FILE)
    tasks = tuple(t for t in instances.tasks_of(instance_set, facts) if t.id in INSTANCES)
    plan = harness.Plan(profiles=PROFILES, tasks=(), instances=tasks, filler=10, scale_filler=20)
    return {(r["task"], r["profile"]): r for r in harness.run(out, plan)["runs"]}


def test_two_results_directories_of_different_lengths_give_the_same_runs(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        for name in ("HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            mp.setenv(name, str(tmp_path / "harness-process-home"))
        mp.delenv("ENGMEM_HOME", raising=False)
        mp.delenv("ENGMEM_STORE", raising=False)
        short = _layer1(tmp_path / "a")
        long = _layer1(tmp_path / "held-longer")
    assert short.keys() == long.keys() and len(short) == len(INSTANCES) * len(PROFILES)
    for key, run in short.items():
        other = long[key]
        assert run["outcome"] == other["outcome"], key
        assert [(s["action"], s["bytes"]) for s in run["steps"]] == [(s["action"], s["bytes"]) for s in other["steps"]], key
        assert run["store_path_bytes"] == other["store_path_bytes"] == SANDBOX_ROOT_BYTES + len("/store")


def test_a_root_is_padded_to_the_fixed_length_and_a_base_too_long_is_refused(tmp_path):
    root = padded_root(tmp_path, "shell-claude")
    assert path_bytes(root) == SANDBOX_ROOT_BYTES and root.name.startswith("shell-claude-")
    with pytest.raises(PathTooLong, match="shorter --out"):
        padded_root(tmp_path / ("x" * SANDBOX_ROOT_BYTES), "shell-claude")


def test_a_layer1_run_under_a_base_too_long_is_refused_before_anything_is_written(tmp_path):
    out = tmp_path / ("x" * SANDBOX_ROOT_BYTES)
    with pytest.raises(PathTooLong):
        harness.run(out, harness.Plan(profiles=("no-history",), tasks=(), filler=10, scale_filler=20))
    assert not out.exists()


def test_live_sandboxes_are_zero_padded_and_of_one_length_in_every_stage(tmp_path):
    by_stage = {stage: live.sandbox_roots(tmp_path, 12, stage=stage) for stage in live.STAGES}
    for stage, roots in by_stage.items():
        assert [r.name[: live.SANDBOX_INDEX_WIDTH] for r in roots[:2]] == ["0000", "0001"]
        assert {r.parent for r in roots} == {(tmp_path / stage / "sandboxes").resolve()}
        assert {path_bytes(r) for r in roots} == {SANDBOX_ROOT_BYTES}
    with pytest.raises(live.LiveRunRefused, match="shorter --out"):
        live.sandbox_roots(tmp_path / ("x" * SANDBOX_ROOT_BYTES), 1, stage="pilot")
    with pytest.raises(live.LiveRunRefused, match="4-digit"):
        live.sandbox_roots(tmp_path, 10 ** live.SANDBOX_INDEX_WIDTH + 1, stage="pilot")
