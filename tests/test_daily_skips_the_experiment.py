"""A daily session skips what only the experiment needs: the Reuse Log refusals and citation notes
at completion, the Reuse Log shape at create, and the unattributed note on a search or a read
(contracts/mcp-server.md, "Daily sessions skip both tiers")."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from support import rpc
from support.docs import ACTIVE_CONTENT, DRAFT_CONTENT

from engmem import drafts, gate1, settings
from engmem.cli import main
from engmem.read_report import UNATTRIBUTED_READ_NOTES
from engmem.settings import Mode
from engmem.telemetry import UNATTRIBUTED_CLI_NOTE, UNATTRIBUTED_MCP_NOTE

DRAFT_ID = "20260101-widget-cache"
BAD_ROW = "| 20250101-gone | no quoted span here | it helped | maybe |"
BAD_REUSE_LOG = (
    "| prior-doc | taken | how it influenced the work | classification |\n"
    "|---|---|---|---|\n"
    f"{BAD_ROW}"
)
WITHOUT_REUSE_LOG = ACTIVE_CONTENT.replace("## Reuse Log\n\nPrior docs used: none.\n\n", "")


def _in_mode(text: str, mode: str) -> str:
    return text.replace("mode: daily", f"mode: {mode}")


@pytest.fixture(params=["daily", "research"])
def mode(request) -> str:
    settings.save_mode(Mode(request.param))
    return request.param


def _create(store: Path, mode: str) -> tuple[str, str]:
    message = drafts.create_draft(store, DRAFT_ID, _in_mode(DRAFT_CONTENT, mode), drafts.CLI)
    return message, re.search(r"version: ([0-9a-f]{16})", message).group(1)


def _complete(store: Path, mode: str, content: str) -> str:
    _, version = _create(store, mode)
    return drafts.complete_draft(store, DRAFT_ID, _in_mode(content, mode), version, drafts.CLI)


def test_only_a_research_create_result_names_the_reuse_log_shape(store, mode):
    message, _ = _create(store, mode)

    assert (drafts.REUSE_LOG_SHAPE in message) is (mode == "research")
    assert message.endswith(drafts.REUSE_LOG_SHAPE if mode == "research" else "experiment.")


def test_a_daily_completion_publishes_a_reuse_log_research_refuses(store, mode):
    content = ACTIVE_CONTENT.replace("Prior docs used: none.", BAD_REUSE_LOG)

    if mode == "research":
        with pytest.raises(drafts.WriteRefused, match="Reuse Log row rejected"):
            _complete(store, mode, content)
        return
    message = _complete(store, mode, content)

    assert message == f"completed sessions/{DRAFT_ID}.md (status: draft -> active)"
    assert BAD_ROW in (store / "sessions" / f"{DRAFT_ID}.md").read_text(encoding="utf-8")


def test_a_daily_completion_never_runs_the_store_wide_citation_scan(store, mode, monkeypatch):
    scans: list[Path] = []
    real = gate1.evaluate
    monkeypatch.setattr(gate1, "evaluate", lambda s: scans.append(s) or real(s))

    _complete(store, mode, ACTIVE_CONTENT)

    assert scans == ([] if mode == "daily" else [store])


def test_a_daily_completion_without_a_reuse_log_is_not_warned(store, mode):
    message = _complete(store, mode, WITHOUT_REUSE_LOG)

    assert ("has no Reuse Log section" in message) is (mode == "research")


def test_a_daily_document_from_an_older_template_keeps_its_reuse_log(store):
    message = _complete(store, "daily", ACTIVE_CONTENT)

    assert message == f"completed sessions/{DRAFT_ID}.md (status: draft -> active)"
    assert (store / "sessions" / f"{DRAFT_ID}.md").read_text(encoding="utf-8") == ACTIVE_CONTENT


@pytest.mark.parametrize(
    ("content", "refusal"),
    [
        pytest.param("---\nid: [unclosed\n---\n", "does not parse", id="malformed"),
        pytest.param(ACTIVE_CONTENT.replace(f"id: {DRAFT_ID}", "id: other"), "does not match", id="id"),
        pytest.param(ACTIVE_CONTENT.replace("mode: daily", "mode: research"), "keeps the mode", id="mode"),
    ],
)
def test_a_daily_completion_still_refuses_what_protects_the_data(store, content, refusal):
    _, version = _create(store, "daily")

    with pytest.raises(drafts.WriteRefused, match=refusal):
        drafts.complete_draft(store, DRAFT_ID, content, version, drafts.CLI)
    with pytest.raises(drafts.WriteRefused, match="changed since you read it"):
        drafts.complete_draft(store, DRAFT_ID, ACTIVE_CONTENT, "0" * 16, drafts.CLI)


def _cli(capsys, fixture_store: Path, *args: str) -> str:
    assert main([*args, "--store", str(fixture_store)]) == 0
    return capsys.readouterr().out.rstrip("\n")


@pytest.mark.parametrize(
    ("command", "note"),
    [
        pytest.param(("search", "1000001"), UNATTRIBUTED_CLI_NOTE, id="search"),
        pytest.param(("search", "1000001", "--role", "decisions"), UNATTRIBUTED_CLI_NOTE, id="role"),
        pytest.param(("read", "1000001-response-cache"), UNATTRIBUTED_READ_NOTES["cli"], id="read"),
    ],
)
def test_only_a_research_search_or_read_ends_with_the_unattributed_note(
    fixture_store, capsys, mode, command, note
):
    out = _cli(capsys, fixture_store, *command)

    assert (out.splitlines()[-1] == note) is (mode == "research")
    assert (note in out) is (mode == "research")


def test_an_unattributed_mcp_search_in_daily_mode_ends_with_the_scoreboard(fixture_store, mode):
    result, is_error = rpc.call(fixture_store, name="engmem_search", arguments={"query": "1000001"})

    assert not is_error
    last = rpc.text(result).splitlines()[-1]
    assert (last == UNATTRIBUTED_MCP_NOTE) is (mode == "research")


def test_an_unreadable_mode_keeps_the_unattributed_note(fixture_store, capsys):
    settings.mode_setting_file().parent.mkdir(parents=True, exist_ok=True)
    settings.mode_setting_file().write_text("experiment\n", encoding="utf-8")

    out = _cli(capsys, fixture_store, "search", "1000001")

    assert out.splitlines()[-1] == UNATTRIBUTED_CLI_NOTE


@pytest.mark.parametrize(
    ("started", "switched_to"),
    [("research", "daily"), ("daily", "research")],
    ids=["research-draft-then-daily", "daily-draft-then-research"],
)
def test_the_draft_on_disk_decides_whether_completion_checks_the_reuse_log(
    store, monkeypatch, started, switched_to
):
    settings.save_mode(Mode(started))
    _, version = _create(store, started)
    settings.save_mode(Mode(switched_to))
    scans: list[Path] = []
    real = gate1.evaluate
    monkeypatch.setattr(gate1, "evaluate", lambda s: scans.append(s) or real(s))
    content = _in_mode(ACTIVE_CONTENT.replace("Prior docs used: none.", BAD_REUSE_LOG), started)

    if started == "research":
        with pytest.raises(drafts.WriteRefused, match="Reuse Log row rejected"):
            drafts.complete_draft(store, DRAFT_ID, content, version, drafts.CLI)
        drafts.complete_draft(store, DRAFT_ID, _in_mode(ACTIVE_CONTENT, started), version, drafts.CLI)
        assert scans == [store]
        return
    message = drafts.complete_draft(store, DRAFT_ID, content, version, drafts.CLI)

    assert message == f"completed sessions/{DRAFT_ID}.md (status: draft -> active)"
    assert scans == []
