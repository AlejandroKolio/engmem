#!/usr/bin/env python3
"""Every Reuse Log row in one table for the Gate 1 review; the verdict column is the human's,
except where the row is terminally excluded -- there the tool fills the reason in (contracts/gate1.md)."""

from __future__ import annotations

import argparse
from pathlib import Path

from engmem import gate1, gate1_audit

HEADER = ["story", "cited", "quote", "integrity", "classification", "distance", "staleness",
          "changed a decision?"]


def _quote(row: gate1.RowVerdict) -> str:
    return row.quotes[0] if row.quotes else "(no quote)"


def _row_line(row: gate1.RowVerdict) -> str:
    cells = [
        row.citing.id,
        row.cited_id,
        _quote(row),
        row.integrity,
        row.classification,
        row.distance or "n/a",
        row.staleness or "n/a",
        gate1.exclusion_reason(row) or "",
    ]
    return "| " + " | ".join(cells) + " |"


def _count(rows: list[gate1.RowVerdict], field: str, value: str) -> int:
    return sum(1 for r in rows if getattr(r, field) == value)


def report(store: Path) -> tuple[list[str], gate1.Verdicts]:
    verdicts = gate1.evaluate(store)
    lines = [_row_line(row) for row in verdicts.rows]
    return lines, verdicts


def _summary(verdicts: gate1.Verdicts) -> list[str]:
    rows = verdicts.rows
    valid = [r for r in rows if gate1.is_valid(r)]
    # sourced through gate1.is_primary_candidate, not recomputed inline, so the printed
    # figure and the pre-registered endpoint can never drift apart (contracts/gate1.md)
    distant = [r for r in rows if gate1.is_primary_candidate(r)]
    adjacent = [r for r in valid if r.distance == "adjacent"]
    undecidable = [r for r in valid if r.distance == "undecidable"]
    stale_distant = sum(1 for r in distant if r.staleness == "cited_superseded")
    dogfooding_excluded = sum(1 for r in rows if r.dogfooding)
    status_excluded = sum(1 for r in rows if gate1.excluded_by_status(r) is not None)

    lines = [
        f"rows: {len(rows)}",
        "citation integrity: "
        f"{_count(rows, 'integrity', 'verified')} verified, "
        f"{_count(rows, 'integrity', 'no_quote')} no quote, "
        f"{_count(rows, 'integrity', 'cited_missing')} cited doc not in store, "
        f"{_count(rows, 'integrity', 'quote_not_found')} quote not found",
        "classification: "
        f"{_count(rows, 'classification', 'reuse')} reuse, "
        f"{_count(rows, 'classification', 'anti-reuse')} anti-reuse, "
        f"{_count(rows, 'classification', 'harmful')} harmful, "
        f"{_count(rows, 'classification', 'missing')} classification missing, "
        f"{_count(rows, 'classification', 'unrecognized')} classification not recognized",
        f"rows excluded from the count: {len(rows) - len(valid)}",
        "excluded from the count, by axis (a row may match more than one -- see the total "
        "above for the true count, not the sum of these): "
        f"{dogfooding_excluded} dogfooding (story about this repository), "
        f"{status_excluded} citing document not active (draft/superseded)",
        f"{len(valid)} valid (the last column above is theirs to fill), of which "
        f"{len(distant)} distant, {len(adjacent)} adjacent, {len(undecidable)} undecidable",
        f"of the {len(distant)} distant, {stale_distant} cite a superseded document "
        "(flagged, not excluded -- see the staleness column)",
        "ENGMEM-SPEC.md section 11's primary endpoint counts the distant figure above.",
        f"documents reporting no reuse: {len(verdicts.none_reports)}",
    ]
    if verdicts.conflicts:
        lines.append("")
        lines.append(
            "Reuse Log conflicts (both 'Prior docs used: none.' and real row(s) present -- "
            "not silently resolved; every row above was still scored on its own merits):"
        )
        for conflict in verdicts.conflicts:
            lines.append(f"  {conflict.doc_id}: {conflict.row_count} row(s)")
    return lines


def _missing_line(label: str, ids: list[str]) -> str:
    """`label: N` alone when nothing is missing; `label: N -- id, id, ...` when something is,
    so the reader never has to cross-reference a count against a second table to act on it."""
    line = f"{label}: {len(ids)}"
    if ids:
        line += " -- " + ", ".join(ids)
    return line


def _telemetry_line(label: str, audit: gate1_audit.AuditReport, measured: str) -> str:
    """`measured` is the normal-path text; when telemetry.jsonl could not be read or decoded
    (ARCH-101), every telemetry-derived line says so explicitly instead of printing a false
    zero -- `gate1_report.py` still exits 0 either way (contracts/gate1.md)."""
    if audit.telemetry_error is None:
        return f"{label}: {measured}"
    return (
        f"{label}: UNMEASURED -- telemetry.jsonl unreadable ({audit.telemetry_error}), not zero"
    )


def _audit_lines(audit: gate1_audit.AuditReport) -> list[str]:
    """Sample-completeness, not row validity (that's `_summary` above) -- a session that never
    ran a search, or a document whose search cannot be traced back to a telemetry row, is
    invisible to the per-row table but is exactly the gap this block exists to name.
    contracts/gate1.md, "The audit coverage block."."""
    started = audit.draft_count + audit.active_count
    orphan_ids_suffix = " -- " + ", ".join(audit.orphan_session_ids) if audit.orphan_session_ids else ""
    unreconstructable_suffix = (
        " -- " + ", ".join(audit.unreconstructable_docs) if audit.unreconstructable_docs else ""
    )
    lines = [
        f"window: {audit.window_label}",
        f"ritual: {started} started (draft: {audit.draft_count} + active: {audit.active_count}) "
        f"-> {audit.active_count} completed (status: active); "
        f"{audit.superseded_count} superseded document(s) counted in neither figure -- a "
        "superseded document did complete the ritual once, but this pair answers 'is the "
        "ritual currently incomplete', not 'did it ever finish'; "
        f"{audit.backfilled_count} backfilled document(s) also counted in neither figure -- "
        "a backfilled document never ran the ritual (ENGMEM-SPEC.md section 4: "
        "'docs written after the fact'); "
        f"{len(audit.no_prereg_docs)} document(s) with no Pre-reg section also counted in "
        "neither figure -- written before the ritual existed, or outside it",
    ]
    if audit.telemetry_error is not None:
        lines.append(
            f"telemetry.jsonl: UNREADABLE ({audit.telemetry_error}) -- every telemetry-derived "
            "figure below is UNMEASURED, not zero; the table and summary above are unaffected"
        )
    lines.append(_telemetry_line(
        "telemetry searches carrying a session_id in this window", audit,
        f"{audit.telemetry_rows_with_session} row(s) across "
        f"{audit.telemetry_distinct_sessions} distinct session(s) -- both figures are named "
        "on purpose: rows-per-session is its own signal, never folded into one number",
    ))
    lines.append(f"session documents with a Reuse Log section (any status): {audit.reuse_log_count}")
    lines.append(
        "session documents honestly reporting 'Prior docs used: none.': "
        f"{audit.none_report_count}"
    )
    lines.append(_missing_line("active documents missing a Reuse Log section", audit.active_missing_reuse))
    lines.append(_missing_line("active documents missing a Search Trace section", audit.active_missing_trace))
    lines.append(_missing_line(
        "session documents with no Pre-reg section, excluded from the ritual population (any "
        "status; a backfilled document is counted on the backfilled figure instead)",
        audit.no_prereg_docs,
    ))
    lines.append(_telemetry_line(
        "telemetry rows whose session_id matches no document in this store", audit,
        f"{audit.orphan_row_count} row(s) across {len(audit.orphan_session_ids)} session_id(s)"
        + orphan_ids_suffix,
    ))
    lines.append(_telemetry_line(
        "documents whose Search Trace says shell/paste but carry zero matching telemetry rows "
        "(retrieval provenance cannot be reconstructed)", audit,
        f"{len(audit.unreconstructable_docs)}" + unreconstructable_suffix,
    ))
    lines.append("")
    lines.append(
        "Caveat: --session is deliberately unvalidated (ENGMEM-SPEC.md section 5, search step "
        "6) -- the orphan-row figure above also counts typos and cross-store contamination, "
        "not only genuine gaps."
    )
    lines.append(
        "Caveat: telemetry.jsonl is append-only with no rotation -- pre-Gate-1 and testing rows "
        "are indistinguishable from experimental rows within the window above; quote the "
        "window line with any figure from this block."
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gate1_report")
    parser.add_argument("--store", required=True, help="store directory holding sessions/")
    parser.add_argument(
        "--since", default=None,
        help="ISO date/datetime; only telemetry rows at or after it count toward the audit "
             "coverage block below (default: all-time, unchanged from every prior run)",
    )
    args = parser.parse_args(argv)

    store = Path(args.store).expanduser()
    lines, verdicts = report(store)

    # printed in full BEFORE the audit block is computed (ARCH-101): the per-row table and the
    # §11 endpoint figure must reach stdout even if telemetry.jsonl -- read only by the audit
    # block below -- turns out to be unreadable or undecodable.
    print("| " + " | ".join(HEADER) + " |")
    print("|" + "---|" * len(HEADER))
    for line in lines:
        print(line)
    print()
    for summary_line in _summary(verdicts):
        print(summary_line)
    print("The last column is yours where the tool left it blank.")
    print()

    audit = gate1_audit.evaluate(store, verdicts, since=args.since)
    print("=== Audit coverage (sample completeness -- not a schema gate; see contracts/gate1.md) ===")
    for audit_line in _audit_lines(audit):
        print(audit_line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
