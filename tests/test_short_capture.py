"""US-07: `/engmem.save.quick` saves the useful core of a decision as one valid, searchable
record, marks what the session did not have, and publishes nothing when the preview is declined."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import write_file
from mcp_harness import _call, _prompts_get_msg, _run, _telemetry_lines, _text

from engmem import gate1, gate1_audit, mcp_server
from engmem.cli import main
from engmem.install import CODEX_COMMAND_REFERENCE
from engmem.output import PRIMER_MAX_CHARS
from engmem.sections import split_sections
from engmem.spine import load_store, split_front_matter

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "src" / "engmem" / "templates" / "engmem.save.quick.md"
TRANSCRIPT = ROOT / "docs" / "design" / "acceptance" / "us-07-short-capture.md"

LABELS = ("Decision", "Reason", "Rejected alternative", "Source")
ABSENT = "not stated in the available material."
SHORT_RECORD_ROLES = {"prereg", "decisions", "primer", "reuse", "trace"}
EXAMPLE_ID = "20260101-export-retry"

UNRELATED = (
    "---\nid: 20251201-ledger-sync\ntitle: Ledger sync\ndate: 2025-12-01\n"
    "task_date: 2025-12-01\nstatus: active\nsuperseded_by:\nbackfilled: false\n"
    "tags: [ledger]\nentities: [LedgerSync]\nrelated: []\ncovers_files: []\n---\n\n"
    "## Decision Log\n\nLedger rows carry an idempotency token from the source system.\n\n"
    "## Cold-start primer\n\nLedgerSync copies rows nightly.\n"
)


def _example(text: str) -> str:
    """The one four-backtick `markdown` block: the record the template prescribes."""
    match = re.search(r"^````markdown\n(.*?)^````$", text, re.MULTILINE | re.DOTALL)
    assert match, "the template no longer shows an example short record"
    return match.group(1)


EXAMPLE = _example(TEMPLATE.read_text(encoding="utf-8"))

OTHER_ID = "20260102-ledger-batch"
OTHER_RECORD = (
    f"---\nid: {OTHER_ID}\ntitle: Ledger batches settle once a night\ndate: 2026-01-02\n"
    "task_date: 2026-01-02\nstatus: active\nsuperseded_by:\nbackfilled: false\n"
    "tags: [ledger]\nentities: [LedgerBatch]\nrelated: []\ncovers_files: []\n"
    "verified_at_commit:\ncapture_minutes:\n---\n\n"
    "## Pre-reg\n\nNaive baseline: settle each batch as it arrives.\n"
    "pre-reg source: self (no sub-agent available)\n\n"
    "## Decision Log\n\n"
    "- Decision: LedgerBatch settles once a night.\n"
    "- Reason: the clearing house accepts one file a day.\n"
    "- Rejected alternative: settling per batch.\n"
    "- Source: a ticket.\n\n"
    "## Future LLM Context (cold-start primer)\n\n"
    "LedgerBatch settles once a night.\nThe clearing house accepts one file a day.\n\n"
    "## Reuse Log\n\nPrior docs used: none.\n\n## Search Trace\n\nshell\n"
)


def _draft_of(record: str) -> str:
    """The draft `/engmem` would have left behind: the record's front matter as a draft, and
    its Pre-reg only."""
    front, body = split_front_matter(record)
    prereg = next(s for s in split_sections(body) if s.canonical == "prereg")
    front = front.replace("status: active", "status: draft")
    return f"---\n{front}---\n\n## Pre-reg\n\n{prereg.body}\n"


def _components(record: str) -> list[dict[str, str]]:
    """Each decision block of the Decision Log as `{label: value}`, in order."""
    _, body = split_front_matter(record)
    log = next(s for s in split_sections(body) if s.canonical == "decisions")
    blocks: list[dict[str, str]] = []
    for line in log.body.splitlines():
        match = re.fullmatch(r"- (Decision|Reason|Rejected alternative|Source): (.+)", line)
        if not match:
            continue
        if match.group(1) == "Decision":
            blocks.append({})
        assert blocks, f"a component line before any Decision line: {line}"
        assert match.group(1) not in blocks[-1], f"a component written twice: {line}"
        blocks[-1][match.group(1)] = match.group(2)
    return blocks


def _assert_is_a_short_record(record: str) -> None:
    _, body = split_front_matter(record)
    roles = {s.canonical for s in split_sections(body)}
    assert SHORT_RECORD_ROLES <= roles, f"missing sections: {SHORT_RECORD_ROLES - roles}"
    assert roles <= SHORT_RECORD_ROLES | {"lessons"}, f"sections beyond the short record: {roles}"
    blocks = _components(record)
    assert 1 <= len(blocks) <= 3
    for block in blocks:
        assert tuple(block) == LABELS, f"a decision block lacks a component: {block}"
    primer = next(s for s in split_sections(body) if s.canonical == "primer")
    assert len([line for line in primer.body.splitlines() if line.strip()]) >= 2


def _store(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    write_file(store / "sessions", "20251201-ledger-sync.md", UNRELATED)
    return store


def _create(store: Path, record: str) -> str:
    doc_id = _front_matter_id(record)
    result, is_error = _call(
        store, name=mcp_server.CREATE_DRAFT_TOOL_NAME,
        arguments={"id": doc_id, "content": _draft_of(record)},
    )
    assert not is_error, _text(result)
    return re.search(r"version: ([0-9a-f]+)\)", _text(result)).group(1)


def _complete(store: Path, record: str, version: str) -> str:
    result, is_error = _call(
        store, name=mcp_server.COMPLETE_DRAFT_TOOL_NAME,
        arguments={"id": _front_matter_id(record), "content": record, "expected_version": version},
    )
    assert not is_error, _text(result)
    return _text(result)


def _attributed_search(store: Path, doc_id: str, query: str) -> None:
    _, is_error = _call(
        store, name=mcp_server.TOOL_NAME, arguments={"query": query, "session_id": doc_id}
    )
    assert not is_error


def _front_matter_id(record: str) -> str:
    return re.search(r"^id: (\S+)$", record, re.MULTILINE).group(1)


def _cli_search(store: Path, query: str, capsys) -> str:
    assert main(["search", query, "--store", str(store)]) == 0
    return capsys.readouterr().out


def _mcp_search(store: Path, query: str, capsys) -> str:
    result, is_error = _call(store, name=mcp_server.TOOL_NAME, arguments={"query": query})
    assert not is_error
    return _text(result)


CHANNELS = [pytest.param(_cli_search, id="cli"), pytest.param(_mcp_search, id="mcp")]


def _with_absent(record: str, *labels: str) -> str:
    for label in labels:
        record = re.sub(rf"^- {label}: .+$", f"- {label}: {ABSENT}", record, flags=re.MULTILINE)
    return record


# --- AC-07.1: the template prescribes the five components and nothing else ---------------


def test_ac_07_1_the_prescribed_record_holds_the_five_components_and_no_other_section():
    _assert_is_a_short_record(EXAMPLE)


def test_ac_07_1_the_template_names_each_component_line_and_the_primer():
    text = TEMPLATE.read_text(encoding="utf-8")

    for label in LABELS:
        assert f"- {label}: <" in text, f"step 1 no longer shows the {label} line"
    assert "## Future LLM Context (cold-start primer)" in text
    assert (
        "Besides Pre-reg, Reuse Log and Search Trace (steps 3–5), which every save carries, no "
        "other section needs filling." in " ".join(text.split())
    )
    components = text[text.index("## The five components"):text.index("## Rules")]
    primer_step = text[text.index("2. Write `## Future LLM"):text.index("3. Write")]
    for part in (components, primer_step):
        assert f"about {PRIMER_MAX_CHARS} characters" in " ".join(part.split())


def test_ac_07_2_the_rules_fix_the_absence_marker_for_source_and_alternative():
    rules = TEMPLATE.read_text(encoding="utf-8").split("## Example")[0]

    assert f"- Source: {ABSENT}" in rules
    assert f"- Rejected alternative: {ABSENT}" in rules
    assert "is never dropped or left blank" in " ".join(rules.split())


def _installed_texts(agent: str, home: Path, project: Path, store: Path) -> str:
    assert main(["install", "--agent", agent, "--store", str(store)]) == 0
    paths = {
        "claude": home / ".claude" / "commands" / "engmem.save.quick.md",
        "copilot-ide": project / ".github" / "prompts" / "engmem.save.quick.prompt.md",
        "copilot-cli": home / ".copilot" / "skills" / "engmem-save-quick" / "SKILL.md",
        "codex": home / ".agents" / "skills" / "engmem-save-quick" / "SKILL.md",
    }
    return paths[agent].read_text(encoding="utf-8")


@pytest.mark.parametrize("agent", ["claude", "copilot-ide", "copilot-cli", "codex", "mcp"])
def test_every_agent_receives_the_short_capture_as_written(agent, env, tmp_path):
    """The install rewrites command names; none of them may reach the record or its rules."""
    home, project = env
    (project / ".git").mkdir()
    store = tmp_path / "store"
    if agent == "mcp":
        (store / "sessions").mkdir(parents=True)
        _, responses, _ = _run(store, _prompts_get_msg(5, name="engmem-save-quick"))
        text = responses[0]["result"]["messages"][0]["content"]["text"]
    else:
        text = _installed_texts(agent, home, project, store)

    assert _example(text) == EXAMPLE
    assert f"- Source: {ABSENT}" in text
    assert "publish nothing" in text
    assert not CODEX_COMMAND_REFERENCE.search(_example(text))


# --- AC-07.3: the record is valid to every validator and found on its decision ---------


def test_ac_07_3_the_prescribed_record_completes_with_no_warning_and_passes_the_audit(tmp_path):
    store = _store(tmp_path)
    version = _create(store, EXAMPLE)
    _attributed_search(store, EXAMPLE_ID, "ExportJob retry backoff")

    message = _complete(store, EXAMPLE, version)

    assert message == f"completed sessions/{EXAMPLE_ID}.md (status: draft -> active)"
    loaded = load_store(store / "sessions")
    assert not loaded.errors
    assert not [w for w in loaded.warnings if EXAMPLE_ID in w.message]
    assert [p.name for p in (store / "sessions").iterdir() if EXAMPLE_ID in p.name] == [
        f"{EXAMPLE_ID}.md"
    ]
    verdicts = gate1.evaluate(store)
    assert EXAMPLE_ID in verdicts.none_reports and not verdicts.conflicts
    audit = gate1_audit.evaluate(store, verdicts)
    assert EXAMPLE_ID not in audit.active_missing_reuse + audit.active_missing_trace
    assert EXAMPLE_ID not in audit.no_prereg_docs + audit.unreconstructable_docs
    assert not audit.orphan_session_ids


@pytest.mark.parametrize("run", CHANNELS)
@pytest.mark.parametrize("query", ["fixed backoff", "downstream store rejects bursts"])
def test_ac_07_3_the_record_is_found_on_its_decision_and_reason(tmp_path, run, query, capsys):
    store = _store(tmp_path)
    _complete(store, EXAMPLE, _create(store, EXAMPLE))

    out = run(store, query, capsys)

    assert out.splitlines()[0].startswith(f"### {EXAMPLE_ID} ")
    assert "ExportJob retries a failed batch three times, 30 seconds apart" in out


# --- AC-07.2: absence is marked, accepted, and adds no match of its own ------------------


def test_ac_07_2_a_record_marking_both_absences_completes_and_is_found(tmp_path, capsys):
    record = _with_absent(EXAMPLE, "Rejected alternative", "Source")
    store = _store(tmp_path)

    message = _complete(store, record, _create(store, record))

    assert "warning" not in message
    assert _components(record)[0]["Source"] == ABSENT
    assert _cli_search(store, "fixed backoff", capsys).startswith(f"### {EXAMPLE_ID} ")


def test_ac_07_2_marker_and_label_words_do_not_outrank_the_decision(tmp_path, capsys):
    """Every short record shares the labels, and every record lacking a source the marker; a
    query mixing them with one record's decision terms must still rank that record first."""
    store = _store(tmp_path)
    first = _with_absent(EXAMPLE, "Source")
    second = _with_absent(OTHER_RECORD, "Rejected alternative", "Source")
    for record in (first, second):
        _complete(store, record, _create(store, record))

    headings = [
        line for line in _cli_search(store, "fixed backoff reason material", capsys).splitlines()
        if line.startswith("### ")
    ]

    assert headings[0].startswith(f"### {EXAMPLE_ID} ")
    assert any(h.startswith(f"### {OTHER_ID} ") for h in headings), (
        "the shared words do match the other record; the test is about the order"
    )


# --- AC-07.4: a declined preview publishes nothing ---------------------------------------


@pytest.mark.parametrize("run", CHANNELS)
def test_ac_07_4_a_declined_preview_leaves_only_the_draft_outside_search(tmp_path, run, capsys):
    store = _store(tmp_path)
    _create(store, EXAMPLE)
    _attributed_search(store, EXAMPLE_ID, "ExportJob retry backoff")

    loaded = load_store(store / "sessions")
    assert {d.id: d.status for d in loaded.docs}[EXAMPLE_ID] == "draft"
    assert not [d for d in loaded.docs if d.status == "active" and d.id == EXAMPLE_ID]
    for query in ("fixed backoff", "wrap the export call in a retry loop"):
        assert EXAMPLE_ID not in run(store, query, capsys)
    audit = gate1_audit.evaluate(store, gate1.evaluate(store))
    assert not audit.orphan_session_ids, "the draft is what this session's searches point at"


def test_the_template_keeps_the_draft_on_decline_and_does_not_call_complete():
    text = TEMPLATE.read_text(encoding="utf-8")
    declined = text[text.index("**Declined**"):text.index("To publish,")]

    assert "publish nothing" in declined
    assert "Do not call `engmem_complete_draft`" in declined
    assert "stays as it is, `status: draft`" in declined


# --- the acceptance transcript ----------------------------------------------------------


@dataclass
class _Block:
    kind: str
    text: str


def _transcript_blocks() -> list[_Block]:
    text = TRANSCRIPT.read_text(encoding="utf-8")
    return [
        _Block(kind=m.group(1).strip(), text=m.group(2))
        for m in re.finditer(r"^```([^\n`]+)\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    ]


def _expected_lines_hold(expected: str, actual: str, store: Path) -> None:
    actual_lines = [line.strip() for line in actual.splitlines()]
    position = 0
    for wanted in expected.replace("<store>", str(store)).splitlines():
        wanted = wanted.strip()
        prefix = wanted.removesuffix(" …")
        found = next(
            (
                i for i in range(position, len(actual_lines))
                if actual_lines[i] == wanted
                or (wanted.endswith(" …") and actual_lines[i].startswith(prefix))
            ),
            None,
        )
        assert found is not None, f"expected line {wanted!r} not in order in:\n{actual}"
        position = found + 1


def _substitute(value, latest: dict[str, str]):
    if isinstance(value, dict):
        return {k: _substitute(v, latest) for k, v in value.items()}
    if isinstance(value, str) and value.startswith("@"):
        return latest[value[1:]]
    return value


def _answer_text(response: dict) -> str:
    result = response["result"]
    if "messages" in result:
        return result["messages"][0]["content"]["text"]
    assert not result.get("isError"), _text(result)
    return _text(result)


def _replay(blocks: list[_Block], store: Path) -> list[str]:
    """Sends every call to the server and checks the agent's side of the script; returns the
    ids it published."""
    latest: dict[str, str] = {}
    evidence: list[str] = []
    published: list[str] = []
    shown: str | None = None
    confirmed: str | None = None

    for index, block in enumerate(blocks):
        if block.kind in ("markdown draft", "markdown preview"):
            latest[block.kind.split()[1]] = block.text
        if block.kind == "markdown preview":
            _assert_is_a_short_record(block.text)
            for component in _components(block.text):
                source = component["Source"]
                assert source == ABSENT or any(source in text for text in evidence), (
                    f"the Source {source!r} appears nowhere earlier in the session"
                )
            shown = block.text
        elif block.kind.startswith("text user"):
            assert confirmed is None, "the user was asked again after confirming the preview"
            evidence.append(block.text)
            if shown is not None:
                assert block.kind in ("text user confirm", "text user decline"), (
                    "the user's reply to a preview must be marked confirm or decline"
                )
                confirmed = shown if block.kind == "text user confirm" else None
                shown = None
        elif block.kind == "text agent" and shown is not None:
            assert "?" not in block.text or block.text.strip() == "Save it?", (
                "the agent asked something besides the one confirmation"
            )
        elif block.kind == "json call":
            message = _substitute(json.loads(block.text), latest)
            if message["params"].get("name") == mcp_server.COMPLETE_DRAFT_TOOL_NAME:
                assert confirmed is not None, "published a preview the user did not confirm"
                assert message["params"]["arguments"]["content"] == confirmed
                published.append(message["params"]["arguments"]["id"])
                confirmed = None
            _, responses, _ = _run(store, {"jsonrpc": "2.0", "id": index, **message})
            answer = _answer_text(responses[0])
            evidence.append(answer)
            expected = blocks[index + 1]
            assert expected.kind == "text result", f"call #{index} has no expected result"
            _expected_lines_hold(expected.text, answer, store)

    assert confirmed is None, "a confirmed preview was never published"
    return published


def _fresh_store(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    return store


def test_the_acceptance_transcript_replays_against_a_fresh_store(tmp_path):
    store = _fresh_store(tmp_path)

    published = _replay(_transcript_blocks(), store)

    statuses = {d.id: d.status for d in load_store(store / "sessions").docs}
    assert statuses == {
        "20260101-report-archive-compression": "active",
        "20260102-billing-cutoff-timezone": "draft",
    }
    assert published == ["20260101-report-archive-compression"]
    audit = gate1_audit.evaluate(store, gate1.evaluate(store))
    assert not (audit.active_missing_reuse or audit.active_missing_trace)
    assert not (audit.unreconstructable_docs or audit.orphan_session_ids)
    assert {row.get("session_id") for row in _telemetry_lines(store)} >= set(statuses)


def test_a_source_only_the_agent_has_said_is_an_invention(tmp_path):
    """The agent's own earlier turn is not evidence: the Source must come from the user or a
    tool."""
    blocks = _transcript_blocks()
    user_turn = next(b for b in blocks if "It is in PR #41." in b.text)
    user_turn.text = user_turn.text.replace(" It is in PR #41.", "")
    assert any("PR #41" in b.text for b in blocks if b.kind == "text agent")

    with pytest.raises(AssertionError, match="appears nowhere earlier in the session"):
        _replay(blocks, _fresh_store(tmp_path))


def test_the_transcript_runs_every_path_it_claims():
    kinds = [b.kind for b in _transcript_blocks()]

    assert kinds.count("text user confirm") == 1 and kinds.count("text user decline") == 1
    assert kinds.count("markdown preview") == 2

