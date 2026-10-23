"""What the MCP server offers: each tool's name, description and input schema (contracts/mcp-server.md).

Data only, so a description can be read and reviewed apart from the code that serves it."""

from __future__ import annotations

from engmem import cost, drafts, feedback
from engmem.sections import CANONICAL_ROLES

TOOL_NAME = "engmem_search"
TOOL_DESCRIPTION = (
    "Search prior engineering session documents recorded by engmem — decisions, "
    "pitfalls, and cold-start context captured from past work in this codebase. "
    "Call this before planning or implementing any non-trivial engineering task, "
    "with the key terms of the task as the query. Returns the best-matching "
    "documents (or the sentence 'prior context: none found' if nothing matched) "
    "plus a one-line summary of how many documents the store holds. Whenever a "
    "draft session document exists for the current task, pass its id as "
    "`session_id` — without it this retrieval cannot be tied to the document "
    "that reuses it."
)

ROLE_TOOL_NAME = "engmem_search_by_role"
ROLE_TOOL_DESCRIPTION = (
    "Retrieve a specific kind of section — e.g. Decision Log, Lessons Learned, "
    "Production Considerations — from across the most relevant prior engineering "
    "session documents, instead of the single best word-matching passage "
    "`engmem_search` returns. Reach for this when the question is about a "
    "document's STRUCTURE rather than its subject matter — questions like 'what "
    "did we reject and why', 'what broke in production', or 'what should I have "
    "known going in' — because a plain keyword search misses those: the matching "
    "sections discuss different subjects and share no vocabulary with each other, "
    "only their role in the document. Documents are still ranked by the query's "
    "words, exactly as `engmem_search` ranks them; this tool then returns that "
    "role's section from each of the top-ranked documents that actually has one. "
    "A document lacking the requested role is skipped, never padded with the "
    "wrong section. Call `engmem_search` first for an ordinary content question; "
    "reach for this tool instead when the question names a kind of section, not "
    "a topic. Whenever a draft session document exists for the current task, pass "
    "its id as `session_id` — without it this retrieval cannot be tied to the "
    "document that reuses it."
)

# ---------------------------------------------------------------------------
# write tools — the save half of the workflow for a shell-less runtime (Claude Desktop): create a
# draft, finish it (draft -> active), mark an earlier document superseded.
CREATE_DRAFT_TOOL_NAME = "engmem_create_draft"
CREATE_DRAFT_TOOL_DESCRIPTION = (
    "Create a new engmem session document as a draft — the first step of "
    "`/engmem` in a runtime with no shell access. Writes `sessions/<id>.md` "
    "with exactly the `content` given: the complete YAML front matter block "
    "followed by the body. The front matter's `status` MUST be `draft`, its "
    "`id` MUST equal the `id` argument, and its `mode` MUST be the configured "
    "mode (`daily` or `research`; the `engmem` prompt names it) — this tool "
    "refuses to create anything else and names the configured mode when it does. "
    "A `research` draft also carries the `## Pre-reg` baseline, or "
    "`baseline_unavailable: <reason>` when none could be had; a `daily` draft "
    "needs no Pre-reg. NEVER overwrites: "
    "if `sessions/<id>.md` already exists, the call fails and nothing is "
    "written, regardless of what the existing file contains. Use "
    "`engmem_complete_draft` to finish a draft this tool already created. The "
    "result names the draft's version: keep it and pass it as `expected_version` "
    "when you complete the draft. "
    f"{drafts.REUSE_LOG_SHAPE}; the count reads cells by position, so a list or "
    "another column order is not counted."
)

COMPLETE_DRAFT_TOOL_NAME = "engmem_complete_draft"
COMPLETE_DRAFT_TOOL_DESCRIPTION = (
    "Finish an existing draft — the last step of `/engmem.save` (or "
    "`/engmem.save.quick`) in a runtime with no shell access. Replaces "
    "`sessions/<id>.md` with the complete new `content` (front matter + full "
    "body): the ONLY sanctioned use is completing a document `engmem_create_draft` "
    "made. This tool refuses to run unless `sessions/<id>.md` already exists AND "
    "its current front matter `status` is `draft`, and refuses the new `content` "
    "unless its front matter `status` is `active` and its `id` equals the `id` "
    "argument. It will never touch a document that is already `active` or "
    "`superseded` — a confused or repeated call cannot destroy work outside a "
    "document still in draft. It also refuses `content` whose Reuse Log holds a "
    f"row without a quoted span or with a classification other than {drafts.CLASSIFICATION_CHOICES}, "
    f"a table whose columns are not in the order `{drafts.REUSE_LOG_TABLE_HEADER}`, or neither a "
    "table row nor `Prior docs used: none.`, returning each defect so it can be fixed in the "
    "same turn. A `Prior docs used: none.` in a session that read documents through engmem is "
    "published with a warning naming them: tell the user, do not rewrite the document. "
    "To mark a DIFFERENT, already-finished document as superseded by this one, call "
    "`engmem_mark_superseded` instead — never pass that document's id here. "
    "Always pass `expected_version`, the version "
    "`engmem_create_draft` returned for this draft: if the draft changed since "
    "then, the call is refused and its result carries the current draft and its "
    "version — re-read it, carry its changes into `content`, and call again with "
    "that version. Without `expected_version` nothing is written: the refusal "
    "carries the current draft and its version the same way."
)

MARK_SUPERSEDED_TOOL_NAME = "engmem_mark_superseded"
MARK_SUPERSEDED_TOOL_DESCRIPTION = (
    "Mark an earlier, already-`active` engmem session document as superseded by "
    "a newer one — the supersede-check step of `/engmem.save`. Takes only the "
    "two ids involved, `id` (the earlier document) and `superseded_by` (the "
    "document that replaces it); every other line of `sessions/<id>.md` — body, "
    "other front matter fields — is left exactly as it was. Refuses to run "
    "unless `sessions/<id>.md` exists and its current `status` is `active` (never "
    "a draft, never an already-superseded document, and never itself: `id` and "
    "`superseded_by` must differ). This does not require `superseded_by` to "
    "already exist as a document — it is fine to mark the earlier document first. "
    "Always pass `expected_version`, the earlier document's version your new document "
    "was built on — the `<version>` of its `<id>@<version>` Reuse Log row, from the "
    "`cite as` line of that search or `engmem_read`: if it changed since "
    "then, or no version is given, nothing is written and the result carries its "
    "current text and version — re-read it, check that your document still holds "
    "against it, and call again with that version."
)


RECORD_FEEDBACK_TOOL_NAME = "engmem_record_feedback"
RECORD_FEEDBACK_TOOL_DESCRIPTION = (
    "Record the USER's assessment of one document a search showed in this session: "
    "`helped`, `not-applicable` or `harmful`, optionally with the decision it changed and "
    "where that can be seen. Call it only with an assessment the user stated in this "
    "conversation -- never your own judgement, and never to fill in one the user did not "
    "give; an unassessed find is reported as unknown influence, which is not a failure. "
    "Refuses unless `session_id` names a session document in the store and a search "
    "attributed to it showed `doc_id`. Appends to the store's feedback log; no session "
    "document is changed and the Gate 1 count does not read it."
)


READ_TOOL_NAME = "engmem_read"
READ_TOOL_DESCRIPTION = (
    "Read one engmem session document by id, or by the `<id>@<version>` a search cites -- the "
    "whole body, or with `role` only its sections of that role (e.g. 'decisions'). Use it to "
    "load a document a search showed: the text it returns is counted toward this session's "
    "observed cost, while a document opened any other way is not observed. A cited version "
    "the document no longer has is read from its retained copy and says so; a draft is read "
    "marked as not reviewed, with no reference to cite. Refuses an id with no document. "
    "Whenever a draft session document exists for the current task, pass its id as "
    "`session_id`, as on `engmem_search`."
)

RECORD_BASELINE_TOOL_NAME = "engmem_record_baseline"
RECORD_BASELINE_TOOL_DESCRIPTION = (
    "Record the tokens and/or seconds your runtime REPORTED for the no-memory baseline call of "
    "this session (the sub-agent of the Pre-reg step), with the runtime's id for that call when "
    "it gives one. Only figures the runtime reported -- never an estimate or a count of your "
    "own; with nothing reported, do not call it, and the cost summary shows the baseline as "
    "missing. Refuses unless `session_id` names a session document in the store. Appends to "
    "the store's cost log; no session document is changed."
)


_SESSION_ID_SCHEMA = {
    "type": "string",
    "description": (
        "The id of the draft session document this search belongs to. Pass it on "
        "every search once the draft exists — it is the only field that ties this "
        "retrieval to the document that later cites it. Omitted, the search is "
        "logged unattributed and drops out of the analysis entirely. Leave it out "
        "only when no draft has been created yet."
    ),
}

# US-09/US-10: the same scope `engmem search --repo` / `--unscoped` / `--all-repos` take, under
# the same rules
_REPO_SCHEMA = {
    "type": "string",
    "description": (
        "Search only the records whose `repos` front matter names this repository "
        "(case-insensitive). Title and tags never stand in for that link, so a record "
        "with no `repos` is left out. For several repositories use `repos`. Omit every "
        "scope argument to search the whole store."
    ),
}
_REPOS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "description": (
        "Search only the records linked to any of these repositories, each record once. "
        "Same matching as `repo`, and joined with it when both are passed. Only when the "
        "user asks to bring in other repositories; never empty."
    ),
}
_UNSCOPED_SCHEMA = {
    "type": "boolean",
    "description": (
        "true: search only the records linked to no repository (no readable `repos` "
        "value). Not combined with another scope argument."
    ),
}
_ALL_REPOS_SCHEMA = {
    "type": "boolean",
    "description": (
        "true: search the whole store, as with no scope argument, but say so in a `scope:` "
        "line and show each result's `repos:` links. Not combined with another scope argument."
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
                            "description": (
                                "Key terms describing the current task — the same "
                                "words you would pass to `engmem search` on the CLI."
                            ),
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
                            "description": (
                                "Key terms describing the current task — used to "
                                "rank documents exactly as `engmem_search` does. "
                                "Role filtering happens after ranking, not instead "
                                "of it."
                            ),
                        },
                        "role": {
                            "type": "string",
                            "enum": list(CANONICAL_ROLES),
                            "description": (
                                "Which section role to retrieve from each "
                                "top-ranked document — e.g. 'decisions' for a "
                                "Decision Log, 'lessons' for Lessons Learned, "
                                "'production' for Production Considerations. A "
                                "document ranked by the query but lacking this "
                                "role is skipped, not returned with a substitute "
                                "section."
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
                                "The new document's id — must equal the filename "
                                "stem and the front matter's own 'id' field, and "
                                "must match <story-id>-<slug> or <YYYYMMDD>-<slug> "
                                "(lowercase letters, digits, and hyphens only)."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "The complete file content to create: the YAML "
                                "front matter block (--- ... ---) followed by the "
                                "body. Front matter 'status' must be 'draft'."
                            ),
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
                            "description": (
                                "The id of the existing draft to finish — "
                                "sessions/<id>.md must already exist with "
                                "status: draft."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "The complete replacement file content: front "
                                "matter (with 'status: active' and the same 'id') "
                                "followed by the full body sections."
                            ),
                        },
                        "expected_version": {
                            "type": "string",
                            "description": (
                                "The draft's version as engmem_create_draft "
                                "returned it, or as a refusal of this tool "
                                "reported it after you re-read the draft. The "
                                "call is refused, and nothing written, if the "
                                "draft on disk is no longer that version."
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
                            "description": (
                                "The id of the earlier, currently-active document "
                                "to mark superseded."
                            ),
                        },
                        "superseded_by": {
                            "type": "string",
                            "description": (
                                "The id of the document that supersedes it."
                            ),
                        },
                        "expected_version": {
                            "type": "string",
                            "description": (
                                "The earlier document's version your new "
                                "document was built on, as the `cite as` "
                                "line of that search or engmem_read named it, or "
                                "as a refusal of this tool reported it after "
                                "you re-read the document. The call is "
                                "refused, and nothing written, if the "
                                "document on disk is no longer that version."
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
                                "The session document whose search showed the document -- "
                                "the id passed as `session_id` on that search."
                            ),
                        },
                        "doc_id": {
                            "type": "string",
                            "description": "The id of the document the search showed.",
                        },
                        "assessment": {
                            "type": "string",
                            "enum": list(feedback.Assessment),
                            "description": "The user's assessment, as the user gave it.",
                        },
                        "decision": {
                            "type": "string",
                            "description": (
                                "Optional: the decision the document changed, in the "
                                f"user's words, at most {feedback.TEXT_MAX} characters."
                            ),
                        },
                        "source": {
                            "type": "string",
                            "description": (
                                "Optional: where that can be seen, e.g. a commit, PR or "
                                f"review, at most {feedback.TEXT_MAX} characters."
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
                            "description": (
                                "The id of the document to read, as a search showed it, or "
                                "the `<id>@<version>` its `cite as` line names."
                            ),
                        },
                        "role": {
                            "type": "string",
                            "enum": list(CANONICAL_ROLES),
                            "description": (
                                "Optional: return only the document's sections of this role."
                            ),
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "The id of the draft session document this read belongs to, "
                                "as on `engmem_search`. Omitted, the read is counted "
                                "unattributed and in no session's cost."
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
                                "Optional: the runtime's id for that call, at most "
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
