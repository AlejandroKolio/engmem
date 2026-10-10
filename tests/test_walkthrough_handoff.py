"""US-18: `engmem walkthrough handoff` wires Codex and Claude Code to one demo store, and `--verify`
says PASS only when a record saved through each client was found through the other. The real
client runs are owner acceptance; these tests cover what engmem controls."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import TRIGGER_RULE, redirect_home

from engmem.cli import main

CODEX_ID = "20261010-order-request-id"
CLAUDE_ID = "20261011-storefront-retry"
FOLLOW_UP_ID = "20261012-orders-key"
FOREIGN_SERVER = '[mcp_servers.other]\ncommand = "other-server"\n'


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
    """The engineer's own Codex and Claude Code wiring, each with someone else's entries."""
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(FOREIGN_SERVER, encoding="utf-8")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"env": {"OTHER": "1"}}\n', encoding="utf-8")
    store = tmp_path / "real-store"
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"engmem": {"command": "python", "args": ["--store", str(store)]}}}),
        encoding="utf-8",
    )
    assert main(["install", "--agent", "codex", "--store", str(store)]) == 0
    assert main(["install", "--agent", "claude", "--store", str(store)]) == 0
    assert main(["store", "set", str(store)]) == 0
    _write(store / "sessions" / "20260101-real.md", _record("20260101-real", "a real decision", ["real"]))
    capsys.readouterr()
    return store


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _head(workspace: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commits(workspace: Path) -> int:
    return int(subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=workspace, capture_output=True, text=True,
        check=True,
    ).stdout)


def _record(
    doc_id: str,
    decision: str,
    repos: list[str],
    *,
    anchors: dict[str, str] | None = None,
    status: str = "active",
    source: str = "demo_orders/orders.py, OrderBook.place_order",
    primer: str = "OrderBook.place_order returns the existing order for a repeated request_id.\n",
) -> str:
    verified = "{" + ", ".join(f"{k}: {v}" for k, v in (anchors or {}).items()) + "}" if anchors else ""
    return (
        f"---\nid: {doc_id}\ntitle: {decision[:40]}\ndate: 2026-10-10\n"
        f"task_date: 2026-10-10\nstatus: {status}\nsuperseded_by:\nbackfilled: false\n"
        "tags: [orders]\nentities: [OrderBook]\nrelated: []\ncovers_files: []\n"
        f"verified_at: {verified}\ncapture_minutes:\nrepos: [{', '.join(repos)}]\nmode: daily\n---\n\n"
        "## Decision Log\n\n"
        f"- Decision: {decision}\n- Reason: a timed-out client retries, and a second order would charge twice.\n"
        "- Rejected alternative: a new request_id per attempt, which places a new order each time.\n"
        f"- Source: {source}\n\n"
        f"## Future LLM Context (cold-start primer)\n\n{primer}\n"
        "## Reuse Log\n\nPrior docs used: none.\n\n## Search Trace\n\nshell\n"
    )


def _setup(demo: Path, capsys, walkthrough: str = "handoff") -> tuple[int, str]:
    code = main(["walkthrough", walkthrough, "--dir", str(demo)])
    return code, capsys.readouterr().out


def _verify(demo: Path, capsys) -> tuple[int, str]:
    code = main(["walkthrough", "handoff", "--verify", "--dir", str(demo)])
    return code, capsys.readouterr().out


def _save(demo: Path, doc_id: str, decision: str, repos: list[str], **kwargs) -> None:
    _write(demo / "store" / "sessions" / f"{doc_id}.md", _record(doc_id, decision, repos, **kwargs))


def _search(demo: Path, query: str, session: str) -> None:
    assert main(["search", query, "--session", session, "--store", str(demo / "store")]) == 0


def _telemetry(demo: Path, *rows: dict) -> None:
    with open(demo / "store" / "telemetry.jsonl", "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({"ts": "2026-10-11T09:00:00+00:00", "query": "retry", **row}) + "\n")


def _codex_record(demo: Path, **kwargs) -> None:
    kwargs.setdefault("anchors", {"orders": _head(demo / "orders")})
    _save(
        demo, CODEX_ID,
        "repeating an operation with the same request_id does not create a second order.",
        kwargs.pop("repos", ["orders"]), **kwargs,
    )


def _claude_record(demo: Path, **kwargs) -> None:
    _save(
        demo, CLAUDE_ID, "submit_order sends every attempt with the same request_id.",
        kwargs.pop("repos", ["storefront"]), source="demo_storefront/submit.py, submit_order",
        **kwargs,
    )


def _follow_up_record(demo: Path, **kwargs) -> None:
    _save(
        demo, FOLLOW_UP_ID, "OrderBook.place_order keeps one key per order.",
        kwargs.pop("repos", ["orders"]), **kwargs,
    )


def _complete_handoff(demo: Path, capsys) -> None:
    """What the three sessions leave in the store: each saved record, and each search."""
    code, out = _setup(demo, capsys)
    assert code == 0, out
    _codex_record(demo)
    _claude_record(demo)
    _search(demo, "retry order request_id", CLAUDE_ID)
    _follow_up_record(demo)
    _search(demo, CLAUDE_ID, FOLLOW_UP_ID)
    capsys.readouterr()


# --- set-up: one store, two clients, nothing of the engineer's touched ------------------------


def test_setup_wires_both_clients_to_one_demo_store_and_prints_every_step(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"

    code, out = _setup(demo, capsys)

    assert code == 0, out
    store = demo / "store"
    assert (store / "sessions").is_dir()
    config = tomllib.loads((demo / "codex-home" / "config.toml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["engmem"]["args"][-2:] == ["--store", str(store)]
    assert config["shell_environment_policy"]["set"] == {"ENGMEM_HOME": str(store)}
    assert TRIGGER_RULE in (demo / "codex-home" / "AGENTS.md").read_text(encoding="utf-8")
    mcp = json.loads((demo / "storefront" / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["engmem"]["args"][-2:] == ["--store", str(store)]
    settings = json.loads((demo / "storefront" / ".claude" / "settings.json").read_text("utf-8"))
    assert settings == {"env": {"ENGMEM_HOME": str(store)}}
    assert (demo / "orders" / "demo_orders" / "orders.py").is_file()
    assert (demo / "storefront" / "demo_storefront" / "submit.py").is_file()
    assert _commits(demo / "orders") == 1 and _commits(demo / "storefront") == 1
    assert f"demo store: {store}" in out
    for step in (
        "engmem doctor --agent codex --store", "engmem doctor --agent claude --store",
        "$engmem record why", "$engmem-save-quick", "/engmem add retrying the submission",
        "/engmem.save.quick", "claude --strict-mcp-config --mcp-config",
        str(demo / "storefront" / ".mcp.json"), "$engmem check OrderBook.place_order",
        "engmem walkthrough handoff --verify",
    ):
        assert step in out, step
    assert "error:" not in out


def test_setup_and_verify_leave_the_real_store_and_both_clients_settings_byte_identical(
    real_store, home, tmp_path, capsys
):
    before = {"store": _tree(real_store), "home": _tree(home)}
    demo = tmp_path / "demo"

    _setup(demo, capsys)
    _verify(demo, capsys)

    assert {"store": _tree(real_store), "home": _tree(home)} == before
    assert main(["store", "show"]) == 0
    assert f"store: {real_store}" in capsys.readouterr().out


def test_rerunning_writes_nothing_new_and_commits_nothing_twice(real_store, tmp_path, capsys):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    submit = demo / "storefront" / "demo_storefront" / "submit.py"
    submit.write_text("# the agent's retry\n", encoding="utf-8")
    before = _tree(demo)

    code, out = _setup(demo, capsys)

    assert code == 0, out
    assert _tree(demo) == before
    assert _commits(demo / "orders") == 1 and _commits(demo / "storefront") == 1
    assert "kept as it is" in out


def test_the_demo_commit_runs_no_hook_of_the_engineers(real_store, tmp_path, capsys, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    ran = tmp_path / "hook-ran"
    for name in ("pre-commit", "post-commit"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho ran > {shlex.quote(str(ran))}\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        f"[core]\n\thooksPath = {hooks.as_posix()}\n[commit]\n\tgpgsign = true\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))

    code, out = _setup(tmp_path / "demo", capsys)

    assert code == 0, out
    assert not ran.exists()
    assert _commits(tmp_path / "demo" / "orders") == 1


@pytest.mark.parametrize(("first", "second"), [("codex", "handoff"), ("handoff", "codex")])
def test_a_directory_another_walkthrough_built_is_refused(real_store, tmp_path, capsys, first, second):
    demo = tmp_path / "demo"
    _setup(demo, capsys, first)
    before = _tree(demo)

    code, out = _setup(demo, capsys, second)

    assert code == 2
    assert f"engmem walkthrough {first}" in out and "another walkthrough" in out
    assert _tree(demo) == before


@pytest.mark.parametrize(
    "child", ["orders", "storefront", "storefront/.claude", "storefront/demo_storefront"]
)
def test_a_demo_directory_that_links_out_of_the_demo_is_refused(real_store, tmp_path, capsys, child):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = demo / child
    # moved aside, not deleted: Git for Windows makes a commit's objects read-only
    target.rename(tmp_path / "aside")
    try:
        target.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is not permitted on this platform")

    code, out = _setup(demo, capsys)

    assert code == 2
    assert "symbolic link" in out and str(target) in out
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize(
    ("name", "real"), [(".mcp.json", ".claude.json"), (".claude/settings.json", ".claude/settings.json")]
)
def test_a_claude_project_file_linked_to_the_real_one_is_refused_untouched(
    real_store, home, tmp_path, capsys, name, real
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    link = demo / "storefront" / name
    link.unlink()
    before = (home / real).read_bytes()
    try:
        link.symlink_to(home / real)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is not permitted on this platform")

    code, out = _setup(demo, capsys)

    assert code == 2
    assert "symbolic link" in out and str(link) in out
    assert (home / real).read_bytes() == before


def test_claude_code_not_connected_stops_before_any_client_step(home, tmp_path, shell_path, capsys):
    assert main(["install", "--agent", "codex", "--store", str(tmp_path / "real")]) == 0
    capsys.readouterr()

    code, out = _setup(tmp_path / "demo", capsys)

    assert code == 1
    assert "step 0 — check the Claude Code wiring:" in out
    assert "error: template:" in out and "engmem install --agent claude" in out
    assert "not ready" in out and "Neither client has been started" in out
    assert "ready: the demo wiring" not in out and "step 1" not in out and "PASS" not in out


# --- AC-18.1, AC-18.2: each client's record reaches the other, through one store -------------


def test_verify_passes_once_each_clients_record_was_found_through_the_other(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)

    code, out = _verify(demo, capsys)

    assert code == 0, out
    assert "ok: same store:" in out
    assert (
        f"ok: codex to claude: session {CLAUDE_ID} (Claude Code, storefront) was shown {CODEX_ID}"
    ) in out
    assert "demo_orders/orders.py, OrderBook.place_order" in out
    assert (
        f"ok: claude to codex: session {FOLLOW_UP_ID} (Codex, orders) searched for {CLAUDE_ID}"
    ) in out
    assert out.rstrip().splitlines()[-1].startswith("PASS")


def _wrong_client(demo: Path) -> None:
    _codex_record(demo, repos=["storefront"], anchors={"storefront": _head(demo / "storefront")})


def _unattributed_finder(demo: Path) -> None:
    _claude_record(demo, repos=["storefront", "orders"])


def _finder_anchored_in_both(demo: Path) -> None:
    anchors = {"storefront": _head(demo / "storefront"), "orders": _head(demo / "orders")}
    _claude_record(demo, repos=["storefront", "orders"], anchors=anchors)


@pytest.mark.parametrize(
    ("damage", "missing", "said"),
    [
        pytest.param(lambda d: (d / "store" / "sessions" / f"{CODEX_ID}.md").unlink(),
                     "codex to claude", "repeat step 1", id="codex-never-saved"),
        pytest.param(lambda d: _codex_record(d, status="draft"),
                     "codex to claude", "still a draft", id="codex-record-a-draft"),
        pytest.param(lambda d: _codex_record(d, primer=""),
                     "codex to claude", "no cold-start primer", id="codex-record-without-primer"),
        pytest.param(_wrong_client, "codex to claude", "repeat step 1",
                     id="request-id-record-saved-in-storefront"),
        pytest.param(_unattributed_finder, "codex to claude", "which client ran them is unknown",
                     id="finder-links-both-workspaces"),
        pytest.param(_finder_anchored_in_both, "codex to claude", "which client ran them is unknown",
                     id="finder-anchored-in-both-workspaces"),
        pytest.param(lambda d: (d / "store" / "sessions" / f"{CLAUDE_ID}.md").unlink(),
                     "claude to codex", "repeat step 2", id="claude-never-saved"),
        pytest.param(_finder_anchored_in_both, "claude to codex",
                     f"{CLAUDE_ID} links both orders and storefront, and its anchors do not tell",
                     id="claude-record-anchored-in-both-workspaces"),
        pytest.param(lambda d: _follow_up_record(d, repos=["storefront"]),
                     "claude to codex", "repeat step 3", id="finder-saved-in-storefront"),
        pytest.param(lambda d: (d / "store" / "sessions" / f"{FOLLOW_UP_ID}.md").unlink(),
                     "claude to codex", "which client ran them is unknown",
                     id="codex-finder-never-saved"),
    ],
)
def test_verify_names_what_is_missing_and_never_passes(
    real_store, tmp_path, capsys, damage, missing, said
):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    damage(demo)

    code, out = _verify(demo, capsys)

    assert code == 1
    [line] = [line for line in out.splitlines() if line.startswith(f"missing: {missing}:")]
    assert said in line, line
    assert "PASS" not in out
    assert out.rstrip().splitlines()[-1].startswith("NOT COMPLETE")


def test_a_record_linking_both_workspaces_belongs_to_the_one_it_was_anchored_in(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    _claude_record(
        demo, repos=["storefront", "orders"], anchors={"storefront": _head(demo / "storefront")}
    )

    code, out = _verify(demo, capsys)

    assert code == 0, out


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([{"session_id": CLAUDE_ID, "surfaced": [CODEX_ID], "weak_only": True}],
                     id="weak-candidate-only"),
        pytest.param([{"session_id": None, "surfaced": [CODEX_ID]}], id="unattributed"),
        pytest.param([{"session_id": FOLLOW_UP_ID, "surfaced": [CODEX_ID]}], id="a-codex-session"),
    ],
)
def test_a_search_that_is_not_claude_codes_find_does_not_hand_over(
    real_store, tmp_path, capsys, rows
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    _codex_record(demo)
    _claude_record(demo)
    _follow_up_record(demo)
    _telemetry(demo, *rows)

    code, out = _verify(demo, capsys)

    assert code == 1
    assert "missing: codex to claude:" in out


def test_codex_finding_the_claude_record_without_its_id_is_not_a_search_by_id(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    (demo / "store" / "telemetry.jsonl").unlink()
    _telemetry(
        demo,
        {"session_id": CLAUDE_ID, "surfaced": [CODEX_ID]},
        {"session_id": FOLLOW_UP_ID, "query": "storefront retry", "surfaced": [CLAUDE_ID]},
    )

    code, out = _verify(demo, capsys)

    assert code == 1
    assert "ok: codex to claude:" in out
    assert f"missing: claude to codex: no search naming {CLAUDE_ID}" in out


@pytest.mark.parametrize(
    ("extra_id", "query", "shown"),
    [
        pytest.param(f"{CLAUDE_ID}-old", CLAUDE_ID, f"{CLAUDE_ID}-old",
                     id="query-names-one-record-search-shows-another"),
        pytest.param("20261011-s", "20261011-storefront", "20261011-s",
                     id="query-names-a-longer-id-that-starts-with-it"),
        pytest.param("storefront-retry", CLAUDE_ID, "storefront-retry",
                     id="query-names-a-longer-id-that-ends-with-it"),
    ],
)
def test_a_search_counts_by_id_only_for_the_record_its_query_names_whole(
    real_store, tmp_path, capsys, extra_id, query, shown
):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    _save(demo, extra_id, "submit_order waits between attempts.", ["storefront"],
          source="demo_storefront/submit.py, submit_order")
    (demo / "store" / "telemetry.jsonl").unlink()
    _telemetry(
        demo,
        {"session_id": CLAUDE_ID, "surfaced": [CODEX_ID]},
        {"session_id": FOLLOW_UP_ID, "query": query, "surfaced": [shown]},
    )

    code, out = _verify(demo, capsys)

    assert code == 1
    assert "missing: claude to codex:" in out
    assert out.rstrip().splitlines()[-1].startswith("NOT COMPLETE")


def test_a_search_naming_the_id_amid_other_words_counts(real_store, tmp_path, capsys):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    (demo / "store" / "telemetry.jsonl").unlink()
    _telemetry(
        demo,
        {"session_id": CLAUDE_ID, "surfaced": [CODEX_ID]},
        {"session_id": FOLLOW_UP_ID, "query": f"check {CLAUDE_ID.upper()}, orders",
         "surfaced": [CLAUDE_ID]},
    )

    code, out = _verify(demo, capsys)

    assert code == 0, out


def test_weakness_is_per_search_and_the_docs_say_so(real_store, tmp_path, capsys):
    """A row records only whether every result was weak (no new telemetry field, §9), so a search
    with one strong hit counts every record it showed; the docs must not claim more."""
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    (demo / "store" / "telemetry.jsonl").unlink()
    _telemetry(
        demo,
        {"session_id": CLAUDE_ID, "surfaced": ["20260101-other", CODEX_ID]},
        {"session_id": FOLLOW_UP_ID, "query": CLAUDE_ID, "surfaced": [CLAUDE_ID]},
    )

    code, out = _verify(demo, capsys)

    assert code == 0, out
    [line] = [line for line in out.splitlines() if line.startswith("ok: codex to claude:")]
    assert "did not show only weak candidates" in line
    root = Path(__file__).resolve().parent.parent
    for doc in ("docs/walkthrough-handoff.md", "ENGMEM-SPEC.md", "docs/design/contracts/install.md"):
        text = " ".join((root / doc).read_text(encoding="utf-8").split())
        assert "per search, not per result" in text, doc


# --- AC-18.3: the source repository is not where the second agent works ----------------------


def test_the_record_keeps_its_source_and_anchor_and_says_the_current_code_was_not_checked(
    real_store, tmp_path, capsys
):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    anchor = _head(demo / "orders")[:7]

    _code, out = _verify(demo, capsys)

    [line] = [line for line in out.splitlines() if line.startswith("note: unavailable source:")]
    assert f"{CODEX_ID} keeps its Source (demo_orders/orders.py, OrderBook.place_order)" in line
    assert f"orders {anchor}: unknown, no checkout of it here" in line
    assert "storefront" in line and "not performed" in line


def test_a_claude_code_search_in_the_storefront_shows_the_codex_record_unchecked(
    real_store, tmp_path, capsys, monkeypatch
):
    demo = tmp_path / "demo"
    _setup(demo, capsys)
    _codex_record(demo)
    monkeypatch.chdir(demo / "storefront")

    assert main(["search", "retry order request_id", "--store", str(demo / "store")]) == 0

    out = capsys.readouterr().out
    assert CODEX_ID in out
    assert "OrderBook.place_order returns the existing order for a repeated request_id." in out
    assert f"orders {_head(demo / 'orders')[:7]}: unknown, no checkout of it here" in out


# --- AC-18.4: a client set to another store is a configuration mismatch -----------------------


def _point_elsewhere(demo: Path, which: str, elsewhere: Path) -> None:
    if which == "claude-mcp":
        path = demo / "storefront" / ".mcp.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["mcpServers"]["engmem"]["args"][-1] = str(elsewhere)
        path.write_text(json.dumps(data), encoding="utf-8")
    elif which == "claude-shell":
        path = demo / "storefront" / ".claude" / "settings.json"
        path.write_text(json.dumps({"env": {"ENGMEM_HOME": str(elsewhere)}}), encoding="utf-8")
    else:
        path = demo / "codex-home" / "config.toml"
        text = path.read_text(encoding="utf-8")
        quoted = json.dumps(str(demo / "store"))
        path.write_text(text.replace(f"--store\", {quoted}", f"--store\", {json.dumps(str(elsewhere))}"),
                        encoding="utf-8")
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        assert str(elsewhere) in config["mcp_servers"]["engmem"]["args"]


@pytest.mark.parametrize("which", ["claude-mcp", "claude-shell", "codex-mcp"])
def test_a_client_set_to_another_store_is_a_mismatch_naming_both_stores(
    real_store, tmp_path, capsys, which
):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    elsewhere = tmp_path / "other-store"
    _point_elsewhere(demo, which, elsewhere)

    code, out = _verify(demo, capsys)

    assert code == 1
    [line] = [line for line in out.splitlines() if line.startswith("mismatch: same store:")]
    assert str(elsewhere) in line and str(demo / "store") in line
    assert "missing:" not in out and "PASS" not in out
    assert "not checked: codex to claude, claude to codex" in out
    assert out.rstrip().splitlines()[-1].startswith("NOT COMPLETE: configuration mismatch")


@pytest.mark.parametrize(
    ("damage", "said"),
    [
        pytest.param(lambda d: (d / "storefront" / ".mcp.json").unlink(), "names no store",
                     id="mcp-config-deleted"),
        pytest.param(lambda d: (d / "storefront" / ".claude" / "settings.json").write_text("{"),
                     "not valid JSON", id="settings-broken"),
        pytest.param(lambda d: (d / "codex-home" / "config.toml").write_text("[x\n"),
                     "not valid TOML", id="codex-config-broken"),
    ],
)
def test_settings_that_name_no_store_are_a_configuration_problem_not_a_missing_record(
    real_store, tmp_path, capsys, damage, said
):
    demo = tmp_path / "demo"
    _complete_handoff(demo, capsys)
    damage(demo)

    code, out = _verify(demo, capsys)

    assert code == 1
    [line] = [line for line in out.splitlines() if line.startswith("mismatch: same store:")]
    assert "cannot tell the store" in line and said in line
    assert "missing:" not in out


def test_verify_refuses_a_directory_the_handoff_did_not_build(real_store, tmp_path, capsys):
    demo = tmp_path / "demo"
    _setup(demo, capsys, "codex")

    code, out = _verify(demo, capsys)

    assert code == 2
    assert "engmem walkthrough handoff --dir" in out
