"""Regex grading: the answer an arm would give from what reached its context, and its check against a key."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

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


UNITS = {
    "milliseconds": ("milliseconds", "millisecond", "msecs", "msec", "ms"),
    "seconds": ("seconds", "second", "secs", "sec", "s"),
    "minutes": ("minutes", "minute", "mins", "min"),
    "hours": ("hours", "hour", "hrs", "hr", "h"),
    "days": ("days", "day"),
    "percent": ("percent", "per cent", "pct", "%"),
}
_UNIT_OF = {spelling: unit for unit, spellings in UNITS.items() for spelling in spellings}
_UNIT = re.compile(
    r"(\d)[ -]?(" + "|".join(re.escape(s) for s in sorted(_UNIT_OF, key=len, reverse=True)) + r")(?![a-z])"
)
_DIGIT_HYPHEN = re.compile(r"(\d)-(?=[a-z])")
_THOUSANDS = re.compile(r"(?<![{\d.,])\d{1,3}(?:,\d{3})+(?![\d}])")
_DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u00a0": " ", "\u202f": " "})
_SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
          "sixteen seventeen eighteen nineteen").split()
_TENS = {word: 10 * i for i, word in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2)}
_WORDS = {**{word: i for i, word in enumerate(_SMALL)}, **_TENS, "hundred": 100, "thousand": 1000}
_ARTICLE = re.compile(r"(?<![^\s(\[{\"'])(?<!\d )(?<!\d\t)[*_`]*(?:a|an|the)[*_`]*[ \t]+")
_ARTICLE_WORD = re.compile(r"(?<![a-z0-9_])(?:a|an|the)(?![a-z0-9_])", re.IGNORECASE)
_NUMBER_RUN = re.compile(
    r"\b(?:" + "|".join(_WORDS) + r")(?:(?:[ -]|(?:(?<=hundred)|(?<=thousand))\s+and\s+)(?:" + "|".join(_WORDS) + r"))*\b"
)


def _numbers(words: list[str]) -> list[int]:
    """Number words read left to right; a word that cannot extend the number so far starts a new one."""
    found: list[int] = []
    total = group = 0
    last = ""
    for word in words:
        value = _WORDS[word]
        if word == "thousand":
            fits = last in ("small", "tens", "hundred") and total == 0
        elif word == "hundred":
            fits = last in ("small", "tens") and 0 < group < 100
        elif word in _TENS or value >= 10:
            fits = last in ("", "hundred", "thousand") and group % 100 == 0
        else:
            fits = last in ("", "hundred", "thousand") or (last == "tens" and value > 0)
        if not fits and last:
            found.append(total + group)
            total = group = 0
        if word == "thousand":
            total, group = (group or 1) * 1000, 0
        elif word == "hundred":
            group = (group or 1) * 100
        else:
            group += value
        last = word if word in ("hundred", "thousand") else "tens" if word in _TENS else "small"
    return [*found, total + group]


def _number_run(match: re.Match) -> str:
    words = [w for w in re.split(r"[ -]+", match.group(0)) if w not in ("", "and")]
    return " ".join(str(n) for n in _numbers(words))


@lru_cache(maxsize=256)
def normalise(text: str) -> str:
    """Lower case, no standalone articles, number words as digits, no thousands commas, and one spelling
    per unit after a number."""
    text = _ARTICLE.sub("", text.translate(_DASHES).lower())
    text = _NUMBER_RUN.sub(_number_run, text)
    text = _THOUSANDS.sub(lambda m: m.group(0).replace(",", ""), text)
    text = _UNIT.sub(lambda m: f"{m.group(1)} {_UNIT_OF[m.group(2)]}", text)
    return _DIGIT_HYPHEN.sub(r"\1 ", text)


_HIDDEN = 0xE000


def normalise_pattern(pattern: str) -> str:
    """`normalise` for a regex: escapes stay as written, but an escaped space, or an escaped hyphen outside
    a character class, is the plain character, as in the text it matches."""
    kept: list[str] = []
    out: list[str] = []
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            escaped = pattern[index + 1]
            if escaped == " " or (escaped == "-" and not in_class):
                out.append(escaped)
            else:
                kept.append(char + escaped)
                out.append(chr(_HIDDEN + len(kept) - 1))
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        out.append(char)
        index += 1
    hidden = re.compile(f"[{chr(_HIDDEN)}-{chr(_HIDDEN + max(len(kept), 1) - 1)}]")
    return hidden.sub(lambda m: kept[ord(m.group(0)) - _HIDDEN], normalise("".join(out)))


_CLASS = re.compile(r"\[(?:\\.|[^\]\\])*\]")
_ESCAPE = re.compile(r"\\.")


def article_in(pattern: str) -> bool:
    """Whether a regex holds `a`, `an` or `the` as a word outside escapes and character classes; the
    normalised text never does, so such a regex could not match it."""
    return _ARTICLE_WORD.search(_ESCAPE.sub(" ", _CLASS.sub(" ", pattern))) is not None


@dataclass(frozen=True)
class Rule:
    """A key regex: it matches a text as written, or, normalised itself, the normalised text."""

    pattern: str
    written: re.Pattern
    normal: re.Pattern

    def search(self, text: str) -> bool:
        return self.matches(text, normalise(text))

    def matches(self, text: str, normal: str) -> bool:
        """`search` with the text's normalised form already at hand."""
        return _whole_match(self.written, text) or _whole_match(self.normal, normal)

    def states(self, text: str) -> bool:
        """A match, as written or normalised, that the answer does not mark as past (`caveated`)."""
        return any(
            not caveated(source, match)
            for pattern, source in ((self.written, text), (self.normal, normalise(text)))
            for match in _whole_matches(pattern, source)
        )


_FRACTION = re.compile(r"\d[.,]")


def _whole_matches(pattern: re.Pattern, text: str):
    """Every match that does not start inside a decimal: "1.5 minutes" holds no `\\b5 minutes`."""
    position = 0
    while (match := pattern.search(text, position)) is not None:
        start = match.start()
        if not (text[start:start + 1].isdigit() and _FRACTION.fullmatch(text, max(start - 2, 0), start)):
            yield match
        position = start + 1


def _whole_match(pattern: re.Pattern, text: str) -> bool:
    return next(_whole_matches(pattern, text), None) is not None


_CLAUSE_BREAK = re.compile(
    r"[,;:!?()\[\]\n\u2014\u2013]|\.(?!\d)"
    r"|\b(?:and|but|so|because|while|whereas|although|though|which|since|as|unless|if)\b",
    re.IGNORECASE,
)
_PAST_BEFORE = re.compile(
    r"\b(from|replacing|replaces|replaced|instead\s+of|previously|formerly|used\s+to\s+be|old|older|previous|former|"
    r"outdated|stale|superseded|obsolete)(?:[ \t]+(?:the|its|their|old|previous|former|outdated|stale))*[ \t]+\Z",
    re.IGNORECASE,
)
_NEEDS_CHANGE = ("from", "old", "older", "previous", "former")
_CHANGE = re.compile(
    r"\b(?:lowered|raised|changed|reduced|increased|decreased|moved|went|dropped|cut|bumped|shortened|lengthened|"
    r"updated|down|up|from|replacing|replaces|replaced|instead\s+of)\b",
    re.IGNORECASE,
)
_RESTORE = re.compile(
    r"\b(?:roll(?:ed|s|ing)?\s+back|restor\w*|revert\w*\s+to|keep\w*|back(?:\s+(?:up|down))?\s+to)\b", re.IGNORECASE
)
_TO_BEFORE = re.compile(r"\bto[ \t]+(?:(?:the|its|their)[ \t]+)*\Z", re.IGNORECASE)
_PAST_AFTER = re.compile(
    r"\A(?:[ \t]+[^\s,;:!?()\[\]]+){0,4}?[ \t]+(?:"
    r"no\s+longer\s+(?:used|applies|apply|valid|current|in\s+effect|correct|accurate)|"
    r"(?:is|are|was|were|has\s+been|had\s+been)[ \t]+"
    r"(?:out\s+of\s+date|outdated|stale|superseded|obsolete|replaced|"
    r"no\s+longer\s+(?:used|applies|apply|valid|current|in\s+effect|correct|accurate)|"
    r"(?:the[ \t]+)?(?:old|older|original|previous|former|earlier)[ \t]+(?:value|setting|figure|default|limit|one)))\b",
    re.IGNORECASE,
)


def caveated(text: str, match: re.Match) -> bool:
    """Whether the answer marks this very match as past in its own clause: a marker right before it ("lowered
    it from 750 ms") or a status said of it ("750 ms in the note is out of date"); never on a way back to it."""
    before = [b for b in _CLAUSE_BREAK.finditer(text, 0, match.start()) if b.end() <= match.start()]
    lead = text[before[-1].end() if before else 0:match.start()]
    if _RESTORE.search(lead):
        return False
    marker = _PAST_BEFORE.search(lead)
    if marker and (marker.group(1).lower() not in _NEEDS_CHANGE or (
            _CHANGE.search(lead, 0, marker.start()) and not _TO_BEFORE.search(lead[:marker.start()]))):
        return True
    after = _CLAUSE_BREAK.search(text, match.end())
    return _PAST_AFTER.match(text[match.end():after.start() if after else len(text)]) is not None


def compile_all(patterns) -> list[Rule]:
    """Case-insensitive rules; a pattern that does not compile, as written or normalised, raises `re.error`."""
    return [Rule(p, re.compile(p, re.IGNORECASE), re.compile(normalise_pattern(p), re.IGNORECASE)) for p in patterns]


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


def grade_text(answer: str, required: list[Rule], stale: list[Rule]) -> tuple[bool, bool]:
    """`(every required regex matches, no stale regex matches)`."""
    return all(p.search(answer) for p in required), not any(p.search(answer) for p in stale)


def grade_answer(answer: str, required: list[Rule], stale: list[Rule]) -> tuple[bool, bool]:
    """`grade_text` for an agent's own words: a stale value it marks as past is no stale answer."""
    return all(p.search(answer) for p in required), not any(p.states(answer) for p in stale)


def grade(
    transcript: Transcript,
    required: list[Rule],
    stale: list[Rule],
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
