"""What the MCP server offers: each tool's name, description and input schema (contracts/mcp-server.md).

Data only, so a description can be read and reviewed apart from the code that serves it."""

from __future__ import annotations

from engmem import cost, drafts, feedback
from engmem.sections import CANONICAL_ROLES

TOOL_NAME = "engmem_search"
TOOL_DESCRIPTION = (
    "Search prior engmem session documents: decisions, pitfalls and context from past work. "
    "Call it before you plan a non-trivial task, with its key terms as `query`. Returns the "
    "best matches, or 'prior context: none found', and a one-line store count. Once a draft "
    "exists, pass its id as `session_id`."
)

ROLE_TOOL_NAME = "engmem_search_by_role"
ROLE_TOOL_DESCRIPTION = (
    "Return one kind of section (`role`) from each top-ranked prior session document. Use it "
    "when the question names a kind of section, not a topic ('what did we reject and why'); "
    "for a topic, use `engmem_search`. `query` ranks documents as in `engmem_search`; a "
    "document without the `role` section is skipped, never replaced. Once a draft exists, pass "
    "its id as `session_id`."
)

CREATE_DRAFT_TOOL_NAME = "engmem_create_draft"
CREATE_DRAFT_TOOL_DESCRIPTION = (
    "Create a session document as a draft: the first step of `/engmem` without a shell. Writes "
    "`content` as `sessions/<id>.md`. The front matter needs `status: draft`, `id` equal to the "
    "`id` argument, and `mode` equal to the configured mode (`daily` or `research`); the "
    "`engmem` prompt and a refusal name it. A `research` draft also needs a `## Pre-reg` "
    "section or `baseline_unavailable: <reason>`. It never overwrites: if `sessions/<id>.md` "
    "exists, nothing is written. The result names the draft's version; pass it as "
    "`expected_version` to `engmem_complete_draft`. "
    f"{drafts.REUSE_LOG_SHAPE}; a list or another column order is not counted."
)

COMPLETE_DRAFT_TOOL_NAME = "engmem_complete_draft"
COMPLETE_DRAFT_TOOL_DESCRIPTION = (
    "Finish a draft from `engmem_create_draft`: the last step of `/engmem.save` or "
    "`/engmem.save.quick` without a shell. Replaces `sessions/<id>.md` with `content`. Refused "
    "unless the file has `status: draft` and `content` has `status: active` and the same `id`. "
    "Also refused, with each defect named: a Reuse Log row without a quoted span or with a "
    f"classification other than {drafts.CLASSIFICATION_CHOICES}, columns not in the order "
    f"`{drafts.REUSE_LOG_TABLE_HEADER}`, or neither a row nor `Prior docs used: none.`. That "
    "line in a session that read documents is published with a warning naming them: tell the "
    "user, do not rewrite. To supersede another document, use `engmem_mark_superseded`; never "
    "pass its id here. Always pass `expected_version` from `engmem_create_draft`. If the draft "
    "changed, or the version is missing, nothing is written and the result gives the current "
    "draft and version: carry its changes into `content` and call again with that version."
)

MARK_SUPERSEDED_TOOL_NAME = "engmem_mark_superseded"
MARK_SUPERSEDED_TOOL_DESCRIPTION = (
    "Mark an earlier session document as superseded by a newer one: the supersede step of "
    "`/engmem.save`. Changes only the `status` and `superseded_by` lines of `sessions/<id>.md`. "
    "Refused unless that document has `status: active` and `id` differs from `superseded_by`; "
    "`superseded_by` need not exist yet. Always pass `expected_version`: the version your new "
    "document was built on, from its `<id>@<version>` Reuse Log row (a `cite as` line). If the "
    "document changed, or the version is missing, nothing is written and the result gives its "
    "current text and version: check that your document still replaces it, then call again "
    "with that version."
)

RECORD_FEEDBACK_TOOL_NAME = "engmem_record_feedback"
RECORD_FEEDBACK_TOOL_DESCRIPTION = (
    "Record the USER's assessment of one document a search showed in this session. Call it only "
    "with an assessment the user stated in this conversation, never your own judgement; an "
    "unassessed find is not a failure. Refused unless `session_id` names a session document "
    "and a search with that `session_id` showed `doc_id`. Changes no session document."
)

READ_TOOL_NAME = "engmem_read"
READ_TOOL_DESCRIPTION = (
    "Read one session document by id, or by the `<id>@<version>` a search cites: the whole "
    "body, or with `role` only the sections of that role. Use it to load a document a search "
    "showed; only reads through this tool count toward the session's observed cost. An older "
    "cited version is read from its retained copy and says so. A draft is marked as not "
    "reviewed and has no reference to cite. Refused for an id with no document. Once a draft "
    "exists, pass its id as `session_id`."
)

RECORD_BASELINE_TOOL_NAME = "engmem_record_baseline"
RECORD_BASELINE_TOOL_DESCRIPTION = (
    "Record the tokens and/or seconds your runtime REPORTED for this session's no-memory "
    "baseline call (the Pre-reg sub-agent). Only reported figures, never an estimate; if nothing "
    "was reported, do not call it. Refused unless `session_id` names a session document. "
    "Changes no session document."
)


_SESSION_ID_SCHEMA = {
    "type": "string",
    "description": "Pass this task's draft id; without it the search is logged unattributed.",
}

_REPO_SCHEMA = {
    "type": "string",
    "description": "Only records whose `repos` names this repository (case-insensitive).",
}
_REPOS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "description": (
        "As `repo`, for several repositories. Only when the user asks for them."
    ),
}
_UNSCOPED_SCHEMA = {
    "type": "boolean",
    "description": (
        "true: only records with no `repos`. Use no other scope argument."
    ),
}
_ALL_REPOS_SCHEMA = {
    "type": "boolean",
    "description": (
        "true: the whole store, with each result's `repos:`. Use no other scope argument."
    ),
}
_SCOPE_PROPERTIES = {
    "repo": _REPO_SCHEMA,
    "repos": _REPOS_SCHEMA,
    "unscoped": _UNSCOPED_SCHEMA,
    "all_repos": _ALL_REPOS_SCHEMA,
}


def tools_list() -> dict:
    """The `tools/list` result: every tool, in order; a new object on each call."""
    return {
        "tools": [
            {
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Key terms of the current task.",
                        },
                        "session_id": _SESSION_ID_SCHEMA,
                        **_SCOPE_PROPERTIES,
                    },
                    "required": ["query"],
                },
            },
            {
                "name": ROLE_TOOL_NAME,
                "description": ROLE_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Key terms of the current task.",
                        },
                        "role": {
                            "type": "string",
                            "enum": list(CANONICAL_ROLES),
                            "description": (
                                "The section role, e.g. 'decisions' (Decision Log), "
                                "'lessons' (Lessons Learned)."
                            ),
                        },
                        "session_id": _SESSION_ID_SCHEMA,
                        **_SCOPE_PROPERTIES,
                    },
                    "required": ["query", "role"],
                },
            },
            {
                "name": CREATE_DRAFT_TOOL_NAME,
                "description": CREATE_DRAFT_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": (
                                "<story-id>-<slug> or <YYYYMMDD>-<slug>: lowercase letters, "
                                "digits and hyphens only."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "The complete file: YAML front matter, then the body.",
                        },
                    },
                    "required": ["id", "content"],
                },
            },
            {
                "name": COMPLETE_DRAFT_TOOL_NAME,
                "description": COMPLETE_DRAFT_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The id of the draft to finish.",
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "The complete new file: front matter with `status: active` "
                                "and the same `id`, then the full body."
                            ),
                        },
                        "expected_version": {
                            "type": "string",
                            "description": (
                                "The version engmem_create_draft or a refusal of this tool gave."
                            ),
                        },
                    },
                    "required": ["id", "content", "expected_version"],
                },
            },
            {
                "name": MARK_SUPERSEDED_TOOL_NAME,
                "description": MARK_SUPERSEDED_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The id of the earlier, active document.",
                        },
                        "superseded_by": {
                            "type": "string",
                            "description": "The id of the document that supersedes it.",
                        },
                        "expected_version": {
                            "type": "string",
                            "description": (
                                "The version a `cite as` line or a refusal of this tool gave."
                            ),
                        },
                    },
                    "required": ["id", "superseded_by", "expected_version"],
                },
            },
            {
                "name": RECORD_FEEDBACK_TOOL_NAME,
                "description": RECORD_FEEDBACK_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": (
                                "The `session_id` of the search that showed the document."
                            ),
                        },
                        "doc_id": {
                            "type": "string",
                            "description": "The id of the document the search showed.",
                        },
                        "assessment": {
                            "type": "string",
                            "enum": list(feedback.Assessment),
                            "description": "As the user gave it.",
                        },
                        "decision": {
                            "type": "string",
                            "description": (
                                "Optional: the decision it changed, in the user's words, at "
                                f"most {feedback.TEXT_MAX} characters."
                            ),
                        },
                        "source": {
                            "type": "string",
                            "description": (
                                "Optional: where that shows (commit, PR, review), at most "
                                f"{feedback.TEXT_MAX} characters."
                            ),
                        },
                    },
                    "required": ["session_id", "doc_id", "assessment"],
                },
            },
            {
                "name": READ_TOOL_NAME,
                "description": READ_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "A document id, or `<id>@<version>`.",
                        },
                        "role": {
                            "type": "string",
                            "enum": list(CANONICAL_ROLES),
                            "description": "Optional: only the sections of this role.",
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "This task's draft id; without it the read is unattributed."
                            ),
                        },
                    },
                    "required": ["id"],
                },
            },
            {
                "name": RECORD_BASELINE_TOOL_NAME,
                "description": RECORD_BASELINE_TOOL_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "The session document the baseline call belongs to.",
                        },
                        "tokens": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Tokens the runtime reported for the call.",
                        },
                        "seconds": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "description": "Duration the runtime reported for the call.",
                        },
                        "call_id": {
                            "type": "string",
                            "description": (
                                "Optional: the runtime's id for the call, at most "
                                f"{cost.CALL_ID_MAX} characters; a later report with the same "
                                "id replaces this one."
                            ),
                        },
                    },
                    "required": ["session_id"],
                },
            },
        ]
    }
