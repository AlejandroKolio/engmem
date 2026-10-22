"""`engmem` CLI entry point: the argument parser and the dispatch to `commands.py`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Container
from typing import NoReturn

from engmem import __version__, feedback
from engmem.commands import (
    cmd_backfill,
    cmd_cost_record_baseline,
    cmd_cost_summary,
    cmd_draft_complete,
    cmd_draft_create,
    cmd_export,
    cmd_feedback_record,
    cmd_feedback_summary,
    cmd_mcp,
    cmd_mode_set,
    cmd_mode_show,
    cmd_read,
    cmd_roles,
    cmd_search,
    cmd_store_set,
    cmd_store_show,
    cmd_telemetry,
)
from engmem.doctor import cmd_doctor
from engmem.install import cmd_install, cmd_uninstall, VALID_AGENTS
from engmem.runtime import StoreSettingError, fail, force_utf8_streams
from engmem.settings import Mode, ModeSettingError
from engmem.walkthrough import DEMOS as WALKTHROUGH_DEMOS
from engmem.walkthrough import cmd_walkthrough


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
        # not argparse `choices=`: cmd_search's own message names the whole vocabulary
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
    search_parser.set_defaults(func=cmd_search)

    roles_parser = subparsers.add_parser(
        "roles",
        help=(
            "list the section roles a `search --role` can address, and how many "
            "documents in the store carry each one"
        ),
    )
    _add_store_option(roles_parser)
    roles_parser.set_defaults(func=cmd_roles)

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
        help=(
            "expose only the search tools, for a server reachable from outside this machine; "
            "refuses writes, does not limit who reads"
        ),
    )
    _add_store_option(mcp_parser)
    mcp_parser.set_defaults(func=cmd_mcp)

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
    store_show_parser.set_defaults(func=cmd_store_show)
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
    store_set_parser.set_defaults(func=cmd_store_set)

    mode_parser = subparsers.add_parser(
        "mode",
        help="show or save whether sessions run in daily mode or research (Gate 1) mode",
    )
    mode_commands = mode_parser.add_subparsers(dest="mode_command", required=True)
    mode_show_parser = mode_commands.add_parser(
        "show", help="print the mode the next session will record, and where it came from"
    )
    mode_show_parser.set_defaults(func=cmd_mode_show)
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
        # not argparse `choices=`: cmd_mode_set names the cause on both streams itself
        help=f"one of {', '.join(Mode)} (default when nothing is saved: daily)",
    )
    mode_set_parser.set_defaults(func=cmd_mode_set)

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

    walkthrough_parser = subparsers.add_parser(
        "walkthrough",
        help=(
            "build a demo for real agent sessions, with its own store and client settings, and "
            "print each step: codex walks one save-and-find cycle in Codex, handoff passes a "
            "task between Codex and Claude Code; --verify checks it was completed"
        ),
    )
    walkthrough_parser.add_argument(
        "client",
        metavar="DEMO",
        # not argparse `choices=`: `cmd_walkthrough` names the cause on both streams itself
        help=f"one of {', '.join(WALKTHROUGH_DEMOS)}",
    )
    walkthrough_parser.add_argument(
        "--dir",
        required=True,
        help=(
            "the demo directory: new, empty, or one the same walkthrough built; holds the demo "
            "store, the demo's client settings and its workspaces"
        ),
    )
    walkthrough_parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "check the demo store for what the walkthrough's steps leave behind; PASS only when "
            "every check holds"
        ),
    )
    walkthrough_parser.set_defaults(func=cmd_walkthrough)

    telemetry_parser = subparsers.add_parser(
        "telemetry",
        help=(
            "show accumulated search telemetry — totals, hit rate, and context "
            "spent, split by channel (cli/mcp) and overall"
        ),
    )
    _add_store_option(telemetry_parser)
    telemetry_parser.set_defaults(func=cmd_telemetry)

    draft_parser = subparsers.add_parser(
        "draft",
        help=(
            "create a session's draft, or complete it as active, with the document on stdin -- "
            "the same checks the MCP write tools make, never a plain file write"
        ),
    )
    draft_commands = draft_parser.add_subparsers(dest="draft_command", required=True)
    draft_create_parser = draft_commands.add_parser(
        "create",
        help="publish a new `status: draft` document; never overwrites an existing id",
    )
    draft_create_parser.add_argument("doc_id", metavar="DOC_ID", help="the new document's id")
    _add_store_option(draft_create_parser)
    draft_create_parser.set_defaults(func=cmd_draft_create)
    draft_complete_parser = draft_commands.add_parser(
        "complete",
        help="publish the finished document as `status: active` over its own draft",
    )
    draft_complete_parser.add_argument("doc_id", metavar="DOC_ID", help="the draft's id")
    draft_complete_parser.add_argument(
        "--expected-version",
        default=None,
        help=(
            "the version `engmem draft create` printed (required); a draft changed since is "
            "refused, and so is a call without it -- both refusals print the current version"
        ),
    )
    _add_store_option(draft_complete_parser)
    draft_complete_parser.set_defaults(func=cmd_draft_complete)

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
    feedback_record_parser.set_defaults(func=cmd_feedback_record)
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
    feedback_summary_parser.set_defaults(func=cmd_feedback_summary)

    read_parser = subparsers.add_parser(
        "read",
        help=(
            "print one document, or only its sections of one role, and count the bytes "
            "delivered for `engmem cost summary`"
        ),
    )
    read_parser.add_argument(
        "doc_id",
        metavar="DOC_ID",
        help="the id of the document to read, or the ID@VERSION a search cites",
    )
    read_parser.add_argument(
        "--role",
        default=None,
        # not argparse `choices=`: `compose_read` names the whole vocabulary on both streams
        help="print only the sections of this ROLE (see `engmem roles`)",
    )
    read_parser.add_argument(
        "--session",
        default=None,
        help="id of the draft document this read belongs to, as on `engmem search`",
    )
    _add_store_option(read_parser)
    read_parser.set_defaults(func=cmd_read)

    export_parser = subparsers.add_parser(
        "export",
        help=(
            "preview one published document as a file to hand to someone else, with references "
            "to the rest of the store and local paths cut, then write exactly that file with "
            "--out FILE --confirm <fingerprint>; changes nothing in the store"
        ),
    )
    export_parser.add_argument("doc_id", metavar="DOC_ID", help="the id of the document to export")
    export_parser.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help="the new file to write, outside the store; an existing file is never replaced",
    )
    export_parser.add_argument(
        "--confirm",
        default=None,
        metavar="FINGERPRINT",
        help="the fingerprint the preview printed; the file is written only if it still matches",
    )
    _add_store_option(export_parser)
    export_parser.set_defaults(func=cmd_export)

    cost_parser = subparsers.add_parser(
        "cost",
        help=(
            "show what a session's searches and reads delivered, apart from estimates and "
            "missing data, or record what the client reported for its baseline call"
        ),
    )
    cost_commands = cost_parser.add_subparsers(dest="cost_command", required=True)
    record_baseline_parser = cost_commands.add_parser(
        "record-baseline",
        help=(
            "append the tokens and/or seconds the client reported for the session's no-memory "
            "baseline call; only figures the client reported, never an estimate"
        ),
    )
    record_baseline_parser.add_argument(
        "session_id", metavar="SESSION_ID", help="the session document the baseline belongs to"
    )
    record_baseline_parser.add_argument(
        "--tokens", type=int, default=None, help="tokens the client reported for the call"
    )
    record_baseline_parser.add_argument(
        "--seconds", type=float, default=None, help="duration the client reported for the call"
    )
    record_baseline_parser.add_argument(
        "--call-id", default=None, help="the client's id for that call, if it gives one"
    )
    _add_store_option(record_baseline_parser)
    record_baseline_parser.set_defaults(func=cmd_cost_record_baseline)
    cost_summary_parser = cost_commands.add_parser(
        "summary",
        help=(
            "bytes engmem delivered to one session, estimated tokens, the client-reported "
            "baseline and what is not observed; never a savings figure"
        ),
    )
    cost_summary_parser.add_argument(
        "--session",
        required=True,
        help="the session document id the operations were attributed to",
    )
    _add_store_option(cost_summary_parser)
    cost_summary_parser.set_defaults(func=cmd_cost_summary)

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
    backfill_parser.set_defaults(func=cmd_backfill)

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
