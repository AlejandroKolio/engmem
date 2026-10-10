"""`engmem install` / `uninstall` — writes and removes an agent's templates and trigger rule."""

from __future__ import annotations

import argparse
import copy
import enum
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from engmem import __version__
from engmem.runtime import (
    StoreSettingError,
    StoreSource,
    fail,
    locate_store,
    resolve_store,
    store_path_of,
)
from engmem.staging import commit, discard, newline_of, read_document, stage


# source filename -> installed filename; engmem.start.md installs as engmem.md
# so the agent command is /engmem, not /engmem.start
TEMPLATE_INSTALL_MAP = {
    "engmem.start.md": "engmem.md",
    "engmem.save.md": "engmem.save.md",
    "engmem.save.quick.md": "engmem.save.quick.md",
}


# Copilot IDE only reads .github/prompts/ files with the double extension
# .prompt.md; a plain .md there is silently ignored
COPILOT_IDE_TEMPLATE_INSTALL_MAP = {
    "engmem.start.md": "engmem.prompt.md",
    "engmem.save.md": "engmem.save.prompt.md",
    "engmem.save.quick.md": "engmem.save.quick.prompt.md",
}


# Copilot CLI ignores .github/prompts/ (github/copilot-cli#1113); it reads personal skills from
# ~/.copilot/skills/<name>/SKILL.md instead. Codex reads the same layout from ~/.agents/skills
COPILOT_CLI_SKILL_MAP = {
    "engmem.start.md": "engmem",
    "engmem.save.md": "engmem-save",
    "engmem.save.quick.md": "engmem-save-quick",
}


# template bodies reference the dotted Claude command names; rewritten to the hyphenated skill
# names for Copilot CLI. Longest pattern first, or /engmem.save.quick would decay into
# /engmem-save.quick
COPILOT_CLI_COMMAND_RENAMES = (
    ("/engmem.save.quick", "/engmem-save-quick"),
    ("/engmem.save", "/engmem-save"),
)


# Codex mentions a skill as `$name`, so every command reference becomes one; the lookbehind
# keeps a path such as `~/Developer/engmem` out of it
CODEX_COMMAND_REFERENCE = re.compile(r"(?<![\w.~/-])/engmem((?:\.save)?(?:\.quick)?)\b")
# `$ARGUMENTS` is a Claude placeholder; under Codex the `$` would read as a skill mention
CODEX_ARGUMENTS_PLACEHOLDER = ('"$ARGUMENTS"', "the user's message that invoked this skill")


VALID_AGENTS = ("claude", "copilot-ide", "copilot-cli", "claude-desktop", "codex", "chatgpt")


# the skill-based and MCP-only agents are absent — see _agent_command_dir
_AGENT_TEMPLATE_MAPS = {
    "claude": TEMPLATE_INSTALL_MAP,
    "copilot-ide": COPILOT_IDE_TEMPLATE_INSTALL_MAP,
}


TRIGGER_RULE = (
    'Before proposing a plan, run `engmem search "<key terms for the task>" --session '
    "<draft-id>` (the draft `/engmem` just created; without it the search is unattributed)"
)
LEGACY_TRIGGER_RULES = (
    'Before proposing a plan, run `engmem search "<key terms for the task>"`',
)
_EVERY_TRIGGER_RULE = frozenset((TRIGGER_RULE, *LEGACY_TRIGGER_RULES))


class TriggerRuleOutcome(enum.StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    PRESENT = "present"


_TRIGGER_RULE_NOTES = {
    TriggerRuleOutcome.PRESENT: " (trigger rule already present — not added)",
    TriggerRuleOutcome.UPDATED: " (trigger rule updated to the current wording)",
}

# install's permissive skip-check: matches even a user's own unrelated sentence, safe only because
# it decides "leave alone", never "delete" — uninstall must never key removal off this
TRIGGER_MARKER = "`engmem search"

# the precise marker written just above the rule line, so uninstall removes
# exactly that line; _remove_trigger_rule also recognises the bare
# pre-sentinel TRIGGER_RULE line as a migration fallback for older installs
TRIGGER_SENTINEL = "<!-- engmem-trigger-rule -->"

# TOML has no writer in the standard library, so the Codex entry is a line block engmem owns
# between these two comments; uninstall removes exactly that span
CODEX_MCP_BEGIN = "# engmem-mcp-server: begin (managed by `engmem install --agent codex`)"
CODEX_MCP_END = "# engmem-mcp-server: end"


class CodexMcpOutcome(enum.StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    PRESENT = "present"
    FOREIGN = "foreign"


_CODEX_MCP_NOTES = {
    CodexMcpOutcome.ADDED: "MCP server entry added",
    CodexMcpOutcome.UPDATED: "MCP server entry updated",
    CodexMcpOutcome.PRESENT: "MCP server entry already current",
    CodexMcpOutcome.FOREIGN: "an [mcp_servers.engmem] engmem did not write is left untouched",
}


class _SetupError(Exception):
    """A step install/uninstall cannot complete; the message names the path and the cause, and
    reaches the user as an exit-2 diagnosis rather than a traceback."""


def _validate_agent(command: str, agent: str) -> int:
    # not argparse choices= — see --role's identical reasoning in cli.py
    if agent in VALID_AGENTS:
        return 0
    fail(f"engmem {command}: unknown agent {agent!r} — valid agents: {', '.join(VALID_AGENTS)}")
    return 2


def _ensure_directory(path: Path, what: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _SetupError(f"cannot create the {what} {path}: {exc}") from exc


def _read_user_file(path: Path) -> tuple[str, bytes]:
    """`(text, byte-order mark)` for a file engmem did not write; both failure modes name it."""
    try:
        return read_document(path)
    except UnicodeDecodeError as exc:
        raise _SetupError(
            f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start}) — engmem will "
            f"not rewrite a file it cannot read. Re-save it as UTF-8 and re-run"
        ) from exc
    except OSError as exc:
        raise _SetupError(f"cannot read {path}: {exc}") from exc


def _replace_user_file(path: Path, text: str, bom: bytes = b"") -> None:
    """Atomic replacement for a file engmem does not own — see contracts/install.md."""
    # written through the link, never over it: `os.replace` on the link itself detaches a
    # CLAUDE.md symlinked into a dotfiles repo from what it points at
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    try:
        tmp_path = stage(target, bom + text.encode("utf-8"))
    except OSError as exc:
        raise _SetupError(f"cannot write {path}: {exc}") from exc
    # BaseException, not OSError alone: a Ctrl-C landing between the stage and the commit must
    # still take the temp file with it
    try:
        commit(tmp_path, target)
    except OSError as exc:
        discard(tmp_path)
        raise _SetupError(f"cannot write {path}: {exc}") from exc
    except BaseException:
        discard(tmp_path)
        raise


def _write_template(path: Path, text: str) -> None:
    # engmem's own file, rewritten whole by every install: a torn write costs a re-run, not data
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise _SetupError(f"cannot write the template {path}: {exc}") from exc


def _ensure_store(store: Path) -> None:
    _ensure_directory(store / "sessions", "store directory")
    _git_init(store, "the store")


def _git_init(directory: Path, what: str) -> None:
    """`git init` in `directory` unless it already has a `.git`; `what` names it in the error."""
    if (directory / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "init"], cwd=directory, check=True, capture_output=True, text=True,
            # decoded as UTF-8 whatever the locale; `replace` so a byte in another charset cannot
            # raise before the failure is reported
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        raise _SetupError(
            f"`git` not found on PATH — {what} must be a git repository. "
            "Install git and re-run."
        ) from exc
    except OSError as exc:
        # a `git` on PATH that cannot be executed at all: exec raises PermissionError here,
        # not FileNotFoundError, and it is still the user's environment, not a bug
        raise _SetupError(f"cannot run `git init` in {directory}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise _SetupError(f"`git init` failed in {directory}: {exc.stderr.strip()}") from exc


def _stamp_after_front_matter(content: str, stamp: str) -> str:
    # must not precede the YAML front matter: Claude Code only parses it when
    # --- is the file's very first line
    lines = content.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "".join(lines[: i + 1]) + stamp + "\n" + "".join(lines[i + 1 :])
    return stamp + "\n" + content


def _version_stamp(source_name: str) -> str:
    return f"<!-- engmem-template: {source_name.removesuffix('.md')} v{__version__} -->"


def _template_source(source_name: str) -> str:
    return (resources.files("engmem") / "templates" / source_name).read_text(encoding="utf-8")


def _install_templates(dest_dir: Path, install_map: dict[str, str]) -> None:
    _ensure_directory(dest_dir, "templates directory")
    for source_name, installed_name in install_map.items():
        content = _template_source(source_name)
        _write_template(
            dest_dir / installed_name,
            _stamp_after_front_matter(content, _version_stamp(source_name)),
        )


def _to_skill_front_matter(content: str, skill_name: str) -> str:
    # Copilot and Codex skills require a name: field and have no argument-hint concept
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"template for skill '{skill_name}' does not open with YAML front matter")
    closing = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
    )
    if closing is None:
        raise ValueError(f"template for skill '{skill_name}' has an unclosed front matter block")
    body = [line for line in lines[1:closing] if not line.startswith("argument-hint:")]
    front_matter = ["---\n", f"name: {skill_name}\n", *body, "---\n"]
    return "".join(front_matter) + "".join(lines[closing + 1 :])


def _copilot_cli_skill_body(content: str) -> str:
    for dotted, hyphenated in COPILOT_CLI_COMMAND_RENAMES:
        content = content.replace(dotted, hyphenated)
    return content


def _codex_skill_body(content: str) -> str:
    content = content.replace(*CODEX_ARGUMENTS_PLACEHOLDER)
    return CODEX_COMMAND_REFERENCE.sub(
        lambda match: "$engmem" + match.group(1).replace(".", "-"), content
    )


_SKILL_BODY_REWRITES = {
    "copilot-cli": _copilot_cli_skill_body,
    "codex": _codex_skill_body,
}


def _install_skill_templates(skills_root: Path, agent: str) -> None:
    for source_name, skill_name in COPILOT_CLI_SKILL_MAP.items():
        content = _SKILL_BODY_REWRITES[agent](_template_source(source_name))
        content = _to_skill_front_matter(content, skill_name)
        skill_dir = skills_root / skill_name
        _ensure_directory(skill_dir, "skill directory")
        _write_template(
            skill_dir / "SKILL.md",
            _stamp_after_front_matter(content, _version_stamp(source_name)),
        )


def _with_current_trigger_rule(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    updated = False
    for i, line in enumerate(lines):
        if line.strip() not in LEGACY_TRIGGER_RULES:
            continue
        body = line.rstrip("\r\n")
        indent = body[: len(body) - len(body.lstrip())]
        lines[i] = indent + TRIGGER_RULE + line[len(body):]
        updated = True
    return "".join(lines), updated


def _append_trigger_rule(instructions_file: Path) -> TriggerRuleOutcome:
    """Updates a wording engmem itself wrote, adds the sentinel-anchored rule, or leaves a file
    already carrying `TRIGGER_MARKER` alone — see contracts/install.md."""
    existing, bom = _read_user_file(instructions_file) if instructions_file.exists() else ("", b"")
    migrated, updated = _with_current_trigger_rule(existing)
    if updated:
        _replace_user_file(instructions_file, migrated, bom)
        return TriggerRuleOutcome.UPDATED
    if TRIGGER_MARKER in existing:
        return TriggerRuleOutcome.PRESENT
    _ensure_directory(instructions_file.parent, "instructions directory")
    # the file's own line ending, not this platform's: LF appended to a CRLF file leaves it
    # with both, and the whole file then reads as changed under `core.autocrlf`
    newline = newline_of(existing) or "\n"
    prefix = existing if not existing or existing.endswith(("\n", "\r")) else existing + newline
    _replace_user_file(
        instructions_file,
        prefix + TRIGGER_SENTINEL + newline + TRIGGER_RULE + newline,
        bom,
    )
    return TriggerRuleOutcome.ADDED


def _unlink_reporting_failure(path: Path) -> bool:
    # one locked/unreadable file must not abort the whole uninstall
    try:
        path.unlink()
        return True
    except OSError as exc:
        print(f"error: could not remove {path}: {exc}", file=sys.stderr)
        return False


def _remove_files(paths: Iterable[Path]) -> tuple[int, int]:
    removed = failed = 0
    for path in paths:
        if not path.is_file():
            continue
        if _unlink_reporting_failure(path):
            removed += 1
        else:
            failed += 1
    return removed, failed


def _remove_skill_dirs(skills_root: Path) -> tuple[int, int]:
    removed = failed = 0
    for skill_name in COPILOT_CLI_SKILL_MAP.values():
        skill_dir = skills_root / skill_name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        if not _unlink_reporting_failure(skill_file):
            failed += 1
            continue
        # prune only if now empty — a user may keep their own files alongside
        try:
            if not any(skill_dir.iterdir()):
                skill_dir.rmdir()
        except OSError:
            pass
        removed += 1
    return removed, failed


def _remove_trigger_rule(instructions_file: Path) -> int:
    """Drops exactly the lines engmem wrote, keyed off the sentinel rather than the broader
    `TRIGGER_MARKER`."""
    if not instructions_file.is_file():
        return 0
    existing, bom = _read_user_file(instructions_file)
    lines = existing.splitlines(keepends=True)
    kept: list[str] = []
    dropped = 0
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == TRIGGER_SENTINEL:
            dropped += 1
            i += 1
            if i < len(lines) and lines[i].strip() in _EVERY_TRIGGER_RULE:
                dropped += 1
                i += 1
            continue
        if stripped in _EVERY_TRIGGER_RULE:
            dropped += 1
            i += 1
            continue
        kept.append(lines[i])
        i += 1
    if dropped:
        _replace_user_file(instructions_file, "".join(kept), bom)
    return dropped


def _remove_trigger_rule_reporting_failure(instructions_file: Path | None) -> tuple[int, bool]:
    # an unwritable instructions file must not abort the uninstall and take the summary of what
    # was already removed with it
    if instructions_file is None:
        return 0, False
    try:
        return _remove_trigger_rule(instructions_file), False
    except _SetupError as exc:
        fail(str(exc))
        return 0, True


def _claude_desktop_config_path() -> Path:
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


def _load_json_object(path: Path) -> dict:
    """`{}` for an absent file, the parsed object for a valid one, else `_SetupError`."""
    if not path.is_file():
        return {}
    # the BOM is dropped, not carried: engmem re-serialises the whole document, and RFC 8259
    # forbids emitting one
    text, _ = _read_user_file(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _SetupError(
            f"{path} is not valid JSON: {exc} — fix or remove the file by hand and "
            f"re-run (refusing to overwrite a config engmem cannot parse)"
        ) from exc
    if not isinstance(data, dict):
        raise _SetupError(
            f"{path} does not contain a JSON object at the top level — fix or remove "
            f"the file by hand and re-run (refusing to overwrite it)"
        )
    return data


def _stdio_mcp_entry(store: Path) -> dict:
    # a bare "engmem" is often not on PATH under an MCP client's minimal launch
    # environment, and relative paths are a documented failure mode
    return {
        "command": sys.executable,
        "args": ["-m", "engmem.cli", "mcp", "--store", str(store)],
    }


def _install_claude_desktop(store: Path) -> None:
    """Merges the `engmem` key into whatever is already on disk, never overwriting wholesale."""
    config_path = _claude_desktop_config_path()
    config = _load_json_object(config_path)
    mcp_servers = config.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        raise _SetupError(
            f"{config_path} has a `mcpServers` key that is not a JSON object — "
            f"fix it by hand and re-run (refusing to overwrite it)"
        )
    mcp_servers["engmem"] = _stdio_mcp_entry(store)
    config["mcpServers"] = mcp_servers
    _ensure_directory(config_path.parent, "Claude Desktop config directory")
    _replace_user_file(config_path, json.dumps(config, indent=2) + "\n")


def _uninstall_claude_desktop() -> tuple[int, int]:
    """`(removed, failed)`; removes exactly `mcpServers.engmem` and nothing else."""
    config_path = _claude_desktop_config_path()
    try:
        config = _load_json_object(config_path)
        mcp_servers = config.get("mcpServers")
        if not isinstance(mcp_servers, dict) or "engmem" not in mcp_servers:
            return 0, 0
        del mcp_servers["engmem"]
        _replace_user_file(config_path, json.dumps(config, indent=2) + "\n")
    except _SetupError as exc:
        # duplicated to stdout: fail() reaches the stdout-only consumer too
        fail(str(exc))
        return 0, 1
    return 1, 0


def _claude_desktop_entry_present() -> bool:
    try:
        config = _load_json_object(_claude_desktop_config_path())
    except _SetupError:
        return False
    mcp_servers = config.get("mcpServers")
    return isinstance(mcp_servers, dict) and "engmem" in mcp_servers


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def _codex_config_path() -> Path:
    return _codex_home() / "config.toml"


def _toml_string(value: str) -> str:
    # JSON's escapes are a subset of a TOML basic string's; ensure_ascii stays off because TOML
    # rejects the surrogate pairs json would write for an astral character. DEL is the one
    # character TOML forbids raw that json leaves alone
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise _SetupError(f"{value!r} is not valid Unicode and cannot be written to a TOML file")
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _codex_mcp_block(store: Path, newline: str) -> str:
    entry = _stdio_mcp_entry(store)
    args = ", ".join(_toml_string(arg) for arg in entry["args"])
    lines = (
        CODEX_MCP_BEGIN,
        "[mcp_servers.engmem]",
        f"command = {_toml_string(entry['command'])}",
        f"args = [{args}]",
        CODEX_MCP_END,
    )
    return newline.join(lines) + newline


def _parse_toml(path: Path, text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _SetupError(
            f"{path} is not valid TOML: {exc} — fix the file by hand and re-run "
            f"(refusing to rewrite a config engmem cannot parse)"
        ) from exc


def _engmem_mcp_server(config: dict) -> object:
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
        raise _SetupError(
            f"{path} has an engmem MCP block with no `{CODEX_MCP_END}` line — remove the "
            f"[mcp_servers.engmem] block by hand and re-run"
        )
    return "".join(lines[:begin]), "".join(lines[begin : end + 1]), "".join(lines[end + 1 :])


def _install_codex_mcp(store: Path, path: Path | None = None) -> CodexMcpOutcome:
    """Adds or refreshes engmem's own block in `path` (default: $CODEX_HOME's config); an entry
    the user wrote is never rewritten."""
    path = path or _codex_config_path()
    existing, bom = _read_user_file(path) if path.is_file() else ("", b"")
    config = _parse_toml(path, existing)
    if not isinstance(config.get("mcp_servers", {}), dict):
        raise _SetupError(
            f"{path} has an `mcp_servers` key that is not a table — fix it by hand and re-run "
            f"(refusing to rewrite it)"
        )
    newline = newline_of(existing) or "\n"
    entry = _stdio_mcp_entry(store)
    block = _codex_mcp_block(store, newline)
    split = _split_codex_block(path, existing)
    if split is None:
        if _engmem_mcp_server(config) is not None:
            return CodexMcpOutcome.FOREIGN
        prefix = existing if not existing or existing.endswith(("\n", "\r")) else existing + newline
        composed, outcome = prefix + block, CodexMcpOutcome.ADDED
    else:
        before, current, after = split
        if current == block:
            return CodexMcpOutcome.PRESENT
        _refuse_keys_added_to_the_block(path, config)
        composed, outcome = before + block + after, CodexMcpOutcome.UPDATED
    expected = copy.deepcopy(config)
    expected.setdefault("mcp_servers", {})["engmem"] = entry
    if _parsed_or_none(composed) != expected:
        # an inline `mcp_servers = {}` cannot be extended by a later table header
        raise _SetupError(
            f"adding [mcp_servers.engmem] would not leave {path} a valid config with that entry "
            f"— add an `engmem` server to mcp_servers by hand, with command = "
            f"{json.dumps(entry['command'])} and args = {json.dumps(entry['args'])}"
        )
    _ensure_directory(path.parent, "Codex config directory")
    _replace_user_file(path, composed, bom)
    return outcome


def _refuse_keys_added_to_the_block(path: Path, config: dict) -> None:
    # the block is rewritten whole, so a key typed under its header would be dropped silently
    current = _engmem_mcp_server(config)
    added = sorted(set(current) - {"command", "args"}) if isinstance(current, dict) else []
    if added:
        raise _SetupError(
            f"{path} has keys engmem did not write in its [mcp_servers.engmem] table "
            f"({', '.join(added)}) — remove them, or remove the `{CODEX_MCP_BEGIN}` and "
            f"`{CODEX_MCP_END}` lines to make the entry yours, and re-run"
        )


def _parsed_or_none(text: str) -> dict | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _uninstall_codex_mcp() -> tuple[int, int]:
    """`(removed, failed)`; removes exactly engmem's managed block and nothing else."""
    path = _codex_config_path()
    if not path.is_file():
        return 0, 0
    try:
        existing, bom = _read_user_file(path)
        # a config engmem has no block in is not engmem's to diagnose, broken or not
        split = _split_codex_block(path, existing)
        if split is None:
            return 0, 0
        config = _parse_toml(path, existing)
        before, _, after = split
        _require_only_engmem_removed(path, config, before + after)
        _replace_user_file(path, before + after, bom)
    except _SetupError as exc:
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
        raise _SetupError(
            f"removing engmem's [mcp_servers.engmem] block would change the rest of {path} — "
            f"remove the block, and whatever you added under it, by hand"
        )


def _without_empty_servers(config: dict | None) -> dict | None:
    # `[mcp_servers.engmem]` creates `mcp_servers` implicitly, so its removal may take an
    # otherwise empty table with it
    if config is None or config.get("mcp_servers") != {}:
        return config
    return {key: value for key, value in config.items() if key != "mcp_servers"}


def _codex_mcp_block_present() -> bool:
    path = _codex_config_path()
    if not path.is_file():
        return False
    try:
        return _split_codex_block(path, _read_user_file(path)[0]) is not None
    except _SetupError:
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


def _store_argument(args: object) -> Path | None:
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
    path = _claude_desktop_config_path()
    mcp_servers = _load_json_object(path).get("mcpServers")
    entry = mcp_servers.get("engmem") if isinstance(mcp_servers, dict) else None
    if not isinstance(entry, dict):
        return None
    # install rewrites `mcpServers.engmem` whatever wrote it, so it is always install's to fix
    return McpWiring(
        "claude-desktop", path, _store_argument(entry.get("args")), True, *_launch(entry)
    )


def _codex_wiring() -> McpWiring | None:
    path = _codex_config_path()
    if not path.is_file():
        return None
    text, _ = _read_user_file(path)
    entry = _engmem_mcp_server(_parse_toml(path, text))
    if not isinstance(entry, dict):
        return None
    managed = _split_codex_block(path, text) is not None
    return McpWiring("codex", path, _store_argument(entry.get("args")), managed, *_launch(entry))


# the clients whose MCP entry lives in a config on this machine, and where that config is
MCP_CONFIGS = {
    "claude-desktop": (_claude_desktop_config_path, _desktop_wiring),
    "codex": (_codex_config_path, _codex_wiring),
}


def mcp_wiring(agent: str) -> McpWiring | None:
    """`agent`'s engmem MCP entry, None when there is none; `_SetupError` names a config that
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
        except _SetupError as exc:
            problems.append(f"{agent}: cannot check the MCP entry: {exc}")
            continue
        if wiring is not None:
            wirings.append(wiring)
    return wirings, problems


def shell_argument(value: str) -> str:
    return subprocess.list2cmdline([value]) if sys.platform == "win32" else shlex.quote(value)


def _shell_quoted_mcp_command(store: Path) -> str:
    """The launch line as one shell argument, quoted for the shell the user will paste it into."""
    entry = _stdio_mcp_entry(store)
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


def _print_chatgpt_steps(store: Path) -> None:
    mcp_command = _shell_quoted_mcp_command(store)
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


def _cwd_is_repo_scoped(args: argparse.Namespace) -> bool:
    return args.agent == "copilot-ide" or args.local


def _reject_non_repo_cwd(command: str) -> int:
    fail(
        f"engmem {command}: {Path.cwd()} is not a git repository root — "
        f"expected a `.git` directory here. Run this command from the root "
        f"of the repository you want to configure."
    )
    return 2


# agents whose artifacts always live under $HOME; --local cannot be honoured for these
_HOME_SCOPED_AGENTS = {
    "claude-desktop": "its config file always lives under the user's home directory",
    "copilot-cli": "Copilot CLI loads skills only from ~/.copilot/skills",
    "codex": "engmem wires Codex through ~/.agents/skills and $CODEX_HOME",
    "chatgpt": "ChatGPT's connector lives in the ChatGPT workspace",
}

# agents whose installed artifacts are SKILL.md directories, not flat template files
_SKILL_AGENTS = ("copilot-cli", "codex")
# agents wired through an MCP server entry; the first two write no templates at all
_MCP_ONLY_AGENTS = ("claude-desktop", "chatgpt")
_MCP_AGENTS = ("claude-desktop", "codex")


def _reject_local_home_scoped(command: str, agent: str) -> int:
    fail(
        f"engmem {command}: --local has no effect with --agent {agent} — "
        f"{_HOME_SCOPED_AGENTS[agent]}, never inside a project. Drop --local."
    )
    return 2


def _agent_command_dir(agent: str, local: bool) -> Path | None:
    """An agent mode's template directory, or None for the MCP-only agents."""
    if agent == "claude":
        return (Path.cwd() if local else Path.home()) / ".claude" / "commands"
    if agent == "copilot-ide":
        return Path.cwd() / ".github" / "prompts"
    if agent == "copilot-cli":
        return Path.home() / ".copilot" / "skills"
    if agent == "codex":
        return Path.home() / ".agents" / "skills"
    return None


def _agent_instructions_file(agent: str, local: bool) -> Path | None:
    """The trigger-rule file for an agent mode, or None where the mode writes none."""
    if agent == "claude":
        # --local puts it at the project root, which is where Claude Code reads a
        # project's CLAUDE.md from; the global one lives inside ~/.claude
        return Path.cwd() / "CLAUDE.md" if local else Path.home() / ".claude" / "CLAUDE.md"
    if agent == "copilot-ide":
        return Path.cwd() / ".github" / "copilot-instructions.md"
    if agent == "codex":
        return _codex_home() / "AGENTS.md"
    return None


def _agent_targets(args: argparse.Namespace) -> tuple[list[Path], Path | None]:
    instructions = _agent_instructions_file(args.agent, args.local)
    dest_dir = _agent_command_dir(args.agent, args.local)
    install_map = _AGENT_TEMPLATE_MAPS.get(args.agent)
    if dest_dir is None or install_map is None:
        return [], instructions
    return [dest_dir / name for name in install_map.values()], instructions


def _template_paths_for_agent(agent: str, local: bool) -> list[Path]:
    """`_agent_targets` keyed on an explicit agent name, for checking another agent's files."""
    dest_dir = _agent_command_dir(agent, local)
    if dest_dir is None:
        return []
    if agent in _SKILL_AGENTS:
        return [dest_dir / name / "SKILL.md" for name in COPILOT_CLI_SKILL_MAP.values()]
    install_map = _AGENT_TEMPLATE_MAPS.get(agent, {})
    return [dest_dir / name for name in install_map.values()]


def _other_agents_with_files_present(args: argparse.Namespace) -> list[str]:
    present = [
        agent
        for agent in ("claude", "copilot-ide", *_SKILL_AGENTS)
        if agent != args.agent
        and any(p.is_file() for p in _template_paths_for_agent(agent, args.local))
    ]
    if args.agent != "claude-desktop" and _claude_desktop_entry_present():
        present.append("claude-desktop")
    if args.agent != "codex" and "codex" not in present and _codex_mcp_block_present():
        present.append("codex")
    return present


def cmd_uninstall(args: argparse.Namespace) -> int:
    invalid = _validate_agent("uninstall", args.agent)
    if invalid:
        return invalid
    if args.local and args.agent in _HOME_SCOPED_AGENTS:
        return _reject_local_home_scoped("uninstall", args.agent)
    if _cwd_is_repo_scoped(args) and not (Path.cwd() / ".git").is_dir():
        return _reject_non_repo_cwd("uninstall")

    # resolved before anything is removed: a saved choice that cannot be read stops the command
    # while there is still nothing to report half-done
    store = resolve_store(args.store)
    template_paths, instructions = _agent_targets(args)

    entries_removed = entries_failed = 0
    if args.agent in _SKILL_AGENTS:
        removed, failed = _remove_skill_dirs(_agent_command_dir(args.agent, args.local))
    elif args.agent in ("claude-desktop", "chatgpt"):
        removed = failed = 0
    else:
        removed, failed = _remove_files(template_paths)
    if args.agent == "claude-desktop":
        entries_removed, entries_failed = _uninstall_claude_desktop()
    elif args.agent == "codex":
        entries_removed, entries_failed = _uninstall_codex_mcp()

    rule_lines, rule_failed = _remove_trigger_rule_reporting_failure(instructions)

    incomplete = bool(failed or entries_failed) or rule_failed
    # the verb itself must carry the outcome for a reader who stops at line one
    summary_verb = "engmem uninstall incomplete" if incomplete else "engmem uninstalled"
    removed_parts = [
        *([f"{removed} template file(s) removed"] if args.agent not in _MCP_ONLY_AGENTS else []),
        *([f"{entries_removed} config entry(ies) removed"] if args.agent in _MCP_AGENTS else []),
        f"{rule_lines} trigger rule line(s) removed",
    ]
    print(f"{summary_verb}: {', '.join(removed_parts)}, agent={args.agent}")
    if failed:
        print(f"{failed} template file(s) could not be removed — see stderr; re-run to retry")
    if entries_failed:
        print(f"{entries_failed} config entry(ies) could not be removed — see stderr; re-run to retry")
    if rule_failed:
        print(
            f"the trigger rule could not be removed from {instructions} — "
            f"see stderr; re-run to retry"
        )
    if args.agent == "chatgpt":
        print(
            "the ChatGPT connector lives in your ChatGPT workspace: stop `tunnel-client` and "
            "delete the engmem app under Settings -> Apps & Connectors"
        )
    elif removed + entries_removed == 0 and not incomplete:
        # nothing found for the requested agent — worth a nudge before the
        # user concludes engmem was never installed
        other_agents = _other_agents_with_files_present(args)
        if other_agents:
            print(
                f"note: no {args.agent} files were found, but files for "
                f"{', '.join(other_agents)} are present — pass --agent to match "
                f"the mode you installed with if this wasn't what you meant"
            )
    print(f"store left untouched: {store}  (your documents — remove it yourself if you want)")
    return 2 if incomplete else 0


def cmd_install(args: argparse.Namespace) -> int:
    invalid = _validate_agent("install", args.agent)
    if invalid:
        return invalid
    if args.local and args.agent in _HOME_SCOPED_AGENTS:
        return _reject_local_home_scoped("install", args.agent)
    if _cwd_is_repo_scoped(args) and not (Path.cwd() / ".git").is_dir():
        return _reject_non_repo_cwd("install")

    try:
        return _run_install(args)
    except _SetupError as exc:
        fail(f"engmem install: {exc}")
        return 2


def _run_install(args: argparse.Namespace) -> int:
    store = resolve_store(args.store)
    _ensure_store(store)

    trigger_outcome: TriggerRuleOutcome | None = None
    if args.agent in ("claude", "copilot-ide"):
        _install_templates(
            _agent_command_dir(args.agent, args.local), _AGENT_TEMPLATE_MAPS[args.agent]
        )
        trigger_outcome = _append_trigger_rule(
            _agent_instructions_file(args.agent, args.local)
        )
    elif args.agent == "claude-desktop":
        # no commands directory, no instructions file — only the MCP config entry
        _install_claude_desktop(store)
    elif args.agent == "copilot-cli":
        # skills are self-invoked, no global-instructions file to append to
        _install_skill_templates(_agent_command_dir("copilot-cli", args.local), "copilot-cli")
    elif args.agent == "codex":
        # Codex runs a shell and an MCP client both; the templates pick whichever is there. The
        # config goes first: it is the step that can refuse, and a refusal must leave nothing
        codex_outcome = _install_codex_mcp(store)
        _install_skill_templates(_agent_command_dir("codex", args.local), "codex")
        trigger_outcome = _append_trigger_rule(_agent_instructions_file("codex", args.local))

    trigger_note = _TRIGGER_RULE_NOTES.get(trigger_outcome, "")
    print(f"engmem installed: store={store}, agent={args.agent}{trigger_note}")
    if args.agent == "codex":
        print(f"{_CODEX_MCP_NOTES[codex_outcome]} in {_codex_config_path()}")
        override = _codex_home() / "AGENTS.override.md"
        if override.exists():
            print(
                f"note: Codex reads {override} instead of AGENTS.md, so the trigger rule will "
                f"not load — copy it there by hand"
            )
        print(
            f"note: under Codex's default workspace-write sandbox `engmem` cannot write to "
            f"{store} — add it to [sandbox_workspace_write] writable_roots, or approve each write "
            f"when Codex asks"
        )
    if args.agent == "chatgpt":
        _print_chatgpt_steps(store)
        return 0
    note = _store_note(store, args.agent)
    if note is not None:
        print(note)
    return 0


def _store_note(store: Path, agent: str) -> str | None:
    """`--store` is a one-off override and is saved nowhere, so say where the commands run
    without it will look instead — see contracts/install.md."""
    who = (
        "engmem commands run without --store"
        if agent == "claude-desktop"
        else "the installed templates and engmem commands run without --store"
    )
    try:
        later = locate_store(None)
    except StoreSettingError as exc:
        return f"note: {who} will stop with an error until the saved choice is fixed: {exc}"
    # compared resolved: two spellings of one directory are the store they will find
    if store.resolve() == later.path.resolve():
        return None
    if later.source is StoreSource.ENV:
        action = f"set ENGMEM_HOME={store} to use this store"
    else:
        action = f"run `engmem store set {shell_argument(str(store))}` to make it their store"
    return f"note: --store is used by this install only — {who} use {later.path} ({later.source}); {action}"
