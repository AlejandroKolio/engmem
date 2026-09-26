"""Every text-mode open and text-mode subprocess in shipped code names its encoding, checked from
the syntax tree.

`test_encoding.py` catches a default-encoding open only on the paths its script runs; this reads
every call site, including the error branches and install paths nothing exercises there."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FILES = sorted((REPO / "src" / "engmem").rglob("*.py")) + sorted((REPO / "tools").glob("*.py"))
TEXT_METHODS = {"read_text", "write_text"}
SUBPROCESS_CALLS = {"run", "Popen", "check_output", "check_call", "call"}


def _mode(call: ast.Call) -> ast.expr | None:
    explicit = next((kw.value for kw in call.keywords if kw.arg == "mode"), None)
    if explicit is not None:
        return explicit
    # open(file, mode), io.open, codecs.open and os.fdopen(fd, mode) take the mode second;
    # Path.open(mode, ...) takes it first
    index = 1 if isinstance(call.func, ast.Name) or _is_module_call(call.func) else 0
    return call.args[index] if len(call.args) > index else None


def _is_binary(call: ast.Call) -> bool:
    # read_text/write_text and text-mode subprocess calls are text-only; their first argument
    # is data or a command, never a mode
    if isinstance(call.func, ast.Attribute) and call.func.attr in TEXT_METHODS:
        return False
    if _is_text_subprocess(call):
        return False
    mode = _mode(call)
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value


def _names_encoding(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in call.keywords)


def _is_os(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "os"


def _is_module_call(func: ast.expr) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in {"io", "codecs", "os"}
    )


def _is_text_subprocess(call: ast.Call) -> bool:
    # text=True decodes the child's output with the locale's code page unless encoding= is given
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in SUBPROCESS_CALLS
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
        and any(
            kw.arg in {"text", "universal_newlines"}
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in call.keywords
        )
    )


def _opens(tree: ast.Module) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "open" and not _is_os(func.value):
            # os.open returns a file descriptor: no text mode, nothing to decode
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr in TEXT_METHODS:
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "fdopen" and _is_os(func.value):
            calls.append(node)
        elif _is_text_subprocess(node):
            calls.append(node)
    return calls


SITES = [
    (path, call)
    for path in FILES
    for call in _opens(ast.parse(path.read_text(encoding="utf-8")))
    if not _is_binary(call)
]


def test_write_text_data_containing_b_is_still_a_text_call():
    """`write_text` takes data first, not a mode: a "b" in the data must not exempt the call."""
    [call] = _opens(ast.parse('p.write_text("abc")'))

    assert not _is_binary(call)
    assert not _names_encoding(call)


def test_a_text_mode_subprocess_is_scanned():
    [call] = _opens(ast.parse('subprocess.run(["git", "init"], text=True)'))

    assert not _is_binary(call)
    assert not _names_encoding(call)


def test_a_text_mode_subprocess_with_a_string_command_containing_b_is_scanned():
    """A string command sits where `_mode` looks for a mode: its "b" must not exempt the call."""
    source = 'subprocess.check_output("git rev-parse --abbrev-ref HEAD", shell=True, text=True)'
    [call] = _opens(ast.parse(source))

    assert not _is_binary(call)
    assert not _names_encoding(call)


def test_the_scan_finds_call_sites_at_all():
    """Guards the guard: a scan that matches nothing passes vacuously."""
    assert len(SITES) > 10, SITES


@pytest.mark.parametrize(
    "path, call", SITES, ids=[f"{p.relative_to(REPO)}:{c.lineno}" for p, c in SITES]
)
def test_every_text_open_names_its_encoding(path, call):
    assert _names_encoding(call), (
        f"{path.relative_to(REPO)}:{call.lineno}: text-mode open without encoding= reads and "
        "writes in the locale's code page, which is not UTF-8 on Windows or under LANG=C"
    )
