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


def _verdict(required: list[str], stale: list[str], answer: str) -> tuple[bool, bool]:
    return grading.grade_text(answer, grading.compile_all(required), grading.compile_all(stale))


@pytest.mark.parametrize(
    ("required", "answer"),
    [
        (r"three times.*40.second", "retries 3 times with a fixed 40-second pause"),
        (r"three times.*40.second", "retries three times, 40 seconds apart"),
        (r"three times.*40.second", "Retries THREE times with a 40 s backoff"),
        (r"three times.*40.second", "retries 3 times every 40s"),
        (r"three times.*40.second", "retries three times with a forty-second pause"),
        (r"40-second", "a 40 second pause"),
        (r"40-second", "a 40‑second pause"),
        (r"\b45 minutes", "after forty-five minutes"),
        (r"\b45 minutes", "a 45-min idle timeout"),
        (r"\b45 minutes", "a 45 minute idle timeout"),
        (r"\b4\b.*report jobs", "runs four report jobs at once"),
        (r"\b300 milliseconds", "flushes every 300 ms"),
        (r"\b300 milliseconds", "flushes every three hundred milliseconds"),
        (r"\b300 milliseconds", "flushes every 300msec"),
        (r"\b6 hours", "every 6 h"),
        (r"\b6 hours", "every six hrs"),
        (r"\b13 hours", "after thirteen hours"),
        (r"\b12\s*(percent|%)", "a twelve percent jitter"),
        (r"\b12\s*(percent|%)", "a 12 per cent jitter"),
        (r"\b2,?000\s*rows", "two thousand rows per page"),
        (r"\b1,?800 messages", "eighteen hundred messages"),
        (r"\b1,?800 messages", "one thousand eight hundred messages"),
        (r"\b5,?000 keys", "5,000 keys per pass"),
        (r"\b540 requests", "five hundred and forty requests"),
        (r"\b90 days", "ninety days"),
        (r"\b87 days", "87-day retention"),
    ],
)
def test_a_correct_value_in_other_words_or_digits_passes(required, answer):
    assert _verdict([required], [], answer)[0]


@pytest.mark.parametrize(
    ("required", "answer"),
    [
        (r"three times.*40.second", "retries 4 times with a 40-second pause"),
        (r"three times.*40.second", "retries three times with a forty-five-second pause"),
        (r"three times.*40.second", "retries thirty times with a 40-second pause"),
        (r"\b45 minutes", "after forty minutes"),
        (r"\b45 minutes", "after five minutes"),
        (r"\b45 minutes", "after 45 hours"),
        (r"\b8 seconds", "after eighteen seconds"),
        (r"\b8 seconds", "after twenty-eight seconds"),
        (r"\b8 seconds", "after eight hundred seconds"),
        (r"\b8 seconds", "after 8 milliseconds"),
        (r"\b8 seconds", "after 8 minutes"),
        (r"\b4\b.*report jobs", "runs forty report jobs"),
        (r"\b300 milliseconds", "flushes every 300 seconds"),
        (r"\b2,?000\s*rows", "twenty thousand rows"),
        (r"\b2,?000\s*rows", "2,000,000 rows"),
        (r"\b6 pages", "sixteen pages"),
        (r"\b90 days", "nine days"),
    ],
)
def test_a_wrong_value_in_any_wording_still_fails(required, answer):
    assert not _verdict([required], [], answer)[0]


@pytest.mark.parametrize(
    ("required", "stale", "answer", "verdict"),
    [
        (r"\b64 connections", r"\b200 connections", "sixty-four connections", (True, True)),
        (r"\b64 connections", r"\b200 connections", "64 connections, not the old two hundred connections", (True, False)),
        (r"\b8 seconds", r"\b30 seconds", "eight seconds; it was 30-second before", (True, False)),
        (r"\b8 seconds", r"\b30 seconds", "8 s", (True, True)),
        (r"\b95 seconds", r"\b(10|20|45) seconds", "95 seconds", (True, True)),
        (r"\b95 seconds", r"\b(10|20|45) seconds", "ninety-five seconds, up from forty-five seconds", (True, False)),
        (r"\b13 hours", r"\b(2|3|5|7|9|11) hours", "thirteen hours", (True, True)),
        (r"\b13 hours", r"\b(2|3|5|7|9|11) hours", "13 h, previously 11h", (True, False)),
        (r"\b400 entries", r"\b900 entries", "four hundred entries", (True, True)),
        (r"\b400 entries", r"\b900 entries", "nine hundred entries", (False, False)),
        (r"\b4 minutes", r"\b25 minutes", "four minutes", (True, True)),
        (r"\b4 minutes", r"\b25 minutes", "twenty-five minutes", (False, False)),
        (r"\b540 requests", r"\b(?!540\b)\d+\s*requests", "540 requests", (True, True)),
        (r"\b540 requests", r"\b(?!540\b)\d+\s*requests", "540 requests; it used to be four hundred requests",
         (True, False)),
        (r"\b300 milliseconds", r"\b750 milliseconds", "300 ms", (True, True)),
        (r"\b300 milliseconds", r"\b750 milliseconds", "seven hundred fifty ms", (False, False)),
    ],
)
def test_stale_values_are_caught_in_any_wording_and_never_match_the_current_one(required, stale, answer, verdict):
    assert _verdict([required], [stale], answer) == verdict


@pytest.mark.parametrize(
    ("text", "normal"),
    [
        ("Three times, 40-second", "3 times, 40 seconds"),
        ("twenty-eight seconds", "28 seconds"),
        ("one hundred and twenty ms", "120 milliseconds"),
        ("two thousand rows", "2000 rows"),
        ("2,000 rows", "2000 rows"),
        ("12%", "12 percent"),
        ("one two three", "1 2 3"),
        ("one and two", "1 and 2"),
        ("a hundred rows", "100 rows"),
        ("someone stopped the 2 min timer", "someone stopped 2 minutes timer"),
        ("a 5-attempt cap", "5 attempt cap"),
        ("between the ack and the commit", "between ack and commit"),
        ("plan a, then an [a-f]", "plan a, then [a-f]"),
        ("(the ack) and **the** commit", "(ack) and commit"),
        ("3 a day", "3 a day"),
        ("between the\nack", "between the\nack"),
        ("5 sets", "5 sets"),
        ("2 minimum", "2 minimum"),
        (r"\d{2,300}s", r"\d{2,300}s"),
    ],
)
def test_normalisation_rewrites_numbers_units_hyphens_and_case_only(text, normal):
    assert grading.normalise(text) == normal


def test_a_required_phrase_without_articles_matches_an_answer_that_uses_them():
    assert _verdict(["between ack and commit"], [], "A crash between the ack and the commit lost it")[0]
    assert not _verdict(["between ack and commit"], [], "A crash between the ack and a later step")[0]


@pytest.mark.parametrize(
    ("pattern", "holds"),
    [
        ("between the ack", True),
        (r"the\s+ack", True),
        (r"\bthe\b", True),
        (r"an? cent", True),
        ("A cent", True),
        ("between ack and commit", False),
        (r"[a-z]+ ms", False),
        (r"\btheir\b", False),
        ("Thermal", False),
        (r"\a\t", False),
    ],
)
def test_a_regex_holding_an_article_word_is_told_apart(pattern, holds):
    assert grading.article_in(pattern) is holds


C11 = r"\b300 milliseconds", r"\b750 milliseconds"


@pytest.mark.parametrize(
    "answer",
    [
        "MeterFlusher uses 300 ms; the 750 ms in the old note is out of date",
        "MeterFlusher's flush lag limit is 300 milliseconds. Commit 54d6060 lowered it from 750 ms, so the stored "
        "engmem value of 750 ms is out of date.",
        "300 milliseconds (`FLUSH_LAG_LIMIT = 300` in `src/meter/FlushPolicy.kt`). The note's 750 ms was the "
        "original value, before commit 54d6060 changed it.",
        "300 milliseconds (`FLUSH_LAG_LIMIT = 300` in `src/meter/FlushPolicy.kt`). The note's 750 ms is out of date "
        "because commit 54d6060 changed it.",
        "300 ms instead of 750 ms",
        "300 ms, replacing the old seven hundred fifty ms",
        "300 ms; it used to be 750 ms",
        "300 ms (previously 750 ms)",
        "300 ms; 750 ms is no longer used",
        "Changed from 750 ms to 300 ms",
        "300 ms, down from 750 ms",
        "It went from 750 ms to 300 ms",
        "300 ms; the old 750 ms was replaced",
        "The limit was lowered to 300 ms from the old 750 ms",
        "300 ms; the old 750 ms no longer applies",
        "300 milliseconds (`FLUSH_LAG_LIMIT = 300`). The 750 ms in the notes is the older value, replaced in commit "
        "54d6060.",
        "lowered it from 750 ms to 300 ms",
    ],
)
def test_a_stale_value_the_answer_marks_as_past_is_no_stale_answer(answer):
    rules = [grading.compile_all([pattern]) for pattern in C11]
    assert grading.grade_answer(answer, *rules) == (True, True)
    assert grading.grade_text(answer, *rules) == (True, False)


@pytest.mark.parametrize(
    "answer",
    [
        "750 ms",
        "750 ms, not 300 ms",
        "It is 750 ms, previously 300 ms",
        "750 ms replaces the old 300 ms",
        "It was raised to 750 ms; 300 ms was a test value",
        "The old note says 300 ms but the limit is now 750 ms",
        "750 ms is not out of date, 300 ms is",
        "The limit is 750 ms (it was 300 ms before)",
        "It is 750 ms and the old note said 300 ms",
        "seven hundred fifty milliseconds; 300 ms was rejected",
        "The old note is wrong. The limit is 750 ms",
        "It was changed to 750 ms earlier this year",
        "The limit is now 750 ms as in the original note",
        "The decision was 750 ms. A later commit mentions 300 ms but the decision log is authoritative.",
        "The chosen flush lag limit was 750 ms; 300 ms appears only in a test fixture.",
        "From the decision log the flush interval is 750 ms; the 300 ms figure is a draft.",
        "Taken from the note: 750 ms. The 300 ms in the code is a bug.",
        "Instead of 300 ms use 750 ms.",
        "Go with 750 ms as originally decided, not 300 ms.",
        "Use 750 ms: the newer 300 ms was a mistake.",
        "Keep 750 ms \u2014 the 300 ms change was reverted.",
        "The flush interval was set at 750 ms and the retry is 300 ms.",
        "Use 300 ms, not 750 ms",
        "Keep 750 ms since 300 ms is stale",
        "Keep 750 ms as 300 ms is outdated",
        "Keep 750 ms \u2014 300 ms is stale",
        "Keep 750 ms \u2013 300 ms is stale",
        "Keep 750 ms and 300 ms is stale",
        "Roll back to the previous 750 ms limit; 300 ms caused stalls.",
        "Keep the old 750 ms; the 300 ms change broke things.",
        "Restore the former 750 ms; 300 ms is wrong.",
        "Take the value from the 750 ms decision; 300 ms is a typo.",
        "Revert to 750 ms instead of the newer 300 ms.",
        "750 ms is no longer disputed; ignore 300 ms.",
        "Use 750 ms unless it is out of date; 300 ms is in the code.",
        "Use 750 ms if 300 ms is stale.",
        "300 ms, not the old 750 ms",
        "Restore the superseded 750 ms; 300 ms broke ingestion.",
        "Go back to the replaced 750 ms",
        "MeterFlusher uses a flush lag limit of 300 milliseconds (`FlushPolicy.FLUSH_LAG_LIMIT = 300`). It was 750 ms "
        "until commit 54d6060 changed it.",
        "The limit was changed back to the previous 750 ms after 300 ms caused stalls.",
        "Raised back up to the former 750 ms; 300 ms was too low.",
        "The update was reverted from 300 ms back to the old 750 ms.",
        "Raised it to the former 750 ms; 300 ms was too low.",
    ],
)
def test_a_stale_value_stated_as_current_still_fails_beside_a_caveat_elsewhere(answer):
    rules = [grading.compile_all([pattern]) for pattern in C11]
    assert grading.grade_answer(answer, *rules)[1] is False


def test_a_regex_matches_the_text_as_written_even_where_normalisation_would_change_it():
    assert _verdict([r"[0-9]+ ?ms\b"], [], "300 ms")[0]


@pytest.mark.parametrize(
    ("pattern", "normal"),
    [
        (r"three times.*40.second", r"3 times.*40.second"),
        (r"\bsixty-four connections", r"\b64 connections"),
        (r"seal interval\D{0,15}\b40 seconds", r"seal interval\D{0,15}\b40 seconds"),
        (r"\S+\W\B", r"\S+\W\B"),
        (r"13\ hour", r"13 hours"),
        (r"40\-second", r"40 seconds"),
        (r"[0\-9]s", r"[0\-9]s"),
        (r"\d{2,300}", r"\d{2,300}"),
    ],
)
def test_a_regex_is_normalised_around_its_escapes(pattern, normal):
    assert grading.normalise_pattern(pattern) == normal


def test_a_stale_regex_with_escapes_still_catches_the_value_in_words():
    stale = [r"seal interval\D{0,15}\b40 seconds"]
    assert _verdict([r"\b25 seconds"], stale, "25 seconds; the seal interval of forty seconds is gone") == (True, False)


@pytest.mark.parametrize(
    ("required", "answer"),
    [
        (r"\b5 minutes", "1.5 minutes"),
        (r"\b30 seconds", "0.30 seconds"),
        (r"\b8 seconds", "0.8 seconds"),
        (r"\b12\s*(percent|%)", "1.12 percent"),
        (r"\b12\s*(percent|%)", "1,12 %"),
    ],
)
def test_the_fraction_of_a_decimal_is_not_the_value(required, answer):
    assert not _verdict([required], [], answer)[0]


@pytest.mark.parametrize("answer", ["5 minutes", "five minutes", "0.5 minutes at first, then 5 minutes", "step 2. 5 minutes"])
def test_a_whole_value_still_passes_beside_a_decimal(answer):
    assert _verdict([r"\b5 minutes"], [], answer)[0]


def test_a_stale_decimal_fraction_is_not_a_stale_value():
    assert _verdict([r"\b8 seconds"], [r"\b30 seconds"], "8 seconds, not 0.30 seconds") == (True, True)
