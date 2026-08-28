"""Per-row Reuse Log verdicts shared by `tools/verify_citations.py` and `tools/gate1_report.py`.

Rules and their reasons: contracts/gate1.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from engmem.sections import split_sections
from engmem.spine import Doc, _coerce_list_field, load_store, split_front_matter

QUOTE_RE = re.compile(r'"([^"\n]{4,})"|“([^”\n]{4,})”|«([^»\n]{4,})»')
NONE_LINE = "prior docs used: none."

VALID_CLASSIFICATIONS = frozenset({"reuse", "anti-reuse", "harmful"})
KNOWN_STATUSES_EXCLUDED_FROM_THE_COUNT = frozenset({"draft", "superseded"})
# this repository's own package name (pyproject.toml) -- see contracts/gate1.md
# "Dogfooding identification" for why `repos` and not `covers_files` or a deny-list
DOGFOODING_REPOS = frozenset({"engmem"})


@dataclass
class RowVerdict:
    citing: Doc
    cited: Doc | None
    cited_id: str  # as written in the row; may not resolve when integrity == "cited_missing"
    source: str  # "<filename>:<line>", for human-readable messages
    quotes: tuple[str, ...]
    integrity: str  # no_quote | cited_missing | quote_not_found | verified
    unfound_quotes: tuple[str, ...]
    classification_raw: str
    classification: str  # reuse | anti-reuse | harmful | missing | unrecognized
    distance: str | None  # adjacent | distant | undecidable -- set only when integrity == verified
    staleness: str | None  # cited_active | cited_superseded -- set whenever `cited` resolved
    dogfooding: bool


@dataclass
class DocConflict:
    doc_id: str
    row_count: int


@dataclass
class Verdicts:
    rows: list[RowVerdict]
    none_reports: list[str]  # doc ids that cleanly reported "Prior docs used: none."
    conflicts: list[DocConflict]  # both the none-sentence AND real rows -- reported, not resolved


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class _TableRow:
    line: str
    cells: list[str]


def _iter_rows(section_body: str) -> list[_TableRow]:
    rows = []
    for line in section_body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or re.fullmatch(r"[|\s:-]+", stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) >= 2 and cells[0].casefold() not in ("prior-doc", ""):
            rows.append(_TableRow(line=line, cells=cells))
    return rows


def _line_number(path: Path, row_line: str) -> int:
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if line.strip() == row_line.strip():
            return n
    return 0


def _repos(path: Path) -> set[str] | None:
    """`None` means the front matter could not be read for `repos` -- distinct from "read fine,
    no repos field". Uses `spine.split_front_matter`/`_coerce_list_field` directly rather than a
    second, weaker parser -- see contracts/gate1.md, "Front matter is parsed once"."""
    text = path.read_text(encoding="utf-8-sig")
    try:
        # no opening `---` line -> ("", text): yaml.safe_load("") -> {} -> set() below,
        # the same answer the old explicit startswith guard gave -- removed as dead code
        front_matter_text, _body = split_front_matter(text)
    except ValueError:
        return None
    try:
        raw = yaml.safe_load(front_matter_text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        # a bare scalar (`repos: engmem`) degrades to a one-element list, exactly like every
        # other list field (spine._coerce_list_field) -- a mapping or number is unreadable
        # and must not read as "no repos"
        values, _warning = _coerce_list_field("repos", raw.get("repos"))
    except ValueError:
        return None
    return set(values)


def _distance(citing: Doc, cited: Doc, repos: dict[str, set[str] | None]) -> str:
    if cited.id in citing.related or citing.id in cited.related:
        return "adjacent"
    if set(citing.tags) & set(cited.tags):
        return "adjacent"
    citing_repos, cited_repos = repos.get(citing.id), repos.get(cited.id)
    if citing_repos is None or cited_repos is None:
        # unparseable front matter must not read as "shares nothing", which is
        # what made an unparseable document score distant -- see contracts/gate1.md
        return "undecidable"
    if citing_repos & cited_repos:
        return "adjacent"
    return "distant"


def _classification(cells: list[str]) -> tuple[str, str]:
    if len(cells) < 4:
        return "", "missing"
    raw = cells[3].strip()
    if not raw:
        return raw, "missing"
    normalized = raw.casefold()
    if normalized in VALID_CLASSIFICATIONS:
        return raw, normalized
    return raw, "unrecognized"


def _row_verdict(
    citing: Doc,
    table_row: _TableRow,
    docs: dict[str, Doc],
    bodies: dict[str, str],
    repos: dict[str, set[str] | None],
) -> RowVerdict:
    cells = table_row.cells
    cited_id = cells[0].strip("[] `")
    cited = docs.get(cited_id)
    source = f"{Path(citing.path).name}:{_line_number(Path(citing.path), table_row.line)}"

    quotes = tuple(next(g for g in m.groups() if g) for m in QUOTE_RE.finditer(cells[1]))

    # exactly the check order verify_citations.py already used: each step needs the
    # data the step before it established -- see contracts/gate1.md
    if not quotes:
        integrity, unfound = "no_quote", ()
    elif cited is None:
        integrity, unfound = "cited_missing", ()
    else:
        unfound = tuple(q for q in quotes if _normalized(q) not in bodies[cited_id])
        integrity = "quote_not_found" if unfound else "verified"

    staleness = None
    if cited is not None:
        staleness = "cited_superseded" if cited.status == "superseded" else "cited_active"

    distance = _distance(citing, cited, repos) if integrity == "verified" else None

    classification_raw, classification = _classification(cells)

    citing_repos = repos.get(citing.id) or set()
    dogfooding = bool({r.casefold() for r in citing_repos} & DOGFOODING_REPOS)

    return RowVerdict(
        citing=citing, cited=cited, cited_id=cited_id, source=source, quotes=quotes,
        integrity=integrity, unfound_quotes=unfound,
        classification_raw=classification_raw, classification=classification,
        distance=distance, staleness=staleness, dogfooding=dogfooding,
    )


def evaluate(store: Path) -> Verdicts:
    result = load_store(store / "sessions")
    docs = {d.id: d for d in result.docs}
    bodies = {d.id: _normalized(d.body) for d in result.docs}
    repos = {d.id: _repos(Path(d.path)) for d in result.docs}

    rows: list[RowVerdict] = []
    none_reports: list[str] = []
    conflicts: list[DocConflict] = []

    for doc in sorted(docs.values(), key=lambda d: d.id):
        reuse = next((s for s in split_sections(doc.body) if s.canonical == "reuse"), None)
        if reuse is None:
            continue

        table_rows = _iter_rows(reuse.body)
        has_none_line = NONE_LINE in reuse.body.casefold()

        if has_none_line and table_rows:
            # a plausible half-edit: report it loudly rather than silently picking a side
            conflicts.append(DocConflict(doc_id=doc.id, row_count=len(table_rows)))
        elif has_none_line:
            none_reports.append(doc.id)
            continue

        for table_row in table_rows:
            rows.append(_row_verdict(doc, table_row, docs, bodies, repos))

    return Verdicts(rows=rows, none_reports=none_reports, conflicts=conflicts)


def excluded_by_status(row: RowVerdict) -> str | None:
    """The citing document's own lifecycle state, when it excludes the row from the count."""
    return row.citing.status if row.citing.status in KNOWN_STATUSES_EXCLUDED_FROM_THE_COUNT else None


def exclusion_reason(row: RowVerdict) -> str | None:
    """`None` means the row is eligible: verified, classified reuse, in scope, not dogfooding --
    axis E (the verdict column) is then left for the human, whatever axis C (distance) says."""
    if row.integrity == "no_quote":
        return "excluded: no quote in the `taken` cell"
    if row.integrity == "cited_missing":
        return "excluded: cited document not in store"
    if row.integrity == "quote_not_found":
        return "excluded: quote not found in cited document"
    status = excluded_by_status(row)
    if status is not None:
        return f"excluded: citing document status is {status}"
    if row.dogfooding:
        return "excluded: dogfooding (story about this repository)"
    if row.classification == "harmful":
        return "excluded: classification harmful"
    if row.classification == "anti-reuse":
        return "excluded: classification anti-reuse"
    if row.classification == "missing":
        return "excluded: classification cell missing"
    if row.classification == "unrecognized":
        return f"excluded: classification {row.classification_raw!r} not recognized"
    return None


def is_valid(row: RowVerdict) -> bool:
    return exclusion_reason(row) is None


def is_primary_candidate(row: RowVerdict) -> bool:
    return is_valid(row) and row.distance == "distant"
