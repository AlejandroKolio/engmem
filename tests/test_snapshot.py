"""US-11: each repository's snapshot anchor is shown next to the commit this session's checkout is
at, per repository, without ever claiming the text is true."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import DRAFT_CONTENT, write_file
from mcp_harness import _call, _run, _telemetry_lines, _text, _tools_call_msg

from engmem import provenance
from engmem.cli import main
from engmem.output import MAX_OUTPUT_BYTES, SCOREBOARD_RESERVE
from engmem.provenance import (
    DUBIOUS_OWNERSHIP,
    SessionCheckout,
    SnapshotState,
    _shown_path,
    snapshots,
)
from engmem.search_report import render_result
from engmem.spine import load_store, parse_document
from engmem.telemetry import UNATTRIBUTED_CLI_NOTE, UNATTRIBUTED_MCP_NOTE

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ALPHA = "svc-alpha"
BETA = "svc-beta"
QUERY = "Settings"


@pytest.fixture(autouse=True)
def _hermetic_git(tmp_path_factory, monkeypatch):
    """The developer's own git config (signing, hooks, a default branch) must not steer a test."""
    empty = tmp_path_factory.mktemp("gitconfig") / "config"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def _git(checkout: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    return completed.stdout.strip()


def _elsewhere(checkout: Path) -> str:
    """The reason for a repository whose checkout is not the one engmem runs in."""
    toplevel = _git(checkout, "rev-parse", "--show-toplevel")
    return f"no checkout of it here, engmem's working directory is in {_shown_path(toplevel)}"


def _outside(directory: Path) -> str:
    return f"engmem's working directory {_shown_path(str(directory))} is not in a git checkout"


def _commit(checkout: Path) -> str:
    _git(checkout, "commit", "--allow-empty", "-q", "-m", "change")
    return _git(checkout, "rev-parse", "HEAD")


def _checkout(root: Path, name: str, *, commits: int = 1) -> tuple[Path, str]:
    """A real git repository named like the repository it stands for, and its HEAD."""
    path = root / "checkouts" / name
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    head = ""
    for _ in range(commits):
        head = _commit(path)
    return path, head


def _doc(doc_id: str, *, repos: str | None = None, verified_at: str | None = None,
         legacy: str | None = None) -> str:
    lines = [f"repos: {repos}"] if repos is not None else []
    if verified_at is not None:
        lines.append(f"verified_at: {verified_at}")
    if legacy is not None:
        lines.append(f"verified_at_commit: {legacy}")
    extra = "".join(f"{line}\n" for line in lines)
    return (
        f"---\nid: {doc_id}\ntitle: {doc_id} Settings\ndate: 2026-01-01\ntask_date: 2026-01-01\n"
        f"status: active\nsuperseded_by:\nbackfilled: false\ntags: []\nentities: [Settings]\n"
        f"related: []\ncovers_files: []\n{extra}---\n\n## Decision Log\n\nSettings are read once.\n"
    )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    (tmp_path / "store" / "sessions").mkdir(parents=True)
    return tmp_path / "store"


def _add(store: Path, doc_id: str, **fields: str) -> None:
    write_file(store / "sessions", f"{doc_id}.md", _doc(doc_id, **fields))


def _cli(store: Path, capsys, *extra: str) -> str:
    assert main(["search", QUERY, "--store", str(store), *extra]) == 0
    return capsys.readouterr().out


def _mcp(store: Path, **arguments) -> str:
    result, is_error = _call(store, name="engmem_search", arguments={"query": QUERY, **arguments})
    assert not is_error, _text(result)
    return _text(result)


def _snapshot_line(output: str, doc_id: str) -> str | None:
    block = next(b for b in output.split("\n\n") if b.startswith(f"### {doc_id} "))
    return next((line for line in block.splitlines() if line.startswith("snapshot: ")), None)


# AC-11.1 ---------------------------------------------------------------------------------------


def test_ac_11_1_a_matching_anchor_shows_the_anchor_and_says_only_that_the_commits_match(
    tmp_path, store, monkeypatch, capsys
):
    checkout, head = _checkout(tmp_path, ALPHA)
    _add(store, "2101-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    (checkout / "src").mkdir()
    monkeypatch.chdir(checkout / "src")

    line = _snapshot_line(_cli(store, capsys), "2101-alpha")

    assert line == f"snapshot: {ALPHA} {head[:7]}: same commit as HEAD"
    for claim in ("verified", "valid", "true", "correct", "up to date"):
        assert claim not in line


def test_ac_11_1_repository_names_compare_like_a_repo_scope(tmp_path, store, monkeypatch, capsys):
    checkout, head = _checkout(tmp_path, ALPHA)
    _add(store, "2102-alpha", repos="[SVC-Alpha]", verified_at=f"{{' SVC-Alpha ': {head.upper()}}}")
    monkeypatch.chdir(checkout)

    assert _snapshot_line(_cli(store, capsys), "2102-alpha") == (
        f"snapshot: SVC-Alpha {head[:7]}: same commit as HEAD"
    )


def test_ac_11_1_an_abbreviated_anchor_matches_its_full_commit(
    tmp_path, store, monkeypatch, capsys
):
    checkout, head = _checkout(tmp_path, ALPHA)
    _add(store, "2103-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: '{head[:12]}'}}")
    monkeypatch.chdir(checkout)

    assert _snapshot_line(_cli(store, capsys), "2103-alpha") == (
        f"snapshot: {ALPHA} {head[:7]}: same commit as HEAD"
    )


# AC-11.2 ---------------------------------------------------------------------------------------


def test_ac_11_2_a_moved_checkout_asks_for_a_re_check_and_keeps_the_record(
    tmp_path, store, monkeypatch, capsys
):
    checkout, anchored = _checkout(tmp_path, ALPHA)
    _add(store, "2201-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {anchored}}}")
    head = _commit(checkout)
    monkeypatch.chdir(checkout)

    out = _cli(store, capsys)

    assert _snapshot_line(out, "2201-alpha") == (
        f"snapshot: {ALPHA} {anchored[:7]}: HEAD is now {head[:7]}, re-check"
    )
    assert out.startswith("### 2201-alpha (score: "), "still a hit, ranked as before"
    for verdict in ("false", "stale", "invalid", "superseded", "outdated"):
        assert verdict not in _snapshot_line(out, "2201-alpha")


def test_ac_11_2_the_state_stays_a_refinable_value_for_us_12(tmp_path, store, monkeypatch):
    checkout, anchored = _checkout(tmp_path, ALPHA)
    _add(store, "2202-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {anchored}}}")
    head = _commit(checkout)
    monkeypatch.chdir(checkout)
    (doc,) = load_store(store / "sessions").docs

    (snapshot,) = snapshots(doc, SessionCheckout())

    assert (snapshot.state, snapshot.anchor, snapshot.current) == (
        SnapshotState.DIFFERS, anchored, head
    )


# AC-11.3 ---------------------------------------------------------------------------------------


def test_ac_11_3_a_linked_repository_without_an_anchor_is_unknown(
    tmp_path, store, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    _add(store, "2301-alpha", repos=f"[{ALPHA}]")

    assert _snapshot_line(_cli(store, capsys), "2301-alpha") == (
        f"snapshot: {ALPHA}: unknown, no anchor recorded"
    )


def test_ac_11_3_a_checkout_of_another_repository_is_not_this_one(
    tmp_path, store, monkeypatch, capsys
):
    _, head = _checkout(tmp_path, ALPHA)
    beta, _ = _checkout(tmp_path, BETA)
    _add(store, "2302-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    monkeypatch.chdir(beta)

    assert _snapshot_line(_cli(store, capsys), "2302-alpha") == (
        f"snapshot: {ALPHA} {head[:7]}: unknown, {_elsewhere(beta)}"
    )
    assert _snapshot_line(_cli(store, capsys), "2302-alpha").endswith(BETA)


def test_ac_11_3_a_long_checkout_path_is_cut_from_the_front(tmp_path, store, monkeypatch, capsys):
    _, head = _checkout(tmp_path, ALPHA)
    beta, _ = _checkout(tmp_path / ("deep" * 20), BETA)
    _add(store, "2313-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    monkeypatch.chdir(beta)

    line = _snapshot_line(_cli(store, capsys), "2313-alpha")
    shown = line.split("engmem's working directory is in ", 1)[1]

    assert shown.startswith("…") and shown.endswith(f"/checkouts/{BETA}") and len(shown) == 60


def test_ac_11_3_a_directory_outside_any_checkout_is_named(tmp_path, store, monkeypatch, capsys):
    _, head = _checkout(tmp_path, ALPHA)
    _add(store, "2303-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    # a parent of tmp_path may itself be a checkout; the ceiling keeps git from reaching it
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    assert _snapshot_line(_cli(store, capsys), "2303-alpha") == (
        f"snapshot: {ALPHA} {head[:7]}: unknown, {_outside(Path.cwd())}"
    )


def test_ac_11_3_the_reason_does_not_depend_on_the_locale(tmp_path, store, monkeypatch, capsys):
    _, head = _checkout(tmp_path, ALPHA)
    _add(store, "2309-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    # where git ships its translations, these turn "not a git repository" into German
    monkeypatch.setenv("LANGUAGE", "de")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")

    assert _snapshot_line(_cli(store, capsys), "2309-alpha") == (
        f"snapshot: {ALPHA} {head[:7]}: unknown, {_outside(Path.cwd())}"
    )


def _git_supports_assumed_other_owner(tmp_path: Path) -> bool:
    probe, _ = _checkout(tmp_path / "probe", "probe")
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=probe, capture_output=True, text=True,
        env={**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}, stdin=subprocess.DEVNULL,
    )
    return "dubious ownership" in completed.stderr


def test_ac_11_3_a_checkout_git_refuses_names_the_refusal_and_no_command(
    tmp_path, store, monkeypatch, capsys
):
    if not _git_supports_assumed_other_owner(tmp_path):
        pytest.skip("this git cannot simulate another owner (GIT_TEST_ASSUME_DIFFERENT_OWNER)")
    checkout, head = _checkout(tmp_path, ALPHA)
    _add(store, "2310-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    line = _snapshot_line(_cli(store, capsys), "2310-alpha")

    assert line == f"snapshot: {ALPHA} {head[:7]}: unknown, {DUBIOUS_OWNERSHIP}"
    assert "safe.directory" not in line and "git config" not in line


def test_ac_11_3_a_git_error_is_its_error_line_not_a_hint(tmp_path, store, monkeypatch, capsys):
    checkout, head = _checkout(tmp_path, ALPHA)
    _add(store, "2311-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    monkeypatch.chdir(checkout / ".git")

    assert _snapshot_line(_cli(store, capsys), "2311-alpha") == (
        f"snapshot: {ALPHA} {head[:7]}: unknown, git rev-parse failed: fatal: this operation "
        "must be run in a work tree"
    )


@pytest.mark.parametrize("stderr, reason", [
    pytest.param(
        "hint: something to try\nerror: the real problem\nhint: run git config --global x\n",
        "git rev-parse failed: error: the real problem", id="error-between-hints",
    ),
    pytest.param("hint: only advice\n", "git rev-parse failed: git exited with code 128",
                 id="hints-only"),
])
def test_ac_11_3_only_the_error_line_reaches_the_reason(
    tmp_path, store, monkeypatch, capsys, stderr, reason
):
    _add(store, "2312-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {'a' * 40}}}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(provenance, "_run_git", lambda git, directory: (128, "", stderr))

    assert _snapshot_line(_cli(store, capsys), "2312-alpha") == (
        f"snapshot: {ALPHA} aaaaaaa: unknown, {reason}"
    )


def test_ac_11_3_no_git_on_path_is_named(tmp_path, store, monkeypatch, capsys):
    checkout, head = _checkout(tmp_path, ALPHA)
    _add(store, "2304-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    monkeypatch.chdir(checkout)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert _snapshot_line(_cli(store, capsys), "2304-alpha") == (
        f"snapshot: {ALPHA} {head[:7]}: unknown, git is not on PATH"
    )


def test_ac_11_3_a_checkout_with_no_commit_yet_is_named(tmp_path, store, monkeypatch, capsys):
    checkout, _ = _checkout(tmp_path, ALPHA, commits=0)
    anchor = "a" * 40
    _add(store, "2305-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {anchor}}}")
    monkeypatch.chdir(checkout)

    assert _snapshot_line(_cli(store, capsys), "2305-alpha") == (
        f"snapshot: {ALPHA} aaaaaaa: unknown, the checkout {ALPHA} has no commit at HEAD yet"
    )


@pytest.mark.parametrize("failure, reason", [
    pytest.param(
        subprocess.TimeoutExpired("git", provenance.GIT_LOOKUP_SECONDS),
        f"git did not answer within {provenance.GIT_LOOKUP_SECONDS:g} s", id="timeout",
    ),
    pytest.param(
        PermissionError(13, "denied"), "git could not be run (PermissionError)", id="oserror"
    ),
])
def test_ac_11_3_git_that_cannot_answer_is_named(
    tmp_path, store, monkeypatch, capsys, failure, reason
):
    checkout, head = _checkout(tmp_path, ALPHA)
    _add(store, "2306-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    monkeypatch.chdir(checkout)

    def refuse(*args, **kwargs):
        raise failure

    monkeypatch.setattr(provenance, "_run_git", refuse)

    assert _snapshot_line(_cli(store, capsys), "2306-alpha") == (
        f"snapshot: {ALPHA} {head[:7]}: unknown, {reason}"
    )


@pytest.mark.parametrize("verified_at, part, warning", [
    pytest.param(
        f"{{{ALPHA}: not-a-sha}}", f"{ALPHA} not-a-sha: unknown, its anchor is not a commit id",
        "not a commit id", id="not-hex",
    ),
    pytest.param(
        f"{{{ALPHA}: abc12}}", f"{ALPHA} abc12: unknown, its anchor is not a commit id",
        "not a commit id", id="too-short",
    ),
    pytest.param(
        f"{{{ALPHA}: 1234567}}", f"{ALPHA}: unknown, its anchor is not text",
        "not text", id="unquoted-number",
    ),
    pytest.param(
        f"[{ALPHA}]", f"{ALPHA}: unknown, verified_at cannot be read (see the load warning)",
        "not a mapping", id="not-a-mapping",
    ),
])
def test_ac_11_3_a_malformed_anchor_is_unknown_with_a_warning_and_never_a_load_failure(
    tmp_path, store, monkeypatch, capsys, verified_at, part, warning
):
    monkeypatch.chdir(tmp_path)
    _add(store, "2307-alpha", repos=f"[{ALPHA}]", verified_at=verified_at)

    loaded = load_store(store / "sessions")
    out = _cli(store, capsys)

    assert [d.id for d in loaded.docs] == ["2307-alpha"] and not loaded.errors
    assert any(warning in p.message for p in loaded.warnings), loaded.warnings
    assert _snapshot_line(out, "2307-alpha") == f"snapshot: {part}"


def test_ac_11_3_an_unreadable_field_on_a_record_with_no_repository_still_shows(
    tmp_path, store, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    _add(store, "2308-unlinked", verified_at="not-a-mapping")

    assert _snapshot_line(_cli(store, capsys), "2308-unlinked") == (
        "snapshot: verified_at: unknown, verified_at cannot be read (see the load warning)"
    )


# AC-11.4 ---------------------------------------------------------------------------------------


def test_ac_11_4_only_the_anchored_repository_is_compared(tmp_path, store, monkeypatch, capsys):
    alpha, head = _checkout(tmp_path, ALPHA)
    _add(store, "2401-shared", repos=f"[{ALPHA}, {BETA}]", verified_at=f"{{{ALPHA}: {head}}}")
    monkeypatch.chdir(alpha)

    assert _snapshot_line(_cli(store, capsys), "2401-shared") == (
        f"snapshot: {ALPHA} {head[:7]}: same commit as HEAD | {BETA}: unknown, no anchor recorded"
    )


def test_ac_11_4_a_match_in_one_checkout_does_not_cover_the_other(
    tmp_path, store, monkeypatch, capsys
):
    alpha, head_a = _checkout(tmp_path, ALPHA)
    _, head_b = _checkout(tmp_path, BETA, commits=2)
    _add(store, "2402-shared", repos=f"[{ALPHA}, {BETA}]",
         verified_at=f"{{{ALPHA}: {head_a}, {BETA}: {head_b}}}")
    monkeypatch.chdir(alpha)

    assert _snapshot_line(_cli(store, capsys), "2402-shared") == (
        f"snapshot: {ALPHA} {head_a[:7]}: same commit as HEAD | {BETA} {head_b[:7]}: unknown, "
        f"{_elsewhere(alpha)}"
    )


def test_ac_11_4_an_anchor_for_an_unlinked_repository_is_still_shown(
    tmp_path, store, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    sha = "b" * 40
    _add(store, "2403-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{BETA}: {sha}}}")

    assert _snapshot_line(_cli(store, capsys), "2403-alpha") == (
        f"snapshot: {ALPHA}: unknown, no anchor recorded | {BETA} bbbbbbb: unknown, "
        f"{_outside(Path.cwd())}"
    )


def test_a_successor_block_shows_its_own_snapshot(tmp_path, store, monkeypatch, capsys):
    alpha, head = _checkout(tmp_path, ALPHA)
    monkeypatch.chdir(alpha)
    old = _doc("2404-old", repos=f"[{BETA}]").replace(
        "status: active\nsuperseded_by:", "status: superseded\nsuperseded_by: 2405-new"
    )
    write_file(store / "sessions", "2404-old.md", old)
    new = _doc("2405-new", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    write_file(store / "sessions", "2405-new.md", new.replace("Settings", "Cache"))

    out = _cli(store, capsys)

    assert "### 2405-new (successor)" in out
    assert _snapshot_line(out, "2405-new") == f"snapshot: {ALPHA} {head[:7]}: same commit as HEAD"


# the legacy single field -----------------------------------------------------------------------


def test_legacy_anchor_with_exactly_one_repository_becomes_its_anchor(
    tmp_path, store, monkeypatch, capsys
):
    checkout, head = _checkout(tmp_path, ALPHA)
    _add(store, "2501-legacy", repos=f"[{ALPHA}, ' svc-ALPHA ']", legacy=head)
    monkeypatch.chdir(checkout)

    assert _snapshot_line(_cli(store, capsys), "2501-legacy") == (
        f"snapshot: {ALPHA} {head[:7]}: same commit as HEAD"
    )


@pytest.mark.parametrize("repos, line", [
    pytest.param(
        None, "snapshot: verified_at_commit abc1234: unknown, the record names no repository",
        id="no-repository",
    ),
    pytest.param(
        f"[{ALPHA}, {BETA}]",
        f"snapshot: {ALPHA}: unknown, no anchor recorded | {BETA}: unknown, no anchor recorded | "
        "verified_at_commit abc1234: unknown, the record names 2 repositories",
        id="two-repositories",
    ),
    pytest.param("{a: b}", "snapshot: verified_at_commit abc1234: unknown, its repository links "
                 "cannot be read", id="unreadable-repos"),
])
def test_legacy_anchor_is_never_guessed_onto_a_repository(
    tmp_path, store, monkeypatch, capsys, repos, line
):
    monkeypatch.chdir(tmp_path)
    _add(store, "2502-legacy", repos=repos, legacy="abc1234")

    assert _snapshot_line(_cli(store, capsys), "2502-legacy") == line


@pytest.mark.parametrize("repos, line", [
    pytest.param(f"[{ALPHA}]", f"snapshot: {ALPHA}: unknown, its anchor is not text", id="one-repo"),
    pytest.param(None, "snapshot: verified_at_commit: unknown, its anchor is not text",
                 id="no-repo"),
])
def test_an_unquoted_numeric_legacy_anchor_is_not_text(tmp_path, store, monkeypatch, capsys,
                                                       repos, line):
    monkeypatch.chdir(tmp_path)
    _add(store, "2506-octal", repos=repos, legacy="0123456")

    loaded = load_store(store / "sessions")

    assert loaded.docs[0].verified_at_commit is None
    assert any("verified_at_commit is 42798, not text" in p.message for p in loaded.warnings)
    assert _snapshot_line(_cli(store, capsys), "2506-octal") == line


def test_the_per_repository_field_wins_over_the_legacy_one(tmp_path, store, monkeypatch, capsys):
    checkout, head = _checkout(tmp_path, ALPHA)
    _add(
        store, "2503-both", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}", legacy="abc1234"
    )
    monkeypatch.chdir(checkout)

    assert _snapshot_line(_cli(store, capsys), "2503-both") == (
        f"snapshot: {ALPHA} {head[:7]}: same commit as HEAD"
    )


def test_a_record_with_no_repository_and_no_anchor_prints_no_snapshot_line(
    tmp_path, store, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    _add(store, "2504-plain")
    _add(store, "2505-blank", legacy="", verified_at="")

    out = _cli(store, capsys)

    assert not any(line.startswith("snapshot:") for line in out.splitlines())


# channels, cost and the budget -----------------------------------------------------------------


@pytest.fixture
def anchored_store(tmp_path, store, monkeypatch) -> tuple[Path, Path]:
    alpha, head = _checkout(tmp_path, ALPHA)
    _add(store, "2601-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    _add(store, "2602-shared", repos=f"[{ALPHA}, {BETA}]", verified_at=f"{{{ALPHA}: {head}}}")
    _add(store, "2603-legacy", legacy="abc1234")
    monkeypatch.chdir(alpha)
    return store, alpha


@pytest.mark.parametrize("role", [None, "decisions"])
def test_cli_and_mcp_show_the_same_snapshot_lines(anchored_store, capsys, role):
    store, _ = anchored_store
    cli_args = ["--role", role] if role else []
    tool, extra = ("engmem_search_by_role", {"role": role}) if role else ("engmem_search", {})

    cli_out = _cli(store, capsys, *cli_args)
    result, is_error = _call(store, name=tool, arguments={"query": QUERY, **extra})
    mcp_out = _text(result)

    assert not is_error
    assert [l for l in cli_out.splitlines() if l != UNATTRIBUTED_CLI_NOTE] == [
        l for l in mcp_out.splitlines() if l != UNATTRIBUTED_MCP_NOTE
    ]
    assert sum(line.startswith("snapshot: ") for line in cli_out.splitlines()) == 3


def test_git_runs_once_per_search_and_never_when_nothing_needs_it(
    anchored_store, tmp_path, monkeypatch, capsys
):
    store, _ = anchored_store
    calls: list[Path] = []
    real = provenance.find_checkout
    monkeypatch.setattr(provenance, "find_checkout", lambda d: calls.append(d) or real(d))

    _cli(store, capsys)
    assert len(calls) == 1

    plain = tmp_path / "plain"
    (plain / "sessions").mkdir(parents=True)
    _add(plain, "2604-alpha", repos=f"[{ALPHA}]", legacy="abc1234 extra")
    _add(plain, "2605-plain")
    _cli(plain, capsys)
    assert len(calls) == 1, "no shown record had an anchor to compare"


def test_each_mcp_call_reads_the_checkout_again(anchored_store):
    store, alpha = anchored_store
    first = _mcp(store)
    head = _commit(alpha)
    second = _mcp(store)

    assert "2601-alpha" in first and "same commit as HEAD" in _snapshot_line(first, "2601-alpha")
    assert f"HEAD is now {head[:7]}, re-check" in _snapshot_line(second, "2601-alpha")


def test_one_mcp_session_looks_the_checkout_up_again_for_every_call(anchored_store, monkeypatch):
    store, _ = anchored_store
    calls: list[Path] = []
    real = provenance.find_checkout
    monkeypatch.setattr(provenance, "find_checkout", lambda d: calls.append(d) or real(d))

    _, responses, _ = _run(
        store,
        _tools_call_msg(1, arguments={"query": QUERY}),
        _tools_call_msg(2, arguments={"query": QUERY}),
    )

    assert len(responses) == 2 and len(calls) == 2


def test_the_snapshot_line_is_inside_the_budget_and_context_bytes(
    tmp_path, store, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    names = [f"repository-with-a-long-name-{n:02d}" for n in range(40)]
    anchors = ", ".join(f"{name}: {'c' * 40}" for name in names)
    for n in range(4):
        _add(store, f"27{n:02d}-many", repos=f"[{', '.join(names)}]", verified_at=f"{{{anchors}}}")

    out = _cli(store, capsys)
    rendered = render_result(load_store(store / "sessions").docs, QUERY, None,
                             checkout=SessionCheckout())

    assert len(rendered.text.encode("utf-8")) <= MAX_OUTPUT_BYTES - SCOREBOARD_RESERVE
    (row,) = _telemetry_lines(store)
    assert row["context_bytes"] == len(rendered.text.encode("utf-8"))
    assert out.startswith(rendered.text)


def test_a_repository_name_cannot_forge_a_line(tmp_path, store, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _add(store, "2801-forged", verified_at='{"svc\\n### forged (score: 9.9)": abc1234}')

    out = _cli(store, capsys)

    assert not any(line.startswith("### forged") for line in out.splitlines())
    assert "svc\\n### forged" in _snapshot_line(out, "2801-forged")


def test_a_repository_pointed_to_by_git_dir_does_not_answer_for_the_checkout(
    tmp_path, store, monkeypatch, capsys
):
    alpha, head = _checkout(tmp_path, ALPHA)
    beta, _ = _checkout(tmp_path, BETA, commits=2)
    _add(store, "2901-alpha", repos=f"[{ALPHA}]", verified_at=f"{{{ALPHA}: {head}}}")
    monkeypatch.chdir(alpha)
    monkeypatch.setenv("GIT_DIR", str(beta / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(beta))

    assert _snapshot_line(_cli(store, capsys), "2901-alpha") == (
        f"snapshot: {ALPHA} {head[:7]}: same commit as HEAD"
    )


# recorded at capture ---------------------------------------------------------------------------


def _complete(store: Path, content: str) -> tuple[str, bool]:
    write_file(store / "sessions", "20260101-widget-cache.md", DRAFT_CONTENT)
    result, is_error = _call(
        store, name="engmem_complete_draft",
        arguments={"id": "20260101-widget-cache", "content": content},
    )
    return _text(result), is_error


def _active(front_matter: str) -> str:
    return (
        "---\nid: 20260101-widget-cache\ntitle: WidgetCache rollout\ndate: 2026-01-01\n"
        "task_date: 2026-01-01\nstatus: active\ntags: [cache]\nentities: [WidgetCache]\n"
        f"{front_matter}mode: daily\n---\n\n## Decision Log\n\nWidgetCache warms on boot.\n\n"
        "## Reuse Log\n\nPrior docs used: none.\n"
    )


def test_completing_a_record_warns_about_a_malformed_anchor_and_still_completes(store):
    text, is_error = _complete(
        store, _active(f"repos: [{ALPHA}]\nverified_at: {{{ALPHA}: nope}}\n")
    )

    assert not is_error, text
    assert (
        f"warning: sessions/20260101-widget-cache.md: verified_at '{ALPHA}' is 'nope', not a "
        "commit id — a search shows that anchor as unknown"
    ) in text
    assert parse_document(store / "sessions" / "20260101-widget-cache.md").status == "active"


@pytest.mark.parametrize("verified_at, note", [
    pytest.param(
        f"{{{ALPHA}: {'e' * 40}, SVC-ALPHA: {'f' * 40}}}",
        "verified_at names 'SVC-ALPHA' twice — the first anchor is kept", id="duplicate",
    ),
    pytest.param(
        "[x]", "verified_at is not a mapping of repository to commit: ['x'] — its snapshot "
        "anchors are unknown", id="not-a-mapping",
    ),
    pytest.param(
        f"{{'': {'e' * 40}}}", f"verified_at has an entry with no repository name: '{'e' * 40}' "
        "— ignored", id="no-name",
    ),
    pytest.param(
        f"{{~: {'e' * 40}}}", f"verified_at has an entry with no repository name: '{'e' * 40}' "
        "— ignored", id="null-name",
    ),
    pytest.param(
        f"{{{ALPHA}: 1234567}}", f"verified_at '{ALPHA}' is 1234567, not text — quote the commit "
        "id; its anchor is unknown", id="not-text",
    ),
])
def test_completing_a_record_names_each_anchor_warning_as_it_is(store, verified_at, note):
    text, is_error = _complete(store, _active(f"repos: [{ALPHA}]\nverified_at: {verified_at}\n"))

    assert not is_error, text
    notes = [line for line in text.splitlines() if "verified_at" in line]
    assert notes == [f"warning: sessions/20260101-widget-cache.md: {note}"]


@pytest.mark.parametrize("front_matter", [
    pytest.param(f"repos: [{ALPHA}]\nverified_at: {{{ALPHA}: {'e' * 40}}}\n", id="anchored"),
    pytest.param(f"repos: [{ALPHA}, {BETA}]\nverified_at:\n", id="no-shell"),
    pytest.param("verified_at_commit: abc1234\n", id="legacy"),
])
def test_completing_a_well_formed_record_adds_no_snapshot_warning(store, front_matter):
    text, is_error = _complete(store, _active(front_matter))

    assert not is_error, text
    assert "verified_at" not in text


# the front matter ------------------------------------------------------------------------------


def test_verified_at_is_read_as_written(tmp_path):
    path = write_file(tmp_path, "3001-alpha.md", _doc(
        "3001-alpha", verified_at=f"{{{ALPHA}: ' {'F' * 40} ', {BETA}: , '': {'a' * 40}, "
        f"SVC-ALPHA: {'1' * 40}}}",
    ))

    doc = parse_document(path)

    assert doc.verified_at == {ALPHA: "F" * 40, BETA: ""}
    assert any("no repository name" in w for w in doc.field_warnings)
    assert any("twice" in w for w in doc.field_warnings)


def test_the_templates_record_anchors_per_repository():
    templates = Path(provenance.__file__).parent / "templates"
    for name in ("engmem.save.md", "engmem.save.quick.md"):
        text = (templates / name).read_text(encoding="utf-8")
        assert "rev-parse HEAD" in text and "verified_at: {platform-core: <sha>}" in text, name
    start = (templates / "engmem.start.md").read_text(encoding="utf-8")
    assert "\nverified_at:" in start and "verified_at_commit" not in start
