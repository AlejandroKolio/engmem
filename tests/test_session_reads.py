"""US-27: the save starts from what the session read through engmem -- `feedback summary
--session` marks it, and a none Reuse Log in such a session completes with a warning naming it."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from support import rpc
from support.docs import piped_stdin, write_file
from support.paths import REPO_ROOT

from engmem import mcp_tools
from engmem.cli import main
from engmem.spine import load_store
from engmem.staging import version_of

SESSION = "20260901-task"
TEMPLATES = REPO_ROOT / "src" / "engmem" / "templates"
RELAY = "tell the user, and do not edit the published file."
WARNING = "warning: Reuse Log says none, but this session read: "
NONE_LOG = "Prior docs used: none."
ROW_LOG = (
    "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
    '| widget-cache | WidgetCache: "A decision about widget-cache." | kept it | reuse |'
)


def _text(doc_id: str, *, status: str = "active", reuse_log: str | None = None) -> str:
    body = f"## Decision Log\n\nA decision about {doc_id}.\n"
    if reuse_log is not None:
        body += f"\n## Reuse Log\n\n{reuse_log}\n"
    return (
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-08-01\ntask_date: 2026-08-01\n"
        f"status: {status}\nsuperseded_by:\nbackfilled: false\ntags: [platform]\n"
        "entities: [WidgetCache]\nrelated: []\ncovers_files: []\nrepos: [platform-core]\n"
        f"verified_at:\ncapture_minutes:\nmode: daily\n---\n\n{body}"
    )


def _doc(sessions: Path, doc_id: str, *, status: str = "active") -> None:
    write_file(sessions, f"{doc_id}.md", _text(doc_id, status=status))


def _log(path: Path, *rows: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _read_row(session_id: str, doc_id: object) -> dict:
    return {
        "ts": "2026-09-01T10:00:00+00:00", "engmem_version": "0", "op_id": uuid.uuid4().hex,
        "op": "read", "session_id": session_id, "channel": "cli", "bytes": 10,
        "doc_id": doc_id, "role": None,
    }


def _search_row(session_id: str, surfaced: list[str]) -> dict:
    return {
        "ts": "2026-09-01T10:00:00+00:00", "query": "q", "session_id": session_id,
        "channel": "cli", "n_docs": 5, "hits": [{"id": i, "score": 1.0} for i in surfaced],
        "surfaced": surfaced, "result": "hit", "context_bytes": 10,
        "context_tokens_estimate": 3, "scope": None,
    }


@pytest.fixture
def store(tmp_path) -> Path:
    """A, B, C active, D a draft, S the session draft; S's searches showed A and B."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for doc_id in ("widget-cache", "logo-policy", "retry-backoff"):
        _doc(sessions, doc_id)
    _doc(sessions, "draft-notes", status="draft")
    _doc(sessions, SESSION, status="draft")
    _log(tmp_path / "telemetry.jsonl", _search_row(SESSION, ["widget-cache", "logo-policy"]))
    return tmp_path


def _reads(store: Path) -> None:
    """S reads A, itself, the draft D and C, against sorted order; another session reads B."""
    _log(
        store / "cost.jsonl",
        *(_read_row(SESSION, d) for d in ("widget-cache", SESSION, "draft-notes", "retry-backoff")),
        _read_row("20260902-other", "logo-policy"),
    )


def _cli(capsys, *args: str) -> tuple[int, str]:
    code = main([*args])
    out, err = capsys.readouterr()
    return code, out + err


def _summary(capsys, store: Path, session_id: str = SESSION) -> tuple[int, str]:
    return _cli(capsys, "feedback", "summary", "--session", session_id, "--store", str(store))


def test_the_summary_marks_what_the_session_read(store, capsys):
    _reads(store)

    code, out = _summary(capsys, store)

    assert code == 0, out
    lines = out.splitlines()
    assert f"  unknown: {SESSION} -> widget-cache [read]" in lines
    assert f"  unknown: {SESSION} -> logo-policy" in lines
    assert [line for line in lines if line.startswith("read, not surfaced:")] == [
        "read, not surfaced: retry-backoff -- read through engmem but shown by no search; "
        "not a find, so it cannot be assessed"
    ]


def test_an_assessed_find_the_session_read_is_marked_too(store, capsys):
    _reads(store)
    code, out = _cli(
        capsys, "feedback", "record", SESSION, "widget-cache", "helped", "--store", str(store)
    )
    assert code == 0, out

    code, out = _summary(capsys, store)

    assert code == 0, out
    assert f"  helped (user assessment, via cli): {SESSION} -> widget-cache [read]" in out.splitlines()


def test_reads_nobody_can_cite_leave_the_summary_byte_for_byte(store, capsys):
    before = _summary(capsys, store)
    _log(
        store / "cost.jsonl",
        _read_row("20260902-other", "retry-backoff"),
        _read_row(SESSION, "draft-notes"),
        _read_row(SESSION, SESSION),
    )

    assert _summary(capsys, store) == before


def test_a_session_that_names_no_document_lists_no_reads(store, capsys):
    _log(store / "cost.jsonl", *(_read_row("nosuch", d) for d in ("retry-backoff", "widget-cache")))

    code, out = _summary(capsys, store, "nosuch")

    assert code == 0, out
    assert "read, not surfaced" not in out


def test_an_unreadable_cost_log_fails_the_session_summary_only(store, capsys):
    store_wide = _cli(capsys, "feedback", "summary", "--store", str(store))
    (store / "cost.jsonl").mkdir()

    code, out = _summary(capsys, store)

    assert code == 2
    assert "cannot read the store's logs" in out
    assert _cli(capsys, "feedback", "summary", "--store", str(store)) == store_wide


def test_a_read_row_with_a_malformed_doc_id_counts_as_a_read_and_names_nothing(store, capsys):
    _log(store / "cost.jsonl", _read_row(SESSION, [1]), _read_row(SESSION, None))

    code, out = _cli(capsys, "cost", "summary", "--session", SESSION, "--store", str(store))
    assert code == 0, out
    assert "  document reads: 2 delivery(ies), 20 bytes" in out.splitlines()
    code, out = _summary(capsys, store)
    assert code == 0, out
    assert "[read]" not in out and "read, not surfaced" not in out


def _complete_cli(store: Path, content: str, capsys, monkeypatch) -> str:
    version = version_of((store / "sessions" / f"{SESSION}.md").read_bytes())
    monkeypatch.setattr(sys, "stdin", piped_stdin(content))
    code, out = _cli(
        capsys, "draft", "complete", SESSION, "--expected-version", version, "--store", str(store)
    )
    assert code == 0, out
    return out


def _complete_mcp(store: Path, content: str, capsys, monkeypatch) -> str:
    result, is_error = rpc.call(
        store, name=mcp_tools.COMPLETE_DRAFT_TOOL_NAME,
        arguments=rpc.versioned(store, {"id": SESSION, "content": content}),
    )
    assert not is_error, rpc.text(result)
    return rpc.text(result)


CHANNELS = [pytest.param(_complete_cli, id="cli"), pytest.param(_complete_mcp, id="mcp")]


def _finish(store: Path, reuse_log: str, complete, capsys, monkeypatch) -> str:
    message = complete(store, _text(SESSION, reuse_log=reuse_log), capsys, monkeypatch)
    docs = {d.id: d for d in load_store(store / "sessions").docs}
    assert docs[SESSION].status == "active"
    return message


@pytest.mark.parametrize("complete", CHANNELS)
def test_a_none_reuse_log_in_a_session_that_read_completes_with_a_warning(
    store, capsys, monkeypatch, complete
):
    _reads(store)

    message = _finish(store, NONE_LOG, complete, capsys, monkeypatch)

    assert (
        f"{WARNING}retry-backoff, widget-cache — if one changed a decision, the published "
        "Reuse Log is missing its row; tell the user, who can add it"
    ) in message.splitlines()


@pytest.mark.parametrize("complete", CHANNELS)
@pytest.mark.parametrize("reuse_log", [ROW_LOG, f"{NONE_LOG}\n\n{ROW_LOG}"], ids=["row", "conflict"])
def test_a_reuse_log_with_a_row_draws_no_reads_warning(
    store, capsys, monkeypatch, complete, reuse_log
):
    _reads(store)

    assert WARNING not in _finish(store, reuse_log, complete, capsys, monkeypatch)


def test_a_session_with_no_citable_reads_draws_no_warning(store, capsys, monkeypatch):
    _log(store / "cost.jsonl", _read_row(SESSION, "draft-notes"), _read_row(SESSION, SESSION))

    assert WARNING not in _finish(store, NONE_LOG, _complete_cli, capsys, monkeypatch)


def test_an_unreadable_cost_log_at_completion_is_a_note_after_publishing(
    store, capsys, monkeypatch
):
    (store / "cost.jsonl").mkdir()

    message = _finish(store, NONE_LOG, _complete_cli, capsys, monkeypatch)

    assert "note: reads check did not run (" in message
    assert "citation check did not run" not in message


@pytest.mark.parametrize(
    ("name", "reuse_log_step"),
    [
        ("engmem.save.md", "One row per prior document that **actually influenced** this work"),
        ("engmem.save.quick.md", "Write it as exactly `Prior docs used: none.`"),
    ],
)
def test_each_save_template_lists_the_session_finds_before_its_reuse_log(name, reuse_log_step):
    text = " ".join((TEMPLATES / name).read_text(encoding="utf-8").split())

    assert text.index("engmem feedback summary --session <id>") < text.index(reuse_log_step)
    assert RELAY in text
