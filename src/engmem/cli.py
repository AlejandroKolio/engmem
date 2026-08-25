"""`engmem` CLI entry point: argument parsing and the `search`/`roles`/`mcp`/
`telemetry`/`backfill`/`install`/`uninstall` subcommands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from engmem import __version__
from engmem.backfill import BackfillWriteError, apply_backfill, propose_backfill
from engmem.install import VALID_AGENTS, cmd_install, cmd_uninstall
from engmem.output import (
    render_backfill_proposal,
    render_no_match,
    render_role_search_results,
    render_scoreboard,
    render_search_results,
    render_telemetry_summary,
    select_role_hits,
)
from engmem.scoring import role_coverage, search as run_search, search_with_role_sections
from engmem.sections import CANONICAL_ROLES
from engmem.spine import LoadResult, load_store, stray_documents
from engmem.telemetry import log_role_search, log_search
from engmem.telemetry import summarize as summarize_telemetry
from engmem.runtime import fail, resolve_store


def _session_id(raw: str | None) -> str | None:
    # blank/whitespace == not passed, so the log can tell "unattributed" from
    # "session unknown"; no other validation — the CLI doesn't know which ids exist
    stripped = (raw or "").strip()
    return stripped or None


def _load_sessions(store: Path) -> tuple[LoadResult | None, int]:
    """Resolves `sessions/`, reports "store not found" (exit 2) if missing,
    else loads it and prints every error/warning to stderr. An unreadable
    `sessions/` itself also gets an stdout line — "N failed to load" alone
    would misread as one bad file rather than an unknown true count. Returns
    `(result, 0)` or `(None, 2)`, always a 2-tuple."""
    sessions_dir = store / "sessions"
    if not sessions_dir.is_dir():
        fail(
            f"store not found: {sessions_dir} does not exist "
            f"(run `engmem install` to create the store, or check --store/ENGMEM_HOME)"
        )
        return None, 2
    result = load_store(sessions_dir)
    for problem in result.errors:
        print(f"error: {problem.message}", file=sys.stderr)
    for problem in result.warnings:
        print(f"warning: {problem.message}", file=sys.stderr)
    if result.scan_error is not None:
        print(f"error: {result.scan_error.message}")
    return result, 0


def _cmd_search(args: argparse.Namespace) -> int:
    # cheapest usage-error check first: a typo'd --role needs no store to be wrong
    if args.role is not None and args.role not in CANONICAL_ROLES:
        fail(
            f"engmem search: unknown role {args.role!r} — valid roles: "
            f"{', '.join(CANONICAL_ROLES)} (see `engmem roles` for which of these "
            f"the store actually has documents for)"
        )
        return 2

    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure

    strays, stray_scan_errors = stray_documents(store)
    if strays:
        names = ", ".join(p.name for p in strays[:3])
        if len(strays) > 3:
            names += f", +{len(strays) - 3} more"
        print(
            f"warning: {len(strays)} markdown file(s) are never searched — engmem reads "
            f"only {store / 'sessions'}/*.md, not the store root and not subdirectories "
            f"({names}). Move them directly into sessions/ to make them findable.",
            file=sys.stderr,
        )
    for scan_error in stray_scan_errors:
        # "found nothing" and "could not check" must never look the same on stdout
        print(f"warning: {scan_error}", file=sys.stderr)
        print(f"warning: {scan_error}")

    if args.role is not None:
        role_outcome, role_map = search_with_role_sections(result.docs, args.query)
        role_hits, _matched_role_total = select_role_hits(role_outcome, role_map, args.role)
        # role_result_text is exactly what the reader is shown, and what
        # context_bytes below measures — the stray-files note is printed
        # separately and deliberately excluded (store housekeeping, not content)
        role_result_text = render_role_search_results(role_outcome, role_map, args.role)
        print(role_result_text)
        if not role_hits and strays:
            print(
                f"({len(strays)} markdown file(s) sit outside the searched set — only "
                f"sessions/*.md is read, not the store root and not subdirectories. "
                f"They may hold the answer.)"
            )

        print(render_scoreboard(result.docs, failed=len(result.errors)))

        telemetry_error = log_role_search(
            store / "telemetry.jsonl",
            query=args.query,
            role=args.role,
            n_docs=len(result.docs),
            role_hits=role_hits,
            session_id=_session_id(args.session),
            channel="cli",
            context_bytes=len(role_result_text.encode("utf-8")),
        )
        if telemetry_error is not None:
            print(f"note: telemetry not recorded ({telemetry_error})")

        return 0

    outcome = run_search(result.docs, args.query)

    # result_text is exactly what the reader is shown, and what context_bytes
    # below measures — the stray-files note stays a separate print (same
    # reasoning as the --role branch above)
    if outcome.hits or outcome.superseded_notes:
        result_text = render_search_results(outcome, result.docs)
    else:
        result_text = render_no_match()
    print(result_text)
    if not (outcome.hits or outcome.superseded_notes) and strays:
        print(
            f"({len(strays)} markdown file(s) sit outside the searched set — only "
            f"sessions/*.md is read, not the store root and not subdirectories. "
            f"They may hold the answer.)"
        )

    print(render_scoreboard(result.docs, failed=len(result.errors)))

    telemetry_error = log_search(
        store / "telemetry.jsonl",
        query=args.query,
        n_docs=len(result.docs),
        outcome=outcome,
        session_id=_session_id(args.session),
        channel="cli",
        context_bytes=len(result_text.encode("utf-8")),
    )
    if telemetry_error is not None:
        # search already succeeded; report the telemetry gap on stdout rather
        # than crash (discarding good results) or stay silent (agent misses it)
        print(f"note: telemetry not recorded ({telemetry_error})")

    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    # stdout here is MCP JSON-RPC only — fail() (both streams) must never be
    # called from this function. Lazy import keeps the rest of the CLI working
    # if mcp_server has an unrelated problem.
    store = resolve_store(args.store)
    from engmem.mcp_server import serve

    return serve(store)


def _cmd_roles(args: argparse.Namespace) -> int:
    """Discoverability for `search --role`: the full canonical vocabulary
    plus how many documents in this store currently carry each role."""
    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure

    coverage = role_coverage(result.docs)
    print(
        f"engmem roles: {len(CANONICAL_ROLES)} known role(s) — pass one to "
        f'`engmem search "<query>" --role <role>`'
    )
    name_width = max(len(role) for role in CANONICAL_ROLES)
    for role in CANONICAL_ROLES:
        count = coverage.get(role, 0)
        note = "  (no document in this store has this section yet)" if count == 0 else ""
        print(f"  {role:<{name_width}}  {count} document(s){note}")

    print(render_scoreboard(result.docs, failed=len(result.errors)))
    return 0


def _cmd_telemetry(args: argparse.Namespace) -> int:
    """Reading surface for `telemetry.jsonl`: totals, hit rate, and context
    spent, by channel and overall. A terminal-only subcommand, not an MCP
    tool — an agent has no requirement that needs read access to this log."""
    store = resolve_store(args.store)
    summary = summarize_telemetry(store / "telemetry.jsonl")
    sessions = store / "sessions"
    docs = load_store(sessions).docs if sessions.is_dir() else []
    misses = sum(len(d.navigation_miss) for d in docs)
    print(render_telemetry_summary(summary, misses))
    return 0


def _backfill_targets(args: argparse.Namespace, docs: list) -> tuple[list, int]:
    """`(targets, 0)`, or `([], 2)` on a usage failure already reported via
    `fail` — always a 2-tuple so a caller can unconditionally unpack it."""
    if args.id is not None:
        target = next((d for d in docs if d.id == args.id), None)
        if target is None:
            fail(f"engmem backfill: no document with id {args.id!r} in the store")
            return [], 2
        return [target], 0
    return [d for d in docs if not d.spine_complete], 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure

    targets, failure = _backfill_targets(args, result.docs)
    if failure:
        return failure

    if not targets:
        # say so explicitly rather than printing nothing, indistinguishable
        # from a crash or an empty store
        print(
            f"engmem backfill: {len(result.docs)} document(s) in the store — "
            f"all already have a complete spine; nothing to backfill"
        )
        return 0

    proposals = [propose_backfill(d) for d in targets]
    for proposal in proposals:
        print(render_backfill_proposal(proposal))

    actionable = [
        (doc, proposal)
        for doc, proposal in zip(targets, proposals)
        if not proposal.already_complete and proposal.fields
    ]
    if not actionable:
        print("engmem backfill: nothing to write")
        return 0

    if args.dry_run:
        print(
            f"(dry run — {len(actionable)} document(s) shown above would be "
            f"written; nothing written)"
        )
        return 0

    if args.yes:
        print("(--yes: skipping confirmation)")
    else:
        # the one write path here that talks to a human, not an agent reading
        # stdout, so an interactive prompt is the right shape
        try:
            answer = input(
                f"Write the {len(actionable)} proposal(s) shown above? [y/N]: "
            )
        except EOFError:
            answer = ""
        if answer.strip().casefold() not in ("y", "yes"):
            print("engmem backfill: cancelled — no changes written")
            return 0

    written = failed = 0
    for doc, proposal in actionable:
        try:
            message = apply_backfill(doc, proposal)
        except (OSError, ValueError, yaml.YAMLError, BackfillWriteError) as exc:
            fail(f"engmem backfill: {doc.id}: could not write ({exc})")
            failed += 1
            continue
        print(f"{doc.id}: {message}")
        written += 1

    print(f"engmem backfill: {written} document(s) written, {failed} failed")
    return 2 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engmem")
    # argparse's own action, unusually, is the right one here: it prints to stdout and
    # exits 0, so asking the version is an answer rather than a usage error.
    parser.add_argument(
        "--version", action="version", version=f"engmem {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="search prior engineering sessions")
    search_parser.add_argument("query")
    search_parser.add_argument(
        "--session",
        default=None,
        help=(
            "id of the draft document this search belongs to — recorded in "
            "telemetry.jsonl so a retrieval can be tied to the document that reused it"
        ),
    )
    search_parser.add_argument(
        "--role",
        default=None,
        # not argparse `choices=` (exits via parser.error() — stderr only, no
        # stdout line); _cmd_search validates and calls fail() instead
        help=(
            "restrict output to a specific section ROLE (e.g. decisions, lessons, "
            "production) from each of the top-matching documents, instead of each "
            "document's single best word-matching section. Documents still rank by "
            "the query's words; a document lacking the requested role is skipped, "
            "never padded with the wrong section. Run `engmem roles` to see the "
            "full vocabulary and which roles the store currently has documents for."
        ),
    )
    search_parser.add_argument("--store", default=None)
    search_parser.set_defaults(func=_cmd_search)

    roles_parser = subparsers.add_parser(
        "roles",
        help=(
            "list the section roles a `search --role` can address, and how many "
            "documents in the store carry each one"
        ),
    )
    roles_parser.add_argument("--store", default=None)
    roles_parser.set_defaults(func=_cmd_roles)

    install_parser = subparsers.add_parser(
        "install", help="set up the store and agent prompt templates"
    )
    install_parser.add_argument(
        "--agent",
        default="claude",
        # not argparse `choices=` — see `_validate_agent`
        help=f"one of {', '.join(VALID_AGENTS)} (default: claude)",
    )
    install_parser.add_argument("--local", action="store_true")
    install_parser.add_argument("--store", default=None)
    install_parser.set_defaults(func=cmd_install)

    uninstall_parser = subparsers.add_parser(
        "uninstall", help="remove the agent templates and trigger rule (keeps the store)"
    )
    uninstall_parser.add_argument(
        "--agent",
        default="claude",
        # not argparse `choices=` — see `_validate_agent`
        help=f"one of {', '.join(VALID_AGENTS)} (default: claude)",
    )
    uninstall_parser.add_argument("--local", action="store_true")
    uninstall_parser.add_argument("--store", default=None)
    uninstall_parser.set_defaults(func=cmd_uninstall)

    mcp_parser = subparsers.add_parser(
        "mcp", help="run the MCP stdio server over stdin/stdout, for clients that cannot run shell commands"
    )
    mcp_parser.add_argument("--store", default=None)
    mcp_parser.set_defaults(func=_cmd_mcp)

    telemetry_parser = subparsers.add_parser(
        "telemetry",
        help=(
            "show accumulated search telemetry — totals, hit rate, and context "
            "spent, split by channel (cli/mcp) and overall"
        ),
    )
    telemetry_parser.add_argument("--store", default=None)
    telemetry_parser.set_defaults(func=_cmd_telemetry)

    backfill_parser = subparsers.add_parser(
        "backfill",
        help=(
            "propose front matter for documents written before engmem existed, "
            "derived from their own headings and preamble; shows the proposal and "
            "writes nothing until you agree to it"
        ),
    )
    backfill_target = backfill_parser.add_mutually_exclusive_group(required=True)
    backfill_target.add_argument(
        "--id", default=None, help="back-fill exactly one document, by id"
    )
    backfill_target.add_argument(
        "--all",
        action="store_true",
        help="back-fill every document in the store with an incomplete spine",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the proposal(s) and write nothing — the preview half of the confirmation",
    )
    backfill_parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "skip the interactive confirmation and write immediately — only "
            "sensible after reviewing the proposal with --dry-run first"
        ),
    )
    backfill_parser.add_argument("--store", default=None)
    backfill_parser.set_defaults(func=_cmd_backfill)

    return parser


def _force_utf8_streams() -> None:
    """Locators carry `§`, which cp437 and cp866 cannot encode — left to the console's
    code page, `engmem search` dies with UnicodeEncodeError instead of printing."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
