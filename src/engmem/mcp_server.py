"""A stdio MCP server for engmem; stdout carries JSON-RPC frames and nothing else."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, TextIO

import yaml

from engmem import __version__, cost, feedback, gate1
from engmem.cache import identity_for
from engmem.read_report import ReadError, compose_read
from engmem.settings import Mode, ModeSettingError, effective_mode, mode_setting_file, saved_mode
from engmem.scoring import Scope
from engmem.search_report import compose
from engmem.sections import CANONICAL_ROLES, split_sections
# imported, not reimplemented, so a document a write tool below produces is
# validated by the exact rules load_store applies when reading it back
from engmem.spine import (
    Doc,
    LoadResult,
    load_store,
    parse_document,
    sessions_dir_unreadable,
    split_front_matter,
    stray_documents,
    validate_doc_id,
)
from engmem.staging import (
    DocumentLockedError,
    commit,
    commit_new,
    discard,
    document_lock,
    newline_of,
    read_document,
    stage,
    version_of,
)
from engmem.versions import is_version, split_reference

PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 reserved error codes (https://www.jsonrpc.org/specification#error_object)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOOL_NAME = "engmem_search"
TOOL_DESCRIPTION = (
    "Search prior engineering session documents recorded by engmem — decisions, "
    "pitfalls, and cold-start context captured from past work in this codebase. "
    "Call this before planning or implementing any non-trivial engineering task, "
    "with the key terms of the task as the query. Returns the best-matching "
    "documents (or the sentence 'prior context: none found' if nothing matched) "
    "plus a one-line summary of how many documents the store holds. Whenever a "
    "draft session document exists for the current task, pass its id as "
    "`session_id` — without it this retrieval cannot be tied to the document "
    "that reuses it."
)

ROLE_TOOL_NAME = "engmem_search_by_role"
ROLE_TOOL_DESCRIPTION = (
    "Retrieve a specific kind of section — e.g. Decision Log, Lessons Learned, "
    "Production Considerations — from across the most relevant prior engineering "
    "session documents, instead of the single best word-matching passage "
    "`engmem_search` returns. Reach for this when the question is about a "
    "document's STRUCTURE rather than its subject matter — questions like 'what "
    "did we reject and why', 'what broke in production', or 'what should I have "
    "known going in' — because a plain keyword search misses those: the matching "
    "sections discuss different subjects and share no vocabulary with each other, "
    "only their role in the document. Documents are still ranked by the query's "
    "words, exactly as `engmem_search` ranks them; this tool then returns that "
    "role's section from each of the top-ranked documents that actually has one. "
    "A document lacking the requested role is skipped, never padded with the "
    "wrong section. Call `engmem_search` first for an ordinary content question; "
    "reach for this tool instead when the question names a kind of section, not "
    "a topic. Whenever a draft session document exists for the current task, pass "
    "its id as `session_id` — without it this retrieval cannot be tied to the "
    "document that reuses it."
)

# ---------------------------------------------------------------------------
# write tools — the save half of the workflow for a shell-less runtime (Claude Desktop): create a
# draft, finish it (draft -> active), mark an earlier document superseded.
CREATE_DRAFT_TOOL_NAME = "engmem_create_draft"
CREATE_DRAFT_TOOL_DESCRIPTION = (
    "Create a new engmem session document as a draft — the first step of "
    "`/engmem` in a runtime with no shell access. Writes `sessions/<id>.md` "
    "with exactly the `content` given: the complete YAML front matter block "
    "followed by the body. The front matter's `status` MUST be `draft`, its "
    "`id` MUST equal the `id` argument, and its `mode` MUST be the configured "
    "mode (`daily` or `research`; the `engmem` prompt names it) — this tool "
    "refuses to create anything else and names the configured mode when it does. "
    "A `research` draft also carries the `## Pre-reg` baseline, or "
    "`baseline_unavailable: <reason>` when none could be had; a `daily` draft "
    "needs no Pre-reg. NEVER overwrites: "
    "if `sessions/<id>.md` already exists, the call fails and nothing is "
    "written, regardless of what the existing file contains. Use "
    "`engmem_complete_draft` to finish a draft this tool already created. The "
    "result names the draft's version: keep it and pass it as `expected_version` "
    "when you complete the draft."
)

COMPLETE_DRAFT_TOOL_NAME = "engmem_complete_draft"
COMPLETE_DRAFT_TOOL_DESCRIPTION = (
    "Finish an existing draft — the last step of `/engmem.save` (or "
    "`/engmem.save.quick`) in a runtime with no shell access. Replaces "
    "`sessions/<id>.md` with the complete new `content` (front matter + full "
    "body): the ONLY sanctioned use is completing a document `engmem_create_draft` "
    "made. This tool refuses to run unless `sessions/<id>.md` already exists AND "
    "its current front matter `status` is `draft`, and refuses the new `content` "
    "unless its front matter `status` is `active` and its `id` equals the `id` "
    "argument. It will never touch a document that is already `active` or "
    "`superseded` — a confused or repeated call cannot destroy work outside a "
    "document still in draft. It also refuses `content` whose Reuse Log holds a "
    "row without a quoted span or with a classification other than `reuse` / "
    "`anti-reuse` / `harmful`, returning each offending row so it can be fixed "
    "in the same turn. To mark a DIFFERENT, already-finished document as "
    "superseded by this one, call `engmem_mark_superseded` instead — never pass "
    "that document's id here. Always pass `expected_version`, the version "
    "`engmem_create_draft` returned for this draft: if the draft changed since "
    "then, the call is refused and its result carries the current draft and its "
    "version — re-read it, carry its changes into `content`, and call again with "
    "that version. Without `expected_version` only the draft's status is checked, "
    "so a change to a draft that is still a draft is overwritten."
)

MARK_SUPERSEDED_TOOL_NAME = "engmem_mark_superseded"
MARK_SUPERSEDED_TOOL_DESCRIPTION = (
    "Mark an earlier, already-`active` engmem session document as superseded by "
    "a newer one — the supersede-check step of `/engmem.save`. Takes only the "
    "two ids involved, `id` (the earlier document) and `superseded_by` (the "
    "document that replaces it); every other line of `sessions/<id>.md` — body, "
    "other front matter fields — is left exactly as it was. Refuses to run "
    "unless `sessions/<id>.md` exists and its current `status` is `active` (never "
    "a draft, never an already-superseded document, and never itself: `id` and "
    "`superseded_by` must differ). This does not require `superseded_by` to "
    "already exist as a document — it is fine to mark the earlier document first."
)


RECORD_FEEDBACK_TOOL_NAME = "engmem_record_feedback"
RECORD_FEEDBACK_TOOL_DESCRIPTION = (
    "Record the USER's assessment of one document a search showed in this session: "
    "`helped`, `not-applicable` or `harmful`, optionally with the decision it changed and "
    "where that can be seen. Call it only with an assessment the user stated in this "
    "conversation -- never your own judgement, and never to fill in one the user did not "
    "give; an unassessed find is reported as unknown influence, which is not a failure. "
    "Refuses unless `session_id` names a session document in the store and a search "
    "attributed to it showed `doc_id`. Appends to the store's feedback log; no session "
    "document is changed and the Gate 1 count does not read it."
)


READ_TOOL_NAME = "engmem_read"
READ_TOOL_DESCRIPTION = (
    "Read one published engmem session document by id -- the whole body, or with `role` only "
    "its sections of that role (e.g. 'decisions'). Use it to load a document a search showed: "
    "the text it returns is counted toward this session's observed cost, while a document "
    "opened any other way is not observed. Refuses a draft and an id with no document. "
    "Whenever a draft session document exists for the current task, pass its id as "
    "`session_id`, as on `engmem_search`."
)

RECORD_BASELINE_TOOL_NAME = "engmem_record_baseline"
RECORD_BASELINE_TOOL_DESCRIPTION = (
    "Record the tokens and/or seconds your runtime REPORTED for the no-memory baseline call of "
    "this session (the sub-agent of the Pre-reg step), with the runtime's id for that call when "
    "it gives one. Only figures the runtime reported -- never an estimate or a count of your "
    "own; with nothing reported, do not call it, and the cost summary shows the baseline as "
    "missing. Refuses unless `session_id` names a session document in the store. Appends to "
    "the store's cost log; no session document is changed."
)


# prompt name -> source template filename under src/engmem/templates/ — the
# same files install.py's _install_templates copies out; a second reader of
# that one source of truth, not a fork of it
START_PROMPT_NAME = "engmem"
PROMPT_TEMPLATES = {
    START_PROMPT_NAME: "engmem.start.md",
    "engmem-save": "engmem.save.md",
    "engmem-save-quick": "engmem.save.quick.md",
}

_WHITESPACE_RE = re.compile(r"\s+")
_ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"


class _ProtocolError(Exception):
    """Raised to produce a JSON-RPC error response, never for a tool-level failure."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _ToolError(Exception):
    """Raised by the write-tool helpers for a failure the security contract calls for."""


# ---------------------------------------------------------------------------
# template loading — shared by prompts/list and prompts/get
# ---------------------------------------------------------------------------


def _load_template(source_name: str) -> tuple[str, Any, str]:
    """`(description, argument_hint, body)`, with the front matter removed from body."""
    content = (
        (resources.files("engmem") / "templates" / source_name).read_text(encoding="utf-8")
    )
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", None, content

    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return "", None, content

    front_matter_text = "".join(lines[1:closing])
    body = "".join(lines[closing + 1 :]).lstrip("\n")

    front = yaml.safe_load(front_matter_text) or {}
    if not isinstance(front, dict):
        front = {}
    description = str(front.get("description", ""))
    argument_hint = front.get("argument-hint")
    return description, argument_hint, body


def _argument_name_from_hint(hint: str) -> str:
    # strips engmem's own <angle-bracket> wrapping, then turns the words into
    # an identifier a JSON-RPC client can pass by name
    cleaned = hint.strip()
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1].strip()
    return _WHITESPACE_RE.sub("_", cleaned) or "argument"


# ---------------------------------------------------------------------------
# store access for the tool — the text itself is search_report.compose's, shared with the CLI
# ---------------------------------------------------------------------------


class _StoreLoadError(Exception):
    """Raised when the store itself cannot be read at all."""


def _load_store_for_tool(store: Path) -> tuple[LoadResult, list[Path], list[str]]:
    """Loads `sessions/`, raising `_StoreLoadError` rather than returning on a broken store."""
    sessions_dir = store / "sessions"

    unreadable = sessions_dir_unreadable(sessions_dir)
    if unreadable:
        raise _StoreLoadError(f"error: {unreadable}")
    if not sessions_dir.is_dir():
        raise _StoreLoadError(
            f"error: store not found: {sessions_dir} does not exist "
            "(run `engmem install` to create the store, or check the configured "
            "store path)."
        )

    result = load_store(sessions_dir)
    for problem in result.errors:
        print(f"engmem-mcp: {problem.message}", file=sys.stderr)
    for problem in result.warnings:
        print(f"engmem-mcp: {problem.message}", file=sys.stderr)

    try:
        strays, stray_scan_errors = stray_documents(store)
    except OSError as exc:
        # distinct from load_store's handling above: the store root itself
        # being unscannable, not sessions/
        raise _StoreLoadError(
            f"error: could not scan {store} for stray markdown files: {exc}"
        ) from exc

    return result, strays, stray_scan_errors


def _run_search_for_tool(
    store: Path,
    query: str,
    session_id: str | None = None,
    role: str | None = None,
    scope: Scope | None = None,
    read_only: bool = False,
) -> tuple[str, bool]:
    """`(text, is_error)`: the composed result the CLI prints, carried in the tool result."""
    try:
        result, strays, stray_scan_errors = _load_store_for_tool(store)
    except _StoreLoadError as exc:
        return str(exc), True

    for scan_error in stray_scan_errors:
        print(f"engmem-mcp: {scan_error}", file=sys.stderr)
    # channel="mcp" keeps a Desktop search and a terminal search distinguishable in
    # telemetry.jsonl; compose writes the row, so the MCP path is never invisible to Gate 1
    text = compose(
        store, result, strays, stray_scan_errors, query, role, session_id, "mcp", scope,
        retain_versions=not read_only, record_cost=not read_only,
    )
    return text, False


# ---------------------------------------------------------------------------
# write tools — path containment, atomic staged writes, and the three handlers.
# ---------------------------------------------------------------------------


def _resolve_sessions_dir(store: Path) -> Path:
    """Resolved `store/sessions`, raising `_ToolError` when it is missing or escapes the store."""
    sessions_dir = store / "sessions"
    unreadable = sessions_dir_unreadable(sessions_dir)
    if unreadable:
        raise _ToolError(unreadable)
    if not sessions_dir.is_dir():
        raise _ToolError(
            f"store not found: {sessions_dir} does not exist (run `engmem install` "
            "to create the store, or check the configured store path)."
        )
    try:
        resolved_store = store.resolve(strict=False)
        resolved_sessions = sessions_dir.resolve(strict=True)
    except OSError as exc:
        raise _ToolError(f"could not resolve store path {store}: {exc}") from exc

    if resolved_sessions != resolved_store / "sessions":
        raise _ToolError(
            f"refusing to write: {sessions_dir} resolves to {resolved_sessions}, "
            f"outside {resolved_store} — sessions/ appears to be a symlink (or the "
            "store path contains one) pointing somewhere the store does not own."
        )
    return resolved_sessions


def _resolve_write_target(store: Path, doc_id: object) -> Path:
    """The chokepoint every write tool calls first; never returns a path outside `sessions/`."""
    reason = validate_doc_id(doc_id)
    if reason is not None:
        raise _ToolError(f"invalid document id {doc_id!r}: {reason}")

    resolved_sessions = _resolve_sessions_dir(store)
    target = resolved_sessions / f"{doc_id}.md"

    # checked before resolve(), which would follow a symlink to its target
    # and hide the real problem behind the containment check below
    if target.is_symlink():
        raise _ToolError(
            f"refusing to write through a symlink: sessions/{doc_id}.md is a "
            "symlink, not a plain file — this tool never follows or replaces one."
        )

    resolved_target = target.resolve(strict=False)
    if resolved_target.parent != resolved_sessions or resolved_target.name != f"{doc_id}.md":
        raise _ToolError(f"refusing to write outside sessions/: {doc_id!r}")

    return target


def _stage_content(target: Path, content: bytes) -> tuple[Doc | None, Path, str | None]:
    """Stages `content` and parses it; the caller commits or discards what comes back."""
    tmp_path = stage(target, content)
    try:
        doc = parse_document(tmp_path)
    except (yaml.YAMLError, ValueError, OSError) as exc:
        return None, tmp_path, f"content does not parse: {exc}"
    return doc, tmp_path, None


def _create_conflict(doc_id: str, detail: str | None = None) -> _ToolError:
    reason = detail or "it already exists"
    return _ToolError(
        f"refusing to create sessions/{doc_id}.md: {reason} — "
        f"{CREATE_DRAFT_TOOL_NAME} never overwrites. Use "
        f"{COMPLETE_DRAFT_TOOL_NAME} to finish an existing draft, or choose a different id."
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


def _handle_create_draft(arguments: object, store: Path) -> tuple[str, bool]:
    doc_id = arguments.get("id") if isinstance(arguments, dict) else None
    content = arguments.get("content") if isinstance(arguments, dict) else None

    try:
        target = _resolve_write_target(store, doc_id)

        if target.exists():
            raise _create_conflict(doc_id)

        content_bytes = content.encode("utf-8")
        doc, tmp_path, parse_error = _stage_content(target, content_bytes)
        try:
            if parse_error is not None:
                raise _ToolError(f"sessions/{doc_id}.md {parse_error}")
            if doc.id != doc_id:
                raise _ToolError(
                    f"content's front matter id {doc.id!r} does not match the "
                    f"'id' argument {doc_id!r} — they must be identical."
                )
            if doc.status != "draft":
                raise _ToolError(
                    f"content's front matter status is {doc.status!r}, not "
                    f"'draft' — {CREATE_DRAFT_TOOL_NAME} only ever creates a draft."
                )
            mode_refusal = _mode_refusal(doc)
            if mode_refusal is not None:
                raise _ToolError(
                    f"refusing to create sessions/{doc_id}.md: {mode_refusal}"
                )
            try:
                commit_new(tmp_path, target)
            except FileExistsError as exc:
                # another creator took the id after the check above
                detail = None if os.path.lexists(target) else exc.strerror
                raise _create_conflict(doc_id, detail) from exc
        # BaseException, not _ToolError: `commit_new` links or renames, so it can raise
        # OSError of its own, and the staged file must go either way
        except BaseException:
            discard(tmp_path)
            raise
    except _ToolError as exc:
        return str(exc), True

    return (
        f"created sessions/{doc_id}.md (status: draft, version: {version_of(content_bytes)}). "
        f'Now pass session_id: "{doc_id}" on every {TOOL_NAME} and {ROLE_TOOL_NAME} call '
        "for the rest of this task — a search without it is logged unattributed "
        "and drops out of the analysis. Keep the version: pass it as expected_version "
        f"to {COMPLETE_DRAFT_TOOL_NAME} when you finish this draft. {_mode_note(doc)}"
    ), False


def _refuse_if_changed_since_read(target: Path, read: Doc, doc_id: str) -> None:
    """`backfill.apply_backfill`'s rule: a document that moved under the read a write is based
    on is refused, never overwritten from the copy that is already stale."""
    if read.source_identity != identity_for(target):
        raise _ToolError(
            f"sessions/{doc_id}.md changed on disk while it was being read — "
            "refusing to write, so a newer version is not replaced by a stale one. "
            "Call this tool again."
        )


@contextmanager
def _locked(target: Path, doc_id: str) -> Iterator[None]:
    """`staging.document_lock`, with a lock held past the wait refused by name."""
    try:
        with document_lock(target):
            yield
    except DocumentLockedError as exc:
        raise _ToolError(
            f"sessions/{doc_id}.md is being written by another engmem call — nothing was "
            "written; call this tool again. If this repeats while no engmem process is "
            f"running, an interrupted write left sessions/{exc.lock.name} behind: remove it."
        ) from None


def _refuse_if_not_the_version_read(target: Path, doc_id: str, expected_version: str) -> None:
    """A draft that changed since the client read it is refused with its current text and
    version, never overwritten from the client's stale copy."""
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise _ToolError(
            f"sessions/{doc_id}.md could not be re-read ({exc}) — nothing was written."
        ) from exc
    current = version_of(data)
    if current == expected_version.strip():
        return
    raise _ToolError(
        f"refusing to complete sessions/{doc_id}.md: it changed since you read it "
        f"(expected_version {expected_version!r}, current version {current!r}) — "
        "nothing was written. Re-read the current draft below, carry its changes into "
        f"your content, and call {COMPLETE_DRAFT_TOOL_NAME} again with "
        f'expected_version: "{current}".\n'
        f"--- current sessions/{doc_id}.md ---\n"
        + data.decode("utf-8", errors="replace")
    )


def _handle_complete_draft(arguments: object, store: Path) -> tuple[str, bool]:
    doc_id = arguments.get("id") if isinstance(arguments, dict) else None
    content = arguments.get("content") if isinstance(arguments, dict) else None
    expected_version = arguments.get("expected_version") if isinstance(arguments, dict) else None

    try:
        target = _resolve_write_target(store, doc_id)
        # held from the first look at the document to the replace: two completions of one
        # draft can no longer both see `status: draft` and both commit
        with _locked(target, doc_id):
            doc = _complete_draft_locked(target, doc_id, content, expected_version)
    except _ToolError as exc:
        return str(exc), True

    message = f"completed sessions/{doc_id}.md (status: draft -> active)"
    for note in [*_snapshot_notes(doc), *_citation_notes(store, doc)]:
        message += f"\n{note}"
    return message, False


def _complete_draft_locked(
    target: Path, doc_id: str, content: str, expected_version: str | None
) -> Doc:
    if not target.exists():
        raise _ToolError(
            f"no draft found for id {doc_id!r}: sessions/{doc_id}.md does not "
            f"exist — create it first with {CREATE_DRAFT_TOOL_NAME}."
        )
    try:
        existing = parse_document(target)
    except (yaml.YAMLError, ValueError, OSError) as exc:
        # can't confirm the current document is actually a draft; refuse
        raise _ToolError(
            f"sessions/{doc_id}.md does not parse ({exc}) — refusing to "
            "overwrite a document that cannot first be confirmed to be a draft."
        ) from exc
    if existing.status != "draft":
        raise _ToolError(
            f"refusing to overwrite sessions/{doc_id}.md: its current status "
            f"is {existing.status!r}, not 'draft' — {COMPLETE_DRAFT_TOOL_NAME} "
            "only ever finishes a document still in draft."
        )
    if expected_version is not None:
        _refuse_if_not_the_version_read(target, doc_id, expected_version)

    doc, tmp_path, parse_error = _stage_content(target, content.encode("utf-8"))
    try:
        if parse_error is not None:
            raise _ToolError(f"sessions/{doc_id}.md {parse_error}")
        if doc.id != doc_id:
            raise _ToolError(
                f"content's front matter id {doc.id!r} does not match the "
                f"'id' argument {doc_id!r} — they must be identical."
            )
        if doc.status != "active":
            raise _ToolError(
                f"content's front matter status is {doc.status!r}, not "
                f"'active' — {COMPLETE_DRAFT_TOOL_NAME} must complete the "
                "sanctioned draft -> active transition; leave status as "
                "'draft' and simply call this tool again later if the "
                "document is not actually ready to finish yet."
            )
        _refuse_a_changed_observation(existing, doc, doc_id)
        rejections = _reuse_log_rejections(doc, target.parent)
        if rejections:
            raise _ToolError(
                "\n".join(
                    [
                        f"refusing to complete sessions/{doc_id}.md: its Reuse Log "
                        "fails the mechanical rules Gate 1 counts by — fix the rows "
                        "below in the content and call this tool again.",
                        *rejections,
                    ]
                )
            )
        # the lock excludes engmem writers only; a hand edit or another tool landing since the
        # read above is caught here, in a window the lock cannot close
        _refuse_if_changed_since_read(target, existing, doc_id)
        commit(tmp_path, target)
    # BaseException, not _ToolError: `commit` chmods and replaces, so it can raise
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
        raise _ToolError(
            f"refusing to complete sessions/{doc_id}.md: content's mode is "
            f"{_mode_label(content.mode)}, but the draft was started with "
            f"{_mode_label(draft.mode)} — a session keeps the mode it started in; copy "
            "`mode` from the draft unchanged."
        )
    if draft.baseline_unavailable is not None and content.baseline_unavailable is None:
        raise _ToolError(
            f"refusing to complete sessions/{doc_id}.md: the draft records "
            f"baseline_unavailable ({draft.baseline_unavailable!r}) and the content drops it — "
            "a baseline cannot be supplied after the search; copy `baseline_unavailable` "
            "from the draft unchanged."
        )


_CLASSIFICATION_CHOICES = " / ".join(
    c for c in gate1.Classification if c in gate1.VALID_CLASSIFICATIONS
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
        for row in gate1.reuse_log_rows(section.body):
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
                    "Reuse Log row rejected — no classification cell, or an empty one "
                    f"(expected one of {_CLASSIFICATION_CHOICES}): {line}"
                )
            elif classification is gate1.Classification.UNRECOGNIZED:
                rejections.append(
                    f'Reuse Log row rejected — classification "{raw}" is not one of '
                    f"{_CLASSIFICATION_CHOICES}: {line}"
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


def _citation_notes(store: Path, doc: Doc) -> list[str]:
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
            + f" — next time cite the `<id>@<version>` the {TOOL_NAME} result's `cite as` "
            "line gives"
        )
    return notes


def _handle_mark_superseded(arguments: object, store: Path) -> tuple[str, bool]:
    doc_id = arguments.get("id") if isinstance(arguments, dict) else None
    superseded_by = arguments.get("superseded_by") if isinstance(arguments, dict) else None

    try:
        target = _resolve_write_target(store, doc_id)

        reason = validate_doc_id(superseded_by)
        if reason is not None:
            raise _ToolError(f"invalid 'superseded_by' id {superseded_by!r}: {reason}")
        if superseded_by == doc_id:
            raise _ToolError(
                f"'superseded_by' must name a different document than 'id' "
                f"(both are {doc_id!r})."
            )

        # the same lock `engmem_complete_draft` holds: two supersedes of one document can no
        # longer both see `status: active`, and the later one replace the earlier's successor
        with _locked(target, doc_id):
            _mark_superseded_locked(target, doc_id, superseded_by)
    except _ToolError as exc:
        return str(exc), True

    return (
        f"marked sessions/{doc_id}.md superseded by {superseded_by} "
        "(status: active -> superseded)"
    ), False


def _mark_superseded_locked(target: Path, doc_id: str, superseded_by: str) -> None:
    if not target.exists():
        raise _ToolError(
            f"no document found for id {doc_id!r}: sessions/{doc_id}.md does "
            "not exist."
        )
    try:
        # the validating parse first, and it stats before it reads, so its
        # `source_identity` covers the raw re-read below as well
        existing = parse_document(target)
    except (yaml.YAMLError, ValueError, OSError) as exc:
        raise _ToolError(
            f"sessions/{doc_id}.md does not parse ({exc}) — refusing to "
            "modify a document that cannot first be confirmed to be active."
        ) from exc
    if existing.status != "active":
        raise _ToolError(
            f"refusing to supersede sessions/{doc_id}.md: its current status "
            f"is {existing.status!r}, not 'active' — a draft was never "
            "published, and an already-superseded document does not get "
            "superseded twice."
        )

    try:
        # bytes, not `read_text`: this handler rebuilds the whole file from what it reads,
        # and `read_text` drops the BOM and translates every line ending on the way in —
        # see contracts/backfill.md, "Line endings"
        existing_text, bom = read_document(target)
    # ValueError as well as OSError: `read_document` decodes what it read, so a document
    # replaced with invalid UTF-8 between the two reads raises `UnicodeDecodeError` here.
    # That is the document's problem, refused as one — not a -32603 server fault
    except (OSError, ValueError) as exc:
        raise _ToolError(
            f"sessions/{doc_id}.md could not be re-read ({exc}) — nothing was written."
        ) from exc
    _refuse_if_changed_since_read(target, existing, doc_id)

    front_matter_text, body = split_front_matter(existing_text)
    front_matter_text = _patch_front_matter_line(front_matter_text, "status", "superseded")
    front_matter_text = _patch_front_matter_line(
        front_matter_text, "superseded_by", superseded_by
    )
    newline = newline_of(front_matter_text) or newline_of(body) or "\n"
    new_content = f"---{newline}" + front_matter_text + f"---{newline}" + body

    doc, tmp_path, parse_error = _stage_content(target, bom + new_content.encode("utf-8"))
    try:
        if parse_error is not None:
            raise _ToolError(f"sessions/{doc_id}.md {parse_error}")
        if doc.status != "superseded" or doc.superseded_by != superseded_by:
            raise _ToolError(
                "internal error: patched front matter did not produce the "
                "expected status/superseded_by — refusing to write it."
            )
        commit(tmp_path, target)
    # BaseException, not _ToolError: `commit` chmods and replaces, so it can raise
    # OSError of its own, and the staged file must go either way
    except BaseException:
        discard(tmp_path)
        raise


def _handle_record_feedback(arguments: object, store: Path) -> tuple[str, bool]:
    try:
        result, _, _ = _load_store_for_tool(store)
        entry = feedback.record(
            store, {d.id for d in result.docs},
            session_id=arguments["session_id"], doc_id=arguments["doc_id"],
            assessment=arguments["assessment"], decision=arguments.get("decision"),
            source=arguments.get("source"), channel="mcp",
        )
    except (_StoreLoadError, feedback.FeedbackError) as exc:
        return str(exc), True
    return feedback.confirmation(entry), False


def _optional_number(arguments: dict, field_name: str, *, integer: bool) -> object:
    value = arguments.get(field_name)
    allowed = (int,) if integer else (int, float)
    if value is not None and (isinstance(value, bool) or not isinstance(value, allowed)):
        kind = "an integer" if integer else "a number"
        raise _ProtocolError(INVALID_PARAMS, f"invalid params: {field_name!r} must be {kind}")
    return value


def _handle_record_baseline(arguments: object, store: Path) -> tuple[str, bool]:
    tokens = _optional_number(arguments, "tokens", integer=True)
    seconds = _optional_number(arguments, "seconds", integer=False)
    try:
        result, _, _ = _load_store_for_tool(store)
        entry = cost.record_baseline(
            store, {d.id for d in result.docs}, session_id=arguments["session_id"],
            tokens=tokens, seconds=seconds, call_id=arguments.get("call_id"), channel="mcp",
        )
    except (_StoreLoadError, cost.CostError) as exc:
        return str(exc), True
    return cost.confirmation(entry), False


def _handle_read(arguments: object, store: Path) -> tuple[str, bool]:
    role = arguments.get("role")
    if role is not None and role not in CANONICAL_ROLES:
        raise _ProtocolError(
            INVALID_PARAMS,
            f"invalid params: 'role' must be one of: {', '.join(CANONICAL_ROLES)} (got {role!r})",
        )
    session_id = (arguments.get("session_id") or "").strip() or None
    try:
        result, _, _ = _load_store_for_tool(store)
        return compose_read(store, result.docs, arguments["id"], role, session_id, "mcp"), False
    except (_StoreLoadError, ReadError) as exc:
        return str(exc), True


def _patch_front_matter_line(front_matter_text: str, key: str, value: str) -> str:
    """Rewrites exactly one `key:` line, or appends it. Raises `_ToolError` when the key
    appears more than once, when the front matter is a flow mapping, and — for a key that is
    present — when it is indented or its value spans lines."""
    # asked of the parser, not a regex, for the reason `backfill._drop_declared_keys` gives:
    # a column-0 `key:` is not a declaration test, and `^` under `re.MULTILINE` anchors after
    # `\n` only — never after a bare `\r`, so a CR-only document matched nothing and the
    # append branch below silently wrote the key a second time
    try:
        node = (
            yaml.compose(front_matter_text, Loader=yaml.SafeLoader) if front_matter_text else None
        )
    except yaml.YAMLError as exc:
        # this function runs once per patched key, so the second call composes what the first
        # rewrote: an anchored `status: &st active` loses its anchor and every alias to it is
        # then undefined. A document problem, refused as such rather than escaping as -32603
        raise _ToolError(
            f"front matter cannot be re-read to rewrite '{key}:' ({exc}) — refusing to "
            "modify it. Rewriting one line does not carry a YAML anchor or alias with it; "
            "write the front matter's values out in full instead."
        ) from exc
    if isinstance(node, yaml.MappingNode) and node.flow_style:
        # a root flow mapping has no line of its own for any key: replacing one rewrites the
        # brace or the comma beside it, and appending lands after the closing `}`. Column 0
        # does not catch it — `{\nstatus: active\n}` puts the key there
        raise _ToolError(
            f"front matter is a flow mapping, so '{key}:' is not a line of its own — "
            "refusing to rewrite it."
        )
    pairs = node.value if isinstance(node, yaml.MappingNode) else []
    matches = [(k, v) for k, v in pairs if k.value == key]

    if len(matches) > 1:
        raise _ToolError(
            f"front matter has more than one '{key}:' line — refusing to guess "
            "which one to change."
        )

    lines = front_matter_text.splitlines(keepends=True)
    if not matches:
        newline = newline_of(front_matter_text) or "\n"
        prefix = front_matter_text
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        return prefix + f"{key}: {value}" + newline

    key_node, value_node = matches[0]
    index = key_node.start_mark.line
    # the replacement is written at column 0, so an indented block mapping would be de-indented
    # by it, and the second `compose` would then reject text the first accepted. Refused here
    # by shape instead of as a parse failure a caller cannot act on; the flow case is rejected
    # above, and between them the key is a line of its own
    if key_node.start_mark.column != 0:
        raise _ToolError(
            f"'{key}:' is not a line of its own in this front matter — refusing to "
            "rewrite it."
        )
    if value_node.end_mark.line != index:
        raise _ToolError(
            f"'{key}:' spans more than one line — refusing to rewrite it."
        )
    # the line's own terminator, whatever it is: `splitlines` recognises more separators
    # than `\r\n` and PyYAML accepts several of them
    content = lines[index].splitlines()[0]
    terminator = lines[index][len(content):]
    lines[index] = f"{key}: {value}" + terminator
    return "".join(lines)


# ---------------------------------------------------------------------------
# method handlers — each takes (params, store) and returns a JSON-serializable
# result, or raises _ProtocolError for a protocol-level fault
# ---------------------------------------------------------------------------


def _handle_ping(params: dict, store: Path) -> dict:
    # spec 2025-06-18 Utilities/Ping: respond promptly with an empty result,
    # unconditionally — used by clients (Claude Desktop included) as a liveness probe
    return {}


def _handle_initialize(params: dict, store: Path) -> dict:
    # we support exactly one protocol version, so we always answer with it
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}, "prompts": {}},
        "serverInfo": {"name": "engmem", "version": __version__},
    }


def _handle_initialized_notification(params: dict, store: Path) -> dict:
    return {}  # never actually sent; present so the method isn't method-not-found


_SESSION_ID_SCHEMA = {
    "type": "string",
    "description": (
        "The id of the draft session document this search belongs to. Pass it on "
        "every search once the draft exists — it is the only field that ties this "
        "retrieval to the document that later cites it. Omitted, the search is "
        "logged unattributed and drops out of the analysis entirely. Leave it out "
        "only when no draft has been created yet."
    ),
}

# US-09/US-10: the same scope `engmem search --repo` / `--unscoped` / `--all-repos` take, under
# the same rules
_REPO_SCHEMA = {
    "type": "string",
    "description": (
        "Search only the records whose `repos` front matter names this repository "
        "(case-insensitive). Title and tags never stand in for that link, so a record "
        "with no `repos` is left out. For several repositories use `repos`. Omit every "
        "scope argument to search the whole store."
    ),
}
_REPOS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "description": (
        "Search only the records linked to any of these repositories, each record once. "
        "Same matching as `repo`, and joined with it when both are passed. Only when the "
        "user asks to bring in other repositories; never empty."
    ),
}
_UNSCOPED_SCHEMA = {
    "type": "boolean",
    "description": (
        "true: search only the records linked to no repository (no readable `repos` "
        "value). Not combined with another scope argument."
    ),
}
_ALL_REPOS_SCHEMA = {
    "type": "boolean",
    "description": (
        "true: search the whole store, as with no scope argument, but say so in a `scope:` "
        "line and show each result's `repos:` links. Not combined with another scope argument."
    ),
}
_SCOPE_PROPERTIES = {
    "repo": _REPO_SCHEMA,
    "repos": _REPOS_SCHEMA,
    "unscoped": _UNSCOPED_SCHEMA,
    "all_repos": _ALL_REPOS_SCHEMA,
}


def _handle_tools_list(params: dict, store: Path) -> dict:
    return {
        "tools": [
            {
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Key terms describing the current task — the same "
                                "words you would pass to `engmem search` on the CLI."
                            ),
                        },
                        "session_id": _SESSION_ID_SCHEMA,
                        **_SCOPE_PROPERTIES,
                    },
                    "required": ["query"],
                },
            },
            {
                "name": ROLE_TOOL_NAME,
                "description": ROLE_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Key terms describing the current task — used to "
                                "rank documents exactly as `engmem_search` does. "
                                "Role filtering happens after ranking, not instead "
                                "of it."
                            ),
                        },
                        "role": {
                            "type": "string",
                            "enum": list(CANONICAL_ROLES),
                            "description": (
                                "Which section role to retrieve from each "
                                "top-ranked document — e.g. 'decisions' for a "
                                "Decision Log, 'lessons' for Lessons Learned, "
                                "'production' for Production Considerations. A "
                                "document ranked by the query but lacking this "
                                "role is skipped, not returned with a substitute "
                                "section."
                            ),
                        },
                        "session_id": _SESSION_ID_SCHEMA,
                        **_SCOPE_PROPERTIES,
                    },
                    "required": ["query", "role"],
                },
            },
            {
                "name": CREATE_DRAFT_TOOL_NAME,
                "description": CREATE_DRAFT_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": (
                                "The new document's id — must equal the filename "
                                "stem and the front matter's own 'id' field, and "
                                "must match <story-id>-<slug> or <YYYYMMDD>-<slug> "
                                "(lowercase letters, digits, and hyphens only)."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "The complete file content to create: the YAML "
                                "front matter block (--- ... ---) followed by the "
                                "body. Front matter 'status' must be 'draft'."
                            ),
                        },
                    },
                    "required": ["id", "content"],
                },
            },
            {
                "name": COMPLETE_DRAFT_TOOL_NAME,
                "description": COMPLETE_DRAFT_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": (
                                "The id of the existing draft to finish — "
                                "sessions/<id>.md must already exist with "
                                "status: draft."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "The complete replacement file content: front "
                                "matter (with 'status: active' and the same 'id') "
                                "followed by the full body sections."
                            ),
                        },
                        "expected_version": {
                            "type": "string",
                            "description": (
                                "The draft's version as engmem_create_draft "
                                "returned it, or as a refusal of this tool "
                                "reported it after you re-read the draft. The "
                                "call is refused, and nothing written, if the "
                                "draft on disk is no longer that version. "
                                "Omitted, a draft changed since you read it is "
                                "overwritten."
                            ),
                        },
                    },
                    "required": ["id", "content"],
                },
            },
            {
                "name": MARK_SUPERSEDED_TOOL_NAME,
                "description": MARK_SUPERSEDED_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": (
                                "The id of the earlier, currently-active document "
                                "to mark superseded."
                            ),
                        },
                        "superseded_by": {
                            "type": "string",
                            "description": (
                                "The id of the document that supersedes it."
                            ),
                        },
                    },
                    "required": ["id", "superseded_by"],
                },
            },
            {
                "name": RECORD_FEEDBACK_TOOL_NAME,
                "description": RECORD_FEEDBACK_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": (
                                "The session document whose search showed the document -- "
                                "the id passed as `session_id` on that search."
                            ),
                        },
                        "doc_id": {
                            "type": "string",
                            "description": "The id of the document the search showed.",
                        },
                        "assessment": {
                            "type": "string",
                            "enum": list(feedback.Assessment),
                            "description": "The user's assessment, as the user gave it.",
                        },
                        "decision": {
                            "type": "string",
                            "description": (
                                "Optional: the decision the document changed, in the "
                                f"user's words, at most {feedback.TEXT_MAX} characters."
                            ),
                        },
                        "source": {
                            "type": "string",
                            "description": (
                                "Optional: where that can be seen, e.g. a commit, PR or "
                                f"review, at most {feedback.TEXT_MAX} characters."
                            ),
                        },
                    },
                    "required": ["session_id", "doc_id", "assessment"],
                },
            },
            {
                "name": READ_TOOL_NAME,
                "description": READ_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The id of the document to read, as a search showed it.",
                        },
                        "role": {
                            "type": "string",
                            "enum": list(CANONICAL_ROLES),
                            "description": (
                                "Optional: return only the document's sections of this role."
                            ),
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "The id of the draft session document this read belongs to, "
                                "as on `engmem_search`. Omitted, the read is counted "
                                "unattributed and in no session's cost."
                            ),
                        },
                    },
                    "required": ["id"],
                },
            },
            {
                "name": RECORD_BASELINE_TOOL_NAME,
                "description": RECORD_BASELINE_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The session document the baseline call belongs to.",
                        },
                        "tokens": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Tokens the runtime reported for the call.",
                        },
                        "seconds": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "description": "Duration the runtime reported for the call.",
                        },
                        "call_id": {
                            "type": "string",
                            "description": (
                                "Optional: the runtime's id for that call, at most "
                                f"{cost.CALL_ID_MAX} characters; a later report with the same "
                                "id replaces this one."
                            ),
                        },
                    },
                    "required": ["session_id"],
                },
            },
        ]
    }


# every tool but the two searches, each one outside `_SEARCH_TOOL_NAMES` and so off a --read-only
# server; `engmem_read` writes no document, only its row in `cost.jsonl`
_WRITE_TOOL_REQUIRED_STRING_ARGS = {
    CREATE_DRAFT_TOOL_NAME: ("id", "content"),
    COMPLETE_DRAFT_TOOL_NAME: ("id", "content"),
    MARK_SUPERSEDED_TOOL_NAME: ("id", "superseded_by"),
    RECORD_FEEDBACK_TOOL_NAME: ("session_id", "doc_id", "assessment"),
    READ_TOOL_NAME: ("id",),
    RECORD_BASELINE_TOOL_NAME: ("session_id",),
}

_WRITE_TOOL_OPTIONAL_STRING_ARGS = {
    COMPLETE_DRAFT_TOOL_NAME: ("expected_version",),
    RECORD_FEEDBACK_TOOL_NAME: ("decision", "source"),
    READ_TOOL_NAME: ("role", "session_id"),
    RECORD_BASELINE_TOOL_NAME: ("call_id",),
}

_WRITE_TOOL_HANDLERS = {
    CREATE_DRAFT_TOOL_NAME: _handle_create_draft,
    COMPLETE_DRAFT_TOOL_NAME: _handle_complete_draft,
    MARK_SUPERSEDED_TOOL_NAME: _handle_mark_superseded,
    RECORD_FEEDBACK_TOOL_NAME: _handle_record_feedback,
    READ_TOOL_NAME: _handle_read,
    RECORD_BASELINE_TOOL_NAME: _handle_record_baseline,
}


def _scope_names(args: dict) -> list[str]:
    """`repo` and `repos` together, refused rather than read as absent when blank or empty."""
    repo = args.get("repo")
    repos = args.get("repos")
    if repo is not None and not isinstance(repo, str):
        raise _ProtocolError(INVALID_PARAMS, "invalid params: 'repo' must be a string")
    if repos is not None and not (
        isinstance(repos, list) and all(isinstance(name, str) for name in repos)
    ):
        raise _ProtocolError(INVALID_PARAMS, "invalid params: 'repos' must be a list of strings")
    if repo is not None and not repo.strip():
        raise _ProtocolError(
            INVALID_PARAMS,
            "invalid params: 'repo' needs a repository name (got a blank value) — omit it "
            "to search the whole store",
        )
    if repos is not None and not repos:
        raise _ProtocolError(
            INVALID_PARAMS,
            "invalid params: 'repos' needs at least one repository name (got an empty list) "
            "— omit it to search the whole store",
        )
    if repos is not None and not all(name.strip() for name in repos):
        raise _ProtocolError(
            INVALID_PARAMS,
            "invalid params: 'repos' needs repository names (got a blank value) — omit it "
            "to search the whole store",
        )
    return ([repo] if repo is not None else []) + (repos or [])


def _search_scope(arguments: object) -> Scope | None:
    """The scope a search tool call asks for; `null` and `false` are absent
    (contracts/mcp-server.md)."""
    args = arguments if isinstance(arguments, dict) else {}
    unscoped = args.get("unscoped")
    all_repos = args.get("all_repos")
    names = _scope_names(args)
    if unscoped is not None and not isinstance(unscoped, bool):
        raise _ProtocolError(INVALID_PARAMS, "invalid params: 'unscoped' must be a boolean")
    if all_repos is not None and not isinstance(all_repos, bool):
        raise _ProtocolError(INVALID_PARAMS, "invalid params: 'all_repos' must be a boolean")
    chosen = []
    if names:
        given = [key for key in ("repo", "repos") if args.get(key) is not None]
        chosen.append("/".join(f"'{key}'" for key in given))
    if unscoped:
        chosen.append("'unscoped'")
    if all_repos:
        chosen.append("'all_repos'")
    if len(chosen) > 1:
        raise _ProtocolError(
            INVALID_PARAMS,
            f"invalid params: {' and '.join(chosen)} are different scopes — pass one",
        )
    if unscoped:
        return Scope(unscoped=True)
    if all_repos:
        return Scope(all_repos=True)
    if not names:
        return None
    return Scope(repos=tuple(names))


def _handle_tools_call(params: dict, store: Path, read_only: bool = False) -> dict:
    if "name" not in params:
        raise _ProtocolError(INVALID_PARAMS, "invalid params: missing required 'name' field")
    name = params.get("name")
    if name not in (TOOL_NAME, ROLE_TOOL_NAME, *_WRITE_TOOL_HANDLERS):
        raise _ProtocolError(INVALID_PARAMS, f"unknown tool: {name!r}")

    arguments = params.get("arguments")

    if name in _WRITE_TOOL_HANDLERS:
        # the inputSchema declares each of these required, so a missing or blank value is a
        # protocol-level fault; a valid-but-refused write (bad id shape, escaping sessions/,
        # overwrite refusal) is an ordinary isError result from the handler instead
        for field_name in _WRITE_TOOL_REQUIRED_STRING_ARGS[name]:
            value = arguments.get(field_name) if isinstance(arguments, dict) else None
            if not isinstance(value, str) or not value.strip():
                raise _ProtocolError(
                    INVALID_PARAMS,
                    f"invalid params: {field_name!r} must be a non-empty string",
                )
        for field_name in _WRITE_TOOL_OPTIONAL_STRING_ARGS.get(name, ()):
            value = arguments.get(field_name) if isinstance(arguments, dict) else None
            if value is not None and not isinstance(value, str):
                raise _ProtocolError(
                    INVALID_PARAMS, f"invalid params: {field_name!r} must be a string"
                )
        text, is_error = _WRITE_TOOL_HANDLERS[name](arguments, store)
        result: dict = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        return result

    query = arguments.get("query") if isinstance(arguments, dict) else None
    if not isinstance(query, str) or not query.strip():
        # schema declares query required, so a missing/blank value is a protocol fault, not
        # isError (reserved for a schema-conforming call that fails at runtime)
        raise _ProtocolError(
            INVALID_PARAMS, "invalid params: 'query' must be a non-empty string"
        )

    session_id = arguments.get("session_id") if isinstance(arguments, dict) else None
    if session_id is not None and not isinstance(session_id, str):
        # wrong JSON type is malformed, not something to coerce — str() on a
        # dict would write Python repr syntax into the log
        raise _ProtocolError(
            INVALID_PARAMS, "invalid params: 'session_id' must be a string"
        )
    if session_id is not None:
        # blank == absent; mirrors cli.py's _session_id so both paths log the same key
        session_id = session_id.strip() or None
    scope = _search_scope(arguments)

    if name == ROLE_TOOL_NAME:
        role = arguments.get("role") if isinstance(arguments, dict) else None
        if not isinstance(role, str) or role not in CANONICAL_ROLES:
            raise _ProtocolError(
                INVALID_PARAMS,
                "invalid params: 'role' must be one of: "
                + ", ".join(CANONICAL_ROLES)
                + (f" (got {role!r})" if role is not None else " (missing)"),
            )
        text, is_error = _run_search_for_tool(
            store, query, session_id, role, scope, read_only
        )
    else:
        text, is_error = _run_search_for_tool(
            store, query, session_id, scope=scope, read_only=read_only
        )

    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _prompt_summary(name: str, source_name: str) -> dict:
    description, argument_hint, _ = _load_template(source_name)
    arguments = []
    if argument_hint:
        arguments.append(
            {
                "name": _argument_name_from_hint(str(argument_hint)),
                "description": (
                    f"Substituted for {_ARGUMENTS_PLACEHOLDER} in the prompt body "
                    f"({argument_hint})"
                ),
                "required": False,
            }
        )
    return {
        "name": name,
        "title": f"/{name}",
        "description": description,
        "arguments": arguments,
    }


def _handle_prompts_list(params: dict, store: Path) -> dict:
    return {
        "prompts": [_prompt_summary(name, source) for name, source in PROMPT_TEMPLATES.items()]
    }


def _mode_line() -> str:
    """The answer to the start template's step 0, read on every call so a server that outlives
    a `engmem mode set` hands the next session the new mode."""
    setting = mode_setting_file()
    try:
        mode = saved_mode()
    except ModeSettingError as exc:
        return (
            f"engmem mode for this session: daily (the saved mode cannot be read: {exc} — "
            "tell the user, as step 0 says)"
        )
    if mode is None:
        return f"engmem mode for this session: daily (default — no saved choice in {setting})"
    return f"engmem mode for this session: {mode} (saved choice in {setting})"


def _handle_prompts_get(params: dict, store: Path) -> dict:
    if "name" not in params:
        raise _ProtocolError(INVALID_PARAMS, "invalid params: missing required 'name' field")
    name = params.get("name")
    # the isinstance guard is not redundant: a dict or list `name` is unhashable, so looking it
    # up would raise TypeError and leave as a -32603 internal error — a malformed request
    # reported as a server bug. Same treatment `tools/call` gives a wrongly typed tool name
    if not isinstance(name, str) or name not in PROMPT_TEMPLATES:
        raise _ProtocolError(INVALID_PARAMS, f"unknown prompt: {name!r}")
    source_name = PROMPT_TEMPLATES[name]

    description, argument_hint, body = _load_template(source_name)

    if argument_hint:
        supplied = params.get("arguments")
        arg_name = _argument_name_from_hint(str(argument_hint))
        value = supplied.get(arg_name) if isinstance(supplied, dict) else None
        # missing/None substitutes to empty; a supplied non-string value is a malformed request
        # (the spec types these as string), not coerced — str()/repr() would leak Python syntax,
        # and a truthiness check would wrongly blank out a legitimate "0"
        if value is not None and not isinstance(value, str):
            raise _ProtocolError(
                INVALID_PARAMS, f"invalid params: argument {arg_name!r} must be a string"
            )
        body = body.replace(_ARGUMENTS_PLACEHOLDER, value if value is not None else "")

    if name == START_PROMPT_NAME:
        body = f"{_mode_line()}\n\n{body}"

    return {
        "description": description,
        "messages": [{"role": "user", "content": {"type": "text", "text": body}}],
    }


_METHODS = {
    "ping": _handle_ping,
    "initialize": _handle_initialize,
    "notifications/initialized": _handle_initialized_notification,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "prompts/list": _handle_prompts_list,
    "prompts/get": _handle_prompts_get,
}

_SEARCH_TOOL_NAMES = (TOOL_NAME, ROLE_TOOL_NAME)


def _handle_read_only_tools_list(params: dict, store: Path) -> dict:
    tools = _handle_tools_list(params, store)["tools"]
    return {"tools": [tool for tool in tools if tool["name"] in _SEARCH_TOOL_NAMES]}


def _handle_read_only_tools_call(params: dict, store: Path) -> dict:
    # an allowlist, like tools/list: a tool added later stays unreachable here until named above
    name = params.get("name")
    if "name" in params and name not in _SEARCH_TOOL_NAMES:
        raise _ProtocolError(
            INVALID_PARAMS, f"unknown tool: {name!r} — this server runs with --read-only"
        )
    # D2: a read-only server keeps no version copies and offers no reference — a client behind a
    # bridge cannot save a Reuse Log, and the store is not written for it — and, for the same
    # reason, records no delivery in cost.jsonl (contracts/mcp-server.md)
    return _handle_tools_call(params, store, read_only=True)


# `engmem mcp --read-only`: the two search tools and nothing that writes a document
_READ_ONLY_METHODS = {
    **_METHODS,
    "tools/list": _handle_read_only_tools_list,
    "tools/call": _handle_read_only_tools_call,
}

# methods legitimately invoked with no id — their handler still runs even
# though no response can be sent; every other method is request/response, and
# _dispatch skips the handler entirely for a non-conforming id-less message
_NOTIFICATION_METHODS = {"notifications/initialized"}


# ---------------------------------------------------------------------------
# JSON-RPC envelope handling
# ---------------------------------------------------------------------------


def _error_response(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _has_unpaired_surrogate(message: Any) -> bool:
    """True when any string anywhere in the decoded message cannot be encoded as UTF-8."""
    # an explicit stack rather than recursion: the decoder already accepted this nesting depth,
    # and a recursive walk of the same structure could raise RecursionError where it did not
    stack: list[Any] = [message]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _dispatch(message: Any, store: Path, read_only: bool = False) -> dict | None:
    """The response dict to write, or None when nothing should be written."""
    if not isinstance(message, dict):
        return _error_response(None, INVALID_REQUEST, "invalid request: expected a JSON object")

    # rejected here, once, rather than at each field: a lone surrogate reaches every consumer
    # downstream as a different failure — `content.encode` raises, `json.dumps` echoes an
    # escape strict clients reject — and none of them can be UTF-8, which the MCP stdio
    # transport requires. The id is null because the id itself may be the offending string
    if _has_unpaired_surrogate(message):
        return _error_response(
            None,
            INVALID_REQUEST,
            "invalid request: message contains an unpaired surrogate, which is not UTF-8",
        )

    has_id = "id" in message
    msg_id = message.get("id")

    # id must be string/number/null; on failure the error's own id is null,
    # never an echo of something that couldn't be trusted in the first place.
    # `isinstance(True, int)`, so booleans are excluded by name or `id: true` echoes back
    if (
        has_id
        and msg_id is not None
        and (isinstance(msg_id, bool) or not isinstance(msg_id, (str, int, float)))
    ):
        return _error_response(None, INVALID_REQUEST, "invalid request: 'id' must be a string, number, or null")

    # enforced, not warn-and-served: every real client sends exactly "2.0"
    if message.get("jsonrpc") != "2.0":
        return (
            _error_response(msg_id, INVALID_REQUEST, "invalid request: 'jsonrpc' must be exactly \"2.0\"")
            if has_id
            else None
        )

    method = message.get("method")

    if not isinstance(method, str) or not method:
        return (
            _error_response(msg_id, INVALID_REQUEST, "invalid request: missing 'method'")
            if has_id
            else None
        )

    handler = (_READ_ONLY_METHODS if read_only else _METHODS).get(method)
    if handler is None:
        return (
            _error_response(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")
            if has_id
            else None
        )

    if not has_id and method not in _NOTIFICATION_METHODS:
        # unanswerable — skip the handler rather than do possibly expensive
        # work (e.g. a full store scan) whose result would be discarded
        return None

    params = message.get("params")
    if not isinstance(params, dict):
        params = {}

    try:
        result = handler(params, store)
    except _ProtocolError as exc:
        return _error_response(msg_id, exc.code, exc.message) if has_id else None
    except Exception as exc:  # last-resort guard: the stdio loop must never die
        print(f"engmem-mcp: unhandled error in {method}: {exc!r}", file=sys.stderr)
        return (
            _error_response(msg_id, INTERNAL_ERROR, f"internal error: {exc}")
            if has_id
            else None
        )

    if not has_id:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _write(stdout: TextIO, message: dict) -> None:
    # json.dumps escapes embedded newlines, keeping a multi-line tool result to one line
    stdout.write(json.dumps(message) + "\n")
    stdout.flush()


def _mute_broken_stdout(stdout: TextIO) -> None:
    """Points the dead stdout fd at os.devnull so the interpreter's shutdown flush of the frame
    still sitting in its buffer cannot fail — see contracts/mcp-server.md."""
    devnull_fd = None
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, stdout.fileno())
    except (AttributeError, OSError, ValueError):
        return
    finally:
        if devnull_fd is not None:
            os.close(devnull_fd)


def _use_utf8_transport(stdin: TextIO) -> None:
    """Pins stdin to UTF-8, decoding an undecodable byte to a surrogate instead of raising."""
    reconfigure = getattr(stdin, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="surrogateescape")


def serve(
    store: Path, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, read_only: bool = False
) -> int:
    """Runs the MCP stdio loop until stdin closes; the only writer to stdout."""
    _use_utf8_transport(stdin)
    try:
        for raw_line in stdin:
            line = raw_line.strip()
            if not line:
                continue

            try:
                message = json.loads(line)
            except (json.JSONDecodeError, RecursionError) as exc:
                # RecursionError: the stdlib json decoder has no depth cap, so a
                # pathologically nested line blows the stack before JSONDecodeError —
                # same fate as any other unparseable line
                _write(stdout, _error_response(None, PARSE_ERROR, f"parse error: {exc}"))
                continue

            response = _dispatch(message, store, read_only)
            if response is not None:
                _write(stdout, response)
    except BrokenPipeError:
        # client already gone — a clean teardown, not a crash; stderr only
        _mute_broken_stdout(stdout)
        print("engmem-mcp: downstream pipe closed — stopping", file=sys.stderr)
        return 0

    return 0
