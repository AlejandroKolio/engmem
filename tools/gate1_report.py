#!/usr/bin/env python3
"""Every Reuse Log row in one table for the Gate 1 review.

    python tools/gate1_report.py --store <store>

Adjacent = shares a repo or tag with the citing document, or one `related` edge
(`ENGMEM-SPEC.md` §11). The verdict column is the human's. `repos` is read from the front
matter directly — `spine.py` does not parse it.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

from engmem.sections import split_sections
from engmem.spine import load_store

QUOTE_RE = re.compile(r'"([^"\n]{4,})"|“([^”\n]{4,})”|«([^»\n]{4,})»')
NONE_LINE = "prior docs used: none."


def _repos(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8-sig")
    if not text.startswith("---"):
        return set()
    end = text.find("\n---", 3)
    try:
        raw = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return set()
    value = raw.get("repos") if isinstance(raw, dict) else None
    return {str(v) for v in value} if isinstance(value, list) else set()


def _distance(citing, cited, repos: dict[str, set[str]]) -> str:
    if cited.id in citing.related or citing.id in cited.related:
        return "adjacent"
    if repos.get(citing.id, set()) & repos.get(cited.id, set()):
        return "adjacent"
    if set(citing.tags) & set(cited.tags):
        return "adjacent"
    return "distant"


def _rows(section_body: str) -> list[list[str]]:
    out = []
    for line in section_body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or re.fullmatch(r"[|\s:-]+", stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) >= 2 and cells[0].casefold() not in ("prior-doc", ""):
            out.append(cells)
    return out


def report(store: Path) -> tuple[list[str], int, int]:
    docs = {d.id: d for d in load_store(store / "sessions").docs}
    repos = {d.id: _repos(Path(d.path)) for d in docs.values()}
    lines, distant, none_count = [], 0, 0

    for doc in sorted(docs.values(), key=lambda d: d.id):
        reuse = next((s for s in split_sections(doc.body) if s.canonical == "reuse"), None)
        if reuse is None:
            continue
        if NONE_LINE in reuse.body.casefold():
            none_count += 1
            continue

        for cells in _rows(reuse.body):
            cited_id = cells[0].strip("[] `")
            cited = docs.get(cited_id)
            quotes = [next(g for g in m.groups() if g) for m in QUOTE_RE.finditer(cells[1])]
            quote = quotes[0] if quotes else "(no quote)"
            distance = _distance(doc, cited, repos) if cited else "cited doc not in store"
            distant += distance == "distant"
            lines.append(f"| {doc.id} | {cited_id} | {quote} | {distance} | |")

    return lines, distant, none_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gate1_report")
    parser.add_argument("--store", required=True, help="store directory holding sessions/")
    args = parser.parse_args(argv)

    lines, distant, none_count = report(Path(args.store).expanduser())

    print("| story | cited | quote | distance | changed a decision? |")
    print("|---|---|---|---|---|")
    for line in lines:
        print(line)
    print()
    print(f"reuse rows: {len(lines)}   distant: {distant}   "
          f"documents reporting no reuse: {none_count}")
    print("The last column is yours. ENGMEM-SPEC.md §11 holds the endpoints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
