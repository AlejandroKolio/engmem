"""End-to-end evidence against a real `engmem mcp` subprocess, covering stdio buffering and the
installed console script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import (
    ACTIVE_CONTENT,
    DRAFT_CONTENT,
    requires_symlinks,
)

from engmem.spine import load_store

requires_broken_pipe_semantics = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows has no SIGPIPE/EPIPE write semantics to exercise here",
)

ENGMEM_BIN = Path(sys.executable).parent / (
    "engmem.exe" if sys.platform == "win32" else "engmem"
)


class _Client:
    """Records every line read from the child's stdout, so one teardown assertion covers the whole
    session, not just inspected calls."""

    def __init__(self, store: Path) -> None:
        assert ENGMEM_BIN.exists(), f"expected installed console script at {ENGMEM_BIN}"
        self.proc = subprocess.Popen(
            [str(ENGMEM_BIN), "mcp", "--store", str(store)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.all_stdout_lines: list[str] = []
        self._next_id = 1

    def call(self, method: str, params: dict | None = None) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            request["params"] = params
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()

        line = self.proc.stdout.readline()
        assert line, (
            "subprocess closed stdout with no response — stderr:\n"
            f"{self.proc.stderr.read() if self.proc.stderr else ''}"
        )
        self.all_stdout_lines.append(line)
        response = json.loads(line)  # raises loudly if a frame is not valid JSON
        assert response.get("id") == msg_id
        return response

    def tool_call(self, name: str, arguments: dict) -> dict:
        response = self.call(
            "tools/call", {"name": name, "arguments": arguments}
        )
        assert "result" in response, f"expected a tool result, got {response!r}"
        return response["result"]

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


@pytest.fixture
def client(tmp_path):
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    c = _Client(store)
    c.store = store  # type: ignore[attr-defined]
    yield c
    c.close()
    # The one invariant that kills a client session if broken anywhere in this
    # process's lifetime: every line this subprocess ever wrote to its real
    # stdout must be a complete, valid JSON-RPC frame — checked again here, over
    # the whole recorded transcript, not just the individual `call()` returns.
    for recorded_line in c.all_stdout_lines:
        json.loads(recorded_line)


def _result_text(result: dict) -> str:
    return result["content"][0]["text"]


def test_create_then_complete_then_search_over_a_real_subprocess(client):
    init = client.call("initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == "engmem"

    # --- create a draft, then read it back with load_store ---
    create_result = client.tool_call(
        "engmem_create_draft",
        {"id": "20260101-widget-cache", "content": DRAFT_CONTENT},
    )
    assert not create_result.get("isError"), _result_text(create_result)

    on_disk = client.store / "sessions" / "20260101-widget-cache.md"
    assert on_disk.read_text(encoding="utf-8") == DRAFT_CONTENT

    load_result = load_store(client.store / "sessions")
    assert load_result.errors == [], [p.message for p in load_result.errors]
    draft_doc = {d.id: d for d in load_result.docs}["20260101-widget-cache"]
    assert draft_doc.status == "draft"

    # --- complete the draft: status draft -> active, body sections present ---
    complete_result = client.tool_call(
        "engmem_complete_draft",
        {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
    )
    assert not complete_result.get("isError"), _result_text(complete_result)

    load_result = load_store(client.store / "sessions")
    assert load_result.errors == []
    active_doc = {d.id: d for d in load_result.docs}["20260101-widget-cache"]
    assert active_doc.status == "active"
    for heading in (
        "## Decision Log",
        "## Landmines",
        "## Cold-start primer",
        "## Reuse Log",
        "## Search Trace",
    ):
        assert heading in active_doc.body

    # --- findable by engmem_search in the same session ---
    search_result = client.tool_call("engmem_search", {"query": "WidgetCache CacheWarmer"})
    assert not search_result.get("isError"), _result_text(search_result)
    assert "20260101-widget-cache" in _result_text(search_result)


@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "/etc/passwd", "..", ".", ".hidden-doc", "widget\\cache-job"],
    ids=["dotdot-traversal", "absolute-path", "bare-dotdot", "bare-dot", "leading-dot", "backslash"],
)
def test_escape_vectors_are_refused_over_a_real_subprocess(client, bad_id):
    result = client.tool_call(
        "engmem_create_draft", {"id": bad_id, "content": DRAFT_CONTENT}
    )
    assert result.get("isError") is True
    assert "Traceback" not in _result_text(result)
    assert list((client.store / "sessions").iterdir()) == []


@requires_symlinks
def test_sessions_symlink_escape_is_refused_over_a_real_subprocess(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (store / "sessions").symlink_to(outside, target_is_directory=True)

    c = _Client(store)
    c.store = store  # type: ignore[attr-defined]
    try:
        c.call("initialize", {"protocolVersion": "2025-06-18"})
        result = c.tool_call(
            "engmem_create_draft",
            {"id": "20260101-widget-cache", "content": DRAFT_CONTENT},
        )
        assert result.get("isError") is True
        assert list(outside.iterdir()) == []
    finally:
        c.close()
        for recorded_line in c.all_stdout_lines:
            json.loads(recorded_line)


def test_mark_superseded_over_a_real_subprocess(client):
    client.call("initialize", {"protocolVersion": "2025-06-18"})
    client.tool_call(
        "engmem_create_draft", {"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )
    client.tool_call(
        "engmem_complete_draft", {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}
    )

    supersede_result = client.tool_call(
        "engmem_mark_superseded",
        {"id": "20260101-widget-cache", "superseded_by": "20260201-widget-cache-v2"},
    )
    assert not supersede_result.get("isError"), _result_text(supersede_result)

    load_result = load_store(client.store / "sessions")
    doc = {d.id: d for d in load_result.docs}["20260101-widget-cache"]
    assert doc.status == "superseded"
    assert doc.superseded_by == "20260201-widget-cache-v2"


# ---------------------------------------------------------------------------
# telemetry `channel` — a CLI search (`engmem search`, a plain subprocess) and
# an MCP search (this file's `_Client`, a real `engmem mcp` subprocess) against
# the SAME store must both land in `telemetry.jsonl`, distinguishable from each
# other by `channel`, so the two populations can never silently merge.
# ---------------------------------------------------------------------------


def test_cli_and_mcp_searches_against_the_same_store_are_distinguishable_by_channel(client):
    client.call("initialize", {"protocolVersion": "2025-06-18"})
    client.tool_call(
        "engmem_create_draft", {"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
    )
    client.tool_call(
        "engmem_complete_draft", {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}
    )

    mcp_result = client.tool_call("engmem_search", {"query": "WidgetCache CacheWarmer"})
    assert not mcp_result.get("isError"), _result_text(mcp_result)

    cli_run = subprocess.run(
        [str(ENGMEM_BIN), "search", "WidgetCache", "--store", str(client.store)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert "20260101-widget-cache" in cli_run.stdout

    rows = [
        json.loads(line)
        for line in (client.store / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    channels = [row["channel"] for row in rows]
    assert "mcp" in channels
    assert "cli" in channels
    assert channels.count("mcp") == 1
    assert channels.count("cli") == 1
    for row in rows:
        assert row["context_bytes"] > 0, "a hit must record nonzero context_bytes"
        assert row["context_tokens_estimate"] > 0


@requires_broken_pipe_semantics
def test_broken_pipe_on_the_real_process_stdout_exits_cleanly_without_a_traceback(tmp_path):
    """A client that closes the pipe must leave a clean exit, not a traceback — reachable only
    over a real process, never through an injected stream."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)

    proc = subprocess.Popen(
        [str(ENGMEM_BIN), "mcp", "--store", str(store)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    assert proc.stdout is not None and proc.stdin is not None
    proc.stdout.close()  # no reader left: the child's next write raises BrokenPipeError

    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
    proc.stdin.flush()
    proc.stdin.close()

    exit_code = proc.wait(timeout=5)
    stderr = proc.stderr.read() if proc.stderr else ""

    assert exit_code == 0
    assert "Traceback" not in stderr
    assert "downstream pipe closed" in stderr


@requires_broken_pipe_semantics
def test_a_block_buffered_stdout_leaves_nothing_for_the_interpreter_shutdown_flush(tmp_path):
    """The frame that failed to send stays in stdout's buffer and CPython flushes it again on the
    way out, exiting 120 on the dead pipe. Only a block-buffered stdout reaches that path, so the
    environment must not carry `PYTHONUNBUFFERED` — with it set, the bug is invisible."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"}

    proc = subprocess.Popen(
        [str(ENGMEM_BIN), "mcp", "--store", str(store)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout is not None and proc.stdin is not None
    proc.stdout.close()

    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
    proc.stdin.flush()
    proc.stdin.close()

    exit_code = proc.wait(timeout=5)
    stderr = proc.stderr.read() if proc.stderr else ""

    assert exit_code == 0, f"exit {exit_code}, stderr: {stderr!r}"
    assert "Exception ignored" not in stderr, stderr
    assert "Traceback" not in stderr, stderr
    assert "downstream pipe closed" in stderr


def test_an_undecodable_stdin_byte_does_not_kill_the_session(tmp_path):
    """`sys.stdin`'s error handler is environment-dependent — `surrogateescape` under UTF-8 mode
    or a C locale, but `strict` under a plain `en_US.UTF-8`, where one bad byte from the client
    raised UnicodeDecodeError out of the read loop and took the whole session with it. Reachable
    only over a real process: an injected `StringIO` has already decoded."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)

    env = dict(
        os.environ,
        PYTHONIOENCODING="utf-8:strict",
        PYTHONUTF8="0",
        LANG="en_US.UTF-8",
        LC_ALL="en_US.UTF-8",
    )
    proc = subprocess.Popen(
        [str(ENGMEM_BIN), "mcp", "--store", str(store)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    bad_line = b"\xff\xfe not utf-8 at all\n"
    ping_line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"

    # communicate() owns the write and the close; closing stdin first raises
    # ValueError: flush of closed file below 3.13
    stdout, stderr = proc.communicate(input=bad_line + ping_line, timeout=10)

    assert proc.returncode == 0
    assert b"Traceback" not in stderr, stderr.decode(errors="replace")
    frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert len(frames) == 2, frames
    assert frames[0]["error"]["code"] == -32700
    assert frames[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_telemetry_write_failure_over_a_real_subprocess_leaves_search_and_protocol_intact(
    tmp_path,
):
    """A shadowed `telemetry.jsonl` must not crash the subprocess or corrupt its framing; the
    failure belongs in the result text."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    (store / "telemetry.jsonl").mkdir()

    c = _Client(store)
    c.store = store  # type: ignore[attr-defined]
    try:
        c.call("initialize", {"protocolVersion": "2025-06-18"})
        c.tool_call(
            "engmem_create_draft", {"id": "20260101-widget-cache", "content": DRAFT_CONTENT}
        )
        c.tool_call(
            "engmem_complete_draft", {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT}
        )

        result = c.tool_call("engmem_search", {"query": "WidgetCache CacheWarmer"})
        text = _result_text(result)

        assert not result.get("isError"), text
        assert "20260101-widget-cache" in text, "the search result itself must stay complete"
        assert "note: telemetry not recorded" in text
        assert (store / "telemetry.jsonl").is_dir(), "the failed write must not have recreated it"
    finally:
        c.close()
        for recorded_line in c.all_stdout_lines:
            json.loads(recorded_line)  # every stdout line must still be a valid JSON-RPC frame
