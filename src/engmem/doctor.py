"""`engmem doctor` — reads the setup one agent depends on and names what is broken; changes
nothing. Why each check is shaped as it is: contracts/doctor.md."""

from __future__ import annotations

import argparse
import enum
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from engmem import __version__
from engmem.install import (
    _HOME_SCOPED_AGENTS,
    _SKILL_AGENTS,
    LEGACY_TRIGGER_RULES,
    MCP_CONFIGS,
    TRIGGER_MARKER,
    TRIGGER_RULE,
    McpWiring,
    WiringVerdict,
    _agent_instructions_file,
    _codex_config_path,
    _codex_home,
    _parse_toml,
    _read_user_file,
    _reject_local_home_scoped,
    _SetupError,
    _template_paths_for_agent,
    _validate_agent,
    mcp_wiring,
    rewire_action,
    wiring_verdict,
)
from engmem.runtime import (
    StoreChoice,
    StoreSettingError,
    StoreSource,
    default_store,
    locate_store,
    saved_store,
    source_description,
)
from engmem.staging import discard, read_document, stage

_PROBE_SECONDS = 15.0
_PROBE_OUTPUT_BYTES = 64 * 1024
_PROBE_TARGET = "engmem-doctor-probe.md"
_STAMP = re.compile(r"<!-- engmem-template: \S+ v(\S+) -->")
# the agents whose templates or skills run `engmem` as a shell command
_SHELL_AGENTS = ("claude", "copilot-ide", "copilot-cli", "codex")
_CLIENT_NAMES = {
    "claude": "Claude Code",
    "copilot-ide": "Copilot in the IDE",
    "copilot-cli": "Copilot CLI",
    "claude-desktop": "Claude Desktop",
    "codex": "Codex",
}


class Status(enum.StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Finding:
    status: Status
    subject: str
    detail: str

    def __str__(self) -> str:
        return f"{self.status}: {self.subject}: {self.detail}"


def _cause(exc: OSError) -> str:
    return f"{type(exc).__name__}: {exc.strerror or exc}"


def _install_command(agent: str, local: bool) -> str:
    return f"engmem install --agent {agent}{' --local' if local else ''}"


def cmd_doctor(args: argparse.Namespace) -> int:
    """Prints a summary line, then one line per check; exit 1 when any check is an error."""
    invalid = _validate_agent("doctor", args.agent)
    if invalid:
        return invalid
    if args.local and args.agent in _HOME_SCOPED_AGENTS:
        return _reject_local_home_scoped("doctor", args.agent)
    findings = diagnose(args.agent, args.local, args.store)
    counts = {status: sum(f.status is status for f in findings) for status in Status}
    print(
        f"engmem doctor --agent {args.agent}: {counts[Status.ERROR]} error(s), "
        f"{counts[Status.WARNING]} warning(s), {counts[Status.UNVERIFIED]} unverified"
    )
    for finding in findings:
        print(finding)
    return 1 if counts[Status.ERROR] else 0


def diagnose(agent: str, local: bool, explicit: str | None) -> list[Finding]:
    findings, choice = _store_findings(explicit)
    if choice is not None:
        findings += _sessions_findings(choice.path, agent, local)
    findings.append(
        Finding(Status.OK, "engmem", f"{__version__}, this command runs from {sys.executable}")
    )
    if agent in _SHELL_AGENTS:
        findings.append(_shell_engmem_finding(agent, local))
    findings += _template_findings(agent, local)
    findings += _trigger_rule_findings(agent, local)
    if agent in MCP_CONFIGS:
        findings += _mcp_findings(agent, choice)
    findings.append(_sandbox_finding(agent, choice))
    return findings


def _store_findings(explicit: str | None) -> tuple[list[Finding], StoreChoice | None]:
    try:
        choice = locate_store(explicit)
    except StoreSettingError as exc:
        return [Finding(Status.ERROR, "store", str(exc))], None
    findings = [Finding(Status.OK, "store", f"{choice.path} — source: {source_description(choice)}")]
    if choice.source not in (StoreSource.FLAG, StoreSource.ENV):
        return findings, choice
    try:
        saved = saved_store()
    except StoreSettingError as exc:
        findings.append(Finding(
            Status.ERROR, "saved choice",
            f"{exc} — a client launched without {choice.source} stops on it",
        ))
        return findings, choice
    fallback = StoreChoice(saved, StoreSource.SAVED) if saved else StoreChoice(
        default_store(), StoreSource.DEFAULT
    )
    if os.path.realpath(fallback.path) != os.path.realpath(choice.path):
        findings.append(Finding(
            Status.WARNING, "store",
            f"{choice.source} chooses it for this shell only — a client launched without it uses "
            f"{fallback.path}, source: {source_description(fallback)}",
        ))
    return findings, choice


def _sessions_findings(store: Path, agent: str, local: bool) -> list[Finding]:
    sessions = store / "sessions"
    try:
        names = [entry.name for entry in sessions.iterdir()]
    except FileNotFoundError as exc:
        return [Finding(
            Status.ERROR, "sessions",
            f"{sessions} does not exist ({_cause(exc)}) — run `{_install_command(agent, local)}` "
            f"to create the store, or `engmem store set PATH` if your documents are elsewhere",
        )]
    except OSError as exc:
        return [Finding(
            Status.ERROR, "sessions",
            f"cannot list {sessions} ({_cause(exc)}) — give your user read access to it, or "
            f"`engmem store set PATH` if the store is elsewhere",
        )]
    documents = sum(name.endswith(".md") and not name.startswith(".") for name in names)
    if documents == 0:
        listing = Finding(
            Status.WARNING, "sessions",
            f"{sessions} is readable but holds no documents — the memory is empty, so every "
            f"search finds nothing until a session is saved",
        )
    else:
        listing = Finding(Status.OK, "sessions", f"{sessions} — readable, {documents} document(s)")
    return [listing, _write_probe(sessions)]


def _write_probe(sessions: Path) -> Finding:
    """A real create through `staging.stage`, the step every draft and save takes first."""
    probe: Path | None = None
    try:
        probe = stage(sessions / _PROBE_TARGET, b"engmem doctor write probe\n")
        probe.unlink()
    except OSError as exc:
        if probe is None:
            return Finding(
                Status.ERROR, "sessions write",
                f"cannot create a file in {sessions} ({_cause(exc)}) — drafts and saves fail the "
                f"same way; give your user write access to it",
            )
        return Finding(
            Status.ERROR, "sessions write",
            f"created the probe {probe} but could not remove it ({_cause(exc)}) — delete it by hand",
        )
    except BaseException:
        # `stage` removes its own file when it raises; this covers an interrupt after it returned
        if probe is not None:
            discard(probe)
        raise
    return Finding(Status.OK, "sessions write", f"{sessions} — a probe file was created and removed")


def _version_probe(subject: str, argv: list[str], fix: str) -> Finding:
    """Runs `argv`, which must print `engmem <version>`, bounded by `_PROBE_SECONDS`."""
    shown = " ".join(argv)
    try:
        returncode, stdout, stderr = _run_bounded(argv)
    except subprocess.TimeoutExpired:
        return Finding(
            Status.ERROR, subject,
            f"`{shown}` did not answer within {_PROBE_SECONDS:g} s (TimeoutExpired) — {fix}",
        )
    except OSError as exc:
        return Finding(Status.ERROR, subject, f"cannot run {argv[0]} ({_cause(exc)}) — {fix}")
    printed = stdout.strip()
    if returncode != 0 or not printed.startswith("engmem "):
        said = (stderr.strip().splitlines() or [printed or "no output"])[-1]
        return Finding(Status.ERROR, subject, f"`{shown}` exited {returncode}: {said} — {fix}")
    version = printed.removeprefix("engmem ")
    if version != __version__:
        return Finding(
            Status.WARNING, subject,
            f"{argv[0]} runs engmem {version}, this command runs {__version__} — two installs; "
            f"{fix}",
        )
    return Finding(Status.OK, subject, f"{argv[0]} runs engmem {version}")


def _run_bounded(argv: list[str]) -> tuple[int, str, str]:
    """`(exit code, stdout, stderr)`; raises `TimeoutExpired` or `OSError`. See
    contracts/doctor.md, "Executables", for the neutral cwd, the environment and the files."""
    # output into files, not pipes: on Windows `run` drains pipes after the kill, and a
    # grandchild holding them would keep doctor waiting past the bound
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=_PROBE_SECONDS,
            cwd=os.path.abspath(os.sep),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1"},
        )
        out.seek(0)
        err.seek(0)
        return (
            completed.returncode,
            out.read(_PROBE_OUTPUT_BYTES).decode("utf-8", "replace"),
            err.read(_PROBE_OUTPUT_BYTES).decode("utf-8", "replace"),
        )


def _on_path(name: str) -> str | None:
    """`shutil.which` without the current directory: Windows' lookup tries it before PATH, and a
    relative or empty PATH entry names it too — a file there is the project's, not an install."""
    names = [name]
    if sys.platform == "win32":
        extensions = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        suffixed = [name + extension for extension in extensions]
        # a name already ending in one of them is tried as given first, as `shutil.which` does
        has_extension = os.path.splitext(name)[1].casefold() in {e.casefold() for e in extensions}
        names = [name, *suffixed] if has_extension else suffixed
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(entry):
            continue
        for candidate_name in names:
            candidate = os.path.join(entry, candidate_name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _shell_engmem_finding(agent: str, local: bool) -> Finding:
    found = _on_path("engmem")
    if found is None:
        return Finding(
            Status.ERROR, "engmem command",
            f"`engmem` is not on this shell's PATH (FileNotFoundError) — the {agent} templates run "
            f"`engmem search`; install engmem as a command (`uv tool install` or `pipx install`, "
            f"see README) or add its directory to PATH",
        )
    return _version_probe(
        "engmem command", [found, "--version"],
        f"put the engmem you use first on PATH, then re-run `{_install_command(agent, local)}`",
    )


def _template_findings(agent: str, local: bool) -> list[Finding]:
    subject = "skill" if agent in _SKILL_AGENTS else "template"
    install = _install_command(agent, local)
    return [
        _template_finding(subject, path, install)
        for path in _template_paths_for_agent(agent, local)
    ]


def _template_finding(subject: str, path: Path, install: str) -> Finding:
    try:
        text, _bom = read_document(path)
    except FileNotFoundError as exc:
        return Finding(Status.ERROR, subject, f"{path} is missing ({_cause(exc)}) — run `{install}`")
    except UnicodeDecodeError as exc:
        return Finding(
            Status.ERROR, subject, f"{path} is not valid UTF-8 ({exc.reason}) — run `{install}`"
        )
    except OSError as exc:
        return Finding(
            Status.ERROR, subject,
            f"cannot read {path} ({_cause(exc)}) — give your user read access, or run `{install}`",
        )
    stamp = _STAMP.search(text)
    if stamp is None:
        return Finding(
            Status.WARNING, subject,
            f"{path} carries no engmem version stamp — edited by hand or installed by an old "
            f"engmem; `{install}` rewrites it",
        )
    if stamp.group(1) != __version__:
        return Finding(
            Status.WARNING, subject,
            f"{path} was installed by engmem {stamp.group(1)}, this is {__version__} — run "
            f"`{install}` to update it",
        )
    return Finding(Status.OK, subject, f"{path} (v{__version__})")


def _trigger_rule_findings(agent: str, local: bool) -> list[Finding]:
    path = _agent_instructions_file(agent, local)
    if path is None:
        return []
    findings = [_trigger_rule_finding(path, _install_command(agent, local))]
    override = _codex_home() / "AGENTS.override.md"
    if agent == "codex" and override.exists():
        findings.append(Finding(
            Status.WARNING, "trigger rule",
            f"Codex reads {override} instead of {path}, so the rule does not load — copy it there",
        ))
    return findings


def _trigger_rule_finding(path: Path, install: str) -> Finding:
    try:
        text, _bom = read_document(path)
    except FileNotFoundError:
        return Finding(
            Status.WARNING, "trigger rule",
            f"{path} does not exist, so the agent is never told to search before planning — "
            f"run `{install}`",
        )
    except (OSError, UnicodeDecodeError) as exc:
        return Finding(
            Status.ERROR, "trigger rule", f"cannot read {path} ({type(exc).__name__}: {exc})"
        )
    lines = {line.strip() for line in text.splitlines()}
    if TRIGGER_RULE in lines:
        return Finding(Status.OK, "trigger rule", f"{path}")
    if lines.intersection(LEGACY_TRIGGER_RULES):
        return Finding(
            Status.WARNING, "trigger rule",
            f"{path} has an earlier wording, without --session, so searches go unattributed — "
            f"run `{install}` to update it",
        )
    if TRIGGER_MARKER in text:
        return Finding(Status.OK, "trigger rule", f"{path} — a rule in your own wording")
    return Finding(
        Status.WARNING, "trigger rule",
        f"{path} has no rule to search before planning — run `{install}`",
    )


def _mcp_findings(agent: str, choice: StoreChoice | None) -> list[Finding]:
    config_path, _find = MCP_CONFIGS[agent]
    try:
        wiring = mcp_wiring(agent)
    except _SetupError as exc:
        return [Finding(Status.ERROR, "mcp entry", f"{agent}: {exc}")]
    if wiring is None:
        return [Finding(
            Status.ERROR, "mcp entry",
            f"{agent}: no engmem entry in {config_path()} — run `engmem install --agent {agent}`",
        )]
    return [_entry_finding(wiring, choice), _command_finding(wiring)]


def _entry_finding(wiring: McpWiring, choice: StoreChoice | None) -> Finding:
    launches = f"{wiring.agent}: {wiring.config} launches the MCP server"
    if choice is None:
        return Finding(
            Status.UNVERIFIED, "mcp entry",
            f"{launches} with --store {_shown(wiring.store)} — not compared: the CLI store could "
            f"not be resolved (see the store error)",
        )
    cli = f"{choice.path} (source: {source_description(choice)})"
    verdict = wiring_verdict(wiring, choice.path)
    if verdict is WiringVerdict.INVALID:
        return Finding(
            Status.ERROR, "mcp entry",
            f"{launches} with --store {_shown(wiring.store)}, which is not a usable path (it "
            f"contains a NUL byte), so the server cannot start — {rewire_action(wiring, choice.path)}",
        )
    if verdict is WiringVerdict.MATCHES:
        return Finding(
            Status.OK, "mcp entry",
            f"{launches} with --store {wiring.store} — the same store as the CLI",
        )
    if verdict is WiringVerdict.AT_LAUNCH:
        return Finding(
            Status.UNVERIFIED, "mcp entry",
            f"{launches} without --store — it resolves the store at launch from "
            f"{wiring.agent}'s own environment, which engmem cannot see; this shell resolves {cli}",
        )
    if verdict is WiringVerdict.NO_FIXED_BASE:
        return Finding(
            Status.WARNING, "mcp entry",
            f"{launches} with --store {wiring.store}, a relative path that depends on the "
            f"directory the client starts it in — {rewire_action(wiring, choice.path)}",
        )
    return Finding(
        Status.WARNING, "mcp entry",
        f"CLI and MCP use different stores — CLI {cli}, MCP {wiring.store} (--store in "
        f"{wiring.config}); not broken by itself, but they search and write different memories: "
        f"{rewire_action(wiring, choice.path)}, or keep both if that is intended",
    )


def _shown(path: Path | None) -> str:
    """A path as text, quoted when it holds a NUL byte, which no terminal line should carry."""
    text = str(path)
    return repr(text) if "\0" in text else text


def _command_finding(wiring: McpWiring) -> Finding:
    fix = f"run `engmem install --agent {wiring.agent}` to record this Python"
    if not wiring.command:
        return Finding(Status.ERROR, "mcp command", f"{wiring.config} records no command — {fix}")
    if "\0" in wiring.command:
        return Finding(
            Status.ERROR, "mcp command",
            f"{wiring.config} records {wiring.command!r}, which contains a NUL byte — {fix}",
        )
    command = Path(wiring.command)
    if not command.is_absolute() and os.path.dirname(wiring.command):
        return Finding(
            Status.WARNING, "mcp command",
            f"{wiring.command} is a relative path, so it depends on the directory the client starts "
            f"in — {fix}, an absolute path",
        )
    if not command.is_absolute():
        found = _on_path(wiring.command)
        if found is None:
            return Finding(
                Status.ERROR, "mcp command",
                f"{wiring.command} is not on this shell's PATH (FileNotFoundError) — {fix}",
            )
        return Finding(
            Status.UNVERIFIED, "mcp command",
            f"{wiring.command} is {found} on this shell's PATH, but the client launches it with "
            f"its own PATH, which engmem cannot see — {fix}, an absolute path",
        )
    if not command.is_file():
        return Finding(
            Status.ERROR, "mcp command", f"{command} does not exist (FileNotFoundError) — {fix}"
        )
    if not os.access(command, os.X_OK):
        return Finding(
            Status.ERROR, "mcp command",
            f"{command} is not executable (PermissionError) — {fix}, or chmod +x it",
        )
    if wiring.args[:2] != ("-m", "engmem.cli"):
        return Finding(
            Status.UNVERIFIED, "mcp command",
            f"{command} exists and is executable; not run — engmem runs only the "
            f"`<python> -m engmem.cli` entry install writes",
        )
    return _version_probe("mcp command", [str(command), "-m", "engmem.cli", "--version"], fix)


def _sandbox_finding(agent: str, choice: StoreChoice | None) -> Finding:
    store = choice.path if choice is not None else None
    if agent == "chatgpt":
        return Finding(
            Status.UNVERIFIED, "tunnel",
            "ChatGPT reaches engmem through `tunnel-client`, whose profile records its own "
            "--mcp-command and --store; engmem does not read that profile — compare it with the "
            "store above",
        )
    if agent == "codex":
        return _codex_sandbox_finding(store)
    client = _CLIENT_NAMES[agent]
    return Finding(
        Status.UNVERIFIED, "sandbox",
        f"the checks above ran in this shell, outside {client}; whether {client}'s own sandbox "
        f"or permission rules let it read and write {store or 'the store'} is not verified",
    )


def _codex_sandbox_finding(store: Path | None) -> Finding:
    path = _codex_config_path()
    try:
        config = _parse_toml(path, _read_user_file(path)[0]) if path.is_file() else {}
    except _SetupError as exc:
        return Finding(Status.UNVERIFIED, "sandbox", f"cannot read Codex's sandbox settings: {exc}")
    mode = config.get("sandbox_mode")
    if mode == "danger-full-access":
        return Finding(
            Status.UNVERIFIED, "sandbox",
            f'{path} sets sandbox_mode = "danger-full-access", so Codex runs commands without a '
            f"sandbox — but access from inside Codex is not verified",
        )
    if mode == "read-only":
        return Finding(
            Status.WARNING, "sandbox",
            f'{path} sets sandbox_mode = "read-only": shell `engmem` commands Codex runs cannot '
            f"write drafts, saves or telemetry — approve each write when Codex asks, or use "
            f"workspace-write with the store in [sandbox_workspace_write] writable_roots",
        )
    roots = _writable_roots(config)
    unusable = next((root for root in roots if _real(root) is None), None)
    if unusable is not None:
        return Finding(
            Status.WARNING, "sandbox",
            f"{path} lists {unusable!r} in [sandbox_workspace_write] writable_roots, which is not "
            f"a usable path (it contains a NUL byte) — fix it by hand; whether Codex may write to "
            f"the store is not verified",
        )
    if store is not None and _under_writable_root(store, roots):
        return Finding(
            Status.UNVERIFIED, "sandbox",
            f"{store} is under [sandbox_workspace_write] writable_roots in {path} — the host "
            f"checks above passed, but access from inside Codex's sandbox is not verified",
        )
    return Finding(
        Status.WARNING, "sandbox",
        f"{store or 'the store'} is not in [sandbox_workspace_write] writable_roots in {path} — "
        f"unless it is inside the workspace Codex runs in, shell `engmem` commands under "
        f"Codex's workspace-write sandbox cannot write drafts, saves or telemetry there, whatever "
        f"the host checks above say; add it to writable_roots, or approve each write when Codex "
        f"asks",
    )


def _writable_roots(config: dict) -> list[str]:
    table = config.get("sandbox_workspace_write")
    roots = table.get("writable_roots") if isinstance(table, dict) else None
    if not isinstance(roots, list):
        return []
    return [root for root in roots if isinstance(root, str) and root]


def _real(root: str) -> str | None:
    """The root resolved, None when it cannot name a path at all (a NUL byte, checked in the
    string: Windows' non-strict realpath returns such a path unchanged instead of raising)."""
    if "\0" in root:
        return None
    return os.path.realpath(os.path.expanduser(root))


def _under_writable_root(store: Path, roots: list[str]) -> bool:
    real_store = Path(os.path.realpath(store))
    return any(real_store.is_relative_to(_real(root)) for root in roots)
