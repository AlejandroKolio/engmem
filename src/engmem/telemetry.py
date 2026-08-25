"""Records one JSONL row per search and summarizes them for Gate 1 review.
Row shape and field semantics: `ENGMEM-SPEC.md` §5, point 6/6a.
"""

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
    """Appends one JSON line. Returns None on success, else a short failure
    reason — never raises, so a write failure cannot crash a search that already
    printed a correct answer, but is reported by the caller rather than hidden."""
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
    """Same shape and failure contract as `log_search`, for a role-addressed
    search (`search --role`, or `engmem_search_by_role`). `role_hits` is already
    role-filtered, so it — not the underlying word ranking — is what `surfaced`
    and `result` reflect; reusing `log_search`'s computation would over-report
    documents the role filter skipped."""
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


def _accumulate(bucket: ChannelTotals, record: dict) -> None:
    bucket.total += 1
    result = record.get("result")
    if result == "hit":
        bucket.hits += 1
    elif result == "ambiguous":
        bucket.ambiguous += 1
    else:
        bucket.misses += 1  # covers "miss" and any value from a row shape this reader predates
    bucket.context_bytes += int(record.get("context_bytes") or 0)
    bucket.context_tokens_estimate += int(record.get("context_tokens_estimate") or 0)


def summarize(jsonl_path: Path) -> TelemetrySummary:
    """Never raises: a missing file reads as zero rows, and a line that fails
    `json.loads` is counted in `unreadable` and skipped rather than aborting."""
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
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                unreadable += 1
                continue
            # "unknown", not the pre-channel default "cli" — a guess would misattribute it
            channel = record.get("channel") or "unknown"
            bucket = by_channel.setdefault(channel, ChannelTotals(channel=channel))
            _accumulate(bucket, record)
            _accumulate(overall, record)

    return TelemetrySummary(
        total=overall.total,
        unreadable=unreadable,
        by_channel=sorted(by_channel.values(), key=lambda c: c.channel),
        overall=overall,
    )
