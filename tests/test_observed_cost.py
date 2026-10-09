"""US-16: the cost of the work engmem observed -- bytes it delivered to a session, estimated
tokens, what the client reported for the baseline call and what was not observed, each named as
such (contracts/output.md, "Observed cost")."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp_harness import _call, _run, _text, _tools_call_msg

from engmem import cost, mcp_server, versions
from engmem.cli import main
from engmem.telemetry import estimate_tokens

SESSION = "20260901-task"
QUERY = "WidgetCache eviction"
READ_TOOL = mcp_server.READ_TOOL_NAME
BASELINE_TOOL = mcp_server.RECORD_BASELINE_TOOL_NAME
WIDGET_BODY = (
    "## Cold-start primer\n\nWidgetCache eviction runs nightly.\n\n"
    "## Decision Log\n\nWe chose LRU eviction for WidgetCache.\n\n"
    "## Lessons Learned\n\nMeasure before tuning."
)
SEARCH_ROW_KEYS = {
    "ts", "query", "session_id", "channel", "n_docs", "hits", "surfaced", "result",
    "context_bytes", "context_tokens_estimate", "scope",
}


def _doc(sessions: Path, doc_id: str, *, status: str = "active", body: str = WIDGET_BODY) -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{doc_id}.md").write_bytes(
        f"---\nid: {doc_id}\ntitle: {doc_id} title\ndate: 2026-08-01\nstatus: {status}\n"
        f"tags: [platform]\nentities: [WidgetCache]\nrelated: []\n---\n\n{body}\n".encode("utf-8")
    )


@pytest.fixture
def store(tmp_path) -> Path:
    sessions = tmp_path / "sessions"
    _doc(sessions, SESSION, status="draft", body="")
    _doc(sessions, "widget-cache")
    _doc(sessions, "other-note", body="## Decision Log\n\nUnrelated decision.")
    return tmp_path


def _cli(capsys, *args: str) -> tuple[int, str, str]:
    code = main([*args])
    out, err = capsys.readouterr()
    return code, out, err


def _cli_ok(capsys, store: Path, *args: str) -> str:
    code, out, err = _cli(capsys, *args, "--store", str(store))
    assert code == 0, out + err
    return out


def _summary(capsys, store: Path, session: str = SESSION) -> str:
    return _cli_ok(capsys, store, "cost", "summary", "--session", session)


def _rows(store: Path) -> list[dict]:
    path = store / cost.COST_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _line(out: str, prefix: str) -> str:
    """The first line starting with `prefix` once its indent is dropped, without the indent."""
    return next(line.strip() for line in out.splitlines() if line.lstrip().startswith(prefix))


def _delivered(stdout: str) -> int:
    """The bytes of the response itself: `print` adds the one newline that is transport."""
    return len(stdout.removesuffix("\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# AC-16.1 -- every delivery counted; searches and reads apart and in total
# ---------------------------------------------------------------------------


def test_each_delivery_counts_the_bytes_of_the_whole_response_it_sent(store, capsys):
    searched = _cli_ok(capsys, store, "search", QUERY, "--session", SESSION)
    read = _cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION)
    result, is_error = _call(store, name=READ_TOOL, arguments={"id": "widget-cache", "session_id": SESSION})
    assert not is_error
    mcp_search, _ = _call(store, name="engmem_search", arguments={"query": QUERY, "session_id": SESSION})

    rows = _rows(store)
    assert [(r["op"], r["channel"], r["bytes"]) for r in rows] == [
        ("search", "cli", _delivered(searched)),
        ("read", "cli", _delivered(read)),
        ("read", "mcp", len(_text(result).encode("utf-8"))),
        ("search", "mcp", len(_text(mcp_search).encode("utf-8"))),
    ]
    assert len({r["op_id"] for r in rows}) == 4
    assert set(rows[0]) == {"ts", "op_id", "op", "session_id", "channel", "bytes"}
    assert set(rows[1]) == {"ts", "op_id", "op", "session_id", "channel", "bytes", "doc_id", "role"}


def test_the_summary_shows_search_responses_and_reads_apart_and_in_total(store, capsys):
    searched = _cli_ok(capsys, store, "search", QUERY, "--session", SESSION)
    first = _cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION)
    second = _cli_ok(capsys, store, "read", "other-note", "--session", SESSION)
    search_bytes = _delivered(searched)
    read_bytes = _delivered(first) + _delivered(second)

    out = _summary(capsys, store)

    assert _line(out, "search responses:") == f"search responses: 1 delivery(ies), {search_bytes} bytes"
    assert _line(out, "document reads:") == f"document reads: 2 delivery(ies), {read_bytes} bytes"
    assert _line(out, "total:").startswith(
        f"total: 3 delivery(ies), {search_bytes + read_bytes} bytes"
    )
    assert _line(out, "measured by engmem").startswith("measured by engmem, UTF-8 bytes")


def test_text_repeated_by_different_operations_is_counted_each_time(store, capsys):
    once = _delivered(_cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION))
    _cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION)

    out = _summary(capsys, store)

    assert _line(out, "document reads:") == f"document reads: 2 delivery(ies), {2 * once} bytes"


def test_one_operation_whose_line_appears_twice_is_counted_once(store, capsys):
    once = _delivered(_cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION))
    path = store / cost.COST_FILE
    path.write_bytes(path.read_bytes() * 2)

    out = _summary(capsys, store)

    assert _line(out, "document reads:") == f"document reads: 1 delivery(ies), {once} bytes"
    assert _line(out, "lines repeating an operation") == (
        "lines repeating an operation already counted, skipped: 1"
    )


def test_only_the_operations_attributed_to_the_session_are_its_cost(store, capsys):
    _cli_ok(capsys, store, "read", "widget-cache")
    _cli_ok(capsys, store, "read", "widget-cache", "--session", "20260902-other")

    out = _summary(capsys, store)

    assert _line(out, "document reads:") == "document reads: 0 delivery(ies), 0 bytes"


def test_a_role_read_delivers_only_that_roles_sections(store, capsys):
    out = _cli_ok(capsys, store, "read", "widget-cache", "--role", "decisions", "--session", SESSION)

    assert "We chose LRU eviction" in out
    assert "runs nightly" not in out and "Measure before tuning" not in out
    assert _rows(store)[0]["role"] == "decisions"


def test_a_read_through_mcp_delivers_the_text_the_cli_prints(store, capsys):
    cli_text = _cli_ok(capsys, store, "read", "widget-cache", "--role", "decisions", "--session", SESSION)
    result, _ = _call(
        store, name=READ_TOOL,
        arguments={"id": "widget-cache", "role": "decisions", "session_id": SESSION},
    )

    assert _text(result) == cli_text.removesuffix("\n")
    cli_row, mcp_row = _rows(store)
    for row in (cli_row, mcp_row):
        del row["ts"], row["op_id"], row["channel"]
    assert cli_row == mcp_row


@pytest.mark.parametrize(
    ("command", "note"),
    [
        pytest.param(("read", "widget-cache"), "note: unattributed read — pass --session <draft-id>", id="read"),
        pytest.param(("search", QUERY), "note: unattributed search — pass --session <draft-id>", id="search"),
    ],
)
def test_an_unattributed_delivery_counts_its_closing_note_and_is_no_sessions_cost(
    store, capsys, command, note
):
    out = _cli_ok(capsys, store, *command)

    assert out.splitlines()[-1] == note
    row = _rows(store)[0]
    assert row["session_id"] is None
    assert row["bytes"] == _delivered(out)


def test_a_read_escapes_control_characters_but_keeps_the_documents_lines(store, capsys):
    _doc(store / "sessions", "escape-note", body="## Decision Log\n\nline one\x1b[31m\nline two\ttabbed")

    out = _cli_ok(capsys, store, "read", "escape-note", "--session", SESSION)

    assert "\x1b" not in out
    assert "line one\\x1b[31m\nline two\ttabbed" in out


def test_a_superseded_document_is_read_with_its_successor_named(store, capsys):
    (store / "sessions" / "widget-cache.md").write_text(
        (store / "sessions" / "widget-cache.md").read_text(encoding="utf-8").replace(
            "status: active\n", "status: superseded\nsuperseded_by: other-note\n"
        ),
        encoding="utf-8",
    )

    out = _cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION)

    assert out.splitlines()[0] == (
        "document: widget-cache -- widget-cache title (status: superseded, superseded by other-note)"
    )


def test_an_mcp_read_with_an_unknown_role_is_a_protocol_fault(store):
    _, responses, _ = _run(
        store, _tools_call_msg(1, name=READ_TOOL, arguments={"id": "widget-cache", "role": "nonsense"})
    )

    assert responses[0]["error"]["code"] == mcp_server.INVALID_PARAMS
    assert not (store / cost.COST_FILE).exists()


def test_a_line_torn_by_an_interrupted_write_does_not_swallow_the_next(store, capsys):
    (store / cost.COST_FILE).write_bytes(b'{"op": "read", "op_id": "torn"')
    once = _delivered(_cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION))

    out = _summary(capsys, store)

    assert _line(out, "document reads:") == f"document reads: 1 delivery(ies), {once} bytes"
    assert _line(out, "unreadable cost.jsonl") == "unreadable cost.jsonl line(s) in the store, skipped: 1"


@pytest.mark.parametrize(
    ("doc_id", "extra", "reason"),
    [
        pytest.param("no-such-doc", (), "no document with id 'no-such-doc'", id="unknown-id"),
        pytest.param(SESSION, (), "is a draft", id="draft"),
        pytest.param("other-note", ("--role", "lessons"), "has no lessons section", id="no-section"),
        pytest.param("widget-cache", ("--role", "nonsense"), "unknown role", id="unknown-role"),
    ],
)
def test_a_refused_read_delivers_nothing_and_records_nothing(store, capsys, doc_id, extra, reason):
    code, out, err = _cli(capsys, "read", doc_id, *extra, "--store", str(store))

    assert code == 2
    assert reason in out and reason in err
    assert "LRU" not in out
    assert not (store / cost.COST_FILE).exists()


def test_an_mcp_read_of_a_draft_is_refused_as_a_tool_error(store):
    result, is_error = _call(store, name=READ_TOOL, arguments={"id": SESSION})

    assert is_error and "is a draft" in _text(result)
    assert not (store / cost.COST_FILE).exists()


def test_an_unrecorded_delivery_is_named_in_the_response(store, capsys):
    (store / cost.COST_FILE).mkdir()

    searched = _cli_ok(capsys, store, "search", QUERY, "--session", SESSION)
    read = _cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION)

    assert "note: cost not recorded (" in searched
    assert "note: cost not recorded (" in read
    assert "We chose LRU eviction" in read


def test_a_search_row_in_telemetry_keeps_exactly_the_keys_it_had(store, capsys):
    _cli_ok(capsys, store, "search", QUERY, "--session", SESSION)

    row = json.loads((store / "telemetry.jsonl").read_text(encoding="utf-8"))
    assert set(row) == SEARCH_ROW_KEYS


def test_a_read_only_search_records_no_delivery(store):
    _, responses, _ = _run(
        store, _tools_call_msg(1, arguments={"query": QUERY, "session_id": SESSION}), read_only=True
    )

    assert "error" not in responses[0]
    assert not (store / cost.COST_FILE).exists()


def test_a_search_logged_without_a_delivery_is_named_as_missing(store, capsys):
    _run(store, _tools_call_msg(1, arguments={"query": QUERY, "session_id": SESSION}), read_only=True)
    _cli_ok(capsys, store, "search", QUERY, "--session", SESSION)

    out = _summary(capsys, store)

    assert _line(out, "search responses:").startswith("search responses: 1 delivery(ies)")
    assert "with no delivery record, not counted: 1" in out
    assert "missing data" in _line(out, "- searches of this session in telemetry.jsonl")


# ---------------------------------------------------------------------------
# AC-16.2 -- what the client reported for the baseline call, tied to that call
# ---------------------------------------------------------------------------


def test_a_baseline_the_client_reported_is_shown_as_measured_by_it_and_tied_to_its_call(store, capsys):
    out = _cli_ok(
        capsys, store, "cost", "record-baseline", SESSION,
        "--tokens", "1234", "--seconds", "12.5", "--call-id", "agent-call-7",
    )
    assert "recorded the baseline call agent-call-7" in out

    summary = _summary(capsys, store)

    assert _line(summary, "baseline call") == (
        "baseline call (no-memory sub-agent), measured and reported by the client:"
    )
    assert _line(summary, "call agent-call-7:") == (
        "call agent-call-7: 1234 tokens, 12.5 s (reported via cli)"
    )


def test_a_baseline_reported_through_mcp_is_the_same_record(store, capsys):
    result, is_error = _call(
        store, name=BASELINE_TOOL,
        arguments={"session_id": SESSION, "tokens": 900, "call_id": "c1"},
    )
    assert not is_error, _text(result)

    summary = _summary(capsys, store)

    assert _line(summary, "call c1:") == "call c1: 900 tokens (reported via mcp)"
    assert _rows(store)[0]["reported_by"] == "client"


def test_a_call_reported_again_shows_only_its_latest_report(store, capsys):
    for tokens in ("100", "150"):
        _cli_ok(capsys, store, "cost", "record-baseline", SESSION, "--tokens", tokens, "--call-id", "c1")
    _cli_ok(capsys, store, "cost", "record-baseline", SESSION, "--seconds", "3")

    summary = _summary(capsys, store)

    assert _line(summary, "call c1:") == "call c1: 150 tokens (reported via cli)"
    assert _line(summary, "a call with no id:") == "a call with no id: 3 s (reported via cli)"
    assert _line(summary, "earlier reports") == (
        "earlier reports of the same baseline call, replaced by the latest: 1"
    )


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        pytest.param((SESSION,), "nothing to record", id="no-figure"),
        pytest.param((SESSION, "--tokens", "0"), "tokens must be a whole number above zero", id="zero-tokens"),
        pytest.param((SESSION, "--tokens", "-5"), "tokens must be a whole number above zero", id="negative"),
        pytest.param((SESSION, "--seconds", "nan"), "seconds must be a number above zero", id="nan"),
        pytest.param((SESSION, "--seconds", "inf"), "seconds must be a number above zero", id="inf"),
        pytest.param(("20260999-none", "--tokens", "5"), "no document with id", id="no-session"),
        pytest.param((SESSION, "--tokens", "5", "--call-id", "x" * 201), "at most 200", id="long-id"),
    ],
)
def test_a_refused_baseline_writes_nothing(store, capsys, args, reason):
    code, out, err = _cli(capsys, "cost", "record-baseline", *args, "--store", str(store))

    assert code == 2
    assert reason in out and reason in err
    assert not (store / cost.COST_FILE).exists()


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"session_id": SESSION, "tokens": True}, id="bool-tokens"),
        pytest.param({"session_id": SESSION, "tokens": 1.5}, id="float-tokens"),
        pytest.param({"session_id": SESSION, "seconds": "3"}, id="string-seconds"),
        pytest.param({"session_id": SESSION, "tokens": 5, "call_id": 7}, id="number-call-id"),
        pytest.param({"tokens": 5}, id="no-session"),
    ],
)
def test_an_mcp_baseline_of_the_wrong_type_is_a_protocol_fault(store, arguments):
    _, responses, _ = _run(store, _tools_call_msg(1, name=BASELINE_TOOL, arguments=arguments))

    assert responses[0]["error"]["code"] == mcp_server.INVALID_PARAMS
    assert not (store / cost.COST_FILE).exists()


def test_an_mcp_baseline_with_no_figure_is_refused_as_a_tool_error(store):
    result, is_error = _call(store, name=BASELINE_TOOL, arguments={"session_id": SESSION})

    assert is_error and "nothing to record" in _text(result)
    assert not (store / cost.COST_FILE).exists()


# ---------------------------------------------------------------------------
# AC-16.3 -- missing data and incomplete coverage marked; an estimate never passed off as measured
# ---------------------------------------------------------------------------


def test_with_nothing_reported_the_tokens_and_baseline_are_missing_data(store, capsys):
    _cli_ok(capsys, store, "search", QUERY, "--session", SESSION)

    out = _summary(capsys, store)

    assert _line(out, "actual tokens of these deliveries:") == (
        "actual tokens of these deliveries: not reported by the client -- missing data"
    )
    assert _line(out, "baseline call") == (
        "baseline call (no-memory sub-agent): not reported by the client -- missing data"
    )


def test_estimated_tokens_are_labelled_an_estimate(store, capsys):
    searched = _cli_ok(capsys, store, "search", QUERY, "--session", SESSION)
    tokens = estimate_tokens(_delivered(searched))

    out = _summary(capsys, store)

    assert _line(out, "estimated tokens") == (
        f"estimated tokens of that total: {tokens} -- ceil(bytes / 3.5), an estimate, not a "
        "measurement"
    )


def test_reading_outside_engmem_is_named_as_not_observed(store, capsys):
    _cli_ok(capsys, store, "search", QUERY, "--session", SESSION)

    out = _summary(capsys, store)

    assert "coverage, not complete:" in out
    assert "no document read observed" in out and "missing data" in _line(out, "- no document read")
    assert "the model's full context outside engmem" in out


def test_with_reads_observed_a_file_opened_directly_is_still_named(store, capsys):
    _cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION)

    out = _summary(capsys, store)

    assert "a document opened as a file, not through `engmem read`, is not observed" in out


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("[1, 2]", id="not-an-object"),
        pytest.param({"op": "read", "session_id": SESSION, "bytes": 5}, id="no-op-id"),
        pytest.param({"op": "read", "op_id": "a", "session_id": SESSION, "bytes": True}, id="bool-bytes"),
        pytest.param({"op": "read", "op_id": "a", "session_id": SESSION, "bytes": -1}, id="negative"),
        pytest.param({"op": "write", "op_id": "a", "session_id": SESSION, "bytes": 5}, id="unknown-op"),
        pytest.param({"op": "read", "op_id": "a", "session_id": 7, "bytes": 5}, id="session-type"),
        pytest.param({"op": "baseline", "op_id": "a", "session_id": SESSION}, id="empty-baseline"),
        pytest.param({"op": "baseline", "op_id": "a", "session_id": None, "tokens": 9}, id="no-session"),
        pytest.param(
            {"op": "baseline", "op_id": "a", "session_id": SESSION, "tokens": "9"}, id="string-tokens"
        ),
    ],
)
def test_a_line_the_reader_cannot_count_is_named_and_skipped(store, capsys, line):
    text = line if isinstance(line, str) else json.dumps(line)
    (store / cost.COST_FILE).write_text(text + "\n", encoding="utf-8")
    once = _delivered(_cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION))

    out = _summary(capsys, store)

    assert _line(out, "document reads:") == f"document reads: 1 delivery(ies), {once} bytes"
    assert _line(out, "unreadable cost.jsonl") == "unreadable cost.jsonl line(s) in the store, skipped: 1"


def test_an_undecodable_cost_log_fails_rather_than_reading_as_no_cost(store, capsys):
    (store / cost.COST_FILE).write_bytes(b"\xff\xfe\n")

    code, out, err = _cli(capsys, "cost", "summary", "--session", SESSION, "--store", str(store))

    assert code == 2
    assert "cannot read the store's logs" in out and "cannot read the store's logs" in err
    assert "delivery(ies)" not in out


def test_a_blank_session_is_a_usage_error(store, capsys):
    code, out, _ = _cli(capsys, "cost", "summary", "--session", "  ", "--store", str(store))

    assert code == 2 and "--session needs a session document id" in out


def test_a_session_with_no_document_is_named(store, capsys):
    out = _summary(capsys, store, "20260999-none")

    assert "note: no document with id 20260999-none in the store" in out


# ---------------------------------------------------------------------------
# AC-16.4 -- no savings claimed without a measured alternative
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_baseline", [False, True], ids=["no-baseline", "baseline"])
def test_no_saving_is_claimed_and_the_observed_costs_are_named(store, capsys, with_baseline):
    _cli_ok(capsys, store, "search", QUERY, "--session", SESSION)
    if with_baseline:
        _cli_ok(capsys, store, "cost", "record-baseline", SESSION, "--tokens", "5000")

    out = _summary(capsys, store)

    assert _line(out, "savings:") == (
        "savings: not claimed -- the task was not also done without memory, so nothing measured "
        "can be compared against these costs; a baseline call is a cost of observing, not that "
        "alternative"
    )
    assert [line for line in out.splitlines() if "sav" in line.casefold()] == [_line(out, "savings:")]
    assert _line(out, "search responses:").startswith("search responses: 1 delivery(ies)")


# ---------------------------------------------------------------------------
# ARCH-001 -- a read names and retains the version of exactly the bytes it delivered
# ---------------------------------------------------------------------------


def _cite(out: str) -> str:
    line = _line(out, "cite as (Reuse Log prior-doc):")
    return line.removeprefix("cite as (Reuse Log prior-doc): ")


def test_a_read_after_an_edit_cites_the_new_version_it_delivered(store, capsys):
    searched = _cite(_cli_ok(capsys, store, "search", QUERY, "--session", SESSION))
    search_ref = next(r for r in searched.split(", ") if r.startswith("widget-cache@"))
    _, search_version = versions.split_reference(search_ref)
    search_copy = versions.retained_path(store, "widget-cache", search_version).read_bytes()
    path = store / "sessions" / "widget-cache.md"
    path.write_text(path.read_text(encoding="utf-8").replace("LRU", "LFU"), encoding="utf-8")

    out = _cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION)

    doc_id, version = versions.split_reference(_cite(out))
    assert doc_id == "widget-cache" and version != search_version
    body, problem = versions.retained_body(store, doc_id, version)
    assert problem is None
    assert "We chose LFU eviction" in body and "We chose LFU eviction" in out
    assert versions.retained_path(store, "widget-cache", search_version).read_bytes() == search_copy


def test_a_role_read_cites_the_whole_documents_version(store, capsys):
    whole = _cite(_cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION))

    role = _cite(_cli_ok(capsys, store, "read", "widget-cache", "--role", "decisions", "--session", SESSION))

    assert role == whole
    doc_id, version = versions.split_reference(role)
    assert versions.retained_path(store, doc_id, version).read_bytes() == (
        store / "sessions" / "widget-cache.md"
    ).read_bytes()


def test_the_cite_line_is_part_of_the_reads_delivered_bytes(store, capsys):
    out = _cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION)

    assert out.splitlines()[-1].startswith("cite as (Reuse Log prior-doc): widget-cache@")
    assert _rows(store)[0]["bytes"] == _delivered(out)


def test_a_version_that_cannot_be_retained_is_named_and_no_reference_offered(store, capsys):
    (store / versions.VERSIONS_DIR).write_text("not a directory", encoding="utf-8")

    out = _cli_ok(capsys, store, "read", "widget-cache", "--session", SESSION)

    assert "note: version of 'widget-cache' not retained" in out
    assert "cite as" not in out


# ---------------------------------------------------------------------------
# ARCH-002 -- what a CLI command prints on stderr is named as not counted
# ---------------------------------------------------------------------------


def test_cli_diagnostics_on_stderr_are_named_as_not_counted(store, capsys):
    out = _summary(capsys, store)

    assert "diagnostics a CLI command prints on stderr" in _line(out, "- diagnostics")
