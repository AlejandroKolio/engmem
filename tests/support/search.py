"""The search runners several modules drive the same way; plain functions, because the CLI/MCP
pair is passed as a parametrize value."""

from __future__ import annotations

from pathlib import Path


def run_search(store: Path, *args: str, capsys) -> tuple[int, str, str]:
    from engmem.cli import main

    exit_code = main(["search", *args, "--store", str(store)])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def cli_search(store: Path, query: str, capsys) -> str:
    from engmem.cli import main

    assert main(["search", query, "--store", str(store)]) == 0
    return capsys.readouterr().out


def mcp_search(store: Path, query: str, capsys) -> str:
    from support import rpc

    result, is_error = rpc.call(store, name="engmem_search", arguments={"query": query})
    assert not is_error
    return rpc.text(result)
