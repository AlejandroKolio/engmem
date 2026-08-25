"""`engmem install` / `engmem uninstall` — writes and removes the prompt
templates and trigger rule in an agent's own configuration. Command contract:
`ENGMEM-SPEC.md` §5.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

from engmem import __version__
from engmem.runtime import fail, resolve_store


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


# Copilot CLI ignores .github/prompts/ (github/copilot-cli#1113); it reads
# personal skills from ~/.copilot/skills/<name>/SKILL.md instead. Dots are
# illegal in skill names, so they become hyphens.
COPILOT_CLI_SKILL_MAP = {
    "engmem.start.md": "engmem",
    "engmem.save.md": "engmem-save",
    "engmem.save.quick.md": "engmem-save-quick",
}


# template bodies reference the dotted Claude command names; rewritten to the
# hyphenated skill names for Copilot CLI. Longest pattern first, or
# /engmem.save.quick would decay into /engmem-save.quick
COPILOT_CLI_COMMAND_RENAMES = (
    ("/engmem.save.quick", "/engmem-save-quick"),
    ("/engmem.save", "/engmem-save"),
)


VALID_AGENTS = ("claude", "copilot-ide", "copilot-cli", "claude-desktop")


# claude-desktop and copilot-cli are absent — see _agent_command_dir
_AGENT_TEMPLATE_MAPS = {
    "claude": TEMPLATE_INSTALL_MAP,
    "copilot-ide": COPILOT_IDE_TEMPLATE_INSTALL_MAP,
}


TRIGGER_RULE = (
    'Before proposing a plan, run `engmem search "<key terms for the task>"`'
)

# install's permissive skip-check: matches even a user's own unrelated
# sentence, safe only because it decides "leave alone", never "delete" —
# uninstall must never key removal off this
TRIGGER_MARKER = "`engmem search"

# the precise marker written just above the rule line, so uninstall removes
# exactly that line; _remove_trigger_rule also recognises the bare
# pre-sentinel TRIGGER_RULE line as a migration fallback for older installs
TRIGGER_SENTINEL = "<!-- engmem-trigger-rule -->"


def _validate_agent(command: str, agent: str) -> int:
    # not argparse choices= — see --role's identical reasoning in cli.py
    if agent in VALID_AGENTS:
        return 0
    fail(f"engmem {command}: unknown agent {agent!r} — valid agents: {', '.join(VALID_AGENTS)}")
    return 2


def _safe_mkdir(path: Path) -> str | None:
    """mkdir(parents=True, exist_ok=True); returns None on success, else a
    message naming the failure (e.g. a path component exists and isn't a
    directory) instead of letting the OSError escape."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        return None
    except OSError as exc:
        return f"{path} exists and is not usable as a directory: {exc}"


def _ensure_store(store: Path) -> int:
    sessions = store / "sessions"
    mkdir_error = _safe_mkdir(sessions)
    if mkdir_error is not None:
        fail(f"engmem install: cannot create the store: {mkdir_error}")
        return 2
    if (store / ".git").exists():
        return 0
    try:
        subprocess.run(
            ["git", "init"], cwd=store, check=True, capture_output=True, text=True
        )
    except FileNotFoundError:
        fail(
            "engmem install: `git` not found on PATH — the store must be a git "
            "repository. Install git and re-run."
        )
        return 2
    except subprocess.CalledProcessError as exc:
        fail(f"engmem install: `git init` failed in {store}: {exc.stderr.strip()}")
        return 2
    return 0


def _stamp_after_front_matter(content: str, stamp: str) -> str:
    # must not precede the YAML front matter: Claude Code only parses it when
    # --- is the file's very first line
    lines = content.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "".join(lines[: i + 1]) + stamp + "\n" + "".join(lines[i + 1 :])
    return stamp + "\n" + content


def _install_templates(dest_dir: Path, install_map: dict[str, str]) -> int:
    mkdir_error = _safe_mkdir(dest_dir)
    if mkdir_error is not None:
        fail(f"engmem install: cannot create the templates directory: {mkdir_error}")
        return 2
    source_root = resources.files("engmem") / "templates"
    for source_name, installed_name in install_map.items():
        content = (source_root / source_name).read_text(encoding="utf-8")
        stamp = (
            f"<!-- engmem-template: {source_name.removesuffix('.md')} v{__version__} -->"
        )
        (dest_dir / installed_name).write_text(
            _stamp_after_front_matter(content, stamp), encoding="utf-8"
        )
    return 0


def _to_skill_front_matter(content: str, skill_name: str) -> str:
    # Copilot skills require a name: field and have no argument-hint concept
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


def _install_skill_templates(skills_root: Path) -> None:
    source_root = resources.files("engmem") / "templates"
    for source_name, skill_name in COPILOT_CLI_SKILL_MAP.items():
        content = (source_root / source_name).read_text(encoding="utf-8")
        for dotted, hyphenated in COPILOT_CLI_COMMAND_RENAMES:
            content = content.replace(dotted, hyphenated)
        content = _to_skill_front_matter(content, skill_name)
        stamp = (
            f"<!-- engmem-template: {source_name.removesuffix('.md')} v{__version__} -->"
        )
        skill_dir = skills_root / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            _stamp_after_front_matter(content, stamp), encoding="utf-8"
        )


def _append_trigger_rule(instructions_file: Path) -> bool:
    """Adds the sentinel-anchored rule and returns True, or leaves the file
    untouched and returns False if `TRIGGER_MARKER` already appears anywhere
    in it. The caller reports a False result so a skip is never silent."""
    existing = (
        instructions_file.read_text(encoding="utf-8")
        if instructions_file.exists()
        else ""
    )
    if TRIGGER_MARKER in existing:
        return False
    instructions_file.parent.mkdir(parents=True, exist_ok=True)
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    instructions_file.write_text(
        prefix + TRIGGER_SENTINEL + "\n" + TRIGGER_RULE + "\n", encoding="utf-8"
    )
    return True


def _unlink_reporting_failure(path: Path) -> bool:
    # one locked/unreadable file must not abort the whole uninstall
    try:
        path.unlink()
        return True
    except OSError as exc:
        print(f"error: could not remove {path}: {exc}", file=sys.stderr)
        return False


def _remove_files(paths) -> tuple[int, int]:
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
    """Drops exactly the lines engmem wrote: a `TRIGGER_SENTINEL` line plus
    the rule line after it, or a bare pre-sentinel `TRIGGER_RULE` line.
    Deliberately not keyed off `TRIGGER_MARKER` — that broad substring can
    also match a user's own prose."""
    if not instructions_file.is_file():
        return 0
    lines = instructions_file.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    dropped = 0
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == TRIGGER_SENTINEL:
            dropped += 1
            i += 1
            if i < len(lines) and lines[i].strip() == TRIGGER_RULE:
                dropped += 1
                i += 1
            continue
        if stripped == TRIGGER_RULE:
            dropped += 1
            i += 1
            continue
        kept.append(lines[i])
        i += 1
    if dropped:
        instructions_file.write_text("".join(kept), encoding="utf-8")
    return dropped


class _MalformedConfigError(Exception):
    """Claude Desktop's config file exists but is not a mergeable JSON
    object — never discarded silently."""


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
    """`{}` for an absent or empty file, the parsed object for a valid one,
    or raises `_MalformedConfigError` — never a value a caller could write
    back without knowing it discarded something."""
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _MalformedConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _MalformedConfigError(
            f"{path} does not contain a JSON object at the top level"
        )
    return data


def _claude_desktop_mcp_entry(store: Path) -> dict:
    # a bare "engmem" is often not on PATH under Claude Desktop's minimal
    # launch environment, and relative paths are a documented failure mode
    return {
        "command": sys.executable,
        "args": ["-m", "engmem.cli", "mcp", "--store", str(store)],
    }


def _install_claude_desktop(store: Path) -> int:
    """Merges the `engmem` key into whatever is already on disk; never
    overwrites the file wholesale."""
    config_path = _claude_desktop_config_path()
    try:
        config = _load_json_object(config_path)
    except _MalformedConfigError as exc:
        fail(
            f"engmem install: {exc} — fix or remove the file by hand and re-run "
            f"(refusing to overwrite a config engmem cannot parse)"
        )
        return 2
    mcp_servers = config.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        fail(
            f"engmem install: {config_path} has a `mcpServers` key that is not a "
            f"JSON object — fix it by hand and re-run (refusing to overwrite it)"
        )
        return 2
    mcp_servers["engmem"] = _claude_desktop_mcp_entry(store)
    config["mcpServers"] = mcp_servers
    mkdir_error = _safe_mkdir(config_path.parent)
    if mkdir_error is not None:
        fail(f"engmem install: cannot create the Claude Desktop config directory: {mkdir_error}")
        return 2
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return 0


def _uninstall_claude_desktop() -> tuple[int, int]:
    """`(removed, failed)`, matching `_remove_files`'s shape. Removes exactly
    `mcpServers.engmem`; every other server and key is left in place."""
    config_path = _claude_desktop_config_path()
    try:
        config = _load_json_object(config_path)
    except _MalformedConfigError as exc:
        # duplicated to stdout: fail() reaches the stdout-only consumer too
        fail(
            f"{exc} — fix or remove the file by hand and re-run "
            f"(refusing to overwrite a config engmem cannot parse)"
        )
        return 0, 1
    mcp_servers = config.get("mcpServers")
    if not isinstance(mcp_servers, dict) or "engmem" not in mcp_servers:
        return 0, 0
    del mcp_servers["engmem"]
    config["mcpServers"] = mcp_servers
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return 1, 0


def _claude_desktop_entry_present() -> bool:
    """Whether an `engmem` entry already sits in the config — informational
    only, used to nudge `uninstall --agent <other>`. A malformed config
    counts as "nothing found" here."""
    try:
        config = _load_json_object(_claude_desktop_config_path())
    except _MalformedConfigError:
        return False
    mcp_servers = config.get("mcpServers")
    return isinstance(mcp_servers, dict) and "engmem" in mcp_servers


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
}


def _reject_local_home_scoped(command: str, agent: str) -> int:
    fail(
        f"engmem {command}: --local has no effect with --agent {agent} — "
        f"{_HOME_SCOPED_AGENTS[agent]}, never inside a project. Drop --local."
    )
    return 2


def _agent_command_dir(agent: str, local: bool) -> Path | None:
    """The one place install and uninstall both compute an agent mode's
    template directory, so their destinations can't drift apart. None for
    `claude-desktop` — its only artifact is a JSON key, not a file."""
    if agent == "claude":
        return (Path.cwd() if local else Path.home()) / ".claude" / "commands"
    if agent == "copilot-ide":
        return Path.cwd() / ".github" / "prompts"
    if agent == "copilot-cli":
        return Path.home() / ".copilot" / "skills"
    return None


def _agent_instructions_file(agent: str, local: bool) -> Path | None:
    """The trigger-rule file for an agent mode, or None where the mode writes
    no rule (`copilot-cli`, `claude-desktop`)."""
    if agent == "claude":
        base = Path.cwd() if local else Path.home()
        return Path.cwd() / "CLAUDE.md" if local else base / ".claude" / "CLAUDE.md"
    if agent == "copilot-ide":
        return Path.cwd() / ".github" / "copilot-instructions.md"
    return None


def _agent_targets(args: argparse.Namespace) -> tuple[list[Path], Path | None]:
    instructions = _agent_instructions_file(args.agent, args.local)
    dest_dir = _agent_command_dir(args.agent, args.local)
    install_map = _AGENT_TEMPLATE_MAPS.get(args.agent)
    if dest_dir is None or install_map is None:
        return [], instructions
    return [dest_dir / name for name in install_map.values()], instructions


def _template_paths_for_agent(agent: str, local: bool) -> list[Path]:
    """Same computation as `_agent_targets`, keyed on an explicit agent name
    rather than `args.agent` — used to check whether a *different* agent's
    files are present. `claude-desktop` is checked separately, by config
    entry rather than file existence."""
    dest_dir = _agent_command_dir(agent, local)
    if dest_dir is None:
        return []
    if agent == "copilot-cli":
        return [dest_dir / name / "SKILL.md" for name in COPILOT_CLI_SKILL_MAP.values()]
    install_map = _AGENT_TEMPLATE_MAPS.get(agent, {})
    return [dest_dir / name for name in install_map.values()]


def _other_agents_with_files_present(args: argparse.Namespace) -> list[str]:
    present = [
        agent
        for agent in ("claude", "copilot-ide", "copilot-cli")
        if agent != args.agent
        and any(p.is_file() for p in _template_paths_for_agent(agent, args.local))
    ]
    if args.agent != "claude-desktop" and _claude_desktop_entry_present():
        present.append("claude-desktop")
    return present


def cmd_uninstall(args: argparse.Namespace) -> int:
    failed = _validate_agent("uninstall", args.agent)
    if failed:
        return failed
    if args.local and args.agent in _HOME_SCOPED_AGENTS:
        return _reject_local_home_scoped("uninstall", args.agent)
    if _cwd_is_repo_scoped(args) and not (Path.cwd() / ".git").is_dir():
        return _reject_non_repo_cwd("uninstall")

    template_paths, instructions = _agent_targets(args)

    if args.agent == "copilot-cli":
        removed, failed = _remove_skill_dirs(_agent_command_dir("copilot-cli", args.local))
    elif args.agent == "claude-desktop":
        removed, failed = _uninstall_claude_desktop()
    else:
        removed, failed = _remove_files(template_paths)

    rule_lines = _remove_trigger_rule(instructions) if instructions else 0

    store = resolve_store(args.store)
    item_noun = "config entry(ies)" if args.agent == "claude-desktop" else "template file(s)"
    # the verb itself must carry the outcome for a reader who stops at line one
    summary_verb = "engmem uninstall incomplete" if failed else "engmem uninstalled"
    print(
        f"{summary_verb}: {removed} {item_noun} removed, "
        f"{rule_lines} trigger rule line(s) removed, agent={args.agent}"
    )
    if failed:
        print(f"{failed} {item_noun} could not be removed — see stderr; re-run to retry")
    if removed == 0 and not failed:
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
    return 2 if failed else 0


def cmd_install(args: argparse.Namespace) -> int:
    failed = _validate_agent("install", args.agent)
    if failed:
        return failed
    if args.local and args.agent in _HOME_SCOPED_AGENTS:
        return _reject_local_home_scoped("install", args.agent)
    if _cwd_is_repo_scoped(args) and not (Path.cwd() / ".git").is_dir():
        return _reject_non_repo_cwd("install")

    store = resolve_store(args.store)
    failed = _ensure_store(store)
    if failed:
        return failed

    trigger_added: bool | None = None
    if args.agent in ("claude", "copilot-ide"):
        failed = _install_templates(
            _agent_command_dir(args.agent, args.local), _AGENT_TEMPLATE_MAPS[args.agent]
        )
        if failed:
            return failed
        trigger_added = _append_trigger_rule(
            _agent_instructions_file(args.agent, args.local)
        )
    elif args.agent == "claude-desktop":
        # no commands directory, no instructions file — only the MCP config entry
        failed = _install_claude_desktop(store)
        if failed:
            return failed
    else:
        # copilot-cli: skills are self-invoked, no global-instructions file to append to
        _install_skill_templates(_agent_command_dir("copilot-cli", args.local))

    trigger_note = ""
    if trigger_added is False:
        trigger_note = " (trigger rule already present — not added)"
    print(f"engmem installed: store={store}, agent={args.agent}{trigger_note}")
    return 0
