"""A minimal stdio JSON-RPC client, as an MCP host speaks to `engmem mcp`."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
CLOSE_TIMEOUT = 10
REQUEST_TIMEOUT = 60


class McpError(RuntimeError):
    """The server answered with a JSON-RPC error, closed its stdout, or did not answer in time."""


class McpSession:
    """One server process; every request is timed, and the result text is what the agent sees."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path, timeout: float = REQUEST_TIMEOUT) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
            bufsize=1,
        )
        self._next_id = 1
        self.timeout = timeout
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        """Reads stdout on its own thread, so a hung server times out instead of blocking; portable."""
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self.proc.stdout.close()
        self._lines.put("")

    def __enter__(self) -> "McpSession":
        self.request("initialize", {"protocolVersion": PROTOCOL_VERSION})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> tuple[dict, float]:
        """`(result, seconds)` of one request; a JSON-RPC error raises `McpError`."""
        msg_id = self._next_id
        self._next_id += 1
        message: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        started = time.perf_counter()
        self._send(message)
        try:
            line = self._lines.get(timeout=self.timeout)
        except queue.Empty:
            self.proc.kill()
            raise McpError(f"no answer to {method} within {self.timeout} s") from None
        seconds = time.perf_counter() - started
        if not line:
            raise McpError(f"server closed stdout during {method}")
        response = json.loads(line)
        if response.get("id") != msg_id:
            raise McpError(f"response id {response.get('id')!r} does not match request {msg_id}")
        if "error" in response:
            raise McpError(f"{method}: {response['error'].get('message')}")
        return response["result"], seconds

    def tool(self, name: str, arguments: dict) -> tuple[str, bool, float]:
        """`(text, is_error, seconds)` of one tools/call."""
        result, seconds = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "".join(part.get("text", "") for part in result.get("content", []))
        return text, bool(result.get("isError")), seconds

    def tools(self) -> tuple[list[dict], float]:
        """The tool definitions a host puts into the model's context, and the time to list them."""
        result, seconds = self.request("tools/list")
        return result["tools"], seconds

    def prompt(self, name: str, task: str | None) -> tuple[str, float]:
        """The prompt body a host injects, with the task passed as its one argument."""
        listing, _ = self.request("prompts/list")
        spec = next(p for p in listing["prompts"] if p["name"] == name)
        arguments = {}
        if task is not None and spec.get("arguments"):
            arguments[spec["arguments"][0]["name"]] = task
        result, seconds = self.request("prompts/get", {"name": name, "arguments": arguments})
        text = "".join(m["content"].get("text", "") for m in result["messages"])
        return text, seconds

    def close(self) -> None:
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=CLOSE_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
