"""US-17: `engmem walkthrough codex` builds a demo the owner walks in a real Codex session, against a
store of its own, and `--verify` says PASS only when the whole capture -> recall cycle left its
evidence. The real Codex run is owner acceptance (E-05); these tests cover what engmem controls."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import TRIGGER_RULE, redirect_home, requires_permission_enforcement

from engmem.cli import main

CODEX_BEGIN = "# engmem-mcp-server: begin (managed by `engmem install --agent codex`)"
FIXTURE_ID = "20261010-order-request-id"
RECALL_SESSION = "20261011-order-retry"
FOREIGN_SERVER = '[mcp_servers.other]\ncommand = "other-server"\n'
FOREIGN_RULE = "- Prefer small commits.\n"

GOOD_RETRY = '''

def place_order_with_retry(book, request_id, item, attempts=3):
    for attempt in range(attempts):
        try:
            return book.place_order(request_id, item)
        except TimeoutError:
            if attempt == attempts - 1:
                raise
'''

FRESH_ID_RETRY = '''

def place_order_with_retry(book, request_id, item, attempts=3):
    for attempt in range(attempts):
        try:
            return book.place_order(f"{request_id}-{attempt}", item)
        except TimeoutError:
            if attempt == attempts - 1:
                raise
'''

NOT_IDEMPOTENT = '''

def _always_new(self, request_id, item):
    order = {"order_id": len(self.orders) + 1, "request_id": request_id, "item": item}
    self.orders.append(order)
    return order


OrderBook.place_order = _always_new
'''


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    redirect_home(monkeypatch, home)
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture
def shell_path(tmp_path, monkeypatch) -> Path:
    """This interpreter as `engmem`, first on PATH, as doctor's own tests do."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if sys.platform == "win32":
        (bin_dir / "engmem.bat").write_bytes(f'@"{sys.executable}" -m engmem.cli %*\r\n'.encode())
    else:
        launcher = bin_dir / "engmem"
        launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -m engmem.cli \"$@\"\n")
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]))
    return bin_dir


@pytest.fixture
def real_store(home, tmp_path, shell_path, capsys) -> Path:
    """The engineer's own Codex wiring, with someone else's MCP entry and instructions in it."""
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text(FOREIGN_SERVER, encoding="utf-8")
    (codex / "AGENTS.md").write_text(FOREIGN_RULE, encoding="utf-8")
    store = tmp_path / "real-store"
    assert main(["install", "--agent", "codex", "--store", str(store)]) == 0
    assert main(["store", "set", str(store)]) == 0
    _write(store / "sessions" / "20260101-real.md", _record("20260101-real", "a real decision"))
    capsys.readouterr()
    return store


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _record(
    doc_id: str,
    decision: str,
    *,
    status: str = "active",
    reason: str = "a client retries after a timeout, and a second order would charge twice.",
    source: str = "demo_orders/orders.py, OrderBook.place_order",
) -> str:
    return (
        f"---\nid: {doc_id}\ntitle: Orders keyed by request_id\ndate: 2026-10-10\n"
        f"task_date: 2026-10-10\nstatus: {status}\nsuperseded_by:\nbackfilled: false\n"
        "tags: [orders]\nentities: [OrderBook]\nrelated: []\ncovers_files: []\n"
        "verified_at_commit:\ncapture_minutes:\nmode: daily\n---\n\n"
        "## Decision Log\n\n"
        f"- Decision: {decision}\n- Reason: {reason}\n"
        "- Rejected alternative: a new request_id per attempt, which places a new order each time.\n"
        f"- Source: {source}\n\n"
        "## Future LLM Context (cold-start primer)\n\n"
        "OrderBook.place_order returns the existing order for a repeated request_id.\n"
        "A retry must reuse the request_id of the first attempt.\n\n"
        "## Reuse Log\n\nPrior docs used: none.\n\n## Search Trace\n\nshell\n"
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _setup(demo: Path, capsys) -> tuple[int, str]:
    code = main(["walkthrough", "codex", "--dir", str(demo)])
    return code, capsys.readouterr().out


def _verify(demo: Path, capsys) -> tuple[int, str]:
    code = main(["walkthrough", "codex", "--verify", "--dir", str(demo)])
    return code, capsys.readouterr().out


def _check(workspace: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(workspace)}
    env.pop("PYTHONSAFEPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "demo_orders.check"], cwd=workspace, env=env,
        capture_output=True, text=True, timeout=60,
    )


def _add_retry(demo: Path, code: str = GOOD_RETRY) -> None:
    orders = demo / "workspace" / "demo_orders" / "orders.py"
    orders.write_bytes(orders.read_bytes() + code.encode("utf-8"))


def _save_fixture(
    demo: Path,
    decision: str = "repeating an operation with the same request_id does not create a second order.",
    **overrides,
) -> None:
    _write(demo / "store" / "sessions" / f"{FIXTURE_ID}.md", _record(FIXTURE_ID, decision, **overrides))


def _telemetry(demo: Path, *rows: dict) -> None:
    path = demo / "store" / "telemetry.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({"ts": "2026-10-11T09:00:00+00:00", "query": "retry", **row}) + "\n")


# --- AC-17.1: the demo store and the command of each step are visible -----------------------


def test_setup_builds_a_separate_demo_and_names_the_store_and_every_step(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"

    code, out = _setup(demo, capsys)

    assert code == 0, out
    store, codex_home, workspace = demo / "store", demo / "codex-home", demo / "workspace"
    assert (store / "sessions").is_dir()
    assert (workspace / "demo_orders" / "orders.py").is_file()
    assert (workspace / "demo_orders" / "check.py").is_file()
    assert (workspace / ".git").is_dir()
    config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["engmem"]["args"][-2:] == ["--store", str(store)]
    assert config["sandbox_workspace_write"]["writable_roots"] == [str(store)]
    assert config["shell_environment_policy"]["set"] == {"ENGMEM_HOME": str(store)}
    assert TRIGGER_RULE in (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert f"demo store: {store}" in out
    for step in (
        "engmem doctor --agent codex --store", "$engmem ", "$engmem-save-quick",
        "engmem search", "python -m demo_orders.check", "engmem walkthrough codex --verify",
        str(codex_home), "codex",
    ):
        assert step in out, step
    assert "error:" not in out


def test_setup_and_verify_leave_the_real_store_and_codex_wiring_byte_identical(
    real_store, home, tmp_path, capsys
):
    before = {"store": _tree(real_store), "codex": _tree(home / ".codex"),
              "skills": _tree(home / ".agents" / "skills")}
    demo = tmp_path / "demo"

    _setup(demo, capsys)
    _verify(demo, capsys)

    after = {"store": _tree(real_store), "codex": _tree(home / ".codex"),
             "skills": _tree(home / ".agents" / "skills")}
    assert after == before
    assert main(["store", "show"]) == 0
    assert f"store: {real_store}" in capsys.readouterr().out


def test_the_demo_config_names_the_store_by_its_resolved_path(real_store, tmp_path, capsys):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is not permitted on this platform")

    code, out = _setup(linked_parent / "demo", capsys)

    assert code == 0, out
    store = str(real_parent.resolve() / "demo" / "store")
    config = tomllib.loads((real_parent / "demo" / "codex-home" / "config.toml").read_text("utf-8"))
    assert config["sandbox_workspace_write"]["writable_roots"] == [store]
    assert config["shell_environment_policy"]["set"] == {"ENGMEM_HOME": store}
    assert config["mcp_servers"]["engmem"]["args"][-1] == store


def test_a_directory_that_is_not_a_walkthrough_is_refused_untouched(home, tmp_path, capsys):
    demo = tmp_path / "demo"
    _write(demo / "notes.md", "mine\n")

    code, out = _setup(demo, capsys)

    assert code == 2
    assert "not empty" in out and str(demo) in out
    assert _tree(demo) == {"notes.md": b"mine\n"}


@pytest.mark.parametrize(
    "child", ["store", "store/sessions", "codex-home", "workspace", "workspace/demo_orders"]
)
@pytest.mark.parametrize("seen_as_link", [True, False], ids=["symlink", "junction"])
def test_a_demo_directory_that_links_out_of_the_demo_is_refused(
    real_store, tmp_path, capsys, monkeypatch, child, seen_as_link
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = demo / child
    shutil.rmtree(target)
    try:
        target.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is not permitted on this platform")
    if not seen_as_link:
        # a Windows junction redirects like a link, but `is_symlink()` says False for it
        monkeypatch.setattr(Path, "is_symlink", lambda self: False)

    code, out = _setup(demo, capsys)

    assert code == 2
    assert str(target) in out
    assert ("symbolic link" if seen_as_link else "outside") in out
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("name", ["config.toml", "AGENTS.md"])
def test_a_demo_codex_file_linked_to_the_real_one_is_refused_untouched(
    real_store, home, tmp_path, capsys, name
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    real = home / ".codex" / name
    before = real.read_bytes()
    link = demo / "codex-home" / name
    link.unlink()
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is not permitted on this platform")

    code, out = _setup(demo, capsys)

    assert code == 2
    assert "symbolic link" in out and str(link) in out
    assert real.read_bytes() == before


def test_an_unknown_client_is_a_usage_error(home, tmp_path, capsys):
    code = main(["walkthrough", "claude", "--dir", str(tmp_path / "demo")])

    assert code == 2
    assert "codex" in capsys.readouterr().out
    assert not (tmp_path / "demo").exists()


# --- AC-17.3: a blocked operation names the reason and the fix, never "ready" or PASS --------


def _not_ready(out: str) -> None:
    assert "not ready" in out
    assert "$engmem-save-quick" not in out
    assert "PASS" not in out


def test_a_missing_codex_connection_stops_before_any_codex_step(
    home, tmp_path, shell_path, capsys
):
    code, out = _setup(tmp_path / "demo", capsys)

    assert code == 1
    assert "error: skill:" in out and "engmem install --agent codex" in out
    _not_ready(out)


@requires_permission_enforcement
def test_a_store_the_demo_cannot_write_stops_with_the_reason(real_store, tmp_path, capsys):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    sessions = demo / "store" / "sessions"
    sessions.chmod(0o500)
    try:
        code, out = _setup(demo, capsys)
    finally:
        sessions.chmod(0o700)

    assert code == 1
    assert "error: sessions write:" in out and "write access" in out
    _not_ready(out)


@pytest.mark.parametrize(
    ("config", "subject"),
    [
        pytest.param('sandbox_mode = "read-only"\n', "warning: sandbox:", id="read-only-sandbox"),
        pytest.param(
            '[mcp_servers.engmem]\ncommand = {missing}\n'
            'args = ["-m", "engmem.cli", "mcp"]\n',
            "error: mcp command:", id="mcp-server-cannot-start",
        ),
        pytest.param(
            '[mcp_servers.engmem]\ncommand = "python"\nargs = ["-m", "engmem.cli", "mcp", '
            '"--store", "/somewhere/else"]\n',
            "warning: mcp entry:", id="mcp-writes-another-store",
        ),
    ],
)
def test_a_sandbox_or_mcp_entry_that_blocks_the_demo_stops_with_the_reason(
    real_store, tmp_path, capsys, config, subject
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    missing = json.dumps(str(tmp_path / "nonexistent" / "python"))
    (demo / "codex-home" / "config.toml").write_text(
        config.replace("{missing}", missing), encoding="utf-8"
    )

    code, out = _setup(demo, capsys)

    assert code == 1
    assert subject in out
    _not_ready(out)


_REAL_RUN = subprocess.run


def _doctor_only(fake):
    """Replaces the doctor run alone; `git init` still runs for real."""
    def run(argv, *args, **kwargs):
        if "doctor" not in argv:
            return _REAL_RUN(argv, *args, **kwargs)
        return fake(argv, kwargs)
    return run


def _doctor_exits_2_silently(argv, kwargs):
    return subprocess.CompletedProcess(argv, 2, stdout="", stderr="error: engmem doctor: boom\n")


def _doctor_hangs(argv, kwargs):
    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))


def _doctor_cannot_start(argv, kwargs):
    raise PermissionError(13, "Permission denied")


@pytest.mark.parametrize(
    ("run", "said"),
    [
        pytest.param(_doctor_exits_2_silently, "exited 2: error: engmem doctor: boom", id="exit-2"),
        pytest.param(_doctor_hangs, "did not finish", id="timeout"),
        pytest.param(_doctor_cannot_start, "PermissionError", id="cannot-start"),
    ],
)
def test_a_doctor_that_gives_no_report_is_not_taken_as_a_pass(
    real_store, tmp_path, capsys, monkeypatch, run, said
):
    monkeypatch.setattr("engmem.walkthrough.subprocess.run", _doctor_only(run))

    code, out = _setup(tmp_path / "demo", capsys)

    assert code == 1
    assert said in out
    _not_ready(out)


# --- AC-17.4: re-running duplicates nothing and keeps everyone else's configuration ----------


def test_rerunning_keeps_foreign_entries_and_the_agents_work_and_adds_no_second_wiring(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    config, agents = demo / "codex-home" / "config.toml", demo / "codex-home" / "AGENTS.md"
    config.write_bytes(config.read_bytes() + b"\n" + FOREIGN_SERVER.encode())
    agents.write_bytes(agents.read_bytes() + FOREIGN_RULE.encode())
    _add_retry(demo)
    _save_fixture(demo)
    before = _tree(demo)

    code, out = _setup(demo, capsys)

    assert code == 0, out
    assert _tree(demo) == before
    assert config.read_text(encoding="utf-8").count(CODEX_BEGIN) == 1
    assert agents.read_text(encoding="utf-8").count(TRIGGER_RULE) == 1


# --- AC-17.2: --verify reports PASS only with the record, its recall and the check -----------


def _complete_cycle(demo: Path, capsys) -> None:
    _setup(demo, capsys)
    _save_fixture(demo)
    assert main([
        "search", "retry order request_id", "--session", RECALL_SESSION,
        "--store", str(demo / "store"),
    ]) == 0
    _add_retry(demo)
    capsys.readouterr()


def test_verify_passes_once_the_record_its_recall_and_the_check_all_hold(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"
    _complete_cycle(demo, capsys)

    code, out = _verify(demo, capsys)

    assert code == 0, out
    assert f"ok: record: {FIXTURE_ID}" in out
    assert "demo_orders/orders.py, OrderBook.place_order" in out
    assert f"ok: recall: session {RECALL_SESSION} was shown {FIXTURE_ID}" in out
    assert "ok: acceptance check:" in out
    assert out.rstrip().splitlines()[-1].startswith("PASS")
    assert "Codex transcript" in out
    assert "code the agent edited" in out and "differs" not in out


def test_verify_passes_when_any_saved_record_was_found_by_another_session(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"
    _complete_cycle(demo, capsys)
    earlier = "20261009-add-order-retry"
    _write(
        demo / "store" / "sessions" / f"{earlier}.md",
        _record(earlier, "place_order_with_retry reuses the request_id of the first attempt."),
    )

    code, out = _verify(demo, capsys)

    assert code == 0, out
    assert earlier in out and FIXTURE_ID in out
    assert out.rstrip().splitlines()[-1].startswith(f"PASS: {FIXTURE_ID}")


def test_a_check_the_agent_rewrote_does_not_stand_in_for_the_shipped_one(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    _save_fixture(demo)
    _telemetry(demo, {"session_id": RECALL_SESSION, "surfaced": [FIXTURE_ID]})
    check = demo / "workspace" / "demo_orders" / "check.py"
    check.write_text('print("acceptance check: PASS")\n', encoding="utf-8")

    code, out = _verify(demo, capsys)

    assert code == 1
    assert "missing: acceptance check:" in out and "place_order_with_retry" in out
    assert "differs from the check engmem ships" in out
    assert out.rstrip().splitlines()[-1].startswith("NOT COMPLETE")


@pytest.mark.parametrize(
    ("damage", "missing"),
    [
        pytest.param(lambda d: (d / "store" / "sessions" / f"{FIXTURE_ID}.md").unlink(),
                     "missing: record:", id="never-saved"),
        pytest.param(lambda d: _save_fixture(d, status="draft"), "missing: record:", id="draft"),
        pytest.param(lambda d: _save_fixture(d, decision="OrderBook keeps orders in a list."),
                     "missing: record:", id="another-decision"),
        pytest.param(lambda d: _save_fixture(d, source="not stated in the available material."),
                     "missing: record:", id="no-source"),
        pytest.param(lambda d: _save_fixture(d, reason="not stated in the available material."),
                     "missing: record:", id="no-reason"),
        pytest.param(lambda d: (d / "store" / "telemetry.jsonl").unlink(),
                     "missing: recall:", id="never-searched"),
        pytest.param(lambda d: _add_retry(d, NOT_IDEMPOTENT),
                     "missing: acceptance check:", id="two-orders"),
    ],
)
def test_verify_names_what_is_missing_and_how_to_recover_and_never_passes(
    real_store, tmp_path, capsys, damage, missing
):
    demo = tmp_path / "demo"
    _complete_cycle(demo, capsys)
    damage(demo)

    code, out = _verify(demo, capsys)

    assert code == 1
    [line] = [line for line in out.splitlines() if line.startswith(missing)]
    assert " — " in line
    assert "PASS" not in out
    assert out.rstrip().splitlines()[-1].startswith("NOT COMPLETE")


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([{"session_id": None, "surfaced": [FIXTURE_ID]}], id="unattributed"),
        pytest.param([{"session_id": FIXTURE_ID, "surfaced": [FIXTURE_ID]}], id="its-own-session"),
        pytest.param([{"session_id": RECALL_SESSION, "surfaced": [FIXTURE_ID], "weak_only": True}],
                     id="weak-candidate-only"),
    ],
)
def test_a_search_that_is_not_a_later_sessions_find_is_not_recall(
    real_store, tmp_path, capsys, rows
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    _save_fixture(demo)
    _add_retry(demo)
    _telemetry(demo, *rows)

    code, out = _verify(demo, capsys)

    assert code == 1
    assert "missing: recall:" in out


def test_verify_refuses_a_directory_the_walkthrough_did_not_build(home, tmp_path, capsys):
    (tmp_path / "demo").mkdir()

    code, out = _verify(tmp_path / "demo", capsys)

    assert code == 2
    assert "engmem walkthrough codex --dir" in out


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        pytest.param(r"C:\demo\store", r"'C:\demo\store'", id="no-spaces"),
        pytest.param(r"C:\it's $HOME", r"'C:\it''s $HOME'", id="quote-and-dollar"),
    ],
)
def test_on_windows_the_env_line_is_powershell_with_literal_values(monkeypatch, value, literal):
    from engmem.walkthrough import _env_command

    monkeypatch.setattr("engmem.walkthrough.sys.platform", "win32")

    lines = _env_command({"CODEX_HOME": value, "ENGMEM_HOME": value}, "codex")

    assert lines[0] == f"$env:CODEX_HOME = {literal}; $env:ENGMEM_HOME = {literal}; codex"
    assert "PowerShell" in lines[1] and "stay set" in lines[1]
    assert lines[2].strip() == f'cmd.exe: set "CODEX_HOME={value}" && set "ENGMEM_HOME={value}" && codex'


def test_an_engmem_directory_in_the_demo_does_not_stand_in_for_the_package(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    _write(demo / "engmem" / "__init__.py", "")
    _write(demo / "engmem" / "cli.py", 'raise SystemExit("shadowed")\n')

    code, out = _setup(demo, capsys)

    assert code == 0, out
    assert "shadowed" not in out


# --- the example's acceptance check ----------------------------------------------------------


@pytest.mark.parametrize(
    ("added", "passes", "said"),
    [
        pytest.param("", False, "place_order_with_retry", id="before-session-2"),
        pytest.param(GOOD_RETRY, True, "1 order", id="retry-reuses-the-request-id"),
        pytest.param(FRESH_ID_RETRY, False, "2 orders", id="retry-with-a-new-request-id"),
        pytest.param(GOOD_RETRY + NOT_IDEMPOTENT, False, "2 orders", id="place-order-not-idempotent"),
    ],
)
def test_the_acceptance_check_counts_orders_after_two_identical_requests(
    real_store, tmp_path, capsys, added, passes, said
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    _add_retry(demo, added)

    result = _check(demo / "workspace")

    assert (result.returncode == 0) is passes, result.stdout + result.stderr
    assert said in result.stdout
