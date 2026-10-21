"""One search result, composed once for every channel that shows it (contracts/output.md)."""

from __future__ import annotations

import errno
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from engmem.cost import OP_SEARCH, record_delivery
from engmem.output import (
    RELATED_MAX,
    RoleHit,
    _escape_controls,
    render_no_match,
    render_role_search_results,
    render_scoreboard,
    render_search_results,
    scope_line,
    select_role_hits,
    surfaced_ids,
)
from engmem.provenance import SessionCheckout
from engmem.scoring import (
    Scope,
    SearchOutcome,
    matching_drafts,
    ranking_corpus,
    search,
    search_with_role_sections,
)
from engmem.spine import Doc, LoadResult
from engmem.telemetry import (
    UNATTRIBUTED_CLI_NOTE,
    UNATTRIBUTED_MCP_NOTE,
    log_role_search,
    log_search,
    scope_row,
)
from engmem.versions import VERSION_SEPARATOR, RetentionError, retain

Channel = Literal["cli", "mcp"]

_UNATTRIBUTED_NOTES = {"cli": UNATTRIBUTED_CLI_NOTE, "mcp": UNATTRIBUTED_MCP_NOTE}


@dataclass(frozen=True)
class RenderedResult:
    text: str  # exactly what `context_bytes` measures, and nothing else
    surfaced_anything: bool
    outcome: SearchOutcome
    role_hits: list[RoleHit] | None  # role search only
    n_searched: int  # the documents ranked: the store's published ones, or the scope's


def render_result(
    docs: list[Doc],
    query: str,
    role: str | None,
    scope: Scope | None = None,
    checkout: SessionCheckout | None = None,
) -> RenderedResult:
    """The ranked result for one query, before any store note. Its one I/O is `checkout`'s git
    lookup, made only when a shown record has an anchor to compare (contracts/provenance.md)."""
    n_searched = len(ranking_corpus(docs, scope))
    lead = scope_line(scope, n_searched) if scope is not None else None
    if role is not None:
        outcome, role_map = search_with_role_sections(docs, query, scope)
        role_hits, _matched_role_total = select_role_hits(outcome, role_map, role)
        text = render_role_search_results(outcome, role_map, role, lead, checkout)
        return RenderedResult(text, bool(role_hits), outcome, role_hits, n_searched)

    outcome = search(docs, query, scope)
    surfaced = bool(outcome.hits or outcome.superseded_notes)
    text = (
        render_search_results(outcome, docs, lead, checkout) if surfaced else render_no_match(lead)
    )
    return RenderedResult(text, surfaced, outcome, None, n_searched)


DRAFTS_NAMED_MAX = 5


def drafts_note(drafts: list[Doc]) -> str:
    ids = ", ".join(_escape_controls(doc.id) for doc in drafts[:DRAFTS_NAMED_MAX])
    if len(drafts) > DRAFTS_NAMED_MAX:
        ids += f" +{len(drafts) - DRAFTS_NAMED_MAX} more"
    # one text on both channels: a search's result lines are the same on CLI and MCP (AC-09.4)
    return (
        f"unsaved drafts also match (not reviewed at save, not ranked, not citable): {ids} -- "
        "read one with `engmem read <id>` (MCP: engmem_read)"
    )


def stray_note(count: int) -> str:
    return (
        f"({count} markdown file(s) sit outside the searched set — only "
        f"sessions/*.md is read, not the store root and not subdirectories. "
        f"They may hold the answer.)"
    )


def _related_paths_shown(doc: Doc, by_id: dict[str, Doc]) -> list[str]:
    """The ids a hit block's `related:` line gives a path for, which `/engmem` loads next."""
    shown: list[str] = []
    for related_id in doc.related[:RELATED_MAX]:
        related = by_id.get(related_id)
        if related is not None and related.status == "superseded":
            related = by_id.get(related.superseded_by or "")
        if related is not None:
            shown.append(related.id)
    return shown


def _shown_docs(docs: list[Doc], rendered: RenderedResult) -> list[Doc]:
    """The documents whose text this result showed or gave a path for, in render order: never a
    draft, and never a superseded document a redirect line only names."""
    by_id = {doc.id: doc for doc in docs}
    if rendered.role_hits is not None:
        ids = [role_hit.doc.id for role_hit in rendered.role_hits]
    else:
        ids = []
        for doc_id in surfaced_ids(rendered.outcome):
            ids.append(doc_id)
            doc = by_id.get(doc_id)
            if doc is not None and doc.status == "active":
                ids += _related_paths_shown(doc, by_id)
    unique = list(dict.fromkeys(ids))
    return [
        by_id[i] for i in unique if i in by_id and by_id[i].status not in ("draft", "superseded")
    ]


@dataclass(frozen=True)
class _Gap:
    """A record this search could not write, and the note naming it on its own."""

    record: str
    error: Exception
    note: str
    ids: tuple[str, ...] = ()


_RECORD_ORDER = ("telemetry", "cost", "versions")
# a sharing violation: another process holds the file open, not a store refusing writes
_WINDOWS_FILE_IN_USE = (32, 33)


def _not_writable(error: Exception) -> bool:
    if getattr(error, "winerror", None) in _WINDOWS_FILE_IN_USE:
        return False
    return isinstance(error, PermissionError) or (
        isinstance(error, OSError) and error.errno == errno.EROFS
    )


def _retain_shown(store: Path, shown: list[Doc]) -> tuple[list[str], list[_Gap]]:
    """Each shown document's `<id>@<version>`, retained under `versions/` first, and one gap per
    distinct cause naming every document it hit."""
    references: list[str] = []
    failures: dict[str, tuple[Exception, list[str]]] = {}
    for doc in shown:
        try:
            references.append(f"{doc.id}{VERSION_SEPARATOR}{retain(store, doc)}")
        except (RetentionError, OSError) as exc:
            failures.setdefault(str(exc), (exc, []))[1].append(repr(doc.id))
    gaps = [
        _Gap(
            "versions",
            exc,
            f"note: version of {', '.join(ids)} not retained ({cause}) — a quote from "
            f"{'them' if len(ids) > 1 else 'it'} can only be checked against the current text",
            tuple(ids),
        )
        for cause, (exc, ids) in failures.items()
    ]
    return references, gaps


def _cite_line(references: list[str]) -> list[str]:
    return ["cite as (Reuse Log prior-doc): " + ", ".join(references)] if references else []


def cite_lines(store: Path, shown: list[Doc]) -> list[str]:
    """The `<id>@<version>` a Reuse Log row cites each shown document by, each one retained under
    `versions/` first (US-13; contracts/gate1.md, "Versioned citations")."""
    references, gaps = _retain_shown(store, shown)
    return _cite_line(references) + [gap.note for gap in gaps]


def _unwritable_note(gaps: list[_Gap]) -> str | None:
    """One line for every record the store refused, when it refused more than one
    (contracts/output.md, "One note for an unwritable store")."""
    refused = [gap for gap in gaps if _not_writable(gap.error)]
    records = [record for record in _RECORD_ORDER if any(gap.record == record for gap in refused)]
    if len(records) < 2:
        return None
    ids = list(dict.fromkeys(i for gap in refused for i in gap.ids))
    names = [
        f"versions of {', '.join(ids)}" if record == "versions" else record for record in records
    ]
    quote = (
        f", so a quote from {'them' if len(ids) > 1 else 'it'} can only be checked against the "
        "current text"
        if ids
        else ""
    )
    return (
        f"note: the store refused writing — {', '.join(names[:-1])} and {names[-1]} were not "
        f"recorded{quote} ({refused[0].error})"
    )


def compose(
    store: Path,
    loaded: LoadResult,
    strays: list[Path],
    stray_scan_errors: list[str],
    query: str,
    role: str | None,
    session_id: str | None,
    channel: Channel,
    scope: Scope | None = None,
    retain_versions: bool = True,
    record_cost: bool = True,
    name_drafts: bool = True,
) -> str:
    """The whole stdout of one search, its one telemetry row and, with `record_cost`, its one
    `cost.jsonl` row; the caller only transports it."""
    before: list[str] = []
    # "found nothing" and "could not look" must never read the same, on either channel
    if loaded.scan_error is not None:
        before.append(f"error: {loaded.scan_error.message}")
    before += [f"warning: {scan_error}" for scan_error in stray_scan_errors]

    # a new lookup per search: an MCP server outlives a checkout's HEAD moving
    rendered = render_result(loaded.docs, query, role, scope, SessionCheckout())
    before.append(rendered.text)
    # store housekeeping, deliberately outside `rendered.text` and so outside `context_bytes`
    if not rendered.surfaced_anything and strays:
        before.append(stray_note(len(strays)))
    gaps: list[_Gap] = []
    if retain_versions:
        references, gaps = _retain_shown(store, _shown_docs(loaded.docs, rendered))
        before += _cite_line(references)
    after: list[str] = []
    drafts = (
        matching_drafts(loaded.docs, query, scope, exclude=session_id, published=rendered.outcome)
        if name_drafts
        else []
    )
    if drafts:
        after.append(drafts_note(drafts))
    after.append(render_scoreboard(loaded.docs, failed=len(loaded.errors)))

    telemetry_path = store / "telemetry.jsonl"
    context_bytes = len(rendered.text.encode("utf-8"))
    scope_record = scope_row(scope, rendered.n_searched)
    if role is not None:
        telemetry_error = log_role_search(
            telemetry_path, query=query, role=role, n_docs=len(loaded.docs),
            role_hits=rendered.role_hits, session_id=session_id, channel=channel,
            context_bytes=context_bytes, scope=scope_record,
        )
    else:
        telemetry_error = log_search(
            telemetry_path, query=query, n_docs=len(loaded.docs), outcome=rendered.outcome,
            session_id=session_id, channel=channel, context_bytes=context_bytes,
            scope=scope_record,
        )
    # the search itself succeeded: report the gap in the result rather than lose the result
    if telemetry_error is not None:
        gaps.append(_Gap(
            "telemetry", telemetry_error, f"note: telemetry not recorded ({telemetry_error})"
        ))
    trailer = [_UNATTRIBUTED_NOTES[channel]] if session_id is None else []
    if record_cost:
        # the delivery is the whole text sent, so it is measured last, without only this note
        cost_error = record_delivery(
            store, op=OP_SEARCH, session_id=session_id, channel=channel,
            byte_count=len(_assemble(before, after, gaps, trailer).encode("utf-8")),
        )
        if cost_error is not None:
            gaps.append(_Gap("cost", cost_error, f"note: cost not recorded ({cost_error})"))
    return _assemble(before, after, gaps, trailer)


def _assemble(before: list[str], after: list[str], gaps: list[_Gap], trailer: list[str]) -> str:
    """Each gap's own note where it always stood, unless an unwritable store folds them into one."""
    folded = _unwritable_note(gaps)
    own = [gap for gap in gaps if folded is None or not _not_writable(gap.error)]
    versions = [gap.note for gap in own if gap.record == "versions"]
    records = [gap.note for gap in own if gap.record != "versions"]
    notes = ([folded] if folded else []) + records
    return "\n".join(before + versions + after + notes + trailer)
