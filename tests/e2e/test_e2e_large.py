"""The large tier, opt-in via ENGMEM_E2E_LARGE: the scaling scenario on thousands of documents.
It records the outcome in its report and asserts only that every profile ran on the large store."""

from __future__ import annotations

import os
import shutil

import pytest

from agentsim import harness, report

pytestmark = pytest.mark.e2e

LARGE = os.environ.get("ENGMEM_E2E_LARGE", "")
BYTES_PER_DOC = 8_000
COPIES = 5
HEADROOM = 2 * 1024**3


@pytest.mark.skipif(not LARGE.isdigit(), reason="opt-in: set ENGMEM_E2E_LARGE to a document count, e.g. 3000")
def test_the_scaling_scenario_on_a_large_store(tmp_path):
    documents = int(LARGE)
    free = shutil.disk_usage(tmp_path).free
    if free - documents * BYTES_PER_DOC * COPIES < HEADROOM:
        pytest.skip(f"only {free // 1024**2} MB free")
    plan = harness.Plan(
        scale_filler=documents,
        tasks=("scale",),
        profiles=("shell-claude", "mcp-only", "grep", "no-history"),
    )
    results = harness.run(tmp_path / "out", plan)
    (tmp_path / "out" / "report.md").write_text(report.render(results), encoding="utf-8")
    assert results["meta"]["scale_store_docs"] >= documents
    assert {r["profile"] for r in results["runs"]} == set(plan.profiles)
    assert all(r["store_docs"] >= documents for r in results["runs"])
    print(report.render(results))
