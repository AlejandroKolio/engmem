"""What a document's sections become for ranking, and how they are cached: entries keyed by the
hash of the body they were built from (contracts/scoring.md, "Section index caching";
contracts/cache.md)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from engmem import cache
from engmem.sections import CANONICAL_ROLES, Section, split_sections
from engmem.spine import Doc
from engmem.tokens import camel_expansion, normalize_token, tokenize_raw


def _tokenize_counts(text: str) -> tuple[Counter[str], Counter[str]]:
    """Literal and derived token-frequency counters for a chunk of body text."""
    literal: Counter[str] = Counter()
    derived: Counter[str] = Counter()
    for raw in tokenize_raw(text):
        orig = normalize_token(raw)
        literal[orig] += 1
        derived[orig] += 1
        expansion = camel_expansion(raw)
        if expansion:
            parts_norm, acronym = expansion
            for p in parts_norm:
                derived[p] += 1
            if acronym:
                derived[acronym] += 1
    return literal, derived


@dataclass
class SectionEntry:
    doc_id: str
    section: Section
    literal_tf: Counter[str]
    derived_tf: Counter[str]
    length: int


# `canonical` is the one nullable field, so it is checked separately below. `bool` is
# excluded from the int fields on purpose: `isinstance(True, int)` is True, and an `index`
# of `true` renders a locator of `§True-notes` — plausible, wrong and silent
_PAYLOAD_FIELD_TYPES = {
    "anchor": (str,),
    "heading": (str,),
    "body": (str,),
    "size_bytes": (int,),
    "level": (int,),
    "index": (int,),
    "literal_tf": (dict,),
    "derived_tf": (dict,),
}


_NULLABLE_PAYLOAD_FIELDS = ("canonical",)


def _section_to_payload(section: Section, literal: Counter[str], derived: Counter[str]) -> dict:
    return {
        "anchor": section.anchor,
        "heading": section.heading,
        "body": section.body,
        "size_bytes": section.size_bytes,
        "level": section.level,
        "canonical": section.canonical,
        "index": section.index,
        "literal_tf": dict(literal),
        "derived_tf": dict(derived),
    }


def _token_counts(table: dict, name: str) -> Counter[str]:
    """A frequency table from a payload, rejected unless every value really is a token count."""
    # `dict` in _PAYLOAD_FIELD_TYPES is only half the check — see contracts/scoring.md
    for count in table.values():
        if type(count) is not int or count < 0:
            # TypeError even for the range violation: `_entries_from_cache` catches only
            # (KeyError, TypeError), and anything else escapes and crashes the search
            raise TypeError(f"{name} holds {count!r}, not a token count")
    return Counter(table)


def _section_entry_from_payload(doc_id: str, data: dict) -> SectionEntry:
    canonical = data["canonical"]
    if canonical is not None and not isinstance(canonical, str):
        raise TypeError("canonical is neither a string nor null")
    # a value outside CANONICAL_ROLES is what a role added without a cache format bump
    # writes; role_coverage's `counts = {role: 0 for role in CANONICAL_ROLES}` has no slot
    # for it, so believing it here is the KeyError this whole block exists to prevent
    if canonical is not None and canonical not in CANONICAL_ROLES:
        raise TypeError(f"canonical is {canonical!r}, not a recognised role")
    section = Section(
        anchor=data["anchor"],
        heading=data["heading"],
        body=data["body"],
        size_bytes=data["size_bytes"],
        level=data["level"],
        canonical=data["canonical"],
        index=data["index"],
    )
    # every field, by type. `Counter` accepts any iterable, so a `literal_tf` that arrived as
    # a list would count its elements and rank on a plausible index of all-ones; a non-string
    # `body` reaches `output._section_snippet` and crashes the search there. Both are shapes
    # that would otherwise pass, which is what "a mismatch is a miss, never a crash" claims
    for name, expected in _PAYLOAD_FIELD_TYPES.items():
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, expected):
            raise TypeError(f"{name} is {type(value).__name__}, not {expected[0].__name__}")
    literal_tf = _token_counts(data["literal_tf"], "literal_tf")
    derived_tf = _token_counts(data["derived_tf"], "derived_tf")
    return SectionEntry(
        doc_id=doc_id,
        section=section,
        literal_tf=literal_tf,
        derived_tf=derived_tf,
        length=sum(literal_tf.values()),
    )


def _entries_from_cache(doc: Doc, cached_payload: object) -> list[SectionEntry] | None:
    # a shape mismatch (payload predates a format bump) is a miss, not a crash
    try:
        return [
            _section_entry_from_payload(doc.id, s) for s in cached_payload["sections"]
        ]
    except (KeyError, TypeError) as exc:
        cache._warn(
            f"cache payload for {doc.path.name} has an unexpected shape, "
            f"recomputing ({exc})"
        )
        return None


# the cache's own rule, kept in memory for a process that searches more than once
_KEPT_ENTRIES: cache.KeptByPath[list[SectionEntry]] = cache.KeptByPath()


def entries_for_doc(doc: Doc) -> list[SectionEntry]:
    """Section-level token index for one document, from cache when its body is unchanged."""
    # the hash of the very text the payload is built from below, so an entry can only ever be
    # served for that text — contracts/cache.md, "Keyed by content"
    key = cache.content_key(doc.body)
    return _KEPT_ENTRIES.get(doc.path, (doc.id, key), lambda: _built_entries(doc, key))


def _built_entries(doc: Doc, key: str) -> list[SectionEntry]:
    cached_payload = cache.load(doc.path, key)
    if cached_payload is not None:
        entries = _entries_from_cache(doc, cached_payload)
        if entries is not None:
            return entries

    entries = []
    payload_sections = []
    for section in split_sections(doc.body):
        literal, derived = _tokenize_counts(f"{section.heading}\n{section.body}")
        entries.append(
            SectionEntry(
                doc_id=doc.id,
                section=section,
                literal_tf=literal,
                derived_tf=derived,
                length=sum(literal.values()),
            )
        )
        payload_sections.append(_section_to_payload(section, literal, derived))

    cache.store(doc.path, key, {"sections": payload_sections})
    return entries


def build_section_index(docs: list[Doc], store_docs: list[Doc]) -> list[SectionEntry]:
    """Section entries for `docs`, pruning the cache against `store_docs` — the whole store,
    which is the only set entitled to say an entry is an orphan."""
    store_paths = [doc.path for doc in store_docs]
    cache.prune_orphans(store_paths)
    _KEPT_ENTRIES.retain(store_paths)
    entries: list[SectionEntry] = []
    for doc in docs:
        entries.extend(entries_for_doc(doc))
    return entries
