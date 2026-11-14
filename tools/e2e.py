#!/usr/bin/env python3
"""The end-to-end agent harness (contracts/e2e.md): `run` is Layer 1, `live` is Layer 2 (dry run by default)."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json

from agentsim import corpus, decision, harness, instances, live, phase4, report
from agentsim.padding import DEFAULT_DOC_SIZE, DOC_SIZES
from agentsim.sandbox import ForbiddenPath, PathTooLong, allow, check_allowed
from engmem.runtime import force_utf8_streams

REPO_ROOT = Path(__file__).resolve().parent.parent
BYTES_PER_LARGE_DOC = 8_000
DISK_HEADROOM = 2 * 1024**3


def _default_out(kind: str) -> Path:
    return REPO_ROOT / ".tmp" / "e2e" / f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"


def _csv(value: str | None) -> tuple[str, ...] | None:
    return tuple(part for part in value.split(",") if part) if value else None


def _allow(args: argparse.Namespace) -> None:
    for path in args.allow_out or ():
        allow(Path(path))


def check_disk(path: Path, documents: int, copies: int) -> None:
    """Refuses a store that, with its sandbox copies, would eat into the disk's last gigabytes."""
    need = documents * BYTES_PER_LARGE_DOC * copies
    probe = path if path.exists() else next(p for p in path.parents if p.exists())
    free = shutil.disk_usage(probe).free
    if free - need < DISK_HEADROOM:
        raise SystemExit(
            f"error: {free // 1024**2} MB free at {probe}; this run needs about {need // 1024**2} MB "
            f"and keeps {DISK_HEADROOM // 1024**3} GB free"
        )


def cmd_run(args: argparse.Namespace) -> int:
    _allow(args)
    out = check_allowed(Path(args.out) if args.out else _default_out("run"))
    scale = corpus.LARGE_FILLER if args.large else args.scale_filler
    tasks = _csv(args.tasks) or (("scale",) if args.large else tuple(t.id for t in corpus.tasks()))
    profiles = _csv(args.profiles) or harness.Plan().profiles
    check_disk(out, scale, len(profiles) + 2)
    plan = harness.Plan(
        filler=args.filler,
        scale_filler=scale,
        profiles=profiles,
        tasks=tasks,
        every_profile_everywhere=args.every_profile,
        smoke=args.smoke,
    )
    results = harness.run(out, plan, progress=lambda r: print(
        f"{r['task']:>12} {r['profile']:<18} correct={r['outcome']['correct']!s:<5} "
        f"calls={r['cost']['calls']:<3} tokens={r['cost']['total_tokens']}", flush=True))
    harness.write_results(results, out / "results.json")
    (out / "report.md").write_text(report.render(results), encoding="utf-8", newline="\n")
    if not args.keep_sandboxes:
        shutil.rmtree(out / "sandboxes", ignore_errors=True)
        shutil.rmtree(out / "corpus", ignore_errors=True)
    print(f"report: {out / 'report.md'}")
    print(f"results: {out / 'results.json'}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    _allow(args)
    out = check_allowed(Path(args.out))
    check_disk(out, args.scale_filler, 1)
    generated = corpus.generate(out, filler=args.filler, scale_filler=args.scale_filler, doc_size=args.doc_size)
    print(f"stores: {', '.join(str(p) for p in generated.stores.values())}")
    print(f"tasks: {out / 'tasks.json'}")
    print(f"facts: {out / 'facts.json'}")
    return 0


def cmd_facts(args: argparse.Namespace) -> int:
    """Only the facts file a blind author works from, beside nothing else of the corpus."""
    _allow(args)
    out = check_allowed(Path(args.out))
    from agentsim import factset

    factset.write_facts(out / "facts.json", corpus.all_facts(), args.doc_size)
    print(f"facts: {out / 'facts.json'}")
    return 0


def cmd_phase4(args: argparse.Namespace) -> int:
    _allow(args)
    out = check_allowed(Path(args.out) if args.out else _default_out("phase4"))
    sizes = tuple(args.doc_size or (DEFAULT_DOC_SIZE,))
    check_disk(out, corpus.SCALE_FILLER * len(sizes), len(phase4.PROFILES) + 2)
    try:
        document = phase4.run(out, Path(args.instances), sizes, smoke=args.smoke, progress=lambda r: print(
            f"{r['task']:>16} {r['profile']:<14} correct={r['outcome']['correct']!s:<5} "
            f"tokens={r['cost']['to_answer_tokens']}", flush=True))
    except phase4.InstancesRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    (out / "phase4-results.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    (out / "phase4-report.md").write_text(phase4.render(document), encoding="utf-8", newline="\n")
    if not args.keep_sandboxes:
        shutil.rmtree(out / "check", ignore_errors=True)
        for size in sizes:
            for name in ("sandboxes", "corpus"):
                shutil.rmtree(out / size / name, ignore_errors=True)
    print(f"report: {out / 'phase4-report.md'}")
    return 0


def _live_tasks(path: Path) -> tuple[list, bool]:
    instance_set, facts = phase4.load(path)
    with tempfile.TemporaryDirectory(prefix="engmem-e2e-check-") as root:
        phase4.check_against_stores(instance_set, facts, Path(root))
    handoff = [t for t in corpus.tasks() if t.saver]
    return instances.tasks_of(instance_set, facts) + handoff, instance_set.provisional


def cmd_live(args: argparse.Namespace) -> int:
    _allow(args)
    out = check_allowed(Path(args.out) if args.out else live.default_out(f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"))
    try:
        tasks, provisional = _live_tasks(Path(args.instances))
    except phase4.InstancesRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    agents = list(_csv(args.agents) or ("claude",))
    stage = "pilot" if args.pilot else args.stage
    print(live.dry_run(tasks, agents, args.model, out, stage=stage, provisional=provisional), end="")
    if not args.execute:
        print("dry run only: pass --execute --i-accept-the-cost --pilot to start the pilot")
        return 0
    chosen, repeats = live.stage_tasks(tasks, stage)
    runs = live.plan(chosen, agents, repeats=repeats, model=args.model)
    try:
        results = live.execute(
            runs, out, confirmed=args.accept_cost, stage=stage,
            pass_env=list(_csv(args.pass_env) or ()), doc_size=args.doc_size, provisional=provisional,
        )
    except live.LiveRunRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{len(results)} live run(s) written under {out}")
    return 0


PENDING_EXIT = 3


def cmd_analyse(args: argparse.Namespace) -> int:
    path = Path(args.results)
    audit = Path(args.audit) if args.audit else live.audit_path(path)
    try:
        results, pending = live.apply_verdicts(json.loads(path.read_text(encoding="utf-8")), live.human_verdicts(audit))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if pending:
        print(f"{len(pending)} run(s) naming both the current and the stale value await a human_verdict in {audit}.")
        if not args.provisional:
            print("No decision is shown until every one has a verdict; --provisional shows one graded by the regexes.")
            return PENDING_EXIT
        print("PROVISIONAL: they count by their regex grade until a verdict is filled in.")
    by_agent: dict[str, list] = {}
    for record in results:
        by_agent.setdefault(record["agent"], []).append(record)
    verdicts = {}
    for agent, records in by_agent.items():
        rows, left_out = live.analysis_records(records)
        for scope, label in (("analysis", "never started, left out"), ("cost", "unpriced, left out of the cost ratio only")):
            counts = live.excluded_per_arm(left_out, scope)
            print(f"runs {label}, per arm: " + (", ".join(f"{arm}: {n}" for arm, n in sorted(counts.items())) or "none"))
        e_vs_plus = decision.analyse(rows, "E", "G+")
        plus_vs_g = decision.analyse(rows, "G+", "G")
        verdicts[agent] = e_vs_plus
        heading = f"## {agent}"
        if pending:
            heading = f"PROVISIONAL: {len(pending)} run(s) in all await a human verdict\n{heading}"
        print(f"{heading}\n")
        print(decision.render(e_vs_plus, plus_vs_g))
    claims = decision.general_claims(verdicts)
    print(("PROVISIONAL " if pending else "") + "General claims (same direction in every agent): "
          + (", ".join(f"{k}: {v}" for k, v in claims.items()) or "none"))
    return PENDING_EXIT if pending else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="e2e", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    allow_out = argparse.ArgumentParser(add_help=False)
    allow_out.add_argument(
        "--allow-out", action="append", metavar="PATH",
        help="also allow this directory for --out and the sandboxes (default: .tmp/ and the system temp dir)",
    )

    run = commands.add_parser(parents=[allow_out], name="run", help="Layer 1: scripted agents against a generated store")
    run.add_argument("--out", help="results directory (default: .tmp/e2e/run-<time>)")
    run.add_argument("--filler", type=int, default=corpus.DEFAULT_FILLER, help="generated documents in the base store")
    run.add_argument("--scale-filler", type=int, default=corpus.SCALE_FILLER, help="generated documents in the scale store")
    run.add_argument("--large", action="store_true", help=f"the large tier: {corpus.LARGE_FILLER} documents, scale task only")
    run.add_argument("--tasks", help="comma-separated task ids")
    run.add_argument("--profiles", help="comma-separated profile names")
    run.add_argument("--every-profile", action="store_true", help="run every shell variant on every class")
    run.add_argument("--smoke", action="store_true", help="the reduced plan the default test suite runs")
    run.add_argument("--keep-sandboxes", action="store_true", help="keep the stores and sandboxes after the run")
    run.set_defaults(func=cmd_run)

    generate = commands.add_parser(parents=[allow_out], name="generate", help="write the synthetic stores, tasks.json and facts.json")
    generate.add_argument("--out", required=True)
    generate.add_argument("--filler", type=int, default=corpus.DEFAULT_FILLER)
    generate.add_argument("--scale-filler", type=int, default=corpus.SCALE_FILLER)
    generate.add_argument("--doc-size", choices=tuple(DOC_SIZES), default=DEFAULT_DOC_SIZE)
    generate.set_defaults(func=cmd_generate)

    facts = commands.add_parser(parents=[allow_out], name="facts", help="write facts.json, the blind author's only input")
    facts.add_argument("--out", required=True)
    facts.add_argument("--doc-size", choices=tuple(DOC_SIZES), default=DEFAULT_DOC_SIZE)
    facts.set_defaults(func=cmd_facts)

    p4 = commands.add_parser(parents=[allow_out], name="phase4", help="Layer 1 on authored instances, every arm")
    p4.add_argument("--out", help="results directory (default: .tmp/e2e/phase4-<time>)")
    p4.add_argument("--instances", default=str(instances.default_file()), help="the task-instance file (default: the blind set)")
    p4.add_argument("--doc-size", action="append", choices=tuple(DOC_SIZES),
                    help=f"repeatable; default {DEFAULT_DOC_SIZE}; two or more report the crossover")
    p4.add_argument("--smoke", action="store_true", help="the reduced plan the default test suite runs")
    p4.add_argument("--keep-sandboxes", action="store_true")
    p4.set_defaults(func=cmd_phase4)

    live_parser = commands.add_parser(parents=[allow_out], name="live", help="Layer 2: real agents; prints the plan unless --execute")
    live_parser.add_argument("--out")
    live_parser.add_argument("--instances", default=str(instances.default_file()), help="default: the blind set")
    live_parser.add_argument("--agents", help=f"comma-separated, from {', '.join(live.AGENTS)} (default claude)")
    live_parser.add_argument("--model", default=live.DEFAULT_MODEL, help="pinned for every session and recorded")
    live_parser.add_argument("--stage", choices=live.STAGES, default="pilot")
    live_parser.add_argument("--doc-size", choices=tuple(DOC_SIZES), default=DEFAULT_DOC_SIZE)
    live_parser.add_argument("--execute", action="store_true", help="start agents (never the default)")
    live_parser.add_argument("--i-accept-the-cost", dest="accept_cost", action="store_true")
    live_parser.add_argument("--pilot", action="store_true", help="the pilot stage: the first task, once per arm")
    live_parser.add_argument("--pass-env", help="comma-separated variables passed into the sandbox, e.g. an API key")
    live_parser.set_defaults(func=cmd_live)

    analyse = commands.add_parser(
        name="analyse",
        help=f"the pre-registered decision rules over live results; exits {PENDING_EXIT} while a run awaits a human verdict",
    )
    analyse.add_argument("--results", required=True)
    analyse.add_argument(
        "--provisional", action="store_true",
        help="while runs await a human verdict, still show a decision graded by the regexes (marked PROVISIONAL)",
    )
    analyse.add_argument("--audit", help="the audit sample with human verdicts (default: the stage's audit-sample.jsonl)")
    analyse.set_defaults(func=cmd_analyse)
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ForbiddenPath, PathTooLong) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
