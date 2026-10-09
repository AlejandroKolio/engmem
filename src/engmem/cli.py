"""`engmem` CLI entry point: argument parsing and the subcommands."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Container
from pathlib import Path
from typing import NoReturn

import yaml

from engmem import __version__, feedback
from engmem.backfill import BackfillWriteError, apply_backfill, propose_backfill
from engmem.doctor import cmd_doctor
from engmem.install import (
    VALID_AGENTS,
    McpWiring,
    WiringVerdict,
    cmd_install,
    cmd_uninstall,
    installed_mcp_wirings,
    rewire_action,
    wiring_verdict,
)
from engmem.output import (
    render_backfill_proposal,
    render_scoreboard,
    render_telemetry_summary,
)
from engmem.runtime import (
    StoreSettingError,
    StoreSource,
    fail,
    force_utf8_streams,
    locate_store,
    resolve_store,
    save_store,
    saved_store,
    source_description,
    store_setting_file,
)
from engmem.scoring import Scope, role_coverage
from engmem.search_report import compose
from engmem.sections import CANONICAL_ROLES
from engmem.settings import Mode, ModeSettingError, mode_setting_file, save_mode, saved_mode
from engmem.spine import Doc, LoadResult, load_store, sessions_dir_unreadable, stray_documents
from engmem.telemetry import summarize as summarize_telemetry


def _session_id(raw: str | None) -> str | None:
    # blank/whitespace == not passed, so the log can tell "unattributed" from
    # "session unknown"; no other validation — the CLI doesn't know which ids exist
    stripped = (raw or "").strip()
    return stripped or None


def _load_sessions(store: Path) -> tuple[LoadResult | None, int]:
    """`_read_sessions`, plus the stdout line for an unlistable `sessions/` — for subcommands whose
    stdout is not `search_report.compose`'s, which carries that line itself."""
    result, failure = _read_sessions(store)
    if result is not None and result.scan_error is not None:
        print(f"error: {result.scan_error.message}")
    return result, failure


def _read_sessions(store: Path) -> tuple[LoadResult | None, int]:
    """Loads `sessions/`, reporting every error to stderr; returns `(result, 0)` or `(None, 2)`."""
    sessions_dir = store / "sessions"
    unreadable = sessions_dir_unreadable(sessions_dir)
    if unreadable:
        fail(unreadable)
        return None, 2
    if not sessions_dir.is_dir():
        fail(
            f"store not found: {sessions_dir} does not exist "
            f"(run `engmem install` to create the store, or `engmem store show` to see which "
            f"setting chose this path)"
        )
        return None, 2
    result = load_store(sessions_dir)
    for problem in result.errors:
        print(f"error: {problem.message}", file=sys.stderr)
    for problem in result.warnings:
        print(f"warning: {problem.message}", file=sys.stderr)
    return result, 0


def _report_strays(store: Path) -> tuple[list[Path], list[str]]:
    """Warns on stderr about markdown engmem never reads; the stdout half is `compose`'s."""
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
        print(f"warning: {scan_error}", file=sys.stderr)
    return strays, scan_errors


def _search_scope(args: argparse.Namespace) -> tuple[Scope | None, int]:
    """`(scope, 0)`, or `(None, 2)` after naming why the scope asked for is not one."""
    if args.unscoped:
        return Scope(unscoped=True), 0
    if args.all_repos:
        return Scope(all_repos=True), 0
    if args.repo is None:
        return None, 0
    names = tuple(name.strip() for name in args.repo)
    if not all(names):
        # never dropped or read as "no scope": either would widen the search past what was asked
        fail("engmem search: --repo needs a repository name (got a blank value)")
        return None, 2
    return Scope(repos=names), 0


def _cmd_search(args: argparse.Namespace) -> int:
    # cheapest usage-error check first: a typo'd --role needs no store to be wrong
    if args.role is not None and args.role not in CANONICAL_ROLES:
        fail(
            f"engmem search: unknown role {args.role!r} — valid roles: "
            f"{', '.join(CANONICAL_ROLES)} (see `engmem roles` for which of these "
            f"the store actually has documents for)"
        )
        return 2
    scope, failure = _search_scope(args)
    if failure:
        return failure

    store = resolve_store(args.store)
    result, failure = _read_sessions(store)
    if failure:
        return failure

    strays, stray_scan_errors = _report_strays(store)
    print(compose(
        store, result, strays, stray_scan_errors, args.query, args.role,
        _session_id(args.session), "cli", scope,
    ))
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    # stdout here is MCP JSON-RPC only — fail() (both streams) must never be called from this
    # function.
    try:
        store = resolve_store(args.store)
    except StoreSettingError as exc:
        print(f"error: engmem mcp: {exc}", file=sys.stderr)
        return 2
    from engmem.mcp_server import serve

    return serve(store, read_only=args.read_only)


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


def _cmd_feedback_record(args: argparse.Namespace) -> int:
    """Appends the user's assessment of one find of a session (US-15)."""
    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure
    try:
        entry = feedback.record(
            store, {d.id for d in result.docs},
            session_id=args.session_id, doc_id=args.doc_id, assessment=args.assessment,
            decision=args.decision, source=args.source, channel="cli",
        )
    except feedback.FeedbackError as exc:
        fail(f"engmem feedback record: {exc}")
        return 2
    print(f"engmem feedback: {feedback.confirmation(entry)}")
    return 0


def _cmd_feedback_summary(args: argparse.Namespace) -> int:
    """Finds, user assessments and unknown influence, per store or for one session."""
    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure
    session_id = _session_id(args.session)
    try:
        summary = feedback.summarize(store, {d.id for d in result.docs}, session_id)
    except (OSError, UnicodeDecodeError) as exc:
        # an unreadable log is not a store with no finds -- the reasoning `_cmd_telemetry` gives
        fail(f"engmem feedback summary: cannot read the store's logs ({exc})")
        return 2
    print(feedback.render_summary(summary, session_id))
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


def _wiring_line(wiring: McpWiring, store: Path) -> str:
    where = f"{wiring.agent}: {wiring.config}"
    verdict = wiring_verdict(wiring, store)
    if verdict is WiringVerdict.AT_LAUNCH:
        return (
            f"{where} launches the MCP server without --store — it resolves the store at "
            f"launch, from the client's own environment"
        )
    if verdict is WiringVerdict.NO_FIXED_BASE:
        return (
            f"{where} launches the MCP server with --store {wiring.store}, a relative path with "
            f"no fixed base — it names a different directory for every working directory the "
            f"client launches it from; make it absolute"
        )
    if verdict is WiringVerdict.MATCHES:
        return f"{where} launches the MCP server with --store {wiring.store} — matches"
    if verdict is WiringVerdict.INVALID:
        return (
            f"mismatch: {where} launches the MCP server with --store {str(wiring.store)!r}, which "
            f"is not a usable path (it contains a NUL byte) — {rewire_action(wiring, store)}"
        )
    return (
        f"mismatch: {where} launches the MCP server with --store {wiring.store}, not {store} — "
        f"{rewire_action(wiring, store)}; no documents are moved either way"
    )


def _wiring_lines(store: Path, *, mismatches_only: bool) -> list[str]:
    wirings, problems = installed_mcp_wirings()
    lines = [_wiring_line(wiring, store) for wiring in wirings]
    if mismatches_only:
        lines = [line for line in lines if line.startswith("mismatch:")]
    elif not wirings and not problems:
        lines.append("mcp wiring: none found (claude-desktop, codex)")
    return lines + [f"warning: {problem}" for problem in problems]


def _cmd_store_show(args: argparse.Namespace) -> int:
    """The effective store, the setting it came from, and every MCP entry that records another."""
    choice = locate_store(args.store)
    print(f"store: {choice.path}")
    print(f"source: {source_description(choice)}")
    saved_error = None
    if choice.source in (StoreSource.FLAG, StoreSource.ENV):
        try:
            saved = saved_store()
        except StoreSettingError as exc:
            saved, saved_error = None, exc
        if saved is not None and os.path.realpath(saved) != os.path.realpath(choice.path):
            print(
                f"note: {choice.source} overrides the saved choice {saved} "
                f"({store_setting_file()}) for this command only; the saved choice is unchanged"
            )
    if not (choice.path / "sessions").is_dir():
        print(f"warning: {choice.path / 'sessions'} does not exist — run `engmem install` to create it")
    for line in _wiring_lines(choice.path, mismatches_only=False):
        print(line)
    if saved_error is not None:
        fail(f"engmem store show: {saved_error}")
        return 2
    return 0


def _cmd_store_set(args: argparse.Namespace) -> int:
    """Saves the store every command and template without an override will use from now on."""
    if not args.path.strip():
        fail("engmem store set: PATH is blank — name the store directory to save")
        return 2
    store = resolve_store(args.path)
    if "\n" in str(store) or "\r" in str(store):
        fail(f"engmem store set: {store!r} contains a line break, which a one-line setting cannot hold")
        return 2
    try:
        setting = save_store(store)
    except (OSError, UnicodeEncodeError) as exc:
        fail(f"engmem store set: cannot write {store_setting_file()}: {exc}")
        return 2
    print(f"engmem store: saved {store} in {setting}")
    if not (store / "sessions").is_dir():
        print(f"note: {store / 'sessions'} does not exist yet — run `engmem install` to create it")
    env = os.environ.get("ENGMEM_HOME")
    if env and env.strip():
        print(
            f"note: ENGMEM_HOME={env} is set here and takes precedence over the saved choice — "
            f"unset it for the saved store to apply"
        )
    for line in _wiring_lines(store, mismatches_only=True):
        print(line)
    return 0


_MODE_MEANING = {
    Mode.DAILY: (
        "daily: /engmem writes no Pre-reg and launches no baseline sub-agent; sessions are "
        "recorded `mode: daily` and stay outside the Gate 1 experiment"
    ),
    Mode.RESEARCH: (
        "research: /engmem runs the Gate 1 protocol — a Pre-reg baseline before the first "
        "search, or `baseline_unavailable: <reason>` when none can be had"
    ),
}


def _cmd_mode_show(args: argparse.Namespace) -> int:
    """The mode the next session will record, and where it came from."""
    setting = mode_setting_file()
    mode = saved_mode()
    print(f"mode: {mode or Mode.DAILY}")
    if mode is None:
        print(f"source: default (no saved choice in {setting})")
    else:
        print(f"source: saved choice ({setting})")
    print(_MODE_MEANING[mode or Mode.DAILY])
    return 0


def _cmd_mode_set(args: argparse.Namespace) -> int:
    """Saves the mode every session started from now on records; past sessions keep theirs."""
    try:
        mode = Mode(args.mode.strip().casefold())
    except ValueError:
        fail(f"engmem mode set: unknown mode {args.mode!r} — one of {', '.join(Mode)}")
        return 2
    try:
        setting = save_mode(mode)
    except OSError as exc:
        fail(f"engmem mode set: cannot write {mode_setting_file()}: {exc}")
        return 2
    print(f"engmem mode: saved {mode} in {setting}")
    print(_MODE_MEANING[mode])
    print(
        "note: the next session started with /engmem records this mode; sessions already "
        "started keep the mode they recorded"
    )
    return 0


_STORE_HELP = (
    "path to the engmem store (default: $ENGMEM_HOME, else the store saved by "
    "`engmem store set`, else ~/Developer/engmem)"
)


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
    scope_options = search_parser.add_mutually_exclusive_group()
    scope_options.add_argument(
        "--repo",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "search only the records whose `repos` front matter names this repository "
            "(case-insensitive); repeat it to search several, a record linked to any of them "
            "counting once. Title and tags never stand in for that link, so a record with no "
            "`repos` is left out; find those with --unscoped. Without a scope flag the whole "
            "store is searched."
        ),
    )
    scope_options.add_argument(
        "--unscoped",
        action="store_true",
        help="search only the records linked to no repository (no readable `repos` value)",
    )
    scope_options.add_argument(
        "--all-repos",
        action="store_true",
        help=(
            "search the whole store, as without a scope flag, but say so in a `scope:` line and "
            "show each result's `repos:` links"
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
    mcp_parser.add_argument(
        "--read-only",
        action="store_true",
        help="expose only the search tools, for a server reachable from outside this machine",
    )
    _add_store_option(mcp_parser)
    mcp_parser.set_defaults(func=_cmd_mcp)

    store_parser = subparsers.add_parser(
        "store", help="show which store is used and why, or save the one to use by default"
    )
    store_commands = store_parser.add_subparsers(dest="store_command", required=True)
    store_show_parser = store_commands.add_parser(
        "show",
        help=(
            "print the effective store, the setting it came from, and any installed MCP "
            "entry that points at a different store"
        ),
    )
    _add_store_option(store_show_parser)
    store_show_parser.set_defaults(func=_cmd_store_show)
    store_set_parser = store_commands.add_parser(
        "set",
        help=(
            "save PATH as the store used by every command and installed template run without "
            "--store or ENGMEM_HOME"
        ),
    )
    store_set_parser.add_argument(
        "path",
        metavar="PATH",
        help="the store directory; saved absolute, with a leading ~ expanded, as --store reads it",
    )
    store_set_parser.set_defaults(func=_cmd_store_set)

    mode_parser = subparsers.add_parser(
        "mode",
        help="show or save whether sessions run in daily mode or research (Gate 1) mode",
    )
    mode_commands = mode_parser.add_subparsers(dest="mode_command", required=True)
    mode_show_parser = mode_commands.add_parser(
        "show", help="print the mode the next session will record, and where it came from"
    )
    mode_show_parser.set_defaults(func=_cmd_mode_show)
    mode_set_parser = mode_commands.add_parser(
        "set",
        help=(
            "save the mode for every session started from now on; sessions already "
            "recorded keep theirs"
        ),
    )
    mode_set_parser.add_argument(
        "mode",
        metavar="MODE",
        # not argparse `choices=`: _cmd_mode_set names the cause on both streams itself
        help=f"one of {', '.join(Mode)} (default when nothing is saved: daily)",
    )
    mode_set_parser.set_defaults(func=_cmd_mode_set)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help=(
            "check why an agent may not see its memory: the store and its source, the "
            "executables, access to sessions/, and the agent's wiring — changes nothing"
        ),
    )
    doctor_parser.add_argument(
        "--agent",
        default="claude",
        # not argparse `choices=` — see `_validate_agent`
        help=f"one of {', '.join(VALID_AGENTS)} (default: claude)",
    )
    doctor_parser.add_argument(
        "--local", action="store_true", help="check the project-local install, as install --local"
    )
    _add_store_option(doctor_parser)
    doctor_parser.set_defaults(func=cmd_doctor)

    telemetry_parser = subparsers.add_parser(
        "telemetry",
        help=(
            "show accumulated search telemetry — totals, hit rate, and context "
            "spent, split by channel (cli/mcp) and overall"
        ),
    )
    _add_store_option(telemetry_parser)
    telemetry_parser.set_defaults(func=_cmd_telemetry)

    feedback_parser = subparsers.add_parser(
        "feedback",
        help=(
            "record the user's assessment of a document a session's search showed -- helped, "
            "not-applicable or harmful -- or summarize finds, assessments and unknown influence"
        ),
    )
    feedback_commands = feedback_parser.add_subparsers(dest="feedback_command", required=True)
    feedback_record_parser = feedback_commands.add_parser(
        "record",
        help=(
            "append the user's assessment of one find; only what the user said, never an "
            "agent's own judgement"
        ),
    )
    feedback_record_parser.add_argument(
        "session_id", metavar="SESSION_ID", help="the session document whose search showed it"
    )
    feedback_record_parser.add_argument(
        "doc_id", metavar="DOC_ID", help="the document the search showed"
    )
    feedback_record_parser.add_argument(
        "assessment",
        metavar="ASSESSMENT",
        # not argparse `choices=`: `feedback.record` names the three on both streams itself
        help=f"one of {feedback.ASSESSMENT_CHOICES}",
    )
    feedback_record_parser.add_argument(
        "--decision", default=None, help="the decision the document changed, in the user's words"
    )
    feedback_record_parser.add_argument(
        "--source", default=None, help="where that can be seen, e.g. a commit, PR or review"
    )
    _add_store_option(feedback_record_parser)
    feedback_record_parser.set_defaults(func=_cmd_feedback_record)
    feedback_summary_parser = feedback_commands.add_parser(
        "summary",
        help=(
            "count finds, user assessments and finds whose influence is unknown; not part of "
            "the Gate 1 count"
        ),
    )
    feedback_summary_parser.add_argument(
        "--session", default=None, help="only this session, listing its unassessed finds too"
    )
    _add_store_option(feedback_summary_parser)
    feedback_summary_parser.set_defaults(func=_cmd_feedback_summary)

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


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser(argv)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (StoreSettingError, ModeSettingError) as exc:
        # one place for every command: none of them may fall back to another store, or read a
        # broken mode as daily (contracts/runtime.md); `engmem mcp` handles its own
        fail(f"engmem {args.command}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
