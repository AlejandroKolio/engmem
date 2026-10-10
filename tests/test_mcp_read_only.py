"""`engmem mcp --read-only`: the server a remote client such as ChatGPT reaches through a bridge."""

from __future__ import annotations

import pytest

from conftest import ACTIVE_CONTENT, DRAFT_CONTENT, write_file
from mcp_harness import _run, _text, _tools_call_msg

from engmem.mcp_server import INVALID_PARAMS

SEARCH_TOOLS = ["engmem_search", "engmem_search_by_role"]
WRITE_CALLS = [
    pytest.param(
        "engmem_create_draft", {"id": "20260102-new", "content": DRAFT_CONTENT}, id="create_draft"
    ),
    pytest.param(
        "engmem_complete_draft",
        {"id": "20260101-widget-cache", "content": ACTIVE_CONTENT},
        id="complete_draft",
    ),
    pytest.param(
        "engmem_mark_superseded",
        {"id": "20260101-widget-cache", "superseded_by": "20260102-new"},
        id="mark_superseded",
    ),
    pytest.param(
        "engmem_record_feedback",
        {"session_id": "20260101-widget-cache", "doc_id": "20260102-new", "assessment": "helped"},
        id="record_feedback",
    ),
    pytest.param("engmem_read", {"id": "20260101-widget-cache"}, id="read"),
    pytest.param(
        "engmem_record_baseline",
        {"session_id": "20260101-widget-cache", "tokens": 100},
        id="record_baseline",
    ),
]


@pytest.fixture
def store(tmp_path):
    sessions = tmp_path / "store" / "sessions"
    sessions.mkdir(parents=True)
    write_file(sessions, "20260101-widget-cache.md", ACTIVE_CONTENT)
    return tmp_path / "store"


def _snapshot(store):
    return {p.name: p.read_bytes() for p in (store / "sessions").iterdir()}


def test_read_only_lists_only_the_search_tools(store):
    _, responses, _ = _run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, read_only=True)

    assert [tool["name"] for tool in responses[0]["result"]["tools"]] == SEARCH_TOOLS


def test_the_default_server_still_lists_the_write_tools(store):
    _, responses, _ = _run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert len(responses[0]["result"]["tools"]) == len(SEARCH_TOOLS) + len(WRITE_CALLS)


@pytest.mark.parametrize(("name", "arguments"), WRITE_CALLS)
def test_read_only_refuses_every_write_tool_and_leaves_the_store_alone(store, name, arguments):
    before = _snapshot(store)

    _, responses, _ = _run(store, _tools_call_msg(1, name=name, arguments=arguments), read_only=True)

    error = responses[0]["error"]
    assert error["code"] == INVALID_PARAMS
    assert "--read-only" in error["message"]
    assert _snapshot(store) == before


def test_read_only_still_searches(store):
    _, responses, _ = _run(
        store,
        _tools_call_msg(1, name="engmem_search", arguments={"query": "WidgetCache"}),
        read_only=True,
    )

    result = responses[0]["result"]
    assert not result.get("isError")
    assert "20260101-widget-cache" in _text(result)


def test_read_only_keeps_the_missing_name_diagnosis(store):
    _, responses, _ = _run(
        store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}, read_only=True
    )

    assert "missing required 'name'" in responses[0]["error"]["message"]


def test_the_read_only_help_says_it_does_not_limit_who_reads(capsys):
    """US-19, AC-19.4: the flag offered for a reachable server is not offered as privacy."""
    from engmem.cli import main

    with pytest.raises(SystemExit):
        main(["mcp", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert "refuses writes, does not limit who reads" in help_text
