#!/usr/bin/env python3
"""Side A of the search-vs-self-grep baseline (`ENGMEM-SPEC.md` §11).

    python tools/baseline_cost.py --store <store> --queries queries.tsv

`queries.tsv`: `query<TAB>expected-doc-id` per line. Side B needs a fresh agent session,
so that column is left empty.
"""

from __future__ import annotations

import argparse
import io
import contextlib
from pathlib import Path

from engmem.cli import main as engmem_main
from engmem.telemetry import estimate_tokens


def _search_output(query: str, store: Path) -> tuple[str, str]:
    """`(stdout, stderr)`. The store's own diagnostics are captured rather than left to
    interleave with the table — the same problem would otherwise be printed once per
    query. They are reported at the end instead, never dropped: a baseline measured over
    a store that is quietly missing a document is not a baseline."""
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

    print("| query | expected | tokens A (engmem) | A found? | tokens B (self-grep) | B found? |")
    print("|---|---|---|---|---|---|")

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
        print(f"| {query} | {expected} | {tokens} | {hit} | | |")

    print()
    if problems:
        print("store problems seen while measuring (same for every query):")
        for line in problems:
            print(f"  {line}")
        print()
    print(f"{len(pairs)} queries   side A: {total} tokens total, found {found}")
    print("Side B is yours: a fresh agent session per query, told to find the same document")
    print("with file reading only. Count its tool output the same way — bytes / 3.5.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
