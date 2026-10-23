"""What each `engmem` subcommand does once its arguments are parsed; `cli.py` builds the parser
that dispatches to these and owns the entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from engmem import cost, drafts, feedback, unsaved_drafts
from engmem.backfill import BackfillWriteError, apply_backfill, propose_backfill
from engmem.export import ExportError, prepare_export, render_preview, write_export
from engmem.mcp_clients import (
    installed_mcp_wirings,
    McpWiring,
    rewire_action,
    wiring_verdict,
    WiringVerdict,
)
from engmem.output import (
    _escape_controls,
    render_backfill_proposal,
    render_scoreboard,
    render_telemetry_summary,
)
from engmem.provenance import find_checkout
from engmem.read_report import ReadError, compose_read
from engmem.runtime import (
    StoreSettingError,
    StoreSource,
    fail,
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
from engmem.settings import Mode, mode_setting_file, save_mode, saved_mode
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


def cmd_search(args: argparse.Namespace) -> int:
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


def cmd_mcp(args: argparse.Namespace) -> int:
    # stdout here is MCP JSON-RPC only — fail() (both streams) must never be called from this
    # function.
    try:
        store = resolve_store(args.store)
    except StoreSettingError as exc:
        print(f"error: engmem mcp: {exc}", file=sys.stderr)
        return 2
    from engmem.mcp_server import serve

    return serve(store, read_only=args.read_only)


def cmd_roles(args: argparse.Namespace) -> int:
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


def cmd_telemetry(args: argparse.Namespace) -> int:
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


def cmd_feedback_record(args: argparse.Namespace) -> int:
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


def _document_from_stdin(command: str) -> str | None:
    """The document text an agent pipes in; None, with the reason printed, when there is none."""
    if sys.stdin is None or sys.stdin.isatty():
        fail(f"engmem draft {command}: pass the document on stdin -- nothing was written")
        return None
    try:
        text = sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"engmem draft {command}: the document on stdin is not UTF-8 ({exc}) -- nothing "
             "was written")
        return None
    if not text.strip():
        fail(f"engmem draft {command}: stdin is empty; pass the document on stdin -- nothing "
             "was written")
        return None
    return text


def cmd_draft_create(args: argparse.Namespace) -> int:
    """Publishes a new draft through the one writer the MCP tool uses (US-24)."""
    content = _document_from_stdin("create")
    if content is None:
        return 2
    try:
        print(drafts.create_draft(resolve_store(args.store), args.doc_id, content, drafts.CLI))
    except (drafts.WriteRefused, OSError) as exc:
        fail(f"engmem draft create: {exc}")
        return 2
    return 0


def cmd_draft_status(args: argparse.Namespace) -> int:
    """The unsaved drafts of the repository in the working directory (US-29)."""
    if args.hook:
        # a reminder must never break the client: any failure once the arguments parse prints
        # nothing, since a SessionStart hook's stdout reaches the agent
        try:
            text = _draft_status_hook(resolve_store(args.store))
        except Exception:
            return 0
        if text:
            print(text)
        return 0
    checkout = find_checkout(Path.cwd())
    if checkout.name is None:
        print(f"engmem: {_escape_controls(checkout.reason)}")
        return 0
    try:
        found = unsaved_drafts.unsaved_drafts(resolve_store(args.store), checkout.name)
    except OSError as exc:
        fail(f"engmem draft status: cannot read the store ({exc})")
        return 2
    print(unsaved_drafts.status_line(checkout.name, found))
    return 0


def _draft_status_hook(store: Path) -> str:
    checkout = find_checkout(Path.cwd())
    if checkout.name is None:
        return ""
    found = unsaved_drafts.unsaved_drafts(store, checkout.name)
    return unsaved_drafts.hook_json(found) if found else ""


def cmd_draft_complete(args: argparse.Namespace) -> int:
    """Publishes a finished draft as active through the one writer the MCP tool uses (US-24)."""
    content = _document_from_stdin("complete")
    if content is None:
        return 2
    try:
        print(drafts.complete_draft(
            resolve_store(args.store), args.doc_id, content, args.expected_version, drafts.CLI
        ))
    except (drafts.WriteRefused, OSError) as exc:
        fail(f"engmem draft complete: {exc}")
        return 2
    return 0


def cmd_feedback_summary(args: argparse.Namespace) -> int:
    """Finds, user assessments and unknown influence, per store or for one session."""
    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure
    session_id = _session_id(args.session)
    try:
        summary = feedback.summarize(store, {d.id for d in result.docs}, session_id)
        reads = [] if session_id is None else feedback.citable_reads(
            session_id, cost.read_doc_ids(store, session_id), result.docs
        )
    except (OSError, UnicodeDecodeError) as exc:
        # an unreadable log is not a store with no finds -- the reasoning `cmd_telemetry` gives
        fail(f"engmem feedback summary: cannot read the store's logs ({exc})")
        return 2
    print(feedback.render_summary(summary, session_id, reads))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    """One published document, or one role's sections of it, delivered and counted (US-16)."""
    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure
    try:
        text = compose_read(
            store, result.docs, args.doc_id, args.role, _session_id(args.session), "cli",
            result.errors,
        )
    except ReadError as exc:
        fail(f"engmem read: {exc}")
        return 2
    print(text)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """One published document as a file for someone else: a preview, then the file it showed (US-20)."""
    if (args.out is None) != (args.confirm is None):
        fail(
            "engmem export: --out and --confirm go together -- run `engmem export <id>` for the "
            "preview and its fingerprint, then add --out FILE --confirm <fingerprint>; nothing "
            "written"
        )
        return 2
    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure
    try:
        prepared = prepare_export(store, result, args.doc_id)
        if args.out is None:
            print(render_preview(prepared))
            return 0
        target = write_export(prepared, Path(args.out), store, args.confirm)
    except ExportError as exc:
        fail(f"engmem export: {exc}")
        return 2
    print(
        f"engmem export: wrote {target} ({len(prepared.data)} bytes, fingerprint "
        f"{prepared.fingerprint}) -- the file the preview showed; the store is unchanged"
    )
    return 0


def cmd_cost_record_baseline(args: argparse.Namespace) -> int:
    """Appends what the client reported for a session's baseline call (US-16)."""
    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure
    try:
        entry = cost.record_baseline(
            store, {d.id for d in result.docs}, session_id=args.session_id,
            tokens=args.tokens, seconds=args.seconds, call_id=args.call_id, channel="cli",
        )
    except cost.CostError as exc:
        fail(f"engmem cost record-baseline: {exc}")
        return 2
    print(f"engmem cost: {cost.confirmation(entry)}")
    return 0


def cmd_cost_summary(args: argparse.Namespace) -> int:
    """Measured, estimated, client-reported and missing cost of one session, each labelled."""
    session_id = _session_id(args.session)
    if session_id is None:
        fail("engmem cost summary: --session needs a session document id (got a blank value)")
        return 2
    store = resolve_store(args.store)
    result, failure = _load_sessions(store)
    if failure:
        return failure
    try:
        summary = cost.summarize(store, session_id, {d.id for d in result.docs})
    except (OSError, UnicodeDecodeError) as exc:
        # an unreadable log is not a session that cost nothing -- `cmd_telemetry`'s reasoning
        fail(f"engmem cost summary: cannot read the store's logs ({exc})")
        return 2
    print(cost.render_summary(summary))
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
    `drafts._resolve_sessions_dir` makes before it writes."""
    try:
        own = store.resolve(strict=False) / "sessions"
        return (store / "sessions").resolve(strict=True) == own
    except OSError:
        return True  # unreadable or absent: `_load_sessions` reports it in its own words


def cmd_backfill(args: argparse.Namespace) -> int:
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


def cmd_store_show(args: argparse.Namespace) -> int:
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


def cmd_store_set(args: argparse.Namespace) -> int:
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


def cmd_mode_show(args: argparse.Namespace) -> int:
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


def cmd_mode_set(args: argparse.Namespace) -> int:
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
