"""FR-018 statically: the runtime guard in conftest patches this process only, and engmem's own
tests run the entry point in a child, where that patch does not reach."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "engmem"
MODULES = sorted(PACKAGE.glob("*.py"))

# `urllib.parse` is absent on purpose: backfill uses it to unquote link targets, which is string
# work. It is `urllib.request` that reaches the network.
NETWORK_MODULES = frozenset(
    {
        "socket", "ssl", "http", "urllib.request", "urllib.error", "ftplib", "smtplib",
        "telnetlib", "poplib", "imaplib", "xmlrpc", "requests", "httpx", "aiohttp", "urllib3",
        "webbrowser",
    }
)

# the one shell-out engmem makes; `clone`, `fetch`, `push` and `pull` are the network verbs
ALLOWED_ARGV = frozenset({("git", "init")})


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names |= {f"{node.module}.{alias.name}" for alias in node.names}
    return names


def _forbidden(names: set[str]) -> set[str]:
    return {
        name
        for name in names
        if any(name == blocked or name.startswith(f"{blocked}.") for blocked in NETWORK_MODULES)
    }


def _is_subprocess_call(func: ast.expr) -> bool:
    # `subprocess.run(...)` or a bare `run(...)` imported from it; anything else taking a list of
    # strings is ordinary code, and matching it would redden this test with no network in sight
    if isinstance(func, ast.Attribute):
        return isinstance(func.value, ast.Name) and func.value.id == "subprocess"
    return isinstance(func, ast.Name) and func.id in {"run", "Popen", "call", "check_output"}


def _literal_argv_lists(tree: ast.AST) -> list[tuple[str, ...]]:
    argvs = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args and _is_subprocess_call(node.func)):
            continue
        first = node.args[0]
        if isinstance(first, ast.List) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in first.elts
        ):
            argvs.append(tuple(e.value for e in first.elts))
    return argvs


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_module_imports_anything_that_reaches_the_network(module):
    found = _forbidden(_imported_names(ast.parse(module.read_text(encoding="utf-8"))))

    assert not found, (
        f"{module.name} imports {sorted(found)} — engmem makes zero network calls of any kind "
        "(spec FR-018), and a child process escapes the runtime guard in tests/conftest.py"
    )


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_module_shells_out_to_a_command_that_reaches_the_network(module):
    argvs = _literal_argv_lists(ast.parse(module.read_text(encoding="utf-8")))
    unexpected = [argv for argv in argvs if argv not in ALLOWED_ARGV]

    assert not unexpected, (
        f"{module.name} runs {unexpected}, which is not on the local-only allowlist — "
        "`git clone`, `git fetch` and `curl` all reach the network without importing a socket"
    )


def test_the_scan_sees_the_package_and_its_one_known_shell_out():
    """Both guards above pass trivially if the scan finds nothing; this fails when it does."""
    assert len(MODULES) > 5, MODULES

    argvs = [
        argv
        for module in MODULES
        for argv in _literal_argv_lists(ast.parse(module.read_text(encoding="utf-8")))
    ]

    assert ("git", "init") in argvs, "install.py's `git init` is no longer where the scan looks"


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: __import__("socket").socket(), id="socket"),
        pytest.param(lambda: __import__("socket").create_connection(("127.0.0.1", 9)), id="connect"),
        pytest.param(lambda: __import__("socket").getaddrinfo("example.com", 80), id="getaddrinfo"),
        pytest.param(lambda: __import__("socket").gethostbyname("example.com"), id="gethostbyname"),
        pytest.param(
            lambda: __import__("socket").gethostbyname_ex("example.com"), id="gethostbyname_ex"
        ),
    ],
)
def test_the_runtime_guard_blocks_every_way_out(call):
    """The guard itself, asserted: a silently unpatched entry point is a guard that reports nothing."""
    with pytest.raises(RuntimeError, match="Network access attempted"):
        call()
