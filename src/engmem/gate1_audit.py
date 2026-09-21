"""Sample-completeness audit for Gate 1: did the session-per-session experimental record run
the ritual at all, not whether any one Reuse Log row is individually valid (that question is
`gate1.py`'s). Rationale and the six counters this module answers: contracts/gate1.md,
"The audit coverage block."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from engmem import gate1
from engmem.sections import split_sections
from engmem.spine import Doc, load_store
from engmem.telemetry import SessionRow, read_session_rows

# the Search Trace section's own vocabulary (ENGMEM-SPEC.md §4, templates/engmem.start.md
# step 5) -- `shell`/`paste` both mean a search actually ran; `miss` means it never did, so a
# `miss` document has nothing to reconstruct and is not part of the cross-check below
TRACE_RAN = frozenset({"shell", "paste"})
TRACE_VALUES = TRACE_RAN | {"miss"}


@dataclass
class AuditReport:
    window_label: str
    since_error: str | None  # a `--since` value this module could not parse; window falls back
    # to all-time rather than failing the report (gate1_report.py exits 0 unconditionally)

    draft_count: int
    active_count: int
    superseded_count: int
    backfilled_count: int  # ENGMEM-SPEC.md §4: "docs written after the fact" never ran the
    # ritual -- excluded from draft/active/superseded and from the missing-section lists below,
    # counted here on its own. Its sibling exclusion is `no_prereg_docs` below;
    # a document matching both is counted here only.

    telemetry_error: str | None  # None when telemetry.jsonl was read cleanly; otherwise every
    # telemetry-derived field below is at its empty/zero default and must be rendered as
    # UNMEASURED, never as a true zero -- gate1_report.py still exits 0

    telemetry_rows_with_session: int
    telemetry_distinct_sessions: int

    reuse_log_count: int  # any status, by section PRESENCE (`_has_role`) -- the same rule
    # `active_missing_trace` uses
    none_report_count: int  # gate1.Verdicts.none_reports, reused rather than re-derived

    no_prereg_docs: list[str] = field(default_factory=list)  # any status, not backfilled, no
    # `## Pre-reg` section -- written before the ritual existed, or outside it; excluded from the
    # ritual population and named here so the sample's composition stays visible

    active_missing_reuse: list[str] = field(default_factory=list)
    active_missing_trace: list[str] = field(default_factory=list)

    orphan_session_ids: list[str] = field(default_factory=list)  # session_id -> no doc here
    orphan_row_count: int = 0

    unreconstructable_docs: list[str] = field(default_factory=list)  # trace says shell/paste,
    # zero telemetry rows carry this doc's id as session_id


def _parse_since(raw: str) -> tuple[datetime | None, str | None]:
    """UTC-anchored: a bare date is midnight UTC that day. `(None, reason)` on a value this
    parser cannot read; the caller falls back to all-time rather than failing the report."""
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, f"--since {raw!r} is not a recognized ISO date/datetime -- using all-time"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, None


def _in_window(row: SessionRow, since: datetime | None) -> bool:
    if since is None:
        return True
    return row.ts is not None and row.ts >= since


def _trace_value(doc: Doc) -> str | None:
    """The document's `## Search Trace` value, or `None` when the section is absent or no line
    in it is EXACTLY `shell`/`paste`/`miss` (an earlier, looser word-by-word reader
    read a provenance claim out of ordinary prose in both directions -- a `miss` document
    scanned as `shell`, and a genuine `shell` document scanned as `miss`, whenever the section
    also carried a sentence mentioning the other words. Reverted to the exact whole-line match;
    see contracts/gate1.md, "The telemetry figures," "Recorded reversal, the reader.")."""
    for section in split_sections(doc.body):
        if section.canonical != "trace":
            continue
        for line in section.body.splitlines():
            word = line.strip().strip(".:").casefold()
            if word in TRACE_VALUES:
                return word
    return None


def _has_role(doc: Doc, role: str) -> bool:
    """Section PRESENCE only -- a `## Reuse Log` holding only the template's own header row and
    separator (`templates/engmem.save.md`) still counts as present; content is a different
    question (`none_report_count`, or `gate1.py`'s row verdicts) -- see contracts/gate1.md,
    "A separate module, and exactly one figure from `verdicts`," its "Recorded reversal."
    paragraph."""
    return any(s.canonical == role for s in split_sections(doc.body))


def evaluate(
    store: Path, verdicts: gate1.Verdicts, *, since: str | None = None
) -> AuditReport:
    """`verdicts` is `gate1.evaluate(store)`, passed in rather than recomputed here, reused for
    exactly one figure: `none_report_count`, from `verdicts.none_reports`, which already tells a
    clean "none" report apart from one conflicted by real rows alongside it -- re-deriving that
    with a second, simpler check would regress a bug this project already fixed once. Every
    other figure below -- document status, section presence by role, the telemetry join -- is
    computed directly from `load_store`/`split_sections`, not through `gate1.evaluate()`'s
    heavier per-row machinery. See contracts/gate1.md, "The audit coverage block.\""""
    result = load_store(store / "sessions")
    docs = {d.id: d for d in result.docs}

    # ENGMEM-SPEC.md §4: a `backfilled: true` document was "written after the fact" -- it never
    # started the ritual, never ran a search, never had a Pre-reg baseline. Excluded from the
    # ritual population (draft/active/superseded) and from the missing-section lists below;
    # counted on its own rather than silently folded into either side.
    backfilled_count = sum(1 for d in docs.values() if d.backfilled)
    # A missing `## Pre-reg` section is the evidence a `backfilled: true` flag nobody stamped
    # would have carried: the document never ran the ritual either. Counted once, under
    # `backfilled_count`, when both apply. ENGMEM-SPEC.md §11, amendment of 2026-09-12;
    # contracts/gate1.md, "(e) A document with no Pre-reg section never ran the ritual either."
    no_prereg_docs = sorted(
        d.id for d in docs.values() if not d.backfilled and not _has_role(d, "prereg")
    )
    ritual_docs = [d for d in docs.values() if not d.backfilled and _has_role(d, "prereg")]

    draft = [d for d in ritual_docs if d.status == "draft"]
    active = [d for d in ritual_docs if d.status == "active"]
    superseded = [d for d in ritual_docs if d.status == "superseded"]

    since_dt: datetime | None = None
    since_error: str | None = None
    if since is not None:
        since_dt, since_error = _parse_since(since)
    window_label = "all-time" if since_dt is None else f"since {since_dt.isoformat()}"
    if since_error is not None:
        window_label = f"all-time ({since_error})"

    try:
        all_session_rows = read_session_rows(store / "telemetry.jsonl")
        telemetry_error: str | None = None
    except (OSError, UnicodeDecodeError) as exc:
        # gate1_report.py exits 0 unconditionally (contracts/gate1.md) -- a torn append or an
        # unreadable telemetry.jsonl (concurrent writers: cli.py and mcp_server.py) must not take
        # down the per-row table or the §11 endpoint figure it is printed alongside. Every
        # telemetry-derived field below stays at its empty/zero default; the renderer names them
        # UNMEASURED rather than printing a false zero.
        all_session_rows = []
        telemetry_error = str(exc)

    session_rows = [r for r in all_session_rows if _in_window(r, since_dt)]
    sessions_by_id: dict[str, list[SessionRow]] = {}
    for row in session_rows:
        sessions_by_id.setdefault(row.session_id, []).append(row)

    orphan_ids: list[str] = []
    orphan_row_count = 0
    unreconstructable: list[str] = []
    if telemetry_error is None:
        orphan_ids = sorted(sid for sid in sessions_by_id if sid not in docs)
        orphan_row_count = sum(len(sessions_by_id[sid]) for sid in orphan_ids)
        # every document, ritual or not: this figure fires on an affirmative claim a
        # document makes about itself, and a claim is owed evidence whoever made it
        unreconstructable = sorted(
            d.id for d in docs.values()
            if _trace_value(d) in TRACE_RAN and not sessions_by_id.get(d.id)
        )

    return AuditReport(
        window_label=window_label,
        since_error=since_error,
        draft_count=len(draft),
        active_count=len(active),
        superseded_count=len(superseded),
        backfilled_count=backfilled_count,
        telemetry_error=telemetry_error,
        telemetry_rows_with_session=len(session_rows),
        telemetry_distinct_sessions=len(sessions_by_id),
        reuse_log_count=sum(1 for d in docs.values() if _has_role(d, "reuse")),
        none_report_count=len(verdicts.none_reports),
        no_prereg_docs=no_prereg_docs,
        active_missing_reuse=sorted(d.id for d in active if not _has_role(d, "reuse")),
        active_missing_trace=sorted(d.id for d in active if not _has_role(d, "trace")),
        orphan_session_ids=orphan_ids,
        orphan_row_count=orphan_row_count,
        unreconstructable_docs=unreconstructable,
    )
