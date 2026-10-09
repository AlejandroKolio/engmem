"""US-14: a weak candidate is told apart from a reliable find, in the ranking, the result text
and the telemetry row — against the versioned corpus `tests/fixtures/retrieval-eval/v1`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import fixture_docs, write_file

from engmem.output import NO_RELIABLE_MATCH, WEAK_TAG, render_telemetry_summary, surfaced_ids
from engmem.scoring import (
    Hit,
    Scope,
    SearchOutcome,
    Strength,
    SupersededNote,
    _build_query_words,
    search,
)
from engmem.search_report import compose, render_result
from engmem.spine import load_store
from engmem.telemetry import summarize

EVAL_ROOT = Path(__file__).parent / "fixtures" / "retrieval-eval" / "v1"
DOCS = Path(__file__).parent.parent / "docs" / "design"
EXPECTED = json.loads((EVAL_ROOT / "expected.json").read_text(encoding="utf-8"))


def eval_docs():
    return load_store(EVAL_ROOT / EXPECTED["corpus"]).docs


def _rows(store: Path) -> list[dict]:
    path = store / "telemetry.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _doc(
    doc_id: str,
    title: str,
    status: str = "active",
    superseded_by: str = "",
    entities: str = "[]",
    body: str = "A decision.",
) -> str:
    return (
        f"---\nid: {doc_id}\ntitle: {title}\ndate: 2026-05-01\nstatus: {status}\n"
        f"superseded_by: {superseded_by}\ntags: []\nentities: {entities}\nrelated: []\n---\n\n"
        f"## Decision Log\n\n{body}\n"
    )


def _one_doc_search(tmp_path, query: str, **doc):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "note.md", _doc("note", "Note", **doc))
    write_file(sessions, "other.md", _doc("other", "Unrelated", body="Nothing shared here."))
    return search(load_store(sessions).docs, query).hits


def _case_params():
    return [pytest.param(case, id=case["id"]) for case in EXPECTED["queries"]]


@pytest.mark.parametrize("case", _case_params())
def test_the_corpus_classifies_every_query_as_approved(case):
    outcome = search(eval_docs(), case["query"])
    got = [
        {"doc": hit.doc.id, "strength": "weak" if hit.weak else "strong"}
        for hit in outcome.hits
    ]
    expected = [{"doc": h["doc"], "strength": h["strength"]} for h in case["expected"]]
    assert got == expected, case["reason"]


def _strong_but_irrelevant(case) -> bool:
    return any(h["strength"] == "strong" and not h["relevant"] for h in case["expected"])


@pytest.mark.parametrize("case", _case_params())
def test_a_reliable_find_that_is_not_relevant_is_a_known_limit_and_only_then(case):
    assert case["known_limit"] is _strong_but_irrelevant(case), case["reason"]


def test_the_contract_lists_every_known_limit_of_the_corpus():
    contract = (DOCS / "contracts" / "scoring.md").read_text(encoding="utf-8")
    section = contract.split("### Known limits of corpus v1", 1)[1].split("\n## ", 1)[0]

    for case in EXPECTED["queries"]:
        assert (f"`{case['id']}`" in section) is case["known_limit"], case["id"]


def test_the_corpus_covers_every_acceptance_criterion():
    covered = {ac for case in EXPECTED["queries"] for ac in case["covers"]}
    assert covered == {"AC-14.1", "AC-14.2", "AC-14.3", "AC-14.4"}


WEAK_QUERY = "database migration transactional rollback policy"
MIXED_QUERY = "ledger replay policy deploy"


@pytest.fixture
def eval_store(tmp_path) -> Path:
    shutil.copytree(EVAL_ROOT / EXPECTED["corpus"], tmp_path / "sessions")
    return tmp_path


def _text(query: str, role: str | None = None) -> str:
    return render_result(eval_docs(), query, role).text


def _block(text: str, doc_id: str) -> str:
    (block,) = [b for b in text.split("\n\n") if b.startswith(f"### {doc_id} ")]
    return block


def test_ac_14_1_a_single_shared_word_is_shown_only_as_a_weak_candidate_with_its_reason():
    text = _text(WEAK_QUERY)

    assert text.splitlines()[0] == NO_RELIABLE_MATCH
    block = _block(text, "logo-colour-policy")
    assert block.splitlines()[0].endswith(WEAK_TAG)
    assert (
        "weak candidate: matched 1 of 5 query words (policy) and no identifier"
        in block
    )


def test_ac_14_1_a_reliable_find_leads_and_only_the_weak_candidates_are_marked():
    text = _text(MIXED_QUERY)

    assert NO_RELIABLE_MATCH not in text
    reliable = _block(text, "4711-ledger-replay")
    assert WEAK_TAG not in reliable and "weak candidate:" not in reliable
    assert text.index("### 4711-ledger-replay") < text.index("### cache-warmup-order")
    for doc_id, word in (("cache-warmup-order", "deploy"), ("logo-colour-policy", "policy")):
        assert f"matched 1 of 4 query words ({word})" in _block(text, doc_id)


@pytest.mark.parametrize(
    "query, matched",
    [
        pytest.param("LedgerReplayGuard flaky failover", "entities=ledgerreplayguard", id="entity"),
        pytest.param("4711", "id=4711", id="ticket-number"),
    ],
)
def test_ac_14_2_an_exact_identifier_is_a_reliable_find_with_its_reason_shown(query, matched):
    text = _text(query)

    block = _block(text, "4711-ledger-replay")
    assert WEAK_TAG not in text and NO_RELIABLE_MATCH not in text
    assert f"matched: {matched}" in block


def test_ac_14_3_no_matching_term_reports_absence_and_fills_nothing():
    rendered = render_result(eval_docs(), "kubernetes autoscaling quota", None)

    assert rendered.outcome.hits == []
    assert rendered.text == "prior context: none found"


@pytest.mark.parametrize(
    "query, result, weak_only",
    [
        pytest.param(WEAK_QUERY, "hit", True, id="only-weak"),
        pytest.param(MIXED_QUERY, "hit", False, id="mixed"),
        pytest.param("export retry backoff jitter", "hit", False, id="reliable"),
        pytest.param("kubernetes autoscaling quota", "miss", False, id="miss"),
    ],
)
@pytest.mark.parametrize("channel", ["cli", "mcp"])
def test_ac_14_4_a_weak_only_result_is_told_apart_in_its_telemetry_row(
    eval_store, channel, query, result, weak_only
):
    loaded = load_store(eval_store / "sessions")

    compose(eval_store, loaded, [], [], query, None, "s1", channel, retain_versions=False)

    (row,) = _rows(eval_store)
    # written only when true: an ordinary row stays byte-identical to one before US-14
    expected = {"weak_only": True} if weak_only else {}
    assert row["result"] == result
    assert {k: v for k, v in row.items() if k == "weak_only"} == expected


def test_ac_14_4_a_role_search_row_says_when_it_kept_only_weak_candidates(eval_store):
    loaded = load_store(eval_store / "sessions")

    compose(eval_store, loaded, [], [], WEAK_QUERY, "decisions", "s1", "cli",
            retain_versions=False)

    (row,) = _rows(eval_store)
    assert (row["result"], row["weak_only"]) == ("hit", True)


def test_a_role_search_row_with_a_reliable_find_carries_no_weak_only_key(eval_store):
    loaded = load_store(eval_store / "sessions")

    compose(eval_store, loaded, [], [], MIXED_QUERY, "decisions", "s1", "cli",
            retain_versions=False)

    (row,) = _rows(eval_store)
    assert row["result"] == "hit" and "weak_only" not in row


def test_ac_14_4_the_summary_counts_weak_only_hits_apart_and_reads_old_rows_as_before(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    rows = [
        {"channel": "cli", "result": "hit", "weak_only": True},
        {"channel": "cli", "result": "hit", "weak_only": False},
        {"channel": "cli", "result": "hit"},
        {"channel": "cli", "result": "miss", "weak_only": True},
        {"channel": "cli", "result": "hit", "weak_only": "yes"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    summary = summarize(path)

    assert (summary.overall.hits, summary.overall.weak_only) == (4, 1)
    assert (
        "weak-only hits: 1 of 4 hit(s) showed only weak candidates"
        in render_telemetry_summary(summary)
    )


def test_a_summary_without_weak_only_rows_reads_exactly_as_before(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    path.write_text(json.dumps({"channel": "cli", "result": "hit"}) + "\n", encoding="utf-8")

    assert "weak" not in render_telemetry_summary(summarize(path))


def test_both_channels_show_the_same_weak_marking(eval_store):
    loaded = load_store(eval_store / "sessions")

    cli = compose(eval_store, loaded, [], [], MIXED_QUERY, None, None, "cli",
                  retain_versions=False)
    mcp = compose(eval_store, loaded, [], [], MIXED_QUERY, None, None, "mcp",
                  retain_versions=False)

    assert cli.splitlines()[:-1] == mcp.splitlines()[:-1]
    assert cli.count(WEAK_TAG) == 2


def test_a_role_search_marks_weak_candidates_and_says_when_nothing_reliable_has_the_role():
    mixed = _text(MIXED_QUERY, "decisions")
    weak = _text(WEAK_QUERY, "decisions")

    assert mixed.count(WEAK_TAG) == 2 and NO_RELIABLE_MATCH not in mixed
    assert "weak candidate: matched 1 of 4 query words (deploy)" in mixed
    assert weak.splitlines()[:2] == ["role: decisions", NO_RELIABLE_MATCH]


def test_a_single_word_id_named_whole_is_an_identifier(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "tokenbucket.md", _doc("tokenbucket", "Rate limiter sizing"))

    (hit,) = search(load_store(sessions).docs, "tokenbucket guidance tuning").hits

    assert not hit.weak


def test_two_spellings_of_one_word_count_once():
    words = _build_query_words("responsecache ResponseCache eviction")

    assert [w.original for w in words] == ["responsecache", "eviction"]
    assert words[0].camel and words[0].tokens == ("responsecache", "response", "cache", "rc")


@pytest.mark.parametrize(
    "query, expected_first",
    [
        pytest.param("1000001", "1000001-response-cache", id="g1"),
        pytest.param("ResponseCacheController", "1000001-response-cache", id="g3"),
        pytest.param("response cache", "1000001-response-cache", id="g4"),
        pytest.param("MessageQueue sweeper", "mq-message-sweeper", id="g7"),
        pytest.param("σφάλματα και MessageQueue sweeper", "mq-message-sweeper", id="g8"),
        pytest.param("ETAG", "ttl-etag-revalidation-v2", id="g9"),
        pytest.param("etag revalidation", "ttl-etag-revalidation-v2", id="g11"),
        pytest.param("MazeRenderer", "maze-render-new", id="g12"),
    ],
)
def test_no_approved_golden_result_becomes_a_weak_candidate(query, expected_first):
    first = search(fixture_docs(), query).hits[0]

    assert (first.doc.id, first.weak) == (expected_first, False)


def test_a_weak_superseded_match_is_marked_on_its_redirect(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "old.md", _doc("old-note", "Logo policy", "superseded", "new-note"))
    write_file(sessions, "new.md", _doc("new-note", "Brand guide"))

    text = render_result(load_store(sessions).docs, "migration policy", None).text

    assert text.splitlines()[:3] == [
        NO_RELIABLE_MATCH, "", f"old-note{WEAK_TAG}: superseded by new-note",
    ]


def _hit(doc, score: float, weak: bool) -> Hit:
    return Hit(doc=doc, score=score, strength=Strength(weak, ("w",), 2))


def _note(doc, score: float, weak: bool) -> SupersededNote:
    return SupersededNote(doc=doc, successor=None, score=score,
                          strength=Strength(weak, ("w",), 2))


@pytest.mark.parametrize(
    "hits_weak, note_weak, note_score, shown",
    [
        pytest.param(False, True, 9.0, False, id="weak-redirect-loses-to-reliable-finds"),
        pytest.param(True, False, 0.5, True, id="reliable-redirect-beats-weak-candidates"),
        pytest.param(False, False, 9.0, True, id="higher-reliable-redirect-as-before"),
        pytest.param(False, False, 0.5, False, id="lower-reliable-redirect-as-before"),
    ],
)
def test_a_redirect_is_shown_only_when_it_would_have_won_a_place(
    hits_weak, note_weak, note_score, shown
):
    docs = [d for d in fixture_docs() if d.status == "active"]
    hits = [_hit(doc, 1.0, hits_weak) for doc in docs[:3]]
    note = _note(docs[3], note_score, note_weak)
    outcome = SearchOutcome(hits=hits, ambiguous=False, superseded_notes=[note])

    assert (docs[3].id in surfaced_ids(outcome)) is shown


@pytest.mark.parametrize(
    "entities, query, weak",
    [
        pytest.param("[brand policy, BrandPalette]", WEAK_QUERY, True, id="multi-word-partial"),
        pytest.param("[brand policy]", "brand policy for migrations", False, id="multi-word-full"),
        pytest.param("[revalidation]", "revalidation rollback window", False, id="single-word"),
        pytest.param("[TTL]", "ttl rollback window", False, id="short-entity"),
    ],
)
def test_an_entity_is_an_identifier_only_when_matched_in_full(tmp_path, entities, query, weak):
    (hit,) = _one_doc_search(tmp_path, query, entities=entities)

    assert hit.doc.id == "note" and hit.weak is weak


@pytest.mark.parametrize(
    "body, query, weak",
    [
        pytest.param("The policy και the rest.", "και policy rollback", True, id="greek-3-chars"),
        pytest.param("The policy for it.", "for", True, id="only-a-short-word"),
        pytest.param("The policy for it.", "the policy", False, id="short-word-plus-one-word"),
        pytest.param(
            "The policy for rollback.", "policy for rollback", False, id="two-long-words"
        ),
    ],
)
def test_a_short_word_counts_only_as_an_identifier(tmp_path, body, query, weak):
    (hit,) = _one_doc_search(tmp_path, query, body=body)

    assert hit.weak is weak


def test_the_reason_line_says_why_a_short_word_did_not_count():
    text = _text("rollback policy for database migrations")

    assert (
        "matched 2 of 5 query words (policy, for; words of 3 characters or fewer count only "
        "as identifiers) and no identifier"
    ) in _block(text, "logo-colour-policy")


def test_a_scoped_search_with_only_weak_candidates_names_the_scope_first():
    text = render_result(eval_docs(), WEAK_QUERY, None, Scope(all_repos=True)).text

    lines = text.splitlines()
    assert lines[0].startswith("scope: all repositories") and lines[1] == NO_RELIABLE_MATCH
