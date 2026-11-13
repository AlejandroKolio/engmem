"""Daily mode pays only for the product, and research mode keeps the protocol byte for byte
(contracts/prompt-templates.md, "Daily mode pays only for the product")."""

from __future__ import annotations

import re

import pytest

from support import rpc, templates
from support.templates import MARKER
from support.docs import ACTIVE_CONTENT
from support.paths import REPO_ROOT

from engmem import gate1, gate1_audit, mcp_tools
from engmem.cli import main
from engmem.settings import Mode
from engmem.template_render import render

GOLDEN = REPO_ROOT / "tests" / "fixtures" / "research-renders"
START, SAVE, QUICK = "engmem.start.md", "engmem.save.md", "engmem.save.quick.md"
SOURCES = (START, SAVE, QUICK)
CHANNELS = {"files": False, "mcp": True}
GIT_FACTS = "git rev-parse --show-toplevel HEAD --abbrev-ref HEAD; git config user.name"
TARGET_BYTES = {(START, "files"): 4500, (START, "mcp"): 4000, (QUICK, "files"): 6500, (SAVE, "files"): 11000}


def _daily(name: str, channel: str = "files") -> str:
    return render(templates.source(name), mcp=CHANNELS[channel], mode=Mode.DAILY)


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("channel", sorted(CHANNELS))
@pytest.mark.parametrize("name", SOURCES)
def test_every_research_render_is_byte_for_byte_the_one_before_the_daily_split(name, channel):
    golden = (GOLDEN / f"{name.removesuffix('.md')}.{channel}.md").read_bytes()
    rendered = render(templates.source(name), mcp=CHANNELS[channel], mode=Mode.RESEARCH)

    assert rendered.encode("utf-8") == golden.replace(b"\r\n", b"\n")


@pytest.mark.parametrize("name, channel", sorted(TARGET_BYTES))
def test_a_daily_render_stays_within_its_target(name, channel):
    size = len(_daily(name, channel).replace("\r\n", "\n").encode("utf-8"))

    assert size <= TARGET_BYTES[name, channel] - 125, f"{size} bytes before the stamp and skill name"


@pytest.mark.parametrize("channel", sorted(CHANNELS))
def test_the_daily_start_writes_nothing_and_passes_no_session_id(channel):
    text = _daily(START, channel)

    for absent in ("engmem draft create", "engmem_create_draft", "--session", "session_id",
                   "engmem store show", "## Search Trace", "\n## 1. Pre-reg", "answer_steps",
                   "record-baseline"):
        assert absent not in text, absent
    assert "no draft, no Pre-reg, and no session id" in _flat(text)


@pytest.mark.parametrize("channel", sorted(CHANNELS))
def test_the_daily_start_keeps_the_search_budget_the_reads_by_role_and_the_answer(channel):
    text = _flat(_daily(START, channel))

    assert text.index("## Scope") < text.index("## 0. Mode") < text.index("## Daily mode")
    assert "Two searches at most" in text and "at most 5 documents in all" in text
    assert "--role primer" in text and "`engmem_read` with `id` and `role`" in text
    assert "paste the output back" in text
    assert "Keep the `cite as` line of each search and read" in text
    assert "`prior context: none found`" in text and "in the same message" in text


@pytest.mark.parametrize("channel", sorted(CHANNELS))
def test_the_daily_start_reads_the_section_the_primer_points_to_or_the_task_asks_about(channel):
    text = _flat(_daily(START, channel))
    read = text[text.index("2. **Read.**"):text.index("3. **Report")]

    assert read.index("`--role decisions`") < read.index("the section the primer points to or the task asks about")
    assert "Lessons Learned (`lessons`) for pitfalls, failures, causes and lessons" in read
    assert "Production Considerations (`production`) for production settings, limits and config keys" in read
    assert "at most 5 documents in all" in read


@pytest.mark.parametrize("name", [SAVE, QUICK])
@pytest.mark.parametrize("channel", sorted(CHANNELS))
def test_a_daily_save_writes_none_of_what_only_the_experiment_reads(name, channel):
    text = _daily(name, channel)

    for absent in ("## Reuse Log", "## Search Trace", "## Pre-reg", "date -u", "feedback summary",
                   "engmem cost summary", "navigation_miss", "engmem_record_feedback",
                   "baseline_tokens", "prior-doc"):
        assert absent not in text, absent


@pytest.mark.parametrize("name", [SAVE, QUICK])
def test_a_daily_save_creates_then_completes_after_one_git_call(name):
    text = _flat(_daily(name))

    assert text.count(GIT_FACTS) == 1
    create = text.index("engmem draft create <id>")
    assert create < text.index("engmem draft complete <id> --expected-version")
    assert text.index("engmem_create_draft") < text.index("engmem_complete_draft")
    assert "`status: draft`" in text and "mode: daily" in text
    assert "<<'EOF'" in text and "quoted delimiter" in text
    assert "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)" in text
    assert "`@'` … `'@ |" in text
    assert "`sessions/<id>.md` yourself" in text or "never as a plain file write" in text
    assert "`related`" in text and "prior documents this work used" in text


def test_the_daily_quick_save_publishes_only_after_the_one_confirmation():
    text = _flat(_daily(QUICK))

    preview = text.index("**The preview.**")
    assert preview < text.index("**Publish**")
    assert "Declined, or a reply you cannot read as either: publish nothing" in text
    assert "show it again and publish only after a new confirmation" in text


def test_the_daily_full_save_skips_16_and_supersedes_by_the_kept_cite_as_version():
    text = _flat(_daily(SAVE))

    assert "## 16. Reuse Log" not in text and "## 17. Status at a Glance" in text
    assert "Write every section below but 16" in text
    assert "in daily mode, the `<version>` the `cite as` line you kept gives." in text
    assert "usefulness" in text and "ask no usefulness question" in text


def test_a_daily_start_leaves_nothing_the_research_audit_reads(fixture_store, capsys):
    """Searches and reads with no session id: no attributed row, no orphan, no draft."""
    before = gate1_audit.evaluate(fixture_store, gate1.evaluate(fixture_store))

    assert main(["search", "widget cache", "--store", str(fixture_store)]) == 0
    assert main(["read", "1000001-response-cache", "--role", "primer",
                 "--store", str(fixture_store)]) == 0
    rpc.call(fixture_store, name=mcp_tools.TOOL_NAME, arguments={"query": "widget cache"})
    capsys.readouterr()
    after = gate1_audit.evaluate(fixture_store, gate1.evaluate(fixture_store))

    assert after.telemetry_rows_with_session == before.telemetry_rows_with_session == 0
    assert after.orphan_session_ids == [] and after.orphan_row_count == 0
    assert after.unreconstructable_docs == before.unreconstructable_docs
    assert after.draft_count == before.draft_count


def test_a_daily_save_document_completes_without_a_reuse_log(store):
    """What the daily save writes is accepted as is: no Reuse Log refusal, no citation note."""
    content = ACTIVE_CONTENT.replace("## Reuse Log\n\nPrior docs used: none.\n\n", "")
    front = content[: content.index("\n---\n", 4) + 5].replace("status: active", "status: draft")
    doc_id = next(line for line in front.splitlines() if line.startswith("id: ")).removeprefix("id: ")

    created, is_error = rpc.call(
        store, name=mcp_tools.CREATE_DRAFT_TOOL_NAME, arguments={"id": doc_id, "content": front}
    )
    assert not is_error, rpc.text(created)
    version = rpc.text(created).split("version: ", 1)[1][:16]
    completed, is_error = rpc.call(
        store, name=mcp_tools.COMPLETE_DRAFT_TOOL_NAME,
        arguments={"id": doc_id, "content": content, "expected_version": version},
    )

    assert not is_error, rpc.text(completed)
    assert rpc.text(completed) == f"completed sessions/{doc_id}.md (status: draft -> active)"


FENCES = ("ends here", "research mode's", "reads no clock")
STEP = re.compile(r"^(\d+\. |#+ |\*\*)")


def _regions(source: str) -> list[tuple[str, str, str]]:
    """The source's `daily` and `research` regions in order: kind, text, and the text outside any
    branch that comes before it."""
    regions, current, lines, between = [], None, [], []
    for line in source.splitlines(keepends=True):
        marker = MARKER.match(line.rstrip("\r\n"))
        if marker is None:
            (lines if current is not None else between).append(line)
            continue
        name = line.split(": ", 1)[1].split(" ", 1)[0]
        if name != "end":
            current = name
            continue
        if current in ("daily", "research"):
            regions.append((current, "".join(lines), "".join(between)))
            between = []
        current, lines = None, []
    return regions


def _steps(text: str) -> list[str]:
    outside_examples = re.sub(r"^````markdown\n.*?^````$", "", text, flags=re.MULTILINE | re.DOTALL)
    return [line for line in outside_examples.splitlines() if STEP.match(line.strip())]


@pytest.mark.parametrize("name", SOURCES)
def test_a_mode_blind_copy_fences_every_research_step_a_daily_reader_would_meet(name):
    """A daily agent reading a project copy meets research steps only after its own path ends, or
    right before the daily text that replaces them."""
    regions = _regions(templates.source(name))
    unfenced = []
    for index, (kind, text, _) in enumerate(regions):
        if kind != "research" or not _steps(text):
            continue
        before = [t for k, t, _ in regions[:index] if k == "daily"]
        replaced = (
            index + 1 < len(regions) and regions[index + 1][0] == "daily"
            and not regions[index + 1][2].strip()
        )
        if before and not replaced and not any(f in _flat(before[-1]) for f in FENCES):
            unfenced.append(_steps(text)[0])
    assert unfenced == []


@pytest.mark.parametrize("name", [SAVE, QUICK])
def test_a_daily_save_says_how_a_failed_completion_is_finished_later(name):
    text = _flat(_daily(name))

    assert "If complete fails after create" in text and "stays" in text
    assert "finishes it later" in text and "`engmem_complete_draft`" in text


@pytest.mark.parametrize("name", [START, SAVE, QUICK])
def test_daily_related_entries_are_ids_without_the_version(name):
    assert "(the part before `@`)" in _flat(_daily(name)) or "(before `@`)" in _flat(_daily(name))


def test_the_daily_full_save_leaves_out_capture_minutes_and_the_reuse_log_row_version():
    text = _flat(_daily(SAVE))

    assert "leave out `capture_minutes`" in text
    assert "row in your Reuse Log" not in text


def test_the_daily_pointer_names_the_pre_reg_section_a_daily_copy_lacks():
    assert "a copy with no `## 1. Pre-reg` section" in _flat(_daily(START))
