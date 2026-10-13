"""The release build re-checks the sdist against the same top-level allowlist as CI.

Each workflow holds its own copy; when only `ci.yml` learned about `scripts/`, every release
tag failed in `build` after the pull request that added it had passed."""

from __future__ import annotations

import ast
import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _allowed(workflow: str) -> set[str]:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    match = re.search(r"ALLOWED = (\{.*?\})", text, re.DOTALL)
    assert match, f"no ALLOWED set in {workflow}"
    return ast.literal_eval(match.group(1))


def test_release_and_ci_allow_the_same_sdist_entries():
    assert _allowed("release.yml") == _allowed("ci.yml")
