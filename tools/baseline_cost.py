#!/usr/bin/env python3
"""Estimated token cost of engmem's own search result, per query, as telemetry records it (see
ENGMEM-SPEC.md search-token calibration). Not a baseline: no alternative is run here."""

from __future__ import annotations

import argparse
from pathlib import Path

from engmem.runtime import fail, force_utf8_streams
from engmem.search_report import render_result, stray_note
from engmem.spine import load_store, sessions_dir_unreadable, stray_documents
from engmem.telemetry import estimate_tokens


def _queries(path: Path) -> list[tuple[str, str]]:
    pairs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        query, _, expected = line.partition("\t")
        pairs.append((query.strip(), expected.strip()))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baseline_cost")
    parser.add_argument("--store", required=True)
    parser.add_argument("--queries", required=True, help="query<TAB>expected-doc-id per line")
    args = parser.parse_args(argv)
    force_utf8_streams()

    store = Path(args.store).expanduser()
    sessions = store / "sessions"
    unreadable = sessions_dir_unreadable(sessions)
    if unreadable or not sessions.is_dir():
        # before the table: a wrong --store would otherwise read as a column of "none found"
        fail(unreadable or f"store not found: {sessions} does not exist — check --store")
        return 2
    pairs = _queries(Path(args.queries).expanduser())

    print("NOTE: this measures the estimated token cost of engmem's own search result for")
    print("each query below -- the bytes telemetry records as context_bytes, at")
    print("ceil(bytes / 3.5), the same estimator `engmem telemetry` uses")
    print("(see ENGMEM-SPEC.md search-token calibration). It is NOT a baseline and NOT a")
    print("savings figure: no self-grep or no-memory alternative is run or compared here.")
    print("A real comparison needs a second, deliberately separate measurement -- a fresh")
    print("agent session per query, file-reading only, counted the same way -- which this")
    print("tool does not perform.")
    print()

    print("| query | expected | estimated tokens (engmem search result) | found? |")
    print("|---|---|---|---|")

    # loaded once and rendered without `compose`: measuring must not add the telemetry row a
    # search writes, and the figure is the rendered result `context_bytes` records, nothing else
    loaded = load_store(sessions)
    problems = [f"error: {p.message}" for p in loaded.errors]
    problems += [f"warning: {p.message}" for p in loaded.warnings]
    strays, stray_scan_errors = stray_documents(store)
    if strays:
        problems.append(f"warning: {stray_note(len(strays))}")
    problems += [f"warning: {scan_error}" for scan_error in stray_scan_errors]

    total, found = 0, 0
    for query, expected in pairs:
        text = render_result(loaded.docs, query, None).text
        tokens = estimate_tokens(len(text.encode("utf-8")))
        hit = "found" if expected and expected in text else "not found"
        total += tokens
        found += hit == "found"
        print(f"| {query} | {expected} | {tokens} | {hit} |")

    print()
    if problems:
        print("store problems seen while measuring:")
        for line in problems:
            print(f"  {line}")
        print()
    print(
        f"{len(pairs)} queries -- estimated tokens of engmem search result, not a baseline, "
        f"not compared against any alternative: {total} total, found {found}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
