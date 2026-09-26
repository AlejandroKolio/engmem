"""A structural guard read from the syntax tree, so it holds for error branches no test exercises.

It covers every engmem module `mcp_server` imports, directly or not: a stray `print` in `gate1`
corrupts the JSON-RPC stream exactly as one in `mcp_server` does."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "engmem"
ENTRY = "mcp_server"


def _tree(module: str) -> ast.Module:
    return ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))


def _engmem_imports(tree: ast.Module) -> set[str]:
    """Module names under `engmem` this tree imports, wherever the import statement sits."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[1] for a in node.names if a.name.startswith("engmem.")}
        elif isinstance(node, ast.ImportFrom):
            # relative imports would need their own resolution; engmem has none, so one
            # appearing fails loudly here rather than dropping modules out of the guard
            assert node.level == 0, f"relative import at line {node.lineno} — resolve it here"
            if not node.module:
                continue
            if node.module == "engmem":
                found |= {a.name for a in node.names if (PACKAGE / f"{a.name}.py").is_file()}
            elif node.module.startswith("engmem."):
                found.add(node.module.split(".")[1])
    return found


def _closure(entry: str) -> list[str]:
    # importing any engmem module runs the package's __init__ first, imports and all
    seen, pending = set(), ["__init__", entry]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(_engmem_imports(_tree(module)) - seen)
    return sorted(seen)


CLOSURE = _closure(ENTRY)
TREES = {module: _tree(module) for module in CLOSURE}


def _print_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]


ALL_PRINTS = [(module, call) for module in CLOSURE for call in _print_calls(TREES[module])]


def test_the_closure_reaches_past_the_entry_module():
    """Guards the guard: a closure of `mcp_server` alone is the blind spot this file replaced."""
    assert {"gate1", "spine", "scoring", "telemetry"} <= set(CLOSURE), CLOSURE


def test_the_entry_module_contains_print_calls_at_all():
    """Guards the guard: refactoring the diagnostics away from `print` would make the assertions
    below pass vacuously."""
    assert _print_calls(TREES[ENTRY]), "expected diagnostics in mcp_server — has the guard gone stale?"


@pytest.mark.parametrize(
    "module, call", ALL_PRINTS, ids=[f"{m}-line-{c.lineno}" for m, c in ALL_PRINTS]
)
def test_every_print_goes_to_stderr(module, call):
    targets = [kw for kw in call.keywords if kw.arg == "file"]
    assert targets, (
        f"{module}.py:{call.lineno}: print() with no file= writes to stdout, "
        "where it corrupts the JSON-RPC frame in flight"
    )
    target = targets[0].value
    assert (
        isinstance(target, ast.Attribute)
        and target.attr == "stderr"
        and isinstance(target.value, ast.Name)
        and target.value.id == "sys"
    ), f"{module}.py:{call.lineno}: diagnostics must go to sys.stderr, nowhere else"


@pytest.mark.parametrize("module", CLOSURE)
def test_no_module_imports_the_dual_stream_failure_helper(module):
    """`runtime.fail` deliberately writes to stdout as well, because the CLI's consumer never
    reads stderr."""
    imported: list[str] = []
    for node in ast.walk(TREES[module]):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("engmem.runtime"):
            imported += [a.name for a in node.names]
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names if a.name.startswith("engmem.runtime")]

    assert "fail" not in imported, (
        f"{module} must not import runtime.fail — it writes to stdout, "
        "which on the MCP path is the protocol channel"
    )


@pytest.mark.parametrize("module", CLOSURE)
def test_no_bare_sys_stdout_writes_outside_the_transport(module):
    """A direct `sys.stdout.write` bypasses the transport's single writer and can interleave mid-
    frame."""
    offenders = [
        node.lineno
        for node in ast.walk(TREES[module])
        if isinstance(node, ast.Attribute)
        and node.attr in {"write", "flush"}
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "stdout"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "sys"
    ]
    assert not offenders, (
        f"{module}.py: direct sys.stdout access at line(s) {offenders} — "
        "write through the stream `serve()` was given instead"
    )
