#!/usr/bin/env python3
"""Checks that every Reuse Log quote occurs in the document it cites; exit 1 when one cannot be
verified."""

from __future__ import annotations

import argparse
from pathlib import Path

from engmem import gate1


def verify(store: Path) -> tuple[list[str], list[str], list[str]]:
    """`(problems, stale, conflicts)` — only `problems` decides the exit code."""
    verdicts = gate1.evaluate(store)
    problems: list[str] = []
    stale: list[str] = []

    for row in verdicts.rows:
        if row.integrity == "no_quote":
            problems.append(f"{row.source}: no quote in the `taken` cell (cites {row.cited_id})")
        elif row.integrity == "cited_missing":
            problems.append(f"{row.source}: cited document {row.cited_id} is not in the store")
        elif row.integrity == "quote_not_found":
            for quote in row.unfound_quotes:
                problems.append(f'{row.source}: quote not found in {row.cited_id} — "{quote}"')

        # a genuine quote from a document that has since been replaced: not a fabrication, but
        # reuse of stale knowledge — and after the session ends the two read the same
        if row.staleness == "cited_superseded":
            successor = row.cited.superseded_by or "(no successor recorded)"
            stale.append(f"{row.source}: cites {row.cited_id}, superseded by {successor}")

    conflicts = [
        # neither skipped nor silently resolved: every row above was still checked on its own
        # merits, and this line says the section's own "no reuse" claim contradicts that
        f"{conflict.doc_id}: Reuse Log has both 'Prior docs used: none.' and "
        f"{conflict.row_count} row(s) — every row above was still checked individually"
        for conflict in verdicts.conflicts
    ]

    return problems, stale, conflicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_citations")
    parser.add_argument("--store", required=True, help="store directory holding sessions/")
    args = parser.parse_args(argv)

    problems, stale, conflicts = verify(Path(args.store).expanduser())
    for problem in problems:
        print(problem)
    for note in stale:
        print(note)
    for conflict in conflicts:
        print(conflict)
    print(
        f"verify_citations: {len(problems)} unverifiable citation(s), "
        f"{len(stale)} citing a superseded document, "
        f"{len(conflicts)} with a conflicting Reuse Log"
    )
    # only an unverifiable quote fails the run: a superseded citation or a conflicting
    # section verified fine and is a finding for the review, not a defect that fails it
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
