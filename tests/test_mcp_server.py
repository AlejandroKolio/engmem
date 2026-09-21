"""Contract tests for the stdio MCP server: stdout carries newline-delimited JSON-RPC frames and
nothing else."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from conftest import requires_permission_enforcement
from mcp_harness import (
    PROTOCOL_VERSION,
    _initialize_msg,
    _prompts_get_msg,
    _result_text,
    _role_tools_call_msg,
    _run,
    _stdin_of,
    _stdin_of_messages,
    _telemetry_lines,
    _tools_call_msg,
)

from engmem import __version__, mcp_server
from engmem.mcp_server import serve
from engmem.output import (
    render_no_match,
    render_role_search_results,
    render_search_results,
)
from engmem.scoring import search as run_search, search_with_role_sections
from engmem.sections import CANONICAL_ROLES
from engmem.spine import load_store
from engmem.telemetry import estimate_tokens


# ---------------------------------------------------------------------------
# The real process stdout must never see a single byte from `serve()` — every test in this module
# runs against this guard so a stray write anywhere fails loudly instead of silently landing in a
# buffer no assertion ever reads (see the module docstring).
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
# helpers — the JSON-RPC plumbing itself lives in mcp_harness.py, shared with
# test_mcp_write_tools.py
# ---------------------------------------------------------------------------


def _engmem_arg_name(store) -> str:
    """Discovered via prompts/list the way a real client would, so an argument rename cannot
    silently desync these tests."""
    list_msg = {"jsonrpc": "2.0", "id": 999, "method": "prompts/list"}
    _, responses, _ = _run(store, list_msg)
    prompt = next(p for p in responses[0]["result"]["prompts"] if p["name"] == "engmem")
    return prompt["arguments"][0]["name"]


def _search_call(session_id=None) -> dict:
    arguments = {"query": "WidgetCache"}
    if session_id is not None:
        arguments["session_id"] = session_id
    return _tools_call_msg(3, arguments=arguments)


def _role_search_call(session_id=None) -> dict:
    return _role_tools_call_msg(3, query="WidgetCache", role="decisions", session_id=session_id)


# The two read tools answer the same contract questions; each row names the store fixture its
# call needs and the builder for the call itself.
_BOTH_READ_TOOLS = [
    pytest.param("store", _search_call, id="search"),
    pytest.param("role_store", _role_search_call, id="role_search"),
]


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
# ping — MUST be answered promptly with an empty result, unconditionally (spec 2025-06-18,
# Utilities/Ping): not gated behind a declared capability.
# ---------------------------------------------------------------------------


def test_ping_returns_an_empty_result(store):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    _, responses, _ = _run(store, msg)

    assert responses[0]["result"] == {}
    assert "error" not in responses[0]
    assert responses[0]["id"] == 1


# ---------------------------------------------------------------------------
# a message with no id is a notification — no response, ever, whatever else is wrong with it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "notification",
    [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        # no "method", no "id" — unanswerable either way
        {"jsonrpc": "2.0", "params": {}},
        {"jsonrpc": "1.0", "method": "notifications/initialized"},
    ],
    ids=["valid_notification", "no_method", "wrong_jsonrpc_version"],
)
def test_a_notification_shaped_message_gets_no_response(store, notification):
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
    """`engmem_search` stays first: every test that indexes `tools[0]` depends on this order."""
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    _, responses, _ = _run(store, msg)

    tools = responses[0]["result"]["tools"]
    tool = tools[0]
    assert tool["name"] == "engmem_search"
    assert "prior" in tool["description"].lower() or "session" in tool["description"].lower()
    assert "search" in tool["description"].lower()


def test_tools_list_exposes_exactly_the_five_documented_tools(store):
    """A closed inventory, so a stray sixth tool or a silently dropped one fails loudly rather
    than passing by omission. Role-addressed retrieval is a second named tool of its own, not a
    parameter bolted onto the first."""
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


def test_tools_list_input_schema_requires_string_query_and_leaves_session_id_optional(store):
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    _, responses, _ = _run(store, msg)

    schema = responses[0]["result"]["tools"][0]["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["session_id"]["type"] == "string"
    assert schema["required"] == ["query"], "session_id must stay optional"


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


@pytest.mark.parametrize("store_fixture, build_call", _BOTH_READ_TOOLS)
def test_a_tool_result_is_a_single_stdout_line_even_with_multiline_content(
    request, store_fixture, build_call
):
    # a rendered result embeds whole section lines; the JSON encoding must escape those as \n,
    # not raw newlines, or the frame breaks
    _, _, raw = _run(request.getfixturevalue(store_fixture), build_call())

    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# tools/call — robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message", [_search_call(), _role_search_call()], ids=["search", "role_search"]
)
def test_a_missing_store_is_reported_in_the_result_without_dying(empty_store, message):
    exit_code, responses, _ = _run(empty_store, message)

    assert exit_code == 0
    result = responses[0]["result"]
    assert result.get("isError") is True
    assert "store not found" in result["content"][0]["text"]
    # the store-missing report is a normal tool RESULT, not a JSON-RPC protocol error
    assert "error" not in responses[0]


@pytest.mark.parametrize(
    "message",
    [
        _tools_call_msg(3, arguments={}),
        _tools_call_msg(3, arguments={"query": "   "}),
        _tools_call_msg(3, name="not_a_real_tool", arguments={"query": "x"}),
        _prompts_get_msg(5, name="not-a-real-prompt"),
    ],
    ids=["missing_query", "blank_query", "unknown_tool", "unknown_prompt"],
)
def test_a_request_the_schema_does_not_admit_is_invalid_params(store, message):
    _, responses, _ = _run(store, message)

    assert responses[0]["error"]["code"] == -32602


# --- a `name` that is missing entirely (no params, null params, or params of the wrong JSON
# type) must not be reported as "unknown tool: None" or "unknown prompt: 'engmem'" — either
# tells a client author they asked for something that exists under another name.

_NO_PARAMS_KEY = object()  # sentinel: omit the "params" key entirely, vs. explicit params=None


@pytest.mark.parametrize("method", ["tools/call", "prompts/get"])
@pytest.mark.parametrize(
    "params",
    [_NO_PARAMS_KEY, None, [1, 2], "engmem"],
    ids=["omitted", "null", "array", "string"],
)
def test_malformed_params_name_the_missing_field(store, method, params):
    msg = {"jsonrpc": "2.0", "id": 3, "method": method}
    if params is not _NO_PARAMS_KEY:
        msg["params"] = params
    _, responses, _ = _run(store, msg)

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "name" in error["message"].lower()
    # never "unknown tool: None" or "unknown prompt: 'engmem'" — `name` is missing, not
    # present-but-wrong
    assert "none" not in error["message"].lower()
    assert "unknown" not in error["message"].lower()


# --- a permission failure scanning the store root for stray markdown must surface as a tool-level
# `isError` result, not a JSON-RPC internal error — the module's own docstring says a store
# condition is never a protocol fault.


def test_tools_call_unreadable_store_root_scan_is_a_tool_error_not_a_protocol_fault(
    store, monkeypatch
):
    def _boom(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    # spine's `stray_documents` is the seam the scan runs through, and the one this test drives
    monkeypatch.setattr("engmem.mcp_server.stray_documents", _boom)

    exit_code, responses, _ = _run(
        store, _tools_call_msg(3, arguments={"query": "WidgetCache"})
    )

    assert exit_code == 0
    assert "error" not in responses[0]
    result = responses[0]["result"]
    assert result.get("isError") is True
    assert "content" in result


def test_tools_call_prints_load_store_warnings_to_stderr(store, capsys):
    """Mirrors the errors loop right above it in `_load_store_for_tool`: warnings take the same
    path to stderr."""
    (store / "sessions" / "20260103-no-entities.md").write_text(
        """---
id: 20260103-no-entities
title: No entities doc
date: 2026-01-03
task_date: 2026-01-03
status: active
tags: []
entities: []
related: []
covers_files: []
---

## Cold-start primer

Body text with no entities listed.
""",
        encoding="utf-8",
    )

    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    # asserted here, not drained and left for the autouse fixture: draining capsys inside the
    # test would make `_real_stdout_must_stay_empty`'s teardown check pass vacuously
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "entities is empty" in captured.err


# --- a `tools/call` with no `id` is unanswerable — any response would be discarded (notifications
# get none) — so the (potentially expensive) store scan behind it must never run at all.


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
    """Missing directory, malformed YAML and an unreadable document at once — none may crash the
    loop or corrupt a frame."""
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
    # `_run` already asserts every stdout line parses as JSON; the explicit check here pins the
    # hazard this store shape creates — a raw print() leaking from an error-reporting path.
    for line in raw.splitlines():
        json.loads(line)
    text = responses[0]["result"]["content"][0]["text"]
    assert "20260101-widget-cache" in text
    assert "failed to load" in text  # both broken docs are reflected in the scoreboard


# ---------------------------------------------------------------------------
# tools/call — engmem_search_by_role (role-addressed retrieval)
# ---------------------------------------------------------------------------


@pytest.fixture
def role_store(tmp_path) -> Path:
    """One document carrying the role and one matching the same query without it, which must be
    skipped rather than padded."""
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


@pytest.mark.parametrize(
    "arguments, expected_substring",
    [
        ({"query": "WidgetCache"}, " (missing)"),
        ({"query": "WidgetCache", "role": "not-a-real-role"}, " (got 'not-a-real-role')"),
    ],
    ids=["missing_role", "unknown_role_value_names_valid_ones"],
)
def test_role_tool_bad_role_argument_is_invalid_params(role_store, arguments, expected_substring):
    _, responses, _ = _run(
        role_store, _tools_call_msg(3, name="engmem_search_by_role", arguments=arguments)
    )

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert expected_substring in error["message"].lower()


def test_role_tool_writes_its_own_telemetry_line_with_role_and_session(role_store):
    _run(
        role_store,
        _role_tools_call_msg(3, query="WidgetCache", role="decisions", session_id="s-1"),
    )

    telemetry = _telemetry_lines(role_store)[-1]
    assert telemetry["role"] == "decisions"
    assert telemetry["session_id"] == "s-1"
    assert telemetry["surfaced"] == ["20260101-widget-cache"]


def test_role_tool_notes_stray_files_when_no_role_hits(role_store):
    (role_store / "some-stray-notes.md").write_text("not in sessions/", encoding="utf-8")

    _, responses, _ = _run(
        role_store, _role_tools_call_msg(3, query="WidgetCache", role="production")
    )

    text = responses[0]["result"]["content"][0]["text"]
    assert "sit outside the searched set" in text


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


@pytest.mark.parametrize(
    "prompt_name, expected_phrase",
    [("engmem", "recover prior context"), ("engmem-save", "save what was learned")],
)
def test_prompt_descriptions_come_from_front_matter_not_body(store, prompt_name, expected_phrase):
    """Both listings read the same front matter, so neither may fall back to the body's first
    line."""
    list_msg = {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"}
    _, list_responses, _ = _run(store, list_msg)
    _, get_responses, _ = _run(store, _prompts_get_msg(5, name=prompt_name))

    listed = {p["name"]: p for p in list_responses[0]["result"]["prompts"]}[prompt_name]
    assert expected_phrase in listed["description"].lower()
    assert expected_phrase in get_responses[0]["result"]["description"].lower()


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


def test_prompts_get_message_shape_is_a_single_user_text_message(store):
    _, responses, _ = _run(store, _prompts_get_msg(5, name="engmem-save-quick"))

    messages = responses[0]["result"]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"]["type"] == "text"
    assert isinstance(messages[0]["content"]["text"], str)


def test_prompts_get_engmem_substitutes_supplied_argument_into_arguments_placeholder(store):
    arg_name = _engmem_arg_name(store)
    msg = _prompts_get_msg(
        5, name="engmem", arguments={arg_name: "roll out WidgetCache eviction"}
    )
    _, responses, _ = _run(store, msg)

    text = responses[0]["result"]["messages"][0]["content"]["text"]
    assert "roll out WidgetCache eviction" in text
    assert "$ARGUMENTS" not in text


# --- the argument substitution once used a truthiness check (`if value else ""`) where an
# emptiness check belongs, and fell back to Python's `str()`/ `repr()` for non-string JSON values
# — leaking `{'a': 1}`-style text into a prompt body.


@pytest.mark.parametrize("supplied", [None, ""], ids=["absent", "explicit_empty_string"])
def test_prompts_get_an_absent_or_empty_argument_substitutes_to_empty_not_the_literal(
    store, supplied
):
    arguments = None if supplied is None else {_engmem_arg_name(store): supplied}
    _, responses, _ = _run(store, _prompts_get_msg(5, name="engmem", arguments=arguments))

    text = responses[0]["result"]["messages"][0]["content"]["text"]
    assert "$ARGUMENTS" not in text
    assert 'Task description: ""' in text


@pytest.mark.parametrize(
    "bad_value", [0, False, {"a": 1}, ["a", "b"]], ids=["int", "bool", "dict", "list"]
)
def test_prompts_get_non_string_argument_is_rejected_not_stringified(store, bad_value):
    arg_name = _engmem_arg_name(store)
    msg = _prompts_get_msg(5, name="engmem", arguments={arg_name: bad_value})
    _, responses, _ = _run(store, msg)

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "must be a string" in error["message"].lower()
    # never a Python repr of the bad value leaking into the message
    assert repr(bad_value) not in error["message"]


# ---------------------------------------------------------------------------
# `_load_template` defensive branches: a shipped template (install.py's own
# templates/*.md) always has well-formed mapping front matter, so these three
# shapes only arise from a corrupted or hand-edited install — not a live user
# action, but a real state the file on disk can end up in.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content, expected_body",
    [
        pytest.param(
            "no front matter delimiter at all\n",
            "no front matter delimiter at all\n",
            id="missing_delimiter",
        ),
        pytest.param(
            "---\nunclosed: front matter\n",
            "---\nunclosed: front matter\n",
            id="unclosed_delimiter",
        ),
        pytest.param(
            "---\n- just\n- a\n- list\n---\nbody\n",
            "body\n",
            id="non_mapping_front_matter",
        ),
    ],
)
def test_load_template_falls_back_on_a_malformed_template_file(
    monkeypatch, tmp_path, content, expected_body
):
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "broken.md").write_text(content, encoding="utf-8")
    monkeypatch.setattr(mcp_server.resources, "files", lambda pkg: tmp_path)

    description, argument_hint, body = mcp_server._load_template("broken.md")

    assert description == ""
    assert argument_hint is None
    assert body == expected_body


@pytest.mark.parametrize(
    "bad_name",
    [{"engmem": 1}, ["engmem"], 7, None],
    ids=["object", "array", "number", "null"],
)
def test_prompts_get_non_string_name_is_invalid_params_not_an_internal_error(store, bad_name):
    # an object or array `name` is unhashable: looked up in PROMPT_TEMPLATES it raises
    # TypeError, which the last-resort guard would report as -32603 — a client's malformed
    # request dressed up as a server bug
    _, responses, _ = _run(store, _prompts_get_msg(5, name=bad_name))

    error = responses[0]["error"]
    assert error["code"] == -32602
    assert "unknown prompt" in error["message"]


# ---------------------------------------------------------------------------
# transport-level robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unparseable_line",
    [
        "{not valid json",
        # a pathologically nested array raises RecursionError past json.JSONDecodeError
        "[" * 100_000 + "]" * 100_000,
    ],
    ids=["syntax_error", "deeply_nested"],
)
def test_an_unparseable_line_returns_parse_error_and_the_loop_keeps_running(
    store, unparseable_line
):
    stdin = _stdin_of(unparseable_line, json.dumps(_initialize_msg(msg_id=9)))
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


# ---------------------------------------------------------------------------
# UTF-8 transport. `\ud800` is well-formed JSON syntax that `json.loads` accepts, but the string
# it produces cannot be encoded as UTF-8 at all — so it can be neither written to a document nor
# echoed back inside a frame a strict client will parse. It has to be refused at the envelope.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": "\ud800", "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": "prompts/get", "params": {"name": "\ud800"}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "engmem_search", "arguments": {"query": "cache \udfff"}},
        },
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"\ud800": "value"}},
    ],
    ids=["in_the_id", "in_a_prompt_name", "in_a_tool_argument", "in_a_params_key"],
)
def test_an_unpaired_surrogate_anywhere_in_a_message_is_invalid_request(store, message):
    stdin = _stdin_of(json.dumps(message), json.dumps(_initialize_msg(msg_id=14)))
    stdout = io.StringIO()

    exit_code = serve(store, stdin=stdin, stdout=stdout)

    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["error"]["code"] == -32600
    assert "surrogate" in first["error"]["message"]
    # the id may itself be the unencodable value, so it is never echoed
    assert first["id"] is None
    # every stdout frame must survive a strict re-encode, not just Python's tolerant json
    assert lines[0].encode("utf-8")
    assert json.loads(lines[1])["id"] == 14, "the loop must keep serving the next request"
    assert exit_code == 0


def test_a_valid_surrogate_pair_is_not_mistaken_for_an_unpaired_one(store):
    """Guard against over-rejection: an astral character arrives as two `\\u` escapes that the
    decoder joins into one code point, which encodes to UTF-8 perfectly well."""
    msg = _tools_call_msg(15, arguments={"query": "widget \U0001f600 cache"})
    _, responses, _ = _run(store, msg)

    assert "error" not in responses[0], responses[0]
    assert responses[0]["result"]["content"][0]["text"]


def test_a_non_object_top_level_message_is_invalid_request(store):
    stdin = _stdin_of(json.dumps([1, 2, 3]))
    stdout = io.StringIO()

    serve(store, stdin=stdin, stdout=stdout)

    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# `jsonrpc` version validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "1.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": 99, "id": 1, "method": "initialize", "params": {}},
        {"id": 1, "method": "initialize", "params": {}},
    ],
    ids=["wrong_version_string", "wrong_version_type", "field_missing"],
)
def test_invalid_jsonrpc_field_is_invalid_request(store, message):
    _, responses, _ = _run(store, message)

    assert responses[0]["error"]["code"] == -32600


# --- a request-shaped message with no usable `method` at all is answered, where the same
# message without an id is not (see the notification table above).


@pytest.mark.parametrize(
    "method",
    [None, "", 123],
    ids=["absent", "empty_string", "wrong_type"],
)
def test_missing_or_malformed_method_with_an_id_is_invalid_request(store, method):
    msg = {"jsonrpc": "2.0", "id": 1, "params": {}}
    if method is not None:
        msg["method"] = method
    _, responses, _ = _run(store, msg)

    assert responses[0]["error"]["code"] == -32600
    assert "method" in responses[0]["error"]["message"].lower()


# ---------------------------------------------------------------------------
# `id` type validation: JSON-RPC 2.0 restricts `id` to string, number, or null.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    # `isinstance(True, int)` is True, so a plain `(str, int, float)` check waves `id: true`
    # through and echoes it back as an id JSON-RPC 2.0 does not allow
    [{"a": 1}, [1, 2], True],
    ids=["object_id", "array_id", "boolean_id"],
)
def test_a_non_scalar_id_is_invalid_request_with_a_null_id_in_the_response(store, bad_id):
    msg = {"jsonrpc": "2.0", "id": bad_id, "method": "initialize", "params": {}}
    _, responses, _ = _run(store, msg)

    assert responses[0]["error"]["code"] == -32600
    # the error's own id is null, never an echo of an id that could not be trusted in the first place
    assert responses[0]["id"] is None


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


class _BrokenPipeStdoutOnARealFd(_BrokenPipeStdout):
    """The same dead stream, but backed by a real file descriptor, the way `sys.stdout` is."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


def test_broken_pipe_teardown_leaves_the_stdout_fd_pointing_somewhere_harmless(store):
    """The frame that failed to send is still in stdout's buffer, and interpreter shutdown flushes
    it once more; on a descriptor left pointing at the dead pipe that flush fails and CPython exits
    120 instead of 0."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    stdin = _stdin_of_messages(_initialize_msg())

    try:
        exit_code = serve(store, stdin=stdin, stdout=_BrokenPipeStdoutOnARealFd(write_fd))

        assert exit_code == 0
        assert os.write(write_fd, b"a late flush must land somewhere harmless\n") > 0
    finally:
        os.close(write_fd)


@pytest.mark.parametrize(
    "run_for_tool",
    [
        pytest.param(
            lambda store: mcp_server._run_search_for_tool(store, "WidgetCache"), id="search"
        ),
        pytest.param(
            lambda store: mcp_server._run_role_search_for_tool(store, "WidgetCache", "decisions"),
            id="role_search",
        ),
    ],
)
def test_a_tool_result_reports_an_unreadable_subdirectory(tmp_path, monkeypatch, run_for_tool):
    """A subdirectory the scan could not list must be said out loud: "could not check" and "found
    nothing" are different answers."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    (store / "sessions" / "widget-cache.md").write_text(
        "---\nid: widget-cache\ntitle: Widget Cache\ndate: 2026-05-04\n"
        "entities: [WidgetCache]\n---\n\n## Decision Log\n\nBody.\n",
        encoding="utf-8",
    )

    def boom(_store):
        return [], [f"{_store}/sessions/archive: cannot list directory for stray documents"]

    monkeypatch.setattr("engmem.mcp_server.stray_documents", boom)

    text, _is_error = run_for_tool(store)

    assert "cannot list directory" in text


# ---------------------------------------------------------------------------
# telemetry — the MCP path is inside the experiment (ENGMEM-SPEC.md §1): a search
# that leaves no row makes the session invisible to the Gate 1 analysis
# ---------------------------------------------------------------------------


def test_tools_call_writes_a_telemetry_line(store):
    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    rows = _telemetry_lines(store)
    assert len(rows) == 1
    assert rows[0]["query"] == "WidgetCache"
    assert rows[0]["result"] == "hit"
    assert rows[0]["surfaced"] == ["20260101-widget-cache"]


@pytest.mark.parametrize(
    "session_id, logged",
    [
        pytest.param(None, None, id="absent"),
        pytest.param("", None, id="empty"),
        pytest.param("   ", None, id="blank"),
        pytest.param("  20260823-draft  ", "20260823-draft", id="padded"),
        pytest.param("20260823-draft", "20260823-draft", id="plain"),
    ],
)
def test_the_session_id_argument_is_logged_the_same_way_the_cli_logs_it(store, session_id, logged):
    """Blank is absent, not an unknown session, and the value is stripped — both mirroring
    cli.py's `_session_id`. The key is always present, so an unattributed row is explicit."""
    _run(store, _search_call(session_id))

    row = _telemetry_lines(store)[0]
    assert "session_id" in row
    assert row["session_id"] == logged


UNATTRIBUTED_NOTE = "note: unattributed search — pass session_id <draft-id>"


@pytest.mark.parametrize("store_fixture, build_call", _BOTH_READ_TOOLS)
@pytest.mark.parametrize(
    "session_id, note_expected",
    [
        pytest.param(None, True, id="absent"),
        pytest.param("", True, id="empty"),
        pytest.param("   ", True, id="blank"),
        pytest.param("20260823-draft", False, id="present"),
    ],
)
def test_the_unattributed_note_ends_exactly_the_searches_with_no_session_id(
    request, store_fixture, build_call, session_id, note_expected
):
    _, responses, _ = _run(request.getfixturevalue(store_fixture), build_call(session_id))

    text = _result_text(responses)
    lines = text.rstrip("\n").splitlines()
    if note_expected:
        assert lines[-1] == UNATTRIBUTED_NOTE
        assert lines[-2].startswith("docs: ")
    else:
        assert UNATTRIBUTED_NOTE not in text
    assert "isError" not in responses[0]["result"]


def test_the_unattributed_note_comes_after_the_telemetry_failure_note(store):
    (store / "telemetry.jsonl").mkdir()

    _, responses, _ = _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    tail = _result_text(responses).rstrip("\n").splitlines()[-3:]
    assert tail[0].startswith("docs: ")
    assert tail[1].startswith("note: telemetry not recorded")
    assert tail[2] == UNATTRIBUTED_NOTE
    assert "isError" not in responses[0]["result"]


def test_the_unattributed_note_does_not_inflate_context_bytes(store):
    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))
    _run(
        store,
        _tools_call_msg(3, arguments={"query": "WidgetCache", "session_id": "20260823-draft"}),
    )

    rows = _telemetry_lines(store)
    assert rows[0]["context_bytes"] == rows[1]["context_bytes"]


@pytest.mark.parametrize(
    "bad_value", [0, False, {"a": 1}, ["x"]], ids=["int", "bool", "dict", "list"]
)
def test_non_string_session_id_is_rejected_not_stringified(store, bad_value):
    """A wrongly-typed value is a malformed request, not something to coerce — `str()` on a dict
    would log Python repr syntax."""
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
    """No store means no measurement; a row here would inflate the search count with runs that
    never happened."""
    _run(empty_store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    assert _telemetry_lines(empty_store) == []


@pytest.mark.parametrize("store_fixture, build_call", _BOTH_READ_TOOLS)
def test_a_telemetry_failure_note_reaches_the_agent_without_breaking_the_protocol(
    request, store_fixture, build_call
):
    """Here stdout carries frames only, so the note must travel inside the tool result text."""
    store = request.getfixturevalue(store_fixture)
    (store / "telemetry.jsonl").mkdir()

    _, responses, _ = _run(store, build_call())

    text = _result_text(responses)
    assert "note: telemetry not recorded" in text
    assert "20260101-widget-cache" in text, "the search result itself must still be complete"
    assert "isError" not in responses[0]["result"], "the search succeeded; only the log failed"


def _search_tools(store) -> dict:
    _, responses, _ = _run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in responses[0]["result"]["tools"]}
    return {name: tools[name] for name in (mcp_server.TOOL_NAME, mcp_server.ROLE_TOOL_NAME)}


@pytest.mark.parametrize("tool_name", [mcp_server.TOOL_NAME, mcp_server.ROLE_TOOL_NAME])
def test_the_session_id_schema_asks_for_the_id_and_names_the_cost_of_omitting_it(
    store, tool_name
):
    """Optional to the schema, asked for in the wording: at tool-call time the description is
    what the model reads, and calling it "Optional" is what produced rows with no session_id."""
    description = _search_tools(store)[tool_name]["inputSchema"]["properties"]["session_id"][
        "description"
    ]

    assert "optional" not in description.lower(), (
        "the description must ask for the id, not offer it"
    )
    assert "unattributed" in description, (
        "and must say what omitting it costs — the row drops out of the analysis"
    )


@pytest.mark.parametrize("tool_name", [mcp_server.TOOL_NAME, mcp_server.ROLE_TOOL_NAME])
def test_each_search_tool_description_asks_for_the_session_id(store, tool_name):
    """A client that shows only the tool description must still surface the requirement."""
    assert "session_id" in _search_tools(store)[tool_name]["description"]


# ---------------------------------------------------------------------------
# `channel` + `context_bytes` — the MCP side of both.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store_fixture, build_call", _BOTH_READ_TOOLS)
def test_a_read_tool_records_mcp_as_the_channel(request, store_fixture, build_call):
    """A Desktop search and a terminal search stay distinguishable in telemetry.jsonl."""
    store = request.getfixturevalue(store_fixture)

    _run(store, build_call())

    assert _telemetry_lines(store)[-1]["channel"] == "mcp"


def test_tools_call_context_bytes_matches_the_rendered_result_for_a_hit(store):
    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    docs = load_store(store / "sessions").docs
    outcome = run_search(docs, "WidgetCache")
    expected_text = render_search_results(outcome, docs)

    assert _telemetry_lines(store)[0]["context_bytes"] == len(expected_text.encode("utf-8"))


def test_tools_call_context_bytes_is_near_zero_for_a_miss(store):
    _run(store, _tools_call_msg(3, arguments={"query": "SomethingNeverIndexed"}))

    row = _telemetry_lines(store)[0]
    expected = len(render_no_match().encode("utf-8"))
    assert row["context_bytes"] == expected
    assert row["context_bytes"] < 50, "a miss must put almost nothing in context"


def test_tools_call_context_bytes_excludes_the_stray_files_note(store):
    """Stray-file housekeeping is not prior-document content and must not inflate `context_bytes`."""
    (store / "some-stray-notes.md").write_text("not in sessions/", encoding="utf-8")

    _, responses, _ = _run(
        store, _tools_call_msg(3, arguments={"query": "SomethingNeverIndexed"})
    )
    text = responses[0]["result"]["content"][0]["text"]
    assert "sit outside the searched set" in text, "the stray note must still render"

    row = _telemetry_lines(store)[0]
    assert row["context_bytes"] == len(render_no_match().encode("utf-8"))


def test_role_tool_context_bytes_matches_the_rendered_role_result(role_store):
    _run(role_store, _role_tools_call_msg(3, query="WidgetCache", role="decisions"))

    docs = load_store(role_store / "sessions").docs
    outcome, role_map = search_with_role_sections(docs, "WidgetCache")
    expected_text = render_role_search_results(outcome, role_map, "decisions")

    assert _telemetry_lines(role_store)[-1]["context_bytes"] == len(
        expected_text.encode("utf-8")
    )


def test_tools_call_records_a_token_estimate_derived_from_context_bytes(store):
    _run(store, _tools_call_msg(3, arguments={"query": "WidgetCache"}))

    row = _telemetry_lines(store)[0]
    assert row["context_tokens_estimate"] == estimate_tokens(row["context_bytes"])


@requires_permission_enforcement
def test_an_unreadable_store_is_a_tool_error_not_an_internal_error(tmp_path):
    """`is_dir()` propagates a PermissionError, which reached the client as `-32603` — the
    channel reserved for engmem's own bugs — with no named cause and no remedy."""
    store = tmp_path / "store"
    sessions = store / "sessions"
    sessions.mkdir(parents=True)

    # the *parent*: `is_dir()` on `sessions` itself still succeeds while `store` is readable,
    # and `load_store` then reports the unlistable directory in its own words
    os.chmod(store, 0o000)
    try:
        _, responses, _ = _run(
            store,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "engmem_search", "arguments": {"query": "anything"}}},
        )
    finally:
        os.chmod(store, 0o755)

    result = responses[0]["result"]
    assert result.get("isError") is True
    assert "store not readable" in result["content"][0]["text"]
