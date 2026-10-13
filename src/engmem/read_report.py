"""One document read, composed once for every channel that delivers it, with its delivery recorded
(US-16; contracts/output.md, "Observed cost")."""

from __future__ import annotations

import re
from pathlib import Path

from engmem import confined
from engmem.cost import OP_READ, record_delivery
from engmem.output import _escape_control_char, _escape_controls
from engmem.search_report import Channel, cite_lines
from engmem.sections import CANONICAL_ROLES, sections_for_role, split_sections
from engmem.spine import Doc, Problem, load_error_for
from engmem.staging import version_of
from engmem.versions import VERSION_SEPARATOR, retained_body, split_reference

DRAFT_NOTE = (
    "note: draft -- not reviewed at save, so not a recorded decision and no Reuse Log reference "
    "is offered; finish it with /engmem.save"
)
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


def not_found_message(doc_id: str, errors: list[Problem]) -> str:
    """"No document", plus the load error that kept its file out, when one did (US-22)."""
    message = f"no document with id '{_escape_controls(doc_id)}' in the store"
    problem = load_error_for(errors, doc_id)
    if problem is not None:
        message += f" -- its file was not loaded: {_escape_controls(problem.message)}"
    return message


def _resolve(
    docs: list[Doc], reference: str, errors: list[Problem]
) -> tuple[Doc, str | None]:
    """The document a bare id or an `<id>@<version>` names, and the version asked for, if any."""
    # an id the spine loads with an `@` in it is still read whole, never split
    exact = next((d for d in docs if d.id == reference), None)
    if exact is not None:
        return exact, None
    doc_id, version = split_reference(reference)
    doc = next((d for d in docs if d.id == doc_id), None)
    if doc is None:
        raise ReadError(not_found_message(doc_id, errors))
    return doc, version


def _current_version(doc: Doc) -> str | None:
    try:
        return version_of(confined.read_bytes(doc.path, confined.SESSION_DEPTH))
    except OSError:
        return None


def _cited_body(store: Path, doc: Doc, version: str) -> str:
    body, problem = retained_body(store, doc.id, version)
    if body is None:
        safe_id = _escape_controls(doc.id)
        raise ReadError(
            f"'{safe_id}{VERSION_SEPARATOR}{_escape_controls(version)}' cannot be read: {problem} -- "
            f"read '{safe_id}' for the current text"
        )
    return body


def _cite(store: Path, doc: Doc, cited_version: str | None) -> list[str]:
    """The read's `cite as` line: the cited version as asked, its copy already retained, or the
    version just delivered (contracts/gate1.md, "Versioned citations")."""
    if cited_version is not None:
        reference = f"{_escape_controls(doc.id)}{VERSION_SEPARATOR}{cited_version}"
        return [f"cite as (Reuse Log prior-doc): {reference}"]
    # the version names the whole document's bytes, even for a role read: a quote from any
    # section is checked against that one retained copy
    return cite_lines(store, [doc])


def _header(doc: Doc) -> str:
    status = f"status: {_escape_controls(doc.status)}"
    if doc.status == "superseded" and doc.superseded_by:
        status += f", superseded by {_escape_controls(doc.superseded_by)}"
    return f"document: {_escape_controls(doc.id)} -- {_escape_controls(doc.title)} ({status})"


def _role_text(doc: Doc, body: str, role: str) -> str:
    if role not in CANONICAL_ROLES:
        raise ReadError(
            f"unknown role {role!r} -- valid roles: {', '.join(CANONICAL_ROLES)}"
        )
    sections = sections_for_role(split_sections(body), role)
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
    errors: list[Problem] | None = None,
) -> str:
    """The whole delivered text of one read, and its one `cost.jsonl` row; raises `ReadError`."""
    doc, version = _resolve(docs, doc_id.strip(), errors or [])
    cited = version is not None and version != _current_version(doc)
    body = _cited_body(store, doc, version) if cited else doc.body
    text = body.strip("\n") if role is None else _role_text(doc, body, role)
    lines = [_header(doc)]
    if cited:
        lines.append(
            f"note: this is version {version}, as cited; the document has changed since -- "
            f"read '{_escape_controls(doc.id)}' for the current text"
        )
    if doc.status == "draft":
        lines.append(DRAFT_NOTE)
    if role is not None:
        lines.append(f"section role: {role}")
    lines += ["", _escape_body(text)]
    if doc.status != "draft":
        lines += _cite(store, doc, version if cited else None)
    trailer = [UNATTRIBUTED_READ_NOTES[channel]] if session_id is None else []
    # the delivery is the whole text sent, so it is measured last, without only this note
    cost_error = record_delivery(
        store, op=OP_READ, session_id=session_id, channel=channel,
        byte_count=len("\n".join(lines + trailer).encode("utf-8")), doc_id=doc.id, role=role,
    )
    if cost_error is not None:
        lines.append(f"note: cost not recorded ({cost_error})")
    return "\n".join(lines + trailer)
