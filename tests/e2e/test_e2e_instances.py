"""The task-instance file: a strict loader, keys checked against the facts file, every instance run
(contracts/e2e.md, "Task instances")."""

from __future__ import annotations

import copy
import json
import re
import tempfile
from pathlib import Path

import pytest

from agentsim import corpus, instances, phase4
from agentsim.instances import InstanceError

pytestmark = pytest.mark.e2e

FACTS = instances.facts_by_id(corpus.all_facts())
PAYLOAD = json.loads(instances.PROVISIONAL_FILE.read_text(encoding="utf-8"))


def _with(**changes) -> dict:
    payload = copy.deepcopy(PAYLOAD)
    payload["instances"] = [dict(payload["instances"][6], **changes)]
    return payload


def test_the_provisional_file_loads_is_marked_provisional_and_has_two_per_class():
    loaded = instances.parse(PAYLOAD, FACTS)
    assert loaded.provisional is True
    per_class: dict[int, int] = {}
    for instance in loaded.instances:
        per_class[instance.scenario_class] = per_class.get(instance.scenario_class, 0) + 1
    assert per_class == {cls: 2 for cls in (1, 2, 3, 4, 6, 7, 10, 11, 12, 13)}
    assert {i.wording for i in loaded.instances} == set(instances.WORDINGS)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"class": 5}, "`class` must be one of"),
        ({"class": True}, "`class` must be one of"),
        ({"wording": "keyword"}, "`wording` must be one of"),
        ({"task": "  "}, "`task` must be a non-empty string"),
        ({"required": []}, "`required` must not be empty"),
        ({"required": ["("]}, "does not compile"),
        ({"stale": "200"}, "`stale` must be a list"),
        ({"keys": ["f99-missing"]}, "is not in the facts file"),
        ({"keys": ["f01-keyword"]}, "is a class 1 fact"),
        ({"required": [r"\b65 connections"]}, "matches no keyed value"),
        ({"stale": [r"\b64 connections"]}, "matches a current value"),
        ({"stale": [r"\b999 connections"]}, "matches no stale value"),
        ({"id": "Upper Case"}, "`id` must be lowercase"),
        ({"extra": 1}, "unknown field extra"),
        ({"repo": 3}, "`repo` must be a string"),
        ({"task": "Is the ReportStore pool 64 connections per instance?"}, "matches the task text"),
        ({"stale": []}, r"miss 1 stale value\(s\)"),
        ({"stale": [r"\bsixty-four connections"]}, "matches a current value"),
        ({"task": "Is the ReportStore pool sixty-four connections per instance?"}, "matches the task text"),
        ({"required": [r"\b64 connections per the instance"]}, "`required` regex .* holds the word a, an or the"),
        ({"stale": [r"\bthe\s+200 connections"]}, "`stale` regex .* holds the word a, an or the"),
    ],
    ids=[
        "unknown-class", "bool-class", "wording", "empty-task", "no-required", "bad-regex", "stale-not-list",
        "unknown-key", "key-of-another-class", "required-misses", "stale-matches-current", "stale-matches-nothing",
        "id-format", "unknown-field", "repo-type", "required-in-task", "stale-uncovered",
        "stale-matches-current-in-words", "required-in-task-in-words", "article-in-required", "article-in-stale",
    ],
)
def test_each_breach_of_the_format_is_refused_with_its_rule(changes, message):
    with pytest.raises(InstanceError, match=message):
        instances.parse(_with(**changes), FACTS)


def test_a_key_value_no_required_regex_covers_is_refused():
    two = _with(**{"class": 10, "keys": ["f10-quotecache-refresh-period", "f10-ledgersealer-seal-batch-size"],
                   "required": [r"\b95 seconds"], "stale": []})
    with pytest.raises(InstanceError, match="no required regex matches the value of key 'f10-ledgersealer"):
        instances.parse(two, FACTS)


@pytest.mark.parametrize(
    ("top", "message"),
    [
        ({"format": "other/1"}, "not a engmem-e2e-instances/1 file"),
        ({"provisional": "yes"}, "`provisional` must be true or false"),
        ({"instances": []}, "`instances` must be a non-empty list"),
        ({"reviewer": "x"}, "unknown top-level field"),
    ],
    ids=["format", "provisional", "empty", "unknown-top"],
)
def test_the_file_level_rules(top, message):
    payload = copy.deepcopy(PAYLOAD) | top
    with pytest.raises(InstanceError, match=message):
        instances.parse(payload, FACTS)


def test_stale_regexes_must_cover_every_stale_value_of_their_keys_together():
    fact = FACTS["f12-exportthrottle-burst-ceiling"]
    base = {"class": 12, "keys": [fact.id], "required": [rf"\b{fact.value}"]}
    half = fact.stale_values[: len(fact.stale_values) // 2]
    rest = fact.stale_values[len(fact.stale_values) // 2:]
    with pytest.raises(InstanceError, match=f"miss {len(rest)} stale"):
        instances.parse(_with(**base, stale=[instances_alt(half)]), FACTS)
    instances.parse(_with(**base, stale=[instances_alt(half), instances_alt(rest)]), FACTS)


def instances_alt(values) -> str:
    return r"\b(?:" + "|".join(re.escape(v) for v in values) + ")"


def test_rejected_lists_every_refused_instance_on_its_own():
    payload = copy.deepcopy(PAYLOAD)
    payload["instances"][0]["wording"] = "keyword"
    payload["instances"][3]["required"] = ["("]
    found = instances.rejected(payload, FACTS)
    assert [i for i, _ in found] == [PAYLOAD["instances"][0]["id"], PAYLOAD["instances"][3]["id"]]
    assert instances.rejected(PAYLOAD, FACTS) == []


def test_an_id_used_twice_is_refused():
    payload = copy.deepcopy(PAYLOAD)
    payload["instances"].append(dict(payload["instances"][0]))
    with pytest.raises(InstanceError, match="used twice"):
        instances.parse(payload, FACTS)


def test_every_instance_becomes_a_task_in_file_order_and_none_is_dropped():
    loaded = instances.parse(PAYLOAD, FACTS)
    tasks = instances.tasks_of(loaded, FACTS)
    assert [t.id for t in tasks] == [i.id for i in loaded.instances]
    plan = phase4.plan(loaded, FACTS, doc_size="4kb", smoke=False)
    assert [t.id for t in plan.instances] == [i.id for i in loaded.instances]
    for profile in phase4.PROFILES:
        if profile != "grep-notes":
            assert {t.id for p, t in plan.pairs([]) if p.name == profile} == {i.id for i in loaded.instances}


def test_a_task_takes_its_checkout_from_the_instance_or_else_its_first_fact():
    loaded = instances.parse(PAYLOAD, FACTS)
    tasks = {t.id: t for t in instances.tasks_of(loaded, FACTS)}
    assert tasks["c06-identifier"].repo == "intake-gateway" != FACTS["f06-cross-repo"].repo
    assert tasks["c11-identifier"].repo == "meter-service"
    assert tasks["c07-identifier"].store == "scale"


def test_every_provisional_instance_is_answerable_from_the_generated_stores(tmp_path):
    generated = corpus.generate(tmp_path, filler=10, scale_filler=20)
    loaded = instances.parse(PAYLOAD, FACTS)
    assert instances.answerable_problems(loaded, FACTS, generated) == []


def test_an_instance_whose_answer_document_is_gone_is_not_answerable(tmp_path):
    generated = corpus.generate(tmp_path, filler=10, scale_filler=20)
    loaded = instances.parse(PAYLOAD, FACTS)
    (generated.stores["base"] / f"{FACTS['f04-stale'].doc}.md").unlink()
    problems = instances.answerable_problems(loaded, FACTS, generated)
    assert any("c04-identifier" in p and "missing" in p for p in problems)


def test_a_refused_file_stops_the_phase4_run_before_anything_runs(tmp_path):
    path = tmp_path / "instances.json"
    path.write_text(json.dumps(_with(wording="keyword")), encoding="utf-8")
    with pytest.raises(phase4.InstancesRefused, match="wording"):
        phase4.run(tmp_path / "out", path)
    assert not (tmp_path / "out").exists()


def _blind_rejected() -> list[tuple[str, str]]:
    found = instances.rejected(json.loads(instances.BLIND_FILE.read_text(encoding="utf-8")), FACTS)
    if found:
        return found
    with tempfile.TemporaryDirectory() as root:
        generated = corpus.generate(Path(root), filler=10, scale_filler=20)
        return instances.stale_leaks(instances.load(instances.BLIND_FILE, FACTS), FACTS, generated)


BLIND_REJECTED = _blind_rejected()
awaiting_author = pytest.mark.skipif(
    bool(BLIND_REJECTED),
    reason=f"blind instances back with the author: {', '.join(dict.fromkeys(i for i, _ in BLIND_REJECTED))}",
)


def test_the_blind_set_is_the_default_and_any_instance_it_refuses_is_named_with_its_rule():
    assert instances.default_file() == instances.BLIND_FILE
    for instance_id, reason in BLIND_REJECTED:
        assert reason.startswith(f"instance {instance_id!r}: ") or reason.startswith("stale regex ")


@awaiting_author
def test_the_blind_set_loads_unchanged_and_is_not_provisional():
    loaded = instances.load(instances.BLIND_FILE, FACTS)
    assert loaded.provisional is False
    per_class: dict[int, int] = {}
    for instance in loaded.instances:
        per_class[instance.scenario_class] = per_class.get(instance.scenario_class, 0) + 1
    assert per_class == {cls: 10 for cls in (1, 2, 3, 4, 6, 7, 10, 11, 12, 13)}
    assert {i.wording for i in loaded.instances} == set(instances.WORDINGS)


@awaiting_author
def test_every_blind_instance_is_answerable_and_planned_under_every_arm(tmp_path):
    loaded = instances.load(instances.BLIND_FILE, FACTS)
    generated = corpus.generate(tmp_path, filler=10, scale_filler=20)
    assert instances.answerable_problems(loaded, FACTS, generated) == []
    plan = phase4.plan(loaded, FACTS, doc_size="4kb", smoke=False)
    ids = [i.id for i in loaded.instances]
    for profile in phase4.PROFILES:
        if profile != "grep-notes":
            assert sorted(t.id for p, t in plan.pairs([]) if p.name == profile) == sorted(ids)


def _quotecache(stale: str) -> instances.InstanceSet:
    payload = _with(**{"class": 10, "keys": ["f10-quotecache-refresh-period"], "required": [r"\b95 seconds"],
                       "stale": [stale]})
    return instances.parse(payload, FACTS)


def test_a_stale_regex_matching_a_value_in_an_unrelated_document_is_refused_and_names_it(tmp_path):
    generated = corpus.generate(tmp_path, filler=10, scale_filler=20)
    broad = _quotecache(r"\b(?!95\b)\d+ seconds")
    leaks = instances.stale_leaks(broad, FACTS, generated)
    assert leaks and all(i == broad.instances[0].id for i, _ in leaks)
    assert any("20260312-ledger-sweeper-retry: " in why for _, why in leaks)
    exact = _quotecache(r"\b(?:10|20|45) seconds")
    assert instances.stale_leaks(exact, FACTS, generated) == []


def test_the_phase4_check_refuses_a_leaking_stale_regex_at_every_size_before_anything_runs(tmp_path):
    payload = _with(**{"class": 10, "keys": ["f10-quotecache-refresh-period"], "required": [r"\b95 seconds"],
                       "stale": [r"\b(?!95\b)\d+ seconds"]})
    path = tmp_path / "instances.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(phase4.InstancesRefused) as refused:
        phase4.run(tmp_path / "out", path)
    for size in ("4kb", "12kb", "30kb"):
        assert f"{size}: instance 'c04-identifier': stale regex" in str(refused.value)
    assert not (tmp_path / "out" / "4kb").exists()


def test_a_stale_regex_that_leaks_only_once_normalised_is_refused(tmp_path):
    generated = corpus.generate(tmp_path, filler=10, scale_filler=20)
    worded = _quotecache(r"\b(?:10|20|45) seconds|\bforty seconds")
    leaks = instances.stale_leaks(worded, FACTS, generated)
    assert any("20260312-ledger-sweeper-retry: " in why for _, why in leaks)
    for _, why in leaks:
        assert not re.search(r"\bforty seconds", why.split(": ", 1)[1], re.IGNORECASE)
