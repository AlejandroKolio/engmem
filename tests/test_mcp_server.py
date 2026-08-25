"""Contract tests for the stdio MCP server (`engmem.mcp_server.serve`).

Transport rule under test throughout: stdout carries newline-delimited JSON-RPC
frames and nothing else. Every test that inspects stdout parses it strictly as
one JSON object per line; a stray non-JSON line anywhere is a protocol violation.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from engmem import __version__, mcp_server
from engmem.mcp_server import serve

PROTOCOL_VERSION = "2025-06-18"


# ---------------------------------------------------------------------------
# The real process stdout must never see a single byte from `serve()` — every
# test in this module runs against this guard so a stray write anywhere fails
# loudly instead of silently landing in a buffer no assertion ever reads (see
# the module docstring). The tests below also pass `serve()` an explicit
# `io.StringIO()` via `_run`/direct calls, which is unaffected — this fixture
# only catches code that reaches the real `sys.stdout` directly instead of
# that injected stream.
#
# Deliberately built on pytest's own `capsys` fixture rather than a
# monkeypatched stand-in object: pytest's capture manager re-establishes its
# own capture object on `sys.stdout` at the setup/call phase boundary, which
# silently discards a plain `monkeypatch.setattr(sys, "stdout", ...)` done
# during fixture setup — tried first, and proven not to work. Reading pytest's
# own capture buffer via `capsys` sidesteps that entirely: whatever pytest
# itself intercepted is exactly what is asserted empty here, regardless of
# which capture object pytest chose.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _real_stdout_must_stay_empty(capsys):
    yield
    captured = capsys.readouterr()
    assert captured.out == "", (
        f"a stray write reached the real process stdout: {captured.out!r} — "
        "MCP output must go only through the injected `stdout` stream"
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stdin_of(*lines: str) -> io.StringIO:
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return io.StringIO(text)


def _stdin_of_messages(*messages: dict) -> io.StringIO:
    return _stdin_of(*(json.dumps(m) for m in messages))


def _run(store: Path, *messages: dict) -> tuple[int, list[dict], str]:
    """Feeds each message as its own line, returns (exit_code, parsed_responses,
    raw_stdout). Every non-empty stdout line MUST be valid JSON — that is checked
    unconditionally here, not left to individual tests to remember."""
    stdin = _stdin_of_messages(*messages)
    stdout = io.StringIO()
    exit_code = serve(store, stdin=stdin, stdout=stdout)
    raw = stdout.getvalue()
    responses = []
    for line in raw.splitlines():
        assert line.strip(), "blank stdout line — must never be written"
        responses.append(json.loads(line))  # raises loudly if a frame is not JSON
    return exit_code, responses, raw


def _initialize_msg(msg_id=1, protocol_version=PROTOCOL_VERSION) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "initialize",
        "params": {"protocolVersion": protocol_version},
    }


def _tools_call_msg(msg_id, *, name="engmem_search", arguments=None) -> dict:
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}


def _prompts_get_msg(msg_id, *, name, arguments=None) -> dict:
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "prompts/get", "params": params}


def _engmem_arg_name(store) -> str:
    """The declared argument name for the `engmem` prompt, discovered the way a
    real client would (via prompts/list) rather than hard-coded, so a rename of
    the underlying argument-hint text does not silently desync these tests."""
    list_msg = {"jsonrpc": "2.0", "id": 999, "method": "prompts/list"}
    _, responses, _ = _run(store, list_msg)
    prompt = next(p for p in responses[0]["result"]["prompts"] if p["name"] == "engmem")
    return prompt["arguments"][0]["name"]


# ---------------------------------------------------------------------------
# fixtures — abstract, invented test data only (no real project
# identifiers); a WidgetCache/CacheWarmer pair mirrors the shape of a real
# engmem session document without naming anything real.
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "20260101-widget-cache.md").write_text(
        """---
id: 20260101-widget-cache
title: WidgetCache rollout
date: 2026-01-01
task_date: 2026-01-01
status: active
tags: [cache]
entities: [WidgetCache]
related: []
covers_files: []
---

## Cold-start primer

WidgetCache serves cached widgets behind CacheWarmer.
""",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def empty_store(tmp_path) -> Path:
    """A store root with no sessions/ directory at all."""
    return tmp_path


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def test_initialize_returns_protocol_version_capabilities_and_server_info(store):
    _, responses, _ = _run(store, _initialize_msg())

    assert len(responses) == 1
    result = responses[0]["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["capabilities"] == {"tools": {}, "prompts": {}}
    assert result["serverInfo"] == {"name": "engmem", "version": __version__}
    assert responses[0]["id"] == 1
    assert responses[0]["jsonrpc"] == "2.0"


def test_initialize_with_unsupported_client_version_does_not_error(store):
    # version negotiation: respond with the version we support, never error
    _, responses, _ = _run(store, _initialize_msg(protocol_version="1999-01-01"))

    assert "error" not in responses[0]
    assert responses[0]["result"]["protocolVersion"] == PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# ping — MUST be answered promptly with an empty result, unconditionally
# (spec 2025-06-18, Utilities/Ping): not gated behind a declared capability.
# Claude Desktop uses this as a liveness probe.
# ---------------------------------------------------------------------------


def test_ping_returns_an_empty_result(store):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    _, responses, _ = _run(store, msg)

    assert responses[0]["result"] == {}
    assert "error" not in responses[0]
    assert responses[0]["id"] == 1


# ---------------------------------------------------------------------------
# notifications/initialized — no id, no response, ever
# ---------------------------------------------------------------------------


def test_notifications_initialized_gets_no_response(store):
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    exit_code, responses, raw = _run(store, notification)

    assert exit_code == 0
    assert responses == []
    assert raw == ""


def test_notifications_initialized_then_a_real_request_still_works(store):
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    _, responses, _ = _run(store, notification, _initialize_msg(msg_id=7))

    assert len(responses) == 1  # the notification produced nothing
    assert responses[0]["id"] == 7


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


def test_tools_list_exposes_engmem_search_first(store):
    """`engmem_search` stays first in the list — the second, role-addressed tool
    (see the role-search tests below) is an addition alongside it, never a
    replacement, and every test that indexes `tools[0]` (e.g. the input-schema
    test right below) depends on this order."""
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    _, responses, _ = _run(store, msg)

    tools = responses[0]["result"]["tools"]
    tool = tools[0]
    assert tool["name"] == "engmem_search"
    assert "prior" in tool["description"].lower() or "session" in tool["description"].lower()
    assert "search" in tool["description"].lower()


def test_tools_list_exposes_exactly_two_read_tools(store):
    """Role-addressed retrieval is a second tool alongside `engmem_search`, not a
    parameter bolted onto it — a model deciding whether the question is about
    subject matter or structure picks between two named tools. (The store's write
    tools — engmem_create_draft, engmem_complete_draft, engmem_mark_superseded —
    are a separate, later addition; see test_mcp_write_tools.py.)"""
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    _, responses, _ = _run(store, msg)

    tools = responses[0]["result"]["tools"]
    names = {tool["name"] for tool in tools}
    read_tool_names = {n for n in names if n in ("engmem_search", "engmem_search_by_role")}
    assert read_tool_names == {"engmem_search", "engmem_search_by_role"}


def test_tools_list_exposes_exactly_the_five_documented_tools(store):
    """A closed inventory check, deliberately separate from the read-only check
    above: every tool this server has ever declared, named once, so a stray sixth
    tool (or a silently dropped one) fails loudly here rather than passing by
    omission."""
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    _, responses, _ = _run(store, msg)

    tools = responses[0]["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {
        "engmem_search",
        "engmem_search_by_role",
        "engmem_create_draft",
        "engmem_complete_draft",
        "engmem_mark_superseded",
    }


def test_tools_list_role_tool_schema_declares_role_enum_and_requires_it(store):
    from engmem.sections import CANONICAL_ROLES

    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    _, responses, _ = _run(store, msg)

    tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
    role_tool = tools["engmem_search_by_role"]
    schema = role_tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["query"]["type"] == "string"
    role_prop = schema["properties"]["role"]
    assert role_prop["type"] == "string"
    assert set(role_prop["enum"]) == set(CANONICAL_ROLES)
    assert set(schema["required"]) == {"query", "role"}
    # the description must help a model decide WHEN to reach for this tool over
    # plain search, not just what it does
    description = role_tool["description"].lower()
    assert "structure" in description or "section" in description


def test_tools_list_input_schema_requires_string_query(store):
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    _, responses, _ = _run(store, msg)

    schema = responses[0]["result"]["tools"][0]["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["required"] == ["query"]


# ---------------------------------------------------------------------------
# tools/call — the happy paths
# ---------------------------------------------------------------------------


def test_tools_call_hit_returns_same_rendered_shape_the_cli_prints(store):
    _, responses, _ = _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    result = responses[0]["result"]
    assert "isError" not in result
    content = result["content"]
    assert content[0]["type"] == "text"
    text = content[0]["text"]
    assert "20260101-widget-cache" in text
    assert "score" in text.lower()
    assert "docs:" in text  # the scoreboard footer rides along, same as the CLI


def test_tools_call_no_match_returns_the_cli_no_match_sentence(store):
    _, responses, _ = _run(
        store, _tools_call_msg(3, arguments={"query": "SomethingNeverIndexed"})
    )

    text = responses[0]["result"]["content"][0]["text"]
    assert "prior context: none found" in text
    assert "docs:" in text


def test_tools_call_result_is_a_single_stdout_line_even_with_multiline_content(store):
    # WidgetCache's rendered result embeds a Cold-start primer line; the JSON
    # encoding must escape that as \n, not a raw newline, or the frame breaks.
    _, _, raw = _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# tools/call — robustness
# ---------------------------------------------------------------------------


def test_tools_call_missing_store_reports_in_result_without_dying(empty_store):
    exit_code, responses, _ = _run(
        empty_store, _tools_call_msg(3, arguments={"query": "WidgetCache"})
    )

    assert exit_code == 0
    result = responses[0]["result"]
    assert result.get("isError") is True
    assert "store not found" in result["content"][0]["text"]
    # the store-missing report is a normal tool RESULT, not a JSON-RPC protocol error
    assert "error" not in responses[0]


def test_tools_call_missing_query_argument_is_invalid_params(store):
    _, responses, _ = _run(store, _tools_call_msg(3, arguments={}))

    error = responses[0]["error"]
    assert error["code"] == -32602


def test_tools_call_blank_query_is_invalid_params(store):
    _, responses, _ = _run(store, _tools_call_msg(3, arguments={"query": "   "}))

    error = responses[0]["error"]
    assert error["code"] == -32602


def test_tools_call_unknown_tool_name_is_invalid_params(store):
    _, responses, _ = _run(
        store, _tools_call_msg(3, name="not_a_real_tool", arguments={"query": "x"})
    )

    error = responses[0]["error"]
    assert error["code"] == -32602


# --- a `name` that is missing entirely (no params, null params, or params of
# the wrong JSON type) must not be reported as "unknown tool: None" — that
# tells a client author they asked for a real tool literally called None.


def test_tools_call_with_no_params_at_all_names_the_missing_field(store):
    msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/call"}
    _, responses, _ = _run(store, msg)

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "name" in error["message"].lower()
    assert "none" not in error["message"].lower()


def test_tools_call_with_null_params_names_the_missing_field(store):
    msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": None}
    _, responses, _ = _run(store, msg)

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "name" in error["message"].lower()
    assert "none" not in error["message"].lower()


def test_tools_call_with_array_params_names_the_missing_field(store):
    msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": [1, 2]}
    _, responses, _ = _run(store, msg)

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "name" in error["message"].lower()
    assert "none" not in error["message"].lower()


# --- a permission failure scanning the store root for stray markdown must
# surface as a tool-level `isError` result, not a JSON-RPC internal error — the
# module's own docstring says a store condition is never a protocol fault. This
# targets only the stray-file scan (`stray_documents`), not `load_store`'s own
# handling of an unreadable `sessions/` directory (a separate concern).


def test_tools_call_unreadable_store_root_scan_is_a_tool_error_not_a_protocol_fault(
    store, monkeypatch
):
    def _boom(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    # the module no longer keeps a private copy of the scan — it calls spine's
    # `stray_documents`, which is the seam this test drives
    monkeypatch.setattr("engmem.mcp_server.stray_documents", _boom)

    exit_code, responses, _ = _run(
        store, _tools_call_msg(3, arguments={"query": "WidgetCache"})
    )

    assert exit_code == 0
    assert "error" not in responses[0]
    result = responses[0]["result"]
    assert result.get("isError") is True
    assert "content" in result


# --- a `tools/call` with no `id` is unanswerable — any response would be
# discarded (notifications get none) — so the (potentially expensive) store
# scan behind it must never run at all.


def test_notification_shaped_tools_call_never_runs_the_store_scan(store, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "engmem.mcp_server._run_search_for_tool",
        lambda *a, **kw: (calls.append((a, kw)), ("", False))[1],
    )
    notification = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "engmem_search", "arguments": {"query": "WidgetCache"}},
    }

    exit_code, responses, raw = _run(store, notification, _initialize_msg(msg_id=20))

    assert calls == []
    assert exit_code == 0
    assert len(responses) == 1
    assert responses[0]["id"] == 20


def test_tools_call_on_broken_store_never_puts_non_json_on_stdout(tmp_path):
    """Covers the three ways a store can be broken at once: missing directory
    (separate test above), malformed YAML, and an unreadable document (a
    directory shadowing the .md filename, per spine.py's own note on that case). None of
    these may crash the loop or corrupt a stdout frame."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "20260101-widget-cache.md").write_text(
        """---
id: 20260101-widget-cache
title: WidgetCache rollout
date: 2026-01-01
task_date: 2026-01-01
status: active
tags: [cache]
entities: [WidgetCache]
---

## Cold-start primer

WidgetCache serves cached widgets.
""",
        encoding="utf-8",
    )
    (sessions / "broken-yaml.md").write_text("---\nid: broken\ntitle: no closing delimiter\n")
    (sessions / "shadow-dir.md").mkdir()  # a directory, not a file — unreadable as a doc

    exit_code, responses, raw = _run(
        tmp_path, _tools_call_msg(3, arguments={"query": "WidgetCache"})
    )

    assert exit_code == 0
    # `_run` already asserts every stdout line parses as JSON; a second explicit
    # check here pins the exact hazard called out in the task: no raw print()
    # leaking from the CLI's error-reporting paths.
    for line in raw.splitlines():
        json.loads(line)
    text = responses[0]["result"]["content"][0]["text"]
    assert "20260101-widget-cache" in text
    assert "failed to load" in text  # both broken docs are reflected in the scoreboard


# ---------------------------------------------------------------------------
# tools/call — engmem_search_by_role (role-addressed retrieval)
# ---------------------------------------------------------------------------


def _role_tools_call_msg(msg_id, *, query, role, session_id=None) -> dict:
    arguments = {"query": query, "role": role}
    if session_id is not None:
        arguments["session_id"] = session_id
    return _tools_call_msg(msg_id, name="engmem_search_by_role", arguments=arguments)


@pytest.fixture
def role_store(tmp_path) -> Path:
    """One document with a Decision Log AND a Lessons Learned section, and one
    matching the same query with neither — the role tool must return only the
    former's role section and skip the latter outright, never pad it."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "20260101-widget-cache.md").write_text(
        """---
id: 20260101-widget-cache
title: WidgetCache rollout
date: 2026-01-01
task_date: 2026-01-01
status: active
tags: [cache]
entities: [WidgetCache]
related: []
covers_files: []
---

## Decision Log

Rejected a second cache tier; the existing WidgetCache read path was fast enough.

## Lessons Learned

CacheWarmer must run before traffic is shifted onto WidgetCache.
""",
        encoding="utf-8",
    )
    (sessions / "20260102-widget-cache-notes.md").write_text(
        """---
id: 20260102-widget-cache-notes
title: WidgetCache follow-up notes
date: 2026-01-02
task_date: 2026-01-02
status: active
tags: [cache]
entities: [WidgetCache]
related: []
covers_files: []
---

## Cold-start primer

Follow-up notes about WidgetCache with no Decision Log section at all.
""",
        encoding="utf-8",
    )
    return tmp_path


def test_role_tool_returns_the_requested_role_section(role_store):
    _, responses, _ = _run(
        role_store, _role_tools_call_msg(3, query="WidgetCache", role="decisions")
    )

    result = responses[0]["result"]
    assert "isError" not in result
    text = result["content"][0]["text"]
    assert "role: decisions" in text
    assert "20260101-widget-cache" in text
    assert "Rejected a second cache tier" in text
    # the sibling document has no Decision Log — it must not appear at all
    assert "20260102-widget-cache-notes" not in text


def test_role_tool_states_when_no_matched_document_has_the_role(role_store):
    _, responses, _ = _run(
        role_store, _role_tools_call_msg(3, query="WidgetCache", role="production")
    )

    text = responses[0]["result"]["content"][0]["text"]
    assert "role: production" in text
    assert "none" in text.lower()


def test_role_tool_missing_role_argument_is_invalid_params(role_store):
    _, responses, _ = _run(
        role_store, _tools_call_msg(3, name="engmem_search_by_role", arguments={"query": "WidgetCache"})
    )

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "role" in error["message"].lower()


def test_role_tool_unknown_role_value_is_invalid_params_naming_the_valid_ones(role_store):
    _, responses, _ = _run(
        role_store,
        _role_tools_call_msg(3, query="WidgetCache", role="not-a-real-role"),
    )

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "decisions" in error["message"]


def test_role_tool_missing_store_reports_in_result_without_dying(empty_store):
    exit_code, responses, _ = _run(
        empty_store, _role_tools_call_msg(3, query="WidgetCache", role="decisions")
    )

    assert exit_code == 0
    result = responses[0]["result"]
    assert result.get("isError") is True
    assert "store not found" in result["content"][0]["text"]


def test_role_tool_result_is_a_single_stdout_line(role_store):
    _, _, raw = _run(
        role_store, _role_tools_call_msg(3, query="WidgetCache", role="decisions")
    )

    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 1
    for line in lines:
        json.loads(line)


def test_role_tool_writes_its_own_telemetry_line_with_role_and_session(role_store):
    _run(
        role_store,
        _role_tools_call_msg(3, query="WidgetCache", role="decisions", session_id="s-1"),
    )

    telemetry = json.loads(
        (role_store / "telemetry.jsonl").read_text().strip().splitlines()[-1]
    )
    assert telemetry["role"] == "decisions"
    assert telemetry["session_id"] == "s-1"
    assert telemetry["surfaced"] == ["20260101-widget-cache"]


# ---------------------------------------------------------------------------
# prompts/list
# ---------------------------------------------------------------------------


def test_prompts_list_exposes_all_three_prompts(store):
    msg = {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"}
    _, responses, _ = _run(store, msg)

    prompts = responses[0]["result"]["prompts"]
    names = {p["name"] for p in prompts}
    assert names == {"engmem", "engmem-save", "engmem-save-quick"}
    for prompt in prompts:
        assert prompt["description"]
        assert "title" in prompt
        assert "arguments" in prompt


def test_prompts_list_descriptions_come_from_front_matter_not_body(store):
    msg = {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"}
    _, responses, _ = _run(store, msg)

    by_name = {p["name"]: p for p in responses[0]["result"]["prompts"]}
    assert "recover prior context" in by_name["engmem"]["description"].lower()


def test_prompts_list_engmem_declares_one_argument_the_others_declare_none(store):
    msg = {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"}
    _, responses, _ = _run(store, msg)

    by_name = {p["name"]: p for p in responses[0]["result"]["prompts"]}
    assert len(by_name["engmem"]["arguments"]) == 1
    arg = by_name["engmem"]["arguments"][0]
    assert arg["name"]
    assert arg["required"] is False
    assert by_name["engmem-save"]["arguments"] == []
    assert by_name["engmem-save-quick"]["arguments"] == []


# ---------------------------------------------------------------------------
# prompts/get
# ---------------------------------------------------------------------------


def test_prompts_get_strips_yaml_front_matter_from_the_body(store):
    _, responses, _ = _run(store, _prompts_get_msg(5, name="engmem-save"))

    text = responses[0]["result"]["messages"][0]["content"]["text"]
    assert not text.lstrip().startswith("---")
    assert "description:" not in text.splitlines()[0]


def test_prompts_get_uses_front_matter_description_as_prompt_description(store):
    _, responses, _ = _run(store, _prompts_get_msg(5, name="engmem-save"))

    result = responses[0]["result"]
    assert "save what was learned" in result["description"].lower()


def test_prompts_get_message_shape_is_a_single_user_text_message(store):
    _, responses, _ = _run(store, _prompts_get_msg(5, name="engmem-save-quick"))

    messages = responses[0]["result"]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"]["type"] == "text"
    assert isinstance(messages[0]["content"]["text"], str)


def test_prompts_get_engmem_substitutes_supplied_argument_into_arguments_placeholder(store):
    msg = _prompts_get_msg(5, name="engmem")
    # discover the declared argument name the same way a real client would
    list_msg = {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"}
    _, list_responses, _ = _run(store, list_msg)
    arg_name = next(
        p for p in list_responses[0]["result"]["prompts"] if p["name"] == "engmem"
    )["arguments"][0]["name"]

    msg["params"]["arguments"] = {arg_name: "roll out WidgetCache eviction"}
    _, responses, _ = _run(store, msg)

    text = responses[0]["result"]["messages"][0]["content"]["text"]
    assert "roll out WidgetCache eviction" in text
    assert "$ARGUMENTS" not in text


def test_prompts_get_engmem_missing_argument_substitutes_empty_not_literal(store):
    _, responses, _ = _run(store, _prompts_get_msg(5, name="engmem"))

    text = responses[0]["result"]["messages"][0]["content"]["text"]
    assert "$ARGUMENTS" not in text
    assert 'Task description: ""' in text


# --- the argument substitution once used a truthiness check (`if value else
# ""`) where an emptiness check belongs, and fell back to Python's `str()`/
# `repr()` for non-string JSON values — leaking `{'a': 1}`-style text into a
# prompt body. Per the MCP spec, `prompts/get` arguments are always strings
# (`Record<string, string>`); a non-string value is a malformed request, the
# same class of problem as `tools/call` missing its required `query` — so it
# is rejected with -32602 rather than coerced or silently emptied.


def test_prompts_get_explicit_empty_string_argument_substitutes_to_empty(store):
    arg_name = _engmem_arg_name(store)
    msg = _prompts_get_msg(5, name="engmem", arguments={arg_name: ""})
    _, responses, _ = _run(store, msg)

    text = responses[0]["result"]["messages"][0]["content"]["text"]
    assert "$ARGUMENTS" not in text
    assert 'Task description: ""' in text


@pytest.mark.parametrize("bad_value", [0, False, {"a": 1}, ["a", "b"]])
def test_prompts_get_non_string_argument_is_rejected_not_stringified(store, bad_value):
    arg_name = _engmem_arg_name(store)
    msg = _prompts_get_msg(5, name="engmem", arguments={arg_name: bad_value})
    _, responses, _ = _run(store, msg)

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "must be a string" in error["message"].lower()
    # never a Python repr of the bad value leaking into the message
    assert repr(bad_value) not in error["message"]


def test_prompts_get_unknown_prompt_name_is_invalid_params(store):
    _, responses, _ = _run(store, _prompts_get_msg(5, name="not-a-real-prompt"))

    error = responses[0]["error"]
    assert error["code"] == -32602


def test_prompts_get_with_string_params_names_the_missing_field(store):
    # `"params": "engmem"` is not a JSON object at all — `name` is missing, not
    # present-but-wrong, so the message must say so rather than claiming
    # "engmem" is an unknown prompt.
    msg = {"jsonrpc": "2.0", "id": 5, "method": "prompts/get", "params": "engmem"}
    _, responses, _ = _run(store, msg)

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "name" in error["message"].lower()
    assert "none" not in error["message"].lower()


# ---------------------------------------------------------------------------
# transport-level robustness
# ---------------------------------------------------------------------------


def test_malformed_json_line_returns_parse_error_and_the_loop_keeps_running(store):
    stdin = _stdin_of("{not valid json", json.dumps(_initialize_msg(msg_id=9)))
    stdout = io.StringIO()

    exit_code = serve(store, stdin=stdin, stdout=stdout)

    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["error"]["code"] == -32700
    second = json.loads(lines[1])
    assert second["id"] == 9
    assert second["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert exit_code == 0


def test_unknown_method_returns_method_not_found_and_the_loop_keeps_running(store):
    unknown = {"jsonrpc": "2.0", "id": 10, "method": "totally/unknown"}
    _, responses, _ = _run(store, unknown, _initialize_msg(msg_id=11))

    assert responses[0]["error"]["code"] == -32601
    assert responses[1]["id"] == 11


def test_blank_and_whitespace_only_lines_produce_no_response(store):
    stdin = _stdin_of("", "   ", "\t", json.dumps(_initialize_msg(msg_id=12)))
    stdout = io.StringIO()

    serve(store, stdin=stdin, stdout=stdout)

    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == 12


def test_stdin_closing_makes_serve_return_cleanly(store):
    stdin = io.StringIO("")  # already at EOF
    stdout = io.StringIO()

    exit_code = serve(store, stdin=stdin, stdout=stdout)

    assert exit_code == 0
    assert stdout.getvalue() == ""


def test_a_deeply_nested_json_line_does_not_crash_the_loop(store):
    """A pathologically nested array parses past json.JSONDecodeError straight into a
    RecursionError (CPython's json module has no depth cap of its own) — this must be
    reported as an ordinary parse error, not left to escape `serve()` and kill the rest
    of the session."""
    deeply_nested = "[" * 100_000 + "]" * 100_000
    stdin = _stdin_of(deeply_nested, json.dumps(_initialize_msg(msg_id=13)))
    stdout = io.StringIO()

    exit_code = serve(store, stdin=stdin, stdout=stdout)

    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["error"]["code"] == -32700
    second = json.loads(lines[1])
    assert second["id"] == 13
    assert exit_code == 0


def test_a_non_object_top_level_message_is_invalid_request(store):
    stdin = _stdin_of(json.dumps([1, 2, 3]))
    stdout = io.StringIO()

    serve(store, stdin=stdin, stdout=stdout)

    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# `jsonrpc` version validation. Decision: enforce it. Claude Desktop always
# sends exactly "2.0", so a real client is never at risk; a wrong or missing
# version is itself the signature of a client that misimplements the protocol,
# and the spec is unambiguous that -32600 is the answer. Treated exactly like
# the existing "missing method" check just above: an id gets an error
# response, a notification-shaped message gets silence, never a normal result.
# ---------------------------------------------------------------------------


def test_jsonrpc_version_1_0_is_invalid_request(store):
    msg = {"jsonrpc": "1.0", "id": 1, "method": "initialize", "params": {}}
    _, responses, _ = _run(store, msg)

    assert responses[0]["error"]["code"] == -32600


def test_jsonrpc_version_wrong_type_is_invalid_request(store):
    msg = {"jsonrpc": 99, "id": 1, "method": "initialize", "params": {}}
    _, responses, _ = _run(store, msg)

    assert responses[0]["error"]["code"] == -32600


def test_missing_jsonrpc_field_is_invalid_request(store):
    msg = {"id": 1, "method": "initialize", "params": {}}
    _, responses, _ = _run(store, msg)

    assert responses[0]["error"]["code"] == -32600


def test_jsonrpc_wrong_version_notification_produces_no_response(store):
    msg = {"jsonrpc": "1.0", "method": "notifications/initialized"}
    exit_code, responses, raw = _run(store, msg)

    assert exit_code == 0
    assert responses == []
    assert raw == ""


# ---------------------------------------------------------------------------
# `id` type validation: JSON-RPC 2.0 restricts `id` to string, number, or
# null. Per spec, when the id itself cannot be trusted the error response's id
# MUST be null, not an echo of the malformed value.
# ---------------------------------------------------------------------------


def test_object_id_is_invalid_request_with_a_null_id_in_the_response(store):
    msg = {"jsonrpc": "2.0", "id": {"a": 1}, "method": "initialize", "params": {}}
    _, responses, _ = _run(store, msg)

    assert responses[0]["error"]["code"] == -32600
    assert responses[0]["id"] is None


def test_array_id_is_invalid_request(store):
    msg = {"jsonrpc": "2.0", "id": [1, 2], "method": "initialize", "params": {}}
    _, responses, _ = _run(store, msg)

    assert responses[0]["error"]["code"] == -32600


def test_falsy_ids_zero_and_empty_string_are_still_handled_correctly(store):
    # Regression guard: `"id" in message` must keep being used to detect
    # presence, never truthiness — `0` and `""` are valid ids.
    _, zero_responses, _ = _run(store, _initialize_msg(msg_id=0))
    assert zero_responses[0]["id"] == 0

    _, blank_responses, _ = _run(store, _initialize_msg(msg_id=""))
    assert blank_responses[0]["id"] == ""


# ---------------------------------------------------------------------------
# A broken downstream pipe (the client process exited, e.g. a `| head -1`
# reader) must end the loop quietly, not crash with a traceback: the client is
# already gone, so nothing is corrupted by stopping.
# ---------------------------------------------------------------------------


class _BrokenPipeStdout:
    """Stands in for a stdout whose downstream reader has closed the pipe."""

    def write(self, data: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def test_broken_pipe_while_writing_exits_cleanly_without_raising(store):
    stdin = _stdin_of_messages(_initialize_msg())

    exit_code = serve(store, stdin=stdin, stdout=_BrokenPipeStdout())

    assert exit_code == 0


def test_tool_result_reports_an_unreadable_subdirectory(tmp_path, monkeypatch):
    """The CLI names an unlistable directory; the MCP tool asked the same question
    through its own private copy of the scan and silently answered "none"."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    (store / "sessions" / "widget-cache.md").write_text(
        "---\nid: widget-cache\ntitle: Widget Cache\ndate: 2026-05-04\n"
        "entities: [WidgetCache]\n---\n\n## Pre-reg\n\nBody.\n",
        encoding="utf-8",
    )

    def boom(_store):
        return [], [f"{_store}/sessions/archive: cannot list directory for stray documents"]

    monkeypatch.setattr("engmem.mcp_server.stray_documents", boom)

    text, is_error = mcp_server._run_search_for_tool(store, "WidgetCache")

    assert "cannot list directory" in text


# ---------------------------------------------------------------------------
# telemetry — the MCP path is inside the experiment (ENGMEM-SPEC.md §1): a search
# that leaves no row makes the session invisible to the Gate 1 analysis
# ---------------------------------------------------------------------------


def _telemetry_lines(store: Path) -> list[dict]:
    path = store / "telemetry.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_tools_call_writes_a_telemetry_line(store):
    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    rows = _telemetry_lines(store)
    assert len(rows) == 1
    assert rows[0]["query"] == "WidgetCache"
    assert rows[0]["result"] == "hit"
    assert rows[0]["surfaced"] == ["20260101-widget-cache"]


def test_tools_call_records_the_session_id_argument(store):
    _run(
        store,
        _tools_call_msg(
            3, arguments={"query": "WidgetCache", "session_id": "20260823-draft"}
        ),
    )

    assert _telemetry_lines(store)[0]["session_id"] == "20260823-draft"


def test_tools_call_without_session_id_logs_an_explicit_null(store):
    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    row = _telemetry_lines(store)[0]
    assert "session_id" in row and row["session_id"] is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_session_id_is_absent_not_an_unknown_session(store, blank):
    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache", "session_id": blank}))

    assert _telemetry_lines(store)[0]["session_id"] is None


def test_session_id_is_stripped_the_same_way_the_cli_strips_it(store):
    _run(
        store,
        _tools_call_msg(
            3, arguments={"query": "WidgetCache", "session_id": "  20260823-draft  "}
        ),
    )

    assert _telemetry_lines(store)[0]["session_id"] == "20260823-draft"


@pytest.mark.parametrize("bad_value", [0, False, {"a": 1}, ["x"]])
def test_non_string_session_id_is_rejected_not_stringified(store, bad_value):
    """Same rule as `query` and as prompts/get arguments: a wrongly-typed value is a
    malformed request, not something to coerce — `str()` on a dict would write Python
    repr syntax straight into the experiment's own log."""
    _, responses, _ = _run(
        store,
        _tools_call_msg(3, arguments={"query": "WidgetCache", "session_id": bad_value}),
    )

    assert responses[0]["error"]["code"] == -32602
    assert "session_id" in responses[0]["error"]["message"]
    assert _telemetry_lines(store) == [], "a rejected call must not be logged as a search"


def test_a_search_that_found_nothing_is_still_logged(store):
    _run(store, _tools_call_msg(3, arguments={"query": "SomethingNeverIndexed"}))

    rows = _telemetry_lines(store)
    assert len(rows) == 1
    assert rows[0]["result"] == "miss"
    assert rows[0]["surfaced"] == []


def test_missing_store_is_not_logged_as_a_search(empty_store):
    """Mirrors the CLI, which exits before logging: no store means no measurement, and
    a row here would inflate the search count with runs that never happened."""
    _run(empty_store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    assert _telemetry_lines(empty_store) == []


def test_telemetry_failure_note_reaches_the_agent_without_breaking_the_protocol(store):
    """The CLI prints this note to stdout because that is what its reader reads. Here
    stdout carries JSON-RPC frames only, so the note must travel inside the tool result
    text — `_run` already asserts every stdout line parses as JSON."""
    (store / "telemetry.jsonl").mkdir()

    _, responses, _ = _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    text = responses[0]["result"]["content"][0]["text"]
    assert "note: telemetry not recorded" in text
    assert "20260101-widget-cache" in text, "the search result itself must still be complete"
    assert "isError" not in responses[0]["result"], "the search succeeded; only the log failed"


def test_tools_list_declares_session_id_as_an_optional_string(store):
    _, responses, _ = _run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    schema = responses[0]["result"]["tools"][0]["inputSchema"]
    assert schema["properties"]["session_id"]["type"] == "string"
    assert schema["required"] == ["query"], "session_id must stay optional"


# ---------------------------------------------------------------------------
# `channel` + `context_bytes` — the MCP side of both. `channel` must always
# read "mcp" for a search run through the tool-call path, distinguishing this
# population from a CLI search in the same `telemetry.jsonl` file.
# `context_bytes` must be the exact UTF-8 byte count of the document-derived
# render the caller actually received — computed the same way as the CLI's,
# so the number means the same thing regardless of which channel produced it.
# ---------------------------------------------------------------------------


def test_tools_call_records_mcp_as_the_channel(store):
    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    assert _telemetry_lines(store)[0]["channel"] == "mcp"


def test_role_tool_records_mcp_as_the_channel(role_store):
    _run(role_store, _role_tools_call_msg(3, query="WidgetCache", role="decisions"))

    telemetry = json.loads(
        (role_store / "telemetry.jsonl").read_text().strip().splitlines()[-1]
    )
    assert telemetry["channel"] == "mcp"


def test_tools_call_context_bytes_matches_the_rendered_result_for_a_hit(store):
    from engmem.output import render_search_results
    from engmem.scoring import search as run_search
    from engmem.spine import load_store

    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    docs = load_store(store / "sessions").docs
    outcome = run_search(docs, "WidgetCache")
    expected_text = render_search_results(outcome, docs)

    assert _telemetry_lines(store)[0]["context_bytes"] == len(expected_text.encode("utf-8"))


def test_tools_call_context_bytes_is_near_zero_for_a_miss(store):
    from engmem.output import render_no_match

    _run(store, _tools_call_msg(3, arguments={"query": "SomethingNeverIndexed"}))

    row = _telemetry_lines(store)[0]
    expected = len(render_no_match().encode("utf-8"))
    assert row["context_bytes"] == expected
    assert row["context_bytes"] < 50, "a miss must put almost nothing in context"


def test_tools_call_context_bytes_excludes_the_stray_files_note(store):
    """A store with stray markdown files appends a note to the tool result text on a
    miss — that note is store housekeeping, not prior-document content, and must not
    inflate `context_bytes` (mirrors the CLI's same exclusion)."""
    from engmem.output import render_no_match

    (store / "some-stray-notes.md").write_text("not in sessions/", encoding="utf-8")

    _, responses, _ = _run(
        store, _tools_call_msg(3, arguments={"query": "SomethingNeverIndexed"})
    )
    text = responses[0]["result"]["content"][0]["text"]
    assert "sit outside the searched set" in text, "the stray note must still render"

    row = _telemetry_lines(store)[0]
    assert row["context_bytes"] == len(render_no_match().encode("utf-8"))


def test_role_tool_context_bytes_matches_the_rendered_role_result(role_store):
    from engmem.output import render_role_search_results
    from engmem.scoring import search_with_role_sections
    from engmem.spine import load_store

    _run(role_store, _role_tools_call_msg(3, query="WidgetCache", role="decisions"))

    docs = load_store(role_store / "sessions").docs
    outcome, role_map = search_with_role_sections(docs, "WidgetCache")
    expected_text = render_role_search_results(outcome, role_map, "decisions")

    telemetry = json.loads(
        (role_store / "telemetry.jsonl").read_text().strip().splitlines()[-1]
    )
    assert telemetry["context_bytes"] == len(expected_text.encode("utf-8"))


def test_tools_call_records_a_token_estimate_derived_from_context_bytes(store):
    from engmem.telemetry import estimate_tokens

    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    row = _telemetry_lines(store)[0]
    assert row["context_tokens_estimate"] == estimate_tokens(row["context_bytes"])
