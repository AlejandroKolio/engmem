"""One document read, composed once for every channel that delivers it, with its delivery recorded
(US-16; contracts/output.md, "Observed cost")."""

from __future__ import annotations

import re
from pathlib import Path

from engmem.cost import OP_READ, record_delivery
from engmem.output import _escape_control_char, _escape_controls
from engmem.search_report import Channel, cite_lines
from engmem.sections import CANONICAL_ROLES, sections_for_role, split_sections
from engmem.spine import Doc

UNATTRIBUTED_READ_NOTES = {
    "cli": "note: unattributed read — pass --session <draft-id>",
    "mcp": "note: unattributed read — pass session_id <draft-id>",
}
# a line break and a tab are the document's own layout; every other control is escaped
_BODY_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")


class ReadError(Exception):
    """A read refused: the message names why, and nothing was delivered or recorded."""


def _escape_body(text: str) -> str:
    return _BODY_CONTROL_RE.sub(_escape_control_char, text.replace("\r\n", "\n"))


def _published(docs: list[Doc], doc_id: str) -> Doc:
    doc = next((d for d in docs if d.id == doc_id), None)
    if doc is None:
        raise ReadError(f"no document with id '{_escape_controls(doc_id)}' in the store")
    if doc.status == "draft":
        raise ReadError(
            f"'{_escape_controls(doc_id)}' is a draft -- only published documents are read here"
        )
    return doc


def _header(doc: Doc) -> str:
    status = f"status: {_escape_controls(doc.status)}"
    if doc.status == "superseded" and doc.superseded_by:
        status += f", superseded by {_escape_controls(doc.superseded_by)}"
    return f"document: {_escape_controls(doc.id)} -- {_escape_controls(doc.title)} ({status})"


def _role_text(doc: Doc, role: str) -> str:
    if role not in CANONICAL_ROLES:
        raise ReadError(
            f"unknown role {role!r} -- valid roles: {', '.join(CANONICAL_ROLES)}"
        )
    sections = sections_for_role(split_sections(doc.body), role)
    if not sections:
        raise ReadError(f"'{_escape_controls(doc.id)}' has no {role} section")
    return "\n\n".join(f"{'#' * s.level} {s.heading}\n\n{s.body}" for s in sections)


def compose_read(
    store: Path,
    docs: list[Doc],
    doc_id: str,
    role: str | None,
    session_id: str | None,
    channel: Channel,
) -> str:
    """The whole delivered text of one read, and its one `cost.jsonl` row; raises `ReadError`."""
    doc = _published(docs, doc_id.strip())
    text = doc.body.strip("\n") if role is None else _role_text(doc, role)
    lines = [_header(doc)]
    if role is not None:
        lines.append(f"section role: {role}")
    # the version names the whole document's bytes, even for a role read: a quote from any
    # section is checked against that one retained copy (contracts/gate1.md)
    lines += ["", _escape_body(text), *cite_lines(store, [doc])]
    trailer = [UNATTRIBUTED_READ_NOTES[channel]] if session_id is None else []
    # the delivery is the whole text sent, so it is measured last, without only this note
    cost_error = record_delivery(
        store, op=OP_READ, session_id=session_id, channel=channel,
        byte_count=len("\n".join(lines + trailer).encode("utf-8")), doc_id=doc.id, role=role,
    )
    if cost_error is not None:
        lines.append(f"note: cost not recorded ({cost_error})")
    return "\n".join(lines + trailer)
