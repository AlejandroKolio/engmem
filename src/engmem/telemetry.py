"""Records one JSONL row per search and summarizes them for Gate 1 review."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from engmem.output import surfaced_ids
from engmem.scoring import SearchOutcome


def _result_label(outcome: SearchOutcome) -> str:
    if outcome.ambiguous:
        return "ambiguous"
    # a superseded redirect surfaced prior context too, so it counts as a hit
    if outcome.hits or outcome.superseded_notes:
        return "hit"
    return "miss"


DEFAULT_CHANNEL = "cli"

# bytes/token used by estimate_tokens(); see ENGMEM-SPEC.md §5 point 6a for calibration
BYTES_PER_TOKEN_ESTIMATE = 3.5


def estimate_tokens(byte_count: int) -> int:
    # ceil, not floor: a nonzero byte count must never round down to a zero-token estimate
    if byte_count <= 0:
        return 0
    return math.ceil(byte_count / BYTES_PER_TOKEN_ESTIMATE)


UNATTRIBUTED_CLI_NOTE = "note: unattributed search — pass --session <draft-id>"
UNATTRIBUTED_MCP_NOTE = "note: unattributed search — pass session_id <draft-id>"


def log_search(
    jsonl_path: Path,
    *,
    query: str,
    n_docs: int,
    outcome: SearchOutcome,
    session_id: str | None = None,
    channel: str = DEFAULT_CHANNEL,
    context_bytes: int = 0,
) -> str | None:
    """Appends one JSON line, returning a failure reason rather than raising."""
    context_tokens_estimate = estimate_tokens(context_bytes)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "query": query,
        # explicit null, not omitted: distinguishes "no --session passed" from
        # "row predates this field" and from "no search happened"
        "session_id": session_id,
        "channel": channel,
        "n_docs": n_docs,
        "hits": [{"id": h.doc.id, "score": h.score} for h in outcome.hits],
        "surfaced": surfaced_ids(outcome),  # what the reader was shown, incl. redirects
        "result": _result_label(outcome),
        "context_bytes": context_bytes,
        "context_tokens_estimate": context_tokens_estimate,
    }
    try:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        return str(exc)
    return None


def log_role_search(
    jsonl_path: Path,
    *,
    query: str,
    role: str,
    n_docs: int,
    role_hits: list,
    session_id: str | None = None,
    channel: str = DEFAULT_CHANNEL,
    context_bytes: int = 0,
) -> str | None:
    """`log_search` for a role search, reporting role-filtered hits rather than the word ranking."""
    context_tokens_estimate = estimate_tokens(context_bytes)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "role": role,
        "session_id": session_id,
        "channel": channel,
        "n_docs": n_docs,
        "hits": [{"id": rh.doc.id, "score": rh.score} for rh in role_hits],
        "surfaced": [rh.doc.id for rh in role_hits],
        "result": "hit" if role_hits else "miss",
        "context_bytes": context_bytes,
        "context_tokens_estimate": context_tokens_estimate,
    }
    try:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        return str(exc)
    return None


@dataclass
class ChannelTotals:
    channel: str
    total: int = 0
    hits: int = 0
    misses: int = 0
    ambiguous: int = 0
    context_bytes: int = 0
    context_tokens_estimate: int = 0


@dataclass
class TelemetrySummary:
    total: int
    unreadable: int
    by_channel: list[ChannelTotals] = field(default_factory=list)
    overall: ChannelTotals = field(default_factory=lambda: ChannelTotals(channel="overall"))


@dataclass(frozen=True)
class _Row:
    """One log line reduced to the fields the summary totals, each already the right type."""

    channel: str
    result: object
    context_bytes: int
    context_tokens_estimate: int


def _load_json_object(line: str) -> dict | None:
    """One line, decoded and shape-checked — shared by `_read_row` and `read_session_rows`,
    the two readers over the same file (contracts/gate1.md, "A second reader, not a wider
    `_Row`"). `None` covers both a decode failure and valid JSON that is not an object."""
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        # `JSONDecodeError` is only the commonest of these: a number with more digits than
        # CPython will convert raises a plain `ValueError`, and deep nesting a `RecursionError`,
        # both from inside the decoder — one row's defect either way, not the file's
        return None
    if not isinstance(record, dict):
        # valid JSON, but `123` / `[1, 2]` / `null` is not a telemetry row
        return None
    return record


def _read_row(line: str) -> _Row | None:
    """The row this line contributes, or `None` when its shape is not one this reader can total —
    every rejection here is counted as `unreadable`, never raised at the caller."""
    record = _load_json_object(line)
    if record is None:
        return None
    # "unknown", not the pre-channel default "cli" — a guess would misattribute it
    channel = record.get("channel") or "unknown"
    if not isinstance(channel, str):
        return None  # a bucket key of another type makes the by-channel sort unorderable
    try:
        context_bytes = int(record.get("context_bytes") or 0)
        context_tokens_estimate = int(record.get("context_tokens_estimate") or 0)
    except (TypeError, ValueError, OverflowError):
        # a string, a list, `Infinity`: one row's bad field, not the file's
        return None
    return _Row(channel, record.get("result"), context_bytes, context_tokens_estimate)


def _accumulate(bucket: ChannelTotals, row: _Row) -> None:
    bucket.total += 1
    if row.result == "hit":
        bucket.hits += 1
    elif row.result == "ambiguous":
        bucket.ambiguous += 1
    else:
        bucket.misses += 1  # covers "miss" and any value from a row shape this reader predates
    bucket.context_bytes += row.context_bytes
    bucket.context_tokens_estimate += row.context_tokens_estimate


def summarize(jsonl_path: Path) -> TelemetrySummary:
    """A missing file reads as zero rows and any line this reader cannot total counts as
    `unreadable`; a file it cannot read (`OSError`) or decode (`UnicodeDecodeError`) raises."""
    overall = ChannelTotals(channel="overall")
    if not jsonl_path.is_file():
        return TelemetrySummary(total=0, unreadable=0, by_channel=[], overall=overall)

    by_channel: dict[str, ChannelTotals] = {}
    unreadable = 0
    with open(jsonl_path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            row = _read_row(line)
            if row is None:
                unreadable += 1
                continue
            bucket = by_channel.setdefault(row.channel, ChannelTotals(channel=row.channel))
            _accumulate(bucket, row)
            _accumulate(overall, row)

    return TelemetrySummary(
        total=overall.total,
        unreadable=unreadable,
        by_channel=sorted(by_channel.values(), key=lambda c: c.channel),
        overall=overall,
    )


# ---------------------------------------------------------------------------
# `read_session_rows` — a second reader over the same file `summarize()` reads. `_Row` was
# never widened to carry `session_id` (it totals channel/result/context only), so `summarize()`
# structurally cannot answer a per-session question. Rather than grow `_Row` and risk changing
# what `summarize()` totals or how `engmem telemetry` reads, this is an independent, additive
# reader for the one field it never touched — see contracts/gate1.md, "A second reader, not
# a wider `_Row`," for why a second reader and not a wider `_Row`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRow:
    """One telemetry row reduced to the fields `gate1_audit.py` joins on."""

    session_id: str
    # `None` when the row has no `ts`, or one this reader cannot parse — excluded from any
    # `--since` window rather than guessed into it (see `gate1_audit._parse_since`)
    ts: datetime | None


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # every row this project writes calls `datetime.now(timezone.utc).isoformat()`, always
    # offset-aware; a naive value here predates that convention or was hand-written for a test —
    # read as UTC rather than left incomparable against an aware `--since` bound
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_session_rows(jsonl_path: Path) -> list[SessionRow]:
    """Every row naming a non-blank `session_id`, for the Gate 1 audit's completeness join. A
    missing file reads as zero rows and a row this reader cannot use is silently skipped —
    matching `summarize()`'s tolerance for a bad row; a file that cannot be read or decoded
    raises, also matching `summarize()` — both readers make the same distinction on the same
    file. Blank/whitespace-only mirrors `log_search`'s own write-side rule: absent, not a value."""
    if not jsonl_path.is_file():
        return []

    rows: list[SessionRow] = []
    with open(jsonl_path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            record = _load_json_object(line)
            if record is None:
                continue
            session_id = record.get("session_id")
            if not isinstance(session_id, str):
                continue
            session_id = session_id.strip()
            if not session_id:
                continue
            rows.append(SessionRow(session_id=session_id, ts=_parse_ts(record.get("ts"))))
    return rows
