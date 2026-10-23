"""The MCP clients engmem wires up: the server entry in Claude Desktop's JSON config and Codex's
TOML config, the verdict on whether an entry still launches this engmem with this store, and the
shell quoting of engmem's command lines (contracts/install.md)."""

from __future__ import annotations

import copy
import enum
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from engmem.runtime import fail, store_path_of
from engmem.staging import newline_of
from engmem.user_files import SetupError, ensure_directory, read_user_file, replace_user_file


# TOML has no writer in the standard library, so the Codex entry is a line block engmem owns
# between these two comments; uninstall removes exactly that span
CODEX_MCP_BEGIN = "# engmem-mcp-server: begin (managed by `engmem install --agent codex`)"


CODEX_MCP_END = "# engmem-mcp-server: end"


class CodexMcpOutcome(enum.StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    PRESENT = "present"
    FOREIGN = "foreign"


CODEX_MCP_NOTES = {
    CodexMcpOutcome.ADDED: "MCP server entry added",
    CodexMcpOutcome.UPDATED: "MCP server entry updated",
    CodexMcpOutcome.PRESENT: "MCP server entry already current",
    CodexMcpOutcome.FOREIGN: "an [mcp_servers.engmem] engmem did not write is left untouched",
}


def claude_desktop_config_path() -> Path:
    # Windows keeps config under %APPDATA%, not the POSIX Application Support tree
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json"
    )


def load_json_object(path: Path) -> dict:
    """`{}` for an absent file, the parsed object for a valid one, else `SetupError`."""
    if not path.is_file():
        return {}
    # the BOM is dropped, not carried: engmem re-serialises the whole document, and RFC 8259
    # forbids emitting one
    text, _ = read_user_file(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SetupError(
            f"{path} is not valid JSON: {exc} — fix or remove the file by hand and "
            f"re-run (refusing to overwrite a config engmem cannot parse)"
        ) from exc
    if not isinstance(data, dict):
        raise SetupError(
            f"{path} does not contain a JSON object at the top level — fix or remove "
            f"the file by hand and re-run (refusing to overwrite it)"
        )
    return data


def stdio_mcp_entry(store: Path) -> dict:
    # a bare "engmem" is often not on PATH under an MCP client's minimal launch
    # environment, and relative paths are a documented failure mode
    return {
        "command": sys.executable,
        "args": ["-m", "engmem.cli", "mcp", "--store", str(store)],
    }


def install_claude_desktop(store: Path) -> None:
    """Merges the `engmem` key into whatever is already on disk, never overwriting wholesale."""
    config_path = claude_desktop_config_path()
    config = load_json_object(config_path)
    mcp_servers = config.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        raise SetupError(
            f"{config_path} has a `mcpServers` key that is not a JSON object — "
            f"fix it by hand and re-run (refusing to overwrite it)"
        )
    mcp_servers["engmem"] = stdio_mcp_entry(store)
    config["mcpServers"] = mcp_servers
    ensure_directory(config_path.parent, "Claude Desktop config directory")
    replace_user_file(config_path, json.dumps(config, indent=2) + "\n")


def uninstall_claude_desktop() -> tuple[int, int]:
    """`(removed, failed)`; removes exactly `mcpServers.engmem` and nothing else."""
    config_path = claude_desktop_config_path()
    try:
        config = load_json_object(config_path)
        mcp_servers = config.get("mcpServers")
        if not isinstance(mcp_servers, dict) or "engmem" not in mcp_servers:
            return 0, 0
        del mcp_servers["engmem"]
        replace_user_file(config_path, json.dumps(config, indent=2) + "\n")
    except SetupError as exc:
        # duplicated to stdout: fail() reaches the stdout-only consumer too
        fail(str(exc))
        return 0, 1
    return 1, 0


def claude_desktop_entry_present() -> bool:
    try:
        config = load_json_object(claude_desktop_config_path())
    except SetupError:
        return False
    mcp_servers = config.get("mcpServers")
    return isinstance(mcp_servers, dict) and "engmem" in mcp_servers


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def codex_config_path() -> Path:
    return codex_home() / "config.toml"


def toml_string(value: str) -> str:
    # JSON's escapes are a subset of a TOML basic string's; ensure_ascii stays off because TOML
    # rejects the surrogate pairs json would write for an astral character. DEL is the one
    # character TOML forbids raw that json leaves alone
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise SetupError(f"{value!r} is not valid Unicode and cannot be written to a TOML file")
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


_CODEX_HEADER = "[mcp_servers.engmem]"
# `\n` only: `str.splitlines` also splits on U+0085, U+2028 and U+2029, which `toml_string`
# leaves raw in a store path
_LINES_RE = re.compile(r"[^\n]*\n|[^\n]+")


def _codex_entry_lines(store: Path) -> tuple[str, str, str]:
    """The three lines of engmem's block it owns: the header, `command` and `args`."""
    entry = stdio_mcp_entry(store)
    args = ", ".join(toml_string(arg) for arg in entry["args"])
    return _CODEX_HEADER, f"command = {toml_string(entry['command'])}", f"args = [{args}]"


def _codex_mcp_block(store: Path, newline: str) -> str:
    lines = (CODEX_MCP_BEGIN, *_codex_entry_lines(store), CODEX_MCP_END)
    return newline.join(lines) + newline


def _ending(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""


def _is_one_line(line: str, key: str, kind: type) -> bool:
    """`line` alone is exactly `key = <kind>`: a value continued on the next line is not."""
    try:
        parsed = tomllib.loads(line.rstrip("\r\n"))
    except tomllib.TOMLDecodeError:
        return False
    return list(parsed) == [key] and isinstance(parsed[key], kind)


def _refreshed_block(path: Path, block: str, store: Path) -> str:
    """`block` with its header, `command` and `args` lines regenerated and every later line kept
    byte for byte: Codex writes per-tool approvals below them (contracts/install.md)."""
    lines = _LINES_RE.findall(block)
    if (
        len(lines) < 5
        or lines[1].strip() != _CODEX_HEADER
        or not _is_one_line(lines[2], "command", str)
        or not _is_one_line(lines[3], "args", list)
    ):
        raise SetupError(
            f"{path}: engmem's block must start with {_CODEX_HEADER}, then `command` and "
            f"`args` on one line each — restore that order, or remove the block and re-run"
        )
    owned = "".join(new + _ending(old) for new, old in zip(_codex_entry_lines(store), lines[1:4]))
    return lines[0] + owned + "".join(lines[4:])


def parse_toml(path: Path, text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(
            f"{path} is not valid TOML: {exc} — fix the file by hand and re-run "
            f"(refusing to rewrite a config engmem cannot parse)"
        ) from exc


def engmem_mcp_server(config: dict) -> object:
    mcp_servers = config.get("mcp_servers")
    return mcp_servers.get("engmem") if isinstance(mcp_servers, dict) else None


def _split_codex_block(path: Path, text: str) -> tuple[str, str, str] | None:
    """`(before, block, after)` around engmem's managed block, or None when there is none."""
    lines = text.splitlines(keepends=True)
    begin = next((i for i, line in enumerate(lines) if line.strip() == CODEX_MCP_BEGIN), None)
    if begin is None:
        return None
    end = next(
        (i for i in range(begin + 1, len(lines)) if lines[i].strip() == CODEX_MCP_END), None
    )
    if end is None:
        raise SetupError(
            f"{path} has an engmem MCP block with no `{CODEX_MCP_END}` line — remove the "
            f"[mcp_servers.engmem] block by hand and re-run"
        )
    return "".join(lines[:begin]), "".join(lines[begin : end + 1]), "".join(lines[end + 1 :])


def install_codex_mcp(store: Path, path: Path | None = None) -> CodexMcpOutcome:
    """Adds or refreshes engmem's own block in `path` (default: $CODEX_HOME's config); an entry
    the user wrote is never rewritten."""
    path = path or codex_config_path()
    existing, bom = read_user_file(path) if path.is_file() else ("", b"")
    config = parse_toml(path, existing)
    if not isinstance(config.get("mcp_servers", {}), dict):
        raise SetupError(
            f"{path} has an `mcp_servers` key that is not a table — fix it by hand and re-run "
            f"(refusing to rewrite it)"
        )
    newline = newline_of(existing) or "\n"
    entry = stdio_mcp_entry(store)
    block = _codex_mcp_block(store, newline)
    split = _split_codex_block(path, existing)
    current = engmem_mcp_server(config)
    if split is None:
        if current is not None:
            return CodexMcpOutcome.FOREIGN
        prefix = existing if not existing or existing.endswith(("\n", "\r")) else existing + newline
        composed, outcome = prefix + block, CodexMcpOutcome.ADDED
    else:
        before, current_block, after = split
        # a block whose lines were commented out holds no entry, so nothing in it is kept
        refreshed = _refreshed_block(path, current_block, store) if current is not None else block
        composed = before + refreshed + after
        if composed == existing:
            return CodexMcpOutcome.PRESENT
        outcome = CodexMcpOutcome.UPDATED
    expected = copy.deepcopy(config)
    # only the two values engmem writes may change; whatever else the table holds is kept
    kept = current if isinstance(current, dict) else {}
    expected.setdefault("mcp_servers", {})["engmem"] = {**kept, **entry}
    if _parsed_or_none(composed) != expected:
        # an inline `mcp_servers = {}` cannot be extended by a later table header
        raise SetupError(
            f"adding [mcp_servers.engmem] would not leave {path} a valid config with that entry "
            f"— add an `engmem` server to mcp_servers by hand, with command = "
            f"{json.dumps(entry['command'])} and args = {json.dumps(entry['args'])}"
        )
    ensure_directory(path.parent, "Codex config directory")
    replace_user_file(path, composed, bom)
    return outcome


def _parsed_or_none(text: str) -> dict | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def uninstall_codex_mcp() -> tuple[int, int]:
    """`(removed, failed)`; removes exactly engmem's managed block and nothing else."""
    path = codex_config_path()
    if not path.is_file():
        return 0, 0
    try:
        existing, bom = read_user_file(path)
        # a config engmem has no block in is not engmem's to diagnose, broken or not
        split = _split_codex_block(path, existing)
        if split is None:
            return 0, 0
        config = parse_toml(path, existing)
        before, _, after = split
        _require_only_engmem_removed(path, config, before + after)
        replace_user_file(path, before + after, bom)
    except SetupError as exc:
        fail(str(exc))
        return 0, 1
    return 1, 0


def _require_only_engmem_removed(path: Path, config: dict, remaining: str) -> None:
    # keys below engmem's header belong to its table; without the header they fall into the
    # table above it, which parses fine and silently changes what that table means
    # a block whose lines were commented out holds no entry, and cutting it changes nothing
    expected = copy.deepcopy(config)
    servers = expected.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop("engmem", None)
    if _without_empty_servers(_parsed_or_none(remaining)) != _without_empty_servers(expected):
        raise SetupError(
            f"removing engmem's [mcp_servers.engmem] block would change the rest of {path} — "
            f"remove the block, and whatever you added under it, by hand"
        )


def _without_empty_servers(config: dict | None) -> dict | None:
    # `[mcp_servers.engmem]` creates `mcp_servers` implicitly, so its removal may take an
    # otherwise empty table with it
    if config is None or config.get("mcp_servers") != {}:
        return config
    return {key: value for key, value in config.items() if key != "mcp_servers"}


def codex_mcp_block_present() -> bool:
    path = codex_config_path()
    if not path.is_file():
        return False
    try:
        return _split_codex_block(path, read_user_file(path)[0]) is not None
    except SetupError:
        return False


@dataclass(frozen=True)
class McpWiring:
    """One installed MCP entry for engmem: the `--store` it launches the server with, and the
    command line that launches it (empty when the entry's values are not all strings)."""

    agent: str
    config: Path
    store: Path | None
    managed: bool
    command: str | None = None
    args: tuple[str, ...] = ()


class WiringVerdict(enum.StrEnum):
    MATCHES = "matches"
    MISMATCH = "mismatch"
    AT_LAUNCH = "resolved at launch"
    NO_FIXED_BASE = "no fixed base"
    INVALID = "invalid"


def wiring_verdict(wiring: McpWiring, store: Path) -> WiringVerdict:
    """How the entry's `--store` compares with `store`, the one this shell resolves."""
    if wiring.store is None:
        return WiringVerdict.AT_LAUNCH
    # a NUL byte, which a JSON or TOML string can carry and no path can; checked in the string
    # because Windows' non-strict realpath returns such a path unchanged instead of raising
    if "\0" in str(wiring.store):
        return WiringVerdict.INVALID
    if not wiring.store.is_absolute():
        return WiringVerdict.NO_FIXED_BASE
    if os.path.realpath(wiring.store) == os.path.realpath(store):
        return WiringVerdict.MATCHES
    return WiringVerdict.MISMATCH


def rewire_action(wiring: McpWiring, store: Path) -> str:
    """What points the entry at `store`: install for an entry install owns, a hand edit else."""
    target = shell_argument(str(store))
    if wiring.managed:
        return f"run `engmem install --agent {wiring.agent} --store {target}` to rewire it"
    return f"edit [mcp_servers.engmem] in {wiring.config} by hand to pass --store {target}"


def store_argument(args: object) -> Path | None:
    """The store the entry's `engmem mcp` will be handed, read the way the server reads it: the
    last `--store` wins, as in argparse, and a blank one counts as none."""
    if not isinstance(args, list):
        return None
    value = None
    for i, arg in enumerate(args):
        if not isinstance(arg, str):
            continue
        if arg == "--store" and i + 1 < len(args) and isinstance(args[i + 1], str):
            value = args[i + 1]
        elif arg.startswith("--store="):
            value = arg.removeprefix("--store=")
    return store_path_of(value)


def _launch(entry: dict) -> tuple[str | None, tuple[str, ...]]:
    command = entry.get("command")
    args = entry.get("args", [])
    strings = isinstance(args, list) and all(isinstance(arg, str) for arg in args)
    return (command if isinstance(command, str) else None), (tuple(args) if strings else ())


def _desktop_wiring() -> McpWiring | None:
    path = claude_desktop_config_path()
    mcp_servers = load_json_object(path).get("mcpServers")
    entry = mcp_servers.get("engmem") if isinstance(mcp_servers, dict) else None
    if not isinstance(entry, dict):
        return None
    # install rewrites `mcpServers.engmem` whatever wrote it, so it is always install's to fix
    return McpWiring(
        "claude-desktop", path, store_argument(entry.get("args")), True, *_launch(entry)
    )


def _codex_wiring() -> McpWiring | None:
    path = codex_config_path()
    if not path.is_file():
        return None
    text, _ = read_user_file(path)
    entry = engmem_mcp_server(parse_toml(path, text))
    if not isinstance(entry, dict):
        return None
    managed = _split_codex_block(path, text) is not None
    return McpWiring("codex", path, store_argument(entry.get("args")), managed, *_launch(entry))


# the clients whose MCP entry lives in a config on this machine, and where that config is
MCP_CONFIGS = {
    "claude-desktop": (claude_desktop_config_path, _desktop_wiring),
    "codex": (codex_config_path, _codex_wiring),
}


def mcp_wiring(agent: str) -> McpWiring | None:
    """`agent`'s engmem MCP entry, None when there is none; `SetupError` names a config that
    cannot be read. Reading only."""
    return MCP_CONFIGS[agent][1]()


def installed_mcp_wirings() -> tuple[list[McpWiring], list[str]]:
    """Every engmem MCP entry found, plus one line per config that could not be read; reading
    only — `engmem store show` reports, and never rewrites or migrates (AC-05.4)."""
    wirings: list[McpWiring] = []
    problems: list[str] = []
    for agent in MCP_CONFIGS:
        try:
            wiring = mcp_wiring(agent)
        except SetupError as exc:
            problems.append(f"{agent}: cannot check the MCP entry: {exc}")
            continue
        if wiring is not None:
            wirings.append(wiring)
    return wirings, problems


def shell_argument(value: str) -> str:
    return subprocess.list2cmdline([value]) if sys.platform == "win32" else shlex.quote(value)


def shell_quoted_mcp_command(store: Path) -> str:
    """The launch line as one shell argument, quoted for the shell the user will paste it into."""
    entry = stdio_mcp_entry(store)
    argv = [entry["command"], *entry["args"]]
    if sys.platform == "win32":
        return subprocess.list2cmdline([subprocess.list2cmdline(argv)])
    return shlex.quote(shlex.join(argv))


# engmem never observes who reaches either bridge, so neither line may call a setup protected:
# contracts/install.md, "ChatGPT: which bridge, and what it protects"
TUNNEL_BRIDGE_STATUS = (
    "OpenAI's Secure MCP Tunnel is the one supported bridge; OpenAI decides who may reach it, "
    "and engmem did not verify that"
)
PUBLIC_BRIDGE_STATUS = (
    "a public bridge (supergateway plus ngrok or cloudflared) is not protected: whoever holds "
    "its URL reads the store's documents, titles and search snippets; `engmem mcp --read-only` "
    "only refuses writes and does not make the store private"
)


def print_chatgpt_steps(store: Path) -> None:
    mcp_command = shell_quoted_mcp_command(store)
    print(
        "ChatGPT runs no local MCP server itself; it reaches `engmem mcp` through OpenAI's "
        "Secure MCP Tunnel:\n"
        "  1. create a tunnel at https://platform.openai.com/settings/organization/tunnels\n"
        "  2. export CONTROL_PLANE_API_KEY=<the tunnel's runtime key>\n"
        "     tunnel-client init --sample sample_mcp_stdio_local --profile engmem "
        f"--tunnel-id <tunnel-id> --mcp-command {mcp_command}\n"
        "     tunnel-client run --profile engmem\n"
        "  3. in ChatGPT: Settings -> Apps & Connectors -> Advanced -> Developer mode, then "
        "Create app, Connection: Tunnel"
    )
    print(f"bridge: {TUNNEL_BRIDGE_STATUS}")
    print(f"public bridge: {PUBLIC_BRIDGE_STATUS}")
