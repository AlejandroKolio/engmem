"""`engmem install` / `uninstall` — writes and removes an agent's templates and trigger rule."""

from __future__ import annotations

import argparse
import enum
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from engmem import __version__
from engmem.mcp_clients import (
    CODEX_MCP_NOTES,
    MCP_CONFIGS,
    claude_desktop_entry_present,
    codex_config_path,
    codex_home,
    codex_mcp_block_present,
    install_claude_desktop,
    install_codex_mcp,
    shell_argument,
    shell_quoted_mcp_command,
    uninstall_claude_desktop,
    uninstall_codex_mcp,
)
from engmem.runtime import (
    StoreSettingError,
    StoreSource,
    fail,
    locate_store,
    resolve_store,
)
from engmem.staging import newline_of, read_document
from engmem.template_stamps import digest, read_stamp, stamp_line, written_by_engmem
from engmem.user_files import SetupError, ensure_directory, read_user_file, replace_user_file


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


def _validate_agent(command: str, agent: str) -> int:
    # not argparse choices= — see --role's identical reasoning in cli.py
    if agent in VALID_AGENTS:
        return 0
    fail(f"engmem {command}: unknown agent {agent!r} — valid agents: {', '.join(VALID_AGENTS)}")
    return 2


def _write_template(path: Path, text: str) -> None:
    # staged and replaced, never written in place: a torn template no longer matches its digest,
    # so the next install would keep it as the user's edit instead of repairing it
    replace_user_file(path, text)


def _ensure_store(store: Path) -> None:
    ensure_directory(store / "sessions", "store directory")
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
        raise SetupError(
            f"`git` not found on PATH — {what} must be a git repository. "
            "Install git and re-run."
        ) from exc
    except OSError as exc:
        # a `git` on PATH that cannot be executed at all: exec raises PermissionError here,
        # not FileNotFoundError, and it is still the user's environment, not a bug
        raise SetupError(f"cannot run `git init` in {directory}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise SetupError(f"`git init` failed in {directory}: {exc.stderr.strip()}") from exc


def _stamp_after_front_matter(content: str, stamp: str) -> str:
    # must not precede the YAML front matter: Claude Code only parses it when
    # --- is the file's very first line
    lines = content.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "".join(lines[: i + 1]) + stamp + "\n" + "".join(lines[i + 1 :])
    return stamp + "\n" + content


# where install puts its new version of a template the user edited, for them to compare; not
# `.md`, so no client loads it as a command or a skill
PENDING_SUFFIX = ".engmem-new"


def pending_path(path: Path) -> Path:
    return path.with_name(path.name + PENDING_SUFFIX)


def _is_replaceable(path: Path, content: str) -> bool:
    """`path` is absent, or holds a body engmem wrote or is about to write: replacing it loses
    nothing of the user's."""
    try:
        existing, _bom = read_document(path)
    except FileNotFoundError:
        return True
    except UnicodeDecodeError:
        return False
    except OSError as exc:
        raise SetupError(f"cannot read the template {path}: {exc}") from exc
    stamp, body = read_stamp(existing)
    return written_by_engmem(stamp, body) or digest(body) == digest(content)


def pending_is_engmems(pending: Path) -> bool:
    """`pending` is absent, or as an install wrote it: nobody has carried an edit into it."""
    try:
        text, _bom = read_document(pending)
    except FileNotFoundError:
        return True
    except UnicodeDecodeError:
        return False
    except OSError as exc:
        raise SetupError(f"cannot read {pending}: {exc}") from exc
    return written_by_engmem(*read_stamp(text))


@dataclass(frozen=True)
class Conflict:
    pending: Path
    # the user is carrying edits into `pending`, so this install left it as it was too
    merging: bool


def _place_template(path: Path, content: str, source_name: str) -> Conflict | None:
    """Writes the stamped template over `path`, or, when the user edited `path`, beside it,
    never over a `.engmem-new` the user is editing."""
    text = _stamp_after_front_matter(content, stamp_line(source_name, __version__, content))
    pending = pending_path(path)
    unedited = pending_is_engmems(pending)
    if not _is_replaceable(path, content):
        if not unedited:
            return Conflict(pending, merging=True)
        _write_template(pending, text)
        return Conflict(pending, merging=False)
    _write_template(path, text)
    if unedited:
        try:
            # left by an earlier install whose conflict the user has since resolved
            pending.unlink(missing_ok=True)
        except OSError as exc:
            raise SetupError(
                f"cannot remove {pending}, left by an earlier install: {exc}"
            ) from exc
    return None


def _template_source(source_name: str) -> str:
    return (resources.files("engmem") / "templates" / source_name).read_text(encoding="utf-8")


def _install_templates(dest_dir: Path, install_map: dict[str, str]) -> list[Conflict]:
    ensure_directory(dest_dir, "templates directory")
    placed = (
        _place_template(dest_dir / installed_name, _template_source(source_name), source_name)
        for source_name, installed_name in install_map.items()
    )
    return [pending for pending in placed if pending is not None]


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


def _install_skill_templates(skills_root: Path, agent: str) -> list[Conflict]:
    conflicts = []
    for source_name, skill_name in COPILOT_CLI_SKILL_MAP.items():
        content = _SKILL_BODY_REWRITES[agent](_template_source(source_name))
        content = _to_skill_front_matter(content, skill_name)
        skill_dir = skills_root / skill_name
        ensure_directory(skill_dir, "skill directory")
        pending = _place_template(skill_dir / "SKILL.md", content, source_name)
        if pending is not None:
            conflicts.append(pending)
    return conflicts


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
    existing, bom = read_user_file(instructions_file) if instructions_file.exists() else ("", b"")
    migrated, updated = _with_current_trigger_rule(existing)
    if updated:
        replace_user_file(instructions_file, migrated, bom)
        return TriggerRuleOutcome.UPDATED
    if TRIGGER_MARKER in existing:
        return TriggerRuleOutcome.PRESENT
    ensure_directory(instructions_file.parent, "instructions directory")
    # the file's own line ending, not this platform's: LF appended to a CRLF file leaves it
    # with both, and the whole file then reads as changed under `core.autocrlf`
    newline = newline_of(existing) or "\n"
    prefix = existing if not existing or existing.endswith(("\n", "\r")) else existing + newline
    replace_user_file(
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


def _remove_pending(path: Path) -> None:
    """The new version an install left beside `path`: engmem's file, gone with the rest, unless
    the user has been carrying edits into it."""
    pending = pending_path(path)
    if not pending.is_file():
        return
    try:
        unedited = pending_is_engmems(pending)
    except SetupError as exc:
        print(f"error: could not check {pending}: {exc}", file=sys.stderr)
        return
    if not unedited:
        print(f"kept {pending}: it holds edits you made, not engmem's text")
        return
    _unlink_reporting_failure(pending)


def _remove_files(paths: Iterable[Path]) -> tuple[int, int]:
    removed = failed = 0
    for path in paths:
        _remove_pending(path)
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
        _remove_pending(skill_file)
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
    existing, bom = read_user_file(instructions_file)
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
        replace_user_file(instructions_file, "".join(kept), bom)
    return dropped


def _remove_trigger_rule_reporting_failure(instructions_file: Path | None) -> tuple[int, bool]:
    # an unwritable instructions file must not abort the uninstall and take the summary of what
    # was already removed with it
    if instructions_file is None:
        return 0, False
    try:
        return _remove_trigger_rule(instructions_file), False
    except SetupError as exc:
        fail(str(exc))
        return 0, True


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
_MCP_AGENTS = tuple(MCP_CONFIGS)


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
        return codex_home() / "AGENTS.md"
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
    if args.agent != "claude-desktop" and claude_desktop_entry_present():
        present.append("claude-desktop")
    if args.agent != "codex" and "codex" not in present and codex_mcp_block_present():
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
        entries_removed, entries_failed = uninstall_claude_desktop()
    elif args.agent == "codex":
        entries_removed, entries_failed = uninstall_codex_mcp()

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
    except SetupError as exc:
        fail(f"engmem install: {exc}")
        return 2


def _run_install(args: argparse.Namespace) -> int:
    store = resolve_store(args.store)
    _ensure_store(store)

    trigger_outcome: TriggerRuleOutcome | None = None
    conflicts: list[Conflict] = []
    if args.agent in ("claude", "copilot-ide"):
        conflicts = _install_templates(
            _agent_command_dir(args.agent, args.local), _AGENT_TEMPLATE_MAPS[args.agent]
        )
        trigger_outcome = _append_trigger_rule(
            _agent_instructions_file(args.agent, args.local)
        )
    elif args.agent == "claude-desktop":
        # no commands directory, no instructions file — only the MCP config entry
        install_claude_desktop(store)
    elif args.agent == "copilot-cli":
        # skills are self-invoked, no global-instructions file to append to
        conflicts = _install_skill_templates(
            _agent_command_dir("copilot-cli", args.local), "copilot-cli"
        )
    elif args.agent == "codex":
        # Codex runs a shell and an MCP client both; the templates pick whichever is there. The
        # config goes first: it is the step that can refuse, and a refusal must leave nothing
        codex_outcome = install_codex_mcp(store)
        conflicts = _install_skill_templates(_agent_command_dir("codex", args.local), "codex")
        trigger_outcome = _append_trigger_rule(_agent_instructions_file("codex", args.local))

    trigger_note = _TRIGGER_RULE_NOTES.get(trigger_outcome, "")
    # the verb itself carries the outcome for a reader who stops at line one
    verb = "engmem install incomplete" if conflicts else "engmem installed"
    print(f"{verb}: store={store}, agent={args.agent}{trigger_note}")
    for conflict in conflicts:
        print(_conflict_note(conflict))
    if args.agent == "codex":
        print(f"{CODEX_MCP_NOTES[codex_outcome]} in {codex_config_path()}")
        override = codex_home() / "AGENTS.override.md"
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
    return 2 if conflicts else 0


def _conflict_note(conflict: Conflict) -> str:
    pending = conflict.pending
    edited = pending.with_name(pending.name.removesuffix(PENDING_SUFFIX))
    if conflict.merging:
        return (
            f"conflict: {edited} was edited after engmem installed it, and {pending} holds "
            f"edits you are carrying into it, so both were left as they are. Move "
            f"{pending.name} over {edited.name} when it is ready; to start again from this "
            f"version's template, delete {pending.name} and re-run install"
        )
    return (
        f"conflict: {edited} was edited after engmem installed it, so it was left as it is; "
        f"this version's template is beside it, {pending}. Compare the two, carry any edit "
        f"you want to keep into {pending.name}, then move it over {edited.name}. Until then "
        f"{edited.name} keeps the workflow it was installed with; a file you keep editing is "
        f"compared again on every install"
    )


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
