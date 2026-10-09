"""US-12: when a repository's checkout has moved past its anchor, the snapshot line says whether
the record's covered files changed between the two commits, and never whether the text is true."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import write_file
from mcp_harness import _call, _run, _text, _tools_call_msg

from engmem import provenance
from engmem.cli import main
from engmem.provenance import CoveredState, SessionCheckout, SnapshotState, snapshots
from engmem.spine import load_store
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
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    return completed.stdout.strip()


def _write(checkout: Path, files: dict[str, str | None]) -> None:
    for name, content in files.items():
        target = checkout / name
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))


def _commit(checkout: Path, files: dict[str, str | None]) -> str:
    _write(checkout, files)
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "change")
    return _git(checkout, "rev-parse", "HEAD")


INITIAL = {
    "src/a.py": "a = 1\n",
    "src/b.py": "b = 1\n",
    "src/deep/c.py": "c = 1\n",
    "README.md": "readme\n",
}


def _checkout(
    root: Path, name: str = ALPHA, files: dict[str, str] | None = None
) -> tuple[Path, str]:
    path = root / "checkouts" / name
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    return path, _commit(path, dict(INITIAL if files is None else files))


def _doc(doc_id: str, *, repos: str, verified_at: str, covers: str) -> str:
    return (
        f"---\nid: {doc_id}\ntitle: {doc_id} Settings\ndate: 2026-01-01\ntask_date: 2026-01-01\n"
        f"status: active\nsuperseded_by:\nbackfilled: false\ntags: []\nentities: [Settings]\n"
        f"related: []\ncovers_files: {covers}\nrepos: {repos}\nverified_at: {verified_at}\n"
        "---\n\n## Decision Log\n\nSettings are read once.\n"
    )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    (tmp_path / "store" / "sessions").mkdir(parents=True)
    return tmp_path / "store"


def _add(store: Path, doc_id: str, anchor: str, covers: str, *, repos: str = f"[{ALPHA}]",
         verified_at: str | None = None) -> None:
    anchors = verified_at if verified_at is not None else f"{{{ALPHA}: '{anchor}'}}"
    write_file(store / "sessions", f"{doc_id}.md",
               _doc(doc_id, repos=repos, verified_at=anchors, covers=covers))


def _cli(store: Path, capsys) -> str:
    assert main(["search", QUERY, "--store", str(store)]) == 0
    return capsys.readouterr().out


def _mcp(store: Path) -> str:
    result, is_error = _call(store, name="engmem_search", arguments={"query": QUERY})
    assert not is_error, _text(result)
    return _text(result)


def _line(output: str, doc_id: str) -> str:
    block = next(b for b in output.split("\n\n") if b.startswith(f"### {doc_id} "))
    return next(line for line in block.splitlines() if line.startswith("snapshot: "))


def _moved(anchor: str, head: str) -> str:
    return f"snapshot: {ALPHA} {anchor[:7]}: HEAD is now {head[:7]}"


@pytest.fixture
def moved(tmp_path, monkeypatch):
    """A checkout whose `HEAD` moved one commit past the anchor, changing `files`."""

    def make(files: dict[str, str | None]) -> tuple[Path, str, str]:
        checkout, anchor = _checkout(tmp_path)
        head = _commit(checkout, files)
        monkeypatch.chdir(checkout)
        return checkout, anchor, head

    return make


@pytest.fixture
def git_calls(monkeypatch) -> list[bytes]:
    """Every `git cat-file` request batch, in order."""
    calls: list[bytes] = []
    real = provenance._run_cat_file

    def record(git, directory, requests):
        calls.append(requests)
        return real(git, directory, requests)

    monkeypatch.setattr(provenance, "_run_cat_file", record)
    return calls


# AC-12.1 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("covers", [
    pytest.param("[src/a.py]", id="file"),
    pytest.param("[src/a.py, src/deep/c.py]", id="files"),
    pytest.param("[src/deep]", id="directory"),
    pytest.param("['  src/a.py  ', '']", id="spaced-and-blank"),
])
def test_ac_12_1_a_change_elsewhere_is_no_signal(store, moved, capsys, covers):
    _, anchor, head = moved({"README.md": "readme 2\n", "src/b.py": "b = 2\n"})
    _add(store, "1201-alpha", anchor, covers)

    line = _line(_cli(store, capsys), "1201-alpha")

    assert line == f"{_moved(anchor, head)}, covered files unchanged"
    for claim in ("re-check", "verified", "valid", "true", "correct", "up to date"):
        assert claim not in line


def test_ac_12_1_the_check_is_a_value_next_to_the_commit_difference(store, moved):
    _, anchor, head = moved({"README.md": "readme 2\n"})
    _add(store, "1202-alpha", anchor, "[src/a.py]")
    (doc,) = load_store(store / "sessions").docs

    (snapshot,) = snapshots(doc, SessionCheckout())

    assert (snapshot.state, snapshot.current) == (SnapshotState.DIFFERS, head)
    assert snapshot.covered.state is CoveredState.UNCHANGED


def test_ac_12_1_an_abbreviated_anchor_is_checked_like_a_full_one(store, moved, capsys):
    _, anchor, head = moved({"README.md": "readme 2\n"})
    _add(store, "1203-alpha", anchor[:10], "[src/a.py]")

    assert _line(_cli(store, capsys), "1203-alpha") == (
        f"{_moved(anchor, head)}, covered files unchanged"
    )


# AC-12.2 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("files, covers, shown", [
    pytest.param({"src/a.py": "a = 2\n"}, "[src/a.py, src/b.py]", "covered file changed: src/a.py",
                 id="changed"),
    pytest.param({"src/a.py": None}, "[src/a.py]", "covered file changed: src/a.py (deleted)",
                 id="deleted"),
    pytest.param({"src/a.py": None, "src/renamed.py": "a = 1\n"}, "[src/a.py]",
                 "covered file changed: src/a.py (deleted)", id="renamed"),
    pytest.param({"src/deep/new.py": "n = 1\n"}, "[src/deep]", "covered file changed: src/deep",
                 id="file-added-to-a-covered-directory"),
    pytest.param({"src/deep/c.py": None}, "[src/deep/c.py]",
                 "covered file changed: src/deep/c.py (deleted)", id="its-directory-deleted"),
    pytest.param({"src/a.py": "a = 2\n", "src/b.py": None}, "[src/b.py, README.md, src/a.py]",
                 "covered files changed: src/b.py (deleted), src/a.py", id="in-covers-order"),
])
def test_ac_12_2_a_changed_or_deleted_covered_file_is_named_and_asks_for_review(
    store, moved, capsys, files, covers, shown
):
    _, anchor, head = moved(files)
    _add(store, "1204-alpha", anchor, covers)

    out = _cli(store, capsys)
    line = _line(out, "1204-alpha")

    assert line == f"{_moved(anchor, head)}, {shown}, re-check"
    assert out.startswith("### 1204-alpha (score: "), "still a hit, ranked as before"
    for verdict in ("false", "stale", "invalid", "superseded", "outdated", "wrong"):
        assert verdict not in line


def test_ac_12_2_a_mode_change_is_a_change(store, moved, capsys):
    checkout, anchor, _ = moved({})
    _git(checkout, "update-index", "--chmod=+x", "src/a.py")
    _git(checkout, "commit", "-q", "-m", "mode")
    head = _git(checkout, "rev-parse", "HEAD")
    _add(store, "1205-alpha", anchor, "[src/a.py]")

    assert _line(_cli(store, capsys), "1205-alpha") == (
        f"{_moved(anchor, head)}, covered file changed: src/a.py, re-check"
    )


def _raw_tree(checkout: Path, commit: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "tree", f"{commit}^{{tree}}"],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
    ).stdout


def _commit_tree(checkout: Path, tree: bytes, parent: str) -> str:
    """A commit of `tree` as written, byte for byte, on top of `parent`, made HEAD."""
    tree_id = subprocess.run(
        ["git", "-C", str(checkout), "hash-object", "-t", "tree", "--literally", "-w", "--stdin"],
        check=True, capture_output=True, input=tree,
    ).stdout.decode("ascii").strip()
    commit = _git(checkout, "commit-tree", tree_id, "-p", parent, "-m", "rewritten")
    _git(checkout, "update-ref", "HEAD", commit)
    return commit


def test_ac_12_1_a_zero_padded_directory_mode_is_the_same_directory(store, moved, capsys):
    """Older tools wrote `040000`; git reads the number, so the same subtree is unchanged in
    either direction."""
    checkout, anchor, _ = moved({})
    original = _raw_tree(checkout, anchor)
    assert b"40000 src\0" in original
    padded = _commit_tree(checkout, original.replace(b"40000 src\0", b"040000 src\0"), anchor)
    _add(store, "1210-forward", anchor, "[src/deep/c.py, src/deep]")
    forward = _line(_cli(store, capsys), "1210-forward")

    back = _commit_tree(checkout, original, padded)
    _add(store, "1211-back", padded, "[src/deep/c.py, src/deep]")
    backward = _line(_cli(store, capsys), "1211-back")

    assert forward == f"{_moved(anchor, padded)}, covered files unchanged"
    assert backward == (
        f"snapshot: {ALPHA} {padded[:7]}: HEAD is now {back[:7]}, covered files unchanged"
    )


def test_ac_12_3_a_tree_mode_python_would_parse_but_git_never_writes_is_unreadable_output(
    store, moved, capsys
):
    checkout, anchor, _ = moved({})
    original = _raw_tree(checkout, anchor)
    assert b"100644 README.md\0" in original
    head = _commit_tree(
        checkout, original.replace(b"100644 README.md\0", b"0o100644 README.md\0"), anchor
    )
    _add(store, "1212-alpha", anchor, "[README.md]")

    assert _line(_cli(store, capsys), "1212-alpha") == (
        f"{_moved(anchor, head)}, covered files not checked: git printed output engmem cannot "
        "read, re-check"
    )


def test_ac_12_2_many_changed_files_are_named_up_to_the_cap(store, tmp_path, monkeypatch, capsys):
    files = {f"src/m{n}.py": "x\n" for n in range(6)}
    checkout, anchor = _checkout(tmp_path, files=files)
    head = _commit(checkout, {name: "y\n" for name in files})
    monkeypatch.chdir(checkout)
    _add(store, "1206-alpha", anchor, f"[{', '.join(files)}]")

    assert _line(_cli(store, capsys), "1206-alpha") == (
        f"{_moved(anchor, head)}, covered files changed: src/m0.py, src/m1.py, src/m2.py and 3 "
        "more, re-check"
    )


def test_ac_12_2_a_change_is_reported_even_when_another_entry_cannot_be_checked(
    store, moved, capsys
):
    _, anchor, head = moved({"src/a.py": "a = 2\n"})
    _add(store, "1207-alpha", anchor, "[../outside.py, src/a.py, missing.py]")

    assert _line(_cli(store, capsys), "1207-alpha") == (
        f"{_moved(anchor, head)}, covered file changed: src/a.py, re-check"
    )


# AC-12.3 ---------------------------------------------------------------------------------------


def _not_checked(anchor: str, head: str, reason: str) -> str:
    return f"{_moved(anchor, head)}, covered files not checked: {reason}, re-check"


def _cat_file_answers(monkeypatch, answer) -> None:
    """`git cat-file` answers `answer` (or raises it); the checkout lookup still runs git."""

    def run(git, directory, requests):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(provenance, "_run_cat_file", run)


def test_ac_12_3_an_anchor_this_checkout_does_not_have_is_unknown(store, moved, capsys):
    _, _, head = moved({"README.md": "readme 2\n"})
    fake = "deadbee" + "0" * 33
    _add(store, "1301-alpha", fake, "[src/a.py]")

    assert _line(_cli(store, capsys), "1301-alpha") == _not_checked(
        fake, head, "commit deadbee is not in this checkout"
    )


def test_ac_12_3_an_ambiguous_anchor_is_unknown(store, moved, capsys, monkeypatch):
    _, anchor, head = moved({"README.md": "readme 2\n"})
    _add(store, "1302-alpha", anchor[:7], "[src/a.py]")
    _cat_file_answers(monkeypatch, (0, f"{anchor[:7]}^{{commit}} ambiguous\n".encode(), b""))

    assert _line(_cli(store, capsys), "1302-alpha") == _not_checked(
        anchor, head, f"the anchor {anchor[:7]} is ambiguous in this checkout"
    )


def test_ac_12_3_a_branch_named_like_the_anchor_does_not_stand_in_for_it(store, moved, capsys):
    checkout, anchor, head = moved({"src/a.py": "a = 2\n"})
    short = anchor[:7]
    _git(checkout, "branch", short, "HEAD")
    _add(store, "1303-alpha", short, "[src/a.py]")

    assert _line(_cli(store, capsys), "1303-alpha") == _not_checked(
        anchor, head, f"the anchor {short} names a branch or tag here, not a commit"
    )


@pytest.mark.parametrize("covers, shown", [
    pytest.param("[a.py]", "a.py", id="a-bare-name-from-a-subdirectory"),
    pytest.param("['src/*.py']", "src/*.py", id="a-glob-is-a-literal-name"),
    pytest.param("[':(glob)src/a.py']", ":(glob)src/a.py", id="pathspec-magic-is-a-literal-name"),
    pytest.param("[src/new.py]", "src/new.py", id="added-after-the-anchor"),
    pytest.param("[src/a.py/x]", "src/a.py/x", id="below-a-file"),
])
def test_ac_12_3_a_covered_file_the_anchored_commit_lacks_is_unknown(
    store, moved, capsys, covers, shown
):
    _, anchor, head = moved({"src/new.py": "n\n"})
    _add(store, "1304-alpha", anchor, covers)

    assert _line(_cli(store, capsys), "1304-alpha") == _not_checked(
        anchor, head, f"{shown} is not in commit {anchor[:7]}"
    )


@pytest.mark.parametrize("covers, shown", [
    pytest.param("[/etc/passwd]", "/etc/passwd", id="absolute"),
    pytest.param("['../outside.py']", "../outside.py", id="parent"),
    pytest.param("[src/../src/a.py]", "src/../src/a.py", id="parent-inside"),
    pytest.param("[./src/a.py]", "./src/a.py", id="dot"),
    pytest.param("[src//a.py]", "src//a.py", id="empty-component"),
    pytest.param("[src/a.py/]", "src/a.py/", id="trailing-slash"),
    pytest.param('["src/a.py\\nsrc/b.py"]', "src/a.py\\nsrc/b.py", id="line-break"),
    pytest.param('["src/a\\0.py"]', "src/a\\x00.py", id="nul"),
])
def test_ac_12_3_a_path_that_is_not_inside_the_repository_is_never_looked_up(
    store, moved, capsys, git_calls, covers, shown
):
    _, anchor, head = moved({"README.md": "readme 2\n"})
    _add(store, "1305-alpha", anchor, covers)

    assert _line(_cli(store, capsys), "1305-alpha") == _not_checked(
        anchor, head, f"covered file {shown} is not a path inside the repository"
    )
    assert git_calls == []


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="needs POSIX permissions that bind the test's own user")
def test_ac_12_3_a_tree_git_cannot_read_is_unknown_and_never_a_deletion(store, moved, capsys):
    checkout, anchor, head = moved({"src/a.py": "a = 2\n"})
    tree = _git(checkout, "rev-parse", f"{head}:src")
    loose = checkout / ".git" / "objects" / tree[:2] / tree[2:]
    _add(store, "1306-alpha", anchor, "[src/a.py, README.md]")
    loose.chmod(0)
    try:
        line = _line(_cli(store, capsys), "1306-alpha")
    finally:
        loose.chmod(0o644)

    assert line == _not_checked(anchor, head, f"src cannot be read at commit {head[:7]}")


def test_ac_12_3_a_tree_git_reports_missing_under_a_parent_that_names_it_is_never_a_deletion(
    store, moved, capsys, monkeypatch
):
    """The platform-independent form of the test above: git answers `missing` for a tree its
    parent tree names, as it does for an object it cannot open or a partial clone lacks."""
    _, anchor, head = moved({"src/a.py": "a = 2\n"})
    _add(store, "1313-alpha", anchor, "[src/a.py, README.md]")
    real = provenance._run_cat_file
    asked, stand_in = f"{head}:src".encode(), f"{head}:no-such-directory".encode()

    def lose_the_tree(git, directory, requests):
        assert asked + b"\n" in requests
        returncode, stdout, stderr = real(git, directory, requests.replace(asked, stand_in))
        return returncode, stdout.replace(stand_in + b" missing", asked + b" missing"), stderr

    monkeypatch.setattr(provenance, "_run_cat_file", lose_the_tree)

    assert _line(_cli(store, capsys), "1313-alpha") == _not_checked(
        anchor, head, f"src cannot be read at commit {head[:7]}"
    )


@pytest.mark.parametrize("failure, reason", [
    pytest.param(subprocess.TimeoutExpired("git", 5), "git did not answer within 5 s",
                 id="timeout"),
    pytest.param(PermissionError(13, "denied"), "git could not be run (PermissionError)",
                 id="cannot-start"),
    pytest.param((128, b"", b"hint: x\nfatal: bad object\n"), "git cat-file failed: fatal: bad "
                 "object", id="error-line"),
    pytest.param((0, b"garbage", b""), "git printed output engmem cannot read", id="garbled"),
    pytest.param((0, b"", b""), "git printed output engmem cannot read", id="empty"),
])
def test_ac_12_3_git_that_fails_is_a_reason(store, moved, capsys, monkeypatch, failure, reason):
    _, anchor, head = moved({"README.md": "readme 2\n"})
    _add(store, "1307-alpha", anchor, "[src/a.py]")
    _cat_file_answers(monkeypatch, failure)

    assert _line(_cli(store, capsys), "1307-alpha") == _not_checked(anchor, head, reason)


def test_ac_12_3_output_past_the_last_answer_is_not_read_as_an_answer(
    store, moved, capsys, monkeypatch
):
    _, anchor, head = moved({"README.md": "readme 2\n"})
    _add(store, "1312-alpha", anchor, "[src/a.py]")
    real = provenance._run_cat_file

    def trailing(git, directory, requests):
        returncode, stdout, stderr = real(git, directory, requests)
        return returncode, stdout + b"extra\n", stderr

    monkeypatch.setattr(provenance, "_run_cat_file", trailing)

    assert _line(_cli(store, capsys), "1312-alpha") == _not_checked(
        anchor, head, "git printed output engmem cannot read"
    )


def test_ac_12_3_output_past_the_cap_is_not_read_as_an_answer(store, moved, capsys, monkeypatch):
    _, anchor, head = moved({"README.md": "readme 2\n"})
    _add(store, "1308-alpha", anchor, "[src/a.py]")
    monkeypatch.setattr(provenance, "_COVERED_OUTPUT_BYTES", 16)

    assert _line(_cli(store, capsys), "1308-alpha") == _not_checked(
        anchor, head, "the covered directories are too large to compare"
    )


def test_ac_12_3_too_many_covered_files_are_not_checked(store, moved, capsys):
    _, anchor, head = moved({"README.md": "readme 2\n"})
    many = ", ".join(f"src/f{n}.py" for n in range(provenance.MAX_COVERED_FILES + 1))
    _add(store, "1309-alpha", anchor, f"[{many}]")

    assert _line(_cli(store, capsys), "1309-alpha") == _not_checked(
        anchor, head, f"the record lists more than {provenance.MAX_COVERED_FILES} covered files"
    )


def test_ac_12_3_a_record_of_several_repositories_does_not_guess_where_a_file_is(
    store, moved, capsys, git_calls
):
    _, anchor, head = moved({"src/a.py": "a = 2\n"})
    _add(store, "1310-shared", anchor, "[src/a.py]", repos=f"[{ALPHA}, {BETA}]")

    line = _line(_cli(store, capsys), "1310-shared")

    assert line.startswith(_not_checked(
        anchor, head, "covers_files does not say which of the record's 2 repositories each file "
        "is in"
    ))
    assert f"| {BETA}: unknown, no anchor recorded" in line
    assert git_calls == []


def test_ac_12_3_unreadable_repository_links_do_not_guess_either(store, moved, capsys):
    _, anchor, head = moved({"src/a.py": "a = 2\n"})
    _add(store, "1311-alpha", anchor, "[src/a.py]", repos="{x: y}")

    assert _line(_cli(store, capsys), "1311-alpha") == _not_checked(
        anchor, head, "its repository links cannot be read"
    )


# what runs and when ----------------------------------------------------------------------------


def test_only_a_moved_checkout_of_this_repository_with_covered_files_is_diffed(
    store, moved, capsys, git_calls, tmp_path
):
    _, anchor, head = moved({"src/a.py": "a = 2\n"})
    _add(store, "1401-moved", anchor, "[src/a.py]")
    _add(store, "1402-same", head, "[src/a.py]")
    _add(store, "1403-none", anchor, "[]")

    out = _cli(store, capsys)

    assert len(git_calls) == 1
    assert _line(out, "1401-moved").endswith("covered file changed: src/a.py, re-check")
    assert _line(out, "1402-same").endswith("same commit as HEAD")
    assert _line(out, "1403-none") == f"{_moved(anchor, head)}, re-check"

    other = tmp_path / "other"
    (other / "sessions").mkdir(parents=True)
    _add(other, "1404-other", anchor, "[src/a.py]", repos=f"[{BETA}]",
         verified_at=f"{{{BETA}: '{anchor}'}}")
    assert _line(_cli(other, capsys), "1404-other").startswith(
        f"snapshot: {BETA} {anchor[:7]}: unknown, no checkout of it here"
    )
    assert len(git_calls) == 1, "another repository is never diffed"


@pytest.mark.skipif(sys.platform == "win32", reason="the marker program is a POSIX shell script")
def test_no_program_the_repository_configures_is_run(store, moved, capsys, tmp_path):
    checkout, anchor, head = moved({"src/a.py": "a = 2\n"})
    marker = tmp_path / "marker"
    program = tmp_path / "program.sh"
    program.write_text(f'#!/bin/sh\necho "$0 $*" >> "{marker}"\ncat >/dev/null\n', encoding="utf-8")
    program.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for hook in ("post-checkout", "post-index-change", "reference-transaction", "pre-commit"):
        shutil.copy(program, hooks / hook)
    for key, value in [
        ("core.fsmonitor", program), ("core.hooksPath", hooks), ("diff.external", program),
        ("diff.x.textconv", program), ("filter.x.smudge", program), ("filter.x.clean", program),
        ("filter.x.process", program), ("core.pager", program), ("core.sshCommand", program),
        ("core.alternateRefsCommand", program), ("credential.helper", f"!{program}"),
    ]:
        _git(checkout, "config", key, str(value))
    (checkout / ".git" / "info").mkdir(exist_ok=True)
    (checkout / ".git" / "info" / "attributes").write_text("* diff=x filter=x\n", encoding="utf-8")
    _add(store, "1405-alpha", anchor, "[src/a.py]")

    line = _line(_cli(store, capsys), "1405-alpha")

    assert line == f"{_moved(anchor, head)}, covered file changed: src/a.py, re-check"
    assert not marker.exists(), marker.read_text(encoding="utf-8")


def _git_version() -> tuple[int, ...]:
    text = _git(Path.cwd(), "--version").split()[2]
    return tuple(int(part) for part in text.split(".")[:2] if part.isdigit())


POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32",
                                reason="the marker program is a POSIX shell script")
LAZY_FETCH_ENV = pytest.mark.skipif(
    shutil.which("git") is None or _git_version() < (2, 44),
    reason="GIT_NO_LAZY_FETCH exists from git 2.44",
)


@pytest.mark.parametrize("remote", [
    pytest.param("ssh://example.invalid/x", marks=POSIX_ONLY, id="ssh"),
    pytest.param("ext::{program} %S", marks=POSIX_ONLY, id="ext"),
    pytest.param("file", id="file"),
])
@pytest.mark.parametrize("dropped", [
    pytest.param(None, id="both-guards"),
    pytest.param("GIT_NO_LAZY_FETCH", id="protocol-allow-list-alone"),
    pytest.param("GIT_ALLOW_PROTOCOL", marks=LAZY_FETCH_ENV, id="no-lazy-fetch-alone"),
])
def test_a_partial_clone_never_fetches_what_it_lacks(
    store, tmp_path, monkeypatch, capsys, remote, dropped
):
    """Each guard is pinned on its own: before git 2.44 only the empty allow-list holds."""
    source, anchor = _checkout(tmp_path)
    head = _commit(source, {"src/a.py": "a = 2\n"})
    bare = tmp_path / "source.git"
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    _git(bare, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "partial" / ALPHA
    _git(tmp_path, "clone", "-q", "--no-checkout", "--filter=tree:0", bare.as_uri(), str(clone))
    marker = tmp_path / "marker"
    if sys.platform != "win32":
        program = tmp_path / "program.sh"
        program.write_text(f'#!/bin/sh\necho "$0 $*" >> "{marker}"\n', encoding="utf-8")
        program.chmod(0o755)
        _git(clone, "config", "remote.origin.uploadpack", str(program))
        _git(clone, "config", "core.sshCommand", str(program))
        if remote != "file":
            _git(clone, "config", "remote.origin.url", remote.format(program=program))
    before = _git(clone, "count-objects", "-v")
    real_env = provenance._git_env
    monkeypatch.setattr(provenance, "_git_env",
                        lambda: {k: v for k, v in real_env().items() if k != dropped})
    monkeypatch.chdir(clone)
    _add(store, "1406-alpha", anchor, "[src/a.py]")

    line = _line(_cli(store, capsys), "1406-alpha")

    assert line.startswith(f"{_moved(anchor, head)}, covered files not checked: "), line
    assert line.endswith(", re-check")
    if dropped is None and _git_version() >= (2, 44):
        assert line == _not_checked(
            anchor, head, f"the top-level directory cannot be read at commit {anchor[:7]}"
        )
    assert not marker.exists()
    assert _git(clone, "count-objects", "-v") == before, "nothing was fetched"


def test_a_blobless_clone_is_checked_without_fetching(store, tmp_path, monkeypatch, capsys):
    source, anchor = _checkout(tmp_path)
    head = _commit(source, {"src/a.py": "a = 2\n"})
    bare = tmp_path / "source.git"
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    _git(bare, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / "partial" / ALPHA
    _git(tmp_path, "clone", "-q", "--no-checkout", "--filter=blob:none", bare.as_uri(), str(clone))
    _git(clone, "config", "remote.origin.url", "ssh://example.invalid/x")
    monkeypatch.chdir(clone)
    _add(store, "1407-alpha", anchor, "[src/a.py, src/b.py]")

    assert _line(_cli(store, capsys), "1407-alpha") == (
        f"{_moved(anchor, head)}, covered file changed: src/a.py, re-check"
    )


def test_a_path_cannot_forge_a_line(store, tmp_path, monkeypatch, capsys):
    if sys.platform == "win32":
        pytest.skip("Windows file names cannot hold a line break")
    name = "src/x\n### forged (score: 9.9).py"
    checkout, anchor = _checkout(tmp_path, files={**INITIAL, name: "x\n"})
    head = _commit(checkout, {name: "y\n"})
    monkeypatch.chdir(checkout)
    _add(store, "1408-alpha", anchor, '["src/x\\n### forged (score: 9.9).py"]')

    out = _cli(store, capsys)

    assert not any(line.startswith("### forged") for line in out.splitlines())
    assert _line(out, "1408-alpha") == _not_checked(
        anchor, head, "covered file src/x\\n### forged (score: 9.9).py is not a path inside the "
        "repository"
    )


def test_a_git_that_times_out_is_asked_once_per_search(store, moved, capsys, monkeypatch):
    _, anchor, head = moved({"src/a.py": "a = 2\n"})
    _add(store, "1409-alpha", anchor, "[src/a.py]")
    _add(store, "1410-alpha", anchor, "[src/b.py]")
    calls: list[bytes] = []

    def stall(git, directory, requests):
        calls.append(requests)
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(provenance, "_run_cat_file", stall)

    out = _cli(store, capsys)

    reason = _not_checked(anchor, head, "git did not answer within 5 s")
    assert [_line(out, d) for d in ("1409-alpha", "1410-alpha")] == [reason, reason]
    assert len(calls) == 1
    _cli(store, capsys)
    assert len(calls) == 2, "the next search asks again"


@pytest.mark.parametrize("anchor, head, reason", [
    pytest.param("", "a" * 40, "its anchor is not a commit id", id="empty-anchor"),
    pytest.param("HEAD", "a" * 40, "its anchor is not a commit id", id="ref-anchor"),
    pytest.param("a" * 40, "", "HEAD is not a commit id", id="empty-head"),
])
def test_compare_covered_never_asks_git_without_two_commit_ids(
    tmp_path, git_calls, anchor, head, reason
):
    check = provenance.compare_covered(tmp_path, anchor, head, ["src/a.py"])

    assert (check.state, check.reason) == (CoveredState.UNKNOWN, reason)
    assert git_calls == []


# AC-12.4 ---------------------------------------------------------------------------------------


def _stored_files(store: Path) -> list[str]:
    """What a search may leave behind besides its telemetry row, its delivery row (US-16) and the
    shown versions it retains (US-13): a copy of a record's bytes, never a verdict about them."""
    return sorted(
        p.name for p in store.rglob("*")
        if p.name not in ("telemetry.jsonl", "cost.jsonl")
        and "versions" not in p.relative_to(store).parts
    )


def test_ac_12_4_finding_the_record_again_does_not_clear_the_review(store, moved, capsys):
    _, anchor, head = moved({"src/a.py": "a = 2\n"})
    _add(store, "1501-alpha", anchor, "[src/a.py]")
    review = f"{_moved(anchor, head)}, covered file changed: src/a.py, re-check"
    before = _stored_files(store)

    lines = [_line(_cli(store, capsys), "1501-alpha") for _ in range(2)]
    _, responses, _ = _run(store, _tools_call_msg(1, arguments={"query": QUERY}),
                           _tools_call_msg(2, arguments={"query": QUERY}))
    lines += [_line(_text(response["result"]), "1501-alpha") for response in responses]
    assert lines == [review] * 4
    assert _stored_files(store) == before, "no verdict is stored anywhere"

    _add(store, "1501-alpha", head, "[src/a.py]")
    assert _line(_cli(store, capsys), "1501-alpha") == (
        f"snapshot: {ALPHA} {head[:7]}: same commit as HEAD"
    )


def test_ac_12_4_reverting_the_file_reads_as_unchanged_only_against_the_anchor(
    store, moved, capsys
):
    checkout, anchor, _ = moved({"src/a.py": "a = 2\n"})
    head = _commit(checkout, {"src/a.py": INITIAL["src/a.py"]})
    _add(store, "1502-alpha", anchor, "[src/a.py]")

    assert _line(_cli(store, capsys), "1502-alpha") == (
        f"{_moved(anchor, head)}, covered files unchanged"
    )


# both channels ---------------------------------------------------------------------------------


def test_cli_and_mcp_show_the_same_covered_files_line(store, moved, capsys):
    _, anchor, _ = moved({"src/a.py": None, "README.md": "r\n"})
    _add(store, "1601-changed", anchor, "[src/a.py]")
    _add(store, "1602-unchanged", anchor, "[src/b.py]")
    _add(store, "1603-unknown", anchor, "[nope.py]")

    cli_out = _cli(store, capsys)
    mcp_out = _mcp(store)

    assert [l for l in cli_out.splitlines() if l != UNATTRIBUTED_CLI_NOTE] == [
        l for l in mcp_out.splitlines() if l != UNATTRIBUTED_MCP_NOTE
    ]
    assert {_line(cli_out, d).split(", ", 1)[1] for d in ("1601-changed", "1602-unchanged",
                                                         "1603-unknown")} == {
        "covered file changed: src/a.py (deleted), re-check",
        "covered files unchanged",
        f"covered files not checked: nope.py is not in commit {anchor[:7]}, re-check",
    }


# capture ---------------------------------------------------------------------------------------


def test_the_templates_say_what_a_covered_path_is():
    templates = Path(provenance.__file__).parent / "templates"
    for name in ("engmem.save.md", "engmem.save.quick.md"):
        text = (templates / name).read_text(encoding="utf-8")
        assert "covers_files" in text and "top-level directory" in text, name
