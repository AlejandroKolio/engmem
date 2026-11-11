"""The declared policy that replaces an agent's reasoning: mechanical, and blind to the answer."""

from __future__ import annotations

import dataclasses

import pytest

from agentsim import corpus, policy
from agentsim.profiles import GrepAgent
from agentsim.sandbox import Sandbox
from agentsim.trace import Transcript

pytestmark = pytest.mark.e2e

SEARCH_OUTPUT = """### 20260521-pool-cap (score: 18.1)
path: /store/sessions/20260521-pool-cap.md
matched: entities=reportstore,report,store; id=pool
  §8-decision-log   0.2 KB   - Decision: cap the ReportStore pool at 64 connections per instance.
ReportStore pool is capped at 64 connections per instance.
related: 20251104-pool (superseded by 20260521-pool-cap, /store/sessions/20260521-pool-cap.md), 20260101-other (/store/sessions/20260101-other.md)

### 20260113-unrelated (score: 6.4)
path: /store/sessions/20260113-unrelated.md
matched: id=size
  §3-flow   0.2 KB   caller -> TokenMinter -> store

20251104-pool: superseded by 20260521-pool-cap

cite as (Reuse Log prior-doc): 20260521-pool-cap@8082adb42e5311e3, 20260113-unrelated@a3e7f107936eb8c2
docs: 71 | drafts: 0 | last doc: 16d ago
"""


def test_key_terms_drop_stopwords_and_repeats_and_keep_identifiers():
    terms = policy.key_terms("Set the connection pool size for the ReportStore client and the pool")
    assert terms == ["connection", "pool", "size", "ReportStore", "client"]


def test_key_terms_are_capped():
    assert len(policy.key_terms(" ".join(f"word{i}x" for i in range(20)))) == policy.MAX_KEY_TERMS


def test_component_terms_are_the_touched_file_names():
    assert policy.component_terms(("src/map/MapWidget.ts", "TileCache.ts")) == ["MapWidget", "TileCache"]


@pytest.mark.parametrize(
    ("description", "role"),
    [
        ("list the pitfalls we hit before", "lessons"),
        ("what was rejected last time", "decisions"),
        ("Set the connection pool size", None),
    ],
)
def test_a_role_search_only_when_the_task_names_a_kind_of_section(description, role):
    assert policy.role_for(description) == role


def test_the_asked_role_is_read_beside_the_template_minimum():
    assert policy.read_roles("list the pitfalls we hit") == ("primer", "decisions", "lessons")
    assert policy.read_roles("Set the pool size") == ("primer", "decisions")


@pytest.mark.parametrize(
    ("term", "text", "visible"),
    [
        ("IntakeQueue", "matched: intake,queue", True),
        ("IntakeQueue", "matched: intake", False),
        ("pool", "Pool size", True),
        ("console", "no such word", False),
    ],
)
def test_a_term_is_visible_whole_or_as_all_its_camel_parts(term, text, visible):
    assert policy.term_visible(term, text) is visible


def test_reading_a_search_response():
    view = policy.read_search(SEARCH_OUTPUT)
    assert view.order == ["20260521-pool-cap", "20260113-unrelated"]
    assert view.citations["20260521-pool-cap"] == "20260521-pool-cap@8082adb42e5311e3"
    assert view.redirects == {"20251104-pool": "20260521-pool-cap"}
    assert view.related["20260521-pool-cap"] == ["20260521-pool-cap", "20260101-other"]


def test_documents_to_load_skip_irrelevant_results_and_follow_related_once():
    view = policy.read_search(SEARCH_OUTPUT)
    chosen = policy.documents_to_load(view, ["ReportStore", "pool", "connection"])
    assert chosen == ["20260521-pool-cap", "20260101-other"]


def test_none_found_loads_nothing():
    view = policy.read_search("prior context: none found\ndocs: 3 | drafts: 0\n")
    assert policy.documents_to_load(view, ["anything", "else"]) == []
    assert not policy.any_relevant(view, ["anything", "else"])


THREE_RELEVANT = """### a-pool (score: 9.0)
matched: pool,size
### b-pool (score: 8.0)
matched: pool,size
### c-pool (score: 7.0)
matched: pool,size
cite as (Reuse Log prior-doc): a-pool@0000000000000001, b-pool@0000000000000002, c-pool@0000000000000003
"""


def test_every_relevant_found_result_is_loaded_in_rank_order_up_to_five():
    view = policy.read_search(THREE_RELEVANT)
    assert policy.documents_to_load(view, ["pool", "size"]) == ["a-pool", "b-pool", "c-pool"]


def test_the_component_query_needs_only_one_visible_component():
    view = policy.read_search("### d-tile (score: 3.0)\nmatched: tilecache\n")
    assert policy.documents_to_load(view, ["MapWidget", "TileCache"]) == []
    assert policy.documents_to_load(view, ["MapWidget", "TileCache"], policy.RELEVANT_COMPONENTS) == ["d-tile"]


def _doc(sessions, doc_id: str, body: str, superseded_by: str = "") -> None:
    status = f"status: superseded\nsuperseded_by: {superseded_by}" if superseded_by else "status: active\nsuperseded_by:"
    (sessions / f"{doc_id}.md").write_text(f"---\nid: {doc_id}\n{status}\n---\n\n{body}\n", encoding="utf-8")


def test_grep_follows_a_successor_only_from_a_file_that_passes_the_relevance_gate(tmp_path):
    box = Sandbox.create(tmp_path / "box")
    _doc(box.sessions, "a-irrelevant", "a pool of workers", superseded_by="b-successor")
    _doc(box.sessions, "b-successor", "unrelated")
    _doc(box.sessions, "c-relevant", "pool size is 64", superseded_by="d-successor")
    _doc(box.sessions, "d-successor", "unrelated too")
    task = dataclasses.replace(corpus.tasks()[0], description="pool size", files=("x/Nothing.kt",))
    transcript = Transcript()
    GrepAgent(box, task, transcript).run()
    reads = [s.action for s in transcript.steps if s.action.startswith("Read ")]
    assert "Read a-irrelevant.md" in reads and "Read b-successor.md" not in reads
    assert "Read c-relevant.md" in reads and "Read d-successor.md" in reads
    assert policy.worth_following(["pool", "size"], "pool size") and not policy.worth_following(["pool", "size"], "pool")


@pytest.mark.parametrize("task", corpus.tasks(), ids=lambda t: t.id)
def test_the_scope_gate_skips_only_the_non_engineering_task(task):
    assert policy.engineering_task(task.description) is (task.scenario_class != policy.OUT_OF_SCOPE_CLASS)


def test_the_scope_gate_continues_when_no_word_says_otherwise():
    assert policy.engineering_task("Tidy the export module")
    assert not policy.engineering_task("Draft a blog post about our release")
