#!/usr/bin/env python3
"""Estimated token cost of engmem's own search output, per query (see ENGMEM-SPEC.md
search-token calibration). Not a baseline: no alternative (self-grep, no-memory) is run here."""

from __future__ import annotations

import argparse
import io
import contextlib
from pathlib import Path

from engmem.cli import main as engmem_main
from engmem.telemetry import estimate_tokens


def _search_output(query: str, store: Path) -> tuple[str, str]:
    """`(stdout, stderr)` — store diagnostics are captured and reported once, not once per query."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        engmem_main(["search", query, "--store", str(store)])
    return out.getvalue(), err.getvalue()


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

    store = Path(args.store).expanduser()
    pairs = _queries(Path(args.queries).expanduser())

    print("NOTE: this measures the estimated token cost of engmem's own search output for")
    print("each query below -- ceil(bytes / 3.5), the same estimator `engmem telemetry` uses")
    print("(see ENGMEM-SPEC.md search-token calibration). It is NOT a baseline and NOT a")
    print("savings figure: no self-grep or no-memory alternative is run or compared here.")
    print("A real comparison needs a second, deliberately separate measurement -- a fresh")
    print("agent session per query, file-reading only, counted the same way -- which this")
    print("tool does not perform.")
    print()

    print("| query | expected | estimated tokens (engmem search output) | found? |")
    print("|---|---|---|---|")

    total, found = 0, 0
    problems: list[str] = []
    for query, expected in pairs:
        output, errors = _search_output(query, store)
        for line in errors.splitlines():
            if line and line not in problems:
                problems.append(line)
        tokens = estimate_tokens(len(output.encode("utf-8")))
        hit = "found" if expected and expected in output else "not found"
        total += tokens
        found += hit == "found"
        print(f"| {query} | {expected} | {tokens} | {hit} |")

    print()
    if problems:
        print("store problems seen while measuring (same for every query):")
        for line in problems:
            print(f"  {line}")
        print()
    print(
        f"{len(pairs)} queries -- estimated tokens of engmem search output, not a baseline, "
        f"not compared against any alternative: {total} total, found {found}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
