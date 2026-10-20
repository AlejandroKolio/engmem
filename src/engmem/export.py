"""One selected document as a file to hand to someone else: previewed first, then written only as
previewed (US-20). Rules and their reasons: contracts/export.md."""

from __future__ import annotations

import bisect
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from engmem import confined
from engmem.gate1 import NONE_LINE, reuse_log_rows
from engmem.output import _escape_controls
from engmem.read_report import _escape_body, not_found_message
from engmem.sections import SectionSpan, h2_spans
from engmem.spine import DOC_ID_RE, Doc, LoadResult, load_yaml, split_front_matter, stated
from engmem.staging import commit_new, discard, stage, version_of
from engmem.versions import split_reference

MARKER = "[excluded reference]"

# carried into the file; every other front-matter field is cut (contracts/export.md)
KEPT_FIELDS = frozenset({
    "id", "title", "date", "task_date", "status", "backfilled", "tags", "entities",
    "covers_files", "repos", "verified_at", "verified_at_commit", "author", "branch", "pr",
})
REFERENCE_FIELDS = frozenset({"related", "superseded_by", "supersedes", "navigation_miss"})

_PATH_CHARS = r"[^\s<>\"'`|()\[\]{}]*"
_LOCAL_PATH = "|".join((
    r"file://" + _PATH_CHARS,
    r"(?<![\w~])~[\w.-]*[/\\]" + _PATH_CHARS,
    r"(?<![\w/\\])[A-Za-z]:[/\\]" + _PATH_CHARS,
    # the extended-length prefix: `\\?\D:\...` and `\\?\UNC\server\share`
    r"\\\\\?\\" + _PATH_CHARS,
    r"(?<![\w\\])\\\\[\w.$-]+\\" + _PATH_CHARS,
    r"(?<![\w/.~-])/(?i:Users|home|root|tmp|private|var|Volumes|mnt|media|opt|srv)/" + _PATH_CHARS,
    # a drive as Git Bash and MSYS spell it: `/c/Users/...`
    r"(?<![\w/.~-])/[A-Za-z]/(?i:Users|home)/" + _PATH_CHARS,
    r"(?:\$HOME|\$\{HOME\}|%(?i:USERPROFILE|HOMEPATH|APPDATA|LOCALAPPDATA|TEMP|TMP)%)"
    r"(?:[/\\]" + _PATH_CHARS + ")?",
))
# an id is a whole word; `_` ends one, so `<id>_v2` still names the document
_ID_START = r"(?<![^\W_])(?<!-)"
_ID_END = r"(?![^\W_])(?!-)"
_KINDS = {
    "link": "wiki link",
    "path": "local path",
    "home": "local path",
    "versioned": "versioned citation",
    "title": "document title",
    "doc_id": "document id",
}
# far wider than any line, so the dump never folds a long value onto a second line
_NO_WRAP = 1 << 20


class ExportError(Exception):
    """An export refused: the message names why, and nothing was written."""


@dataclass(frozen=True)
class Excluded:
    where: str
    kind: str
    value: str


@dataclass(frozen=True)
class Export:
    doc: Doc
    data: bytes
    fingerprint: str
    excluded: list[Excluded] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _alternation(values: set[str]) -> str:
    # longest first, so an id that extends another is cut whole
    return "|".join(re.escape(v) for v in sorted(values, key=lambda v: (-len(v), v)))


def _home_paths(store: Path) -> set[str]:
    """The store and the home directory as spelled on this machine, wherever they live."""
    paths = {str(store), str(store.resolve()), str(Path.home())}
    return {p for p in paths if Path(p).parent != Path(p)}


def _titles(titles: set[str], own_title: str) -> str:
    """The titles longest first, with the document's own in its place as the `own` group, so a
    neighbour's title inside it is not matched there."""
    ranked = sorted({*titles, own_title} - {""}, key=lambda v: (-len(v), v))
    return "|".join(
        f"(?P<own>{re.escape(v)})" if v == own_title else re.escape(v) for v in ranked
    )


@dataclass(frozen=True)
class _Patterns:
    references: re.Pattern[str]
    # every neighbour title at every position, overlapping ones included: the alternation above
    # finds one match per position, and the own title's match can hide a neighbour's that crosses it
    titles_ahead: re.Pattern[str] | None


def _pattern(
    ids: set[str], titles: set[str], own_title: str, home_paths: set[str]
) -> _Patterns:
    groups = {
        "link": r"\[\[[^\[\]\n]*\]\]",
        "path": _LOCAL_PATH,
        "home": f"(?i:{_alternation(home_paths)}){_PATH_CHARS}" if home_paths else "",
        "versioned": rf"{_ID_START}[a-z0-9]+(?:-[a-z0-9]+)+@[0-9a-f]{{16}}{_ID_END}",
        "title": rf"(?<!\w)(?i:{_titles(titles, own_title)})(?!\w)" if titles else "",
        "doc_id": (
            rf"{_ID_START}(?i:{_alternation(ids)})(?:@[0-9a-f]{{16}})?{_ID_END}" if ids else ""
        ),
    }
    references = re.compile(
        "|".join(f"(?P<{name}>{body})" for name, body in groups.items() if body)
    )
    ahead = rf"(?=((?<!\w)(?i:{_alternation(titles)})(?!\w)))" if titles else None
    return _Patterns(references, re.compile(ahead) if ahead else None)


def _title_spans(text: str, patterns: _Patterns) -> tuple[list[int], list[int]]:
    """`(starts, ends)` of the longest neighbour title at each position of `text`, in order."""
    if patterns.titles_ahead is None:
        return [], []
    found = [(m.start(), m.start() + len(m.group(1))) for m in patterns.titles_ahead.finditer(text)]
    return [a for a, _b in found], [b for _a, b in found]


def _crossed_end(spans: tuple[list[int], list[int]], start: int, end: int) -> int:
    """Where the own title at `[start, end)` stops being safe to keep: past every neighbour title
    that starts inside it or inside one already crossing it; `end` when none crosses."""
    starts, ends = spans
    reach = end
    index = bisect.bisect_left(starts, start)
    while index < len(starts) and starts[index] < reach:
        reach = max(reach, ends[index])
        index += 1
    return reach


def _redact(
    patterns: _Patterns, text: str, where: Callable[[int], str], into: list[Excluded]
) -> str:
    """`text` with every reference replaced by the marker; each cut is listed exactly as removed."""
    pieces: list[str] = []
    cursor = 0
    spans = None
    for match in patterns.references.finditer(text):
        start, end = match.span()
        if start < cursor:
            # a reference begun inside the last cut and running past it goes with that cut
            if end > cursor:
                last = into[-1]
                into[-1] = Excluded(last.where, last.kind, last.value + text[cursor:end])
                cursor = end
            continue
        if match.lastgroup == "title" and match.group("own") is not None:
            spans = spans or _title_spans(text, patterns)
            end = _crossed_end(spans, start, end)
            if end == match.end():
                continue
            # a wrong cut shows in the preview; a neighbour title left in the file does not
        into.append(Excluded(where(start), _KINDS[match.lastgroup], text[start:end]))
        pieces += [text[cursor:start], MARKER]
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _reuse_spans(text: str, spans: list[SectionSpan]) -> list[SectionSpan]:
    """Reuse Log sections with something besides the none-sentence in them."""
    return [
        s for s in spans
        if s.canonical == "reuse"
        and text[s.content_start:s.end].strip()
        and text[s.content_start:s.end].strip().casefold() != NONE_LINE
    ]


def _referenced(
    doc: Doc, raw: dict, body: str, spans: list[SectionSpan], result: LoadResult
) -> tuple[set[str], set[str]]:
    """`(ids, titles)` of the other documents `doc` could name; see contracts/export.md."""
    explicit = {*doc.related, doc.superseded_by or "", *(m.doc for m in doc.navigation_miss)}
    supersedes = raw.get("supersedes") or ""
    explicit |= set(map(str, supersedes)) if isinstance(supersedes, list) else {str(supersedes)}
    explicit |= {
        split_reference(row.cells[0].strip("[] `"))[0]
        for span in _reuse_spans(body, spans)
        for row in reuse_log_rows(body[span.content_start:span.end])
    }
    explicit = {i.strip() for i in explicit} - {""}
    others = [d for d in result.docs if d.id != doc.id]
    # a file's name is a name too: a document whose id differs from it, or one `load_store`
    # could not read, can still be pointed at as `sessions/<stem>.md`
    stems = {d.path.stem for d in others} | {
        p.path.stem for p in result.errors if p.path.suffix == ".md"
    }
    shaped = {i for i in {d.id for d in others} | stems if DOC_ID_RE.match(i.casefold())}
    ids = (explicit | shaped) - {doc.id}
    own_title = doc.title.strip().casefold()
    titles = {
        d.title.strip() for d in others
        if (len(d.title.split()) >= 2 or d.id in explicit)
        and d.title.strip().casefold() != own_title
    }
    return ids, titles


def _cut_reuse_log(body: str, spans: list[SectionSpan]) -> tuple[str, list[tuple[int, str]]]:
    """`(body, [(marker offset, what was cut)])`: a Reuse Log quotes other documents, so its
    content goes whole."""
    pieces: list[str] = []
    cuts: list[tuple[int, str]] = []
    pos = 0
    for span in _reuse_spans(body, spans):
        rows = reuse_log_rows(body[span.content_start:span.end])
        cited = ", ".join(row.cells[0] for row in rows) if rows else "its text"
        pieces.append(body[pos:span.content_start] + "\n\n")
        cuts.append((sum(map(len, pieces)), f"{len(rows)} row(s) citing and quoting {cited}"))
        pieces.append(MARKER + ("\n\n" if span.end < len(body) else ""))
        pos = span.end
    pieces.append(body[pos:])
    return "".join(pieces), cuts


def _place(body: str, first_line: int) -> Callable[[int], str]:
    """Where a body offset lands in the file: its line, and the section it is in."""
    _text, spans = h2_spans(body)

    def where(offset: int) -> str:
        line = first_line + body.count("\n", 0, offset)
        heading = next((s.heading for s in reversed(spans) if s.content_start <= offset), None)
        return f"line {line}" + (f" (section {heading!r})" if heading else "")

    return where


def _shown(value) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _redact_value(value, redact: Callable[[str], str]):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item, redact) for item in value]
    if isinstance(value, dict):
        return {_redact_value(k, redact): _redact_value(v, redact) for k, v in value.items()}
    return value


def _front_matter(
    raw: dict, patterns: _Patterns, excluded: list[Excluded]
) -> dict:
    kept: dict = {}
    for key, value in raw.items():
        if not stated(raw, key):
            continue
        name = str(key)
        where = f"front matter {name}"
        if name not in KEPT_FIELDS:
            reason = (
                "names other documents in your store" if name in REFERENCE_FIELDS
                else "not on the export list"
            )
            excluded.append(Excluded(where, f"field cut, {reason}", _shown(value)))
            continue
        kept[name] = _redact_value(
            value, lambda text, where=where: _redact(patterns, text, lambda _o: where, excluded)
        )
    return kept


def _notes(doc: Doc, raw: dict) -> list[str]:
    notes = []
    if doc.status == "superseded":
        notes.append(
            "this document is superseded: the file says so, but its successor is neither "
            "exported nor named"
        )
    unstated = [f for f in ("id", "title", "date", "status") if not stated(raw, f)]
    if unstated:
        notes.append(
            f"front matter does not state {', '.join(unstated)}: the file carries only what the "
            "document states, nothing derived"
        )
    return notes


def _read(doc: Doc) -> tuple[dict, str]:
    try:
        text = confined.read_utf8(doc.path, confined.SESSION_DEPTH)
        front_matter, body = split_front_matter(text)
        raw = load_yaml(front_matter) or {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ExportError(f"cannot read {doc.path.name} ({exc})") from exc
    if not isinstance(raw, dict):
        raise ExportError(f"{doc.path.name}: front matter is not a mapping")
    return raw, body.strip("\n")


def _selected(result: LoadResult, doc_id: str) -> Doc:
    wanted = doc_id.strip()
    doc = next((d for d in result.docs if d.id == wanted), None)
    if doc is None:
        raise ExportError(not_found_message(wanted, result.errors))
    if doc.status == "draft":
        raise ExportError(
            f"'{_escape_controls(wanted)}' is a draft -- finish it with /engmem.save first; "
            "only published documents are exported"
        )
    return doc


def prepare_export(store: Path, result: LoadResult, doc_id: str) -> Export:
    """The exact file one document would be exported as, and what was cut from it."""
    doc = _selected(result, doc_id)
    raw, body = _read(doc)
    body, spans = h2_spans(body)
    ids, titles = _referenced(doc, raw, body, spans, result)
    patterns = _pattern(ids, titles, doc.title.strip(), _home_paths(store))

    fields: list[Excluded] = []
    kept = _front_matter(raw, patterns, fields)
    header = ""
    if kept:
        dumped = yaml.safe_dump(
            kept, sort_keys=False, allow_unicode=True, default_flow_style=False, width=_NO_WRAP
        )
        header = f"---\n{dumped}---\n\n"

    body, reuse_cuts = _cut_reuse_log(body, spans)
    where = _place(body, header.count("\n") + 1)
    in_body = [Excluded(where(offset), "Reuse Log", what) for offset, what in reuse_cuts]
    body = _redact(patterns, body, where, in_body)
    in_body.sort(key=lambda e: int(e.where.split()[1]))

    text = f"{header}{body}\n" if body else header.removesuffix("\n")
    data = _escape_body(text).encode("utf-8")
    return Export(
        doc=doc, data=data, fingerprint=version_of(data), excluded=fields + in_body,
        notes=_notes(doc, raw),
    )


def render_preview(export: Export) -> str:
    doc = export.doc
    lines = [
        f"engmem export: preview of {_escape_controls(doc.id)} -- {_escape_controls(doc.title)} "
        f"(status: {_escape_controls(doc.status)})",
        f"source: {_escape_controls(doc.path)}",
        *(f"note: {note}" for note in export.notes),
        "",
        f"===== export file begins ({len(export.data)} bytes, UTF-8, LF line endings) =====",
        export.data.decode("utf-8").removesuffix("\n"),
        "===== export file ends =====",
        "",
    ]
    if export.excluded:
        lines.append(
            f"excluded ({len(export.excluded)}), each replaced by {MARKER} or left out -- "
            "listed here only, never written to the file:"
        )
        lines += [
            f"  - {_escape_controls(e.where)}: {e.kind}: {_escape_controls(e.value)}"
            for e in export.excluded
        ]
    else:
        lines.append("excluded: none")
    lines += [
        f"fingerprint: {export.fingerprint}",
        f"to write exactly this file: engmem export {_escape_controls(doc.id)} --out FILE "
        f"--confirm {export.fingerprint}",
        "nothing was written.",
    ]
    return "\n".join(lines)


def _inside(directory: Path, store: Path) -> bool:
    """The store or a directory in its root, compared by file identity after links, so a link,
    a junction or another letter case cannot hide one."""
    owned = [os.stat(store)] + [os.stat(entry) for entry in store.iterdir() if entry.is_dir()]
    return any(
        os.path.samestat(os.stat(candidate), own)
        for candidate in (directory, *directory.parents)
        for own in owned
    )


def _target(out: Path, store: Path) -> Path:
    try:
        directory = out.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExportError(f"{out.parent} does not exist -- engmem creates no directory") from exc
    if not directory.is_dir():
        raise ExportError(f"{out.parent} is not a directory")
    try:
        inside = _inside(directory, store)
    except OSError as exc:
        raise ExportError(f"cannot tell whether {out} is inside the store ({exc})") from exc
    if inside:
        raise ExportError(
            f"{out} is inside the store {store} -- an export is a copy for someone else and "
            "never lands in the store"
        )
    return directory / out.name


def write_export(export: Export, out: Path, store: Path, confirm: str) -> Path:
    """The previewed bytes as the new file `out`; otherwise `ExportError`, nothing written."""
    if confirm.strip().casefold() != export.fingerprint:
        raise ExportError(
            f"the export of {_escape_controls(export.doc.id)} is not the one previewed as "
            f"{_escape_controls(confirm.strip())!r} -- the document or the store changed since, "
            "or the fingerprint is mistyped; preview it again and read it before confirming"
        )
    target = _target(out, store)
    try:
        tmp_path = stage(target, export.data)
    except OSError as exc:
        raise ExportError(f"cannot write {target} ({exc})") from exc
    try:
        commit_new(tmp_path, target)
    except FileExistsError as exc:
        discard(tmp_path)
        raise ExportError(
            f"{target} already exists -- engmem never replaces a file; name a new one"
        ) from exc
    except OSError as exc:
        discard(tmp_path)
        raise ExportError(f"cannot write {target} ({exc})") from exc
    except BaseException:
        discard(tmp_path)
        raise
    return target
