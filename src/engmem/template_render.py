"""One copy of a template per target, without the branches that target can never take. Rules
and reasons: contracts/prompt-templates.md, "Each target gets the branches it can take"."""

from __future__ import annotations

import enum
import re

from engmem.settings import Mode


class Branch(enum.StrEnum):
    FILES = "files"
    MCP = "mcp"
    DAILY = "daily"
    RESEARCH = "research"


_MARKER = re.compile(r"<!-- engmem-branch: (\S+) -->")
_END = "end"


_OTHER_MODE = {Mode.DAILY: Branch.RESEARCH, Mode.RESEARCH: Branch.DAILY}


def _dropped(mcp: bool, mode: Mode | None) -> frozenset[Branch]:
    own_channel = Branch.FILES if mcp else Branch.MCP
    if mode is None:
        return frozenset({own_channel})
    return frozenset({own_channel, _OTHER_MODE[mode]})


def render(source: str, *, mcp: bool, mode: Mode | None = None) -> str:
    """`source` without its marker lines and the branches this target never takes; `mode` is
    None where the copy cannot know it, which keeps both the daily and the research branch."""
    dropped = _dropped(mcp, mode)
    kept: list[str] = []
    open_branch: Branch | None = None
    opened_at = 0
    for number, line in enumerate(source.splitlines(keepends=True), start=1):
        marker = _MARKER.fullmatch(line.rstrip("\r\n"))
        if marker is None:
            if open_branch not in dropped:
                kept.append(line)
            continue
        name = marker.group(1)
        if name == _END:
            if open_branch is None:
                raise ValueError(f"template line {number}: an end marker where no branch is open")
            open_branch = None
            continue
        if open_branch is not None:
            raise ValueError(
                f"template line {number}: branch {name!r} opens inside branch {open_branch.value!r}"
            )
        try:
            open_branch = Branch(name)
        except ValueError:
            raise ValueError(f"template line {number}: unknown branch {name!r}") from None
        opened_at = number
    if open_branch is not None:
        raise ValueError(f"template line {opened_at}: branch {open_branch.value!r} is never closed")
    return "".join(kept)
