"""The one Claude Code SessionStart hook `install --hooks` adds and uninstall removes: it runs
`engmem draft status --hook`, which names unsaved drafts and records nothing (contracts/install.md,
"The SessionStart hook")."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from engmem.executables import on_path
from engmem.mcp_clients import load_json_object
from engmem.output import _escape_controls
from engmem.provenance import GIT_LOOKUP_SECONDS, _git_env
from engmem.runtime import fail
from engmem.user_files import SetupError, ensure_directory, replace_user_file

HOOK_COMMAND = "engmem draft status --hook"
HOOK_TIMEOUT_SECONDS = 10
_EVENT = "SessionStart"
# names a user's variant of the hook in a note; never decides what is removed
_LOOKALIKE = "draft status"
_FIX = "fix it by hand and re-run (refusing to overwrite it)"


def settings_path(local: bool) -> Path:
    # the personal file for --local: the committed `.claude/settings.json` would remind every
    # collaborator
    if local:
        return Path.cwd() / ".claude" / "settings.local.json"
    return Path.home() / ".claude" / "settings.json"


def reject_hooks_flag(agent: str) -> str | None:
    if agent == "claude":
        return None
    return f"--hooks adds a Claude Code hook and is for --agent claude only, not {agent}"


def _groups(config: dict, path: Path) -> list:
    """`hooks.SessionStart`, checked to the depth engmem edits; refuses any other shape."""
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError(f"{path} has a `hooks` key that is not a JSON object — {_FIX}")
    groups = hooks.get(_EVENT, [])
    if not isinstance(groups, list):
        raise SetupError(f"{path} has a `hooks.{_EVENT}` that is not a JSON array — {_FIX}")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise SetupError(
                f"{path} has a `hooks.{_EVENT}` entry that is not an object with a `hooks` "
                f"array — {_FIX}"
            )
    return groups


def _commands(groups: list) -> list[str]:
    return [
        handler["command"]
        for group in groups
        for handler in group["hooks"]
        if isinstance(handler, dict) and isinstance(handler.get("command"), str)
    ]


def _lookalike_notes(groups: list, path: Path) -> list[str]:
    return [
        f"note: {path} has a hook running `{_escape_controls(command)}` — not engmem's exact "
        "entry, so it is left as it is"
        for command in _commands(groups)
        if _LOOKALIKE in command and command != HOOK_COMMAND
    ]


def _write(path: Path, config: dict) -> None:
    ensure_directory(path.parent, "Claude Code settings directory")
    replace_user_file(path, json.dumps(config, indent=2) + "\n")


def install_hook(local: bool) -> list[str]:
    """The lines install prints; raises `SetupError`, having written nothing, on a settings file
    it cannot read or whose shape it does not know."""
    path = settings_path(local)
    config = load_json_object(path)
    groups = _groups(config, path)
    notes = _lookalike_notes(groups, path)
    if HOOK_COMMAND in _commands(groups):
        return [f"SessionStart hook already present in {path}", *notes]
    handler = {"type": "command", "command": HOOK_COMMAND, "timeout": HOOK_TIMEOUT_SECONDS}
    groups.append({"hooks": [handler]})
    config.setdefault("hooks", {})[_EVENT] = groups
    _write(path, config)
    ignore = _ignore_note(path) if local else []
    return [f"SessionStart hook added to {path}", *notes, *ignore]


def _ignore_note(path: Path) -> list[str]:
    """Whether git ignores the personal settings file; never raises, the file is written."""
    unknown = [f"note: could not check whether {path} is git-ignored"]
    git = on_path("git")
    if git is None:
        return unknown
    try:
        completed = subprocess.run(
            [git, "check-ignore", "-q", str(path)], cwd=path.parent, env=_git_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=GIT_LOOKUP_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return unknown
    if completed.returncode == 0:
        return []
    if completed.returncode == 1:
        return [
            f"note: {path} is not git-ignored, so it would be committed with the repository — "
            "add it to .gitignore"
        ]
    return unknown


def uninstall_hook(local: bool) -> tuple[int, int, list[str]]:
    """`(removed, failed, notes)`: removes exactly engmem's handlers and the containers they
    leave empty. A file whose bytes hold no entry is not reported when it cannot be parsed."""
    path = settings_path(local)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return 0, 0, []
    except OSError as exc:
        fail(f"cannot read {path}: {exc}")
        return 0, 1, []
    present = HOOK_COMMAND.encode("ascii") in data
    try:
        config = load_json_object(path)
        groups = _groups(config, path)
    except SetupError as exc:
        if not present:
            return 0, 0, []
        fail(str(exc))
        return 0, 1, []
    notes = _lookalike_notes(groups, path)
    removed = 0
    kept_groups = []
    for group in groups:
        handlers = [h for h in group["hooks"] if not _is_engmems(h)]
        removed += len(group["hooks"]) - len(handlers)
        if handlers or len(handlers) == len(group["hooks"]):
            kept_groups.append({**group, "hooks": handlers})
    if not removed:
        return 0, 0, notes
    hooks = config["hooks"]
    if kept_groups:
        hooks[_EVENT] = kept_groups
    else:
        del hooks[_EVENT]
    if not hooks:
        del config["hooks"]
    try:
        _write(path, config)
    except SetupError as exc:
        fail(str(exc))
        return 0, 1, notes
    return removed, 0, notes


def _is_engmems(handler: object) -> bool:
    return isinstance(handler, dict) and handler.get("command") == HOOK_COMMAND
