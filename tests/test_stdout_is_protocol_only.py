"""A structural guard read from the syntax tree, so it holds for error branches no test exercises."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "src" / "engmem" / "mcp_server.py"
TREE = ast.parse(MODULE.read_text(encoding="utf-8"))


def _print_calls() -> list[ast.Call]:
    return [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]


def test_the_module_contains_print_calls_at_all():
    """Guards the guard: refactoring the diagnostics away from `print` would make the assertions
    below pass vacuously."""
    assert _print_calls(), "expected diagnostics in this module — has the guard gone stale?"


@pytest.mark.parametrize("call", _print_calls(), ids=lambda c: f"line-{c.lineno}")
def test_every_print_goes_to_stderr(call):
    targets = [kw for kw in call.keywords if kw.arg == "file"]
    assert targets, (
        f"{MODULE.name}:{call.lineno}: print() with no file= writes to stdout, "
        "where it corrupts the JSON-RPC frame in flight"
    )
    target = targets[0].value
    assert (
        isinstance(target, ast.Attribute)
        and target.attr == "stderr"
        and isinstance(target.value, ast.Name)
        and target.value.id == "sys"
    ), f"{MODULE.name}:{call.lineno}: diagnostics must go to sys.stderr, nowhere else"


def test_the_module_never_imports_the_dual_stream_failure_helper():
    """`runtime.fail` deliberately writes to stdout as well, because the CLI's consumer never
    reads stderr."""
    imported: list[str] = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("engmem.runtime"):
            imported += [a.name for a in node.names]
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names if a.name.startswith("engmem.runtime")]

    assert "fail" not in imported, (
        "mcp_server must not import runtime.fail — it writes to stdout, "
        "which on this path is the protocol channel"
    )


def test_no_bare_sys_stdout_writes_outside_the_transport():
    """A direct `sys.stdout.write` bypasses the transport's single writer and can interleave mid-
    frame."""
    offenders = [
        node.lineno
        for node in ast.walk(TREE)
        if isinstance(node, ast.Attribute)
        and node.attr in {"write", "flush"}
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "stdout"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "sys"
    ]
    assert not offenders, (
        f"{MODULE.name}: direct sys.stdout access at line(s) {offenders} — "
        "write through the stream `serve()` was given instead"
    )
