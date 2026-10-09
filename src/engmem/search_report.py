"""One search result, composed once for every channel that shows it (contracts/output.md)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from engmem.cost import OP_SEARCH, record_delivery
from engmem.output import (
    RELATED_MAX,
    RoleHit,
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


def cite_lines(store: Path, shown: list[Doc]) -> list[str]:
    """The `<id>@<version>` a Reuse Log row cites each shown document by, each one retained under
    `versions/` first (US-13; contracts/gate1.md, "Versioned citations")."""
    references: list[str] = []
    # one cause, one line: an unwritable store fails every document the same way
    failures: dict[str, list[str]] = {}
    for doc in shown:
        try:
            references.append(f"{doc.id}{VERSION_SEPARATOR}{retain(store, doc)}")
        except (RetentionError, OSError) as exc:
            failures.setdefault(str(exc), []).append(repr(doc.id))
    lines = [
        f"note: version of {', '.join(ids)} not retained ({cause}) — a quote from "
        f"{'them' if len(ids) > 1 else 'it'} can only be checked against the current text"
        for cause, ids in failures.items()
    ]
    if references:
        lines.insert(0, "cite as (Reuse Log prior-doc): " + ", ".join(references))
    return lines


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
) -> str:
    """The whole stdout of one search, its one telemetry row and, with `record_cost`, its one
    `cost.jsonl` row; the caller only transports it."""
    lines: list[str] = []
    # "found nothing" and "could not look" must never read the same, on either channel
    if loaded.scan_error is not None:
        lines.append(f"error: {loaded.scan_error.message}")
    lines += [f"warning: {scan_error}" for scan_error in stray_scan_errors]

    # a new lookup per search: an MCP server outlives a checkout's HEAD moving
    rendered = render_result(loaded.docs, query, role, scope, SessionCheckout())
    lines.append(rendered.text)
    # store housekeeping, deliberately outside `rendered.text` and so outside `context_bytes`
    if not rendered.surfaced_anything and strays:
        lines.append(stray_note(len(strays)))
    if retain_versions:
        lines += cite_lines(store, _shown_docs(loaded.docs, rendered))
    lines.append(render_scoreboard(loaded.docs, failed=len(loaded.errors)))

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
        lines.append(f"note: telemetry not recorded ({telemetry_error})")
    trailer = [_UNATTRIBUTED_NOTES[channel]] if session_id is None else []
    if record_cost:
        # the delivery is the whole text sent, so it is measured last, without only this note
        cost_error = record_delivery(
            store, op=OP_SEARCH, session_id=session_id, channel=channel,
            byte_count=len("\n".join(lines + trailer).encode("utf-8")),
        )
        if cost_error is not None:
            lines.append(f"note: cost not recorded ({cost_error})")
    return "\n".join(lines + trailer)
