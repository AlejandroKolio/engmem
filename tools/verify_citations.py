#!/usr/bin/env python3
"""Check that every Reuse Log quote occurs in the document it cites.

    python tools/verify_citations.py --store <store>

A quote is a span in double quotes or guillemets; backticks mark the artifact, not the
quotation. Exit 0 when clean, 1 when a quote cannot be verified.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from engmem.sections import split_sections
from engmem.spine import load_store

QUOTE_RE = re.compile(r'"([^"\n]{4,})"|“([^”\n]{4,})”|«([^»\n]{4,})»')
NONE_LINE = "prior docs used: none."


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _rows(section_body: str) -> list[str]:
    return [
        line for line in section_body.splitlines()
        if line.strip().startswith("|") and not re.fullmatch(r"[|\s:-]+", line.strip())
    ]


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _line_number(path: Path, row: str) -> int:
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if line.strip() == row.strip():
            return n
    return 0


def verify(store: Path) -> tuple[list[str], list[str]]:
    result = load_store(store / "sessions")
    bodies = {d.id: _normalized(d.body) for d in result.docs}
    docs = {d.id: d for d in result.docs}
    problems: list[str] = []
    stale: list[str] = []

    for doc in result.docs:
        reuse = next((s for s in split_sections(doc.body) if s.canonical == "reuse"), None)
        if reuse is None or NONE_LINE in reuse.body.casefold():
            continue

        for row in _rows(reuse.body):
            cells = _cells(row)
            if len(cells) < 2 or cells[0].casefold() in ("prior-doc", ""):
                continue
            cited = cells[0].strip("[] `")
            where = f"{Path(doc.path).name}:{_line_number(Path(doc.path), row)}"

            quotes = [next(g for g in m.groups() if g) for m in QUOTE_RE.finditer(cells[1])]
            if not quotes:
                problems.append(f"{where}: no quote in the `taken` cell (cites {cited})")
                continue
            if cited not in bodies:
                problems.append(f"{where}: cited document {cited} is not in the store")
                continue
            for quote in quotes:
                if _normalized(quote) not in bodies[cited]:
                    problems.append(f'{where}: quote not found in {cited} — "{quote}"')

            # a genuine quote from a document that has since been replaced: not a
            # fabrication, but reuse of stale knowledge — and after the session ends the
            # two read the same
            if docs[cited].status == "superseded":
                successor = docs[cited].superseded_by or "(no successor recorded)"
                stale.append(f"{where}: cites {cited}, superseded by {successor}")

    return problems, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_citations")
    parser.add_argument("--store", required=True, help="store directory holding sessions/")
    args = parser.parse_args(argv)

    problems, stale = verify(Path(args.store).expanduser())
    for problem in problems:
        print(problem)
    for note in stale:
        print(note)
    print(
        f"verify_citations: {len(problems)} unverifiable citation(s), "
        f"{len(stale)} citing a superseded document"
    )
    # only an unverifiable quote fails the run: a superseded citation verified fine and is
    # a finding for the review, not a defect in the document
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
