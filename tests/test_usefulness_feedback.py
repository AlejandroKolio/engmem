"""US-15: a user's assessment of a find -- helped, not applicable or harmful -- kept apart from
the Gate 1 count, with an unassessed find reported as unknown (contracts/gate1.md, "Usefulness
feedback")."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import run_tool
from mcp_harness import _call, _run, _text, _tools_call_msg

from engmem import feedback, mcp_server
from engmem.cli import main

GATE1_REPORT = Path(__file__).resolve().parent.parent / "tools" / "gate1_report.py"
FEEDBACK_TOOL = mcp_server.RECORD_FEEDBACK_TOOL_NAME
SESSION = "20260901-task"
FEEDBACK_HEADER = "=== Usefulness feedback"


def _doc(sessions: Path, doc_id: str, *, status: str = "active", body: str = "## Decision Log\n\nA decision.") -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{doc_id}.md").write_bytes(
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-08-01\nstatus: {status}\n"
        f"tags: [platform]\nentities: [WidgetCache]\nrepos: [platform-core]\nrelated: []\n---\n\n"
        f"{body}\n".encode("utf-8")
    )


def _telemetry(store: Path, *rows: dict) -> None:
    with open(store / "telemetry.jsonl", "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _search_row(session_id: str | None, surfaced: list[str], *, weak_only: bool = False) -> dict:
    row = {
        "ts": "2026-09-01T10:00:00+00:00", "query": "q", "session_id": session_id,
        "channel": "cli", "n_docs": 4, "hits": [{"id": i, "score": 1.0} for i in surfaced],
        "surfaced": surfaced, "result": "hit" if surfaced else "miss",
        "context_bytes": 10, "context_tokens_estimate": 3, "scope": None,
    }
    if weak_only:
        row["weak_only"] = True
    return row


@pytest.fixture
def store(tmp_path) -> Path:
    """One session that searched twice and was shown three documents."""
    sessions = tmp_path / "sessions"
    _doc(sessions, SESSION, status="draft")
    for doc_id in ("widget-cache", "logo-policy", "retry-backoff"):
        _doc(sessions, doc_id)
    _telemetry(
        tmp_path,
        _search_row(SESSION, ["widget-cache", "retry-backoff"]),
        _search_row(SESSION, ["logo-policy"], weak_only=True),
    )
    return tmp_path


def _cli(capsys, *args: str) -> tuple[int, str, str]:
    code = main([*args])
    out, err = capsys.readouterr()
    return code, out, err


def _record(capsys, store: Path, doc_id: str, assessment: str, *extra: str) -> tuple[int, str, str]:
    return _cli(
        capsys, "feedback", "record", SESSION, doc_id, assessment, *extra, "--store", str(store)
    )


def _summary(capsys, store: Path, *extra: str) -> str:
    code, out, _ = _cli(capsys, "feedback", "summary", *extra, "--store", str(store))
    assert code == 0, out
    return out


def _feedback_rows(store: Path) -> list[dict]:
    path = store / feedback.FEEDBACK_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _line(out: str, prefix: str) -> str:
    return next(line for line in out.splitlines() if line.startswith(prefix))


# ---------------------------------------------------------------------------
# AC-15.1 -- found, no feedback: counted as found, influence unknown
# ---------------------------------------------------------------------------


def test_a_find_without_feedback_is_counted_as_found_with_unknown_influence(store, capsys):
    out = _summary(capsys, store)

    assert "3 find(s) across 1 session(s)" in _line(out, "usefulness feedback")
    assert _line(out, "assessed by the user:").startswith("assessed by the user: 0")
    assert _line(out, "influence unknown").startswith(
        "influence unknown (found, not assessed): 3"
    )
    assert _line(out, "positive reuse by user assessment:").startswith(
        "positive reuse by user assessment: 0"
    )


def test_a_find_comes_from_the_surfaced_ids_of_rows_attributed_to_the_session(tmp_path, capsys):
    """An unattributed search finds nothing for any session, and neither does `hits`: it is the
    ranking, which can name documents the reader was never shown."""
    sessions = tmp_path / "sessions"
    for doc_id in (SESSION, "widget-cache", "logo-policy", "ranked-only", "legacy-hit"):
        _doc(sessions, doc_id)
    shown = _search_row(SESSION, ["widget-cache"])
    shown["hits"].append({"id": "ranked-only", "score": 0.1})
    legacy = _search_row(SESSION, ["legacy-hit"])
    del legacy["surfaced"]
    _telemetry(tmp_path, shown, legacy, _search_row(None, ["logo-policy"]))

    out = _summary(capsys, tmp_path, "--session", SESSION)

    assert "1 find(s) across 1 session(s)" in out
    assert f"unknown: {SESSION} -> widget-cache" in out
    for absent in ("logo-policy", "ranked-only", "legacy-hit"):
        assert absent not in out


def test_finds_of_a_session_id_naming_no_document_are_named_and_not_counted(store, capsys):
    """A mistyped `--session` would otherwise leave finds nobody can ever assess."""
    _telemetry(store, _search_row("20260901-typo", ["widget-cache"]))

    out = _summary(capsys, store)

    assert "3 find(s) across 1 session(s)" in out
    assert _line(out, "finds in searches whose session_id names no document here") == (
        "finds in searches whose session_id names no document here, not counted: 1 -- "
        "20260901-typo"
    )


def test_a_find_shown_only_as_a_weak_candidate_is_found_and_marked(store, capsys):
    out = _summary(capsys, store, "--session", SESSION)

    assert _line(out, "weak candidates among the finds:").startswith(
        "weak candidates among the finds: 1"
    )
    assert f"unknown: {SESSION} -> logo-policy [weak candidate]" in out
    assert f"unknown: {SESSION} -> widget-cache" in out
    assert f"widget-cache [weak candidate]" not in out


def test_a_document_also_shown_by_a_reliable_search_is_not_a_weak_find(store, capsys):
    _telemetry(store, _search_row(SESSION, ["logo-policy", "widget-cache"]))

    out = _summary(capsys, store)

    assert _line(out, "weak candidates among the finds:").startswith(
        "weak candidates among the finds: 0"
    )


# ---------------------------------------------------------------------------
# AC-15.2 -- "helped", with the changed decision and the source, marked as the user's
# ---------------------------------------------------------------------------


def test_helped_is_recorded_with_decision_and_source_as_a_user_assessment(store, capsys):
    code, out, _ = _record(
        capsys, store, "widget-cache", "helped",
        "--decision", "kept the warm-up step", "--source", "PR 12 review",
    )

    assert code == 0, out
    [row] = _feedback_rows(store)
    assert row["session_id"] == SESSION
    assert row["doc_id"] == "widget-cache"
    assert row["assessment"] == "helped"
    assert row["assessed_by"] == "user"
    assert row["decision"] == "kept the warm-up step"
    assert row["source"] == "PR 12 review"
    assert row["channel"] == "cli"
    assert "user assessment" in out

    summary = _summary(capsys, store)
    assert (
        f"  helped (user assessment, via cli): {SESSION} -> widget-cache -- decision: kept the warm-up "
        "step; source: PR 12 review"
    ) in summary.splitlines()
    assert _line(summary, "positive reuse by user assessment:").startswith(
        "positive reuse by user assessment: 1"
    )


def test_decision_and_source_are_optional_and_blank_reads_as_absent(store, capsys):
    code, out, _ = _record(capsys, store, "widget-cache", "helped", "--decision", "  ")

    assert code == 0, out
    [row] = _feedback_rows(store)
    assert row["decision"] is None
    assert row["source"] is None
    assert f"  helped (user assessment, via cli): {SESSION} -> widget-cache" in _summary(capsys, store).splitlines()


def test_the_latest_assessment_of_a_find_replaces_an_earlier_one(store, capsys):
    _record(capsys, store, "widget-cache", "helped")
    _record(capsys, store, "widget-cache", "harmful", "--decision", "rolled it back")

    out = _summary(capsys, store)

    assert len(_feedback_rows(store)) == 2
    assert _line(out, "assessed by the user:") == (
        "assessed by the user: 1 -- helped: 0, not applicable: 0, harmful: 1"
    )


def test_the_mcp_tool_records_the_same_row_as_the_cli(store, capsys):
    _record(capsys, store, "widget-cache", "helped", "--decision", "d", "--source", "s")
    result, is_error = _call(
        store, name=FEEDBACK_TOOL,
        arguments={
            "session_id": SESSION, "doc_id": "widget-cache", "assessment": "helped",
            "decision": "d", "source": "s",
        },
    )

    assert not is_error, _text(result)
    assert "user assessment" in _text(result)
    cli_row, mcp_row = _feedback_rows(store)
    for row in (cli_row, mcp_row):
        del row["ts"]
    assert mcp_row.pop("channel") == "mcp"
    assert cli_row.pop("channel") == "cli"
    assert mcp_row == cli_row


# ---------------------------------------------------------------------------
# AC-15.3 -- not applicable / harmful: shown apart, never positive reuse
# ---------------------------------------------------------------------------


def test_not_applicable_and_harmful_are_shown_apart_and_not_counted_as_positive_reuse(
    store, capsys
):
    _record(capsys, store, "widget-cache", "helped")
    _record(capsys, store, "retry-backoff", "not-applicable")
    _record(capsys, store, "logo-policy", "harmful", "--decision", "followed a stale rule")

    out = _summary(capsys, store)

    assert _line(out, "assessed by the user:") == (
        "assessed by the user: 3 -- helped: 1, not applicable: 1, harmful: 1"
    )
    assert _line(out, "positive reuse by user assessment:").startswith(
        "positive reuse by user assessment: 1"
    )
    lines = out.splitlines()
    assert f"  not applicable (user assessment, via cli): {SESSION} -> retry-backoff" in lines
    assert (
        f"  harmful (user assessment, via cli): {SESSION} -> logo-policy [weak candidate] -- decision: "
        "followed a stale rule"
    ) in lines


@pytest.mark.parametrize("word", ["useful", "reuse", "anti-reuse", "unknown", ""])
def test_an_assessment_outside_the_three_is_refused_and_nothing_is_written(store, capsys, word):
    code, out, err = _record(capsys, store, "widget-cache", word)

    assert code == 2
    assert "helped, not-applicable, harmful" in out + err
    assert _feedback_rows(store) == []


def test_the_assessment_word_is_read_case_insensitively(store, capsys):
    code, out, _ = _record(capsys, store, "widget-cache", "Not-Applicable")

    assert code == 0, out
    assert _feedback_rows(store)[0]["assessment"] == "not-applicable"


# ---------------------------------------------------------------------------
# AC-15.4 -- partly assessed: both counts visible, unknown never a failure
# ---------------------------------------------------------------------------


def test_a_partly_assessed_session_shows_assessed_and_unknown_counts(store, capsys):
    _record(capsys, store, "widget-cache", "harmful")

    out = _summary(capsys, store, "--session", SESSION)

    assert _line(out, "assessed by the user:").startswith("assessed by the user: 1")
    assert _line(out, "influence unknown") == (
        "influence unknown (found, not assessed): 2 -- neither a success nor a failure"
    )
    assert f"unknown: {SESSION} -> retry-backoff" in out
    assert f"unknown: {SESSION} -> widget-cache" not in out


def test_the_summary_makes_no_causal_claim(store, capsys):
    out = _summary(capsys, store)

    assert out.splitlines()[-1] == feedback.CAUSAL_CAVEAT
    assert "not a causal" in feedback.CAUSAL_CAVEAT


# ---------------------------------------------------------------------------
# refusals: what is recorded is a user's assessment of a find this session actually had
# ---------------------------------------------------------------------------


def test_a_document_the_session_never_found_is_refused(store, capsys):
    _doc(store / "sessions", "never-shown")

    code, out, err = _record(capsys, store, "never-shown", "helped")

    assert code == 2
    assert "was not shown by any search attributed to" in out + err
    assert _feedback_rows(store) == []


def test_a_session_id_naming_no_document_is_refused(store, capsys):
    _telemetry(store, _search_row("20260901-typo", ["widget-cache"]))

    code, out, err = _cli(
        capsys, "feedback", "record", "20260901-typo", "widget-cache", "helped",
        "--store", str(store),
    )

    assert code == 2
    assert "no document with id '20260901-typo'" in out + err
    assert _feedback_rows(store) == []


@pytest.mark.parametrize("flag", ["--decision", "--source"])
def test_an_over_long_text_is_refused_rather_than_cut(store, capsys, flag):
    code, out, err = _record(
        capsys, store, "widget-cache", "helped", flag, "x" * (feedback.TEXT_MAX + 1)
    )

    assert code == 2
    assert str(feedback.TEXT_MAX) in out + err
    assert _feedback_rows(store) == []


def test_record_refuses_when_telemetry_cannot_be_decoded(store, capsys):
    (store / "telemetry.jsonl").write_bytes(b"\xff\xfe not utf-8\n")

    code, out, err = _record(capsys, store, "widget-cache", "helped")

    assert code == 2
    assert "telemetry.jsonl" in out + err
    assert _feedback_rows(store) == []


# ---------------------------------------------------------------------------
# reading the feedback log
# ---------------------------------------------------------------------------


def test_a_bad_feedback_line_is_skipped_and_counted_and_the_rest_still_read(store, capsys):
    _record(capsys, store, "widget-cache", "helped")
    with open(store / feedback.FEEDBACK_FILE, "a", encoding="utf-8") as f:
        f.write('{"session_id": "20260901-task", "doc_id": "retry-back\n')
        f.write(json.dumps({"session_id": SESSION, "doc_id": "retry-backoff",
                            "assessment": "great"}) + "\n")
        f.write("[1, 2]\n")

    out = _summary(capsys, store)

    assert _line(out, "assessed by the user:").startswith("assessed by the user: 1")
    assert _line(out, "unreadable feedback line(s), skipped:") == (
        "unreadable feedback line(s), skipped: 3"
    )


def test_an_assessment_matching_no_find_is_named_and_not_counted(store, capsys):
    """A find can disappear after it was assessed, e.g. a telemetry.jsonl replaced by hand."""
    _record(capsys, store, "widget-cache", "helped")
    (store / "telemetry.jsonl").write_text(
        json.dumps(_search_row(SESSION, ["retry-backoff"])) + "\n", encoding="utf-8"
    )

    out = _summary(capsys, store)

    assert _line(out, "assessed by the user:").startswith("assessed by the user: 0")
    assert _line(out, "assessments matching no find, not counted:") == (
        "assessments matching no find, not counted: 1"
    )


def test_a_control_character_in_recorded_text_cannot_forge_a_summary_line(store, capsys):
    _record(capsys, store, "widget-cache", "helped", "--decision", "a\nassessed by the user: 99")

    out = _summary(capsys, store)

    assert [l for l in out.splitlines() if l.startswith("assessed by the user:")] == [
        "assessed by the user: 1 -- helped: 1, not applicable: 0, harmful: 0"
    ]


def test_summary_exits_2_when_telemetry_cannot_be_decoded(store, capsys):
    (store / "telemetry.jsonl").write_bytes(b"\xff\xfe not utf-8\n")

    code, out, _ = _cli(capsys, "feedback", "summary", "--store", str(store))

    assert code == 2
    assert "0 find(s)" not in out


def test_a_store_with_no_telemetry_has_no_finds(tmp_path, capsys):
    _doc(tmp_path / "sessions", SESSION)

    out = _summary(capsys, tmp_path)

    assert "0 find(s) across 0 session(s)" in out


# ---------------------------------------------------------------------------
# end to end: a real search, then the assessment of what it showed
# ---------------------------------------------------------------------------


def test_a_real_attributed_search_yields_finds_the_user_can_assess(tmp_path, capsys):
    sessions = tmp_path / "sessions"
    _doc(sessions, SESSION, status="draft")
    _doc(sessions, "widget-cache", body="## Decision Log\n\nWidgetCache warms before traffic.")

    code, _, _ = _cli(
        capsys, "search", "WidgetCache warms", "--session", SESSION, "--store", str(tmp_path)
    )
    assert code == 0
    code, out, _ = _record(capsys, tmp_path, "widget-cache", "helped")

    assert code == 0, out
    assert _line(_summary(capsys, tmp_path), "assessed by the user:").startswith(
        "assessed by the user: 1"
    )


# ---------------------------------------------------------------------------
# MCP: the tool is a write tool, absent from a read-only server
# ---------------------------------------------------------------------------


def test_the_mcp_tool_refuses_what_the_cli_refuses(store):
    result, is_error = _call(
        store, name=FEEDBACK_TOOL,
        arguments={"session_id": SESSION, "doc_id": "widget-cache", "assessment": "useful"},
    )

    assert is_error
    assert "helped, not-applicable, harmful" in _text(result)
    assert _feedback_rows(store) == []


@pytest.mark.parametrize("arguments", [
    {"doc_id": "widget-cache", "assessment": "helped"},
    {"session_id": SESSION, "doc_id": "widget-cache", "assessment": "helped", "decision": 5},
    {"session_id": SESSION, "doc_id": 7, "assessment": "helped"},
])
def test_a_malformed_mcp_call_is_a_protocol_fault(store, arguments):
    _, responses, _ = _run(store, _tools_call_msg(1, name=FEEDBACK_TOOL, arguments=arguments))

    assert responses[0]["error"]["code"] == mcp_server.INVALID_PARAMS
    assert _feedback_rows(store) == []


def test_a_read_only_server_neither_lists_nor_runs_the_feedback_tool(store):
    arguments = {"session_id": SESSION, "doc_id": "widget-cache", "assessment": "helped"}
    _, responses, _ = _run(
        store,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        _tools_call_msg(2, name=FEEDBACK_TOOL, arguments=arguments),
        read_only=True,
    )

    assert FEEDBACK_TOOL not in [t["name"] for t in responses[0]["result"]["tools"]]
    assert "--read-only" in responses[1]["error"]["message"]
    assert _feedback_rows(store) == []


# ---------------------------------------------------------------------------
# Gate 1: the block is appended, and no figure before it moves
# ---------------------------------------------------------------------------


def _citing_store(tmp_path: Path) -> Path:
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache", body="## Decision Log\n\nCacheWarmer must run before traffic.")
    _doc(
        sessions, SESSION,
        body=(
            "## Pre-reg\n\nBaseline.\n\n## 16. Reuse Log\n\n"
            "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
            '| widget-cache | order: "CacheWarmer must run before traffic" | kept it | reuse |\n\n'
            "## Search Trace\n\nshell"
        ),
    )
    _telemetry(tmp_path, _search_row(SESSION, ["widget-cache"]))
    return tmp_path


def test_harmful_feedback_changes_no_gate1_figure_and_only_appends_a_block(tmp_path, capsys):
    store = _citing_store(tmp_path)
    before = run_tool(GATE1_REPORT, store)
    _record(capsys, store, "widget-cache", "harmful")
    after = run_tool(GATE1_REPORT, store)

    assert before.returncode == after.returncode == 0
    gate1_before, block_before = before.stdout.split(FEEDBACK_HEADER)
    gate1_after, block_after = after.stdout.split(FEEDBACK_HEADER)
    assert gate1_after == gate1_before
    assert "1 valid" in gate1_after
    assert "assessed by the user: 0" in block_before
    assert "assessed by the user: 1 -- helped: 0, not applicable: 0, harmful: 1" in block_after


def test_gate1_report_survives_undecodable_telemetry_in_the_feedback_block(tmp_path):
    store = _citing_store(tmp_path)
    (store / "telemetry.jsonl").write_bytes(b"\xff\xfe not utf-8\n")

    result = run_tool(GATE1_REPORT, store)

    assert result.returncode == 0
    block = result.stdout.split(FEEDBACK_HEADER)[1]
    assert "UNMEASURED" in block
    assert "0 find(s)" not in block


def test_a_session_id_naming_no_document_whose_searches_found_nothing_is_not_named(store, capsys):
    _telemetry(store, _search_row("20260901-typo", []))

    out = _summary(capsys, store)

    assert "20260901-typo" not in out


def test_a_session_is_not_its_own_find(store, capsys):
    _telemetry(store, _search_row(SESSION, [SESSION, "widget-cache"]))

    out = _summary(capsys, store, "--session", SESSION)
    code, refusal, err = _record(capsys, store, SESSION, "helped")

    assert "3 find(s)" in out
    assert f"-> {SESSION}" not in out
    assert code == 2
    assert _feedback_rows(store) == []


def test_a_torn_last_line_does_not_swallow_the_next_record(store, capsys):
    (store / feedback.FEEDBACK_FILE).write_bytes(b'{"session_id": "20260901-task", "doc_')

    code, out, _ = _record(capsys, store, "widget-cache", "helped")
    summary = _summary(capsys, store)

    assert code == 0, out
    assert _line(summary, "assessed by the user:").startswith("assessed by the user: 1")
    assert _line(summary, "unreadable feedback line(s), skipped:") == (
        "unreadable feedback line(s), skipped: 1"
    )


def test_a_complete_last_line_gets_no_blank_line_before_the_next(store, capsys):
    _record(capsys, store, "widget-cache", "helped")
    _record(capsys, store, "retry-backoff", "harmful")

    raw = (store / feedback.FEEDBACK_FILE).read_bytes()

    assert b"\n\n" not in raw
    assert raw.count(b"\n") == 2


def test_the_summary_names_the_channel_that_recorded_each_assessment(store, capsys):
    _record(capsys, store, "widget-cache", "helped")
    _call(store, name=FEEDBACK_TOOL,
          arguments={"session_id": SESSION, "doc_id": "retry-backoff", "assessment": "harmful"})

    lines = _summary(capsys, store).splitlines()

    assert f"  helped (user assessment, via cli): {SESSION} -> widget-cache" in lines
    assert f"  harmful (user assessment, via mcp): {SESSION} -> retry-backoff" in lines


TAMPERED = "evil\u001b]0;pwn\u0007\nline"


def test_an_id_read_back_from_telemetry_reaches_a_refusal_escaped(store, capsys):
    _telemetry(store, _search_row(SESSION, [TAMPERED]))
    _doc(store / "sessions", "never-shown")

    code, out, err = _record(capsys, store, "never-shown", "helped")
    result, is_error = _call(
        store, name=FEEDBACK_TOOL,
        arguments={"session_id": SESSION, "doc_id": "never-shown", "assessment": "helped"},
    )

    assert code == 2 and is_error
    for text in (out, err, _text(result)):
        assert "\x1b" not in text and "\x07" not in text
        assert "evil\\x1b" in text


def test_a_recorded_id_reaches_the_confirmation_escaped(store, capsys):
    _telemetry(store, _search_row(SESSION, [TAMPERED]))

    code, out, _ = _record(capsys, store, TAMPERED, "helped")
    result, is_error = _call(
        store, name=FEEDBACK_TOOL,
        arguments={"session_id": SESSION, "doc_id": TAMPERED, "assessment": "helped"},
    )

    assert code == 0 and not is_error
    for text in (out, _text(result)):
        assert "\x1b" not in text and "\x07" not in text
        assert "evil\\x1b" in text
