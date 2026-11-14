"""The synthetic store and task file the harness runs on: deterministic, parseable, and honest
about where each answer lives (contracts/e2e.md, "Test data")."""

from __future__ import annotations

import random
import re

import pytest

from agentsim import corpus, factset, grading
from engmem.spine import load_store

pytestmark = pytest.mark.e2e

CORE_IDS = {doc.id for doc in corpus.core_docs(random.Random(corpus.SEED))} | {
    doc.id for doc in factset.class_docs(random.Random(corpus.CLASS_SEED))[0]
}


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    return corpus.generate(tmp_path_factory.mktemp("corpus"), filler=30, scale_filler=60)


def _text(generated, doc_id: str) -> str:
    return (generated.stores["base"] / f"{doc_id}.md").read_text(encoding="utf-8")


def test_the_same_seed_writes_the_same_bytes(tmp_path):
    first = corpus.generate(tmp_path / "a", filler=30, scale_filler=60)
    second = corpus.generate(tmp_path / "b", filler=30, scale_filler=60)
    for kind in ("base", "scale"):
        names = sorted(p.name for p in first.stores[kind].iterdir())
        assert names == sorted(p.name for p in second.stores[kind].iterdir())
        for name in names:
            assert (first.stores[kind] / name).read_bytes() == (second.stores[kind] / name).read_bytes()
    assert (first.root / "tasks.json").read_bytes() == (second.root / "tasks.json").read_bytes()


@pytest.mark.parametrize("kind", ["base", "scale"])
def test_engmem_loads_every_generated_document_without_a_complaint(generated, kind):
    result = load_store(generated.stores[kind])
    assert result.scan_error is None
    assert result.errors == []
    assert result.warnings == []
    assert len(result.docs) == sum(1 for _ in generated.stores[kind].glob("*.md"))


def test_every_scenario_class_exists_once_and_declares_its_winner(generated):
    plain = sorted(t.scenario_class for t in generated.tasks if not t.variant)
    assert plain == list(range(1, 10))
    variants = [t for t in generated.tasks if t.variant]
    assert [(t.scenario_class, t.always_second_search) for t in variants] == [(2, True)]
    for task in generated.tasks:
        assert task.expected_winner and task.without_history
        assert task.decision_found and task.decision_naive


@pytest.mark.parametrize("task", [t for t in corpus.tasks() if t.quote and not t.saver], ids=lambda t: t.id)
def test_each_quote_sits_on_one_line_of_its_answer_document(generated, task):
    """A Reuse Log quote is checked verbatim, so a quote split across lines could never be cited."""
    store = generated.store_for(task)
    for doc_id in task.answer_docs:
        lines = (store / f"{doc_id}.md").read_text(encoding="utf-8").splitlines()
        assert any(task.quote in line for line in lines), doc_id


def test_no_quote_leaks_into_a_document_that_does_not_answer_it(generated):
    for task in generated.tasks:
        if not task.quote or task.saver:
            continue
        for path in generated.store_for(task).glob("*.md"):
            if path.stem in task.answer_docs:
                continue
            assert task.quote not in path.read_text(encoding="utf-8"), (task.id, path.name)


def test_the_stale_trap_is_superseded_by_the_answer(generated):
    task = next(t for t in generated.tasks if t.scenario_class == 4)
    stale = _text(generated, task.stale_doc)
    assert "status: superseded" in stale
    assert f"superseded_by: {task.answer_docs[0]}" in stale
    assert task.stale_quote in stale


def test_precision_traps_share_vocabulary_but_not_the_answer(generated):
    keyword = next(t for t in generated.tasks if t.scenario_class == 1)
    trap = _text(generated, "20260313-ledger-sweeper-import-retry")
    assert "LedgerSweeper" in trap and keyword.quote not in trap
    duplicate = _text(generated, "20260314-ledger-sweeper-retry-notes")
    assert keyword.quote in duplicate


def test_the_store_spans_several_repositories_and_the_cross_repo_answer_is_elsewhere(generated):
    repos = {doc.repos[0] for doc in load_store(generated.stores["base"]).docs if doc.repos}
    assert len(repos) >= 2
    task = next(t for t in generated.tasks if t.scenario_class == 6)
    assert f"repos: [{task.repo}]" not in _text(generated, task.answer_docs[0])


def test_the_role_answer_is_outside_the_primer(generated):
    task = next(t for t in generated.tasks if t.scenario_class == 3)
    text = _text(generated, task.answer_docs[0])
    lessons = text.split("## 11. Lessons Learned", 1)[1].split("\n## ", 1)[0]
    primer = text.split("## 13. Future LLM Context", 1)[1].split("\n## ", 1)[0]
    assert task.quote in lessons and task.quote not in primer


def test_no_history_task_words_appear_nowhere_in_the_store(generated):
    task = next(t for t in generated.tasks if t.scenario_class == 5)
    words = ("keyboard", "shortcut", "sidebar", "console")
    for path in generated.stores["scale"].glob("*.md"):
        text = path.read_text(encoding="utf-8").lower()
        assert not any(word in text for word in words), path.name
    assert not task.answer_docs


def test_every_filler_supersede_points_at_a_newer_document_on_the_same_component_and_topic(generated):
    docs = {doc.id: doc for doc in load_store(generated.stores["scale"]).docs}
    superseded = [doc for doc in docs.values() if doc.status == "superseded" and doc.id not in CORE_IDS]
    assert superseded
    for doc in superseded:
        successor = docs[doc.superseded_by]
        assert str(successor.date) >= str(doc.date), doc.id
        if successor.id in CORE_IDS:
            assert successor.id == corpus.ANSWER_CHAIN_SUCCESSOR, doc.id
            assert set(doc.entities) <= set(successor.entities), doc.id
            continue
        assert successor.entities == doc.entities and successor.title == doc.title, doc.id


def test_one_component_and_topic_has_exactly_one_active_filler_document(generated):
    active: dict[str, int] = {}
    for doc in load_store(generated.stores["scale"]).docs:
        if doc.id in CORE_IDS or doc.status != "active":
            continue
        active[doc.title] = active.get(doc.title, 0) + 1
    assert active and set(active.values()) == {1}


def test_filler_on_the_scaling_answers_topic_is_one_chain_that_resolves_to_the_answer(generated):
    docs = {doc.id: doc for doc in load_store(generated.stores["scale"]).docs}
    topic = f"{corpus.ANSWER_CHAIN_COMPONENT} {corpus.ANSWER_CHAIN_TOPIC}"
    chain = [doc for doc in docs.values() if doc.id not in CORE_IDS and doc.title == topic]
    assert chain and all(doc.status == "superseded" for doc in chain)
    task = next(t for t in generated.tasks if t.scenario_class == 7)
    for doc in chain:
        current, seen = doc, set()
        while current.status == "superseded":
            assert current.id not in seen
            seen.add(current.id)
            current = docs[current.superseded_by]
        assert current.id == task.answer_docs[0], doc.id
        assert task.quote not in (generated.store_for(task) / f"{doc.id}.md").read_text(encoding="utf-8")


GENERIC_HANDOFF_ANSWERS = (
    "Generate one idempotency key per order before the first attempt and send the same key on every retry, "
    "with exponential backoff and jitter. Make sure the intake server stores the key with a unique constraint "
    "and returns the original order for a repeated key; keep keys for 24 hours.",
    "Send an Idempotency-Key header with a UUID per order; the server dedups within 24 hours in an "
    "idempotency_keys table.",
    "Use the X-Request-Id header, keep it for sixty minutes in the request_dedup table.",
)


def _handoff():
    return next(t for t in corpus.tasks() if t.saver)


def _matches_all(task, text: str) -> bool:
    return all(rule.search(text) for rule in grading.compile_all(task.required))


def test_the_handoff_specifics_come_from_the_seed():
    assert corpus.handoff_specifics() == corpus.handoff_specifics(corpus.HANDOFF_SEED)
    drawn = {corpus.handoff_specifics(seed) for seed in range(20)}
    assert len(drawn) > 10
    given = corpus.handoff_specifics()
    assert _handoff().required == (re.escape(given.header), rf"\b{given.minutes} minutes", re.escape(given.table))


def test_the_earlier_task_states_every_specific_and_the_save_records_them():
    task = _handoff()
    assert _matches_all(task, task.saver["description"])
    assert _matches_all(task, task.saver["decision"]) and _matches_all(task, task.quote)
    assert task.quote in task.saver["decision"]


@pytest.mark.parametrize("field", ["description", "decision_naive", "without_history"])
def test_the_later_task_gives_away_no_specific(field):
    task = _handoff()
    assert not any(rule.search(getattr(task, field)) for rule in grading.compile_all(task.required))


@pytest.mark.parametrize("answer", GENERIC_HANDOFF_ANSWERS)
def test_general_practice_cannot_answer_the_handoff(answer):
    task = _handoff()
    assert not any(rule.search(answer) for rule in grading.compile_all(task.required))


def test_no_document_in_any_store_holds_a_handoff_specific(generated):
    rules = grading.compile_all(_handoff().required)
    for kind, store in generated.stores.items():
        for path in store.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            assert not any(rule.search(text) for rule in rules), (kind, path.name)
