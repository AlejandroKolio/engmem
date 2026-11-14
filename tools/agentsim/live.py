"""Layer 2: real agents run headless on authored instances; opt-in, dry run by default (contracts/e2e.md)."""

from __future__ import annotations

import glob
import hashlib
import itertools
import json
import math
import random
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import NormalDist
from typing import Callable, Iterable

from agentsim import corpus as corpus_module
from agentsim import grading
from agentsim.corpus import Task
from agentsim.greparms import GUIDANCE, NOTES_INSTRUCTION
from agentsim.sandbox import TIMED_OUT, PathTooLong, Sandbox, padded_root, path_bytes

ARMS = ("E", "G", "G+", "N")
HANDOFF_ARMS = ("E", "GN", "G", "N")
ALL_ARMS = ("E", "G", "G+", "N", "GN")
NOTES_HINT = "Earlier engineering notes, one Markdown file per past task, are in ./notes."
ANSWER_SUFFIX = "Do not edit files. End with `ANSWER: …` and `SOURCES: …`."
SIMULATED_USER_REPLY = "Yes, save it as shown. It supersedes no earlier decision. No usefulness feedback."
CONFIRM_TURNS = 3
CONFIRMATIONS_ASSUMED = 1
MAX_TURNS = 25
LIVE_TIMEOUT = 900
PILOT_FILE = "pilot.json"
RESULTS_FILES = {"pilot": PILOT_FILE, "mini": "mini-results.json", "confirmatory": "live-results.json"}
AUDIT_FILE = "audit-sample.jsonl"
AUDIT_KEY_FILE = "audit-key.jsonl"
AUDIT_SHARE = 0.10
DEFAULT_REPEATS = 1
PLANNED_PER_CLASS = 10
FAMILIES = {"retrieval": (1, 2, 6, 7), "stale": (4, 10, 11, 12), "section": (3, 13)}
ALPHA = 0.05
POWER = 0.80
MDE_SPREADS = (0.5, 1.0)
PLAN_SEED = 20261111
DEFAULT_MODEL = "claude-opus-5-5"
STAGES = ("pilot", "mini", "confirmatory")
ENGMEM_TOOLS = "Bash(engmem:*),Bash(git:*),Bash(date:*),Read,Grep,Glob"
NOTES_TOOLS = "Bash(rg:*),Bash(git:*),Read,Grep,Glob"
PLAIN_TOOLS = "Bash(git:*),Read,Grep,Glob"
SAVER_NOTES_TOOLS = NOTES_TOOLS + ",Write"
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
}
PRICES_AS_OF = "the published list prices at implementation time, per million tokens; cache write at 1.25 x input"


@dataclass(frozen=True)
class Assumption:
    """What one session is assumed to cost before any pilot measures it."""

    seconds: int
    input: int
    cache_read: int
    cache_write: int
    output: int


ANSWER_SESSION = Assumption(seconds=150, input=15_000, cache_read=300_000, cache_write=40_000, output=5_000)
SAVE_SESSION = Assumption(seconds=90, input=5_000, cache_read=150_000, cache_write=20_000, output=4_000)
CONFIRM_SESSION = SAVE_SESSION
NO_HISTORY_SESSION = Assumption(seconds=90, input=8_000, cache_read=120_000, cache_write=20_000, output=4_000)


class LiveRunRefused(RuntimeError):
    """A live run was asked for without the confirmations it needs."""


@dataclass(frozen=True)
class LiveAgent:
    """How one agent CLI is started headless and how its own report is read."""

    name: str
    binary: str
    install_agent: str
    start: Callable[[str, str, str], list[str]]
    save: Callable[[str, str], list[str]]
    reply: Callable[[str, str, str], list[str]]
    usage: Callable[[str], dict]
    session: Callable[[str], str | None]
    max_turns_enforced: bool
    final_text: Callable[[str], str]
    tool_calls: Callable[[str], list[dict]]
    model: Callable[[str], str]
    tools_restricted: bool


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


def _claude_result(text: str) -> dict | None:
    objects = _json_objects(text)
    return next((o for o in reversed(objects) if o.get("type") == "result" or "usage" in o), None)


def claude_usage(text: str) -> dict:
    """Claude Code's closing `result` event (json or stream-json): `usage`, duration, turns, cost."""
    result = _claude_result(text)
    if result is None:
        return {}
    report = _sum_usage([result.get("usage") or {}])
    for key in ("duration_ms", "num_turns", "total_cost_usd"):
        if key in result:
            report[key] = result[key]
    return report


def claude_final_text(text: str) -> str:
    """The final message of a successful session's `result` event; empty when the session ended on
    an error (`error_max_turns` and the like) or never closed."""
    result = next((o for o in reversed(_json_objects(text)) if o.get("type") == "result"), None)
    if result is None or result.get("is_error") or str(result.get("subtype", "success")).startswith("error"):
        return ""
    value = result.get("result")
    return value if isinstance(value, str) else ""


def codex_final_text(text: str) -> str:
    """The last agent message of Codex `exec --json` events (`item.completed` with an agent message)."""
    final = ""
    for event in _json_objects(text):
        item = event.get("item") if event.get("type") == "item.completed" else None
        if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message"):
            value = item.get("text")
            final = value if isinstance(value, str) else final
    return final


def codex_tool_calls(text: str) -> list[dict]:
    """Codex's executed commands, in the shape the read log expects."""
    calls = []
    for event in _json_objects(text):
        item = event.get("item") if event.get("type") == "item.completed" else None
        if isinstance(item, dict) and item.get("type") == "command_execution" and isinstance(item.get("command"), str):
            calls.append({"name": "Bash", "input": {"command": item["command"]}})
    return calls


def codex_model(text: str) -> str:
    for event in _json_objects(text):
        if isinstance(event.get("model"), str):
            return event["model"]
    return ""


def claude_tool_calls(text: str) -> list[dict]:
    """Every `tool_use` block of the assistant events in a stream-json transcript."""
    calls = []
    for event in _json_objects(text):
        message = event.get("message") if event.get("type") == "assistant" else None
        for block in (message or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append({"name": block.get("name", ""), "input": block.get("input") or {}})
    return calls


def claude_model(text: str) -> str:
    for event in _json_objects(text):
        if event.get("type") == "system" and isinstance(event.get("model"), str):
            return event["model"]
    return ""


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


def tools_for(arm: str, saver: bool = False) -> str:
    if arm == "E":
        return ENGMEM_TOOLS
    if arm == "N":
        return PLAIN_TOOLS
    return SAVER_NOTES_TOOLS if saver else NOTES_TOOLS


def _claude_start(prompt: str, tools: str, model: str) -> list[str]:
    return [
        "claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
        "--max-turns", str(MAX_TURNS), "--model", model, "--allowedTools", tools,
    ]


def _claude_resume(session: str, prompt: str, model: str) -> list[str]:
    return [
        "claude", "-p", prompt, "--resume", session, "--output-format", "stream-json", "--verbose",
        "--max-turns", str(MAX_TURNS), "--model", model, "--allowedTools", ENGMEM_TOOLS,
    ]


def _claude_save(session: str, model: str) -> list[str]:
    return _claude_resume(session, "/engmem.save.quick", model)


def _codex_resume(session: str, prompt: str, model: str) -> list[str]:
    return ["codex", "exec", "--json", "--full-auto", "-m", model, "resume", session, prompt]


AGENTS = {
    "claude": LiveAgent(
        name="claude",
        binary="claude",
        install_agent="claude",
        start=_claude_start,
        save=_claude_save,
        reply=_claude_resume,
        usage=claude_usage,
        session=claude_session,
        max_turns_enforced=True,
        final_text=claude_final_text,
        tool_calls=claude_tool_calls,
        model=claude_model,
        tools_restricted=True,
    ),
    "codex": LiveAgent(
        name="codex",
        binary="codex",
        install_agent="codex",
        start=lambda prompt, tools, model: ["codex", "exec", "--json", "--full-auto", "-m", model, prompt],
        save=lambda session, model: _codex_resume(session, "$engmem-save-quick", model),
        reply=_codex_resume,
        usage=codex_usage,
        session=codex_session,
        max_turns_enforced=False,
        final_text=codex_final_text,
        tool_calls=codex_tool_calls,
        model=codex_model,
        tools_restricted=False,
    ),
}


def prompt_for(agent: LiveAgent, arm: str, description: str, *, saver: bool = False) -> str:
    """The first message of a session. Every finder ends with the same answer suffix; GN's earlier
    agent gets the fixed notes instruction instead, since it is the one that writes."""
    if arm == "E":
        command = "$engmem" if agent.name == "codex" else "/engmem"
        return f"{command} {description}" + ("" if saver else f" {ANSWER_SUFFIX}")
    if arm == "N":
        return f"{description} {ANSWER_SUFFIX}"
    if saver:
        return f"{description} {NOTES_HINT} {NOTES_INSTRUCTION}"
    guidance = f" {GUIDANCE}" if arm == "G+" else f" {NOTES_HINT}"
    return f"{description}{guidance} {ANSWER_SUFFIX}"


@dataclass(frozen=True)
class LiveRun:
    """One arm on one task, one repeat. A handoff task has an earlier session that saves first."""

    task: Task
    agent: LiveAgent
    arm: str
    repeat: int
    model: str

    @property
    def handoff(self) -> bool:
        return bool(self.task.saver)

    @property
    def has_earlier(self) -> bool:
        """An earlier session runs in this run's sandbox first: E and GN in the handoff."""
        return self.handoff and self.arm in ("E", "GN")

    def commands(self) -> list[list[str]]:
        argv: list[list[str]] = []
        if self.has_earlier:
            saver_prompt = prompt_for(self.agent, self.arm, self.task.saver["description"], saver=True)
            argv.append(self.agent.start(saver_prompt, tools_for(self.arm, saver=True), self.model))
            if self.arm == "E":
                argv.append(self.agent.save("<session id of the earlier agent>", self.model))
        finder = prompt_for(self.agent, self.arm, self.task.description)
        argv.append(self.agent.start(finder, tools_for(self.arm), self.model))
        if self.arm == "E":
            argv.append(self.agent.save("<session id of this agent>", self.model))
        return argv


def plan(tasks: list[Task], agents: list[str], *, arms: tuple[str, ...] | None = None,
         repeats: int = DEFAULT_REPEATS, model: str = DEFAULT_MODEL, seed: int = PLAN_SEED) -> list[LiveRun]:
    """Blocks of (agent, task, repeat) in a seeded random order; inside each block every arm once, in
    a seeded random order, so the arms interleave and no arm runs all its work first."""
    rng = random.Random(seed)
    blocks = [(name, task, repeat) for name in agents for task in tasks for repeat in range(1, repeats + 1)]
    rng.shuffle(blocks)
    runs = []
    for name, task, repeat in blocks:
        allowed = HANDOFF_ARMS if task.saver else ARMS
        block_arms = [a for a in (arms or allowed) if a in allowed]
        rng.shuffle(block_arms)
        runs += [LiveRun(task, AGENTS[name], arm, repeat, model) for arm in block_arms]
    return runs


def stage_tasks(tasks: list[Task], stage: str) -> tuple[list[Task], int]:
    """The tasks and repeats of a stage: the pilot runs the first task and the handoff once per arm;
    the mini run takes the first instance of every class once; the confirmatory run takes everything."""
    if stage == "pilot":
        handoff = [t for t in tasks if t.saver][:1]
        return [*[t for t in tasks if not t.saver][:1], *handoff], 1
    if stage == "mini":
        firsts: dict[int, Task] = {}
        for task in tasks:
            firsts.setdefault(task.scenario_class, task)
        return list(firsts.values()), 1
    if stage == "confirmatory":
        return tasks, DEFAULT_REPEATS
    raise ValueError(f"unknown stage {stage!r}; known: {', '.join(STAGES)}")


def planned_tasks(tasks: list[Task]) -> list[Task]:
    """The confirmatory set at its planned size, for the estimate only: each class's tasks repeated
    up to `PLANNED_PER_CLASS`, the handoff once."""
    by_class: dict[int, list[Task]] = {}
    for task in tasks:
        if not task.saver:
            by_class.setdefault(task.scenario_class, []).append(task)
    planned = [
        replace(group[i % len(group)], id=f"{group[i % len(group)].id}-{i}")
        for group in by_class.values() for i in range(PLANNED_PER_CLASS)
    ]
    return planned + [t for t in tasks if t.saver]


def family_of(scenario_class: int) -> str | None:
    """The pre-registered pooled family of a class; None for the descriptive classes 5, 8 and 9."""
    return next((name for name, classes in FAMILIES.items() if scenario_class in classes), None)


def minimum_detectable_effect(instances: int, spread: float) -> float:
    """Points of correct-rate difference a paired test over `instances` detects with `POWER`, at a
    Holm worst-case two-sided `ALPHA / len(FAMILIES)`, when per-instance differences have SD `spread`."""
    normal = NormalDist()
    z = normal.inv_cdf(1 - ALPHA / len(FAMILIES) / 2) + normal.inv_cdf(POWER)
    return 100 * z * spread / math.sqrt(instances) if instances else math.inf


def family_sizes(tasks: list[Task]) -> dict[str, int]:
    sizes = {name: 0 for name in FAMILIES}
    for task in tasks:
        family = family_of(task.scenario_class)
        if family and not task.saver:
            sizes[family] += 1
    return sizes


def _sessions(run: LiveRun) -> list[Assumption]:
    """The sessions a run starts, as assumed: answer, plus the earlier agent and saves where they apply."""
    answer = NO_HISTORY_SESSION if run.arm == "N" else ANSWER_SESSION
    sessions = [answer]
    if run.has_earlier:
        sessions.append(ANSWER_SESSION)
        if run.arm == "E":
            sessions += [SAVE_SESSION, *[CONFIRM_SESSION] * CONFIRMATIONS_ASSUMED]
    if run.arm == "E":
        sessions += [SAVE_SESSION, *[CONFIRM_SESSION] * CONFIRMATIONS_ASSUMED]
    return sessions


def usd(tokens: dict, prices: dict) -> float:
    """Price of token counts named like Claude's usage fields."""
    return (
        tokens.get("input_tokens", 0) * prices["input"]
        + tokens.get("output_tokens", 0) * prices["output"]
        + tokens.get("cache_read_input_tokens", 0) * prices["cache_read"]
        + tokens.get("cache_creation_input_tokens", 0) * prices["cache_write"]
    ) / 1_000_000


def _assumed_usage(a: Assumption) -> dict:
    return {"input_tokens": a.input, "output_tokens": a.output,
            "cache_read_input_tokens": a.cache_read, "cache_creation_input_tokens": a.cache_write}


def estimate(runs: list[LiveRun], model: str) -> dict:
    """Stated assumptions times the planned sessions; nothing here is measured."""
    prices = PRICES.get(model)
    sessions = [s for run in runs for s in _sessions(run)]
    tokens = sum(s.input + s.cache_read + s.cache_write + s.output for s in sessions)
    cost = sum(usd(_assumed_usage(s), prices) for s in sessions) if prices else None
    return {
        "runs": len(runs),
        "sessions": len(sessions),
        "hours": round(sum(s.seconds for s in sessions) / 3600, 1),
        "tokens": tokens,
        "usd": round(cost, 2) if cost is not None else None,
    }


def dry_run(tasks: list[Task], agents: list[str], model: str, out: Path, *, stage: str = "pilot",
            provisional: bool = False) -> str:
    """The plan of every stage with its estimate, and the commands of `stage`; starts nothing."""
    lines = [
        "engmem e2e Layer 2 -- DRY RUN: no agent is started.",
        f"model pinned: {model}; agent CLI version: recorded from `<cli> --version` at execution",
        f"flags: --output-format stream-json --verbose, --max-turns {MAX_TURNS}, timeout {LIVE_TIMEOUT} s per session",
        "arms: E (daily /engmem), G (notes + hint), G+ (notes + engmem's advice), N (nothing); handoff adds GN",
        f"order: seeded ({PLAN_SEED}), blocks of task x repeat shuffled, arms shuffled inside each block",
        f"answer suffix: {ANSWER_SUFFIX}",
        f"simulated user: after a save turn that wrote no document, up to {CONFIRM_TURNS} resumed turns reply "
        f"{SIMULATED_USER_REPLY!r}",
        f"prices: {PRICES_AS_OF}" + ("" if model in PRICES else f"; no price known for {model}"),
        "assumed per session: answer "
        + _describe(ANSWER_SESSION) + "; save " + _describe(SAVE_SESSION) + f", and {CONFIRMATIONS_ASSUMED} confirmation turns per save priced as a save"
        + "; no history " + _describe(NO_HISTORY_SESSION),
        f"grading: required-fact and stale-value regexes on the ANSWER text; {AUDIT_SHARE:.0%} exported for a human audit",
        f"results would go to: {out / RESULTS_FILES[stage]}; sandboxes, corpus and audit sample to "
        f"{stage_dir(out, stage)}",
    ]
    if provisional:
        lines.append("instances: PROVISIONAL -- a blind author's file replaces them before any stage beyond the pilot")
    lines += ["", "| stage | tasks | repeats | runs | sessions | hours | tokens | USD |", "|---|---|---|---|---|---|---|---|"]
    selected: list[LiveRun] = []
    for name in STAGES:
        chosen, repeats = stage_tasks(tasks, name)
        runs = plan(chosen, agents, repeats=repeats, model=model)
        est = estimate(runs, model)
        lines.append(
            f"| {name} | {len(chosen)} | {repeats} | {est['runs']} | {est['sessions']} | {est['hours']} "
            f"| {est['tokens']} | {est['usd'] if est['usd'] is not None else 'not priced'} |"
        )
        if name == stage:
            selected = runs
    planned = planned_tasks(tasks)
    est = estimate(plan(planned, agents, repeats=DEFAULT_REPEATS, model=model), model)
    lines.append(
        f"| confirmatory at {PLANNED_PER_CLASS} per class | {len(planned)} | {DEFAULT_REPEATS} | {est['runs']} "
        f"| {est['sessions']} | {est['hours']} | {est['tokens']} | {est['usd'] if est['usd'] is not None else 'not priced'} |"
    )
    lines += [
        "",
        "Pre-registered families (one repeat, analysed per instance; classes 5, 8 and 9 descriptive only):",
        "",
        "| family | classes | instances now | MDE at SD 0.5 | MDE at SD 1.0 | instances at 10 per class |",
        "|---|---|---|---|---|---|",
    ]
    now, at_plan = family_sizes(tasks), family_sizes(planned)
    for name, classes in FAMILIES.items():
        mde = [f"{minimum_detectable_effect(now[name], sd):.0f} pts" for sd in MDE_SPREADS]
        lines.append(f"| {name} | {', '.join(map(str, classes))} | {now[name]} | {mde[0]} | {mde[1]} | {at_plan[name]} |")
    lines.append(
        "Per-class verdicts would need at least 30 instances per class; class results are descriptive."
    )
    for agent_name in agents:
        if not AGENTS[agent_name].tools_restricted:
            lines.append(f"WARNING: {agent_name} runs without a tool restriction; its runs are flagged.")
    lines += ["", f"commands of the {stage} stage:"]
    for index, run in enumerate(selected, start=1):
        lines.append(f"[{index}] {run.task.id} / {run.agent.name} / {run.arm} / repeat {run.repeat}")
        for argv in run.commands():
            lines.append("    $ " + shlex.join(argv))
    return "\n".join(lines) + "\n"


def _describe(a: Assumption) -> str:
    return (f"{a.seconds} s, {a.input} input, {a.cache_read} cache-read, {a.cache_write} cache-write, "
            f"{a.output} output tokens")


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


def _earlier_problem(record: dict) -> str | None:
    """An E handoff's earlier session must have saved the decision it was given."""
    earlier = record.get("earlier")
    if record.get("arm") != "E" or not isinstance(earlier, dict):
        return None
    save = earlier.get("save")
    problem = _call_problem("earlier save", save)
    if problem is not None:
        return problem
    return None if save.get("new_docs") else "the pilot's earlier session saved no new document"


def _record_problem(record: dict) -> str | None:
    """What a pilot run lacks: its calls, the pinned model, a parsed answer, a clean read log, for E a
    save that ran to a final message, and in the handoff an earlier save that added a document."""
    arm = record.get("arm")
    if record.get("refused"):
        return f"the pilot's {record.get('agent')}/{arm} run was refused: {record['refused']}"
    for name in ("start", "save") if arm == "E" else ("start",):
        problem = _call_problem(name, record.get(name))
        if problem is not None:
            return problem
    problem = _earlier_problem(record)
    if problem is not None:
        return problem
    where = f"the pilot's {record.get('agent')}/{arm} run"
    if not record.get("model") or record.get("model_reported") != record.get("model"):
        return f"{where} reported model {record.get('model_reported')!r}, not the pinned {record.get('model')!r}"
    if not record.get("answer"):
        return f"{where} gave no ANSWER: line"
    if record.get("violations"):
        return f"{where} logged reads outside its bounds: {record['violations'][0]}"
    if arm == "E" and not record.get("engmem_calls"):
        return f"{where} made no engmem search or read call"
    if arm == "E" and not (record.get("save") or {}).get("final_message"):
        return f"{where}'s save ended with no final message"
    return None


def pilot_problem(path: Path, pairs: Iterable[tuple[str, str]] = ()) -> str | None:
    """Why the pilot at `path` does not unlock a larger stage for the (agent, arm) `pairs`, or None."""
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
        problem = _record_problem(record)
        if problem is not None:
            return problem
    covered = {(record.get("agent"), record.get("arm")) for record in records}
    missing = sorted(f"{agent}/{arm}" for agent, arm in set(pairs) - covered)
    if missing:
        return f"the pilot ran no {', '.join(missing)} run"
    return None


def execute(
    runs: list[LiveRun],
    out: Path,
    *,
    confirmed: bool,
    stage: str,
    pass_env: list[str],
    doc_size: str = corpus_module.DEFAULT_DOC_SIZE,
    provisional: bool = False,
) -> list[dict]:
    """Runs the plan for real; refuses without confirmation, in a stage that already ran in `out`, and
    beyond the pilot on provisional instances or without a pilot that ran every arm it plans."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; known: {', '.join(STAGES)}")
    if not confirmed:
        raise LiveRunRefused("a live run needs --execute and --i-accept-the-cost")
    pilot = stage == "pilot"
    if not pilot and provisional:
        raise LiveRunRefused("provisional instances run the pilot only; pass the blind author's file")
    if not pilot:
        problem = pilot_problem(out / PILOT_FILE, {(r.agent.name, r.arm) for r in runs})
        if problem is not None:
            raise LiveRunRefused(f"run a successful pilot first (--pilot): {problem}")
    roots = sandbox_roots(out, len(runs), stage=stage)
    _refuse_reuse(out, stage)
    _require_binaries(runs)
    stage_dir(out, stage).mkdir(parents=True)
    generated = prepare_corpus(stage_dir(out, stage) / "corpus", doc_size)
    versions = {r.agent.name: _version(r.agent) for r in runs}
    results = [
        {"run_id": f"{stage}-{index:0{SANDBOX_INDEX_WIDTH}d}"}
        | _execute_one(run, root, generated, pass_env, versions[run.agent.name])
        for index, (run, root) in enumerate(zip(runs, roots))
    ]
    (out / RESULTS_FILES[stage]).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    write_audit_sample(results, {run.task.id: run.task for run in runs}, stage_dir(out, stage))
    return results


SANDBOX_INDEX_WIDTH = 4


def stage_dir(out: Path, stage: str) -> Path:
    """Where one stage keeps its corpus, sandboxes and audit sample; no two stages share a directory."""
    return out / stage


def _refuse_reuse(out: Path, stage: str) -> None:
    """A stage starts only in a fresh directory: an existing sandbox would hand a run another run's notes,
    store and transcripts, and an existing results file would be overwritten."""
    if stage_dir(out, stage).exists():
        raise LiveRunRefused(
            f"{stage_dir(out, stage)} already exists, so its sandboxes would be reused; after checking it, remove "
            f"it, or start a new --out (beyond the pilot, holding a copy of the pilot's {PILOT_FILE})"
        )
    if (out / RESULTS_FILES[stage]).exists():
        raise LiveRunRefused(
            f"{out / RESULTS_FILES[stage]} already exists; after checking it, move it away, or start a new --out "
            f"(beyond the pilot, holding a copy of the pilot's {PILOT_FILE})"
        )


def sandbox_roots(out: Path, count: int, *, stage: str) -> list[Path]:
    """One root per run under the stage's own directory, the index zero-padded and the root padded, so
    every store path has one length."""
    if count > 10 ** SANDBOX_INDEX_WIDTH:
        raise LiveRunRefused(f"{count} runs exceed the {SANDBOX_INDEX_WIDTH}-digit sandbox index")
    try:
        return [padded_root(stage_dir(out, stage) / "sandboxes", f"{i:0{SANDBOX_INDEX_WIDTH}d}") for i in range(count)]
    except PathTooLong as exc:
        raise LiveRunRefused(str(exc)) from None


def prepare_corpus(root: Path, doc_size: str):
    """The stores the sandboxes copy from, with the answer keys (`tasks.json`, `facts.json`) removed,
    so nothing beside the sandboxes holds a key."""
    generated = corpus_module.generate(root, doc_size=doc_size)
    for name in ("tasks.json", "facts.json"):
        (root / name).unlink(missing_ok=True)
    return generated


def default_out(stamp: str) -> Path:
    """Outside the repository, so the instance files with their keys are not above the sandboxes."""
    return Path(tempfile.gettempdir()).resolve() / "engmem-e2e" / f"live-{stamp}"


def _version(agent: LiveAgent) -> str:
    try:
        done = subprocess.run([agent.binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown ({exc})"
    return (done.stdout or done.stderr).strip()


_CHAIN = re.compile(r"&&|\|\||;|\||\n")


def _segments(command: str) -> list[list[str]]:
    """A shell command split at `&&`, `||`, `;`, `|` and newlines, each part split into words."""
    parts = []
    for part in _CHAIN.split(command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if words:
            parts.append(words)
    return parts


_HOME_PREFIX = re.compile(r"^(?:~|\$HOME\b|\$\{HOME\})(?=$|[/\\])")


_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _path_like(word: str) -> str | None:
    value = word.split("=", 1)[1] if (word.startswith("-") or _ASSIGNMENT.match(word)) and "=" in word else word
    if _HOME_PREFIX.match(value):
        return value
    if value.startswith("-") or value.startswith("~"):
        return None
    if "/" in value or "\\" in value or value.startswith("."):
        return value
    return None


SCRIPT_PROGRAMS = ("sed", "awk", "gawk", "mawk", "nawk", "grep", "egrep", "fgrep", "rg")
_SCRIPT_OPTIONS = ("-e", "--expression", "--regexp")
_FILE_OPTIONS = ("-f", "--file")
_VALUE_OPTIONS = {
    "awk": ("-F", "-v"), "gawk": ("-F", "-v"), "mawk": ("-F", "-v"), "nawk": ("-F", "-v"),
    "grep": ("-A", "-B", "-C", "-m"), "egrep": ("-A", "-B", "-C", "-m"), "fgrep": ("-A", "-B", "-C", "-m"),
    "rg": ("-A", "-B", "-C", "-m", "-g", "--glob", "-t", "--type", "-T", "--type-not"),
}


def _operands(words: list[str]) -> list[str]:
    """The words that may name a file, leading `NAME=value` assignments included; for sed, awk, grep and rg
    the script or pattern (the first operand, or an `-e` value) is left out, an `-f` value stays."""
    lead = next((i for i, word in enumerate(words) if not _ASSIGNMENT.match(word)), len(words))
    assigned, words = words[:lead], words[lead:]
    if not words:
        return assigned
    program = Path(words[0]).name
    if program not in SCRIPT_PROGRAMS:
        return assigned + words[1:]
    named: list[str] = []
    operands: list[str] = []
    script_given = False
    options_done = False
    rest = iter(words[1:])
    for word in rest:
        if options_done or not word.startswith("-") or word == "-":
            operands.append(word)
        elif word == "--":
            options_done = True
        elif word in _SCRIPT_OPTIONS:
            script_given = True
            next(rest, None)
        elif program == "rg" and word == "--files":
            script_given = True
        elif word.startswith(tuple(f"{o}=" for o in _SCRIPT_OPTIONS if o.startswith("--"))):
            script_given = True
        elif word in _FILE_OPTIONS:
            script_given = True
            named += [w for w in (next(rest, None),) if w is not None]
        elif word.startswith("--file="):
            script_given = True
            named.append(word)
        elif word in _VALUE_OPTIONS.get(program, ()):
            next(rest, None)
        else:
            named.append(word)
    return assigned + named + (operands if script_given else operands[1:])


def _resolve(path: str, cwd: Path, home: Path) -> Path:
    """`path` against `cwd`, with a leading `~`, `$HOME` or `${HOME}` taken as the sandbox's HOME."""
    match = _HOME_PREFIX.match(path)
    if match:
        rest = path[match.end():].lstrip("/\\")
        return (home / rest).resolve() if rest else home.resolve()
    candidate = Path(path)
    return (candidate if candidate.is_absolute() else cwd / candidate).resolve()


_WILDCARD = re.compile(r"[*?\[]")
GLOB_LIMIT = 1000


def _targets(path: str, cwd: Path, home: Path) -> list[Path] | None:
    """What `path` reaches: itself, or for a glob every existing match, and the literal part before its first
    wildcard when nothing matches; None when that cannot be bounded (over `GLOB_LIMIT` matches, or `..`
    after a wildcard with no match)."""
    if not _WILDCARD.search(path):
        return [_resolve(path, cwd, home)]
    match = _HOME_PREFIX.match(path)
    written = str(home / path[match.end():].lstrip("/\\")) if match else path
    pattern = written if Path(written).is_absolute() else str(cwd / written)
    found = [Path(hit).resolve() for hit in itertools.islice(glob.iglob(pattern), GLOB_LIMIT + 1)]
    if len(found) > GLOB_LIMIT:
        return None
    if found:
        return found
    parts = Path(pattern).parts
    literal = next((i for i, part in enumerate(parts) if _WILDCARD.search(part)), len(parts))
    if ".." in parts[literal:]:
        return None
    return [Path(*parts[:literal]).resolve()]


INSTALLED_COPIES = ("commands", "skills", "CLAUDE.md")


def _reaches_agent_memory(resolved: Path, home: Path) -> bool:
    """Inside `~/.claude` or a `~/.claude*` file such as `~/.claude.json`, or a folder that holds them; never
    engmem's installed copies, which `carried_memory` guards instead."""
    claude = home / ".claude"
    if any(resolved.is_relative_to(claude / name) for name in INSTALLED_COPIES):
        return False
    if claude.is_relative_to(resolved):
        return True
    return resolved.is_relative_to(home) and resolved != home and resolved.relative_to(home).parts[0].startswith(".claude")


MEMORY_READ = "read Claude Code's agent memory"
ENGMEM_MCP_TOOLS = ("engmem_search", "engmem_search_by_role", "engmem_read")


def engmem_calls(calls: list[dict]) -> int:
    """Searches and reads that went through engmem: a shell segment `engmem search|read`, or the MCP tool."""
    count = 0
    for call in calls:
        name = call.get("name", "")
        if any(name == tool or name.endswith(f"__{tool}") for tool in ENGMEM_MCP_TOOLS):
            count += 1
            continue
        command = (call.get("input") or {}).get("command")
        if isinstance(command, str):
            count += sum(1 for words in _segments(command) if words[:1] == ["engmem"] and words[1:2] in (["search"], ["read"]))
    return count


def read_violations(calls: list[dict], root: Path, cwd: Path, store: Path | None) -> list[str]:
    """Every path a tool call names (`~` and `$HOME` are the sandbox HOME) that lies outside the sandbox `root`
    or reaches Claude Code's `~/.claude*`; for E (`store` given), a store path not read by engmem."""
    found = []
    root, store = root.resolve(), store.resolve() if store is not None else None
    home = root / "home"
    for call in calls:
        name, given = call.get("name", ""), call.get("input") or {}
        keys = ("file_path", "path", "notebook_path", *(("pattern",) if name == "Glob" else ()))
        named = [(v, None) for k, v in given.items() if k in keys and isinstance(v, str)]
        command = given.get("command") if isinstance(given.get("command"), str) else ""
        for words in _segments(command):
            program = next((word for word in words if not _ASSIGNMENT.match(word)), "")
            named += [(p, program) for p in map(_path_like, _operands(words)) if p]
        for path, program in named:
            targets = _targets(path, cwd, home)
            if targets is None:
                found.append(f"{name} outside the sandbox, as far as the pattern can be bounded: {path}")
            elif any(not target.is_relative_to(root) for target in targets):
                found.append(f"{name} outside the sandbox: {path}")
            elif any(_reaches_agent_memory(target, home) for target in targets):
                found.append(f"{name} {MEMORY_READ}: {path}")
            elif store is not None and any(t.is_relative_to(store) for t in targets) and program != "engmem":
                found.append(f"{name} read the store directly: {path}")
    return found


def grade_record(task: Task, final_text: str) -> dict:
    answer = grading.answer_of(final_text)
    required, stale = grading.compile_all(task.required), grading.compile_all(task.stale_patterns)
    required_ok, stale_avoided = grading.grade_answer(answer, required, stale)
    stale_named = any(rule.search(answer) for rule in stale)
    both = stale_named and any(rule.search(answer) for rule in required)
    return {
        "answer": answer,
        "required_ok": required_ok,
        "stale_avoided": stale_avoided if task.stale_patterns and answer else None,
        "stale_caveated": stale_avoided and stale_named,
        "needs_human": bool(answer) and (both or (stale_avoided and stale_named)),
        "correct": bool(answer) and required_ok and stale_avoided,
    }


def write_audit_sample(results: list[dict], tasks: dict[str, Task], directory: Path, share: float = AUDIT_SHARE,
                       seed: int = PLAN_SEED) -> list[dict]:
    """A seeded `share` of the runs, at least one, and every run that `needs_human`, in a seeded order, as
    rows blind to arm, agent and grade; the key from row id to run id goes to its own file."""
    if not results:
        return []
    rng = random.Random(seed)
    sample = rng.sample(results, max(1, math.ceil(len(results) * share)))
    sample += [record for record in results if record.get("needs_human") and not any(record is s for s in sample)]
    rng.shuffle(sample)
    rows, key = [], []
    for record in sample:
        task = tasks[record["task"]]
        row_id = secrets.token_hex(6)
        rows.append({"id": row_id, "task": task.id, "question": task.description, "required": list(task.required),
                     "stale": list(task.stale_patterns), "answer": record.get("answer"), "human_verdict": None})
        key.append({"id": row_id, "run_id": record.get("run_id")})
    (directory / AUDIT_FILE).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (directory / AUDIT_KEY_FILE).write_text("".join(json.dumps(row) + "\n" for row in key), encoding="utf-8")
    return rows


def audit_path(results: Path) -> Path:
    """The audit sample written beside a stage's results file."""
    stage = next((name for name, file in RESULTS_FILES.items() if file == results.name), None)
    return results.parent / stage / AUDIT_FILE if stage else results.parent / AUDIT_FILE


def _jsonl(path: Path) -> list[tuple[int, dict]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path.name} line {number}: not a JSON object")
            rows.append((number, row))
    return rows


def human_verdicts(audit: Path) -> dict[str, bool | None]:
    """Every row of an audit sample by run id, through the key beside it; a verdict other than true, false or
    null, or a row the key does not name, raises `ValueError`. No file, no rows."""
    if not audit.is_file():
        return {}
    key_path = audit.parent / AUDIT_KEY_FILE
    if not key_path.is_file():
        raise ValueError(f"{audit.name} has no {AUDIT_KEY_FILE} beside it")
    runs = {row.get("id"): row.get("run_id") for _, row in _jsonl(key_path)}
    verdicts: dict[str, bool | None] = {}
    for number, row in _jsonl(audit):
        verdict = row.get("human_verdict")
        if verdict is not None and not isinstance(verdict, bool):
            raise ValueError(f"{audit.name} line {number}: human_verdict must be true, false or null, not {verdict!r}")
        if runs.get(row.get("id")) is None:
            raise ValueError(f"{audit.name} line {number}: row {row.get('id')!r} is not in {AUDIT_KEY_FILE}")
        verdicts[runs[row["id"]]] = verdict
    return verdicts


def apply_verdicts(results: list[dict], verdicts: dict[str, bool | None]) -> tuple[list[dict], list[dict]]:
    """Every run that `needs_human` takes the human's verdict as `correct`; those still without one are
    returned as pending, graded by their regexes meanwhile. An audited run not among `results` raises."""
    known = {record.get("run_id") for record in results}
    strangers = sorted(str(run) for run in verdicts if run not in known)
    if strangers:
        raise ValueError(f"the audit sample names runs these results do not hold: {', '.join(strangers[:5])}")
    applied, pending = [], []
    for record in results:
        if record.get("needs_human"):
            verdict = verdicts.get(record.get("run_id"))
            if verdict is None:
                pending.append(record)
            else:
                record = record | {"correct": verdict, "graded_by": "human"}
        applied.append(record)
    return applied, pending


def _session_record(agent: LiveAgent, done, model: str) -> dict:
    usage = agent.usage(done.stdout)
    prices = PRICES.get(model)
    reported = usage.get("total_cost_usd")
    return {
        "returncode": done.returncode,
        "timed_out": done.returncode == TIMED_OUT,
        "seconds": round(done.seconds, 2),
        "usage": usage,
        "usd": reported if isinstance(reported, (int, float)) else (usd(usage, prices) if prices else None),
    }


def _prepare(run: LiveRun, sandbox: Sandbox, generated, checkout: Path) -> Path | None:
    sessions = generated.store_for(run.task)
    if run.arm == "E":
        sandbox.reset_store(sessions)
        sandbox.setup_engmem("store", "set", str(sandbox.store))
        sandbox.setup_engmem("install", "--agent", run.agent.install_agent, cwd=checkout)
        return sandbox.store
    if run.arm != "N":
        shutil.copytree(sessions, checkout / "notes")
    return None


_FRONT_LINE = r"(?:(?!---[ \t]*$).*\n)*?"
_ACTIVE = re.compile(rf"\A---[ \t]*\n{_FRONT_LINE}status:[ \t]*active[ \t]*\n{_FRONT_LINE}---[ \t]*$", re.MULTILINE)


def _active_documents(store: Path) -> set[str]:
    """The documents whose front matter says `status: active`; a draft is not yet a saved document."""
    sessions = store / "sessions"
    if not sessions.is_dir():
        return set()
    return {path.stem for path in sessions.glob("*.md") if _ACTIVE.match(path.read_text(encoding="utf-8", errors="replace"))}


_FENCE = re.compile(r"^(`{3,}|~{3,})[^\n]*\n(.*?)^\1", re.MULTILINE | re.DOTALL)
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*$", re.MULTILINE | re.DOTALL)


def shows_preview(message: str) -> bool:
    """A document preview: front matter with `id:` and `status:`, at the top of the message or of a fence."""
    for text in (message, *(m.group(2) for m in _FENCE.finditer(message))):
        front = _FRONT_MATTER.match(text.lstrip())
        if front and re.search(r"^id:\s*\S", front.group(1), re.MULTILINE) \
                and re.search(r"^status:\s*\S", front.group(1), re.MULTILINE):
            return True
    return False


def _save(run: LiveRun, sandbox: Sandbox, checkout: Path, session: str) -> dict:
    """The save turn, then, while no document is written and the last message shows a preview, the
    simulated user's reply in the same session, at most `CONFIRM_TURNS` times; each reply is recorded."""
    before = _active_documents(sandbox.store)
    done = sandbox.run(run.agent.save(session, run.model), cwd=checkout, timeout=LIVE_TIMEOUT)
    record = _session_record(run.agent, done, run.model)
    confirmations = []
    stopped = "bound"
    for _ in range(CONFIRM_TURNS):
        if _active_documents(sandbox.store) - before:
            stopped = "document written"
            break
        if not shows_preview(run.agent.final_text(done.stdout)):
            stopped = "no preview"
            break
        session = run.agent.session(done.stdout) or session
        done = sandbox.run(run.agent.reply(session, SIMULATED_USER_REPLY, run.model), cwd=checkout, timeout=LIVE_TIMEOUT)
        confirmations.append(_session_record(run.agent, done, run.model))
    if stopped == "bound" and _active_documents(sandbox.store) - before:
        stopped = "document written"
    return record | {
        "confirmations": confirmations,
        "replies_stopped": stopped,
        "final_message": bool(run.agent.final_text(done.stdout)),
        "new_docs": len(_active_documents(sandbox.store) - before),
    }


def _execute_one(run: LiveRun, root: Path, generated, pass_env: list[str], version: str) -> dict:
    sandbox = Sandbox.create(root, inherit_env=False, pass_env=tuple(pass_env))
    checkout = sandbox.ensure_checkout(run.task.repo, generated.histories.get(run.task.repo, ()))
    store = _prepare(run, sandbox, generated, checkout)
    started = time.perf_counter()
    record = {
        "task": run.task.id,
        "scenario_class": run.task.scenario_class,
        "wording": run.task.wording,
        "agent": run.agent.name,
        "arm": run.arm,
        "repeat": run.repeat,
        "model": run.model,
        "cli_version": version,
        "tools_restricted": run.agent.tools_restricted,
        "store_path_bytes": path_bytes(sandbox.store),
    }
    if run.has_earlier:
        before = memory_files(sandbox, checkout)
        record["earlier"] = _earlier_agent(run, sandbox, checkout)
        carried = carried_memory(sandbox, checkout, before)
        if carried:
            record["earlier"]["memory_carried"] = carried
            record["refused"] = f"the earlier session left agent memory the finder would load: {', '.join(carried)}"
            record["violations"] = [record["refused"]]
            record["seconds"] = round(time.perf_counter() - started, 2)
            return record
    memory_blocks = run.has_earlier or _projects_hold_files(sandbox)
    first = sandbox.run(run.agent.start(prompt_for(run.agent, run.arm, run.task.description), tools_for(run.arm), run.model),
                        cwd=checkout, timeout=LIVE_TIMEOUT)
    record["start"] = _session_record(run.agent, first, run.model)
    record["model_reported"] = run.agent.model(first.stdout)
    calls = run.agent.tool_calls(first.stdout)
    record["tool_calls"] = len(calls)
    record["engmem_calls"] = engmem_calls(calls)
    found = read_violations(calls, sandbox.root, checkout, store)
    if memory_blocks:
        record["violations"] = found
    else:
        record["violations"] = [v for v in found if MEMORY_READ not in v]
        record["memory_reads"] = [v for v in found if MEMORY_READ in v]
    final = run.agent.final_text(first.stdout)
    record["final_message"] = bool(final)
    record |= grade_record(run.task, final)
    session = run.agent.session(first.stdout)
    if run.arm == "E" and session:
        record["save"] = _save(run, sandbox, checkout, session)
    record["seconds"] = round(time.perf_counter() - started, 2)
    return record


MEMORY_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")


def memory_files(sandbox: Sandbox, checkout: Path) -> dict[str, str]:
    """What an agent loads on its own at a session's start, by path under the sandbox and digest: Claude
    Code's project memory, engmem's installed commands and skills, and CLAUDE.md, CLAUDE.local.md or AGENTS.md."""
    claude = sandbox.home / ".claude"
    found = [*claude.glob("projects/*/memory/**/*"), *claude.glob("commands/**/*"), *claude.glob("skills/**/*")]
    for base in (sandbox.home / ".claude", sandbox.home / ".codex", checkout):
        found += [p for name in MEMORY_NAMES for p in base.rglob(name)]
    return {
        p.relative_to(sandbox.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in found if p.is_file()
    }


def _projects_hold_files(sandbox: Sandbox) -> bool:
    """Whether Claude Code's `~/.claude/projects` already holds a file an earlier session left."""
    return any(p.is_file() for p in (sandbox.home / ".claude" / "projects").rglob("*"))


def carried_memory(sandbox: Sandbox, checkout: Path, before: dict[str, str]) -> list[str]:
    """Memory files the earlier session created or changed, which would carry the handoff past engmem."""
    return sorted(path for path, digest in memory_files(sandbox, checkout).items() if before.get(path) != digest)


def _earlier_agent(run: LiveRun, sandbox: Sandbox, checkout: Path) -> dict:
    """The handoff's first session: engmem's start and save, or GN's fixed notes instruction."""
    prompt = prompt_for(run.agent, run.arm, run.task.saver["description"], saver=True)
    first = sandbox.run(run.agent.start(prompt, tools_for(run.arm, saver=True), run.model), cwd=checkout, timeout=LIVE_TIMEOUT)
    record = {"start": _session_record(run.agent, first, run.model)}
    session = run.agent.session(first.stdout)
    if run.arm == "E" and session:
        record["save"] = _save(run, sandbox, checkout, session)
    return record


def excluded(record: dict) -> str | None:
    """Why a run leaves the analysis entirely: only a run that never started, refused ones with their
    reason. Every started run counts toward correctness, one with no final message or a timeout as incorrect."""
    if record.get("refused"):
        return record["refused"]
    return None if isinstance(record.get("start"), dict) else "never started"


def unpriced(record: dict) -> str | None:
    """Why a started run leaves only the cost ratio: it reported no usage to price (a timeout that
    printed nothing, for one)."""
    start = record.get("start") or {}
    if start.get("usage") and isinstance(start.get("usd"), (int, float)):
        return None
    return "timed out with no usage" if start.get("returncode") == TIMED_OUT else "no usage reported"


def analysis_records(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Rows for the decision rules (`usd` None when unpriced, so cost-only exclusion) and the left-out
    runs, each with its reason and whether it left the analysis or only the cost ratio."""
    rows, left_out = [], []
    for r in results:
        family = family_of(r["scenario_class"])
        if family is None:
            continue
        reason = excluded(r)
        if reason is not None:
            left_out.append({"instance": r["task"], "arm": r["arm"], "agent": r.get("agent"), "reason": reason,
                             "scope": "analysis"})
            continue
        cost_reason = unpriced(r)
        if cost_reason is not None:
            left_out.append({"instance": r["task"], "arm": r["arm"], "agent": r.get("agent"), "reason": cost_reason,
                             "scope": "cost"})
        rows.append({
            "family": family,
            "instance": r["task"],
            "arm": r["arm"],
            "correct": bool(r.get("final_message")) and bool(r.get("correct")),
            "usd": None if cost_reason else float(r["start"]["usd"]),
        })
    return rows, left_out


def excluded_per_arm(left_out: list[dict], scope: str) -> dict[str, int]:
    """How many runs each arm lost from `scope`: "analysis" (never started) or "cost" (unpriced)."""
    counts: dict[str, int] = {}
    for row in left_out:
        if row["scope"] == scope:
            counts[row["arm"]] = counts.get(row["arm"], 0) + 1
    return counts
