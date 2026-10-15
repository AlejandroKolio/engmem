"""The in-process JSON-RPC harness shared by the MCP test modules: build a message, drive
`serve()` through string streams, and read the frames back."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from engmem.mcp_server import serve
from engmem.staging import version_of

PROTOCOL_VERSION = "2025-06-18"


def stdin_of(*lines: str) -> io.StringIO:
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return io.StringIO(text)


def stdin_of_messages(*messages: dict) -> io.StringIO:
    return stdin_of(*(json.dumps(m) for m in messages))


def run(store: Path, *messages: dict, read_only: bool = False) -> tuple[int, list[dict], str]:
    """`(exit_code, parsed_responses, raw_stdout)`; every non-empty stdout line must be valid
    JSON, checked here unconditionally."""
    stdin = stdin_of_messages(*messages)
    stdout = io.StringIO()
    exit_code = serve(store, stdin=stdin, stdout=stdout, read_only=read_only)
    raw = stdout.getvalue()
    responses = []
    for line in raw.splitlines():
        assert line.strip(), "blank stdout line — must never be written"
        responses.append(json.loads(line))  # raises loudly if a frame is not JSON
    return exit_code, responses, raw


def initialize_msg(msg_id=1, protocol_version=PROTOCOL_VERSION) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "initialize",
        "params": {"protocolVersion": protocol_version},
    }


def tools_call_msg(msg_id, *, name="engmem_search", arguments=None) -> dict:
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}


def role_tools_call_msg(msg_id, *, query, role, session_id=None) -> dict:
    arguments = {"query": query, "role": role}
    if session_id is not None:
        arguments["session_id"] = session_id
    return tools_call_msg(msg_id, name="engmem_search_by_role", arguments=arguments)


def prompts_get_msg(msg_id, *, name, arguments=None) -> dict:
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "prompts/get", "params": params}


def call(store: Path, *, name: str, arguments: dict) -> tuple[dict, bool]:
    """Runs a single tools/call and returns (result, is_error)."""
    _, responses, _ = run(store, tools_call_msg(1, name=name, arguments=arguments))
    result = responses[0]["result"]
    return result, bool(result.get("isError"))


def versioned(store: Path, arguments: dict) -> dict:
    """`arguments` with the `expected_version` a client that has just read the document passes
    (US-25); unchanged when there is no document to read."""
    path = store / "sessions" / f"{arguments.get('id')}.md"
    if not path.is_file():
        return arguments
    return {**arguments, "expected_version": version_of(path.read_bytes())}


def text(result: dict) -> str:
    return result["content"][0]["text"]


def result_text(responses: list[dict]) -> str:
    return text(responses[0]["result"])


def telemetry_lines(store: Path) -> list[dict]:
    path = store / "telemetry.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


ENGMEM_BIN = Path(sys.executable).parent / (
    "engmem.exe" if sys.platform == "win32" else "engmem"
)


class McpClient:
    """Records every line read from the child's stdout, so one teardown assertion covers the whole
    session, not just inspected calls."""

    def __init__(self, store: Path, *flags: str) -> None:
        assert ENGMEM_BIN.exists(), f"expected installed console script at {ENGMEM_BIN}"
        self.proc = subprocess.Popen(
            [str(ENGMEM_BIN), "mcp", "--store", str(store), *flags],
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
