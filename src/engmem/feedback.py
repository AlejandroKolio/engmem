"""A user's assessment of a find -- helped, not applicable, harmful -- kept in `feedback.jsonl`,
apart from the Reuse Log and the Gate 1 count (contracts/gate1.md, "Usefulness feedback")."""

from __future__ import annotations

import enum
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from engmem.output import _escape_controls
from engmem.telemetry import SessionRow, _load_json_object, read_session_rows

FEEDBACK_FILE = "feedback.jsonl"
TEXT_MAX = 1000
ASSESSED_BY_USER = "user"
CAUSAL_CAVEAT = (
    "Caveat: a user assessment records what the engineer saw, not what caused it -- it is not "
    "a causal or A/B result, and it is not part of the Gate 1 count."
)


class Assessment(enum.StrEnum):
    HELPED = "helped"
    NOT_APPLICABLE = "not-applicable"
    HARMFUL = "harmful"


ASSESSMENT_CHOICES = ", ".join(Assessment)


class FeedbackError(Exception):
    """A record refused: the message names why, and nothing was written."""


@dataclass(frozen=True)
class Find:
    session_id: str
    doc_id: str
    # every search that showed it showed only weak candidates (US-14)
    weak: bool


@dataclass(frozen=True)
class Feedback:
    session_id: str
    doc_id: str
    assessment: Assessment
    decision: str | None
    source: str | None
    # who wrote the line: `mcp` is an agent saying the user said it (owner decision D3)
    channel: str


@dataclass
class FeedbackSummary:
    finds: list[Find]
    # the latest assessment of each find; an assessment of anything else is in `unmatched`
    assessed: dict[tuple[str, str], Feedback]
    unmatched: int
    unreadable: int
    orphan_sessions: list[str] = field(default_factory=list)
    orphan_finds: int = 0

    @property
    def sessions(self) -> int:
        return len({f.session_id for f in self.finds})

    @property
    def unknown(self) -> list[Find]:
        return [f for f in self.finds if (f.session_id, f.doc_id) not in self.assessed]

    def count(self, assessment: Assessment) -> int:
        return sum(1 for a in self.assessed.values() if a.assessment == assessment)


def _finds(rows: Iterable[SessionRow]) -> dict[str, dict[str, bool]]:
    """session id -> doc id -> weak; a document is weak only while no reliable search showed it.
    A session is never its own find."""
    finds: dict[str, dict[str, bool]] = {}
    for row in rows:
        shown = finds.setdefault(row.session_id, {})
        for doc_id in row.surfaced:
            if doc_id == row.session_id:
                continue
            shown[doc_id] = shown.get(doc_id, True) and row.weak_only
    return finds


def _optional_text(name: str, value: str | None) -> str | None:
    text = (value or "").strip()
    if len(text) > TEXT_MAX:
        raise FeedbackError(
            f"{name} is {len(text)} characters; at most {TEXT_MAX} are kept, and a user's words "
            "are not cut -- shorten it"
        )
    return text or None


def parse_assessment(raw: str) -> Assessment:
    try:
        return Assessment(raw.strip().casefold())
    except ValueError:
        raise FeedbackError(
            f"unknown assessment {raw!r} -- one of {ASSESSMENT_CHOICES}"
        ) from None


def record(
    store: Path,
    known_ids: set[str],
    *,
    session_id: str,
    doc_id: str,
    assessment: str,
    decision: str | None = None,
    source: str | None = None,
    channel: str,
) -> Feedback:
    """Appends one user assessment of a find of `session_id`, or raises `FeedbackError`."""
    chosen = parse_assessment(assessment)
    session_id, doc_id = session_id.strip(), doc_id.strip()
    decision_text = _optional_text("decision", decision)
    source_text = _optional_text("source", source)
    if session_id not in known_ids:
        raise FeedbackError(
            f"no document with id {session_id!r} in the store -- an assessment belongs to a "
            "session document"
        )
    telemetry = store / "telemetry.jsonl"
    try:
        shown = _finds(read_session_rows(telemetry)).get(session_id, {})
    except (OSError, UnicodeDecodeError) as exc:
        raise FeedbackError(f"cannot read {telemetry} to check the session's finds ({exc})") from exc
    if doc_id not in shown:
        raise FeedbackError(
            f"'{_escape_controls(doc_id)}' was not shown by any search attributed to "
            f"'{_escape_controls(session_id)}' -- feedback assesses a find; this session's "
            "finds: " + (", ".join(_escape_controls(d) for d in sorted(shown)) or "none")
        )

    entry = Feedback(session_id, doc_id, chosen, decision_text, source_text, channel)
    line = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "doc_id": doc_id,
        "assessment": str(chosen),
        "assessed_by": ASSESSED_BY_USER,
        "decision": decision_text,
        "source": source_text,
        "channel": channel,
    })
    path = store / FEEDBACK_FILE
    try:
        with open(path, "a+b") as f:
            f.write(_line_start(f) + line.encode("utf-8") + b"\n")
    except OSError as exc:
        raise FeedbackError(f"cannot write {path} ({exc})") from exc
    return entry


def _line_start(f) -> bytes:
    """`\n` when the file ends inside a torn line, so the new line is not swallowed by it."""
    f.seek(0, 2)
    if f.tell() == 0:
        return b""
    f.seek(-1, 2)
    return b"" if f.read(1) == b"\n" else b"\n"


def confirmation(entry: Feedback) -> str:
    """What both channels say after a record."""
    return (
        f"recorded {entry.assessment} for {_escape_controls(entry.doc_id)} in session "
        f"{_escape_controls(entry.session_id)} as a user assessment"
    )


def _feedback_of(line: str) -> Feedback | None:
    record = _load_json_object(line)
    if record is None:
        return None
    session_id, doc_id = record.get("session_id"), record.get("doc_id")
    if not (isinstance(session_id, str) and isinstance(doc_id, str)):
        return None
    try:
        assessment = Assessment(record.get("assessment"))
    except ValueError:
        return None
    decision, source, channel = record.get("decision"), record.get("source"), record.get("channel")
    return Feedback(
        session_id, doc_id, assessment,
        decision if isinstance(decision, str) else None,
        source if isinstance(source, str) else None,
        channel if isinstance(channel, str) and channel else "unknown",
    )


def _read_feedback(path: Path) -> tuple[dict[tuple[str, str], Feedback], int]:
    """The latest assessment per (session, document), and the count of lines that are not one.
    A missing file is no assessment; one that cannot be read or decoded raises."""
    latest: dict[tuple[str, str], Feedback] = {}
    unreadable = 0
    if not path.is_file():
        return latest, unreadable
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            entry = _feedback_of(line)
            if entry is None:
                unreadable += 1
                continue
            latest[(entry.session_id, entry.doc_id)] = entry
    return latest, unreadable


def summarize(store: Path, known_ids: set[str], session_id: str | None = None) -> FeedbackSummary:
    """Every find, with its latest assessment where one exists. Raises `OSError` /
    `UnicodeDecodeError` when telemetry.jsonl or feedback.jsonl cannot be read or decoded."""
    by_session = _finds(read_session_rows(store / "telemetry.jsonl"))
    latest, unreadable = _read_feedback(store / FEEDBACK_FILE)
    if session_id is not None:
        by_session = {s: d for s, d in by_session.items() if s == session_id}
        latest = {k: v for k, v in latest.items() if k[0] == session_id}

    orphans = sorted(s for s, shown in by_session.items() if shown and s not in known_ids)
    finds = [
        Find(s, doc_id, weak)
        for s in sorted(by_session) if s in known_ids
        for doc_id, weak in sorted(by_session[s].items())
    ]
    keys = {(f.session_id, f.doc_id) for f in finds}
    return FeedbackSummary(
        finds=finds,
        assessed={k: v for k, v in latest.items() if k in keys},
        unmatched=sum(1 for k in latest if k not in keys),
        unreadable=unreadable,
        orphan_sessions=orphans,
        orphan_finds=sum(len(by_session[s]) for s in orphans),
    )


_LABELS = {
    Assessment.HELPED: "helped",
    Assessment.NOT_APPLICABLE: "not applicable",
    Assessment.HARMFUL: "harmful",
}


def _pair(session_id: str, doc_id: str, weak: bool) -> str:
    marker = " [weak candidate]" if weak else ""
    return f"{_escape_controls(session_id)} -> {_escape_controls(doc_id)}{marker}"


def _entry_line(entry: Feedback, weak: bool) -> str:
    details = [
        f"{name}: {_escape_controls(text)}"
        for name, text in (("decision", entry.decision), ("source", entry.source))
        if text
    ]
    suffix = " -- " + "; ".join(details) if details else ""
    return (
        f"  {_LABELS[entry.assessment]} (user assessment, via {_escape_controls(entry.channel)}): "
        f"{_pair(entry.session_id, entry.doc_id, weak)}{suffix}"
    )


def render_summary(summary: FeedbackSummary, session_id: str | None = None) -> str:
    """Found, assessed and unknown, each its own figure; with `session_id`, the unknown finds are
    listed too, so the user can see what is left to assess."""
    scope = f" for session {_escape_controls(session_id)}" if session_id is not None else ""
    helped = summary.count(Assessment.HELPED)
    weak = {(f.session_id, f.doc_id): f.weak for f in summary.finds}
    lines = [
        f"usefulness feedback{scope} (user assessments): {len(summary.finds)} find(s) across "
        f"{summary.sessions} session(s) -- a find is a document a search attributed to that "
        "session showed",
        f"assessed by the user: {len(summary.assessed)} -- "
        + ", ".join(f"{_LABELS[a]}: {summary.count(a)}" for a in Assessment),
        f"influence unknown (found, not assessed): {len(summary.unknown)} -- neither a success "
        "nor a failure",
        f"positive reuse by user assessment: {helped} -- helped only; not applicable and harmful "
        "are listed apart and never counted in it",
        f"weak candidates among the finds: {sum(weak.values())} -- shown only by searches whose "
        "every result was a weak candidate",
    ]
    for assessment in Assessment:
        lines.extend(
            _entry_line(entry, weak[key])
            for key, entry in sorted(summary.assessed.items())
            if entry.assessment == assessment
        )
    if session_id is not None:
        lines.extend(
            f"  unknown: {_pair(f.session_id, f.doc_id, f.weak)}" for f in summary.unknown
        )
    if summary.orphan_sessions:
        lines.append(
            "finds in searches whose session_id names no document here, not counted: "
            f"{summary.orphan_finds} -- "
            + ", ".join(_escape_controls(s) for s in summary.orphan_sessions)
        )
    if summary.unmatched:
        lines.append(f"assessments matching no find, not counted: {summary.unmatched}")
    if summary.unreadable:
        lines.append(f"unreadable feedback line(s), skipped: {summary.unreadable}")
    lines.append(CAUSAL_CAVEAT)
    return "\n".join(lines)
