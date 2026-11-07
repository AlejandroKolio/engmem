#!/usr/bin/env python3
"""The end-to-end agent harness (contracts/e2e.md): `run` is Layer 1, `live` is Layer 2 (dry run by default)."""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentsim import corpus, harness, live, report
from agentsim.sandbox import ForbiddenPath, allow, check_allowed

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
    generated = corpus.generate(out, filler=args.filler, scale_filler=args.scale_filler)
    print(f"stores: {', '.join(str(p) for p in generated.stores.values())}")
    print(f"tasks: {out / 'tasks.json'}")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    _allow(args)
    out = check_allowed(Path(args.out) if args.out else _default_out("live"))
    tasks = [t for t in corpus.tasks() if t.id in (_csv(args.tasks) or [t.id for t in corpus.tasks()]) and not t.saver]
    runs = live.plan(tasks, list(_csv(args.agents) or live.AGENTS), list(_csv(args.conditions) or live.CONDITIONS))
    est = live.estimate(
        runs, seconds_per_run=args.seconds_per_run, tokens_per_run=args.tokens_per_run, usd_per_mtok=args.usd_per_mtok
    )
    print(live.dry_run(runs, est, out), end="")
    if not args.execute:
        print("dry run only: pass --execute --i-accept-the-cost --pilot to start the one-scenario pilot")
        return 0
    try:
        results = live.execute(
            runs, out, confirmed=args.accept_cost, pilot=args.pilot, pass_env=list(_csv(args.pass_env) or ())
        )
    except live.LiveRunRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{len(results)} live run(s) written under {out}")
    return 0


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

    generate = commands.add_parser(parents=[allow_out], name="generate", help="write the synthetic stores and tasks.json only")
    generate.add_argument("--out", required=True)
    generate.add_argument("--filler", type=int, default=corpus.DEFAULT_FILLER)
    generate.add_argument("--scale-filler", type=int, default=corpus.SCALE_FILLER)
    generate.set_defaults(func=cmd_generate)

    live_parser = commands.add_parser(parents=[allow_out], name="live", help="Layer 2: real agents; prints the plan unless --execute")
    live_parser.add_argument("--out")
    live_parser.add_argument("--tasks")
    live_parser.add_argument("--agents", help=f"comma-separated, from {', '.join(live.AGENTS)}")
    live_parser.add_argument("--conditions", help=f"comma-separated, from {', '.join(live.CONDITIONS)}")
    live_parser.add_argument("--seconds-per-run", type=int, default=live.DEFAULT_SECONDS_PER_RUN)
    live_parser.add_argument("--tokens-per-run", type=int, default=live.DEFAULT_TOKENS_PER_RUN)
    live_parser.add_argument("--usd-per-mtok", type=float, default=None)
    live_parser.add_argument("--execute", action="store_true", help="start agents (never the default)")
    live_parser.add_argument("--i-accept-the-cost", dest="accept_cost", action="store_true")
    live_parser.add_argument("--pilot", action="store_true", help="run the first planned run only")
    live_parser.add_argument("--pass-env", help="comma-separated variables passed into the sandbox, e.g. an API key")
    live_parser.set_defaults(func=cmd_live)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ForbiddenPath as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
