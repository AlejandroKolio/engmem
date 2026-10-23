"""`engmem draft status`: the unpublished drafts linked to the repository the user is in, for a
person or for a client's SessionStart hook (contracts/install.md, "The SessionStart hook")."""

from __future__ import annotations

import json
from pathlib import Path

from engmem.output import _escape_controls
from engmem.scoring import Scope, in_scope
from engmem.search_report import named_ids
from engmem.spine import Doc, read_drafts

NAMED_MAX = 3


def unsaved_drafts(store: Path, repo: str) -> list[Doc]:
    """Drafts whose `repos` names `repo`, newest first; raises `OSError` on an unreadable store."""
    scope = Scope(repos=(repo,))
    drafts = [d for d in read_drafts(store / "sessions") if in_scope(d, scope)]
    return sorted(drafts, key=lambda d: (d.date, d.id), reverse=True)


def drafts_line(drafts: list[Doc]) -> str:
    return (
        f"engmem: unsaved draft(s) {named_ids(drafts, NAMED_MAX)} — finish with "
        "/engmem.save.quick or leave them"
    )


def status_line(repo: str, drafts: list[Doc]) -> str:
    if not drafts:
        return f"engmem: no unsaved drafts for {_escape_controls(repo)}"
    return drafts_line(drafts)


def hook_json(drafts: list[Doc]) -> str:
    """What a Claude Code SessionStart hook prints: a line for the user, context for the agent."""
    ids = named_ids(drafts, NAMED_MAX)
    return json.dumps({
        "systemMessage": drafts_line(drafts),
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                f"engmem: unsaved session drafts for this repository: {ids}. Offer to finish "
                "one with /engmem.save.quick, or leave them."
            ),
        },
    })
