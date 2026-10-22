"""After an upgrade, doctor tells a compatible workflow from one the runtime refuses, install never
overwrites a template the user edited, and the running MCP server is judged by what it logged
(US-26)."""

from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

import pytest

from support import rpc
from support.paths import TESTS_DIR

from engmem import __version__
from engmem.cli import main
from engmem.install import PENDING_SUFFIX
from engmem.template_stamps import WORKFLOW, read_stamp, stamp_line, written_by_engmem

RELEASED = TESTS_DIR / "fixtures" / "released-templates"
EDIT = "\nMy team runs `make check` before every save.\n"


def _install(agent: str, store: Path, capsys) -> int:
    code = main(["install", "--agent", agent, "--store", str(store)])
    assert main(["store", "set", str(store)]) == 0
    return code


def _doctor(capsys, *args: str) -> tuple[int, list[str]]:
    capsys.readouterr()
    code = main(["doctor", *args])
    return code, capsys.readouterr().out.splitlines()


def _with(lines: list[str], status: str, subject: str) -> list[str]:
    return [line for line in lines if line.startswith(f"{status}: {subject}:")]


def _claude_start(home: Path) -> Path:
    return home / ".claude" / "commands" / "engmem.md"


def _copilot_start(home: Path) -> Path:
    return home / ".copilot" / "skills" / "engmem" / "SKILL.md"


def _restamped(path: Path, version: str, workflow: int) -> None:
    """The file as an engmem of `version` writing `workflow` would have stamped it."""
    stamp, body = read_stamp(path.read_text(encoding="utf-8"))
    line = stamp_line("engmem.start.md", version, body).replace(
        f" workflow {WORKFLOW} ", f" workflow {workflow} "
    )
    path.write_text(_insert_stamp(body, line), encoding="utf-8")


def _insert_stamp(body: str, line: str) -> str:
    head, _, rest = body.partition("---\n")
    front, _, after = rest.partition("---\n")
    return f"{head}---\n{front}---\n{line}\n{after}"


# ---------------------------------------------------------------------------
# AC-26.1: a workflow the runtime refuses is an error, with versions, client and action
# ---------------------------------------------------------------------------


def test_a_released_skill_from_an_older_workflow_is_an_incompatible_error(
    home, tmp_path, shell_path, capsys
):
    _install("copilot-cli", tmp_path / "notes", capsys)
    skill = _copilot_start(home)
    shutil.copyfile(RELEASED / "v0.1.2-copilot-cli-engmem-SKILL.md", skill)

    code, lines = _doctor(capsys, "--agent", "copilot-cli")

    [line] = _with(lines, "error", "skill")
    assert str(skill) in line and "incompatible" in line
    assert "engmem 0.1.2" in line and f"engmem {__version__}" in line, "both versions are named"
    assert "Copilot CLI" in line, "the client affected is named"
    assert "engmem install --agent copilot-cli" in line and "new Copilot CLI session" in line
    assert "you edited it" not in line, "a released body is engmem's own"
    assert code == 1


def test_a_template_from_a_newer_workflow_is_an_incompatible_error(
    home, tmp_path, shell_path, capsys
):
    _install("claude", tmp_path / "notes", capsys)
    _restamped(_claude_start(home), "9.9.9", WORKFLOW + 1)

    code, lines = _doctor(capsys)

    [line] = _with(lines, "error", "template")
    assert "incompatible" in line and "engmem 9.9.9" in line and "upgrade the engmem" in line
    assert code == 1


def test_an_older_template_of_the_same_workflow_is_ok(home, tmp_path, shell_path, capsys):
    """Severity follows compatibility, not age."""
    _install("claude", tmp_path / "notes", capsys)
    _restamped(_claude_start(home), "0.0.9", WORKFLOW)

    code, lines = _doctor(capsys)

    [line] = [line for line in _with(lines, "ok", "template") if "engmem.md" in line]
    assert "installed by engmem 0.0.9, compatible" in line
    assert code == 0


def test_an_unstamped_template_is_a_warning(home, tmp_path, shell_path, capsys):
    _install("claude", tmp_path / "notes", capsys)
    start = _claude_start(home)
    _stamp, body = read_stamp(start.read_text(encoding="utf-8"))
    start.write_text(body, encoding="utf-8")

    code, lines = _doctor(capsys)

    [line] = _with(lines, "warning", "template")
    assert "carries no engmem stamp" in line and "beside it" in line
    assert code == 0


def test_an_edited_template_of_the_current_workflow_is_a_warning(
    home, tmp_path, shell_path, capsys
):
    _install("claude", tmp_path / "notes", capsys)
    with _claude_start(home).open("a", encoding="utf-8") as f:
        f.write(EDIT)

    code, lines = _doctor(capsys)

    [line] = _with(lines, "warning", "template")
    assert "was edited after engmem" in line and "cannot vouch" in line
    assert code == 0


def test_an_edited_template_of_an_older_workflow_says_install_will_keep_the_edit(
    home, tmp_path, shell_path, capsys
):
    _install("claude", tmp_path / "notes", capsys)
    start = _claude_start(home)
    start.write_text(
        (RELEASED / "v0.1.4-claude-engmem.md").read_text(encoding="utf-8") + EDIT,
        encoding="utf-8",
    )

    code, lines = _doctor(capsys)

    [line] = _with(lines, "error", "template")
    assert "incompatible" in line and f"engmem.md{PENDING_SUFFIX}" in line
    assert code == 1


def test_doctor_writes_nothing_for_an_incompatible_template(home, tmp_path, shell_path, capsys):
    _install("claude", tmp_path / "notes", capsys)
    start = _claude_start(home)
    shutil.copyfile(RELEASED / "v0.1.4-claude-engmem.md", start)
    before = {p: p.read_bytes() for p in start.parent.iterdir()}

    _doctor(capsys)

    assert {p: p.read_bytes() for p in start.parent.iterdir()} == before


# ---------------------------------------------------------------------------
# AC-26.2: reinstalling brings the current workflow back
# ---------------------------------------------------------------------------


def test_reinstalling_over_a_released_skill_restores_a_daily_managed_workflow(
    home, tmp_path, shell_path, capsys, monkeypatch
):
    store = tmp_path / "notes"
    _install("copilot-cli", store, capsys)
    skill = _copilot_start(home)
    shutil.copyfile(RELEASED / "v0.1.2-copilot-cli-engmem-SKILL.md", skill)

    assert _install("copilot-cli", store, capsys) == 0, "a released body is replaced silently"
    code, lines = _doctor(capsys, "--agent", "copilot-cli")

    assert code == 0, lines
    assert not (skill.parent / f"SKILL.md{PENDING_SUFFIX}").exists()
    text = skill.read_text(encoding="utf-8")
    stamp, body = read_stamp(text)
    assert (stamp.version, stamp.workflow) == (__version__, WORKFLOW)
    assert "mode: daily" in text or "mode: <" in text, "the draft records its mode"
    assert "engmem draft create" in text, "the draft goes through engmem's writer"
    assert "baseline_unavailable" in text, "a daily draft no longer has to carry a baseline"

    # the workflow it now describes works against this runtime: a daily draft is created
    draft = (
        "---\nid: 20260115-upgrade-check\ntitle: Upgrade check\ndate: 2026-01-15\n"
        "task_date: 2026-01-15\nstatus: draft\nsuperseded_by:\nbackfilled: false\ntags: []\n"
        "entities: []\nrelated: []\ncovers_files: []\nrepos: []\nverified_at:\n"
        "capture_minutes:\nmode: daily\n---\n"
    )
    fake = io.TextIOWrapper(io.BytesIO(draft.encode("utf-8")), encoding="utf-8")
    fake.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", fake)
    capsys.readouterr()
    assert main(["draft", "create", "20260115-upgrade-check", "--store", str(store)]) == 0
    assert "mode: daily" in (store / "sessions" / "20260115-upgrade-check.md").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# AC-26.3: an edited template is never overwritten
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent, start_path",
    [("claude", _claude_start), ("copilot-cli", _copilot_start)],
    ids=["template", "skill"],
)
def test_install_leaves_an_edited_file_and_puts_its_version_beside_it(
    home, tmp_path, shell_path, capsys, agent, start_path
):
    store = tmp_path / "notes"
    _install(agent, store, capsys)
    start = start_path(home)
    with start.open("a", encoding="utf-8") as f:
        f.write(EDIT)
    edited = start.read_bytes()
    capsys.readouterr()

    code = _install(agent, store, capsys)
    out = capsys.readouterr().out

    pending = start.with_name(start.name + PENDING_SUFFIX)
    assert code == 2
    assert out.startswith("engmem install incomplete:"), out
    assert start.read_bytes() == edited, "the user's edit is untouched"
    stamp, body = read_stamp(pending.read_text(encoding="utf-8"))
    assert written_by_engmem(stamp, body) and stamp.version == __version__
    assert f"conflict: {start}" in out and str(pending) in out


def test_an_unknown_unstamped_body_is_kept_as_an_edit(home, tmp_path, shell_path, capsys):
    """No stamp and no released digest: engmem cannot prove it wrote the file, so it keeps it."""
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    start.write_text("---\ndescription: my own start\n---\nMy own steps.\n", encoding="utf-8")

    assert _install("claude", store, capsys) == 2
    assert start.read_text(encoding="utf-8").endswith("My own steps.\n")


def test_a_body_identical_to_the_new_one_is_replaced_whatever_its_stamp(
    home, tmp_path, shell_path, capsys
):
    """Nothing can be lost by writing the same text, so a lost stamp is no conflict."""
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    _stamp, body = read_stamp(start.read_text(encoding="utf-8"))
    start.write_text(body, encoding="utf-8")

    assert _install("claude", store, capsys) == 0
    assert read_stamp(start.read_text(encoding="utf-8"))[0] is not None


def test_taking_engmems_version_resolves_the_conflict(home, tmp_path, shell_path, capsys):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    with start.open("a", encoding="utf-8") as f:
        f.write(EDIT)
    _install("claude", store, capsys)
    pending = start.with_name(start.name + PENDING_SUFFIX)

    pending.replace(start)

    assert _install("claude", store, capsys) == 0
    assert not pending.exists()


def test_a_conflict_the_user_resolved_by_hand_leaves_no_stale_new_file(
    home, tmp_path, shell_path, capsys
):
    """Restoring engmem's own text in place resolves it too; the old `.engmem-new` goes."""
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    original = start.read_bytes()
    start.write_bytes(original + EDIT.encode("utf-8"))
    _install("claude", store, capsys)
    start.write_bytes(original)

    assert _install("claude", store, capsys) == 0
    assert not start.with_name(start.name + PENDING_SUFFIX).exists()


def test_a_released_body_with_windows_line_endings_is_still_engmems(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    released = (RELEASED / "v0.1.4-claude-engmem.md").read_text(encoding="utf-8")
    start.write_bytes(released.replace("\n", "\r\n").encode("utf-8"))

    assert _install("claude", store, capsys) == 0
    assert read_stamp(start.read_text(encoding="utf-8"))[0].workflow == WORKFLOW


def test_uninstall_also_removes_the_version_left_beside_an_edit(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "notes"
    _install("copilot-cli", store, capsys)
    start = _copilot_start(home)
    with start.open("a", encoding="utf-8") as f:
        f.write(EDIT)
    _install("copilot-cli", store, capsys)

    assert main(["uninstall", "--agent", "copilot-cli", "--store", str(store)]) == 0
    assert not start.with_name(start.name + PENDING_SUFFIX).exists()
    assert not start.parent.exists(), "the skill directory is pruned once it is empty"


# ---------------------------------------------------------------------------
# AC-26.4: the running server is judged by what it logged, never by the package
# ---------------------------------------------------------------------------


def _log_row(store: Path, name: str, **fields) -> None:
    row = {"ts": "2026-01-02T03:04:05+00:00", "channel": "mcp", **fields}
    with (store / name).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _runtime(lines: list[str]) -> str:
    [line] = [line for line in lines if ": mcp runtime: " in line]
    return line


def test_no_logged_mcp_call_leaves_the_runtime_unverified(home, tmp_path, shell_path, capsys):
    _install("claude-desktop", tmp_path / "notes", capsys)

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    line = _runtime(lines)
    assert line.startswith("unverified:") and "restart Claude Desktop" in line


@pytest.mark.parametrize(
    "fields, reason",
    [
        pytest.param({}, "did not log its version", id="pre-version-row"),
        pytest.param({"engmem_version": "0.0.1"}, "came from engmem 0.0.1", id="old-server"),
    ],
)
def test_a_server_not_shown_to_be_current_stays_unverified(
    home, tmp_path, shell_path, capsys, fields, reason
):
    store = tmp_path / "notes"
    _install("claude-desktop", store, capsys)
    _log_row(store, "telemetry.jsonl", **fields)

    code, lines = _doctor(capsys, "--agent", "claude-desktop")

    line = _runtime(lines)
    assert line.startswith("unverified:") and reason in line and "restart" in line
    assert code == 0


def test_a_call_logged_by_the_restarted_server_confirms_the_runtime(
    home, tmp_path, shell_path, capsys
):
    """After the restart, one search through the client is the evidence, read from the log."""
    store = tmp_path / "notes"
    _install("claude-desktop", store, capsys)
    _log_row(store, "telemetry.jsonl", engmem_version="0.0.1")
    rpc.run(store, rpc.tools_call_msg(1, arguments={"query": "anything"}))

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    line = _runtime(lines)
    assert line.startswith("ok:") and f"came from engmem {__version__}" in line


def test_the_newest_row_across_both_logs_decides(home, tmp_path, shell_path, capsys):
    store = tmp_path / "notes"
    _install("claude-desktop", store, capsys)
    _log_row(store, "telemetry.jsonl", engmem_version=__version__)
    _log_row(store, "cost.jsonl", ts="2026-01-03T00:00:00+00:00", engmem_version="0.0.1")
    _log_row(store, "telemetry.jsonl", ts="2026-01-04T00:00:00+00:00", channel="cli",
             engmem_version="0.0.1")

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    line = _runtime(lines)
    assert line.startswith("unverified:") and "2026-01-03" in line, "a CLI row is not the server"


# ---------------------------------------------------------------------------
# review round 1: the guards around the conflict, the write and the logs
# ---------------------------------------------------------------------------


def test_an_edit_saved_in_another_encoding_is_kept(home, tmp_path, shell_path, capsys):
    """Bytes that are not UTF-8 cannot be shown to be engmem's, so they are the user's."""
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    edited = start.read_bytes() + "\nЗаметка команды.\n".encode("cp1251")
    start.write_bytes(edited)

    assert _install("claude", store, capsys) == 2
    assert start.read_bytes() == edited
    assert start.with_name(start.name + PENDING_SUFFIX).is_file()


def test_the_advertised_resolution_brings_an_old_edited_file_to_the_current_workflow(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    start.write_text(
        (RELEASED / "v0.1.4-claude-engmem.md").read_text(encoding="utf-8") + EDIT,
        encoding="utf-8",
    )
    capsys.readouterr()
    _install("claude", store, capsys)
    out = capsys.readouterr().out
    pending = start.with_name(start.name + PENDING_SUFFIX)
    assert f"carry any edit you want to keep into {pending.name}" in out

    # what the conflict line says to do: the edit into the new file, the new file over the old
    with pending.open("a", encoding="utf-8") as f:
        f.write(EDIT)
    pending.replace(start)
    code, lines = _doctor(capsys)

    assert not _with(lines, "error", "template"), lines
    [line] = _with(lines, "warning", "template")
    assert "was edited after engmem" in line
    assert code == 0
    assert _install("claude", store, capsys) == 2, "an edited file is compared on every install"


def test_a_template_write_that_fails_leaves_the_installed_one_whole(
    home, tmp_path, shell_path, capsys, monkeypatch
):
    """A torn template would read as the user's edit and never be repaired."""
    import engmem.user_files as user_files

    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    before = start.read_bytes()
    _restamped(start, "0.0.9", WORKFLOW)
    restamped = start.read_bytes()

    def failing_commit(tmp_path_: Path, target: Path) -> None:
        raise OSError(28, "No space left on device")

    # a context of its own: `monkeypatch.undo()` would also undo the sandboxed HOME
    with monkeypatch.context() as patch:
        patch.setattr(user_files, "commit", failing_commit)
        assert main(["install", "--agent", "claude", "--store", str(store)]) == 2

    assert start.read_bytes() == restamped, "nothing half-written over the installed template"
    assert not [p for p in start.parent.iterdir() if p.name.endswith(".tmp")]
    assert _install("claude", store, capsys) == 0
    assert start.read_bytes() == before


def test_a_conflict_on_one_file_still_installs_the_rest(home, tmp_path, shell_path, capsys):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    commands = home / ".claude" / "commands"
    with (commands / "engmem.md").open("a", encoding="utf-8") as f:
        f.write(EDIT)
    _restamped(commands / "engmem.save.md", "0.0.9", 0)
    rule_file = home / ".claude" / "CLAUDE.md"
    rule_file.unlink()

    assert _install("claude", store, capsys) == 2

    stamp, _body = read_stamp((commands / "engmem.save.md").read_text(encoding="utf-8"))
    assert (stamp.version, stamp.workflow) == (__version__, WORKFLOW)
    assert "engmem search" in rule_file.read_text(encoding="utf-8")


def test_doctor_names_a_version_waiting_beside_an_edit(home, tmp_path, shell_path, capsys):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    with start.open("a", encoding="utf-8") as f:
        f.write(EDIT)
    _install("claude", store, capsys)

    code, lines = _doctor(capsys)

    [line] = _with(lines, "warning", "template")
    assert "waits beside it" in line and f"engmem.md{PENDING_SUFFIX}" in line
    assert code == 0


def test_a_claude_uninstall_removes_the_version_left_beside_an_edit(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    with start.open("a", encoding="utf-8") as f:
        f.write(EDIT)
    _install("claude", store, capsys)

    assert main(["uninstall", "--agent", "claude", "--store", str(store)]) == 0
    assert not start.with_name(start.name + PENDING_SUFFIX).exists()


def test_a_directory_where_a_template_belongs_is_a_named_failure(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    start.unlink()
    start.mkdir()
    capsys.readouterr()

    assert main(["install", "--agent", "claude", "--store", str(store)]) == 2
    assert f"cannot read the template {start}" in capsys.readouterr().err


def test_an_unreadable_log_leaves_the_runtime_unverified(home, tmp_path, shell_path, capsys):
    store = tmp_path / "notes"
    _install("claude-desktop", store, capsys)
    (store / "telemetry.jsonl").write_bytes(b'{"ts": "\xff\xfe", "channel": "mcp"}\n')

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    assert _runtime(lines).startswith("unverified:")


def test_log_values_cannot_forge_doctor_lines(home, tmp_path, shell_path, capsys):
    store = tmp_path / "notes"
    _install("claude-desktop", store, capsys)
    _log_row(store, "telemetry.jsonl", engmem_version="0.0.1\nerror: forged: \x1b[2J")
    # an unparseable timestamp that sorts last is not the newest row
    _log_row(store, "cost.jsonl", ts="zzzz", engmem_version=__version__)

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    assert not [line for line in lines if line.startswith("error: forged")]
    assert not [line for line in lines if "\x1b" in line]
    line = _runtime(lines)
    assert line.startswith("unverified:") and "0.0.1\\nerror: forged" in line


# ---------------------------------------------------------------------------
# review round 2: the merge file is the user's once edited; the server's own store
# ---------------------------------------------------------------------------


def _conflicted(home: Path, store: Path, capsys) -> tuple[Path, Path]:
    _install("claude", store, capsys)
    start = _claude_start(home)
    with start.open("a", encoding="utf-8") as f:
        f.write(EDIT)
    _install("claude", store, capsys)
    return start, start.with_name(start.name + PENDING_SUFFIX)


def test_a_rerun_never_overwrites_edits_carried_into_the_new_file(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "notes"
    start, pending = _conflicted(home, store, capsys)
    with pending.open("a", encoding="utf-8") as f:
        f.write("\nMY CARRIED EDIT\n")
    carried = pending.read_bytes()
    edited = start.read_bytes()
    capsys.readouterr()

    assert _install("claude", store, capsys) == 2
    out = capsys.readouterr().out

    assert pending.read_bytes() == carried and start.read_bytes() == edited
    assert f"{pending} holds edits you are carrying into it" in out


def test_uninstall_keeps_a_new_file_the_user_edited(home, tmp_path, shell_path, capsys):
    store = tmp_path / "notes"
    _start, pending = _conflicted(home, store, capsys)
    with pending.open("a", encoding="utf-8") as f:
        f.write("\nMY CARRIED EDIT\n")
    capsys.readouterr()

    assert main(["uninstall", "--agent", "claude", "--store", str(store)]) == 0

    assert pending.is_file()
    assert f"kept {pending}" in capsys.readouterr().out


def test_the_runtime_is_read_from_the_store_the_mcp_entry_names(
    home, tmp_path, shell_path, capsys
):
    """Two stores are a choice doctor accepts; the server logs to its own."""
    server_store, cli_store = tmp_path / "server", tmp_path / "cli"
    assert main(["install", "--agent", "claude-desktop", "--store", str(server_store)]) == 0
    (cli_store / "sessions").mkdir(parents=True)
    assert main(["store", "set", str(cli_store)]) == 0
    rpc.run(server_store, rpc.tools_call_msg(1, arguments={"query": "anything"}))

    _code, lines = _doctor(capsys, "--agent", "claude-desktop")

    line = _runtime(lines)
    assert line.startswith("ok:") and f"the store its MCP entry names, {server_store}" in line


def test_an_edited_new_file_outlives_a_template_that_is_engmems_again(
    home, tmp_path, shell_path, capsys
):
    """Restoring the template in place does not make the merge file disposable."""
    store = tmp_path / "notes"
    _install("claude", store, capsys)
    start = _claude_start(home)
    original = start.read_bytes()
    start.write_bytes(original + EDIT.encode("utf-8"))
    _install("claude", store, capsys)
    pending = start.with_name(start.name + PENDING_SUFFIX)
    with pending.open("a", encoding="utf-8") as f:
        f.write("\nMY CARRIED EDIT\n")
    start.write_bytes(original)

    assert _install("claude", store, capsys) == 0
    assert "MY CARRIED EDIT" in pending.read_text(encoding="utf-8")
