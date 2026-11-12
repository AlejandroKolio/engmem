"""Regex grading: what an arm would answer from what reached its context, and the check against a key."""

from __future__ import annotations

import pytest

from agentsim import grading
from agentsim.profiles import LOAD, SEARCH
from agentsim.trace import TEMPLATE, TOOL, Step, Transcript

pytestmark = pytest.mark.e2e

REQUIRED = grading.compile_all([r"\b64 connections"])
STALE = grading.compile_all([r"\b200 connections"])
OLD, NEW = "20251104-pool", "20260521-pool-cap"


def _read(source: str, text: str) -> Step:
    return Step(LOAD, TOOL, f"Read {source}.md", 0.0, text, source=source)


def _grade(*steps: Step, newest_only: bool = False, subjects: dict | None = None) -> grading.Grade:
    transcript = Transcript()
    for step in steps:
        transcript.add(step)
    return grading.grade(transcript, REQUIRED, STALE, newest_only=newest_only, subjects=subjects or {})


def test_the_current_value_alone_is_correct():
    assert _grade(_read(NEW, "cap at 64 connections")).correct


def test_an_unflagged_stale_value_beside_the_current_one_is_wrong():
    result = _grade(_read(OLD, "pool of 200 connections"), _read(NEW, "cap at 64 connections"))
    assert result.required_ok and not result.stale_avoided and not result.correct


@pytest.mark.parametrize(
    "flag_text",
    [
        f"---\nstatus: superseded\nsuperseded_by: {NEW}\n---\npool of 200 connections",
        "pool of 200 connections",
    ],
    ids=["front-matter", "redirect-line"],
)
def test_a_document_the_context_marks_superseded_is_not_answered_from(flag_text):
    steps = [_read(OLD, flag_text), _read(NEW, "cap at 64 connections")]
    if "status" not in flag_text:
        steps.insert(0, Step(SEARCH, TOOL, "engmem search", 0.0, f"{OLD}: superseded by {NEW}\n"))
    result = _grade(*steps)
    assert result.correct and result.stale_seen


def test_a_chain_redirect_flags_every_link_but_the_last():
    text = "a-1: superseded by b-2 → c-3 → d-4 (successor is still a draft)\n"
    flags = grading.flagged([grading.Segment("", text)])
    assert flags == {"a-1": "superseded", "b-2": "superseded", "c-3": "superseded"}


def test_a_related_line_flags_the_superseded_related_document():
    text = f"related: {OLD} (superseded by {NEW}, /x.md), other (/y.md)\n"
    assert grading.flagged([grading.Segment(NEW, text)]) == {OLD: "superseded"}


def test_a_covered_file_change_flags_the_note_and_the_code_read_answers():
    block = (
        f"### {OLD} (score: 9.0)\nsnapshot: svc abc: HEAD is now def, covered file changed: src/a.kt, re-check\n"
        "pool of 200 connections\n"
    )
    search = Step(SEARCH, TOOL, "engmem search", 0.0, block)
    code = Step(LOAD, TOOL, "Read src/a.kt", 0.0, "val pool = 64 connections", source="code:src/a.kt")
    assert _grade(search, _read(OLD, "pool of 200 connections"), code).correct
    assert not _grade(search, _read(OLD, "pool of 200 connections")).correct


def test_a_non_empty_git_log_flags_its_note_and_an_empty_one_does_not():
    note = _read(OLD, "pool of 200 connections")
    changed = Step(LOAD, TOOL, "git log", 0.0, "abc1234 tune\n", source=f"{grading.GIT_LOG}{OLD}")
    quiet = Step(LOAD, TOOL, "git log", 0.0, "", source=f"{grading.GIT_LOG}{OLD}")
    assert grading.flagged(grading.segments(_transcript(note, changed))) == {OLD: "changed"}
    assert grading.flagged(grading.segments(_transcript(note, quiet))) == {}


def _transcript(*steps: Step) -> Transcript:
    transcript = Transcript()
    for step in steps:
        transcript.add(step)
    return transcript


def test_a_guided_arm_answers_from_the_newest_note_on_a_subject_and_an_unguided_one_does_not():
    subjects = {"20240101-clock-1": "clock cutoff", "20260101-clock-9": "clock cutoff"}
    old = _read("20240101-clock-1", "pool of 200 connections")
    new = _read("20260101-clock-9", "cap at 64 connections")
    assert _grade(old, new, newest_only=True, subjects=subjects).correct
    assert not _grade(old, new, newest_only=False, subjects=subjects).correct


def test_search_output_alone_points_at_a_document_and_is_no_answer():
    search = Step(SEARCH, TOOL, "engmem search", 0.0, f"### {NEW} (score: 5.0)\ncap at 64 connections\n")
    assert not _grade(search).correct
    assert _grade(search, _read(NEW, "primer only")).correct


def test_templates_never_count_as_knowledge():
    template = Step(LOAD, TEMPLATE, "installed copy", 0.0, "cap at 64 connections", source=NEW)
    assert not _grade(template).correct


def test_content_grep_lines_are_split_by_their_file():
    text = (
        f"sessions/{OLD}.md:12:pool of 200 connections\nsessions/{OLD}.md-13-next\n--\n"
        f"sessions/{NEW}.md:4:cap at 64 connections\n"
    )
    found = grading.segments(_transcript(Step(SEARCH, TOOL, "rg -n -i -C1 -e pool sessions", 0.0, text)))
    assert {s.source: s.text for s in found if s.source} == {
        OLD: "pool of 200 connections\nnext", NEW: "cap at 64 connections",
    }


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("thinking\nANSWER: 64 connections\nSOURCES: a.md", "64 connections"),
        ("ANSWER: first\nANSWER: second line\nmore\nSOURCES: x", "second line\nmore"),
        ("no answer here", ""),
    ],
    ids=["one", "last-wins", "none"],
)
def test_the_live_answer_is_the_text_after_the_last_answer_line(text, answer):
    assert grading.answer_of(text) == answer


@pytest.mark.parametrize("line", ["covered file changed: src/a.kt", "covered files changed: src/a.kt, src/b.kt"])
def test_a_covered_file_change_is_flagged_in_the_singular_and_the_plural(line):
    block = f"### {OLD} (score: 9.0)\nsnapshot: svc abc: HEAD is now def, {line}, re-check\n"
    assert grading.flagged([grading.Segment(OLD, block)]) == {OLD: "changed"}
