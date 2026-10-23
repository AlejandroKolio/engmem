"""What the workflows themselves must hold: the same sdist allowlist in CI and release, every
action pinned to a commit, no cache restored into the job that builds what PyPI publishes, and a
hash-locked build backend and a known uv for every build.

Each workflow holds its own allowlist; when only `ci.yml` learned about `scripts/`, every release
tag failed in `build` after the pull request that added it had passed."""

from __future__ import annotations

import ast
import re
import shlex
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


CONSTRAINTS = ".github/build-constraints.txt"
# each flag with the exact value it must carry; compared as tokens, so a path with a suffix or a
# flag in another place's value does not pass as the right one
BUILD_FLAGS = {"--python": "3.11", "--build-constraints": CONSTRAINTS}
BUILD_SWITCHES = ("--managed-python", "--require-hashes")


def _workflow_steps():
    for workflow in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
        for job_name, job in jobs.items():
            for step in job["steps"]:
                yield workflow.name, job_name, step


def test_every_build_installs_a_hash_locked_backend_with_a_fixed_python():
    """`uv build` installs its build backend outside uv.lock; without the hashed file a new or
    compromised hatchling release would change what PyPI gets with no commit of ours."""
    builds = [
        ((name, job), shlex.split(str(step["run"])))
        for name, job, step in _workflow_steps()
        if re.search(r"\buv\s+build\b", str(step.get("run", "")))
    ]
    by_job = {}
    for where, tokens in builds:
        by_job.setdefault(where, []).append(tokens)

    known = {("release.yml", "build"), ("ci.yml", "packaging"), ("ci.yml", "runtime-dependencies")}
    assert known <= set(by_job)
    for where, tokens in builds:
        values = dict(zip(tokens, tokens[1:]))
        wrong = [flag for flag, value in BUILD_FLAGS.items() if values.get(flag) != value]
        assert wrong == [], (where, tokens)
        assert all(switch in tokens for switch in BUILD_SWITCHES), (where, tokens)
    # CI runs the release's exact command, so the wheel built from the sdist is built on every
    # pull request, not first on a tag
    (release,) = by_job[("release.yml", "build")]
    assert by_job[("ci.yml", "packaging")] == [release]
    assert "--sdist" not in release and "--wheel" not in release


def test_every_setup_uv_installs_a_uv_the_pinned_action_knows_the_checksum_of():
    setups = [
        (name, job, step) for name, job, step in _workflow_steps()
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]

    assert setups
    unpinned = [s[:2] for s in setups if (s[2].get("with") or {}).get("version") != "latest-known"]
    assert unpinned == []


def test_the_build_constraints_pin_every_package_by_version_and_hash():
    text = (WORKFLOWS.parent.parent / CONSTRAINTS).read_text(encoding="utf-8")
    entries = re.split(r"\n(?=[A-Za-z0-9])", text.strip())
    requirements = [e for e in entries if not e.startswith("#")]

    assert any(r.startswith("hatchling==") for r in requirements)
    for entry in requirements:
        assert re.match(r"^[A-Za-z0-9._-]+==[^\s\\]+", entry), entry
        assert "--hash=sha256:" in entry, entry.splitlines()[0]
