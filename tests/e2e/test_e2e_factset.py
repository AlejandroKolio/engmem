"""Classes 10-13, the facts file and the size settings: deterministic and honest about each answer."""

from __future__ import annotations

import json
import random
import re

import pytest

from agentsim import corpus, factset, padding
from agentsim.gitfixture import commit_ids
from agentsim.sandbox import Sandbox
from engmem.spine import load_store

pytestmark = pytest.mark.e2e

CLASS_DOCS, CLASS_FACTS = factset.class_docs(random.Random(corpus.CLASS_SEED))


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    return corpus.generate(tmp_path_factory.mktemp("facts"), filler=20, scale_filler=30)


def _text(generated, doc_id: str, store: str = "base") -> str:
    return (generated.stores[store] / f"{doc_id}.md").read_text(encoding="utf-8")


def test_the_facts_file_holds_every_field_and_the_same_bytes_for_one_seed_and_size(tmp_path):
    first = corpus.generate(tmp_path / "a", filler=10, scale_filler=20)
    corpus.generate(tmp_path / "b", filler=10, scale_filler=20)
    assert (tmp_path / "a" / "facts.json").read_bytes() == (tmp_path / "b" / "facts.json").read_bytes()
    payload = json.loads((tmp_path / "a" / "facts.json").read_text(encoding="utf-8"))
    assert payload["format"] == factset.FACTS_FORMAT and payload["doc_size"] == padding.DEFAULT_DOC_SIZE
    for record in payload["facts"]:
        for key in ("id", "section", "value", "stale_values", "entities", "repo", "class", "doc_size"):
            assert key in record, key
    assert {r["class"] for r in payload["facts"]} == set(factset.FACT_CLASSES)
    assert factset.load_facts(tmp_path / "a" / "facts.json") == {f.id: f for f in first.facts}


def test_the_facts_file_is_written_without_the_stores(tmp_path):
    factset.write_facts(tmp_path / "facts.json", corpus.all_facts(), "12kb")
    payload = json.loads((tmp_path / "facts.json").read_text(encoding="utf-8"))
    assert {r["doc_size"] for r in payload["facts"]} == {"12kb"}
    assert [r["id"] for r in payload["facts"]] == [f.id for f in corpus.all_facts()]


@pytest.mark.parametrize("fact", corpus.all_facts(), ids=lambda f: f.id)
def test_each_fact_value_sits_on_one_line_of_its_source_and_no_stale_value_does(generated, fact):
    if fact.source_file:
        source = dict(generated.histories[fact.repo][-1].files)[fact.source_file]
    else:
        source = _text(generated, fact.doc, fact.store)
    assert any(fact.value in line for line in source.splitlines())
    for stale in fact.stale_values:
        assert not re.search(rf"\b{re.escape(stale)}", source), stale


def test_the_stores_load_without_a_complaint(generated):
    result = load_store(generated.stores["base"])
    assert result.errors == [] and result.warnings == []


def test_hard_stale_chains_have_depths_1_3_6_and_keep_the_stale_value_out_of_the_primer(generated):
    depths = sorted(len(f.stale_docs) for f in CLASS_FACTS if f.scenario_class == 10)
    assert depths == [1, 1, 3, 3, 6, 6]
    for fact in (f for f in CLASS_FACTS if f.scenario_class == 10):
        for doc_id, value in zip(fact.stale_docs, fact.stale_values):
            text = _text(generated, doc_id)
            primer = text.split("Future LLM Context", 1)[1].split("\n## ", 1)[0]
            decisions = text.split("Decision Log", 1)[1].split("\n## ", 1)[0]
            assert value in decisions and value not in primer
            assert "status: superseded" in text
    renamed = [f for f in CLASS_FACTS if f.scenario_class == 10 and "renamed" in f.variant]
    for fact in renamed:
        old_words = fact.topic.split()[1].lower()
        assert old_words not in _text(generated, fact.doc).lower()


def test_freshness_notes_are_anchored_at_commit_a_and_only_variant_a_files_change():
    history = factset.meter_history()
    first, last = dict(history[0].files), dict(history[-1].files)
    anchor = commit_ids(history)[0]
    for fact in (f for f in CLASS_FACTS if f.scenario_class == 11):
        note = next(d for d in CLASS_DOCS if d.id == fact.doc)
        assert anchor in note.verified_at and note.covers_files == fact.covers_files
        changed = first[fact.covers_files[0]] != last[fact.covers_files[0]]
        assert changed is fact.variant.startswith("a")


def test_the_commit_ids_match_what_git_writes(tmp_path):
    box = Sandbox.create(tmp_path / "box")
    if not box.has_git:
        pytest.skip("git is not on PATH")
    checkout = box.ensure_checkout(factset.METER_REPO, factset.meter_history())
    log = box.run(["git", "log", "--format=%H"], cwd=checkout).stdout.split()
    assert log[::-1] == commit_ids(factset.meter_history())


def test_near_duplicates_hold_20_to_50_notes_with_some_left_active_and_the_newest_canonical():
    for fact in (f for f in CLASS_FACTS if f.scenario_class == 12):
        group = [d for d in CLASS_DOCS if d.title == next(x.title for x in CLASS_DOCS if x.id == fact.doc)]
        assert 20 <= len(group) <= 50
        assert max(group, key=lambda d: d.date).id == fact.doc
        active = [d for d in group if d.status == "active"]
        assert 1 < len(active) < len(group)


def test_an_exact_identifier_appears_verbatim_in_one_body_and_not_in_its_primer(generated):
    for fact in (f for f in CLASS_FACTS if f.scenario_class == 13):
        (identifier,) = fact.identifiers
        holders = [p.stem for p in generated.stores["base"].glob("*.md") if identifier in p.read_text(encoding="utf-8")]
        assert holders == [fact.doc]
        primer = _text(generated, fact.doc).split("Future LLM Context", 1)[1].split("\n## ", 1)[0]
        assert identifier not in primer


UNIT_SLACK = 600


@pytest.mark.parametrize("size", list(padding.DOC_SIZES))
def test_padding_brings_smaller_full_documents_up_to_the_size_and_leaves_the_rest_alone(size):
    target = padding.DOC_SIZES[size]
    for doc in corpus.core_docs(random.Random(corpus.SEED)) + CLASS_DOCS[:10]:
        padded = padding.pad(doc, target)
        before, after = len(doc.render().encode("utf-8")), len(padded.render().encode("utf-8"))
        if doc.full and before < target:
            assert target - UNIT_SLACK < after <= target, doc.id
        else:
            assert padded == doc


def test_a_stored_document_has_the_byte_size_it_was_padded_to_on_every_platform(tmp_path):
    generated = corpus.generate(tmp_path, filler=4, scale_filler=4, doc_size="12kb")
    for path in generated.stores["base"].glob("*.md"):
        data = path.read_bytes()
        assert b"\r\n" not in data
        assert len(data) == len(data.decode("utf-8").encode("utf-8"))


def test_padding_adds_no_digit_and_keeps_every_section_it_had():
    doc = corpus.core_docs(random.Random(corpus.SEED))[0]
    padded = padding.pad(doc, padding.DOC_SIZES["30kb"])
    for role, text in doc.sections.items():
        kept = "" if text.strip() == "None hit." else text.strip()
        assert padded.sections[role].startswith(kept), role
        assert not re.search(r"\d", padded.sections[role][len(kept):]), role


def test_an_unknown_size_is_refused_with_the_known_ones():
    with pytest.raises(ValueError, match="4kb, 12kb, 30kb"):
        padding.size_bytes("8kb")


def test_every_scored_class_has_at_least_five_distinct_facts_each_with_its_own_document():
    facts = corpus.all_facts()
    for cls in factset.FACT_CLASSES:
        mine = [f for f in facts if f.scenario_class == cls]
        assert len(mine) >= 5, cls
        assert len({f.doc for f in mine}) == len(mine), cls
        assert len({f.value for f in mine}) == len(mine), cls
    assert len({f.id for f in facts}) == len(facts)


def test_the_first_released_facts_keep_their_ids_and_order():
    first = [
        "f01-keyword", "f02-wording-gap", "f03-role", "f04-stale", "f06-cross-repo", "f07-scale",
        "f10-ledgersealer-seal-batch-size", "f10-payoutrouter-fallback-window", "f10-quotecache-refresh-period",
        "f10-tariffloader-reload-interval", "f10-fraudscreen-score-threshold", "f10-statementmailer-send-window",
        "f11-meterflusher-flush-lag-limit", "f11-readingcollector-reading-window", "f11-dedupfilter-dedup-horizon",
        "f11-uploadretrier-retry-budget", "f12-settlementclock-cutoff-time", "f12-exportthrottle-burst-ceiling",
        "f13-ingest-backpressure-high-watermark", "f13-err-seal-digest-mismatch",
        "f13-report-render-max-parallel-pages", "f13-e4417-cursor-token-expired",
    ]
    assert [f.id for f in corpus.all_facts()][: len(first)] == first


def test_near_duplicates_come_in_at_least_five_independent_groups():
    groups = [f for f in CLASS_FACTS if f.scenario_class == 12]
    assert len(groups) >= 5
    titles = {next(d.title for d in CLASS_DOCS if d.id == f.doc) for f in groups}
    assert len(titles) == len(groups)
    for fact in groups:
        assert not set(fact.stale_docs) & {d for other in groups if other is not fact for d in other.stale_docs}


def test_the_freshness_fixture_covers_every_class_11_note_and_both_variants_have_two_or_more():
    files = dict(factset.meter_history()[-1].files)
    notes = [f for f in CLASS_FACTS if f.scenario_class == 11]
    assert all(f.covers_files[0] in files for f in notes)
    variants = [f.variant[0] for f in notes]
    assert variants.count("a") >= 2 and variants.count("b") >= 2
