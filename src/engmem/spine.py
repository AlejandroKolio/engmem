"""Loads `sessions/*.md` into `Doc` records and reports load errors, warnings and degradation."""

from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from engmem.cache import identity_for
from engmem.settings import Mode

# Fields whose absence degrades retrieval: the scored fields plus the ones that order and filter
# results.
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
# every gap is `[^\S\r\n]` — every space `\s` accepts except the line break, which `\s` spans and
# the value may never cross. An ASCII `[ \t]` would also drop the non-breaking space a
# Confluence/Notion export writes after the colon. See contracts/backfill.md
_PREAMBLE_DATE_RE = re.compile(
    r"^[-*][^\S\r\n]*\*{0,2}(?:Date|Updated)\*{0,2}[^\S\r\n]*:"
    r"[^\S\r\n]*\*{0,2}[^\S\r\n]*(\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)
PREAMBLE_SCAN_LINES = 40


def preamble(body: str) -> str:
    """The leading window a preamble label is looked for in."""
    return "\n".join(body.splitlines()[:PREAMBLE_SCAN_LINES])


def preamble_label_value(pattern: re.Pattern[str], body: str) -> str | None:
    """The first value a label actually carries on its own line — see contracts/backfill.md."""
    for match in pattern.finditer(preamble(body)):
        value = match.group(1).strip().strip("*").strip()
        if value:
            return value
    return None


# Canonical id shape (ENGMEM-SPEC.md §4 / data-model.md "id"): `<story-id>-<slug>` or
# `<YYYYMMDD>-<slug>`.
DOC_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


def validate_doc_id(doc_id: object) -> str | None:
    """None if `doc_id` is safe as a filename and matches the spine's id shape, else a reason."""
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
    """A document the search failed to surface but the author used anyway."""

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
    # "" when blank; None when written but not as text (contracts/provenance.md)
    verified_at_commit: str | None
    capture_minutes: int | None
    path: Path
    body: str
    spine_complete: bool = True
    # `(mtime_ns, size)` as of the read below, so a caller deriving values from `body` can
    # prove the file has not moved under it since — see contracts/backfill.md
    source_identity: tuple[int, int] | None = None
    navigation_miss: list[NavigationMiss] = field(default_factory=list)
    degraded_fields: list[str] = field(default_factory=list)
    field_warnings: list[str] = field(default_factory=list)
    # US-08: None = written before modes existed (legacy); otherwise as written, casefolded, so
    # a misspelling stays visible to the count instead of reading as legacy
    mode: str | None = None
    # the stated reason a research session had no baseline; "(no reason stated)" when the field
    # is present without one — see contracts/gate1.md, "Modes"
    baseline_unavailable: str | None = None
    # US-09: the repositories this record is explicitly linked to, as written; None when the
    # value could not be read, so "unknown" never reads as "linked to none" (contracts/gate1.md)
    repos: list[str] | None = field(default_factory=list)
    # US-11: repository name -> the commit it was checked at, as written; a value is None when it
    # was not text, and the whole field None when it was not a mapping (contracts/provenance.md)
    verified_at: dict[str, str | None] | None = field(default_factory=dict)


def linked_repos(doc: Doc) -> list[str]:
    """The repository names `doc` is linked to, stripped; a blank name is no link."""
    return [name.strip() for name in doc.repos or () if name.strip()]


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


def sessions_dir_unreadable(sessions_dir: Path) -> str | None:
    """A reason string when `sessions_dir` cannot even be examined, else None. `is_dir()`
    returns False for an absent path but *propagates* a `PermissionError`."""
    try:
        sessions_dir.is_dir()
    except OSError as exc:
        return f"store not readable: {sessions_dir} ({exc})"
    return None


def _is_dir(path: Path, scan_errors: list[str]) -> bool:
    """`Path.is_dir()` returns False for an absent path but *propagates* a `PermissionError`, so
    an unreadable directory would leave through a function whose callers expect a report."""
    try:
        return path.is_dir()
    except OSError as exc:
        scan_errors.append(f"{path}: cannot examine directory for stray documents ({exc})")
        return False


def stray_documents(store: Path) -> tuple[list[Path], list[str]]:
    """`(strays, scan_errors)` — `os.walk` skips an unreadable directory in silence, so one
    would otherwise read as "no strays"."""
    scan_errors: list[str] = []
    if not _is_dir(store, scan_errors):
        return [], scan_errors

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
    if _is_dir(sessions_dir, scan_errors):
        # os.walk's onerror callback reports one locked-down subdirectory and
        # keeps walking its siblings, instead of losing the whole scan to it
        def _record_walk_error(exc: OSError) -> None:
            scan_errors.append(
                f"{exc.filename}: cannot list directory for stray documents ({exc})"
            )

        # `followlinks=False` (the default): a symlinked subdirectory of `sessions/` is
        # classified as a directory and never descended, so documents under it are neither
        # loaded (the store scan is flat) nor reported here. Following it would buy a cycle
        # risk for a layout nothing in engmem creates — recorded, not closed
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


def _coerce_int(field_name: str, value) -> tuple[int, str | None]:
    """`(minutes, warning)`. A value that coerces but was not written as an integer is read
    and named, so `"12"` and `3.9` do not pass where `backfilled: "false"` warns."""
    # `isinstance(True, int)`, so `int(True)` is 1 — a boolean would arrive as one measured
    # minute rather than as the wrong type it is
    if isinstance(value, bool):
        raise ValueError(f"{field_name} is not a number: {value!r}")
    # int([1, 2]) raises TypeError, not ValueError — catch both so a wrongly
    # typed value is reported, not left as an uncaught exception
    try:
        coerced = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        # OverflowError for `.inf`: `load_store` catches ValueError, so without this one
        # document's infinity takes the whole store load down with it
        raise ValueError(f"{field_name} is not a number: {value!r}") from exc
    if isinstance(value, int):
        return coerced, None
    return coerced, f"{field_name} is {value!r}, not an integer — read as {coerced}"


def _coerce_list_field(field_name: str, value) -> tuple[list[str], str | None]:
    """A bare `str` degrades to a one-element list, since `list("WidgetCache")` explodes into
    characters."""
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


def _coerce_repos(value) -> tuple[list[str] | None, str | None]:
    """`repos` reads like any list field, except that a value of the wrong type costs the
    record its links, not the record itself — see data-model.md, `repos`."""
    try:
        return _coerce_list_field("repos", value)
    except ValueError as exc:
        return None, f"{exc} — its repository links are unknown, so a --repo search leaves it out"


COMMIT_ID_RE = re.compile(r"^[0-9a-f]{7,64}$")


def is_commit_id(value: str) -> bool:
    """An abbreviated or full git object name, SHA-1 or SHA-256, in either case."""
    return bool(COMMIT_ID_RE.match(value.strip().casefold()))


def _coerce_verified_at(value) -> tuple[dict[str, str | None] | None, list[str]]:
    """A bad entry costs that repository its anchor and a bad field costs the record its anchors,
    never the record itself — see contracts/provenance.md."""
    if value is None:
        return {}, []
    if not isinstance(value, dict):
        return None, [
            f"verified_at is not a mapping of repository to commit: {value!r} — its "
            "snapshot anchors are unknown"
        ]
    anchors: dict[str, str | None] = {}
    seen: set[str] = set()
    warnings: list[str] = []
    for raw_name, raw_anchor in value.items():
        name = "" if raw_name is None else str(raw_name).strip()
        # the key `scoring.repo_key` compares repository names by
        key = name.casefold()
        if not name:
            warnings.append(
                f"verified_at has an entry with no repository name: {raw_anchor!r} — ignored"
            )
            continue
        if key in seen:
            warnings.append(f"verified_at names {name!r} twice — the first anchor is kept")
            continue
        seen.add(key)
        if raw_anchor is None or isinstance(raw_anchor, str):
            anchor = (raw_anchor or "").strip()
            anchors[name] = anchor
            if anchor and not is_commit_id(anchor):
                warnings.append(
                    f"verified_at {name!r} is {anchor!r}, not a commit id — a search shows that "
                    "anchor as unknown"
                )
            continue
        # unquoted, an all-digit sha is an int and one with a leading zero is read as octal, so
        # the number YAML hands over may no longer be the commit that was written
        anchors[name] = None
        warnings.append(
            f"verified_at {name!r} is {raw_anchor!r}, not text — quote the commit id; its "
            "anchor is unknown"
        )
    return anchors, warnings


def _legacy_anchor(value, field_warnings: list[str]) -> str | None:
    """The old single field, with the rule `verified_at` applies to a value YAML did not read
    as text."""
    if value is None or isinstance(value, str):
        return value or ""
    field_warnings.append(
        f"verified_at_commit is {value!r}, not text — quote the commit id; its anchor is unknown"
    )
    return None


def _coerce_bool(field_name: str, value) -> tuple[bool, str | None]:
    """Only a real boolean counts. `bool("false")` is True, so any quoted value read as the
    opposite of what it says."""
    if value is None:
        return False, None
    if isinstance(value, bool):
        return value, None
    return False, (
        f"{field_name} is {value!r}, not true/false — treated as false"
    )


def _coerce_navigation_miss(value) -> tuple[list[NavigationMiss], list[str]]:
    """Entries need both `doc` and `query`; anything else is dropped with a warning, never costing
    the document."""
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


def split_front_matter(text: str) -> tuple[str, str]:
    """`(front_matter_text, body)`. Raises `ValueError` on a block opened and never closed."""
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
    match = _PREAMBLE_DATE_RE.search(preamble(body))
    if match:
        try:
            return datetime.date.fromisoformat(match.group(1))
        except ValueError:
            # the regex accepts `2026-02-30`; a document that stated no `date:` must still
            # load (ENGMEM-SPEC.md §4), and the mtime below is what that promise rests on
            pass
    return datetime.date.fromtimestamp(path.stat().st_mtime)  # last resort, not clone-stable


def stated(raw: dict, name: str) -> bool:
    """`date:` with no value states nothing, same as a missing `date`."""
    return raw.get(name) is not None


# the only three states the spine's state machine recognises (data-model.md
# "State transitions"); anything else is unrecognized, not a fourth state
_KNOWN_STATUSES = frozenset({"draft", "active", "superseded"})


def _normalize_status(raw_value, field_warnings: list[str]) -> str:
    """Comparisons elsewhere are lowercase-literal, so `Draft` would rank as neither draft nor
    active."""
    normalized = str(raw_value).strip().casefold()
    if normalized not in _KNOWN_STATUSES:
        field_warnings.append(
            f"status is not a recognized value ({raw_value!r}) — treated as active"
        )
        return "active"
    return normalized


KNOWN_MODES = frozenset(Mode)
NO_REASON_STATED = "(no reason stated)"


def _coerce_mode(value, field_warnings: list[str]) -> str:
    normalized = str(value).strip().casefold()
    if normalized not in KNOWN_MODES:
        field_warnings.append(
            f"mode is not a recognized value ({value!r}) — one of {', '.join(Mode)}; "
            "the Gate 1 count leaves this document out until it is corrected"
        )
    return normalized


def _coerce_baseline_unavailable(value, field_warnings: list[str]) -> str:
    """Any stated value marks the observation incomplete: reading `true` or a blank as "the
    baseline was there" would put a session with no baseline into the valid group."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    field_warnings.append(
        f"baseline_unavailable is {value!r}, not a reason — read as unavailable "
        f"{NO_REASON_STATED}"
    )
    return NO_REASON_STATED


def parse_document(path: Path) -> Doc:
    """One document, or `yaml.YAMLError`/`ValueError`/`OSError` — `load_store` is the path that
    reports those instead, and the only one that also checks ids across the store."""
    # stat first: a change landing mid-read is then caught by a later comparison rather than
    # stamped as if it had already been there
    identity = identity_for(path)
    text = path.read_text(encoding="utf-8-sig")  # strips a BOM, else the opening --- is missed
    front_matter_text, body = split_front_matter(text)
    raw = yaml.safe_load(front_matter_text) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"front matter is not a mapping: parsed as {type(raw).__name__}")

    if stated(raw, "id") and "\n" in str(raw["id"]):
        # breaks filename/related-id equality and corrupts rendered search output
        raise ValueError(f"id contains embedded newline(s): {raw['id']!r}")

    degraded = [f for f in SPINE_FIELDS if not stated(raw, f)]
    body = body.strip("\n")

    date = _coerce_date("date", raw["date"]) if stated(raw, "date") else _derive_date(body, path)
    task_date = (
        _coerce_date("task_date", raw["task_date"]) if stated(raw, "task_date") else date
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
    repos, repos_warning = _coerce_repos(raw.get("repos"))
    if repos_warning:
        field_warnings.append(repos_warning)
    verified_at, verified_at_warnings = _coerce_verified_at(raw.get("verified_at"))
    field_warnings.extend(verified_at_warnings)
    navigation_miss, nav_warnings = _coerce_navigation_miss(raw.get("navigation_miss"))
    field_warnings.extend(nav_warnings)

    backfilled, backfilled_warning = _coerce_bool("backfilled", raw.get("backfilled"))
    if backfilled_warning:
        field_warnings.append(backfilled_warning)

    # None = "never measured"; 0 would misreport as "measured as instantaneous"
    capture_minutes = None
    if stated(raw, "capture_minutes"):
        capture_minutes, minutes_warning = _coerce_int("capture_minutes", raw["capture_minutes"])
        if minutes_warning:
            field_warnings.append(minutes_warning)

    # str like every other id-shaped field. Unquoted, `superseded_by: 2026-01-01` is a
    # `datetime.date`, and `mcp_server._handle_mark_superseded` writes the value unquoted,
    # re-reads it and compares against the `str` it was given — so that round trip refused
    # its own write. A non-string is still reported: it would print as a fabricated id
    superseded_by = None
    if stated(raw, "superseded_by"):
        superseded_by = str(raw["superseded_by"])
        if not isinstance(raw["superseded_by"], str):
            field_warnings.append(
                f"superseded_by is {raw['superseded_by']!r}, not a string — "
                f"read as {superseded_by!r}"
            )

    mode = _coerce_mode(raw["mode"], field_warnings) if stated(raw, "mode") else None
    baseline_unavailable = None
    if stated(raw, "baseline_unavailable"):
        baseline_unavailable = _coerce_baseline_unavailable(
            raw["baseline_unavailable"], field_warnings
        )

    return Doc(
        id=str(raw["id"]) if stated(raw, "id") else path.stem,
        title=str(raw["title"]) if stated(raw, "title") else _derive_title(body, path),
        date=date,
        task_date=task_date,
        status=(
            _normalize_status(raw["status"], field_warnings)
            if stated(raw, "status")
            else "active"  # never default to draft — that would hide the document
        ),
        superseded_by=superseded_by,
        backfilled=backfilled,
        tags=tags,
        entities=entities,
        related=related,
        covers_files=covers_files,
        navigation_miss=navigation_miss,
        verified_at_commit=_legacy_anchor(raw.get("verified_at_commit"), field_warnings),
        capture_minutes=capture_minutes,
        path=path,
        body=body,
        spine_complete=not degraded,
        degraded_fields=degraded,
        field_warnings=field_warnings,
        source_identity=identity,
        mode=mode,
        baseline_unavailable=baseline_unavailable,
        repos=repos,
        verified_at=verified_at,
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
            doc = parse_document(path)
        except (yaml.YAMLError, ValueError, OSError) as exc:
            # OSError covers failures before front matter is even reached:
            # permission-denied, a dangling symlink, a directory shadowing the name
            result.errors.append(Problem(path=path, message=f"{path.name}: {exc}"))
            continue

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

        # after the duplicate check, not before: a rejected document reported its field
        # warnings and none of the three below, so it was half-described and not in the store
        for warning in doc.field_warnings:
            result.warnings.append(Problem(path=path, message=f"{path.name}: {warning}"))

        # a daily draft is front matter alone by design (US-08), so its empty body is not news
        daily_draft = doc.status == "draft" and doc.mode == Mode.DAILY
        if not doc.body.strip() and not daily_draft:
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
