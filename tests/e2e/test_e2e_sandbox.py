"""Every harness run lives in a sandbox: nothing it starts can see the owner's store or settings."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agentsim import sandbox as sandbox_module
from agentsim.sandbox import ForbiddenPath, Sandbox, check_allowed
from engmem import runtime
from support.paths import REPO_ROOT

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("root", sandbox_module.FORBIDDEN_ROOTS, ids=lambda p: p.name)
def test_the_owners_data_roots_are_refused(root):
    with pytest.raises(ForbiddenPath):
        check_allowed(root)
    with pytest.raises(ForbiddenPath):
        check_allowed(root / "anything" / "below")


def test_a_sandbox_in_a_forbidden_root_is_refused_before_anything_is_created(tmp_path):
    forbidden = tmp_path / "forbidden"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sandbox_module, "FORBIDDEN_ROOTS", (forbidden.resolve(),))
        with pytest.raises(ForbiddenPath):
            Sandbox.create(forbidden / "box")
    assert not forbidden.exists()


def test_the_environment_points_inside_the_sandbox_and_drops_engmem_variables(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ENGMEM_HOME", str(tmp_path / "elsewhere"))
        mp.setenv("ENGMEM_STORE", str(tmp_path / "elsewhere"))
        mp.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
        box = Sandbox.create(tmp_path / "box")
    for name in ("ENGMEM_HOME", "ENGMEM_STORE", "CODEX_HOME"):
        assert name not in box.env
    root = box.root
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                 "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS",
                 "GIT_CONFIG_GLOBAL"):
        assert Path(box.env[name]).resolve().is_relative_to(root), name


def test_engmem_run_in_the_sandbox_uses_the_sandbox_store(tmp_path):
    box = Sandbox.create(tmp_path / "box")
    box.setup_engmem("store", "set", str(box.store))
    shown = box.engmem("store", "show")
    assert shown.returncode == 0
    first_line = shown.stdout.splitlines()[0]
    assert first_line.startswith("store: ")
    assert Path(first_line.removeprefix("store: ")).resolve() == box.store.resolve()


def test_reset_store_removes_an_earlier_runs_documents_and_logs(tmp_path):
    source = tmp_path / "corpus"
    source.mkdir()
    (source / "a.md").write_text("x", encoding="utf-8")
    box = Sandbox.create(tmp_path / "box")
    (box.sessions / "left-over.md").write_text("y", encoding="utf-8")
    (box.store / "cost.jsonl").write_text("{}\n", encoding="utf-8")
    box.reset_store(source)
    assert sorted(p.name for p in box.store.iterdir()) == ["sessions"]
    assert sorted(p.name for p in box.sessions.iterdir()) == ["a.md"]


@pytest.mark.parametrize(
    "variant",
    [lambda p: p, str.upper, str.lower, str.swapcase],
    ids=["same", "upper", "lower", "swapped"],
)
def test_a_protected_root_is_refused_in_any_letter_case(tmp_path, variant):
    protected = (tmp_path / "Protected" / "Store").resolve()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sandbox_module, "FORBIDDEN_ROOTS", (protected,))
        target = Path(variant(str(protected / "run" / "out")))
        with pytest.raises(ForbiddenPath):
            check_allowed(target)


def test_a_protected_root_reached_through_another_name_is_refused(tmp_path):
    protected = (tmp_path / "real-store").resolve()
    protected.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(protected, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted here")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sandbox_module, "FORBIDDEN_ROOTS", (protected,))
        with pytest.raises(ForbiddenPath):
            check_allowed(alias / "sub")


def test_protected_roots_come_from_engmems_store_and_the_protect_variable(tmp_path):
    extra_a, extra_b, home_store = tmp_path / "a", tmp_path / "b", tmp_path / "home-store"
    environ = {
        sandbox_module.PROTECT_ENV: f"{extra_a}{os.pathsep}{extra_b}",
        "ENGMEM_HOME": str(home_store),
    }
    roots = sandbox_module.protected_roots(environ)
    for path in (extra_a, extra_b, home_store, runtime.default_store()):
        assert path.resolve() in roots


def test_the_harness_names_no_personal_path():
    markers = ("/" + "Users/", "nb-" + "runs", "nb-" + "r5")
    files = [*(REPO_ROOT / "tools" / "agentsim").glob("*.py"), REPO_ROOT / "tools" / "e2e.py",
             *(REPO_ROOT / "tests" / "e2e").glob("*.py"), REPO_ROOT / "docs" / "design" / "contracts" / "e2e.md"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in markers), path.name


def test_a_live_sandbox_passes_only_the_allowlist_and_the_named_variables(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        mp.setenv("UNLISTED_SECRET", "x")
        mp.setenv("PASSED_KEY", "k")
        box = Sandbox.create(tmp_path / "box", inherit_env=False, pass_env=("PASSED_KEY", "CLAUDE_CONFIG_DIR"))
    assert "CLAUDE_CONFIG_DIR" not in box.env
    assert "UNLISTED_SECRET" not in box.env
    assert box.env["PASSED_KEY"] == "k"
    own = {
        "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PYTHONIOENCODING",
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "PASSED_KEY",
    }
    for name in box.env:
        assert (
            name in sandbox_module.LIVE_ENV_ALLOWLIST or name in own
            or name.startswith(("XDG_", "GIT_AUTHOR_", "GIT_COMMITTER_"))
        ), name


def test_a_path_outside_the_allowlist_is_refused_by_default():
    for outside in (REPO_ROOT / "not-the-tmp-dir" / "out", Path(tempfile.gettempdir()).resolve().parent / "e2e-out"):
        with pytest.raises(ForbiddenPath, match="--allow-out"):
            check_allowed(outside)
    assert check_allowed(REPO_ROOT / ".tmp" / "e2e" / "out")
    assert check_allowed(Path(tempfile.gettempdir()) / "e2e-out")


def test_allow_out_opens_a_directory_but_never_a_protected_root(tmp_path):
    outside = REPO_ROOT / "not-the-tmp-dir"
    protected = (tmp_path / "protected").resolve()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sandbox_module, "ALLOWED_ROOTS", list(sandbox_module.ALLOWED_ROOTS))
        mp.setattr(sandbox_module, "FORBIDDEN_ROOTS", (protected,))
        sandbox_module.allow(outside)
        assert check_allowed(outside / "out") == (outside / "out").resolve()
        with pytest.raises(ForbiddenPath, match="protected"):
            sandbox_module.allow(protected / "inner")
        with pytest.raises(ForbiddenPath, match="protected"):
            check_allowed(protected / "inner")
    assert not outside.exists()
