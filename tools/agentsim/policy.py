"""The declared policy that replaces an agent's reasoning, the same for every profile (contracts/e2e.md)."""

from __future__ import annotations

import re
from dataclasses import dataclass

STOPWORDS = frozenset(
    """
    a an the and or but if when then than so to of in on at by for with without from into onto
    is are was were be been being it its this that these those we us our you your they their
    add make change check set pick choose decide raise list use get let keep do does did done
    after before last time also only just more most less some any each every all which what how
    why who where there here not no new old sooner faster quicker
    """.split()
)
MAX_KEY_TERMS = 8
OUT_OF_SCOPE_CLASS = 9
OUT_OF_SCOPE_WORDS = frozenset(
    """
    interview practise practice study homework essay blog newsletter marketing holiday vacation
    recipe birthday
    """.split()
)
MAX_DOCS_LOADED = 5
READ_ROLES = ("primer", "decisions")
POINTED_SECTIONS = {"lessons": "Lessons Learned", "production": "Production Considerations"}
ASKED_STEMS = {"lessons": ("pitfall", "fail", "cause", "lesson"), "production": ("production", "setting", "limit", "config")}
GREP_TOP_LINES = 20
RESULTS_SHOWN = 3
RELEVANT_TERMS = 2
RELEVANT_COMPONENTS = 1
NONE_FOUND = "prior context: none found"
SUPERSEDED_LINE = re.compile(r"^(\S+)(?:\s+\[weak candidate\])?: superseded by (.+)$", re.MULTILINE)
HEADING = re.compile(r"^### (\S+) \((?:score|successor)", re.MULTILINE)
CITE_AS = re.compile(r"^cite as \(Reuse Log prior-doc\): (.+)$", re.MULTILINE)
RELATED = re.compile(r"^related: (.+)$", re.MULTILINE)
PATH_LINE = re.compile(r"^path: .*$", re.MULTILINE)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_IDENTIFIER = re.compile(
    r"`([^`]+)`"
    r"|\b[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\b"
    r"|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"
    r"|\b[A-Za-z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b"
)
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def identifiers(description: str) -> list[str]:
    """Backticked text, dotted names, SCREAMING_SNAKE and CamelCase tokens, verbatim, in order."""
    found: list[str] = []
    for match in _IDENTIFIER.finditer(description):
        token = match.group(1) or match.group(0)
        if token.lower() not in (f.lower() for f in found):
            found.append(token)
    return found


def key_terms(description: str) -> list[str]:
    """The task's identifiers first and verbatim, then its words minus stopwords, in order, without
    repeats; the words are capped at `MAX_KEY_TERMS`, the identifiers are not."""
    named = identifiers(description)
    words: list[str] = []
    for word in _WORD.findall(description):
        if word.lower() in STOPWORDS or len(word) < 3:
            continue
        if word.lower() not in (s.lower() for s in [*named, *words]):
            words.append(word)
    return named + words[:MAX_KEY_TERMS]


def engineering_task(description: str) -> bool:
    """False when the description names study, writing, marketing or a personal errand."""
    return not any(word.lower() in OUT_OF_SCOPE_WORDS for word in _WORD.findall(description))


def asked_roles(description: str, primer: str) -> list[str]:
    """The daily template's third read: each role whose heading the primer names, or one of whose
    words the task uses, in the template's order."""
    words = [word.lower() for word in _WORD.findall(description)]
    return [
        role for role, heading in POINTED_SECTIONS.items()
        if heading.lower() in primer.lower() or any(w.startswith(ASKED_STEMS[role]) for w in words)
    ]


def component_terms(files: tuple[str, ...]) -> list[str]:
    """The second query: the names of the files the task touches, as the agent sees them."""
    return [path.rsplit("/", 1)[-1].split(".", 1)[0] for path in files]


def term_visible(term: str, text: str) -> bool:
    """`term` in `text`, whole or as all of its camelCase parts, ignoring case."""
    lowered = text.lower()
    if term.lower() in lowered:
        return True
    parts = [p.lower() for p in _CAMEL.findall(term)]
    return len(parts) > 1 and all(p in lowered for p in parts)


def visible_terms(terms: list[str], text: str) -> int:
    return sum(1 for term in terms if term_visible(term, text))


def relevant(terms: list[str], text: str, minimum: int = RELEVANT_TERMS) -> bool:
    """A result counts as relevant when its visible text, less the store path engmem prints, shows
    `minimum` distinct terms."""
    return visible_terms(terms, PATH_LINE.sub("", text)) >= minimum


@dataclass(frozen=True)
class SearchView:
    """What an agent reads off one engmem search response."""

    blocks: dict[str, str]
    order: list[str]
    citations: dict[str, str]
    redirects: dict[str, str]
    related: dict[str, list[str]]


def _split_cite(line: str) -> dict[str, str]:
    citations = {}
    for item in line.split(","):
        reference = item.strip()
        if "@" in reference:
            citations[reference.split("@", 1)[0]] = reference
    return citations


def _related_targets(line: str) -> list[str]:
    """Each related id, or its successor when the line says it was superseded."""
    targets = []
    for part in re.split(r",\s*(?![^()]*\))", line):
        part = part.strip()
        successor = re.search(r"\(superseded by (\S+?),", part)
        if successor:
            targets.append(successor.group(1))
        elif "(superseded" not in part and part:
            targets.append(part.split(" ", 1)[0])
    return targets


def _chain_end(chain: str) -> str:
    """The last id of a redirect's chain `b → c → d`, without a trailing note in brackets."""
    return chain.split(" → ")[-1].split(" ", 1)[0]


def read_search(text: str) -> SearchView:
    """Result blocks in rank order, their `cite as` references, redirects to the end of each chain,
    and related ids. A successor block is read together with the redirect lines that lead to it."""
    redirect_lines = {m.group(1): (_chain_end(m.group(2)), m.group(0)) for m in SUPERSEDED_LINE.finditer(text)}
    starts = [(m.start(), m.group(1)) for m in HEADING.finditer(text)]
    blocks: dict[str, str] = {}
    related: dict[str, list[str]] = {}
    for index, (start, doc_id) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        block = text[start:end]
        cut = SUPERSEDED_LINE.search(block)
        block = block[: cut.start()] if cut else block
        leading = [line for target, line in redirect_lines.values() if target == doc_id]
        blocks[doc_id] = "\n".join([*leading, block])
        match = RELATED.search(block)
        if match:
            related[doc_id] = _related_targets(match.group(1))
    citations: dict[str, str] = {}
    for match in CITE_AS.finditer(text):
        citations.update(_split_cite(match.group(1)))
    return SearchView(
        blocks=blocks,
        order=[doc_id for _, doc_id in starts],
        citations=citations,
        redirects={doc_id: target for doc_id, (target, _) in redirect_lines.items()},
        related=related,
    )


def worth_following(terms: list[str], text: str, minimum: int = RELEVANT_TERMS) -> bool:
    """The one gate both sides use to load a result and to follow its successor: it is relevant."""
    return relevant(terms, text, minimum)


def documents_to_load(view: SearchView, terms: list[str], minimum: int = RELEVANT_TERMS) -> list[str]:
    """Relevant found results in rank order (successors for superseded ones), then related ids; five in all."""
    chosen: list[str] = []
    for doc_id in view.order:
        if len(chosen) == MAX_DOCS_LOADED:
            break
        if not worth_following(terms, view.blocks[doc_id], minimum):
            continue
        target = view.redirects.get(doc_id, doc_id)
        if target not in chosen:
            chosen.append(target)
    for doc_id in list(chosen):
        for related_id in view.related.get(doc_id, []):
            if len(chosen) >= MAX_DOCS_LOADED:
                return chosen
            target = view.redirects.get(related_id, related_id)
            if target not in chosen:
                chosen.append(target)
    return chosen[:MAX_DOCS_LOADED]


def any_relevant(view: SearchView, terms: list[str]) -> bool:
    return any(relevant(terms, block) for block in view.blocks.values())
