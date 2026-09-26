"""Per-row Reuse Log verdicts shared by `tools/verify_citations.py` and `tools/gate1_report.py`.

Rules and their reasons: contracts/gate1.md.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from engmem.sections import split_sections
from engmem.spine import Doc, Problem, _coerce_list_field, load_store, split_front_matter

QUOTE_RE = re.compile(r'"([^"\n]{4,})"|“([^”\n]{4,})”|«([^»\n]{4,})»')
NONE_LINE = "prior docs used: none."


class Integrity(enum.StrEnum):
    """Axis A, in evaluation order -- contracts/gate1.md, "The five axes"."""

    NO_QUOTE = "no_quote"
    CITED_MISSING = "cited_missing"
    QUOTE_NOT_FOUND = "quote_not_found"
    VERIFIED = "verified"


class Classification(enum.StrEnum):
    """Axis B: the three the author may write, plus the two this module derives."""

    REUSE = "reuse"
    ANTI_REUSE = "anti-reuse"
    HARMFUL = "harmful"
    MISSING = "missing"
    UNRECOGNIZED = "unrecognized"


class Distance(enum.StrEnum):
    """Axis C, computed only once axis A reaches `verified`."""

    ADJACENT = "adjacent"
    DISTANT = "distant"
    UNDECIDABLE = "undecidable"


class Staleness(enum.StrEnum):
    """Axis D, computed whenever the cited document resolves in the store."""

    CITED_ACTIVE = "cited_active"
    CITED_SUPERSEDED = "cited_superseded"


# the classification cell a human may write; `missing`/`unrecognized` are this module's
# own readings, so a cell spelling either of them out is `unrecognized` like any other word
VALID_CLASSIFICATIONS = frozenset(
    {Classification.REUSE, Classification.ANTI_REUSE, Classification.HARMFUL}
)
KNOWN_STATUSES_EXCLUDED_FROM_THE_COUNT = frozenset({"draft", "superseded"})
# this repository's own package name (pyproject.toml) -- see contracts/gate1.md
# "Dogfooding identification" for why `repos` and not `covers_files` or a deny-list
DOGFOODING_REPOS = frozenset({"engmem"})


@dataclass
class RowVerdict:
    citing: Doc
    cited: Doc | None
    cited_id: str  # as written in the row; may not resolve when integrity is CITED_MISSING
    source: str  # "<filename>:<line>", for human-readable messages
    quotes: tuple[str, ...]
    integrity: Integrity
    unfound_quotes: tuple[str, ...]
    classification_raw: str
    classification: Classification
    distance: Distance | None  # set only when integrity is VERIFIED
    staleness: Staleness | None  # set whenever `cited` resolved
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
    # every file `load_store` could not read, and an unlistable `sessions/`: named by the tools,
    # never counted in a figure -- a document here has no row above (contracts/gate1.md)
    load_errors: list[Problem]


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class TableRow:
    line: str
    cells: list[str]


def reuse_log_rows(section_body: str) -> list[TableRow]:
    rows = []
    for line in section_body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or re.fullmatch(r"[|\s:-]+", stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) >= 2 and cells[0].casefold() not in ("prior-doc", ""):
            rows.append(TableRow(line=line, cells=cells))
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


def _distance(citing: Doc, cited: Doc, repos: dict[str, set[str] | None]) -> Distance:
    if cited.id in citing.related or citing.id in cited.related:
        return Distance.ADJACENT
    if set(citing.tags) & set(cited.tags):
        return Distance.ADJACENT
    citing_repos, cited_repos = repos.get(citing.id), repos.get(cited.id)
    if citing_repos is None or cited_repos is None:
        # unparseable front matter must not read as "shares nothing", which is
        # what made an unparseable document score distant -- see contracts/gate1.md
        return Distance.UNDECIDABLE
    if citing_repos & cited_repos:
        return Distance.ADJACENT
    return Distance.DISTANT


def quotes_in(taken_cell: str) -> tuple[str, ...]:
    return tuple(next(g for g in m.groups() if g) for m in QUOTE_RE.finditer(taken_cell))


def classification_of(cells: list[str]) -> tuple[str, Classification]:
    if len(cells) < 4:
        return "", Classification.MISSING
    raw = cells[3].strip()
    if not raw:
        return raw, Classification.MISSING
    normalized = raw.casefold()
    if normalized in VALID_CLASSIFICATIONS:
        return raw, Classification(normalized)
    return raw, Classification.UNRECOGNIZED


def _row_verdict(
    citing: Doc,
    table_row: TableRow,
    docs: dict[str, Doc],
    bodies: dict[str, str],
    repos: dict[str, set[str] | None],
) -> RowVerdict:
    cells = table_row.cells
    cited_id = cells[0].strip("[] `")
    cited = docs.get(cited_id)
    source = f"{Path(citing.path).name}:{_line_number(Path(citing.path), table_row.line)}"

    quotes = quotes_in(cells[1])

    # exactly the check order verify_citations.py already used: each step needs the
    # data the step before it established -- see contracts/gate1.md
    if not quotes:
        integrity, unfound = Integrity.NO_QUOTE, ()
    elif cited is None:
        integrity, unfound = Integrity.CITED_MISSING, ()
    else:
        unfound = tuple(q for q in quotes if _normalized(q) not in bodies[cited_id])
        integrity = Integrity.QUOTE_NOT_FOUND if unfound else Integrity.VERIFIED

    staleness = None
    if cited is not None:
        staleness = (
            Staleness.CITED_SUPERSEDED if cited.status == "superseded" else Staleness.CITED_ACTIVE
        )

    distance = _distance(citing, cited, repos) if integrity == Integrity.VERIFIED else None

    classification_raw, classification = classification_of(cells)

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

        table_rows = reuse_log_rows(reuse.body)
        has_none_line = NONE_LINE in reuse.body.casefold()

        if has_none_line and table_rows:
            # a plausible half-edit: report it loudly rather than silently picking a side
            conflicts.append(DocConflict(doc_id=doc.id, row_count=len(table_rows)))
        elif has_none_line:
            none_reports.append(doc.id)
            continue

        for table_row in table_rows:
            rows.append(_row_verdict(doc, table_row, docs, bodies, repos))

    return Verdicts(
        rows=rows, none_reports=none_reports, conflicts=conflicts, load_errors=result.errors
    )


def excluded_by_status(row: RowVerdict) -> str | None:
    """The citing document's own lifecycle state, when it excludes the row from the count."""
    return row.citing.status if row.citing.status in KNOWN_STATUSES_EXCLUDED_FROM_THE_COUNT else None


INTEGRITY_EXCLUSIONS = {
    Integrity.NO_QUOTE: "excluded: no quote in the `taken` cell",
    Integrity.CITED_MISSING: "excluded: cited document not in store",
    Integrity.QUOTE_NOT_FOUND: "excluded: quote not found in cited document",
}

CLASSIFICATION_EXCLUSIONS = {
    Classification.HARMFUL: "excluded: classification harmful",
    Classification.ANTI_REUSE: "excluded: classification anti-reuse",
    Classification.MISSING: "excluded: classification cell missing",
}


def exclusion_reason(row: RowVerdict) -> str | None:
    """`None` means the row is eligible: verified, classified reuse, in scope, not dogfooding --
    axis E (the verdict column) is then left for the human, whatever axis C (distance) says."""
    # the four axes in the order contracts/gate1.md ("Who fills the last column") fixes: integrity,
    # then the citing document's status, then dogfooding, then classification -- first one wins
    integrity_reason = INTEGRITY_EXCLUSIONS.get(row.integrity)
    if integrity_reason is not None:
        return integrity_reason
    status = excluded_by_status(row)
    if status is not None:
        return f"excluded: citing document status is {status}"
    if row.dogfooding:
        return "excluded: dogfooding (story about this repository)"
    if row.classification == Classification.UNRECOGNIZED:
        # the one classification whose reason quotes what the human actually wrote
        return f"excluded: classification {row.classification_raw!r} not recognized"
    return CLASSIFICATION_EXCLUSIONS.get(row.classification)


def is_valid(row: RowVerdict) -> bool:
    return exclusion_reason(row) is None


def is_primary_candidate(row: RowVerdict) -> bool:
    return is_valid(row) and row.distance == Distance.DISTANT
