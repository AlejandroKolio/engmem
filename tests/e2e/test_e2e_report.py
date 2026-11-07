"""The report's sides compare what each agent paid to reach the answer, and keep the save apart."""

from __future__ import annotations

import os
import sys

import pytest

from agentsim import report
from agentsim.mcp import McpError, McpSession

pytestmark = pytest.mark.e2e


def _run(profile: str, condition: str, *, correct: bool, to_answer: int, calls: int, save: int = 0) -> dict:
    return {
        "task": "t",
        "scenario_class": 1,
        "class_name": "answer exists",
        "variant": "",
        "profile": profile,
        "condition": condition,
        "outcome": {"correct": correct, "saved": True if save else None},
        "cost": {
            "to_answer_tokens": to_answer,
            "to_answer_calls": calls,
            "retrieval_tokens": to_answer // 2,
            "total_tokens": to_answer + save + 999,
            "save_tokens": save,
            "save_calls": 8 if save else 0,
            "calls": calls,
        },
    }


def test_the_sides_compare_to_answer_cost_and_report_the_save_on_its_own():
    runs = [
        _run(report.PRIMARY, "engmem", correct=True, to_answer=1000, calls=9, save=700),
        _run("grep", "grep", correct=True, to_answer=2000, calls=3),
        _run("no-history", "none", correct=False, to_answer=0, calls=0),
    ]
    strong, weak, ties = report.sides(runs)
    assert any("fewer tokens to the answer: 1000 vs 2000" in line for line in strong)
    assert any("more calls to the answer than grep: 9 vs 3" in line for line in weak)
    assert any("another 700 tokens and 8 calls" in line for line in weak)
    assert not any("1999" in line or "2699" in line for line in strong + weak + ties)


def test_a_difference_inside_the_band_is_a_tie_not_a_side():
    runs = [
        _run(report.PRIMARY, "engmem", correct=True, to_answer=1000, calls=3),
        _run("grep", "grep", correct=True, to_answer=1020, calls=3),
    ]
    strong, weak, ties = report.sides(runs)
    assert ties and not any("tokens to the answer" in line for line in strong + weak)


def test_a_server_that_never_answers_fails_within_the_timeout(tmp_path):
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    session = McpSession(argv, dict(os.environ), tmp_path, timeout=0.5)
    try:
        with pytest.raises(McpError, match="no answer"):
            session.request("ping")
    finally:
        session.close()


def test_grep_chain_reads_are_named_as_the_cause_of_a_token_gap():
    engmem = _run(report.PRIMARY, "engmem", correct=True, to_answer=1000, calls=9)
    grep = _run("grep", "grep", correct=True, to_answer=2000, calls=5)
    grep["steps"] = [
        {"action": "Read a.md", "tokens": 300, "detail": {"superseded_chain": 1}},
        {"action": "Read b.md", "tokens": 400, "detail": {"superseded_chain": 1}},
        {"action": "Read c.md", "tokens": 500},
    ]
    assert report.chain_reads(grep) == (2, 700)
    strong, _, _ = report.sides([engmem, grep])
    line = next(line for line in strong if "fewer tokens to the answer" in line)
    assert "2 whole-file reads of superseded chain links (700 tokens)" in line
    assert "is not a scaling result" not in line
    for run in (engmem, grep):
        run["scenario_class"] = report.SCALE_CLASS
    strong, _, _ = report.sides([engmem, grep])
    line = next(line for line in strong if "fewer tokens to the answer" in line)
    assert "not the store's size" in line and "is not a scaling result" in line


def test_a_gap_without_chain_reads_carries_no_note():
    engmem = _run(report.PRIMARY, "engmem", correct=True, to_answer=1000, calls=9)
    grep = _run("grep", "grep", correct=True, to_answer=2000, calls=5)
    grep["steps"] = [{"action": "Read c.md", "tokens": 500}]
    strong, _, _ = report.sides([engmem, grep])
    assert not any("scaling" in line for line in strong)
