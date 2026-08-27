"""A stdio MCP server for engmem; stdout carries JSON-RPC frames and nothing else."""

from __future__ import annotations

import json
import os
import re
import sys
from importlib import resources
from pathlib import Path
from typing import Any, TextIO

import yaml

from engmem import __version__
from engmem.cache import identity_for
from engmem.output import (
    render_no_match,
    render_role_search_results,
    render_scoreboard,
    render_search_results,
    select_role_hits,
)
from engmem.scoring import search as run_search, search_with_role_sections
from engmem.sections import CANONICAL_ROLES
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
from engmem.staging import commit, discard, newline_of, read_document, stage
from engmem.telemetry import log_role_search, log_search

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
    "plus a one-line summary of how many documents the store holds."
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
    "a topic."
)

# ---------------------------------------------------------------------------
# write tools — the save half of the workflow for a shell-less runtime (Claude Desktop): create a
# draft, finish it (draft -> active), mark an earlier document superseded.
CREATE_DRAFT_TOOL_NAME = "engmem_create_draft"
CREATE_DRAFT_TOOL_DESCRIPTION = (
    "Create a new engmem session document as a draft — the first step of "
    "`/engmem` in a runtime with no shell access. Writes `sessions/<id>.md` "
    "with exactly the `content` given: the complete YAML front matter block "
    "followed by the body (at minimum the `## Pre-reg` section). The front "
    "matter's `status` MUST be `draft` and its `id` MUST equal the `id` "
    "argument — this tool refuses to create anything else. NEVER overwrites: "
    "if `sessions/<id>.md` already exists, the call fails and nothing is "
    "written, regardless of what the existing file contains. Use "
    "`engmem_complete_draft` to finish a draft this tool already created."
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
    "document still in draft. To mark a DIFFERENT, already-finished document as "
    "superseded by this one, call `engmem_mark_superseded` instead — never pass "
    "that document's id here."
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


# prompt name -> source template filename under src/engmem/templates/ — the
# same files install.py's _install_templates copies out; a second reader of
# that one source of truth, not a fork of it
PROMPT_TEMPLATES = {
    "engmem": "engmem.start.md",
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
# store access for the tool — mirrors _cmd_search's STDOUT shape only
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
    store: Path, query: str, session_id: str | None = None
) -> tuple[str, bool]:
    """`(rendered_text, is_error)`, mirroring exactly what `_cmd_search` prints to stdout."""
    try:
        result, strays, stray_scan_errors = _load_store_for_tool(store)
    except _StoreLoadError as exc:
        return str(exc), True

    outcome = run_search(result.docs, query)

    # "found nothing" and "could not check" must never look the same
    scan_notes = "".join(f"warning: {scan_error}\n" for scan_error in stray_scan_errors)
    for scan_error in stray_scan_errors:
        print(f"engmem-mcp: {scan_error}", file=sys.stderr)

    # result_text is what context_bytes below measures; the stray-files note
    # is store housekeeping, kept out of it (see telemetry.py context_bytes)
    if outcome.hits or outcome.superseded_notes:
        result_text = render_search_results(outcome, result.docs)
    else:
        result_text = render_no_match()
    body = result_text
    if not (outcome.hits or outcome.superseded_notes) and strays:
        body += (
            "\n"
            f"({len(strays)} markdown file(s) sit outside the searched set — "
            "only sessions/*.md is read, not the store root and not "
            "subdirectories. They may hold the answer.)"
        )

    scoreboard = render_scoreboard(result.docs, failed=len(result.errors))
    text = scan_notes + body + "\n" + scoreboard

    # channel="mcp" keeps a Desktop search and a terminal search distinguishable
    # in telemetry.jsonl; skipping this would make the MCP path invisible to Gate 1
    telemetry_error = log_search(
        store / "telemetry.jsonl",
        query=query,
        n_docs=len(result.docs),
        outcome=outcome,
        session_id=session_id,
        channel="mcp",
        context_bytes=len(result_text.encode("utf-8")),
    )
    if telemetry_error is not None:
        # stdout here is JSON-RPC only, so the note rides in the tool result
        # text instead of print() — never isError, since the search succeeded
        text += f"\nnote: telemetry not recorded ({telemetry_error})"

    return text, False


def _run_role_search_for_tool(
    store: Path, query: str, role: str, session_id: str | None = None
) -> tuple[str, bool]:
    """Role-addressed counterpart to `_run_search_for_tool`."""
    try:
        result, strays, stray_scan_errors = _load_store_for_tool(store)
    except _StoreLoadError as exc:
        return str(exc), True

    outcome, role_map = search_with_role_sections(result.docs, query)
    role_hits, _n_with_role = select_role_hits(outcome, role_map, role)

    scan_notes = "".join(f"warning: {scan_error}\n" for scan_error in stray_scan_errors)
    for scan_error in stray_scan_errors:
        print(f"engmem-mcp: {scan_error}", file=sys.stderr)

    result_text = render_role_search_results(outcome, role_map, role)
    body = result_text
    if not role_hits and strays:
        body += (
            "\n"
            f"({len(strays)} markdown file(s) sit outside the searched set — "
            "only sessions/*.md is read, not the store root and not "
            "subdirectories. They may hold the answer.)"
        )

    scoreboard = render_scoreboard(result.docs, failed=len(result.errors))
    text = scan_notes + body + "\n" + scoreboard

    telemetry_error = log_role_search(
        store / "telemetry.jsonl",
        query=query,
        role=role,
        n_docs=len(result.docs),
        role_hits=role_hits,
        session_id=session_id,
        channel="mcp",
        context_bytes=len(result_text.encode("utf-8")),
    )
    if telemetry_error is not None:
        text += f"\nnote: telemetry not recorded ({telemetry_error})"

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


def _handle_create_draft(arguments: object, store: Path) -> tuple[str, bool]:
    doc_id = arguments.get("id") if isinstance(arguments, dict) else None
    content = arguments.get("content") if isinstance(arguments, dict) else None

    try:
        target = _resolve_write_target(store, doc_id)

        if target.exists():
            raise _ToolError(
                f"refusing to create sessions/{doc_id}.md: it already exists — "
                f"{CREATE_DRAFT_TOOL_NAME} never overwrites. Use "
                f"{COMPLETE_DRAFT_TOOL_NAME} to finish an existing draft."
            )

        doc, tmp_path, parse_error = _stage_content(target, content.encode("utf-8"))
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
            commit(tmp_path, target)
        # BaseException, not _ToolError: `commit` chmods and replaces, so it can raise
        # OSError of its own, and the staged file must go either way
        except BaseException:
            discard(tmp_path)
            raise
    except _ToolError as exc:
        return str(exc), True

    return f"created sessions/{doc_id}.md (status: draft)", False


def _handle_complete_draft(arguments: object, store: Path) -> tuple[str, bool]:
    doc_id = arguments.get("id") if isinstance(arguments, dict) else None
    content = arguments.get("content") if isinstance(arguments, dict) else None

    try:
        target = _resolve_write_target(store, doc_id)

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
            commit(tmp_path, target)
        # BaseException, not _ToolError: `commit` chmods and replaces, so it can raise
        # OSError of its own, and the staged file must go either way
        except BaseException:
            discard(tmp_path)
            raise
    except _ToolError as exc:
        return str(exc), True

    return f"completed sessions/{doc_id}.md (status: draft -> active)", False


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
        # `backfill.apply_backfill`'s rule: a document that moved under the read it is being
        # rebuilt from is refused, never rewritten from the copy that is already stale
        if existing.source_identity != identity_for(target):
            raise _ToolError(
                f"sessions/{doc_id}.md changed on disk while it was being read — "
                "refusing to write, so a newer version is not replaced by a stale one. "
                "Call this tool again."
            )

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
    except _ToolError as exc:
        return str(exc), True

    return (
        f"marked sessions/{doc_id}.md superseded by {superseded_by} "
        "(status: active -> superseded)"
    ), False


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
        "Optional: the id of the draft session document this search belongs to. "
        "Recorded in the search log so a retrieval can later be tied to the "
        "document that reused it. Pass it whenever `/engmem` has already created "
        "the draft."
    ),
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
        ]
    }


_WRITE_TOOL_REQUIRED_STRING_ARGS = {
    CREATE_DRAFT_TOOL_NAME: ("id", "content"),
    COMPLETE_DRAFT_TOOL_NAME: ("id", "content"),
    MARK_SUPERSEDED_TOOL_NAME: ("id", "superseded_by"),
}

_WRITE_TOOL_HANDLERS = {
    CREATE_DRAFT_TOOL_NAME: _handle_create_draft,
    COMPLETE_DRAFT_TOOL_NAME: _handle_complete_draft,
    MARK_SUPERSEDED_TOOL_NAME: _handle_mark_superseded,
}


def _handle_tools_call(params: dict, store: Path) -> dict:
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

    if name == ROLE_TOOL_NAME:
        role = arguments.get("role") if isinstance(arguments, dict) else None
        if not isinstance(role, str) or role not in CANONICAL_ROLES:
            raise _ProtocolError(
                INVALID_PARAMS,
                "invalid params: 'role' must be one of: "
                + ", ".join(CANONICAL_ROLES)
                + (f" (got {role!r})" if role is not None else " (missing)"),
            )
        text, is_error = _run_role_search_for_tool(store, query, role, session_id)
    else:
        text, is_error = _run_search_for_tool(store, query, session_id)

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


def _dispatch(message: Any, store: Path) -> dict | None:
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

    handler = _METHODS.get(method)
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


def serve(store: Path, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
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

            response = _dispatch(message, store)
            if response is not None:
                _write(stdout, response)
    except BrokenPipeError:
        # client already gone — a clean teardown, not a crash; stderr only
        _mute_broken_stdout(stdout)
        print("engmem-mcp: downstream pipe closed — stopping", file=sys.stderr)
        return 0

    return 0
