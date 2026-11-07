from __future__ import annotations

import re

from support.paths import REPO_ROOT

TEMPLATES = REPO_ROOT / "src" / "engmem" / "templates"
MARKER = re.compile(r"^<!-- engmem-branch: \S+ -->$")
MCP_PROMPT_SENTENCE = "Through the engmem MCP prompt, the line above this template names it."


def source(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def without_markers(text: str) -> str:
    """Every branch kept, every marker line gone: the template as one target taking all paths."""
    return "".join(
        line for line in text.splitlines(keepends=True) if not MARKER.match(line.rstrip("\r\n"))
    )


def before_rendering(agent: str, source_name: str) -> str:
    """The copy an install wrote before rendering: every branch, with the agent's own rewrites."""
    from engmem import install

    full = without_markers(source(source_name))
    if agent not in install._SKILL_BODY_REWRITES:
        return full
    return install._to_skill_front_matter(
        install._SKILL_BODY_REWRITES[agent](full), install.COPILOT_CLI_SKILL_MAP[source_name]
    )
