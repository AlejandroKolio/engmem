"""`engmem install` / `uninstall` — writes and removes an agent's templates and trigger rule."""

from __future__ import annotations

import argparse
import enum
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from importlib import resources
from pathlib import Path

from engmem import __version__
from engmem.runtime import default_store, fail, resolve_store
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
# ~/.copilot/skills/<name>/SKILL.md instead.
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


VALID_AGENTS = ("claude", "copilot-ide", "copilot-cli", "claude-desktop")


# claude-desktop and copilot-cli are absent — see _agent_command_dir
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
    if (store / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "init"], cwd=store, check=True, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise _SetupError(
            "`git` not found on PATH — the store must be a git repository. "
            "Install git and re-run."
        ) from exc
    except OSError as exc:
        # a `git` on PATH that cannot be executed at all: exec raises PermissionError here,
        # not FileNotFoundError, and it is still the user's environment, not a bug
        raise _SetupError(f"cannot run `git init` in {store}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise _SetupError(f"`git init` failed in {store}: {exc.stderr.strip()}") from exc


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
    for source_name, skill_name in COPILOT_CLI_SKILL_MAP.items():
        content = _template_source(source_name)
        for dotted, hyphenated in COPILOT_CLI_COMMAND_RENAMES:
            content = content.replace(dotted, hyphenated)
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


def _claude_desktop_mcp_entry(store: Path) -> dict:
    # a bare "engmem" is often not on PATH under Claude Desktop's minimal
    # launch environment, and relative paths are a documented failure mode
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
    mcp_servers["engmem"] = _claude_desktop_mcp_entry(store)
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
    """An agent mode's template directory, or None for `claude-desktop`."""
    if agent == "claude":
        return (Path.cwd() if local else Path.home()) / ".claude" / "commands"
    if agent == "copilot-ide":
        return Path.cwd() / ".github" / "prompts"
    if agent == "copilot-cli":
        return Path.home() / ".copilot" / "skills"
    return None


def _agent_instructions_file(agent: str, local: bool) -> Path | None:
    """The trigger-rule file for an agent mode, or None where the mode writes none."""
    if agent == "claude":
        # --local puts it at the project root, which is where Claude Code reads a
        # project's CLAUDE.md from; the global one lives inside ~/.claude
        return Path.cwd() / "CLAUDE.md" if local else Path.home() / ".claude" / "CLAUDE.md"
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
    """`_agent_targets` keyed on an explicit agent name, for checking another agent's files."""
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
    invalid = _validate_agent("uninstall", args.agent)
    if invalid:
        return invalid
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

    rule_lines, rule_failed = _remove_trigger_rule_reporting_failure(instructions)

    store = resolve_store(args.store)
    item_noun = "config entry(ies)" if args.agent == "claude-desktop" else "template file(s)"
    incomplete = bool(failed) or rule_failed
    # the verb itself must carry the outcome for a reader who stops at line one
    summary_verb = "engmem uninstall incomplete" if incomplete else "engmem uninstalled"
    print(
        f"{summary_verb}: {removed} {item_noun} removed, "
        f"{rule_lines} trigger rule line(s) removed, agent={args.agent}"
    )
    if failed:
        print(f"{failed} {item_noun} could not be removed — see stderr; re-run to retry")
    if rule_failed:
        print(
            f"the trigger rule could not be removed from {instructions} — "
            f"see stderr; re-run to retry"
        )
    if removed == 0 and not incomplete:
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
    else:
        # copilot-cli: skills are self-invoked, no global-instructions file to append to
        _install_skill_templates(_agent_command_dir("copilot-cli", args.local))

    trigger_note = _TRIGGER_RULE_NOTES.get(trigger_outcome, "")
    print(f"engmem installed: store={store}, agent={args.agent}{trigger_note}")
    # the templates resolve the store at run time and `--store` is written nowhere they read
    # (no config files); Desktop is the exception, its MCP entry records the path itself
    if args.agent != "claude-desktop" and store != resolve_store(None):
        print(
            f"note: installed templates resolve $ENGMEM_HOME, else {default_store()}; --store "
            f"is not written into them — set ENGMEM_HOME={store} to use this store"
        )
    return 0
