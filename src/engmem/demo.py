"""The parts every `engmem walkthrough` demo shares: claiming its directory, writing only new files
inside it, the demo CODEX_HOME, the doctor gate and the printed environment lines.
Why each is shaped this way: contracts/install.md, "Walkthrough"."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from engmem.install import (
    TriggerRuleOutcome,
    _append_trigger_rule,
    _CODEX_MCP_NOTES,
    _ensure_directory,
    _git_init,
    _install_codex_mcp,
    _SetupError,
    _toml_string,
    shell_argument,
)
from engmem.provenance import _git_env
from engmem.sections import sections_for_role, split_sections
from engmem.spine import Doc

MARKER = ".engmem-walkthrough"
ABSENT = "not stated in the available material"
DOCTOR_SECONDS = 120.0
GIT_SECONDS = 60.0
# a warning on these means Codex would write elsewhere or not at all, so the demo cannot run
_BLOCKING_WARNINGS = ("warning: sandbox:", "warning: mcp entry:", "warning: mcp command:")
_LABELLED = re.compile(r"^\s*[-*]\s+\**(Decision|Reason|Source)\**\s*:\s*\**\s*(.*?)\s*$")
_COMMITTER = ("-c", "user.name=engmem walkthrough", "-c", "user.email=walkthrough@engmem.invalid")

SESSION_1_START = "$engmem record why OrderBook.place_order keys orders by request_id"
SESSION_1_DECISION = (
    "Decision: repeating an operation with the same request_id does not create a second order. "
    "Reason: a client that times out retries with the same request_id, and a second order would "
    "charge the customer twice. Rejected alternative: a new request_id per attempt, because each "
    "attempt would then place its own order. Source: demo_orders/orders.py, OrderBook.place_order"
)

ORDER_BOOK = (
    '"""Orders placed through OrderBook."""\n\n\n'
    "class OrderBook:\n"
    "    def __init__(self):\n"
    "        self.orders = []\n\n"
    "    def place_order(self, request_id, item):\n"
    "        for order in self.orders:\n"
    '            if order["request_id"] == request_id:\n'
    "                return order\n"
    '        order = {"order_id": len(self.orders) + 1, "request_id": request_id, "item": item}\n'
    "        self.orders.append(order)\n"
    "        return order\n"
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str
    failed_as: str = "missing"

    def __str__(self) -> str:
        return f"{'ok' if self.passed else self.failed_as}: {self.name}: {self.detail}"


def claim(root: Path, written_through: list[Path], marker_text: str) -> None:
    """Uses `root` only when it is new, empty, or this walkthrough built it, and never through a
    link."""
    marker = root / MARKER
    if root.exists() and not root.is_dir():
        raise _SetupError(f"{root} is not a directory — choose a new directory for the walkthrough")
    if root.is_dir() and not marker.is_file() and any(root.iterdir()):
        raise _SetupError(
            f"{root} is not empty and was not built by `engmem walkthrough` — choose a new "
            f"directory; the walkthrough writes only into one of its own"
        )
    built_by = marker_of(root)
    if built_by is not None and built_by != marker_text:
        raise _SetupError(
            f"{root} was built by `{built_by.strip()}`, another walkthrough — choose a new "
            f"directory for this one"
        )
    require_contained(root, written_through)
    _ensure_directory(root, "walkthrough directory")
    if not marker.is_file():
        write_new(marker, marker_text)


def marker_of(root: Path) -> str | None:
    """The marker's text, which names the walkthrough that built `root`; None without one."""
    try:
        return (root / MARKER).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def require_contained(root: Path, written_through: list[Path]) -> None:
    """Refuses a link, or a junction or any other path that resolves outside the demo."""
    real_root = os.path.realpath(root)
    for part in written_through:
        if part.is_symlink():
            raise _SetupError(
                f"{part} is a symbolic link — the demo must stay inside {root}; remove the "
                f"link and re-run"
            )
        if os.path.commonpath([real_root, os.path.realpath(part)]) != real_root:
            raise _SetupError(
                f"{part} resolves to {os.path.realpath(part)}, outside {root} — the demo "
                f"must stay inside it; remove the link or junction and re-run"
            )


def workspace_paths(workspace: Path, names: list[str]) -> list[Path]:
    """Each file under `workspace` with every parent directory it is written through."""
    paths = [workspace]
    for name in names:
        relative = Path(name)
        paths += [workspace / parent for parent in reversed(relative.parents[:-1])]
        paths.append(workspace / relative)
    return paths


def codex_home_paths(codex_home: Path) -> list[Path]:
    return [codex_home, codex_home / "config.toml", codex_home / "AGENTS.md"]


def write_new(path: Path, text: str) -> None:
    """A file the walkthrough creates and owns; it is never written over an existing one."""
    try:
        with open(path, "xb") as f:
            f.write(text.encode("utf-8"))
    except OSError as exc:
        raise _SetupError(f"cannot write {path}: {exc}") from exc


def write_workspace(workspace: Path, files: dict[str, str]) -> bool:
    """Writes only the files that are missing, so a re-run keeps what the agent changed."""
    created = not workspace.exists()
    for name, text in files.items():
        path = workspace / name
        if path.exists():
            continue
        _ensure_directory(path.parent, "workspace directory")
        write_new(path, text)
    _git_init(workspace, "the demo workspace")
    return created


def commit_once(workspace: Path) -> bool:
    """One commit of the demo files, so a save can record the commit it checked; never a second,
    and no hook or signing setting of the engineer's runs for it."""
    if _git(workspace, "rev-parse", "-q", "--verify", "HEAD").returncode == 0:
        return False
    hooks = str(workspace / ".git" / "engmem-no-hooks")
    steps = {
        "git add": ("add", "-A"),
        "git commit": (
            *_COMMITTER, "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={hooks}",
            "commit", "-q", "-m", "demo files from engmem walkthrough",
        ),
    }
    for name, argv in steps.items():
        completed = _git(workspace, *argv)
        if completed.returncode != 0:
            said = (completed.stderr.strip().splitlines() or ["no message"])[-1]
            raise _SetupError(f"`{name}` failed in {workspace}: {said}")
    return True


def _git(directory: Path, *argv: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *argv], cwd=directory, stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=GIT_SECONDS, env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise _SetupError(f"`git` did not finish within {GIT_SECONDS:g} s in {directory}") from exc
    except OSError as exc:
        raise _SetupError(f"cannot run `git` in {directory}: {exc}") from exc


def _base_codex_config(store: Path) -> str:
    quoted = _toml_string(str(store))
    return (
        "# Codex settings for the engmem walkthrough, read only when CODEX_HOME names this "
        "directory\n"
        'sandbox_mode = "workspace-write"\n\n'
        "[sandbox_workspace_write]\n"
        f"writable_roots = [{quoted}]\n\n"
        "[shell_environment_policy]\n"
        f"set = {{ ENGMEM_HOME = {quoted} }}\n\n"
    )


def wire_codex_home(codex_home: Path, store: Path) -> list[str]:
    """The demo's own CODEX_HOME: sandbox settings once, then install's idempotent MCP entry and
    trigger rule; the engineer's own ~/.codex is never opened."""
    config = codex_home / "config.toml"
    if not config.exists():
        _ensure_directory(codex_home, "Codex config directory")
        write_new(config, _base_codex_config(store))
    outcome = _install_codex_mcp(store, config)
    instructions = codex_home / "AGENTS.md"
    rule = _append_trigger_rule(instructions)
    rule_note = "already present" if rule is TriggerRuleOutcome.PRESENT else str(rule)
    return [
        f"codex config: {_CODEX_MCP_NOTES[outcome]} in {config}",
        f"codex instructions: trigger rule {rule_note} in {instructions}",
    ]


def _powershell_literal(value: str) -> str:
    """A single-quoted PowerShell string: nothing in it is interpolated, `'` is doubled."""
    return "'" + value.replace("'", "''") + "'"


def env_command(env: dict[str, str], command: str) -> list[str]:
    """`command` run with `env`, as lines to print: one for a POSIX shell; on Windows the
    PowerShell line, what it leaves behind, and the cmd.exe form."""
    if sys.platform != "win32":
        return ["".join(f"{name}={shell_argument(value)} " for name, value in env.items()) + command]
    powershell = "".join(f"$env:{name} = {_powershell_literal(value)}; " for name, value in env.items())
    cmd = "".join(f'set "{name}={value}" && ' for name, value in env.items())
    return [
        powershell + command,
        "  (PowerShell; the variables stay set in that window until you close it)",
        f"  cmd.exe: {cmd}{command}",
    ]


def doctor_findings(
    heading: str, agent: str, store: Path, env: dict[str, str], cwd: Path
) -> list[str]:
    """Runs `engmem doctor --agent <agent>` with `env`, prints its report, and returns the lines
    that stop the demo, each already printed."""
    print(heading)
    command = f"engmem doctor --agent {agent} --store {shell_argument(str(store))}"
    for line in env_command(env, command):
        print(f"  {line}")
    argv = [sys.executable, "-m", "engmem.cli", "doctor", "--agent", agent, "--store", str(store)]
    try:
        completed = subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=DOCTOR_SECONDS,
            # PYTHONSAFEPATH: an `engmem/` left in the demo directory must not stand in for the
            # installed package
            env={**os.environ, **env, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1"},
        )
    except subprocess.TimeoutExpired:
        return [_doctor_failed(f"did not finish within {DOCTOR_SECONDS:g} s")]
    except OSError as exc:
        return [_doctor_failed(f"cannot run it ({type(exc).__name__}: {exc})")]
    report = completed.stdout.splitlines()
    for line in report:
        print(f"  {line}")
    blocking = [
        line for line in report
        if line.startswith("error:") or line.startswith(_BLOCKING_WARNINGS)
    ]
    if completed.returncode != 0 and not blocking:
        said = (completed.stderr.strip().splitlines() or ["no report"])[-1]
        return [_doctor_failed(f"exited {completed.returncode}: {said}")]
    return blocking


def _doctor_failed(cause: str) -> str:
    reason = f"error: engmem doctor: {cause}"
    print(f"  {reason}")
    return reason


def not_ready(reasons: list[str], rerun: str, not_started: str) -> None:
    """`reasons` are `<status>: <subject>: <detail>` lines already printed above."""
    subjects = ", ".join(reason.split(": ", 2)[1] for reason in reasons)
    print(
        f"not ready: {len(reasons)} finding(s) stop the walkthrough ({subjects}) — fix each as "
        f"its line says, then re-run `{rerun}`. {not_started}; the walkthrough is not complete"
    )


def decisions(doc: Doc) -> list[dict[str, str]]:
    """Each `Decision:` line of the Decision Log with the `Reason:`/`Source:` lines after it."""
    groups: list[dict[str, str]] = []
    for section in sections_for_role(split_sections(doc.body), "decisions"):
        for line in section.body.splitlines():
            match = _LABELLED.match(line)
            if match is None:
                continue
            label, value = match.group(1).casefold(), match.group(2)
            if label == "decision":
                groups.append({"decision": value})
            elif groups:
                groups[-1].setdefault(label, value)
    return groups


def stated(value: str | None) -> bool:
    return bool(value) and not value.casefold().startswith(ABSENT)


def missing_labels(group: dict[str, str]) -> list[str]:
    return [label for label in ("reason", "source") if not stated(group.get(label))]
