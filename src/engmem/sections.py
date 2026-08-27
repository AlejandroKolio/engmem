"""Splits a document body into sections and resolves headings to a stable canonical role."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# see contracts/sections.md for the measurement behind this cap
MAX_SECTION_BYTES = 4096

# `(?:[ \t]+#+)?` eats ATX's optional closing `## X ##` so the hashes don't leak
# into the heading text; the space before it keeps `C#` intact.
_H2_RE = re.compile(r"^##[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.MULTILINE)
_H3_RE = re.compile(r"^###[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)$", re.MULTILINE)


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    # only matched fence pairs count; an unclosed fence must not swallow every
    # heading after it to end of document
    spans: list[tuple[int, int]] = []
    opened = None
    for m in _FENCE_RE.finditer(text):
        marker, info = m.group(1), m.group(2)
        if opened is None:
            opened = (m.start(), marker[0], len(marker))
            continue
        start, char, width = opened
        # closer: same character, no shorter, no info string
        if marker[0] == char and len(marker) >= width and not info.strip():
            spans.append((start, m.end()))
            opened = None
    return spans


def _headings(pattern: re.Pattern[str], text: str) -> list[re.Match[str]]:
    # skip headings inside fenced code (sample markdown/shell output, not real structure)
    spans = _fenced_spans(text)
    return [m for m in pattern.finditer(text)
            if not any(a <= m.start() < b for a, b in spans)]

# stripped before slugifying so an anchor survives renumbering that touches no content
_ORDINAL_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+")
_SLUG_STRIP_RE = re.compile(r"[^0-9a-z]+")
_WHITESPACE_RE = re.compile(r"\s+")

# see contracts/sections.md for the measured spelling table this encodes
CANONICAL_ALIASES: dict[str, str] = {
    "decision log": "decisions",
    "decision log & scope": "decisions",
    "executive summary": "summary",
    "testing knowledge": "testing",
    "search keywords": "keywords",
    "one-page cheat sheet": "cheatsheet",
    "production considerations": "production",
    "functional requirements": "requirements",
    "knowledge graph": "graph",
    "business context": "context",
    "domain & technical glossary": "context",
    "glossary": "context",
    "system architecture": "architecture",
    "architecture": "architecture",
    "future llm context (cold-start primer)": "primer",
    "future llm context": "primer",
    "cold-start primer": "primer",
    "future-llm cold-start primer": "primer",
    "acceptance criteria analysis": "acceptance",
    "acceptance criteria status": "acceptance",
    "acceptance criteria": "acceptance",
    "lessons learned": "lessons",
    "lessons learned & traps": "lessons",
    "lessons learned / defects caught": "lessons",
    "landmines": "lessons",
    "end-to-end flow": "flow",
    "end-to-end business flow": "flow",
    "code implementation": "code",
    "status at a glance": "status",
    "status board & todo": "status",
    "pre-reg": "prereg",
    "reuse log": "reuse",
    "search trace": "trace",
}

# every role also resolves to its own bare name, derived rather than hand-listed
# so a role added above cannot be forgotten here
CANONICAL_ALIASES.update({role: role for role in set(CANONICAL_ALIASES.values())})

CANONICAL_ROLES: tuple[str, ...] = tuple(sorted(set(CANONICAL_ALIASES.values())))


@dataclass
class Section:
    anchor: str
    heading: str
    body: str
    size_bytes: int
    level: int
    canonical: str | None = None
    # 1-based, document order; a display convenience only — `anchor`, not this,
    # is the identity that survives a nearby insertion or removal
    index: int = 0

    @property
    def locator(self) -> str:
        return f"§{self.index}-{self.anchor}"


def _strip_ordinal(heading: str) -> str:
    return _ORDINAL_PREFIX_RE.sub("", heading.strip())


def _normalize_for_alias(heading: str) -> str:
    text = unicodedata.normalize("NFKC", _strip_ordinal(heading))
    return _WHITESPACE_RE.sub(" ", text.strip()).casefold()


def _slugify(heading: str) -> str:
    # NFKD then drop combining marks, so `Müller` slugs to `muller`, not `m-ller`.
    decomposed = unicodedata.normalize("NFKD", _strip_ordinal(heading))
    text = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    slug = _SLUG_STRIP_RE.sub("-", text).strip("-")
    return slug or "section"


_TRAILING_PARENTHETICAL_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _fold_trailing_parenthetical(normalized_heading: str) -> str | None:
    # strips one outermost trailing "(...)" and retries the alias table; bounded narrowly on
    # purpose — see contracts/sections.md for why an unbounded prefix match is wrong here
    stripped = _TRAILING_PARENTHETICAL_RE.sub("", normalized_heading).strip()
    if not stripped or stripped == normalized_heading:
        return None
    return stripped


def _canonical_for(heading: str) -> str | None:
    if not heading:
        return None
    normalized = _normalize_for_alias(heading)
    canonical = CANONICAL_ALIASES.get(normalized)
    if canonical is not None:
        return canonical
    folded = _fold_trailing_parenthetical(normalized)
    if folded is not None:
        return CANONICAL_ALIASES.get(folded)
    return None


def _make_section(
    anchor: str, heading: str, content: str, level: int, inherited: str | None = None
) -> Section:
    canonical = _canonical_for(heading) or inherited
    return Section(
        anchor=anchor,
        heading=heading,
        body=content,
        size_bytes=len(content.encode("utf-8")),
        level=level,
        canonical=canonical,
    )


def _split_oversized(heading: str, content: str) -> list[Section]:
    # text before the first "###" keeps the parent heading (it's lead-in, not a
    # subsection); a section with no "###" to split on is returned whole rather
    # than chunked at invented boundaries
    h3_matches = _headings(_H3_RE, content)
    if not h3_matches:
        return [_make_section(_slugify(heading), heading, content, 2)]

    # a subsection inherits the parent's role unless its own heading names one: splitting is
    # a size decision, and a role that survived only on the lead-in disappeared entirely
    # whenever the section opened straight onto its first "###"
    parent_role = _canonical_for(heading)

    sections: list[Section] = []
    lead = content[: h3_matches[0].start()].strip("\n")
    if lead.strip():
        sections.append(_make_section(_slugify(heading), heading, lead, 2))

    for i, hm in enumerate(h3_matches):
        start = hm.end()
        end = h3_matches[i + 1].start() if i + 1 < len(h3_matches) else len(content)
        sub_heading = _strip_ordinal(hm.group(1))
        sub_content = content[start:end].strip("\n")
        sections.append(
            _make_section(_slugify(sub_heading), sub_heading, sub_content, 3, parent_role)
        )

    return sections


_SETEXT_H2_RE = re.compile(
    r"^(?P<text>[^\s#>|*+-][^\n]*?)[ \t]*\n-{3,}[ \t]*$", re.MULTILINE
)


def _rewrite_setext(body: str) -> str:
    # `Title` over `---` becomes `## Title`; only `---` folds (never `===`, the
    # document's own title) and the text line can't start with a list/table/quote/
    # heading marker, so this project's own horizontal rules stay rules
    spans = _fenced_spans(body)
    def fold(m: re.Match[str]) -> str:
        if any(a <= m.start() < b for a, b in spans):
            return m.group(0)
        return f"## {m.group('text').strip()}"
    return _SETEXT_H2_RE.sub(fold, body)


def split_sections(body: str) -> list[Section]:
    """Split on `##`; any piece over `MAX_SECTION_BYTES` splits further on `###`."""
    if not body or not body.strip():
        return []

    body = _rewrite_setext(body)

    h2_matches = _headings(_H2_RE, body)
    if not h2_matches:
        content = body.strip("\n")
        sections = [_make_section("body", "", content, 2)] if content else []
        for idx, section in enumerate(sections, start=1):
            section.index = idx
        return sections

    sections: list[Section] = []

    preamble = body[: h2_matches[0].start()].strip("\n")
    if preamble.strip():
        sections.append(_make_section("preamble", "", preamble, 2))

    for i, m in enumerate(h2_matches):
        start = m.end()
        end = h2_matches[i + 1].start() if i + 1 < len(h2_matches) else len(body)
        heading = _strip_ordinal(m.group(1))
        content = body[start:end].strip("\n")

        if len(content.encode("utf-8")) <= MAX_SECTION_BYTES:
            sections.append(_make_section(_slugify(heading), heading, content, 2))
        else:
            sections.extend(_split_oversized(heading, content))

    for idx, section in enumerate(sections, start=1):
        section.index = idx
    return sections


def role_is_inherited(section: Section) -> bool:
    """True when the section carries a role its own heading does not name — a `###` piece of an
    oversized parent. Real, but second-best: it loses to a section headed with the role."""
    return section.canonical is not None and _canonical_for(section.heading) is None


def sections_for_role(sections: list[Section], role: str) -> list[Section]:
    """Every section carrying `role`, in document order. More than one when an oversized
    section was split: the role's content is then spread across all of them."""
    return [s for s in sections if s.canonical == role]


def sections_by_locator(sections: list[Section]) -> dict[str, Section]:
    """Both the literal slug and the canonical alias resolve to the same section."""
    index: dict[str, Section] = {}
    for s in sections:
        index.setdefault(s.anchor, s)
        if s.canonical and not role_is_inherited(s):
            index.setdefault(s.canonical, s)
    # a second pass, so an inherited role only fills a gap: `## Glossary` still answers for
    # `context` even when an oversized `## Business Context` split earlier in the document
    for s in sections:
        if s.canonical:
            index.setdefault(s.canonical, s)
    return index
