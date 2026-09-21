"""The in-process JSON-RPC harness shared by the MCP test modules: build a message, drive
`serve()` through string streams, and read the frames back."""

from __future__ import annotations

import io
import json
from pathlib import Path

from engmem.mcp_server import serve

PROTOCOL_VERSION = "2025-06-18"


def _stdin_of(*lines: str) -> io.StringIO:
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return io.StringIO(text)


def _stdin_of_messages(*messages: dict) -> io.StringIO:
    return _stdin_of(*(json.dumps(m) for m in messages))


def _run(store: Path, *messages: dict) -> tuple[int, list[dict], str]:
    """`(exit_code, parsed_responses, raw_stdout)`; every non-empty stdout line must be valid
    JSON, checked here unconditionally."""
    stdin = _stdin_of_messages(*messages)
    stdout = io.StringIO()
    exit_code = serve(store, stdin=stdin, stdout=stdout)
    raw = stdout.getvalue()
    responses = []
    for line in raw.splitlines():
        assert line.strip(), "blank stdout line — must never be written"
        responses.append(json.loads(line))  # raises loudly if a frame is not JSON
    return exit_code, responses, raw


def _initialize_msg(msg_id=1, protocol_version=PROTOCOL_VERSION) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "initialize",
        "params": {"protocolVersion": protocol_version},
    }


def _tools_call_msg(msg_id, *, name="engmem_search", arguments=None) -> dict:
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}


def _role_tools_call_msg(msg_id, *, query, role, session_id=None) -> dict:
    arguments = {"query": query, "role": role}
    if session_id is not None:
        arguments["session_id"] = session_id
    return _tools_call_msg(msg_id, name="engmem_search_by_role", arguments=arguments)


def _prompts_get_msg(msg_id, *, name, arguments=None) -> dict:
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "prompts/get", "params": params}


def _call(store: Path, *, name: str, arguments: dict) -> tuple[dict, bool]:
    """Runs a single tools/call and returns (result, is_error)."""
    _, responses, _ = _run(store, _tools_call_msg(1, name=name, arguments=arguments))
    result = responses[0]["result"]
    return result, bool(result.get("isError"))


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _result_text(responses: list[dict]) -> str:
    return _text(responses[0]["result"])


def _telemetry_lines(store: Path) -> list[dict]:
    path = store / "telemetry.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
