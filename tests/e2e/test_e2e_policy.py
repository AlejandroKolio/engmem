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
    assert terms == ["ReportStore", "connection", "pool", "size", "client"]


def test_key_terms_are_capped():
    assert len(policy.key_terms(" ".join(f"word{i}x" for i in range(20)))) == policy.MAX_KEY_TERMS


def test_component_terms_are_the_touched_file_names():
    assert policy.component_terms(("src/map/MapWidget.ts", "TileCache.ts")) == ["MapWidget", "TileCache"]


def test_e_reads_primer_and_decisions_first_as_the_daily_template_says():
    assert policy.READ_ROLES == ("primer", "decisions")


@pytest.mark.parametrize(
    ("description", "primer", "roles"),
    [
        ("Tune the IntakeQueue consumer", "IntakeQueue batching: the pitfalls are in Lessons Learned.", ["lessons"]),
        ("Tune PageComposer", "PageComposer: see Production Considerations and Lessons Learned for the details.",
         ["lessons", "production"]),
        ("What pitfall did we hit with DigestMailer?", "DigestMailer batching: one run a day.", ["lessons"]),
        ("Why did the export fail last night?", "", ["lessons"]),
        ("What caused the duplicate rows?", "", ["lessons"]),
        ("What limit is configured for ExportThrottle?", "", ["production"]),
        ("Which production setting does TileWarmer use?", "TileWarmer: see Lessons Learned.", ["lessons", "production"]),
        ("What is the GatewayBreaker trip threshold?", "The GatewayBreaker trip threshold is 11 failures.", []),
    ],
    ids=["primer-lessons", "primer-both", "task-pitfall", "task-fail", "task-cause", "task-limit", "both-ways",
         "neither"],
)
def test_e_also_reads_the_section_the_primer_points_to_or_the_task_asks_about(description, primer, roles):
    assert policy.asked_roles(description, primer) == roles


@pytest.mark.parametrize(
    ("description", "first"),
    [
        ("What is ingest.backpressure.high_watermark set to?", "ingest.backpressure.high_watermark"),
        ("Replay stops with ERR_SEAL_DIGEST_MISMATCH again", "ERR_SEAL_DIGEST_MISMATCH"),
        ("What does `E4417 cursor token expired` mean here?", "E4417 cursor token expired"),
        ("tune the ReadingCollector window", "ReadingCollector"),
    ],
    ids=["dotted", "snake", "backticked", "camel"],
)
def test_an_identifier_goes_first_and_verbatim_into_the_query(description, first):
    assert policy.key_terms(description)[0] == first


def test_identifiers_do_not_count_toward_the_word_cap():
    description = "ReportStore " + " ".join(f"word{i}x" for i in range(20))
    terms = policy.key_terms(description)
    assert terms[0] == "ReportStore" and len(terms) == policy.MAX_KEY_TERMS + 1


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


CHAIN_OUTPUT = """### 20260103-other (score: 5.1)
matched: id=window

20250210-old-r1  [weak candidate]: superseded by 20250211-old-r2 → 20260520-renamed-hold

### 20260520-renamed-hold (successor)
snapshot: x: unknown, no anchor recorded
The renamed hold is 4 minutes.
"""


def test_a_successor_block_is_read_with_the_redirect_that_leads_to_it_and_the_chain_end():
    view = policy.read_search(CHAIN_OUTPUT)
    assert view.order == ["20260103-other", "20260520-renamed-hold"]
    assert view.redirects == {"20250210-old-r1": "20260520-renamed-hold"}
    assert "20250210-old-r1" in view.blocks["20260520-renamed-hold"]
    assert "20250210-old-r1" not in view.blocks["20260103-other"]
    assert policy.documents_to_load(view, ["old", "hold"]) == ["20260520-renamed-hold"]


def test_the_store_path_of_a_result_is_not_judged_for_relevance():
    block = (
        "### 20260314-walletlocker-lock-timeout (score: 9.0)\n"
        "path: /tmp/a-much-longer-wait/store/sessions/20260314-walletlocker-lock-timeout.md\n"
        "WalletLocker lock timeout of 8 seconds.\n"
    )
    assert not policy.relevant(["long", "wait"], block)
    assert policy.relevant(["walletlocker", "timeout"], block)
