"""Layer 2: real agents run headless on the Layer 1 scenarios; opt-in, dry run by default."""

from __future__ import annotations

import json
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from agentsim import corpus as corpus_module
from agentsim.corpus import Task
from agentsim.sandbox import Sandbox

CONDITIONS = ("engmem", "grep", "none")
NOTES_HINT = "Earlier engineering notes, one Markdown file per past task, are in ./notes."
DEFAULT_SECONDS_PER_RUN = 180
DEFAULT_TOKENS_PER_RUN = 60_000
PILOT_FILE = "pilot.json"
LIVE_TIMEOUT = 900
CLAUDE_TOOLS = "Bash(engmem:*),Bash(git:*),Bash(date:*),Read,Grep,Glob"


class LiveRunRefused(RuntimeError):
    """A live run was asked for without the confirmations it needs."""


@dataclass(frozen=True)
class LiveAgent:
    """How one agent CLI is started headless and how its own report is read."""

    name: str
    binary: str
    install_agent: str
    start: Callable[[str], list[str]]
    save: Callable[[str], list[str]]
    usage: Callable[[str], dict]
    session: Callable[[str], str | None]


def _json_objects(text: str) -> list[dict]:
    """One JSON document, or JSON Lines; anything else is skipped."""
    stripped = text.strip()
    try:
        whole = json.loads(stripped)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        return [whole]
    objects = []
    for line in stripped.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "cached_input_tokens")


def _sum_usage(usages: Iterable[dict]) -> dict:
    total = {key: 0 for key in _TOKEN_KEYS}
    seen = False
    for usage in usages:
        for key in _TOKEN_KEYS:
            value = usage.get(key)
            if isinstance(value, int):
                total[key] += value
                seen = True
    return total if seen else {}


def claude_usage(text: str) -> dict:
    """Claude Code's `--output-format json` result: its `usage`, `duration_ms` and `num_turns`."""
    objects = _json_objects(text)
    result = next((o for o in reversed(objects) if o.get("type") == "result" or "usage" in o), None)
    if result is None:
        return {}
    report = _sum_usage([result.get("usage") or {}])
    for key in ("duration_ms", "num_turns", "total_cost_usd"):
        if key in result:
            report[key] = result[key]
    return report


def codex_usage(text: str) -> dict:
    """Codex `exec --json` events: the usage of each completed turn, summed."""
    usages = [o["usage"] for o in _json_objects(text) if isinstance(o.get("usage"), dict)]
    return _sum_usage(usages)


def claude_session(text: str) -> str | None:
    objects = _json_objects(text)
    return next((o["session_id"] for o in reversed(objects) if isinstance(o.get("session_id"), str)), None)


def codex_session(text: str) -> str | None:
    for o in _json_objects(text):
        for key in ("thread_id", "session_id"):
            if isinstance(o.get(key), str):
                return o[key]
    return None


AGENTS = {
    "claude": LiveAgent(
        name="claude",
        binary="claude",
        install_agent="claude",
        start=lambda prompt: ["claude", "-p", prompt, "--output-format", "json", "--allowedTools", CLAUDE_TOOLS],
        save=lambda session: [
            "claude", "-p", "/engmem.save.quick", "--resume", session, "--output-format", "json",
            "--allowedTools", CLAUDE_TOOLS,
        ],
        usage=claude_usage,
        session=claude_session,
    ),
    "codex": LiveAgent(
        name="codex",
        binary="codex",
        install_agent="codex",
        start=lambda prompt: ["codex", "exec", "--json", "--full-auto", prompt],
        save=lambda session: ["codex", "exec", "--json", "--full-auto", "resume", session, "$engmem-save-quick"],
        usage=codex_usage,
        session=codex_session,
    ),
}


def prompt_for(agent: LiveAgent, condition: str, task: Task) -> str:
    """The first message: the agent's own engmem command, the task with a notes hint, or the bare task."""
    if condition == "engmem":
        command = "$engmem" if agent.name == "codex" else "/engmem"
        return f"{command} {task.description}"
    if condition == "grep":
        return f"{task.description} {NOTES_HINT}"
    return task.description


@dataclass(frozen=True)
class LiveRun:
    task: Task
    agent: LiveAgent
    condition: str

    def commands(self) -> list[list[str]]:
        start = self.agent.start(prompt_for(self.agent, self.condition, self.task))
        if self.condition != "engmem":
            return [start]
        return [start, self.agent.save("<session id from the first call>")]


def plan(tasks: list[Task], agents: list[str], conditions: list[str]) -> list[LiveRun]:
    return [
        LiveRun(task, AGENTS[name], condition)
        for task in tasks
        for name in agents
        for condition in conditions
    ]


def estimate(runs: list[LiveRun], *, seconds_per_run: int, tokens_per_run: int, usd_per_mtok: float | None) -> dict:
    """Stated assumptions times the run count; nothing here is measured."""
    tokens = len(runs) * tokens_per_run
    return {
        "runs": len(runs),
        "minutes": round(len(runs) * seconds_per_run / 60, 1),
        "tokens": tokens,
        "usd": round(tokens / 1_000_000 * usd_per_mtok, 2) if usd_per_mtok is not None else None,
        "assumed_seconds_per_run": seconds_per_run,
        "assumed_tokens_per_run": tokens_per_run,
    }


def dry_run(runs: list[LiveRun], est: dict, out: Path) -> str:
    """The plan and its estimate as text; starts nothing and writes nothing."""
    lines = [
        "engmem e2e Layer 2 -- DRY RUN: no agent is started.",
        f"runs planned: {est['runs']} (pilot first: 1 run, then the rest)",
        f"estimated time: {est['minutes']} min at an assumed {est['assumed_seconds_per_run']} s per run",
        f"estimated tokens: {est['tokens']} at an assumed {est['assumed_tokens_per_run']} per run",
        "estimated cost: "
        + (f"{est['usd']} USD" if est["usd"] is not None else "not computed -- pass --usd-per-mtok to price it"),
        f"results would go to: {out}",
        "each run: a fresh sandbox (HOME, XDG_*, APPDATA), ENGMEM_STORE and ENGMEM_HOME unset, a copy of the generated store",
        "",
    ]
    for index, run in enumerate(runs, start=1):
        lines.append(f"[{index}] {run.task.id} / {run.agent.name} / {run.condition}")
        for argv in run.commands():
            lines.append("    $ " + shlex.join(argv))
    return "\n".join(lines) + "\n"


def _require_binaries(runs: list[LiveRun]) -> None:
    missing = sorted({r.agent.binary for r in runs if shutil.which(r.agent.binary) is None})
    if missing:
        raise LiveRunRefused(f"not on PATH: {', '.join(missing)}")


def _call_problem(name: str, call: object) -> str | None:
    if not isinstance(call, dict):
        return f"the pilot has no {name} call"
    if call.get("returncode") != 0:
        return f"the pilot's {name} call exited {call.get('returncode')!r}"
    if not call.get("usage"):
        return f"the pilot's {name} call reported no usage"
    return None


def pilot_problem(path: Path) -> str | None:
    """Why the pilot at `path` does not unlock a full run, or None when every call succeeded."""
    if not path.is_file():
        return f"no {path.name} in {path.parent}"
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"{path.name} cannot be read: {exc}"
    if not isinstance(records, list) or not records:
        return f"{path.name} records no run"
    for record in records:
        if not isinstance(record, dict):
            return f"{path.name} holds a malformed record"
        names = ("start", "save") if record.get("condition") == "engmem" else ("start",)
        for name in names:
            problem = _call_problem(name, record.get(name))
            if problem is not None:
                return problem
    return None


def execute(
    runs: list[LiveRun],
    out: Path,
    *,
    confirmed: bool,
    pilot: bool,
    pass_env: list[str],
    filler: int = corpus_module.DEFAULT_FILLER,
) -> list[dict]:
    """Runs the plan for real; refuses without confirmation, and a full run refuses without a pilot."""
    if not confirmed:
        raise LiveRunRefused("a live run needs --execute and --i-accept-the-cost")
    if not pilot:
        problem = pilot_problem(out / PILOT_FILE)
        if problem is not None:
            raise LiveRunRefused(f"run a successful pilot first (--pilot): {problem}")
    _require_binaries(runs)
    selected = runs[:1] if pilot else runs
    out.mkdir(parents=True, exist_ok=True)
    generated = corpus_module.generate(out / "corpus", filler=filler)
    results = [_execute_one(run, out / "sandboxes" / f"{i}", generated, pass_env) for i, run in enumerate(selected)]
    target = out / (PILOT_FILE if pilot else "live-results.json")
    target.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results


def _execute_one(run: LiveRun, root: Path, generated, pass_env: list[str]) -> dict:
    sandbox = Sandbox.create(root, inherit_env=False, pass_env=tuple(pass_env))
    checkout = sandbox.ensure_checkout(run.task.repo)
    store_sessions = generated.store_for(run.task)
    if run.condition == "engmem":
        sandbox.reset_store(store_sessions)
        sandbox.setup_engmem("store", "set", str(sandbox.store))
        sandbox.setup_engmem("install", "--agent", run.agent.install_agent, cwd=checkout)
    elif run.condition == "grep":
        shutil.copytree(store_sessions, checkout / "notes")
    started = time.perf_counter()
    first = sandbox.run(run.agent.start(prompt_for(run.agent, run.condition, run.task)), cwd=checkout, timeout=LIVE_TIMEOUT)
    record = {
        "task": run.task.id,
        "agent": run.agent.name,
        "condition": run.condition,
        "start": {"returncode": first.returncode, "seconds": round(first.seconds, 2), "usage": run.agent.usage(first.stdout)},
        "answer_has_quote": bool(run.task.quote) and run.task.quote in first.stdout,
    }
    session = run.agent.session(first.stdout)
    if run.condition == "engmem" and session:
        second = sandbox.run(run.agent.save(session), cwd=checkout, timeout=LIVE_TIMEOUT)
        record["save"] = {"returncode": second.returncode, "seconds": round(second.seconds, 2), "usage": run.agent.usage(second.stdout)}
    record["seconds"] = round(time.perf_counter() - started, 2)
    return record
