"""Scores one run: outcome from what reached the agent's context, cost from its transcript."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from agentsim.corpus import Task
from agentsim.profiles import LOAD, SAVE, SEARCH, START, Profile, RunState
from agentsim.trace import TEMPLATE, TOOL, PASTE, Transcript, tokens

CITATION_PROBLEM = "Reuse Log citation problem"


@dataclass(frozen=True)
class Outcome:
    """Each field is None where it does not apply to the task or the profile."""

    reached: bool | None
    quoted: bool | None
    pitfall_avoided: bool | None
    stale_avoided: bool | None
    none_found_said: bool | None
    correct: bool
    saved: bool | None
    citation_valid: bool | None
    reuse_row_correct: bool | None
    save_refusals: int
    save_warnings: int


def _stale_avoided(task: Task, context: str) -> bool | None:
    if not task.stale_doc:
        return None
    if task.stale_quote not in context:
        return True
    successor = task.answer_docs[0]
    markers = (
        f"{task.stale_doc}: superseded by",
        f"{task.stale_doc} (superseded by",
        f"superseded_by: {successor}",
    )
    return any(marker in context for marker in markers)


def outcome(task: Task, profile: Profile, transcript: Transcript, state: RunState | None) -> Outcome:
    context = transcript.context()
    has_answer = bool(task.quote)
    quoted = task.quote in context if has_answer else None
    reached = (quoted and any(doc in context for doc in task.answer_docs)) if has_answer else None
    stale = _stale_avoided(task, context)
    correct = bool(quoted) and stale is not False if has_answer else True
    pitfall = (correct or task.pitfall in context) if task.pitfall else None
    none_found = ("prior context: none found" in context) if task.scenario_class == 5 and profile.condition == "engmem" else None
    saved = state.saved if state is not None and profile.saves else None
    row = state.reuse_row if state is not None else ""
    citation_valid = None
    reuse_correct = None
    if saved is not None:
        problems = CITATION_PROBLEM in state.save_text or "quote not found" in state.save_text
        if state.row_refused:
            citation_valid = False
        elif row:
            citation_valid = bool(saved and not problems)
        if has_answer:
            reuse_correct = bool(row) and any(f"| {doc}@" in row for doc in task.answer_docs) and row.endswith("| reuse |")
        else:
            reuse_correct = saved and not row
    warnings = state.save_text.count("warning:") if state is not None else 0
    return Outcome(
        reached=reached,
        quoted=quoted,
        pitfall_avoided=pitfall,
        stale_avoided=stale,
        none_found_said=none_found,
        correct=correct,
        saved=saved,
        citation_valid=citation_valid,
        reuse_row_correct=reuse_correct,
        save_refusals=state.save_refusals if state is not None else 0,
        save_warnings=warnings,
    )


def observed_bytes(store: Path, session_id: str | None) -> int | None:
    """What engmem itself recorded as delivered to the session in `cost.jsonl`."""
    path = store / "cost.jsonl"
    if session_id is None or not path.is_file():
        return None
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("session_id") == session_id and row.get("op") in ("search", "read"):
            total += int(row.get("bytes", 0))
    return total


def cost(transcript: Transcript) -> dict:
    """Calls, bytes, tokens and seconds, split by phase and by what the bytes were."""
    tool_bytes = transcript.bytes_of(TOOL) + transcript.bytes_of(PASTE)
    template_bytes = transcript.bytes_of(TEMPLATE)
    retrieval_bytes = sum(
        transcript.bytes_of(kind, phase) for kind in (TOOL, PASTE) for phase in (START, SEARCH, LOAD)
    )
    save_template = transcript.bytes_of(TEMPLATE, SAVE)
    to_answer_bytes = retrieval_bytes + transcript.bytes_of(TEMPLATE, START)
    save_bytes = save_template + transcript.bytes_of(TOOL, SAVE)
    return {
        "to_answer_bytes": to_answer_bytes,
        "to_answer_tokens": tokens(to_answer_bytes),
        "to_answer_calls": transcript.calls(START) + transcript.calls(SEARCH) + transcript.calls(LOAD),
        "save_bytes": save_bytes,
        "save_tokens": tokens(save_bytes),
        "calls": transcript.calls(),
        "retrieval_calls": transcript.calls(SEARCH) + transcript.calls(LOAD),
        "save_calls": transcript.calls(SAVE),
        "user_round_trips": sum(1 for s in transcript.steps if s.kind == PASTE),
        "context_bytes": tool_bytes,
        "context_tokens": tokens(tool_bytes),
        "retrieval_bytes": retrieval_bytes,
        "retrieval_tokens": tokens(retrieval_bytes),
        "template_bytes": template_bytes,
        "template_tokens": tokens(template_bytes),
        "save_template_bytes": save_template,
        "total_tokens": tokens(tool_bytes + template_bytes),
        "seconds": round(transcript.seconds(), 4),
        "seconds_by_phase": {p: round(transcript.seconds(p), 4) for p in (START, SEARCH, LOAD, SAVE)},
    }


def as_dict(value: Outcome) -> dict:
    return asdict(value)
