"""Runs every scenario under every profile, each in a sandbox of its own, and collects the results."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import platform
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agentsim import corpus as corpus_module
from agentsim import metrics, policy
from agentsim.corpus import Corpus, Task
from agentsim.profiles import BY_NAME, DAILY, ENGMEM, GREP, PROFILES, Agent, GrepAgent, Profile, install
from agentsim.sandbox import Sandbox
from agentsim.trace import TOKEN_METHOD, Transcript

ALL_CLASSES = tuple(range(1, 10))
ONE_SCENARIO_PROFILES = ("shell-codex", "shell-copilot-cli")
SMOKE_CLASSES = {
    "mcp-only": ALL_CLASSES,
    "grep": ALL_CLASSES,
    "no-history": ALL_CLASSES,
    "shell-claude": (1, 4, 5, 8, 9),
}
SAVER = "shell-codex"
SAVER_FOR_SAVER = "shell-claude"


@dataclass
class Plan:
    """Which tasks run under which profiles, and how large the stores are."""

    filler: int = corpus_module.DEFAULT_FILLER
    scale_filler: int = corpus_module.SCALE_FILLER
    profiles: tuple[str, ...] = tuple(p.name for p in PROFILES)
    tasks: tuple[str, ...] = tuple(t.id for t in corpus_module.tasks())
    every_profile_everywhere: bool = False
    smoke: bool = False

    def classes_for(self, name: str) -> tuple[int, ...]:
        """Every class, except class 1 only for the extra shell variants and, in `smoke`, as listed."""
        if self.smoke:
            return SMOKE_CLASSES.get(name, (1,))
        if name in ONE_SCENARIO_PROFILES and not self.every_profile_everywhere:
            return (1,)
        return ALL_CLASSES

    def pairs(self, all_tasks: list[Task]) -> list[tuple[Profile, Task]]:
        """Profile-major order, so each profile's sandbox is set up once."""
        chosen = [t for t in all_tasks if t.id in self.tasks]
        return [
            (BY_NAME[name], task)
            for name in self.profiles
            for task in chosen
            if task.scenario_class in self.classes_for(name)
        ]


@dataclass
class _Machine:
    sandbox: Sandbox
    installed: set[str] = field(default_factory=set)
    warm: set[str] = field(default_factory=set)


@dataclass
class _Context:
    out: Path
    corpus: Corpus
    machines: dict[str, _Machine] = field(default_factory=dict)
    saved_stores: dict[str, tuple[Path, dict]] = field(default_factory=dict)

    def machine(self, profile: Profile) -> _Machine:
        if profile.name not in self.machines:
            sandbox = Sandbox.create(self.out / "sandboxes" / profile.name)
            if profile.condition == ENGMEM:
                sandbox.setup_engmem("store", "set", str(sandbox.store))
            self.machines[profile.name] = _Machine(sandbox)
        return self.machines[profile.name]


def _prepare(machine: _Machine, profile: Profile, task: Task, store: Path, kind: str) -> Path:
    sandbox = machine.sandbox
    checkout = sandbox.ensure_checkout(task.repo)
    key = f"{profile.agent}:{checkout if profile.agent == 'copilot-ide' else ''}"
    if profile.condition == ENGMEM and key not in machine.installed:
        install(sandbox, profile, checkout)
        machine.installed.add(key)
    sandbox.reset_store(store)
    if profile.condition == ENGMEM and kind not in machine.warm:
        sandbox.setup_engmem("search", "warm up the section cache", cwd=checkout)
        sandbox.reset_store(store)
        machine.warm.add(kind)
    return checkout


def _saved_store(ctx: _Context, saver_name: str, task: Task) -> tuple[Path, dict]:
    """Class 8: the earlier agent does its task once and saves; its store is snapshot for reuse."""
    if saver_name in ctx.saved_stores:
        return ctx.saved_stores[saver_name]
    saver = BY_NAME[saver_name]
    machine = ctx.machine(saver)
    checkout = _prepare(machine, saver, task, ctx.corpus.store_for(task), task.store)
    transcript = Transcript()
    state = Agent(saver, machine.sandbox, task, checkout, transcript).run(saver=task.saver)
    snapshot = ctx.out / "corpus" / f"saved-by-{saver_name}"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(machine.sandbox.store, snapshot, ignore=shutil.ignore_patterns(".git"))
    summary = {
        "profile": saver_name,
        "doc_id": state.draft_id,
        "saved": state.saved,
        "refusals": state.save_refusals,
        **metrics.cost(transcript),
    }
    ctx.saved_stores[saver_name] = (snapshot, summary)
    return snapshot, summary


def _restore_logs(sandbox: Sandbox, snapshot: Path) -> None:
    """Carries the saver's versions and logs over, as they would sit in a shared store."""
    for entry in snapshot.iterdir():
        if entry.name == "sessions":
            continue
        target = sandbox.store / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def run_one(ctx: _Context, profile: Profile, task: Task) -> dict:
    machine = ctx.machine(profile)
    store = ctx.corpus.store_for(task)
    kind = task.store
    saver_summary = None
    snapshot = None
    if task.saver and profile.condition == ENGMEM:
        saver_name = SAVER_FOR_SAVER if profile.name == SAVER else SAVER
        snapshot, saver_summary = _saved_store(ctx, saver_name, task)
        store = snapshot / "sessions"
        task = dataclasses.replace(task, answer_docs=(saver_summary["doc_id"] or "",))
    elif task.saver:
        task = dataclasses.replace(
            task, answer_docs=(f"{datetime.now(timezone.utc):%Y%m%d}-{task.saver['doc_slug']}",)
        )
    checkout = _prepare(machine, profile, task, store, kind)
    if snapshot is not None:
        _restore_logs(machine.sandbox, snapshot)
    transcript = Transcript()
    state = None
    store_before = _store_state(machine.sandbox.store)
    started = time.perf_counter()
    if profile.condition == ENGMEM:
        state = Agent(profile, machine.sandbox, task, checkout, transcript).run()
    elif profile.condition == GREP:
        GrepAgent(machine.sandbox, task, transcript).run()
    wall = time.perf_counter() - started
    store_written = _store_state(machine.sandbox.store) != store_before if profile.condition == ENGMEM else None
    drafts_left = _drafts_left(machine.sandbox.store, store_before) if profile.condition == ENGMEM else None
    outcome = metrics.outcome(task, profile, transcript, state, store_written)
    session = state.draft_id if state and state.mode != DAILY else None
    observed = None
    if state is not None and (session is not None or snapshot is None):
        observed = metrics.observed_bytes(machine.sandbox.store, session)
    return {
        "task": task.id,
        "scenario_class": task.scenario_class,
        "class_name": task.class_name,
        "variant": task.variant,
        "profile": profile.name,
        "condition": profile.condition,
        "store_docs": sum(1 for _ in store.glob("*.md")),
        "wall_seconds": round(wall, 4),
        "cost": metrics.cost(transcript),
        "outcome": metrics.as_dict(outcome),
        "store_written": store_written,
        "drafts_left": drafts_left,
        "engmem_observed_bytes": observed,
        "template_checks": state.template_checks if state else {},
        "save_note": state.save_note if state else "",
        "saver": saver_summary,
        "steps": [s.as_dict() for s in transcript.steps],
    }


def _store_state(store: Path) -> dict[str, str]:
    """Every file under the store with a digest of its bytes: any write the run made changes it."""
    if not store.is_dir():
        return {}
    return {
        path.relative_to(store).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(store.rglob("*"))
        if path.is_file()
    }


def _drafts_left(store: Path, before: dict[str, str]) -> int:
    """Session documents the run wrote that are still drafts: what a start that never saves leaves."""
    return sum(
        1 for path in (store / "sessions").glob("*.md")
        if f"sessions/{path.name}" not in before
        and re.search(r"^status: draft$", path.read_text(encoding="utf-8"), re.MULTILINE)
    )


def _mean_doc_bytes(sessions: Path) -> int:
    sizes = [path.stat().st_size for path in sessions.glob("*.md")]
    return round(sum(sizes) / len(sizes)) if sizes else 0


def run(out: Path, plan: Plan, progress=None) -> dict:
    """Generates the corpus under `out`, runs the plan, and returns the results document."""
    out.mkdir(parents=True, exist_ok=True)
    corpus = corpus_module.generate(out / "corpus", filler=plan.filler, scale_filler=plan.scale_filler)
    ctx = _Context(out=out, corpus=corpus)
    runs = []
    started = time.perf_counter()
    for profile, task in plan.pairs(corpus.tasks):
        runs.append(run_one(ctx, profile, task))
        if progress is not None:
            progress(runs[-1])
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "token_method": TOKEN_METHOD,
            "seed": corpus_module.SEED,
            "base_store_docs": sum(1 for _ in corpus.stores["base"].glob("*.md")),
            "scale_store_docs": sum(1 for _ in corpus.stores["scale"].glob("*.md")),
            "mean_doc_bytes": _mean_doc_bytes(corpus.stores["base"]),
            "smoke": plan.smoke,
            "grep_top_lines": policy.GREP_TOP_LINES,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "harness_seconds": round(time.perf_counter() - started, 2),
        },
        "tasks": [dataclasses.asdict(t) for t in corpus.tasks if t.id in plan.tasks],
        "runs": runs,
    }


def write_results(results: dict, path: Path) -> None:
    path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8", newline="\n")
