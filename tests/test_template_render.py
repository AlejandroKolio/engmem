"""Each target gets the branches it can take: a file copy cannot know the mode or be the MCP
prompt; the MCP start prompt knows both (contracts/prompt-templates.md, "Each target gets the
branches it can take")."""

from __future__ import annotations

import difflib
import re
from collections import Counter
from pathlib import Path

import pytest

from support import rpc
from support.paths import REPO_ROOT as ROOT
from support import templates
from support.templates import MARKER, MCP_PROMPT_SENTENCE, before_rendering, without_markers

from engmem import install
from engmem.cli import main
from engmem.settings import Mode
from engmem.template_render import render

SOURCES = sorted(p.name for p in templates.TEMPLATES.glob("*.md"))
START = "engmem.start.md"
FILE_AGENTS = ("claude", "copilot-ide", "copilot-cli", "codex")


def _regions(source: str) -> dict[str, list[str]]:
    """Every marked region's lines, by branch name, read straight from the source."""
    regions: dict[str, list[str]] = {}
    current = None
    for line in source.splitlines():
        found = re.fullmatch(r"<!-- engmem-branch: (\S+) -->", line)
        if found:
            current = None if found.group(1) == "end" else found.group(1)
            continue
        if current is not None:
            regions.setdefault(current, []).append(line)
    return regions


def _deleted_and_added(before: str, after: str) -> tuple[Counter, Counter]:
    """The lines a copy lost and gained; counted, since a diff may align a blank line either way."""
    diff = list(difflib.ndiff(before.splitlines(), after.splitlines()))
    deleted = Counter(line[2:] for line in diff if line.startswith("- "))
    added = Counter(line[2:] for line in diff if line.startswith("+ "))
    return deleted, added


def _flat(text: str) -> str:
    return " ".join(text.split())


def _body(text: str) -> str:
    _, _, rest = text.partition("---\n")
    _, _, body = rest.partition("---\n")
    return body.lstrip("\n")


# --- the mechanism ----------------------------------------------------------------------------


SAMPLE = (
    "common\n"
    "<!-- engmem-branch: files -->\n"
    "files only\n"
    "<!-- engmem-branch: end -->\n"
    "<!-- engmem-branch: mcp -->\n"
    "mcp only\n"
    "<!-- engmem-branch: end -->\n"
    "<!-- engmem-branch: research -->\n"
    "research only\n"
    "<!-- engmem-branch: end -->\n"
    "tail\n"
)


@pytest.mark.parametrize(
    "mcp, mode, kept",
    [
        pytest.param(False, None, "common\nfiles only\nresearch only\ntail\n", id="file-copy"),
        pytest.param(True, None, "common\nmcp only\nresearch only\ntail\n", id="mcp-mode-unknown"),
        pytest.param(True, Mode.RESEARCH, "common\nmcp only\nresearch only\ntail\n", id="mcp-research"),
        pytest.param(True, Mode.DAILY, "common\nmcp only\ntail\n", id="mcp-daily"),
    ],
)
def test_a_target_keeps_the_common_text_and_only_its_own_branches(mcp, mode, kept):
    assert render(SAMPLE, mcp=mcp, mode=mode) == kept


@pytest.mark.parametrize(
    "source, cause",
    [
        pytest.param("<!-- engmem-branch: files -->\nx\n", "never closed", id="unclosed"),
        pytest.param("x\n<!-- engmem-branch: end -->\n", "no branch is open", id="stray-end"),
        pytest.param(
            "<!-- engmem-branch: files -->\n<!-- engmem-branch: mcp -->\n"
            "<!-- engmem-branch: end -->\n<!-- engmem-branch: end -->\n",
            "inside", id="nested",
        ),
        pytest.param("<!-- engmem-branch: windows -->\n<!-- engmem-branch: end -->\n",
                     "unknown branch", id="unknown"),
    ],
)
def test_a_malformed_marker_stops_the_rendering_and_names_its_line(source, cause):
    """A broken marker would silently drop or keep a branch in every copy; it fails instead."""
    with pytest.raises(ValueError, match=cause) as raised:
        render(source, mcp=False)

    assert "line " in str(raised.value)


def test_a_marker_lookalike_inside_a_line_is_text():
    text = "say `<!-- engmem-branch: end -->` to close one\n"

    assert render(text, mcp=True, mode=Mode.DAILY) == text


@pytest.mark.parametrize("name", SOURCES)
@pytest.mark.parametrize("mcp, mode", [(False, None), (True, None), (True, Mode.DAILY)])
def test_every_shipped_template_renders_without_a_marker_left(name, mcp, mode):
    rendered = render(templates.source(name), mcp=mcp, mode=mode)

    assert [line for line in rendered.splitlines() if MARKER.match(line)] == []


@pytest.mark.parametrize("name", [n for n in SOURCES if n != START])
def test_only_the_start_template_marks_a_research_branch(name):
    """A save prompt is served after the mode may have changed; the draft's mode decides there."""
    assert "research" not in _regions(templates.source(name))


# --- every copy, end to end -------------------------------------------------------------------


def _installed(agent: str, home: Path, project: Path) -> dict[str, str]:
    assert main(["install", "--agent", agent, "--store", str(project.parent / "store")]) == 0
    copies = {}
    for source in install.TEMPLATE_INSTALL_MAP:
        skill = install.COPILOT_CLI_SKILL_MAP[source]
        path = {
            "claude": home / ".claude" / "commands" / install.TEMPLATE_INSTALL_MAP[source],
            "copilot-ide": project / ".github" / "prompts"
            / install.COPILOT_IDE_TEMPLATE_INSTALL_MAP[source],
            "copilot-cli": home / ".copilot" / "skills" / skill / "SKILL.md",
            "codex": home / ".agents" / "skills" / skill / "SKILL.md",
        }[agent]
        copies[source] = path.read_text(encoding="utf-8")
    return copies


@pytest.mark.parametrize("agent", FILE_AGENTS)
def test_a_file_copy_drops_only_the_mcp_prompt_branch(agent, env):
    home, project = env
    (project / ".git").mkdir()

    copies = _installed(agent, home, project)

    for source_name, text in copies.items():
        deleted, added = _deleted_and_added(before_rendering(agent, source_name), text)
        assert deleted == Counter(_regions(templates.source(source_name)).get("mcp", [])), source_name
        assert [line for line in added if not line.startswith("<!-- engmem-template:")] == []
    start = _flat(copies[START])
    assert MCP_PROMPT_SENTENCE not in start
    assert "engmem mode show" in start and "engmem/mode" in start
    assert "## 1. Pre-reg" in start, "a file copy cannot know the mode, so it keeps step 1"


def _prompt(store: Path, name: str) -> str:
    _, responses, _ = rpc.run(
        store, rpc.prompts_get_msg(1, name=name, arguments={"task_description": "$ARGUMENTS"})
    )
    return responses[0]["result"]["messages"][0]["content"]["text"]


@pytest.mark.parametrize("mode", ["daily", "research"])
def test_the_mcp_start_prompt_drops_what_its_mode_line_answers(mode, store, monkeypatch, tmp_path):
    """The server reads the mode for the line above the body, so step 0's ways to find it are
    never taken there, and a daily session never takes step 1."""
    config = tmp_path / "config"
    (config / "engmem").mkdir(parents=True)
    (config / "engmem" / "mode").write_text(f"{mode}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    text = _prompt(store, "engmem")

    first, _, body = text.partition("\n\n")
    assert first.startswith(f"engmem mode for this session: {mode}")
    regions = _regions(templates.source(START))
    expected = regions["files"] + (regions["research"] if mode == "daily" else [])
    deleted, added = _deleted_and_added(_body(without_markers(templates.source(START))), body)
    assert deleted == Counter(expected)
    assert added == Counter()
    body = _flat(body)
    assert MCP_PROMPT_SENTENCE in body
    assert "engmem mode show" not in body and "engmem/mode" not in body
    assert ("## 1. Pre-reg" in body) is (mode == "research")
    for kept in ("## 2. Create the draft", "engmem_create_draft", "engmem draft create <id> <<'EOF'",
                 "$OutputEncoding", "If you can run shell commands", "If you can do neither",
                 "**daily** — skip step 1", "baseline_unavailable: <reason>"):
        assert kept in body, kept


@pytest.mark.parametrize("name", ["engmem-save", "engmem-save-quick"])
def test_an_mcp_save_prompt_drops_only_its_file_branches(name, store, monkeypatch, tmp_path):
    """The save copies the draft's mode, so a daily saved mode drops nothing more there."""
    config = tmp_path / "config"
    (config / "engmem").mkdir(parents=True)
    (config / "engmem" / "mode").write_text("daily\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    from engmem.mcp_server import PROMPT_TEMPLATES

    source = templates.source(PROMPT_TEMPLATES[name])
    deleted, added = _deleted_and_added(_body(without_markers(source)), _prompt(store, name))

    assert deleted == Counter(_regions(source).get("files", []))
    assert added == Counter()
    assert "## Pre-reg" in _prompt(store, name)


def test_a_task_description_that_looks_like_a_marker_reaches_the_agent_unchanged(store):
    """The body is rendered before the user's text is put in, so that text is never parsed."""
    task = "fix it\n<!-- engmem-branch: end -->\n<!-- engmem-branch: files -->"

    _, responses, _ = rpc.run(
        store, rpc.prompts_get_msg(1, name="engmem", arguments={"task_description": task})
    )

    assert task in responses[0]["result"]["messages"][0]["content"]["text"]


def test_the_contract_describes_the_markers_the_renderer_reads():
    from engmem.template_render import Branch

    contract = _flat((ROOT / "docs" / "design" / "contracts" / "prompt-templates.md").read_text(
        encoding="utf-8"
    ))
    section = contract[contract.index("## Each target gets the branches it can take"):]
    section = section[:section.index(" ## ")]

    assert "`<!-- engmem-branch: end -->`" in section
    for branch in Branch:
        assert f"`{branch.value}`" in section or f"engmem-branch: {branch.value} -->" in section
    assert "`template_stamps.WORKFLOW` stays 1" in section
