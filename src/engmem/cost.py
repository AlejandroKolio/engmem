"""What engmem delivered to a session, and what the client reported, kept in `cost.jsonl` apart
from `telemetry.jsonl` (US-16; contracts/output.md, "Observed cost")."""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from engmem.feedback import _line_start
from engmem.output import _escape_controls
from engmem.telemetry import (
    BYTES_PER_TOKEN_ESTIMATE,
    _load_json_object,
    estimate_tokens,
    read_session_rows,
)

COST_FILE = "cost.jsonl"
CALL_ID_MAX = 200
OP_SEARCH = "search"
OP_READ = "read"
OP_BASELINE = "baseline"
_DELIVERY_OPS = (OP_SEARCH, OP_READ)


class CostError(Exception):
    """A report refused: the message names why, and nothing was written."""


def _append(store: Path, op_id: str, record: dict) -> None:
    """One line, never swallowed by a torn line before it; raises `OSError`."""
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "op_id": op_id, **record})
    with open(store / COST_FILE, "a+b") as f:
        f.write(_line_start(f) + line.encode("utf-8") + b"\n")


def record_delivery(
    store: Path,
    *,
    op: str,
    session_id: str | None,
    channel: str,
    byte_count: int,
    doc_id: str | None = None,
    role: str | None = None,
) -> str | None:
    """Appends one delivery, returning a failure reason rather than raising, as `log_search` does."""
    record: dict = {"op": op, "session_id": session_id, "channel": channel, "bytes": byte_count}
    if op == OP_READ:
        record.update(doc_id=doc_id, role=role)
    try:
        _append(store, uuid.uuid4().hex, record)
    except OSError as exc:
        return str(exc)
    return None


@dataclass(frozen=True)
class Baseline:
    session_id: str
    tokens: int | None
    seconds: float | None
    call_id: str | None
    channel: str
    op_id: str

    @property
    def key(self) -> str:
        return self.call_id or self.op_id


def _positive_tokens(tokens: object) -> int | None:
    if tokens is None:
        return None
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise CostError(f"tokens must be a whole number above zero (got {tokens!r})")
    return tokens


def _positive_seconds(seconds: object) -> float | None:
    if seconds is None:
        return None
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise CostError(f"seconds must be a number above zero (got {seconds!r})")
    if not math.isfinite(seconds) or seconds <= 0:
        raise CostError(f"seconds must be a number above zero (got {seconds!r})")
    return float(seconds)


def _call_id(call_id: str | None) -> str | None:
    text = (call_id or "").strip()
    if len(text) > CALL_ID_MAX:
        raise CostError(
            f"call id is {len(text)} characters; at most {CALL_ID_MAX} are kept, and a client's "
            "id is not cut -- shorten it"
        )
    return text or None


def record_baseline(
    store: Path,
    known_ids: set[str],
    *,
    session_id: str,
    tokens: object = None,
    seconds: object = None,
    call_id: str | None = None,
    channel: str,
) -> Baseline:
    """Appends what the client reported for the baseline call of `session_id`, or raises
    `CostError`."""
    session_id = session_id.strip()
    checked_tokens = _positive_tokens(tokens)
    checked_seconds = _positive_seconds(seconds)
    checked_call_id = _call_id(call_id)
    if checked_tokens is None and checked_seconds is None:
        raise CostError(
            "nothing to record -- give the tokens, the seconds, or both, as the client reported them"
        )
    if session_id not in known_ids:
        raise CostError(
            f"no document with id {session_id!r} in the store -- a baseline belongs to a session "
            "document; create the draft first"
        )
    entry = Baseline(
        session_id, checked_tokens, checked_seconds, checked_call_id, channel, uuid.uuid4().hex
    )
    try:
        _append(store, entry.op_id, {
            "op": OP_BASELINE, "session_id": session_id, "channel": channel,
            "tokens": checked_tokens, "seconds": checked_seconds, "call_id": checked_call_id,
            "reported_by": "client",
        })
    except OSError as exc:
        raise CostError(f"cannot write {store / COST_FILE} ({exc})") from exc
    return entry


def _measures(entry: Baseline) -> str:
    parts = []
    if entry.tokens is not None:
        parts.append(f"{entry.tokens} tokens")
    if entry.seconds is not None:
        parts.append(f"{entry.seconds:g} s")
    return ", ".join(parts)


def confirmation(entry: Baseline) -> str:
    """What both channels say after a record."""
    call = f"call {_escape_controls(entry.call_id)}" if entry.call_id else "a call with no id"
    return (
        f"recorded the baseline {call} for session {_escape_controls(entry.session_id)}: "
        f"{_measures(entry)}, as reported by the client"
    )


@dataclass
class Deliveries:
    count: int = 0
    bytes: int = 0


@dataclass
class CostSummary:
    session_id: str
    searches: Deliveries = field(default_factory=Deliveries)
    reads: Deliveries = field(default_factory=Deliveries)
    baselines: list[Baseline] = field(default_factory=list)
    replaced_baselines: int = 0
    duplicates: int = 0
    unreadable: int = 0
    searches_without_delivery: int = 0
    session_known: bool = True

    @property
    def total(self) -> Deliveries:
        return Deliveries(
            self.searches.count + self.reads.count, self.searches.bytes + self.reads.bytes
        )


@dataclass(frozen=True)
class _Op:
    """One line reduced to what the summary counts, each field already the right type."""

    op: str
    op_id: str
    session_id: str | None
    byte_count: int = 0
    baseline: Baseline | None = None


def _delivery_bytes(record: dict) -> int | None:
    byte_count = record.get("bytes")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        return None
    return byte_count


def _baseline_of(record: dict, session_id: str, op_id: str) -> Baseline | None:
    call_id, channel = record.get("call_id"), record.get("channel")
    if call_id is not None and not isinstance(call_id, str):
        return None
    try:
        tokens = _positive_tokens(record.get("tokens"))
        seconds = _positive_seconds(record.get("seconds"))
    except CostError:
        return None
    if tokens is None and seconds is None:
        return None
    return Baseline(
        session_id, tokens, seconds, call_id or None,
        channel if isinstance(channel, str) and channel else "unknown", op_id,
    )


def _read_op(line: str) -> _Op | None:
    """The operation this line records, or `None` for any shape this reader cannot count."""
    record = _load_json_object(line)
    if record is None:
        return None
    op, op_id, session_id = record.get("op"), record.get("op_id"), record.get("session_id")
    if not (isinstance(op_id, str) and op_id) or not (session_id is None or isinstance(session_id, str)):
        return None
    if op in _DELIVERY_OPS:
        byte_count = _delivery_bytes(record)
        return None if byte_count is None else _Op(op, op_id, session_id, byte_count)
    if op == OP_BASELINE and session_id is not None:
        entry = _baseline_of(record, session_id, op_id)
        return None if entry is None else _Op(op, op_id, session_id, baseline=entry)
    return None


def _read_ops(path: Path) -> tuple[list[_Op], int]:
    """Every countable line, and how many were not; a missing file is none, an unreadable one
    raises."""
    ops: list[_Op] = []
    unreadable = 0
    if not path.is_file():
        return ops, unreadable
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            entry = _read_op(line)
            if entry is None:
                unreadable += 1
                continue
            ops.append(entry)
    return ops, unreadable


def summarize(store: Path, session_id: str, known_ids: set[str]) -> CostSummary:
    """Every delivery and baseline report of `session_id`, each operation once. Raises `OSError` /
    `UnicodeDecodeError` when cost.jsonl or telemetry.jsonl cannot be read or decoded."""
    ops, unreadable = _read_ops(store / COST_FILE)
    summary = CostSummary(
        session_id=session_id, unreadable=unreadable, session_known=session_id in known_ids
    )
    seen: set[str] = set()
    baselines: dict[str, Baseline] = {}
    for entry in ops:
        if entry.session_id != session_id:
            continue
        if entry.op_id in seen:
            summary.duplicates += 1
            continue
        seen.add(entry.op_id)
        if entry.baseline is not None:
            summary.replaced_baselines += entry.baseline.key in baselines
            baselines[entry.baseline.key] = entry.baseline
            continue
        bucket = summary.searches if entry.op == OP_SEARCH else summary.reads
        bucket.count += 1
        bucket.bytes += entry.byte_count
    summary.baselines = list(baselines.values())
    logged = sum(
        1 for row in read_session_rows(store / "telemetry.jsonl") if row.session_id == session_id
    )
    summary.searches_without_delivery = max(0, logged - summary.searches.count)
    return summary


def _deliveries(label: str, deliveries: Deliveries) -> str:
    return f"  {label}: {deliveries.count} delivery(ies), {deliveries.bytes} bytes"


def _baseline_line(entry: Baseline) -> str:
    call = f"call {_escape_controls(entry.call_id)}" if entry.call_id else "a call with no id"
    return f"  {call}: {_measures(entry)} (reported via {_escape_controls(entry.channel)})"


def render_summary(summary: CostSummary) -> str:
    """Measured by engmem, estimated, reported by the client and missing, each named as such."""
    session = _escape_controls(summary.session_id)
    total = summary.total
    lines = [
        f"observed cost for session {session} -- only what engmem delivered through its own "
        "commands and tools",
        "measured by engmem, UTF-8 bytes of each response delivered:",
        _deliveries("search responses", summary.searches),
        _deliveries("document reads", summary.reads),
        _deliveries("total", total)
        + " -- text repeated in different deliveries is counted each time",
        f"estimated tokens of that total: {estimate_tokens(total.bytes)} -- ceil(bytes / "
        f"{BYTES_PER_TOKEN_ESTIMATE:g}), an estimate, not a measurement",
        "actual tokens of these deliveries: not reported by the client -- missing data",
    ]
    if summary.baselines:
        lines.append("baseline call (no-memory sub-agent), measured and reported by the client:")
        lines.extend(_baseline_line(entry) for entry in summary.baselines)
    else:
        lines.append(
            "baseline call (no-memory sub-agent): not reported by the client -- missing data"
        )
    lines.append("coverage, not complete:")
    if summary.reads.count == 0:
        lines.append(
            "  - no document read observed: a document opened as a file, not through "
            "`engmem read`, is not observed, so its cost is missing data"
        )
    else:
        lines.append(
            "  - a document opened as a file, not through `engmem read`, is not observed and not "
            "counted here"
        )
    if summary.searches_without_delivery:
        lines.append(
            f"  - searches of this session in telemetry.jsonl with no delivery record, not "
            f"counted: {summary.searches_without_delivery} (a --read-only server, a run before "
            "this record existed, or a failed write) -- missing data"
        )
    lines.append(
        "  - diagnostics a CLI command prints on stderr (load warnings and errors) reach a shell "
        "agent too and are not counted"
    )
    lines.append(
        "  - the model's full context outside engmem (the task, the code, its own reasoning) is "
        "not measured by this report"
    )
    if summary.duplicates:
        lines.append(f"lines repeating an operation already counted, skipped: {summary.duplicates}")
    if summary.replaced_baselines:
        lines.append(
            f"earlier reports of the same baseline call, replaced by the latest: "
            f"{summary.replaced_baselines}"
        )
    if summary.unreadable:
        lines.append(f"unreadable cost.jsonl line(s) in the store, skipped: {summary.unreadable}")
    if not summary.session_known:
        lines.append(f"note: no document with id {session} in the store")
    lines.append(
        "savings: not claimed -- the task was not also done without memory, so nothing measured "
        "can be compared against these costs; a baseline call is a cost of observing, not that "
        "alternative"
    )
    return "\n".join(lines)
