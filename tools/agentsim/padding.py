"""Document size settings: realistic padding (code blocks, tables, long Lessons) up to about a target size."""

from __future__ import annotations

import dataclasses
import zlib

DOC_SIZES = {"4kb": 4 * 1024, "12kb": 12 * 1024, "30kb": 30 * 1024}
DEFAULT_DOC_SIZE = "4kb"
PADDED_ROLES = ("code", "architecture", "lessons", "testing")
MAX_UNITS = 1000

_CODE_LINES = (
    "    val enabled: Boolean = true,",
    "    val owner: String = \"platform group\",",
    "    val mode: Mode = Mode.STEADY,",
    "    val tags: List<String> = emptyList(),",
    "    val region: String = \"primary\",",
    "    val dryRun: Boolean = false,",
)
_TABLE_ROWS = (
    "| smoke check | platform group | passing |",
    "| staging replay | release crew | passing |",
    "| dashboard review | on-call | done |",
    "| runbook update | on-call | done |",
    "| capacity review | planning | scheduled |",
    "| alert routing | operations | done |",
)
_LESSONS = (
    ("renaming the metric tags in place", "the dashboards lost their history", "new tags beside the old ones"),
    ("deploying on a Friday evening", "nobody watched the error budget over the weekend", "a midweek release window"),
    ("one shared staging tenant", "test data from two teams collided", "a staging tenant per team"),
    ("reading the setting from an environment variable", "a missing variable fell back silently", "a typed setting that fails at start"),
    ("logging every request body", "the log store filled within hours", "sampled bodies behind a flag"),
    ("a manual rollback checklist", "steps were skipped under pressure", "a scripted rollback"),
)


def _rng_index(doc_id: str, salt: int, size: int) -> int:
    return zlib.crc32(f"{doc_id}:{salt}".encode("utf-8")) % size


def _code_block(component: str, doc_id: str, unit: int) -> str:
    lines = ["```kotlin", f"data class {component}Settings{_suffix(unit)}("]
    for k in range(4):
        lines.append(_CODE_LINES[_rng_index(doc_id, unit * 7 + k, len(_CODE_LINES))])
    lines += [")", "```"]
    return "\n".join(lines)


def _table(component: str, doc_id: str, unit: int) -> str:
    lines = [f"| {component} step | owner | state |", "|---|---|---|"]
    for k in range(5):
        lines.append(_TABLE_ROWS[_rng_index(doc_id, unit * 11 + k, len(_TABLE_ROWS))])
    return "\n".join(lines)


def _lesson(component: str, doc_id: str, unit: int) -> str:
    tried, failed, fixed = _LESSONS[_rng_index(doc_id, unit * 13, len(_LESSONS))]
    return (
        f"- Tried: {tried} for {component}.\n  Failed because: {failed}.\n"
        f"  Fixed by: {fixed}.\n  Files: none recorded."
    )


def _suffix(unit: int) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return letters[unit % 26] + (letters[(unit // 26) % 26] if unit >= 26 else "")


_MAKERS = {"code": _code_block, "architecture": _table, "lessons": _lesson, "testing": _table}


def pad(doc, target: int):
    """`doc` padded unit by unit while the next unit fits in `target` UTF-8 bytes; a quick-shape or
    already larger document is unchanged."""
    if not doc.full or _size(doc) >= target:
        return doc
    component = doc.entities[0] if doc.entities else "Service"
    sections = dict(doc.sections)
    current = doc
    for unit in range(MAX_UNITS):
        role = PADDED_ROLES[unit % len(PADDED_ROLES)]
        base = sections.get(role, "")
        if role == "lessons" and base.strip() == "None hit.":
            base = ""
        trial = dict(sections, **{role: f"{base.rstrip()}\n\n{_MAKERS[role](component, doc.id, unit)}".strip()})
        candidate = dataclasses.replace(doc, sections=trial)
        if _size(candidate) > target:
            break
        sections, current = trial, candidate
    return current


def _size(doc) -> int:
    return len(doc.render().encode("utf-8"))


def size_bytes(name: str) -> int:
    """The target of a size setting; an unknown name raises `ValueError` naming the known ones."""
    if name not in DOC_SIZES:
        raise ValueError(f"unknown document size {name!r}; known: {', '.join(DOC_SIZES)}")
    return DOC_SIZES[name]
