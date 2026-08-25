"""Loads `sessions/*.md` into `Doc` records: parses front matter, derives
missing spine fields from the body, and reports load errors/warnings/degradation.
"""

from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Fields whose absence degrades retrieval: the scored fields plus the ones that
# order and filter results. A document missing one still loads, with the value
# derived or defaulted, and the gap is reported rather than hiding the document
# (principle VIII). `backfilled`/`verified_at_commit`/`capture_minutes` are
# deliberately excluded: nothing outside this module reads them, and a draft
# cannot know its own verification commit or capture time in advance.
SPINE_FIELDS = (
    "id",
    "title",
    "date",
    "task_date",
    "status",
    "tags",
    "entities",
)

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_TITLE_PREFIX_RE = re.compile(r"^Knowledge Base\s*[—–-]\s*")
_PREAMBLE_DATE_RE = re.compile(
    r"^[-*]\s*\*{0,2}(?:Date|Updated)\*{0,2}\s*:\s*\*{0,2}\s*(\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)
_PREAMBLE_SCAN_LINES = 40

# Canonical id shape (ENGMEM-SPEC.md §4 / data-model.md "id"): `<story-id>-<slug>`
# or `<YYYYMMDD>-<slug>`. ASCII-lowercase-hyphen only, since an id also doubles
# as a filename: no case-folding surprise, no homoglyph, nothing shell-special.
# Requiring 2+ hyphen-separated groups also rules out every reserved Windows
# device name (CON, AUX, NUL, COM1..9, LPT1..9), none of which contain a hyphen.
DOC_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


def validate_doc_id(doc_id: object) -> str | None:
    """None if `doc_id` is safe as a `sessions/<doc_id>.md` filename and
    matches the spine's id shape, else a reason. Only the write path (creating
    or renaming a document) calls this — `load_store` stays permissive of
    whatever id an existing document already states."""
    if not isinstance(doc_id, str) or not doc_id:
        return "id must be a non-empty string"
    if "\x00" in doc_id:
        return "id must not contain a NUL byte"
    if doc_id in (".", ".."):
        return "id must not be '.' or '..'"
    if doc_id.startswith("."):
        return "id must not start with '.'"
    if "/" in doc_id or "\\" in doc_id:
        return "id must not contain a path separator"
    if not DOC_ID_RE.match(doc_id):
        return (
            "id must match <story-id>-<slug> or <YYYYMMDD>-<slug>: lowercase "
            "letters, digits and hyphens only, at least two hyphen-separated parts "
            f"(got {doc_id!r})"
        )
    return None


@dataclass
class NavigationMiss:
    """A document the search failed to surface but the author used anyway. The query
    matters as much as the id: it says whether the miss was a wording gap or a ranking
    one, and that is what the semantic-search decision is pre-registered against."""

    doc: str
    query: str


@dataclass
class Doc:
    id: str
    title: str
    date: datetime.date
    task_date: datetime.date
    status: str
    superseded_by: str | None
    backfilled: bool
    tags: list[str]
    entities: list[str]
    related: list[str]
    covers_files: list[str]
    verified_at_commit: str
    capture_minutes: int | None
    path: Path
    body: str
    spine_complete: bool = True
    navigation_miss: list[NavigationMiss] = field(default_factory=list)
    degraded_fields: list[str] = field(default_factory=list)
    field_warnings: list[str] = field(default_factory=list)


@dataclass
class Problem:
    path: Path
    message: str


@dataclass
class LoadResult:
    docs: list[Doc] = field(default_factory=list)
    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    # set only when sessions/ itself could not be listed, distinct from a
    # one-document failure in `errors` that leaves the rest of the store intact
    scan_error: Problem | None = None


# files a store root legitimately contains besides sessions/
STORE_ROOT_ALLOWED = {"README.md"}


def stray_documents(store: Path) -> tuple[list[Path], list[str]]:
    """Markdown engmem will never read: sitting in the store root, or filed
    into a subdirectory of sessions/ — the store is scanned flat, so foldering
    a document makes it invisible to search.

    Returns `(strays, scan_errors)`. `Path.glob`/`rglob` swallow a
    `PermissionError` and just yield fewer results, so an unreadable directory
    would otherwise silently read as "no strays"; `scan_errors` names every
    place that happened instead."""
    if not store.is_dir():
        return [], []

    scan_errors: list[str] = []
    try:
        root = sorted(
            p
            for p in store.iterdir()
            if p.name.endswith(".md") and p.name not in STORE_ROOT_ALLOWED
        )
    except OSError as exc:
        root = []
        scan_errors.append(f"{store}: cannot list directory for stray documents ({exc})")

    sessions_dir = store / "sessions"
    nested: list[Path] = []
    if sessions_dir.is_dir():
        # os.walk's onerror callback reports one locked-down subdirectory and
        # keeps walking its siblings, instead of losing the whole scan to it
        def _record_walk_error(exc: OSError) -> None:
            scan_errors.append(
                f"{exc.filename}: cannot list directory for stray documents ({exc})"
            )

        for dirpath, _dirnames, filenames in os.walk(sessions_dir, onerror=_record_walk_error):
            current = Path(dirpath)
            if current == sessions_dir:
                continue
            nested.extend(
                current / name for name in filenames if name.endswith(".md")
            )

    return sorted(root + nested), scan_errors


def _coerce_date(field_name: str, value) -> datetime.date:
    # YAML yields datetime.date only for an unquoted scalar; a quoted date
    # (common in LLM-written front matter) arrives as str, so accept both
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{field_name} is not a date: {value!r}")


def _coerce_int(field_name: str, value) -> int:
    # int([1, 2]) raises TypeError, not ValueError — catch both so a wrongly
    # typed value is reported, not left as an uncaught exception
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not a number: {value!r}") from exc


def _coerce_list_field(field_name: str, value) -> tuple[list[str], str | None]:
    """A bare scalar is a plausible "forgot the brackets" slip only for `str`
    — `list("WidgetCache")` does not raise, it silently explodes into single
    characters. Degrade that case to a one-element list with a warning; any
    other scalar (`5`, `true`, a mapping) is a real type error."""
    if value is None:
        return [], None
    if isinstance(value, list):
        return [str(item) for item in value], None
    if isinstance(value, str):
        return [value], (
            f"{field_name} is a single value, not a list — treated as a "
            f"one-element list: [{value!r}]"
        )
    raise ValueError(f"{field_name} is not a list: {value!r}")


def _coerce_navigation_miss(value) -> tuple[list[NavigationMiss], list[str]]:
    """Entries needing both `doc` and `query`; anything else is dropped with a warning.
    Not a load gate: nothing ranks or filters on this field, so a half-written entry
    must never cost the document."""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [f"navigation_miss is not a list: {value!r} — ignored"]

    entries, warnings = [], []
    for item in value:
        if isinstance(item, dict) and item.get("doc") and item.get("query"):
            entries.append(NavigationMiss(str(item["doc"]), str(item["query"])))
        else:
            warnings.append(f"navigation_miss entry needs both doc and query: {item!r}")
    return entries, warnings


def _split_front_matter(text: str) -> tuple[str, str]:
    # delimiters must be whole lines, so a --- inside a value can't end the block
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text  # no front matter block: incomplete, not corrupt
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    raise ValueError("front matter block is not closed")  # opened but unclosed IS corruption


def _derive_title(body: str, path: Path) -> str:
    match = _H1_RE.search(body)
    if not match:
        return path.stem
    return _TITLE_PREFIX_RE.sub("", match.group(1)).strip() or path.stem


def _derive_date(body: str, path: Path) -> datetime.date:
    head = "\n".join(body.splitlines()[:_PREAMBLE_SCAN_LINES])
    match = _PREAMBLE_DATE_RE.search(head)
    if match:
        return datetime.date.fromisoformat(match.group(1))
    return datetime.date.fromtimestamp(path.stat().st_mtime)  # last resort, not clone-stable


def _stated(raw: dict, name: str) -> bool:
    """`date:` with no value states nothing, same as a missing `date`."""
    return raw.get(name) is not None


# the only three states the spine's state machine recognises (data-model.md
# "State transitions"); anything else is unrecognized, not a fourth state
_KNOWN_STATUSES = frozenset({"draft", "active", "superseded"})


def _normalize_status(raw_value, field_warnings: list[str]) -> str:
    """Status comparisons elsewhere are lowercase-literal, so a raw-case value
    (`Draft`) would neither rank as draft nor count in the scoreboard. An
    unrecognized value defaults to `active` with a warning, rather than pass
    through unremarked."""
    normalized = str(raw_value).strip().casefold()
    if normalized not in _KNOWN_STATUSES:
        field_warnings.append(
            f"status is not a recognized value ({raw_value!r}) — treated as active"
        )
        return "active"
    return normalized


def _parse_one(path: Path) -> Doc:
    text = path.read_text(encoding="utf-8-sig")  # strips a BOM, else the opening --- is missed
    front_matter_text, body = _split_front_matter(text)
    raw = yaml.safe_load(front_matter_text) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"front matter is not a mapping: parsed as {type(raw).__name__}")

    if _stated(raw, "id") and "\n" in str(raw["id"]):
        # breaks filename/related-id equality and corrupts rendered search output
        raise ValueError(f"id contains embedded newline(s): {raw['id']!r}")

    degraded = [f for f in SPINE_FIELDS if not _stated(raw, f)]
    body = body.strip("\n")

    date = _coerce_date("date", raw["date"]) if _stated(raw, "date") else _derive_date(body, path)
    task_date = (
        _coerce_date("task_date", raw["task_date"]) if _stated(raw, "task_date") else date
    )

    field_warnings: list[str] = []

    def _list_field(name: str) -> list[str]:
        values, warning = _coerce_list_field(name, raw.get(name))
        if warning:
            field_warnings.append(warning)
        return values

    tags = _list_field("tags")
    entities = _list_field("entities")
    related = _list_field("related")
    covers_files = _list_field("covers_files")
    navigation_miss, nav_warnings = _coerce_navigation_miss(raw.get("navigation_miss"))
    field_warnings.extend(nav_warnings)

    return Doc(
        id=str(raw["id"]) if _stated(raw, "id") else path.stem,
        title=str(raw["title"]) if _stated(raw, "title") else _derive_title(body, path),
        date=date,
        task_date=task_date,
        status=(
            _normalize_status(raw["status"], field_warnings)
            if _stated(raw, "status")
            else "active"  # never default to draft — that would hide the document
        ),
        superseded_by=raw.get("superseded_by") or None,
        backfilled=bool(raw.get("backfilled") or False),
        tags=tags,
        entities=entities,
        related=related,
        covers_files=covers_files,
        navigation_miss=navigation_miss,
        verified_at_commit=str(raw.get("verified_at_commit") or ""),
        capture_minutes=(
            # None = "never measured"; 0 would misreport as "measured as instantaneous"
            _coerce_int("capture_minutes", raw["capture_minutes"])
            if raw.get("capture_minutes") is not None
            else None
        ),
        path=path,
        body=body,
        spine_complete=not degraded,
        degraded_fields=degraded,
        field_warnings=field_warnings,
    )


def load_store(sessions_dir: Path) -> LoadResult:
    result = LoadResult()
    seen_ids: dict[str, Path] = {}
    sessions_dir = Path(sessions_dir)

    try:
        # iterdir (unlike glob) does not swallow the OSError from an unreadable
        # directory, so scanning through it first surfaces what glob would hide
        paths = sorted(p for p in sessions_dir.iterdir() if p.name.endswith(".md"))
    except OSError as exc:
        # an error, not a warning: an unknown number of documents is hidden
        # behind this, not necessarily zero
        problem = Problem(
            path=sessions_dir,
            message=(
                f"{sessions_dir}: cannot list directory ({exc}) — the number of "
                f"documents normally found here is unknown, not zero"
            ),
        )
        result.errors.append(problem)
        result.scan_error = problem
        return result

    for path in paths:
        try:
            doc = _parse_one(path)
        except (yaml.YAMLError, ValueError, OSError) as exc:
            # OSError covers failures before front matter is even reached:
            # permission-denied, a dangling symlink, a directory shadowing the name
            result.errors.append(Problem(path=path, message=f"{path.name}: {exc}"))
            continue

        for warning in doc.field_warnings:
            result.warnings.append(Problem(path=path, message=f"{path.name}: {warning}"))

        if doc.id in seen_ids:
            other = seen_ids[doc.id]
            result.errors.append(
                Problem(
                    path=path,
                    message=(
                        f"duplicate id '{doc.id}' in {other.name} and {path.name}"
                    ),
                )
            )
            continue
        seen_ids[doc.id] = path

        if not doc.body.strip():
            result.warnings.append(
                Problem(path=path, message=f"{path.name}: document is empty")
            )
        if not doc.entities:
            result.warnings.append(
                Problem(path=path, message=f"{path.name}: entities is empty")
            )
        if doc.id != path.stem:
            result.warnings.append(
                Problem(
                    path=path,
                    message=(
                        f"{path.name}: id '{doc.id}' does not match filename "
                        f"'{path.stem}'"
                    ),
                )
            )

        result.docs.append(doc)

    return result
