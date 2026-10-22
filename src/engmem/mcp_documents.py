"""The MCP tools that write a session document: create, complete and mark superseded, each one
staged and checked before it lands (contracts/mcp-server.md, "Write tools: security contract")."""

from __future__ import annotations

from pathlib import Path

import yaml

from engmem import drafts
from engmem.mcp_tools import MARK_SUPERSEDED_TOOL_NAME
# imported, not reimplemented, so a document a write tool below produces is
# validated by the exact rules load_store applies when reading it back
from engmem.spine import (
    compose_yaml,
    parse_document,
    split_front_matter,
    validate_doc_id,
)
from engmem.staging import commit, decode_document, discard, newline_of

_ToolError = drafts.WriteRefused


def handle_create_draft(arguments: object, store: Path) -> tuple[str, bool]:
    doc_id = arguments.get("id") if isinstance(arguments, dict) else None
    content = arguments.get("content") if isinstance(arguments, dict) else None
    try:
        return drafts.create_draft(store, doc_id, content, drafts.MCP), False
    except _ToolError as exc:
        return str(exc), True


def handle_complete_draft(arguments: object, store: Path) -> tuple[str, bool]:
    doc_id = arguments.get("id") if isinstance(arguments, dict) else None
    content = arguments.get("content") if isinstance(arguments, dict) else None
    expected_version = arguments.get("expected_version") if isinstance(arguments, dict) else None
    try:
        return drafts.complete_draft(store, doc_id, content, expected_version, drafts.MCP), False
    except _ToolError as exc:
        return str(exc), True


def handle_mark_superseded(arguments: object, store: Path) -> tuple[str, bool]:
    doc_id = arguments.get("id") if isinstance(arguments, dict) else None
    superseded_by = arguments.get("superseded_by") if isinstance(arguments, dict) else None
    expected_version = arguments.get("expected_version") if isinstance(arguments, dict) else None

    try:
        target = drafts.resolve_write_target(store, doc_id)

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
        with drafts.locked(target, doc_id, drafts.MCP):
            _mark_superseded_locked(target, doc_id, superseded_by, expected_version)
    except _ToolError as exc:
        return str(exc), True

    return (
        f"marked sessions/{doc_id}.md superseded by {superseded_by} "
        "(status: active -> superseded)"
    ), False


def _mark_superseded_locked(
    target: Path, doc_id: str, superseded_by: str, expected_version: str | None
) -> None:
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

    # bytes, not `read_text`: this handler rebuilds the whole file from what it reads, and
    # `read_text` drops the BOM and translates every line ending on the way in — see
    # contracts/backfill.md, "Line endings". The bytes rebuilt are the bytes the version names
    data = drafts.refuse_unless_version_read(
        target,
        doc_id,
        expected_version,
        drafts.MCP,
        action="supersede",
        tool=MARK_SUPERSEDED_TOOL_NAME,
        redo="check that the document superseding it still holds against what it says now",
    )
    try:
        existing_text, bom = decode_document(data)
    # replaced with invalid UTF-8 since the validating parse: the document's problem, refused
    # as one — not a -32603 server fault
    except ValueError as exc:
        raise _ToolError(
            f"sessions/{doc_id}.md could not be re-read ({exc}) — nothing was written."
        ) from exc

    front_matter_text, body = split_front_matter(existing_text)
    front_matter_text = _patch_front_matter_line(front_matter_text, "status", "superseded")
    front_matter_text = _patch_front_matter_line(
        front_matter_text, "superseded_by", superseded_by
    )
    newline = newline_of(front_matter_text) or newline_of(body) or "\n"
    new_content = f"---{newline}" + front_matter_text + f"---{newline}" + body

    doc, tmp_path, parse_error = drafts.stage_content(target, bom + new_content.encode("utf-8"))
    try:
        if parse_error is not None:
            raise _ToolError(f"sessions/{doc_id}.md {parse_error}")
        if doc.status != "superseded" or doc.superseded_by != superseded_by:
            raise _ToolError(
                "internal error: patched front matter did not produce the "
                "expected status/superseded_by — refusing to write it."
            )
        # the lock excludes engmem writers only; a hand edit landing since the read above is
        # caught here, as late as a check can run before the replace
        drafts.refuse_if_changed_since_read(target, existing, doc_id, "Call this tool again")
        commit(tmp_path, target)
    # BaseException, not _ToolError: `commit` chmods and replaces, so it can raise
    # OSError of its own, and the staged file must go either way
    except BaseException:
        discard(tmp_path)
        raise


def _patch_front_matter_line(front_matter_text: str, key: str, value: str) -> str:
    """Rewrites exactly one `key:` line, or appends it. Raises `_ToolError` when the key
    appears more than once, when the front matter is a flow mapping, and — for a key that is
    present — when it is indented or its value spans lines."""
    # asked of the parser, not a regex, for the reason `backfill._drop_declared_keys` gives:
    # a column-0 `key:` is not a declaration test, and `^` under `re.MULTILINE` anchors after
    # `\n` only — never after a bare `\r`, so a CR-only document matched nothing and the
    # append branch below silently wrote the key a second time
    try:
        node = compose_yaml(front_matter_text) if front_matter_text else None
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
