"""One search result, composed once for every channel that shows it (contracts/output.md)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from engmem.output import (
    RoleHit,
    render_no_match,
    render_role_search_results,
    render_scoreboard,
    render_search_results,
    select_role_hits,
)
from engmem.scoring import SearchOutcome, search, search_with_role_sections
from engmem.spine import Doc, LoadResult
from engmem.telemetry import (
    UNATTRIBUTED_CLI_NOTE,
    UNATTRIBUTED_MCP_NOTE,
    log_role_search,
    log_search,
)

Channel = Literal["cli", "mcp"]

_UNATTRIBUTED_NOTES = {"cli": UNATTRIBUTED_CLI_NOTE, "mcp": UNATTRIBUTED_MCP_NOTE}


@dataclass(frozen=True)
class RenderedResult:
    text: str  # exactly what `context_bytes` measures, and nothing else
    surfaced_anything: bool
    outcome: SearchOutcome
    role_hits: list[RoleHit] | None  # role search only


def render_result(docs: list[Doc], query: str, role: str | None) -> RenderedResult:
    """The ranked result for one query, with no I/O: what a search shows, before any store note."""
    if role is not None:
        outcome, role_map = search_with_role_sections(docs, query)
        role_hits, _matched_role_total = select_role_hits(outcome, role_map, role)
        text = render_role_search_results(outcome, role_map, role)
        return RenderedResult(text, bool(role_hits), outcome, role_hits)

    outcome = search(docs, query)
    surfaced = bool(outcome.hits or outcome.superseded_notes)
    text = render_search_results(outcome, docs) if surfaced else render_no_match()
    return RenderedResult(text, surfaced, outcome, None)


def stray_note(count: int) -> str:
    return (
        f"({count} markdown file(s) sit outside the searched set — only "
        f"sessions/*.md is read, not the store root and not subdirectories. "
        f"They may hold the answer.)"
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
) -> str:
    """The whole stdout of one search, and its one telemetry row; the caller only transports it."""
    lines: list[str] = []
    # "found nothing" and "could not look" must never read the same, on either channel
    if loaded.scan_error is not None:
        lines.append(f"error: {loaded.scan_error.message}")
    lines += [f"warning: {scan_error}" for scan_error in stray_scan_errors]

    rendered = render_result(loaded.docs, query, role)
    lines.append(rendered.text)
    # store housekeeping, deliberately outside `rendered.text` and so outside `context_bytes`
    if not rendered.surfaced_anything and strays:
        lines.append(stray_note(len(strays)))
    lines.append(render_scoreboard(loaded.docs, failed=len(loaded.errors)))

    telemetry_path = store / "telemetry.jsonl"
    context_bytes = len(rendered.text.encode("utf-8"))
    if role is not None:
        telemetry_error = log_role_search(
            telemetry_path, query=query, role=role, n_docs=len(loaded.docs),
            role_hits=rendered.role_hits, session_id=session_id, channel=channel,
            context_bytes=context_bytes,
        )
    else:
        telemetry_error = log_search(
            telemetry_path, query=query, n_docs=len(loaded.docs), outcome=rendered.outcome,
            session_id=session_id, channel=channel, context_bytes=context_bytes,
        )
    # the search itself succeeded: report the gap in the result rather than lose the result
    if telemetry_error is not None:
        lines.append(f"note: telemetry not recorded ({telemetry_error})")
    if session_id is None:
        lines.append(_UNATTRIBUTED_NOTES[channel])
    return "\n".join(lines)
