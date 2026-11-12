"""Regex grading: the answer an arm would give from what reached its context, and its check against a key."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentsim.profiles import LOAD
from agentsim.trace import TEMPLATE, Transcript

CODE = "code:"
GIT_LOG = "gitlog:"
CURRENT = "99999999"
ENGMEM_SEARCHES = ("engmem search", "engmem_search", "engmem_search_by_role", "user runs engmem search")
BLOCK = re.compile(r"^### (\S+) \((?:score|successor)", re.MULTILINE)
REDIRECT = re.compile(r"^(\S+)(?:\s+\[weak candidate\])?: superseded by (.+)$", re.MULTILINE)
RELATED_SUPERSEDED = re.compile(r"(?:^|[\s,\[])(\S+?) \(superseded(?: by|\))")
FRONT_SUPERSEDED = re.compile(r"^status: superseded\s*$", re.MULTILINE)
RG_LINE = re.compile(r"^(?:\S*/)?sessions/([^/:\s]+)\.md[:-]\d+[:-](.*)$")
COVERED_CHANGED = re.compile(r"covered files? changed:")
DATE = re.compile(r"^(\d{8})-")


@dataclass(frozen=True)
class Segment:
    """Text that entered the context, with the document or file it came from (empty when none)."""

    source: str
    text: str
    opened: bool = False


@dataclass(frozen=True)
class Grade:
    """Each regex judged against the answer; `stale_seen` judges the whole context instead."""

    required_ok: bool
    stale_avoided: bool
    correct: bool
    stale_seen: bool
    answer_sources: tuple[str, ...]
    opened: frozenset[str] = frozenset()


def compile_all(patterns) -> list[re.Pattern]:
    """Case-insensitive `re` patterns; a pattern that does not compile raises `re.error`."""
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def _search_segments(text: str) -> list[Segment]:
    starts = [(m.start(), m.group(1)) for m in BLOCK.finditer(text)]
    head_end = starts[0][0] if starts else len(text)
    found = [Segment("", text[:head_end])]
    for index, (start, doc_id) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        found.append(Segment(doc_id, text[start:end]))
    return found


def _rg_segments(text: str) -> list[Segment]:
    grouped: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = RG_LINE.match(line)
        if match:
            grouped.setdefault(match.group(1), []).append(match.group(2))
    return [Segment(doc_id, "\n".join(lines)) for doc_id, lines in grouped.items()]


def segments(transcript: Transcript) -> list[Segment]:
    """Every tool output split by the document it shows; templates never count as knowledge."""
    found: list[Segment] = []
    for step in transcript.steps:
        if step.kind == TEMPLATE:
            continue
        if step.source:
            found.append(Segment(step.source, step.text, opened=step.phase == LOAD))
        elif step.action in ENGMEM_SEARCHES:
            found += _search_segments(step.text)
        elif step.action.startswith("rg ") and " -n " in f" {step.action} ":
            found += _rg_segments(step.text)
        else:
            found.append(Segment("", step.text))
    return found


def flagged(found: list[Segment]) -> dict[str, str]:
    """Documents the context marks as not current: superseded, or a covered file changed since."""
    flags: dict[str, str] = {}
    for segment in found:
        for match in REDIRECT.finditer(segment.text):
            chain = [match.group(1), *(part.split(" ", 1)[0] for part in match.group(2).split(" → "))]
            for doc_id in chain[:-1]:
                flags.setdefault(doc_id, "superseded")
        for match in RELATED_SUPERSEDED.finditer(segment.text):
            flags.setdefault(match.group(1), "superseded")
        if segment.source.startswith(GIT_LOG):
            if segment.text.strip():
                flags.setdefault(segment.source[len(GIT_LOG):], "changed")
            continue
        if not segment.source or segment.source.startswith(CODE):
            continue
        if FRONT_SUPERSEDED.search(segment.text):
            flags.setdefault(segment.source, "superseded")
        if COVERED_CHANGED.search(segment.text) and BLOCK.match(segment.text):
            flags.setdefault(segment.source, "changed")
    return flags


def _date(source: str) -> str:
    if source.startswith(CODE):
        return CURRENT
    match = DATE.match(source)
    return match.group(1) if match else ""


def evidence(found: list[Segment], *, newest_only: bool, subjects: dict[str, str]) -> list[Segment]:
    """What it saw of the documents it opened, minus flagged ones; a guided arm keeps only the newest
    on a subject. Search output alone is no answer; code read from the checkout is current."""
    flags = flagged(found)
    opened = {s.source for s in found if s.opened}
    kept = [
        s for s in found
        if s.source in opened and not s.source.startswith(GIT_LOG) and s.source not in flags
    ]
    if not newest_only:
        return kept
    newest: dict[str, str] = {}
    for segment in kept:
        if segment.source.startswith(CODE):
            continue
        subject = subjects.get(segment.source, segment.source)
        newest[subject] = max(newest.get(subject, ""), _date(segment.source))
    return [
        s for s in kept
        if s.source.startswith(CODE) or _date(s.source) >= newest[subjects.get(s.source, s.source)]
    ]


def grade_text(answer: str, required: list[re.Pattern], stale: list[re.Pattern]) -> tuple[bool, bool]:
    """`(every required regex matches, no stale regex matches)`."""
    return all(p.search(answer) for p in required), not any(p.search(answer) for p in stale)


def grade(
    transcript: Transcript,
    required: list[re.Pattern],
    stale: list[re.Pattern],
    *,
    newest_only: bool,
    subjects: dict[str, str],
) -> Grade:
    """Layer 1: the answer is the arm's evidence; it is correct when it holds every required fact
    and no stale value."""
    found = segments(transcript)
    kept = evidence(found, newest_only=newest_only, subjects=subjects)
    answer = "\n".join(s.text for s in kept)
    required_ok, stale_avoided = grade_text(answer, required, stale)
    stale_seen = any(p.search(s.text) for s in found for p in stale)
    sources = tuple(dict.fromkeys(s.source for s in kept))
    opened = frozenset(s.source for s in found if s.opened)
    return Grade(required_ok, stale_avoided, required_ok and stale_avoided, stale_seen, sources, opened)


ANSWER_LINE = re.compile(r"ANSWER:\s*(.*?)(?=^\s*(?:SOURCES|ANSWER):|\Z)", re.DOTALL | re.MULTILINE)


def answer_of(final_text: str) -> str:
    """Layer 2: the text after the last `ANSWER:` up to `SOURCES:`; empty when there is none."""
    matches = ANSWER_LINE.findall(final_text)
    return matches[-1].strip() if matches else ""
