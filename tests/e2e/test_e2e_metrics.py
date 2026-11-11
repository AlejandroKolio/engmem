"""How a run is scored: from what reached the agent's context, the same way for every profile."""

from __future__ import annotations

import dataclasses

import pytest

from agentsim import corpus, metrics
from agentsim.profiles import BY_NAME, LOAD, SAVE, SEARCH, START, RunState
from agentsim.trace import TEMPLATE, TOOL, Step, Transcript, tokens

pytestmark = pytest.mark.e2e

STALE = next(t for t in corpus.tasks() if t.scenario_class == 4)
NOTHING = next(t for t in corpus.tasks() if t.scenario_class == 5)


def _transcript(*texts: str) -> Transcript:
    transcript = Transcript()
    for text in texts:
        transcript.add(Step(LOAD, TOOL, "Read", 0.0, text))
    return transcript


def test_the_stale_value_without_its_superseded_marker_is_the_trap():
    context = _transcript(f"{STALE.stale_doc}\n{STALE.stale_quote}", f"{STALE.answer_docs[0]}\n{STALE.quote}")
    outcome = metrics.outcome(STALE, BY_NAME["grep"], context, None)
    assert outcome.quoted and outcome.stale_avoided is False and not outcome.correct


@pytest.mark.parametrize(
    "marker",
    [f"{STALE.stale_doc}: superseded by", f"superseded_by: {STALE.answer_docs[0]}"],
)
def test_the_stale_value_with_its_marker_is_avoided(marker):
    context = _transcript(f"{STALE.stale_quote}\n{marker}", f"{STALE.answer_docs[0]}\n{STALE.quote}")
    outcome = metrics.outcome(STALE, BY_NAME["grep"], context, None)
    assert outcome.stale_avoided and outcome.correct and outcome.reached


def test_template_text_never_counts_as_knowledge():
    context = Transcript()
    context.add(Step(LOAD, TEMPLATE, "installed copy", 0.0, f"{STALE.answer_docs[0]} {STALE.quote}"))
    outcome = metrics.outcome(STALE, BY_NAME["grep"], context, None)
    assert not outcome.quoted and not outcome.correct


def test_no_history_is_correct_only_when_there_is_nothing_to_know():
    assert metrics.outcome(NOTHING, BY_NAME["no-history"], Transcript(), None).correct
    assert not metrics.outcome(STALE, BY_NAME["no-history"], Transcript(), None).correct


def test_a_citation_problem_in_the_completion_makes_the_citation_invalid():
    state = RunState(task=STALE, today="2026-01-01", saved=True)
    state.reuse_row = f'| {STALE.answer_docs[0]}@0123456789abcdef | x: "{STALE.quote}" | y | reuse |'
    context = _transcript(f"{STALE.answer_docs[0]}\n{STALE.quote}")
    state.save_text = "completed sessions/x.md"
    assert metrics.outcome(STALE, BY_NAME["shell-claude"], context, state).citation_valid
    state.save_text = "completed sessions/x.md\nwarning: 1 Reuse Log citation problem(s) — quote not found"
    outcome = metrics.outcome(STALE, BY_NAME["shell-claude"], context, state)
    assert outcome.citation_valid is False and outcome.reuse_row_correct


def test_a_refused_row_is_kept_and_scores_an_invalid_citation():
    state = RunState(task=STALE, today="2026-01-01", saved=True, save_text="completed", row_refused=True)
    state.reuse_row = f'| {STALE.answer_docs[0]}@0123456789abcdef | x: "{STALE.quote}" | y | reuse |'
    outcome = metrics.outcome(STALE, BY_NAME["shell-claude"], _transcript(STALE.quote), state)
    assert outcome.citation_valid is False and outcome.reuse_row_correct


def test_a_row_citing_the_stale_document_is_not_a_correct_row():
    state = RunState(task=STALE, today="2026-01-01", saved=True, save_text="completed")
    state.reuse_row = f'| {STALE.stale_doc}@0123456789abcdef | x: "{STALE.stale_quote}" | y | reuse |'
    outcome = metrics.outcome(STALE, BY_NAME["shell-claude"], _transcript(STALE.stale_quote), state)
    assert outcome.reuse_row_correct is False


def test_to_answer_is_the_start_template_plus_the_retrieval_output_and_save_is_apart():
    transcript = Transcript()
    transcript.add(Step(START, TEMPLATE, "installed copy", 0.0, "t" * 70))
    transcript.add(Step(START, TOOL, "engmem mode show", 0.0, "m" * 7))
    transcript.add(Step(SEARCH, TOOL, "engmem search", 0.0, "s" * 14))
    transcript.add(Step(LOAD, TOOL, "engmem read", 0.0, "r" * 21))
    transcript.add(Step(SAVE, TEMPLATE, "installed copy", 0.0, "q" * 35))
    transcript.add(Step(SAVE, TOOL, "engmem draft complete", 0.0, "c" * 7))
    cost = metrics.cost(transcript)
    assert cost["to_answer_bytes"] == 70 + 7 + 14 + 21
    assert cost["to_answer_tokens"] == tokens(70) + cost["retrieval_tokens"] == tokens(112)
    assert cost["to_answer_calls"] == 3
    assert cost["save_bytes"] == 42 and cost["save_calls"] == 1


def test_cost_keeps_templates_apart_from_tool_output():
    transcript = Transcript()
    transcript.add(Step(SEARCH, TEMPLATE, "installed copy", 0.1, "t" * 700))
    transcript.add(Step(SEARCH, TOOL, "engmem search", 0.2, "s" * 35))
    transcript.add(Step(SAVE, TOOL, "engmem draft complete", 0.3, "c" * 7))
    cost = metrics.cost(transcript)
    assert cost["template_bytes"] == 700 and cost["template_tokens"] == 200
    assert cost["context_bytes"] == 42 and cost["retrieval_bytes"] == 35
    assert cost["calls"] == 2 and cost["retrieval_calls"] == 1 and cost["save_calls"] == 1
    assert cost["total_tokens"] == tokens(742)


def test_tokens_round_up_and_zero_stays_zero():
    assert tokens(0) == 0 and tokens(1) == 1 and tokens(7) == 2 and tokens(8) == 3


def test_a_task_without_a_pitfall_scores_none_for_it():
    task = dataclasses.replace(STALE, pitfall="")
    assert metrics.outcome(task, BY_NAME["grep"], Transcript(), None).pitfall_avoided is None


OFF_TOPIC = next(t for t in corpus.tasks() if t.scenario_class == 9)


@pytest.mark.parametrize(
    ("skipped", "written", "correct"),
    [(True, False, True), (True, True, False), (False, False, False)],
    ids=["skipped-clean", "skipped-but-wrote", "not-skipped"],
)
def test_a_non_engineering_task_is_correct_only_when_skipped_without_a_store_write(skipped, written, correct):
    state = RunState(task=OFF_TOPIC, today="2026-01-01", skipped=skipped)

    outcome = metrics.outcome(OFF_TOPIC, BY_NAME["shell-claude"], Transcript(), state, store_written=written)

    assert outcome.correct is correct and outcome.skipped is skipped
    assert (outcome.saved is None) is skipped
