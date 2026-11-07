"""The `tools/list` result is paid for in every turn of a client that loads tools eagerly, so its
size is capped, while each tool keeps the instructions a model needs to call it safely."""

from __future__ import annotations

import json

import pytest

from support.paths import TESTS_DIR

from engmem import drafts, mcp_tools

BASELINE_TOOLS_LIST_BYTES = 16087
TOOLS_LIST_BYTES_CAP = BASELINE_TOOLS_LIST_BYTES * 60 // 100
SCHEMA_SNAPSHOT = TESTS_DIR / "fixtures" / "mcp-tool-schemas.json"


def _without_descriptions(node):
    if isinstance(node, dict):
        return {k: _without_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_without_descriptions(v) for v in node]
    return node


def _wire_bytes(message: dict) -> int:
    return len(json.dumps(message).encode("utf-8"))


def test_tools_list_is_at_most_sixty_percent_of_its_size_before_the_cut():
    assert _wire_bytes(mcp_tools.tools_list()) <= TOOLS_LIST_BYTES_CAP


def test_the_schema_part_is_byte_for_byte_the_snapshot_taken_before_the_cut():
    """Names, types, required lists and enums: clients and saved prompts call them."""
    snapshot = json.loads(SCHEMA_SNAPSHOT.read_text(encoding="utf-8"))

    assert json.dumps(_without_descriptions(mcp_tools.tools_list())) == json.dumps(snapshot)


_INSTRUCTIONS = {
    mcp_tools.TOOL_NAME: [
        "before you plan",
        "key terms as `query`",
        "prior context: none found",
        "pass its id as `session_id`",
    ],
    mcp_tools.ROLE_TOOL_NAME: [
        "names a kind of section, not a topic",
        "for a topic, use `engmem_search`",
        "skipped, never replaced",
        "pass its id as `session_id`",
    ],
    mcp_tools.CREATE_DRAFT_TOOL_NAME: [
        "`status: draft`",
        "`mode`",
        "`## Pre-reg`",
        "`baseline_unavailable: <reason>`",
        "never overwrites",
        "`expected_version`",
        drafts.REUSE_LOG_TABLE_HEADER,
        drafts.CLASSIFICATION_CHOICES,
        "column order",
    ],
    mcp_tools.COMPLETE_DRAFT_TOOL_NAME: [
        "`status: draft`",
        "`status: active`",
        "`engmem_mark_superseded`",
        "`expected_version`",
        "`Prior docs used: none.`",
        "tell the user",
        drafts.REUSE_LOG_TABLE_HEADER,
        drafts.CLASSIFICATION_CHOICES,
        "nothing is written",
        "call again with that version",
    ],
    mcp_tools.MARK_SUPERSEDED_TOOL_NAME: [
        "`status: active`",
        "`superseded_by`",
        "`expected_version`",
        "`cite as`",
        "nothing is written",
        "call again with that version",
    ],
    mcp_tools.RECORD_FEEDBACK_TOOL_NAME: ["USER", "never your own judgement", "`doc_id`"],
    mcp_tools.READ_TOOL_NAME: [
        "`<id>@<version>`",
        "`role`",
        "observed cost",
        "not reviewed",
        "`session_id`",
    ],
    mcp_tools.RECORD_BASELINE_TOOL_NAME: [
        "REPORTED",
        "never an estimate",
        "do not call it",
        "`session_id`",
    ],
}


@pytest.mark.parametrize("name, phrases", sorted(_INSTRUCTIONS.items()))
def test_each_tool_description_keeps_the_instructions_a_client_needs(name, phrases):
    tool = next(t for t in mcp_tools.tools_list()["tools"] if t["name"] == name)

    missing = [p for p in phrases if p not in tool["description"]]
    assert not missing, f"{name} lost: {missing}"


def test_every_tool_has_an_instruction_checklist():
    assert set(_INSTRUCTIONS) == {t["name"] for t in mcp_tools.tools_list()["tools"]}
