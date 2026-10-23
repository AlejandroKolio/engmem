"""What the workflows themselves must hold: the same sdist allowlist in CI and release, every
action pinned to a commit, and no cache restored into the job that builds what PyPI publishes.

Each workflow holds its own allowlist; when only `ci.yml` learned about `scripts/`, every release
tag failed in `build` after the pull request that added it had passed."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _allowed(workflow: str) -> set[str]:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    match = re.search(r"ALLOWED = (\{.*?\})", text, re.DOTALL)
    assert match, f"no ALLOWED set in {workflow}"
    return ast.literal_eval(match.group(1))


def test_release_and_ci_allow_the_same_sdist_entries():
    assert _allowed("release.yml") == _allowed("ci.yml")


def test_every_action_is_pinned_to_a_commit_with_its_release_named():
    """A tag or a branch can be moved under a workflow, and the publish step holds the PyPI
    token; a SHA cannot move. The comment is what Dependabot reads and raises."""
    uses = [
        (workflow.name, line.strip())
        for workflow in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if re.match(r"\s*(?:- )?uses:", line)
    ]
    pinned = re.compile(r"(?:- )?uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")

    assert uses
    assert [u for u in uses if not pinned.fullmatch(u[1])] == []


def test_the_job_that_builds_what_pypi_publishes_restores_no_cache():
    """A tag push can restore a cache that a main-branch run wrote; whatever it holds must not
    reach the artifact that is published."""
    jobs = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))["jobs"]
    setup = [
        step for step in jobs["build"]["steps"]
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]

    assert setup and all(step.get("with", {}).get("enable-cache") is False for step in setup)
