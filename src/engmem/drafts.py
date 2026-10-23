"""The one writer behind creating and completing a session document, shared by the MCP tools and
`engmem draft` (US-24). Path containment and the rules a write must pass: contracts/mcp-server.md,
"Write tools: security contract"; why both surfaces share it: contracts/mcp-server.md, "One writer
for every surface"."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

from engmem import confined, cost, feedback, gate1
from engmem.cache import identity_for
from engmem.output import _escape_controls
from engmem.settings import Mode, ModeSettingError, effective_mode, mode_setting_file
from engmem.sections import split_sections
# imported, not reimplemented, so a document written here is validated by the exact rules
# load_store applies when reading it back
from engmem.spine import Doc, load_store, parse_document, sessions_dir_unreadable, validate_doc_id
from engmem.staging import (
    DocumentLockedError,
    commit,
    commit_new,
    discard,
    document_lock,
    stage,
    version_of,
)
from engmem.versions import is_version, split_reference


class WriteRefused(Exception):
    """A write refused for a reason the security contract or the document rules call for;
    nothing was written."""


@dataclass(frozen=True)
class Surface:
    """How one client surface names the steps, so a refusal tells that client what to do."""

    verb: str
    id_argument: str
    create: str
    complete: str
    search: str
    version_argument: str
    again: str
    attribute: Callable[[str], str]
    pass_version: Callable[[str], str]


MCP = Surface(
    verb="call",
    id_argument="'id' argument",
    create="engmem_create_draft",
    complete="engmem_complete_draft",
    search="engmem_search",
    version_argument="expected_version",
    again="call this tool again",
    attribute=lambda doc_id: (
        f'pass session_id: "{doc_id}" on every engmem_search and engmem_search_by_role call'
    ),
    pass_version=lambda version: f'expected_version: "{version}"',
)
CLI = Surface(
    verb="run",
    id_argument="DOC_ID",
    create="engmem draft create",
    complete="engmem draft complete",
    search="engmem search",
    version_argument="--expected-version",
    again="run the command again",
    attribute=lambda doc_id: f"pass --session {doc_id} on every engmem search",
    pass_version=lambda version: f"--expected-version {version}",
)


def _resolve_sessions_dir(store: Path) -> Path:
    """Resolved `store/sessions`, raising `WriteRefused` when it is missing or escapes the store."""
    sessions_dir = store / "sessions"
    unreadable = sessions_dir_unreadable(sessions_dir)
    if unreadable:
        raise WriteRefused(unreadable)
    if not sessions_dir.is_dir():
        raise WriteRefused(
            f"store not found: {sessions_dir} does not exist (run `engmem install` "
            "to create the store, or check the configured store path)."
        )
    try:
        resolved_store = store.resolve(strict=False)
        resolved_sessions = sessions_dir.resolve(strict=True)
    except OSError as exc:
        raise WriteRefused(f"could not resolve store path {store}: {exc}") from exc

    if resolved_sessions != resolved_store / "sessions":
        raise WriteRefused(
            f"refusing to write: {sessions_dir} resolves to {resolved_sessions}, "
            f"outside {resolved_store} — sessions/ appears to be a symlink (or the "
            "store path contains one) pointing somewhere the store does not own."
        )
    return resolved_sessions


def resolve_write_target(store: Path, doc_id: object) -> Path:
    """The chokepoint every write tool calls first; never returns a path outside `sessions/`."""
    reason = validate_doc_id(doc_id)
    if reason is not None:
        raise WriteRefused(f"invalid document id {doc_id!r}: {reason}")

    resolved_sessions = _resolve_sessions_dir(store)
    target = resolved_sessions / f"{doc_id}.md"

    # checked before resolve(), which would follow a symlink to its target
    # and hide the real problem behind the containment check below
    if target.is_symlink():
        raise WriteRefused(
            f"refusing to write through a symlink: sessions/{doc_id}.md is a "
            "symlink, not a plain file — this tool never follows or replaces one."
        )

    resolved_target = target.resolve(strict=False)
    if resolved_target.parent != resolved_sessions or resolved_target.name != f"{doc_id}.md":
        raise WriteRefused(f"refusing to write outside sessions/: {doc_id!r}")

    return target


def stage_content(target: Path, content: bytes) -> tuple[Doc | None, Path, str | None]:
    """Stages `content` and parses it; the caller commits or discards what comes back."""
    tmp_path = stage(target, content)
    try:
        doc = parse_document(tmp_path)
    except (yaml.YAMLError, ValueError, OSError) as exc:
        return None, tmp_path, f"content does not parse: {exc}"
    return doc, tmp_path, None


def _create_conflict(doc_id: str, surface: Surface, detail: str | None = None) -> WriteRefused:
    reason = detail or "it already exists"
    return WriteRefused(
        f"refusing to create sessions/{doc_id}.md: {reason} — "
        f"{surface.create} never overwrites. Use "
        f"{surface.complete} to finish an existing draft, or choose a different id."
    )


def _mode_refusal(doc: Doc) -> str | None:
    """Why `doc` may not start a session under the mode saved now, None when it may — see
    contracts/mcp-server.md, "The mode a draft records"."""
    setting = mode_setting_file()
    try:
        configured, unreadable = effective_mode(), None
    except ModeSettingError as exc:
        # a daily record is never a wrong observation, so only research waits for the repair
        configured, unreadable = Mode.DAILY, exc
    if doc.mode is None:
        return (
            f"content's front matter has no `mode` — state `mode: {configured}`, the "
            f"configured mode (`engmem mode show`; saved in {setting}). If you followed an "
            "installed engmem template or skill, it may predate modes: have the user run "
            "`engmem install --agent <agent>` again to update it."
        )
    if doc.mode not in set(Mode):
        return (
            f"content's front matter mode {doc.mode!r} is not one of {', '.join(Mode)} — "
            f"the configured mode is {configured} (`engmem mode show`)."
        )
    if unreadable is not None and doc.mode != Mode.DAILY:
        return (
            f"the saved mode cannot be read ({unreadable}), so a research session cannot be "
            "confirmed — create this draft with `mode: daily`, or have the user repair the "
            "file with `engmem mode set research` first."
        )
    if doc.mode != configured:
        return (
            f"content's front matter mode is {doc.mode}, but the configured mode is "
            f"{configured} ({setting}) — record `mode: {configured}`, or have the user run "
            f"`engmem mode set {doc.mode}` first if this session should be {doc.mode}."
        )
    has_prereg = any(s.canonical == "prereg" for s in split_sections(doc.body))
    if doc.mode == Mode.RESEARCH and not has_prereg and doc.baseline_unavailable is None:
        return (
            "a research draft records its baseline before the first search — include the "
            "`## Pre-reg` section, or state `baseline_unavailable: <reason>` in the front "
            "matter when no uncontaminated baseline can be had (`engmem mode` decides "
            "which protocol applies)."
        )
    return None


def _mode_note(doc: Doc) -> str:
    if doc.mode == Mode.DAILY:
        return (
            "Recorded mode: daily — no Pre-reg needed; this session stays outside the Gate 1 "
            "experiment."
        )
    if doc.baseline_unavailable is not None:
        return (
            "Recorded mode: research, baseline_unavailable — search and save as usual; the "
            "session is reported as an incomplete observation."
        )
    return "Recorded mode: research — the baseline is on disk before the first search."


def create_draft(store: Path, doc_id: object, content: str, surface: Surface) -> str:
    """Publishes a new draft, never over an existing name; the message names its version and
    how `surface` passes it back. Raises `WriteRefused`."""
    target = resolve_write_target(store, doc_id)

    if target.exists():
        raise _create_conflict(doc_id, surface)

    content_bytes = content.encode("utf-8")
    doc, tmp_path, parse_error = stage_content(target, content_bytes)
    try:
        if parse_error is not None:
            raise WriteRefused(f"sessions/{doc_id}.md {parse_error}")
        if doc.id != doc_id:
            raise WriteRefused(
                f"content's front matter id {doc.id!r} does not match the "
                f"{surface.id_argument} {doc_id!r} — they must be identical."
            )
        if doc.status != "draft":
            raise WriteRefused(
                f"content's front matter status is {doc.status!r}, not "
                f"'draft' — {surface.create} only ever creates a draft."
            )
        mode_refusal = _mode_refusal(doc)
        if mode_refusal is not None:
            raise WriteRefused(f"refusing to create sessions/{doc_id}.md: {mode_refusal}")
        try:
            commit_new(tmp_path, target)
        except FileExistsError as exc:
            # another creator took the id after the check above
            detail = None if os.path.lexists(target) else exc.strerror
            raise _create_conflict(doc_id, surface, detail) from exc
    # BaseException, not WriteRefused: `commit_new` links or renames, so it can raise
    # OSError of its own, and the staged file must go either way
    except BaseException:
        discard(tmp_path)
        raise

    return (
        f"created sessions/{doc_id}.md (status: draft, version: {version_of(content_bytes)}). "
        f"Now {surface.attribute(doc_id)} for the rest of this task — a search without it is "
        "logged unattributed and drops out of the analysis. Keep the version: pass it as "
        f"{surface.version_argument} to {surface.complete} when you finish this draft. "
        f"{_mode_note(doc)} {REUSE_LOG_SHAPE}"
    )


def refuse_if_changed_since_read(target: Path, read: Doc, doc_id: str, again: str) -> None:
    """`backfill.apply_backfill`'s rule: a document that moved under the read a write is based
    on is refused, never overwritten from the copy that is already stale."""
    if read.source_identity != identity_for(target):
        raise WriteRefused(
            f"sessions/{doc_id}.md changed on disk while it was being read — "
            f"refusing to write, so a newer version is not replaced by a stale one. {again}."
        )


@contextmanager
def locked(target: Path, doc_id: str, surface: Surface) -> Iterator[None]:
    """`staging.document_lock`, with a lock held past the wait refused by name."""
    try:
        with document_lock(target):
            yield
    except DocumentLockedError as exc:
        raise WriteRefused(
            f"sessions/{doc_id}.md is being written by another engmem call — nothing was "
            f"written; {surface.again}. If this repeats while no engmem process is "
            f"running, an interrupted write left sessions/{exc.lock.name} behind: remove it."
        ) from None


def refuse_unless_version_read(
    target: Path,
    doc_id: str,
    expected_version: str | None,
    surface: Surface,
    *,
    action: str,
    tool: str,
    redo: str,
) -> bytes:
    """The document's bytes, when they are the version the client read; otherwise refused with
    its current text and version, never rewritten from the client's stale or unstated read."""
    try:
        data = confined.read_bytes(target, confined.SESSION_DEPTH)
    except OSError as exc:
        raise WriteRefused(
            f"sessions/{doc_id}.md could not be re-read ({exc}) — nothing was written."
        ) from exc
    current = version_of(data)
    stated = (expected_version or "").strip()
    if stated == current:
        return data
    if stated:
        why = (
            f"it changed since you read it ({surface.version_argument} {expected_version!r}, "
            f"current version {current!r})"
        )
    else:
        why = (
            f"no {surface.version_argument} was given, so there is no read to check it "
            "against"
        )
    raise WriteRefused(
        f"refusing to {action} sessions/{doc_id}.md: {why} — nothing was written. Re-read "
        f"the current document below, {redo}, and {surface.verb} {tool} again with "
        f"{surface.pass_version(current)}.\n"
        f"--- current sessions/{doc_id}.md ---\n"
        + data.decode("utf-8", errors="replace")
    )


def complete_draft(
    store: Path, doc_id: object, content: str, expected_version: str | None, surface: Surface
) -> str:
    """Publishes a finished draft as active; the message carries the capture notes. Raises
    `WriteRefused`."""
    target = resolve_write_target(store, doc_id)
    # held from the first look at the document to the replace: two completions of one
    # draft can no longer both see `status: draft` and both commit
    with locked(target, doc_id, surface):
        doc = _complete_draft_locked(target, doc_id, content, expected_version, surface)

    message = f"completed sessions/{doc_id}.md (status: draft -> active)"
    for note in [*_snapshot_notes(doc), *_citation_notes(store, doc, surface)]:
        message += f"\n{note}"
    return message


def _complete_draft_locked(
    target: Path, doc_id: str, content: str, expected_version: str | None, surface: Surface
) -> Doc:
    if not target.exists():
        raise WriteRefused(
            f"no draft found for id {doc_id!r}: sessions/{doc_id}.md does not "
            f"exist — create it first with {surface.create}."
        )
    try:
        existing = parse_document(target)
    except (yaml.YAMLError, ValueError, OSError) as exc:
        # can't confirm the current document is actually a draft; refuse
        raise WriteRefused(
            f"sessions/{doc_id}.md does not parse ({exc}) — refusing to "
            "overwrite a document that cannot first be confirmed to be a draft."
        ) from exc
    if existing.status != "draft":
        raise WriteRefused(
            f"refusing to overwrite sessions/{doc_id}.md: its current status "
            f"is {existing.status!r}, not 'draft' — {surface.complete} "
            "only ever finishes a document still in draft."
        )
    refuse_unless_version_read(
        target,
        doc_id,
        expected_version,
        surface,
        action="complete",
        tool=surface.complete,
        redo="carry its changes into your content",
    )

    doc, tmp_path, parse_error = stage_content(target, content.encode("utf-8"))
    try:
        if parse_error is not None:
            raise WriteRefused(f"sessions/{doc_id}.md {parse_error}")
        if doc.id != doc_id:
            raise WriteRefused(
                f"content's front matter id {doc.id!r} does not match the "
                f"{surface.id_argument} {doc_id!r} — they must be identical."
            )
        if doc.status != "active":
            raise WriteRefused(
                f"content's front matter status is {doc.status!r}, not "
                f"'active' — {surface.complete} must complete the "
                "sanctioned draft -> active transition; leave status as "
                f"'draft' and simply {surface.again} later if the "
                "document is not actually ready to finish yet."
            )
        _refuse_a_changed_observation(existing, doc, doc_id)
        rejections = _reuse_log_rejections(doc, target.parent)
        if rejections:
            raise WriteRefused(
                "\n".join(
                    [
                        f"refusing to complete sessions/{doc_id}.md: its Reuse Log "
                        "fails the mechanical rules Gate 1 counts by — fix the rows "
                        f"below in the content and {surface.again}.",
                        *rejections,
                    ]
                )
            )
        # the lock excludes engmem writers only; a hand edit or another tool landing since the
        # read above is caught here, in a window the lock cannot close
        refuse_if_changed_since_read(target, existing, doc_id, surface.again.capitalize())
        commit(tmp_path, target)
    # BaseException, not WriteRefused: `commit` chmods and replaces, so it can raise
    # OSError of its own, and the staged file must go either way
    except BaseException:
        discard(tmp_path)
        raise
    return doc


def _mode_label(mode: str | None) -> str:
    return "no mode (written before modes existed)" if mode is None else repr(mode)


def _refuse_a_changed_observation(draft: Doc, content: Doc, doc_id: str) -> None:
    """A session keeps the condition it started under (AC-08.4), and a missing baseline once
    recorded cannot be dropped at save time (AC-08.5)."""
    if content.mode != draft.mode:
        raise WriteRefused(
            f"refusing to complete sessions/{doc_id}.md: content's mode is "
            f"{_mode_label(content.mode)}, but the draft was started with "
            f"{_mode_label(draft.mode)} — a session keeps the mode it started in; copy "
            "`mode` from the draft unchanged."
        )
    if draft.baseline_unavailable is not None and content.baseline_unavailable is None:
        raise WriteRefused(
            f"refusing to complete sessions/{doc_id}.md: the draft records "
            f"baseline_unavailable ({draft.baseline_unavailable!r}) and the content drops it — "
            "a baseline cannot be supplied after the search; copy `baseline_unavailable` "
            "from the draft unchanged."
        )


CLASSIFICATION_CHOICES = " / ".join(
    c for c in gate1.Classification if c in gate1.VALID_CLASSIFICATIONS
)
REUSE_LOG_TABLE_HEADER = "| " + " | ".join(gate1.REUSE_LOG_HEADER) + " |"
REUSE_LOG_SHAPE = (
    f"Reuse Log: one `{REUSE_LOG_TABLE_HEADER}` row per prior document — prior-doc is the search "
    f"result's `cite as` id@version, taken quotes the span used, classification is "
    f"{CLASSIFICATION_CHOICES} — or exactly `Prior docs used: none.`"
)


def _reuse_log_rejections(doc: Doc, sessions_dir: Path) -> list[str]:
    """Row-shape defects decidable from the content, checked before the commit; the store is read
    only to spare an existing id spelled with `@` (contracts/mcp-server.md, "Reuse Log rows —
    refuse, then warn")."""
    rejections: list[str] = []
    existing_ids: set[str] | None = None
    for section in split_sections(doc.body):
        if section.canonical != "reuse":
            continue
        headers = gate1.reuse_log_headers(section.body)
        for header in headers:
            if gate1.header_misplaces_cells(header.cells):
                rejections.append(
                    f"Reuse Log table rejected — its columns must come in the order "
                    f"`{REUSE_LOG_TABLE_HEADER}`, because the count reads each row's cells by position: "
                    f"{header.line.strip()}"
                )
        header_lines = {h.line for h in headers}
        rows = gate1.reuse_log_rows(section.body)
        if not rows and gate1.NONE_LINE not in section.body.casefold():
            rejections.append(
                f"Reuse Log rejected — `{section.heading}` has neither a table row nor the line "
                "`Prior docs used: none.`; the count reads only rows of a "
                f"`{REUSE_LOG_TABLE_HEADER}` table, so a list or prose here is never counted."
            )
        for row in rows:
            if row.line in header_lines:
                continue
            line = row.line.strip()
            reference = row.cells[0].strip("[] `")
            _cited_id, version = split_reference(reference)
            if version is not None and not is_version(version) and existing_ids is None:
                existing_ids = {d.id for d in load_store(sessions_dir).docs}
            # `gate1.cited_reference`'s rule: a cell naming an existing id exactly is that id
            if version is not None and not is_version(version) and reference not in existing_ids:
                rejections.append(
                    f"Reuse Log row rejected — version {version!r} is not one a search showed "
                    "(prior-doc is <id> or <id>@<16 lowercase hex digits>, copied from the "
                    f"search result's `cite as` line): {line}"
                )
            if not gate1.quotes_in(row.cells[1]):
                rejections.append(
                    "Reuse Log row rejected — no quoted span of four or more characters: "
                    f"{line}"
                )
            raw, classification = gate1.classification_of(row.cells)
            if classification is gate1.Classification.MISSING:
                rejections.append(
                    "Reuse Log row rejected — its fourth cell, classification, is missing or "
                    f"empty (expected one of {CLASSIFICATION_CHOICES} under "
                    f"`{REUSE_LOG_TABLE_HEADER}`): {line}"
                )
            elif classification is gate1.Classification.UNRECOGNIZED:
                rejections.append(
                    f'Reuse Log row rejected — classification "{raw}" is not one of '
                    f"{CLASSIFICATION_CHOICES}: {line}"
                )
    return rejections


def _snapshot_notes(doc: Doc) -> list[str]:
    """A malformed anchor, named once at capture and never a refusal; see contracts/provenance.md,
    "Recorded at capture"."""
    return [
        f"warning: sessions/{doc.id}.md: {warning}"
        for warning in doc.field_warnings
        if warning.startswith("verified_at")
    ]


def _citation_notes(store: Path, doc: Doc, surface: Surface) -> list[str]:
    """Non-fatal diagnostics on the document just completed to active — never demotes a
    successful commit; see contracts/mcp-server.md, "Reuse Log rows — refuse, then warn"."""
    notes: list[str] = []
    if not any(s.canonical == "reuse" for s in split_sections(doc.body)):
        notes.append(
            f'warning: sessions/{doc.id}.md has no Reuse Log section — it is now active, '
            'so gate1_audit counts it under "active documents missing a Reuse Log section"'
        )
        return notes

    try:
        verdicts = gate1.evaluate(store)
    except Exception as exc:
        notes.append(f"note: citation check did not run ({exc})")
        return notes

    problems: list[str] = []
    legacy: list[str] = []
    for row in verdicts.rows:
        if row.citing.id != doc.id:
            continue
        if gate1.checked_against_current_text(row):
            legacy.append(row.source)
        if row.integrity == gate1.Integrity.VERSION_UNAVAILABLE:
            problems.append(
                f"{row.source}: cited version {gate1.cited_as_written(row)} cannot be "
                f"checked — {row.version_problem}"
            )
        elif row.integrity == gate1.Integrity.CITED_MISSING:
            problems.append(
                f"{row.source}: cited document {gate1.cited_as_written(row)} is not in the "
                "store — prior-doc takes the document's id, not its filename"
            )
        elif row.integrity == gate1.Integrity.QUOTE_NOT_FOUND:
            for quote in row.unfound_quotes:
                problems.append(
                    f'{row.source}: quote not found in {gate1.cited_as_written(row)} — "{quote}"'
                )
    if problems:
        notes.append(
            f"warning: {len(problems)} Reuse Log citation problem(s) — run the engmem "
            "checkout's checker, uv run --project <engmem-checkout> python "
            "<engmem-checkout>/tools/verify_citations.py --store <store>: " + "; ".join(problems)
        )
    if legacy:
        notes.append(
            f"note: {len(legacy)} Reuse Log row(s) name no version, so their quotes are checked "
            "against the cited document's current text, which can change later: "
            + ", ".join(legacy)
            + f" — next time cite the `<id>@<version>` the {surface.search} result's `cite as` "
            "line gives"
        )
    notes.extend(_unlisted_reads_notes(store, doc.id, verdicts))
    return notes


def _unlisted_reads_notes(store: Path, doc_id: str, verdicts: gate1.Verdicts) -> list[str]:
    """A none Reuse Log in a session that read citable documents, named after publishing and
    never refused; see contracts/mcp-server.md, "Reuse Log rows — refuse, then warn"."""
    if doc_id not in verdicts.none_reports and doc_id not in verdicts.none_reports_kept_apart:
        return []
    try:
        ids = cost.read_doc_ids(store, doc_id)
        docs = load_store(store / "sessions").docs if ids else []
    except (OSError, UnicodeDecodeError) as exc:
        return [f"note: reads check did not run ({exc})"]
    reads = feedback.citable_reads(doc_id, ids, docs)
    if not reads:
        return []
    return [
        "warning: Reuse Log says none, but this session read: "
        + ", ".join(_escape_controls(r) for r in reads)
        + " — if one changed a decision, the published Reuse Log is missing its row; tell the "
        "user, who can add it"
    ]
