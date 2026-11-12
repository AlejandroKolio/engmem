"""Scores one run: outcome from what reached the agent's context, cost from its transcript."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from agentsim import grading
from agentsim.accounting import turn_overhead
from agentsim.corpus import Task
from agentsim.policy import NONE_FOUND, OUT_OF_SCOPE_CLASS
from agentsim.profiles import DAILY, LOAD, SAVE, SEARCH, START, Profile, RunState
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
    skipped: bool | None
    correct: bool
    saved: bool | None
    citation_valid: bool | None
    reuse_row_correct: bool | None
    related_names_answer: bool | None
    save_refusals: int
    save_warnings: int
    stale_seen: bool | None = None
    answer_sources: tuple[str, ...] = ()
    answered: bool | None = None
    opened_answer: bool | None = None
    correct_newest_rule: bool | None = None
    required_ok: bool | None = None


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


def outcome(
    task: Task,
    profile: Profile,
    transcript: Transcript,
    state: RunState | None,
    store_written: bool | None = None,
    subjects: dict[str, str] | None = None,
) -> Outcome:
    if task.required:
        return _graded(task, profile, transcript, state, subjects or {})
    context = transcript.context()
    has_answer = bool(task.quote)
    quoted = task.quote in context if has_answer else None
    reached = (quoted and any(doc in context for doc in task.answer_docs)) if has_answer else None
    stale = _stale_avoided(task, context)
    correct = bool(quoted) and stale is not False if has_answer else True
    skipped = state.skipped if state is not None else None
    if task.scenario_class == OUT_OF_SCOPE_CLASS and state is not None:
        correct = bool(skipped) and not store_written
    pitfall = (correct or task.pitfall in context) if task.pitfall else None
    none_found = (NONE_FOUND in context) if task.scenario_class == 5 and profile.condition == "engmem" else None
    saved = state.saved if state is not None and profile.saves and not skipped else None
    row = state.reuse_row if state is not None else ""
    citation_valid = None
    reuse_correct = None
    related_correct = None
    if saved is not None and state.mode == DAILY:
        related_correct = (state.related in task.answer_docs) if has_answer else (saved and not state.related)
    if saved is not None and state.mode != DAILY:
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
        skipped=skipped,
        correct=correct,
        saved=saved,
        citation_valid=citation_valid,
        reuse_row_correct=reuse_correct,
        related_names_answer=related_correct,
        save_refusals=state.save_refusals if state is not None else 0,
        save_warnings=warnings,
    )


NEWEST_RULE = frozenset({"grep-guided"})


def _graded(task: Task, profile: Profile, transcript: Transcript, state: RunState | None, subjects) -> Outcome:
    """An authored instance, graded literally by its regexes; only G+, told to prefer the newest note,
    is graded newest-first. E's newest-first grade is a sensitivity figure, never its score."""
    required = grading.compile_all(task.required)
    stale = grading.compile_all(task.stale_patterns)
    result = grading.grade(transcript, required, stale, newest_only=profile.name in NEWEST_RULE, subjects=subjects)
    sensitivity = None
    if profile.condition == "engmem":
        sensitivity = grading.grade(transcript, required, stale, newest_only=True, subjects=subjects).correct
    answered = bool(result.answer_sources)
    saved = state.saved if state is not None and profile.saves and not state.skipped else None
    return Outcome(
        reached=None,
        quoted=None,
        pitfall_avoided=None,
        stale_avoided=result.stale_avoided if task.stale_patterns and answered else None,
        none_found_said=None,
        skipped=state.skipped if state is not None else None,
        correct=result.correct,
        saved=saved,
        citation_valid=None,
        reuse_row_correct=None,
        related_names_answer=None,
        save_refusals=state.save_refusals if state is not None else 0,
        save_warnings=state.save_text.count("warning:") if state is not None else 0,
        stale_seen=result.stale_seen if task.stale_patterns else None,
        answer_sources=result.answer_sources,
        answered=answered,
        opened_answer=any(doc in result.opened for doc in task.answer_docs),
        correct_newest_rule=sensitivity,
        required_ok=result.required_ok,
    )


def observed_bytes(store: Path, session_id: str | None) -> int | None:
    """What engmem itself recorded as delivered to the session in `cost.jsonl`; with no session
    id, what it recorded unattributed, which in a fresh store is the daily run's own."""
    path = store / "cost.jsonl"
    if not path.is_file():
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
    answer_turns = turn_overhead(transcript.steps, (START, SEARCH, LOAD))
    session_turns = turn_overhead(transcript.steps, (START, SEARCH, LOAD, SAVE))
    return {
        "turns": answer_turns["turns"],
        "turn_tokens": answer_turns["plain"],
        "turn_tokens_cached": answer_turns["cached"],
        "save_turn_tokens": session_turns["plain"] - answer_turns["plain"],
        "save_turn_tokens_cached": session_turns["cached"] - answer_turns["cached"],
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
