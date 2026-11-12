"""The task-instance file a blind author writes from the facts file, and the strict loader the harness uses."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agentsim.corpus import Task
from agentsim.factset import CLASS_NAMES, FACT_CLASSES, Fact

INSTANCES_FORMAT = "engmem-e2e-instances/1"
WORDINGS = ("identifier", "paraphrase", "symptom")
PROVISIONAL_FILE = Path(__file__).resolve().parent / "data" / "instances-provisional.json"
BLIND_FILE = Path(__file__).resolve().parent / "data" / "instances-blind.json"
_TOP_KEYS = {"format", "provisional", "author", "note", "instances"}
_REQUIRED_KEYS = {"id", "class", "wording", "task", "required", "stale", "keys"}
_OPTIONAL_KEYS = {"repo", "files"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class InstanceError(ValueError):
    """The instance file breaks its format; the message names the instance and the rule."""


@dataclass(frozen=True)
class Instance:
    """One authored task with its key: regexes judged against the answer, and the fact ids behind them."""

    id: str
    scenario_class: int
    wording: str
    task: str
    required: tuple[str, ...]
    stale: tuple[str, ...]
    keys: tuple[str, ...]
    repo: str = ""
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstanceSet:
    """A whole instance file: provisional when the harness, not a blind author, wrote it."""

    provisional: bool
    author: str
    instances: tuple[Instance, ...]


def _fail(where: str, rule: str) -> InstanceError:
    return InstanceError(f"{where}: {rule}")


def _strings(value, where: str, name: str, *, empty_ok: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise _fail(where, f"`{name}` must be a list of non-empty strings")
    if not value and not empty_ok:
        raise _fail(where, f"`{name}` must not be empty")
    return tuple(value)


def _patterns(values: tuple[str, ...], where: str, name: str) -> list[re.Pattern]:
    compiled = []
    for value in values:
        try:
            compiled.append(re.compile(value, re.IGNORECASE))
        except re.error as exc:
            raise _fail(where, f"`{name}` regex {value!r} does not compile: {exc}") from None
    return compiled


def _instance(record, facts: dict[str, Fact], seen: set[str]) -> Instance:
    if not isinstance(record, dict):
        raise _fail("instance", "must be an object")
    where = f"instance {record.get('id')!r}"
    missing = _REQUIRED_KEYS - record.keys()
    unknown = record.keys() - _REQUIRED_KEYS - _OPTIONAL_KEYS
    if missing:
        raise _fail(where, f"missing {', '.join(sorted(missing))}")
    if unknown:
        raise _fail(where, f"unknown field {', '.join(sorted(unknown))}")
    if not isinstance(record["id"], str) or not _ID.match(record["id"]):
        raise _fail(where, "`id` must be lowercase letters, digits and hyphens")
    if record["id"] in seen:
        raise _fail(where, "`id` is used twice")
    if record["class"] not in FACT_CLASSES or isinstance(record["class"], bool):
        raise _fail(where, f"`class` must be one of {', '.join(map(str, FACT_CLASSES))}")
    if record["wording"] not in WORDINGS:
        raise _fail(where, f"`wording` must be one of {', '.join(WORDINGS)}")
    if not isinstance(record["task"], str) or not record["task"].strip():
        raise _fail(where, "`task` must be a non-empty string")
    required = _strings(record["required"], where, "required", empty_ok=False)
    stale = _strings(record["stale"], where, "stale", empty_ok=True)
    keys = _strings(record["keys"], where, "keys", empty_ok=False)
    files = _strings(record.get("files", []), where, "files", empty_ok=True)
    repo = record.get("repo", "")
    if not isinstance(repo, str):
        raise _fail(where, "`repo` must be a string")
    instance = Instance(record["id"], record["class"], record["wording"], record["task"].strip(),
                        required, stale, keys, repo, files)
    check_key(instance, facts)
    return instance


def check_key(instance: Instance, facts: dict[str, Fact]) -> None:
    """Its facts exist in its class; required regexes cover every keyed value and none matches the task
    text; stale regexes match stale values only and, together, every stale value of the keys."""
    where = f"instance {instance.id!r}"
    keyed = []
    for key in instance.keys:
        if key not in facts:
            raise _fail(where, f"key {key!r} is not in the facts file")
        if facts[key].scenario_class != instance.scenario_class:
            raise _fail(where, f"key {key!r} is a class {facts[key].scenario_class} fact")
        keyed.append(facts[key])
    required = _patterns(instance.required, where, "required")
    stale = _patterns(instance.stale, where, "stale")
    values = [fact.value for fact in keyed]
    stale_values = [value for fact in keyed for value in fact.stale_values]
    for pattern in required:
        if not any(pattern.search(value) for value in values):
            raise _fail(where, f"required regex {pattern.pattern!r} matches no keyed value")
    for fact in keyed:
        if not any(pattern.search(fact.value) for pattern in required):
            raise _fail(where, f"no required regex matches the value of key {fact.id!r}")
    for pattern in required:
        if pattern.search(instance.task):
            raise _fail(where, f"required regex {pattern.pattern!r} matches the task text, so the task gives the answer")
    for pattern in stale:
        if any(pattern.search(value) for value in values):
            raise _fail(where, f"stale regex {pattern.pattern!r} matches a current value")
        if not any(pattern.search(value) for value in stale_values):
            raise _fail(where, f"stale regex {pattern.pattern!r} matches no stale value of its keys")
    uncovered = [value for value in stale_values if not any(pattern.search(value) for pattern in stale)]
    if uncovered:
        shown = ", ".join(repr(v) for v in uncovered[:5]) + (f" and {len(uncovered) - 5} more" if len(uncovered) > 5 else "")
        raise _fail(where, f"the stale regexes miss {len(uncovered)} stale value(s) of its keys: {shown}")


def parse(payload, facts: dict[str, Fact]) -> InstanceSet:
    """An instance set from decoded JSON; any breach of the format raises `InstanceError`."""
    if not isinstance(payload, dict) or payload.get("format") != INSTANCES_FORMAT:
        raise InstanceError(f"not a {INSTANCES_FORMAT} file")
    unknown = payload.keys() - _TOP_KEYS
    if unknown:
        raise InstanceError(f"unknown top-level field {', '.join(sorted(unknown))}")
    if not isinstance(payload.get("provisional"), bool):
        raise InstanceError("`provisional` must be true or false")
    if not isinstance(payload.get("author", ""), str):
        raise InstanceError("`author` must be a string")
    records = payload.get("instances")
    if not isinstance(records, list) or not records:
        raise InstanceError("`instances` must be a non-empty list")
    seen: set[str] = set()
    instances = []
    for record in records:
        instance = _instance(record, facts, seen)
        seen.add(instance.id)
        instances.append(instance)
    return InstanceSet(payload["provisional"], payload.get("author", ""), tuple(instances))


def rejected(payload, facts: dict[str, Fact]) -> list[tuple[str, str]]:
    """Every instance a rule refuses, each judged on its own, as `(id, reason)`; empty when all pass."""
    found = []
    seen: set[str] = set()
    for record in payload.get("instances", []) if isinstance(payload, dict) else []:
        try:
            seen.add(_instance(record, facts, seen).id)
        except InstanceError as exc:
            found.append((str(record.get("id")) if isinstance(record, dict) else "?", str(exc)))
    return found


def load(path: Path, facts: dict[str, Fact]) -> InstanceSet:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstanceError(f"{path.name} is not JSON: {exc}") from None
    return parse(payload, facts)


def to_task(instance: Instance, facts: dict[str, Fact]) -> Task:
    """The harness task for one instance: checkout, store and graded keys come from its facts."""
    keyed = [facts[key] for key in instance.keys]
    first = keyed[0]
    return Task(
        id=instance.id,
        scenario_class=instance.scenario_class,
        class_name=CLASS_NAMES[instance.scenario_class],
        description=instance.task,
        repo=instance.repo or first.repo,
        files=instance.files,
        expected_winner="pre-registered rules decide (contracts/e2e.md, Layer 2)",
        without_history="answers without the recorded fact",
        decision_found=f"Follow the recorded {first.topic}.",
        decision_naive="Decide from the task description alone.",
        answer_docs=tuple(fact.doc for fact in keyed),
        answer_role=first.section,
        store=first.store,
        required=instance.required,
        stale_patterns=instance.stale,
        keys=instance.keys,
        wording=instance.wording,
    )


def tasks_of(instance_set: InstanceSet, facts: dict[str, Fact]) -> list[Task]:
    """Every instance in file order: none is ever dropped or chosen by its outcome."""
    return [to_task(instance, facts) for instance in instance_set.instances]


def facts_by_id(facts: list[Fact]) -> dict[str, Fact]:
    return {fact.id: fact for fact in facts}


def answerable_problems(instance_set: InstanceSet, facts: dict[str, Fact], generated) -> list[str]:
    """A keyed value not on one line of its source (the answer document, or the covered file at
    HEAD), or a required regex that matches nothing there: why an instance is not answerable."""
    problems = []
    for instance in instance_set.instances:
        for key in instance.keys:
            fact = facts[key]
            source = _source_text(fact, generated)
            if source is None:
                problems.append(f"{instance.id}: the source of key {key!r} is missing")
                continue
            if not any(fact.value in line for line in source.splitlines()):
                problems.append(f"{instance.id}: the value of key {key!r} is not on one line of its source")
        sources = "\n".join(_source_text(facts[key], generated) or "" for key in instance.keys)
        for pattern in instance.required:
            if not re.search(pattern, sources, re.IGNORECASE):
                problems.append(f"{instance.id}: required regex {pattern!r} matches nothing in the sources")
    return problems


def _source_text(fact: Fact, generated) -> str | None:
    if fact.source_file:
        files = dict(generated.histories[fact.repo][-1].files)
        return files.get(fact.source_file)
    path = generated.stores[fact.store] / f"{fact.doc}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def default_file() -> Path:
    """The blind author's file once it exists, else the provisional one."""
    return BLIND_FILE if BLIND_FILE.is_file() else PROVISIONAL_FILE


def stale_leaks(instance_set: InstanceSet, facts: dict[str, Fact], generated) -> list[tuple[str, str]]:
    """Every stale regex that matches a line of a document other than its keys' own documents and their
    `stale_docs`: `(instance id, reason naming the regex, the document and the line)`."""
    found = []
    for instance in instance_set.instances:
        keyed = [facts[key] for key in instance.keys]
        allowed = {fact.doc for fact in keyed} | {doc for fact in keyed for doc in fact.stale_docs}
        stores = {fact.store for fact in keyed}
        for pattern in instance.stale:
            compiled = re.compile(pattern, re.IGNORECASE)
            for store in sorted(stores):
                for path in sorted(generated.stores[store].glob("*.md")):
                    if path.stem in allowed:
                        continue
                    line = next((ln for ln in path.read_text(encoding="utf-8").splitlines() if compiled.search(ln)), None)
                    if line is not None:
                        found.append((instance.id, f"stale regex {pattern!r} matches {path.stem}: {line.strip()}"))
    return found
