"""`engmem` CLI entry point: argument parsing and the subcommands."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Container
from pathlib import Path
from typing import NoReturn

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
from engmem.runtime import fail, resolve_store
from engmem.scoring import role_coverage, search as run_search, search_with_role_sections
from engmem.sections import CANONICAL_ROLES
from engmem.spine import Doc, LoadResult, load_store, sessions_dir_unreadable, stray_documents
from engmem.telemetry import (
    log_role_search,
    log_search,
    summarize as summarize_telemetry,
)


def _session_id(raw: str | None) -> str | None:
    # blank/whitespace == not passed, so the log can tell "unattributed" from
    # "session unknown"; no other validation — the CLI doesn't know which ids exist
    stripped = (raw or "").strip()
    return stripped or None


def _load_sessions(store: Path) -> tuple[LoadResult | None, int]:
    """Loads `sessions/`, reporting every error to stderr; returns `(result, 0)` or `(None, 2)`."""
    sessions_dir = store / "sessions"
    unreadable = sessions_dir_unreadable(sessions_dir)
    if unreadable:
        fail(unreadable)
        return None, 2
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


def _report_strays(store: Path) -> list[Path]:
    """Warns on stderr about markdown engmem never reads; returns the strays for `_stray_note`."""
    strays, scan_errors = stray_documents(store)
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
    for scan_error in scan_errors:
        # "found nothing" and "could not check" must never look the same on stdout
        print(f"warning: {scan_error}", file=sys.stderr)
        print(f"warning: {scan_error}")
    return strays


def _stray_note(count: int) -> str:
    """The stdout half of the stray report — one wording for both search branches, so the two
    cannot drift into describing the same store differently."""
    return (
        f"({count} markdown file(s) sit outside the searched set — only "
        f"sessions/*.md is read, not the store root and not subdirectories. "
        f"They may hold the answer.)"
    )


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

    strays = _report_strays(store)

    if args.role is not None:
        role_outcome, role_map = search_with_role_sections(result.docs, args.query)
        role_hits, _matched_role_total = select_role_hits(role_outcome, role_map, args.role)
        # role_result_text is exactly what the reader is shown, and what
        # context_bytes below measures — the stray-files note is printed
        # separately and deliberately excluded (store housekeeping, not content)
        role_result_text = render_role_search_results(role_outcome, role_map, args.role)
        print(role_result_text)
        if not role_hits and strays:
            print(_stray_note(len(strays)))

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
    surfaced_anything = bool(outcome.hits or outcome.superseded_notes)

    # result_text is exactly what the reader is shown, and what context_bytes below measures — the
    # stray-files note stays a separate print (same reasoning as the --role branch above)
    result_text = (
        render_search_results(outcome, result.docs) if surfaced_anything else render_no_match()
    )
    print(result_text)
    if not surfaced_anything and strays:
        print(_stray_note(len(strays)))

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
    # stdout here is MCP JSON-RPC only — fail() (both streams) must never be called from this
    # function.
    store = resolve_store(args.store)
    from engmem.mcp_server import serve

    return serve(store)


def _cmd_roles(args: argparse.Namespace) -> int:
    """The canonical role vocabulary and how many documents in this store carry each."""
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
    """Reading surface for `telemetry.jsonl`: totals, hit rate and context spent."""
    store = resolve_store(args.store)
    telemetry_path = store / "telemetry.jsonl"
    try:
        summary = summarize_telemetry(telemetry_path)
    except (OSError, UnicodeDecodeError) as exc:
        # a log that cannot be read *or decoded* is not a log with nothing in it: `summarize`
        # reads a *missing* file as zero rows, so letting either through would report
        # "0 row(s)" for a store full of searches — the phantom-empty failure `scan_error`
        # prevents. UnicodeDecodeError is not redundant: a non-UTF-8 byte raises it, and it is
        # a ValueError and not an OSError, so it walked straight past `except OSError`.
        # Deliberately not the whole of ValueError: a bad *byte* invalidates every offset in
        # the file, but a bad *row* does not — `summarize` tallies that one as `unreadable`
        # and keeps going, so a single truncated append cannot cost the whole history
        fail(f"engmem telemetry: cannot read {telemetry_path} ({exc})")
        return 2
    # through `_load_sessions` like every other subcommand: reading the store directly meant
    # an unlistable `sessions/` rendered byte-identically to a store with no misses at all,
    # which is the failure `scan_error` exists to prevent
    result, failure = _load_sessions(store)
    if failure:
        return failure
    misses = sum(len(d.navigation_miss) for d in result.docs)
    print(render_telemetry_summary(summary, misses))
    return 0


def _backfill_targets(args: argparse.Namespace, docs: list[Doc]) -> tuple[list[Doc], int]:
    """`(targets, 0)`, or `([], 2)` on a usage failure already reported via `fail`."""
    if args.id is not None:
        target = next((d for d in docs if d.id == args.id), None)
        if target is None:
            fail(f"engmem backfill: no document with id {args.id!r} in the store")
            return [], 2
        return [target], 0
    return [d for d in docs if not d.spine_complete], 0


def _sessions_is_contained(store: Path) -> bool:
    """`sessions/` must be the store's own directory, not a link out of it — the same check
    `mcp_server._resolve_sessions_dir` makes before it writes."""
    try:
        own = store.resolve(strict=False) / "sessions"
        return (store / "sessions").resolve(strict=True) == own
    except OSError:
        return True  # unreadable or absent: `_load_sessions` reports it in its own words


def _cmd_backfill(args: argparse.Namespace) -> int:
    store = resolve_store(args.store)
    if not _sessions_is_contained(store):
        fail(
            f"engmem backfill: {store / 'sessions'} does not resolve to "
            f"{store.resolve(strict=False) / 'sessions'} — sessions/ appears to be a symlink "
            "(or the store path contains one); refusing to write"
        )
        return 2

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

    # a target can vanish or become unreadable between `load_store` and its own proposal;
    # that is one document's failure, not the batch's
    pairs = []
    proposal_failures = 0
    for doc in targets:
        try:
            pairs.append((doc, propose_backfill(doc)))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            fail(f"engmem backfill: {doc.id}: could not read ({exc})")
            proposal_failures += 1
    for _doc, proposal in pairs:
        print(render_backfill_proposal(proposal))

    actionable = [
        (doc, proposal)
        for doc, proposal in pairs
        if not proposal.already_complete and proposal.fields
    ]
    if not actionable:
        tally = f" ({proposal_failures} could not be read)" if proposal_failures else ""
        print(f"engmem backfill: nothing to write{tally}")
        return 2 if proposal_failures else 0

    if args.dry_run:
        # not "shown above": a document can be listed with only a note and no field to
        # write, so the count of what would be written is a subset of what was printed
        print(
            f"(dry run — {len(actionable)} document(s) with fields to write; "
            f"nothing written)"
        )
        # the preview is what a `--dry-run && --yes` script gates on, so a target it could
        # not even read has to cost it the exit code, exactly as it does on the write path
        return 2 if proposal_failures else 0

    if args.yes:
        print("(--yes: skipping confirmation)")
    else:
        # the one write path here that talks to a human, not an agent reading
        # stdout, so an interactive prompt is the right shape
        try:
            answer = input(
                f"Write the {len(actionable)} document(s) with fields to write? [y/N]: "
            )
        except EOFError:
            # a closed stdin (`--all` behind a pipe, a cron job with no terminal) cannot
            # answer, and an unanswered [y/N] is a "no" — not an error
            answer = ""
        except KeyboardInterrupt:
            # Ctrl-C is not a decline: `engmem backfill --all && deploy.sh` must not run
            # deploy.sh because the user pressed Ctrl-C. Nothing was written either way, and
            # a traceback here would read as a crash mid-write, which did not happen. Named
            # on both streams like every other non-zero exit here — the human who pressed
            # Ctrl-C may have redirected stdout to a file
            fail("engmem backfill: interrupted — no changes written")
            return 130
        if answer.strip().casefold() not in ("y", "yes"):
            print("engmem backfill: cancelled — no changes written")
            # the read failures happened before the prompt; they are not what was declined
            return 2 if proposal_failures else 0

    written = 0
    failed = proposal_failures
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


_STORE_HELP = "path to the engmem store (default: $ENGMEM_HOME, else ~/Developer/engmem)"


class _Parser(argparse.ArgumentParser):
    """argparse writes a usage error to stderr only, and the consuming agent reads stdout
    (ENGMEM-SPEC.md §10 VIII) — so a mistyped command left it nothing at all."""

    # `engmem mcp`'s stdout is the JSON-RPC channel and nothing else (§5): that one
    # subparser turns the mirror off rather than emit a line no client can parse
    mirror_errors_to_stdout = True

    @property
    def value_taking_options(self) -> set[str]:
        """Which options consume a following token, so `_is_mcp_invocation` can tell an option's
        value from a subcommand name without a second list of this CLI's own grammar."""
        # read off the actions the parser OWNS, not intercepted at `add_argument`: an argument
        # group's `add_argument` resolves to `argparse._ActionsContainer`'s, never this class's,
        # so `--id` (declared in backfill's mutually exclusive group) went unrecorded
        return {
            option
            for action in self._actions
            if action.nargs != 0
            for option in action.option_strings
        }

    def error(self, message: str) -> NoReturn:
        if self.mirror_errors_to_stdout:
            print(f"error: {self.prog}: {message}")
        super().error(message)  # usage on stderr, exit 2 — argparse's own, unchanged


def _add_store_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", default=None, help=_STORE_HELP)


def _is_mcp_invocation(
    argv: list[str], commands: Container[str], value_options: Container[str]
) -> bool:
    """`mcp` is the first token that names a subcommand and is not some option's value — still
    what `engmem --store PATH mcp` means when the parse fails before dispatch."""
    skip_value = False
    for token in argv:
        if skip_value:
            # the value of the option before it, whatever it spells: `--store search` names a
            # directory called `search`, not the subcommand
            skip_value = False
            continue
        if token.startswith("-") and token != "-":
            # `--store PATH` takes the next token; `--store=PATH` carries its own value
            skip_value = "=" not in token and token in value_options
            continue
        if token in commands:
            return token == "mcp"
    return False


def build_parser(argv: list[str] | None = None) -> _Parser:
    """The parser for this `argv`; without one, the root parser mirrors usage errors to stdout
    (`argv` is what tells `engmem mcp` apart, whose stdout is the protocol channel)."""
    # subparsers inherit `parser_class` from the parser they hang off, so every
    # subcommand's usage errors reach stdout too
    parser = _Parser(prog="engmem")
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
        # not argparse `choices=`: _cmd_search's own message names the whole vocabulary
        # and points at `engmem roles`, where argparse's would print the list once and
        # leave the reader no way to see which roles this store actually holds
        help=(
            "restrict output to a specific section ROLE (e.g. decisions, lessons, "
            "production) from each of the top-matching documents, instead of each "
            "document's single best word-matching section. Documents still rank by "
            "the query's words; a document lacking the requested role is skipped, "
            "never padded with the wrong section. Run `engmem roles` to see the "
            "full vocabulary and which roles the store currently has documents for."
        ),
    )
    _add_store_option(search_parser)
    search_parser.set_defaults(func=_cmd_search)

    roles_parser = subparsers.add_parser(
        "roles",
        help=(
            "list the section roles a `search --role` can address, and how many "
            "documents in the store carry each one"
        ),
    )
    _add_store_option(roles_parser)
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
    _add_store_option(install_parser)
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
    _add_store_option(uninstall_parser)
    uninstall_parser.set_defaults(func=cmd_uninstall)

    mcp_parser = subparsers.add_parser(
        "mcp", help="run the MCP stdio server over stdin/stdout, for clients that cannot run shell commands"
    )
    # even a usage error stays off stdout here: a client that launched `engmem mcp` with a
    # bad flag reads the stream as JSON-RPC, and one plain line is an unparseable frame
    mcp_parser.mirror_errors_to_stdout = False
    _add_store_option(mcp_parser)
    mcp_parser.set_defaults(func=_cmd_mcp)

    telemetry_parser = subparsers.add_parser(
        "telemetry",
        help=(
            "show accumulated search telemetry — totals, hit rate, and context "
            "spent, split by channel (cli/mcp) and overall"
        ),
    )
    _add_store_option(telemetry_parser)
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
    _add_store_option(backfill_parser)
    backfill_parser.set_defaults(func=_cmd_backfill)

    if argv is not None:
        # a misplaced option is exactly the case this guard exists for, so its *value* has to be
        # skipped with the same grammar every subparser declares — hence the union
        value_options = parser.value_taking_options.union(
            *(sub.value_taking_options for sub in subparsers.choices.values())
        )
        if _is_mcp_invocation(argv, subparsers.choices, value_options):
            # a usage error the *root* parser reports — `engmem --store X mcp`, where the
            # misplaced flag makes the parse fail before mcp's own subparser is ever reached —
            # never gets to consult `mcp_parser.mirror_errors_to_stdout` above, and one plain
            # line on stdout is an unparseable frame to the client either way (§5)
            parser.mirror_errors_to_stdout = False

    return parser


def _force_utf8_streams() -> None:
    """Locators carry `§`, which cp437 and cp866 cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser(argv)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
