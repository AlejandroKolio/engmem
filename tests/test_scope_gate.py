"""`/engmem` starts only for engineering work: the gate before step 0 decides from the task
description alone, and every other part of the start stays as it was (contracts/prompt-templates.md,
"Engineering work only")."""

from __future__ import annotations

import hashlib

import pytest

from support import templates

from engmem.settings import Mode
from engmem.template_render import render

START = "engmem.start.md"
SKIP_LINE = "`engmem: skipped — not an engineering task`"
GATE_HEADING = "## Scope\n"
GATE_MAX_BYTES = 450
RENDERS_BEFORE_THE_GATE = {
    "files-research": ({"mcp": False, "mode": Mode.RESEARCH}, 12333, "df2712fc405dd281"),
    "mcp-research": ({"mcp": True, "mode": Mode.RESEARCH}, 12127, "31560930080ce0f2"),
}


def _source() -> str:
    return templates.source(START)


def _body(text: str) -> str:
    _, _, rest = text.partition("---\n")
    _, _, body = rest.partition("---\n")
    return body


def _gate(text: str) -> str:
    start = text.index(GATE_HEADING)
    return text[start:text.index("## 0. Mode", start)]


def test_the_gate_comes_before_the_mode_step_and_every_search():
    body = _body(_source())

    assert body.index(GATE_HEADING) < body.index("## 0. Mode") < body.index("engmem search")


def test_the_gate_skips_on_one_line_with_no_draft_and_no_search_and_still_answers():
    gate = " ".join(_gate(_source()).split())

    assert SKIP_LINE in gate and "on one line" in gate
    assert "no draft" in gate and "no search" in gate and "answer" in gate
    assert "From the task description alone" in gate
    assert "When unsure, continue." in gate


@pytest.mark.parametrize(
    "out_of_scope",
    ["study or practice", "algorithms, interviews", "writing", "marketing or product reviews",
     "personal tasks"],
)
def test_the_gate_names_what_is_out_of_scope(out_of_scope):
    assert out_of_scope in " ".join(_gate(_source()).split())


def test_the_gate_stays_short():
    assert len(_gate(_source()).replace("\r\n", "\n").encode("utf-8")) <= GATE_MAX_BYTES


@pytest.mark.parametrize("target", sorted(RENDERS_BEFORE_THE_GATE))
def test_every_start_render_is_its_render_before_the_gate_plus_the_gate(target):
    kwargs, size, digest = RENDERS_BEFORE_THE_GATE[target]
    body = _body(render(_source(), **kwargs))

    without = body.replace(_gate(body), "", 1).replace("\r\n", "\n").encode("utf-8")

    assert (len(without), hashlib.sha256(without).hexdigest()[:16]) == (size, digest)


@pytest.mark.parametrize("mcp", [False, True], ids=["files", "mcp"])
@pytest.mark.parametrize("mode", [Mode.DAILY, Mode.RESEARCH, None])
def test_every_start_render_opens_with_the_gate_before_any_step(mcp, mode):
    body = _body(render(_source(), mcp=mcp, mode=mode))

    assert body.index(GATE_HEADING) < body.index("## 0. Mode") < body.index("engmem search")
    assert _gate(body) == _gate(_source())


def test_the_start_description_names_engineering_work_on_a_codebase():
    front = _source().split("---\n")[1]
    description = next(line for line in front.splitlines() if line.startswith("description:"))

    assert "engineering task on a codebase" in description


@pytest.mark.parametrize("work", ["changing", "debugging", "investigating", "choosing an approach"])
def test_the_gate_counts_debugging_and_investigating_as_engineering_work(work):
    gate = " ".join(_gate(_source()).split())

    assert work in gate[: gate.index("reply")]
