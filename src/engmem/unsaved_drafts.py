"""`engmem draft status`: the unpublished drafts linked to the repository the user is in, for a
person or for a client's SessionStart hook (contracts/install.md, "The SessionStart hook")."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from engmem.output import _escape_controls
from engmem.scoring import Scope, in_scope
from engmem.search_report import named_ids
from engmem.spine import Doc, read_drafts

NAMED_MAX = 3
RECENT_DAYS = 14


def unsaved_drafts(store: Path, repo: str) -> list[Doc]:
    """Drafts whose `repos` names `repo`, newest first; raises `OSError` on an unreadable store."""
    scope = Scope(repos=(repo,))
    drafts = [d for d in read_drafts(store / "sessions") if in_scope(d, scope)]
    return sorted(drafts, key=lambda d: (d.date, d.id), reverse=True)


def split_recent(drafts: list[Doc], today: datetime.date) -> tuple[list[Doc], int]:
    """The drafts dated within `RECENT_DAYS` of `today`, in order, and how many are older."""
    cutoff = today - datetime.timedelta(days=RECENT_DAYS)
    recent = [d for d in drafts if d.date >= cutoff]
    return recent, len(drafts) - len(recent)


def _older_note(older: int) -> str:
    return f" (+{older} older; `engmem draft status`)" if older else ""


def drafts_line(drafts: list[Doc], older: int = 0) -> str:
    return (
        f"engmem: unsaved draft(s) {named_ids(drafts, NAMED_MAX)}{_older_note(older)} — finish "
        "with /engmem.save.quick or leave them"
    )


def status_line(repo: str, drafts: list[Doc]) -> str:
    if not drafts:
        return f"engmem: no unsaved drafts for {_escape_controls(repo)}"
    return drafts_line(drafts)


def hook_json(drafts: list[Doc], today: datetime.date) -> str:
    """What a Claude Code SessionStart hook prints: a line for the user and context for the agent,
    naming only recent drafts; empty when there is none."""
    recent, older = split_recent(drafts, today)
    if not recent:
        return ""
    ids = named_ids(recent, NAMED_MAX) + _older_note(older)
    return json.dumps({
        "systemMessage": drafts_line(recent, older),
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                f"engmem: unsaved session drafts for this repository: {ids}. Offer to finish "
                "one with /engmem.save.quick, or leave them."
            ),
        },
    })
