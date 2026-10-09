"""Deterministic search ranking: spine-field matching plus BM25 over body sections."""

from __future__ import annotations

import functools
import math
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from engmem import cache
from engmem.sections import CANONICAL_ROLES, Section, role_is_inherited, split_sections
from engmem.spine import Doc, linked_repos

FIELD_WEIGHTS = {"id": 5, "entities": 3, "title": 2, "tags": 1}

# statuses that never appear as a primary result (data-model.md "State transitions")
_NEVER_PRIMARY = frozenset({"draft", "superseded"})

# see contracts/scoring.md — a lone slug word is as informative as a title word
ID_SLUG_WEIGHT = FIELD_WEIGHTS["title"]

# BM25 over section bodies; k1/b tuned for this corpus's section-length
# distribution — see contracts/scoring.md
BM25_K1 = 1.2
BM25_B = 0.6
# a term in over half the corpus's sections is dropped from body scoring
# entirely — self-tuning stopword substitute; see contracts/scoring.md
DF_CEILING_RATIO = 0.5
# a hit matching fewer distinct query words than this, and no identifier, is a weak candidate;
# a shorter query needs all its words. A count, not a calibrated threshold — contracts/scoring.md
MIN_COVERED_WORDS = 2
# a matched word this short (characters, after normalisation) counts toward MIN_COVERED_WORDS only
# as an identifier: `for`, `и`, `και`, `der` are what two-word coverage was cleared with (US-14)
SHORT_WORD_MAX_CHARS = 3

# Runs over a per-character class string, not the token itself: "A" upper/title-case letter,
# "a" any other letter, "0" number, "m" combining mark. Same shape as the ASCII-era
# `[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+`, with a mark riding on its base character.
_CAMEL_CLASS_RE = re.compile(r"(?:Am*)+(?=Am*a)|(?:Am*)?(?:am*)+|(?:Am*)+|(?:0m*)+")


# planes holding category M today; `test_the_mark_scan_covers_every_plane_with_marks` fails
# when a Unicode version adds a mark elsewhere. Scanning 3 planes, not 17, is ~6x cheaper.
_MARK_PLANES = (0, 1, 14)

# modifier letters (category Lm, so inside `\w`) used as apostrophes: Ukrainian `обʼєкт` is
# written with U+02BC as often as with `'` or `’`, and all forms must split alike
_APOSTROPHE_LETTERS = "\u02bc\u02bb\u02bd\u02ee"


def _mark_ranges(code_points: Iterable[int]) -> list[tuple[int, int]]:
    """Inclusive code-point ranges of the combining marks (category M) among `code_points`."""
    ranges: list[list[int]] = []
    for cp in code_points:
        if not unicodedata.category(chr(cp)).startswith("M"):
            continue
        if ranges and ranges[-1][1] == cp - 1:
            ranges[-1][1] = cp
        else:
            ranges.append([cp, cp])
    return [(lo, hi) for lo, hi in ranges]


@functools.cache
def _token_run_re() -> re.Pattern[str]:
    """Letters, numbers and combining marks; built once, from this Python's Unicode database."""
    # `\w` covers letters and numbers in every plane but not marks, and splitting on a mark cuts
    # a word that has no precomposed form in two; `_` is excluded so snake_case still splits
    planes = (range(plane << 16, (plane + 1) << 16) for plane in _MARK_PLANES)
    ranges = _mark_ranges(cp for plane in planes for cp in plane)
    # as ranges, not 2,000-odd single characters: the regex engine scans a range set 4x faster
    marks = "".join(f"{re.escape(chr(lo))}-{re.escape(chr(hi))}" for lo, hi in ranges)
    return re.compile(rf"(?:[^\W_{_APOSTROPHE_LETTERS}]|[{marks}])+")


def normalize_token(token: str) -> str:
    return unicodedata.normalize("NFKC", token).casefold()


def tokenize_raw(text: str) -> list[str]:
    # NFKC before splitting: it composes `e` + U+0301 into `é` and folds fullwidth forms and
    # ligatures, so the split sees one spelling of each word
    normalized = unicodedata.normalize("NFKC", text)
    return _token_run_re().findall(normalized)


def _char_class(char: str) -> str:
    category = unicodedata.category(char)
    if category in ("Lu", "Lt"):
        return "A"
    if category[0] == "L":
        return "a"
    if category[0] == "N":
        return "0"
    return "m"


def camel_fragments(raw_token: str) -> list[str] | None:
    # fast path for the common prose word: every character would classify as "a", one part
    if raw_token.isalpha() and raw_token.islower():
        return None
    classes = "".join(_char_class(c) for c in raw_token)
    parts = [raw_token[m.start():m.end()] for m in _CAMEL_CLASS_RE.finditer(classes)]
    if len(parts) <= 1:
        return None
    return parts


def _camel_expansion(raw_token: str) -> tuple[list[str], str] | None:
    # normalized fragments + initial-letter acronym, e.g. "WidgetCache" ->
    # (["widget", "cache"], "wc"); one shared rule so field/body/query agree
    frac = camel_fragments(raw_token)
    if not frac:
        return None
    parts_norm = [normalize_token(p) for p in frac]
    acronym = "".join(p[0] for p in parts_norm if p)
    return parts_norm, acronym


def _is_restricted(token: str) -> bool:
    # too short/generic to trust past an exact, unsplit match (§7); shared by
    # the spine and body indexes so restricted-ness never disagrees between them
    return len(token) <= 3 or token.isdigit()


def _field_token_sets(value) -> tuple[set[str], set[str]]:
    """`(original_tokens, all_tokens_including_derived_fragments)`."""
    originals: set[str] = set()
    all_tokens: set[str] = set()
    items = value if isinstance(value, list) else [value]

    for item in items:
        for raw in tokenize_raw(str(item)):
            orig = normalize_token(raw)
            originals.add(orig)
            all_tokens.add(orig)
            expansion = _camel_expansion(raw)
            if expansion:
                parts_norm, acronym = expansion
                all_tokens.update(parts_norm)
                if acronym:
                    all_tokens.add(acronym)

    return originals, all_tokens


def _doc_field_index(doc: Doc) -> dict[str, tuple[set[str], set[str]]]:
    return {
        "id": _field_token_sets(doc.id),
        "entities": _field_token_sets(doc.entities),
        "title": _field_token_sets(doc.title),
        "tags": _field_token_sets(doc.tags),
    }


@dataclass
class _QueryToken:
    text: str
    restricted: bool  # True: may only match ORIGINAL (unsplit) field/section tokens


@dataclass(frozen=True)
class _QueryWord:
    """One word as typed, with the tokens it searches for: itself, then any CamelCase parts."""

    original: str
    tokens: tuple[str, ...]
    camel: bool


def _build_query_words(query: str) -> list[_QueryWord]:
    """Distinct query words in first-seen order, the same tokens `_build_query_tokens` searches
    for grouped by the word that produced them (contracts/scoring.md, "Weak candidates")."""
    tokens: dict[str, list[str]] = {}
    camel: set[str] = set()
    for raw in tokenize_raw(query):
        orig = normalize_token(raw)
        if not orig:
            continue
        word_tokens = tokens.setdefault(orig, [orig])
        expansion = None if _is_restricted(orig) else _camel_expansion(raw)
        if expansion is None:
            continue
        # `responsecache` then `ResponseCache` is one word; the second spelling adds its parts
        camel.add(orig)
        parts_norm, acronym = expansion
        word_tokens += [t for t in (*parts_norm, acronym) if t and t not in word_tokens]
    return [_QueryWord(orig, tuple(ts), orig in camel) for orig, ts in tokens.items()]


def _build_query_tokens(query: str) -> list[_QueryToken]:
    """Distinct query tokens in first-seen order, each carrying its own restricted-ness."""
    # see contracts/scoring.md, "One rule per query token"
    tokens: dict[str, _QueryToken] = {}

    def add(text: str) -> None:
        if text and text not in tokens:
            tokens[text] = _QueryToken(text, _is_restricted(text))

    for raw in tokenize_raw(query):
        orig = normalize_token(raw)
        add(orig)
        if _is_restricted(orig):
            continue

        expansion = _camel_expansion(raw)
        if expansion:
            parts_norm, acronym = expansion
            for p in parts_norm:
                add(p)
            add(acronym)
    return list(tokens.values())


@dataclass(frozen=True)
class Strength:
    """Whether a hit is only a weak candidate, and the words that say why (contracts/scoring.md,
    "Weak candidates"). A verdict on the match, never a probability of usefulness."""

    weak: bool
    covered: tuple[str, ...]  # the query words the hit matched, in query order
    n_words: int  # distinct words in the query
    # the covered words that count toward MIN_COVERED_WORDS; a short word only as an identifier
    counted: tuple[str, ...] = ()


@dataclass
class SectionHit:
    section: Section
    score: float
    matched_tokens: list[str]


@dataclass
class Hit:
    doc: Doc
    score: float
    matched_fields: dict[str, list[str]] = field(default_factory=dict)
    ambiguous: bool = False
    # every matched section, best first; matched_fields only names the winner
    # (see _search_core) — this is the fuller list the renderer walks
    section_hits: list[SectionHit] = field(default_factory=list)
    strength: Strength = field(default_factory=lambda: Strength(False, (), 0))

    @property
    def weak(self) -> bool:
        return self.strength.weak


@dataclass
class SupersededNote:
    doc: Doc
    successor: Doc | None
    score: float
    strength: Strength = field(default_factory=lambda: Strength(False, (), 0))

    @property
    def weak(self) -> bool:
        return self.strength.weak


@dataclass
class SearchOutcome:
    hits: list[Hit]
    ambiguous: bool
    superseded_notes: list[SupersededNote]


def _id_field_weight(
    qt: _QueryToken, id_originals: set[str], id_all: set[str], whole_id_matched: bool
) -> int | None:
    candidates = id_originals if qt.restricted else id_all
    if qt.text not in candidates:
        return None
    # a numeric ticket component is unambiguous alone; a slug word only earns
    # full id weight when the query named the whole id
    if qt.text.isdigit() or whole_id_matched:
        return FIELD_WEIGHTS["id"]
    return ID_SLUG_WEIGHT


def _entity_token_sets(doc: Doc) -> list[set[str]]:
    """Each entity's own original tokens: `brand policy` is one entity of two words."""
    items = doc.entities if isinstance(doc.entities, list) else [doc.entities]
    return [{normalize_token(raw) for raw in tokenize_raw(str(item))} for item in items]


def _names_identifier(
    word: _QueryWord, doc: Doc, entities: list[set[str]], matched: set[str]
) -> bool:
    """The word is the record's whole id or ticket number, an entity matched in full, or a
    CamelCase name it carries whole — never a slug word, nor part of a longer name."""
    id_originals = _doc_field_index(doc)["id"][0]
    if word.original in id_originals and (
        word.original.isdigit() or id_originals.issubset(matched)
    ):
        return True
    if any(word.original in tokens and tokens <= matched for tokens in entities):
        return True
    return word.camel and word.original in matched


def _is_short(word: _QueryWord) -> bool:
    return len(word.original) <= SHORT_WORD_MAX_CHARS


def _strength(words: list[_QueryWord], doc: Doc, matched: set[str]) -> Strength:
    covered = tuple(w.original for w in words if not matched.isdisjoint(w.tokens))
    entities = _entity_token_sets(doc)
    if any(_names_identifier(w, doc, entities, matched) for w in words if w.original in covered):
        return Strength(False, covered, len(words), covered)
    counted = tuple(w.original for w in words if w.original in covered and not _is_short(w))
    # a query of short words alone still needs one counted word, so it never passes on none
    countable = sum(1 for w in words if not _is_short(w))
    needed = max(1, min(MIN_COVERED_WORDS, countable))
    return Strength(len(counted) < needed, covered, len(words), counted)


def _spine_score(query_tokens: list[_QueryToken], doc: Doc) -> tuple[float, dict[str, list[str]]]:
    """Scores `doc` against `query_tokens` on the four spine fields only."""
    if not query_tokens:
        return 0.0, {}

    field_index = _doc_field_index(doc)
    id_originals, id_all = field_index["id"]
    query_texts = {qt.text for qt in query_tokens}
    whole_id_matched = bool(id_originals) and id_originals.issubset(query_texts)

    matched_count = 0
    total_weight = 0.0
    matched_fields: dict[str, list[str]] = {}

    for qt in query_tokens:
        best_weight = 0
        best_field = None
        for field_name, weight in FIELD_WEIGHTS.items():
            if field_name == "id":
                w = _id_field_weight(qt, id_originals, id_all, whole_id_matched)
                if w is None:
                    continue
            else:
                originals, all_tokens = field_index[field_name]
                candidates = originals if qt.restricted else all_tokens
                if qt.text not in candidates:
                    continue
                w = weight
            if w > best_weight:
                best_weight = w
                best_field = field_name

        if best_field is not None:
            matched_count += 1
            total_weight += best_weight
            matched_fields.setdefault(best_field, [])
            if qt.text not in matched_fields[best_field]:
                matched_fields[best_field].append(qt.text)

    if matched_count == 0:
        return 0.0, {}

    score = total_weight * (matched_count / len(query_tokens))
    return score, matched_fields


def _tokenize_counts(text: str) -> tuple[Counter[str], Counter[str]]:
    """Literal and derived token-frequency counters for a chunk of body text."""
    literal: Counter[str] = Counter()
    derived: Counter[str] = Counter()
    for raw in tokenize_raw(text):
        orig = normalize_token(raw)
        literal[orig] += 1
        derived[orig] += 1
        expansion = _camel_expansion(raw)
        if expansion:
            parts_norm, acronym = expansion
            for p in parts_norm:
                derived[p] += 1
            if acronym:
                derived[acronym] += 1
    return literal, derived


@dataclass
class _SectionEntry:
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


def _section_entry_from_payload(doc_id: str, data: dict) -> _SectionEntry:
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
    return _SectionEntry(
        doc_id=doc_id,
        section=section,
        literal_tf=literal_tf,
        derived_tf=derived_tf,
        length=sum(literal_tf.values()),
    )


def _entries_from_cache(doc: Doc, cached_payload: object) -> list[_SectionEntry] | None:
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


def _entries_for_doc(doc: Doc) -> list[_SectionEntry]:
    """Section-level token index for one document, from cache when unchanged."""
    # the identity of the read `doc.body` came from, not of the file as it is now: stat'ing
    # here would key an entry built from the old body to the edited file, and that entry then
    # never invalidates — the search keeps returning the pre-edit document until it changes again
    identity = doc.source_identity
    cached_payload = cache.load(doc.path, identity)
    if cached_payload is not None:
        entries = _entries_from_cache(doc, cached_payload)
        if entries is not None:
            return entries

    entries = []
    payload_sections = []
    for section in split_sections(doc.body):
        literal, derived = _tokenize_counts(f"{section.heading}\n{section.body}")
        entries.append(
            _SectionEntry(
                doc_id=doc.id,
                section=section,
                literal_tf=literal,
                derived_tf=derived,
                length=sum(literal.values()),
            )
        )
        payload_sections.append(_section_to_payload(section, literal, derived))

    cache.store(doc.path, identity, {"sections": payload_sections})
    return entries


def _build_section_index(docs: list[Doc], store_docs: list[Doc]) -> list[_SectionEntry]:
    """Section entries for `docs`, pruning the cache against `store_docs` — the whole store,
    which is the only set entitled to say an entry is an orphan."""
    cache.prune_orphans([doc.path for doc in store_docs])
    entries: list[_SectionEntry] = []
    for doc in docs:
        entries.extend(_entries_for_doc(doc))
    return entries


def _document_frequencies(entries: list[_SectionEntry]) -> tuple[Counter[str], Counter[str]]:
    df_literal: Counter[str] = Counter()
    df_derived: Counter[str] = Counter()
    for entry in entries:
        df_literal.update(entry.literal_tf.keys())
        df_derived.update(entry.derived_tf.keys())
    return df_literal, df_derived


def _bm25_entry_score(
    query_tokens: list[_QueryToken],
    entry: _SectionEntry,
    df_literal: Counter[str],
    df_derived: Counter[str],
    n: int,
    avgdl: float,
) -> tuple[float, list[str]]:
    score = 0.0
    matched: list[str] = []
    for qt in query_tokens:
        # restricted tokens may only match literal text, never a CamelCase
        # fragment, or e.g. "MQ" would match any word starting with those letters
        tf_map = entry.literal_tf if qt.restricted else entry.derived_tf
        tf = tf_map.get(qt.text, 0)
        if tf == 0:
            continue
        df_map = df_literal if qt.restricted else df_derived
        # df >= 1 whenever tf > 0: this entry contributed the key to the counter
        df = df_map[qt.text]
        if df / n > DF_CEILING_RATIO:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * entry.length / avgdl)
        score += idf * tf * (BM25_K1 + 1) / denom
        matched.append(qt.text)
    return score, matched


def _body_scores_from_entries(
    entries: list[_SectionEntry], query_tokens: list[_QueryToken]
) -> dict[str, tuple[float, list[SectionHit]]]:
    """A document scores its single best section, not the sum — else it wins by section count."""
    if not query_tokens:
        return {}

    n = len(entries)
    if n == 0:
        return {}
    avgdl = sum(e.length for e in entries) / n
    if avgdl == 0:
        return {}
    df_literal, df_derived = _document_frequencies(entries)

    by_doc: dict[str, list[SectionHit]] = {}
    for entry in entries:
        score, matched = _bm25_entry_score(query_tokens, entry, df_literal, df_derived, n, avgdl)
        if score <= 0:
            continue
        by_doc.setdefault(entry.doc_id, []).append(
            SectionHit(section=entry.section, score=score, matched_tokens=matched)
        )

    result: dict[str, tuple[float, list[SectionHit]]] = {}
    for doc_id, hits in by_doc.items():
        hits.sort(key=lambda h: -h.score)
        result[doc_id] = (hits[0].score, hits)
    return result


def _cluster_key_for_short_token(token: str, doc: Doc) -> str | None:
    """The longer entity whose CamelCase acronym equals `token`, as this doc's cluster key."""
    for entity in doc.entities:
        for raw in tokenize_raw(entity):
            expansion = _camel_expansion(raw)
            if expansion is None:
                continue
            _, acronym = expansion
            if acronym == token:
                return normalize_token(raw)
    return None


@dataclass(frozen=True)
class Scope:
    """Chosen repositories, the records linked to none, or every record with its links shown
    (US-09, US-10); exactly one of the three."""

    repos: tuple[str, ...] = ()
    unscoped: bool = False
    all_repos: bool = False
    # derived from `repos`, so it stays out of `==`, the hash and the repr (contracts/scoring.md)
    repo_keys: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.repos, str):
            raise TypeError("repos is a tuple of repository names, not one name")
        names = tuple(name.strip() for name in self.repos)
        if not all(names):
            raise ValueError("a repository scope needs a name")
        if (bool(names), self.unscoped, self.all_repos).count(True) != 1:
            raise ValueError(
                "a scope is repositories, the unscoped records or all repositories, exactly one"
            )
        object.__setattr__(self, "repos", _distinct_repos(names))
        object.__setattr__(self, "repo_keys", frozenset(repo_key(name) for name in self.repos))


def repo_key(name: str) -> str:
    """What two repository names are compared by: case and surrounding space do not count."""
    return name.strip().casefold()


def _distinct_repos(names: tuple[str, ...]) -> tuple[str, ...]:
    """One name per `repo_key`, in the order given, spelled as first given."""
    by_key: dict[str, str] = {}
    for name in names:
        by_key.setdefault(repo_key(name), name)
    return tuple(by_key.values())


def in_scope(doc: Doc, scope: Scope) -> bool:
    """Only the explicit `repos` link decides; title and tags never stand in for it."""
    if scope.all_repos:
        return True
    keys = {repo_key(name) for name in linked_repos(doc)}
    if scope.unscoped:
        return not keys
    return not keys.isdisjoint(scope.repo_keys)


def ranking_corpus(docs: list[Doc], scope: Scope | None = None) -> list[Doc]:
    """The documents a search ranks over: no draft, and only the scope's records when one is
    chosen (contracts/scoring.md)."""
    return [
        doc for doc in docs
        if doc.status != "draft" and (scope is None or in_scope(doc, scope))
    ]


def search(docs: list[Doc], query: str, scope: Scope | None = None) -> SearchOutcome:
    corpus = ranking_corpus(docs, scope)
    entries = _build_section_index(corpus, docs)
    return _search_core(corpus, docs, query, entries)


def _search_core(
    corpus: list[Doc],
    store_docs: list[Doc],
    query: str,
    entries: list[_SectionEntry],
) -> SearchOutcome:
    """Ranks `corpus`; `store_docs` only resolves a superseded document's successor."""
    query_tokens = _build_query_tokens(query)
    query_words = _build_query_words(query)
    body_scores = _body_scores_from_entries(entries, query_tokens)

    scored: list[Hit] = []
    for doc in corpus:
        spine_value, matched_fields = _spine_score(query_tokens, doc)
        body_value, section_hits = body_scores.get(doc.id, (0.0, []))

        if not matched_fields and body_value <= 0:
            continue

        matched = {t for tokens in matched_fields.values() for t in tokens}
        matched.update(t for section_hit in section_hits for t in section_hit.matched_tokens)
        if section_hits:
            # only the winning section is named on the explainability line;
            # section_hits itself still carries the full list to the renderer
            winner = section_hits[0]
            matched_fields = dict(matched_fields)
            matched_fields[winner.section.locator] = list(winner.matched_tokens)

        scored.append(
            Hit(
                doc=doc,
                score=spine_value + body_value,
                matched_fields=matched_fields,
                section_hits=section_hits,
                strength=_strength(query_words, doc, matched),
            )
        )

    # reliable finds first, then -score, -date, id: a full deterministic order. BM25 scores
    # shift with corpus size, so golden tests pin order/membership, never an absolute score.
    scored.sort(key=lambda h: (h.weak, -h.score, -_date_ordinal(h.doc), h.doc.id))

    # a single restricted token whose entity match clusters into 2+ differing
    # full forms is ambiguous; keep one representative per cluster (§7)
    restricted_texts = {qt.text for qt in query_tokens if qt.restricted}
    ambiguous = False
    if len(query_tokens) == 1 and restricted_texts:
        short_token = next(iter(restricted_texts))
        entity_matches = [
            h
            for h in scored
            if short_token in _doc_field_index(h.doc)["entities"][0]
        ]
        clusters: dict[str, Hit] = {}
        clustered_ids: set[str] = set()
        for h in entity_matches:
            key = _cluster_key_for_short_token(short_token, h.doc)
            if key is None:
                continue
            # every cluster member is collapsed, whatever its status — but only a document
            # that can be output may represent one. The status partition runs below, so a
            # superseded representative collapses its cluster-mates and is then dropped
            # itself, taking an active document out of the results with it
            clustered_ids.add(h.doc.id)
            if h.doc.status == "superseded":
                continue
            if key not in clusters or h.score > clusters[key].score:
                clusters[key] = h
        if len(clusters) > 1:
            ambiguous = True
            rep_ids = {h.doc.id for h in clusters.values()}
            # collapse each cluster to its representative; any hit that
            # matched some other way stays in the ranking
            scored = [
                h for h in scored if h.doc.id not in clustered_ids or h.doc.id in rep_ids
            ]
            for h in scored:
                h.ambiguous = h.doc.id in rep_ids

    docs_by_id = {d.id: d for d in store_docs}
    hits: list[Hit] = []
    superseded_notes: list[SupersededNote] = []

    for h in scored:
        if h.doc.status == "superseded":
            successor = docs_by_id.get(h.doc.superseded_by) if h.doc.superseded_by else None
            superseded_notes.append(
                SupersededNote(doc=h.doc, successor=successor, score=h.score, strength=h.strength)
            )
            continue
        hits.append(h)

    return SearchOutcome(hits=hits, ambiguous=ambiguous, superseded_notes=superseded_notes)


def _role_index_from_entries(entries: list[_SectionEntry]) -> dict[str, dict[str, Section]]:
    """`doc_id -> {canonical role: Section}`, from the section index a search already parsed."""
    index: dict[str, dict[str, Section]] = {}
    # two passes: a role named by a section's own heading wins over one inherited from an
    # oversized parent, wherever each sits in the document
    for inherited_ok in (False, True):
        for entry in entries:
            section = entry.section
            if section.canonical is None:
                continue
            if not inherited_ok and role_is_inherited(section):
                continue
            # a doc is not expected to carry the same role twice; ties keep the
            # first in document order rather than being silently overwritten
            index.setdefault(entry.doc_id, {}).setdefault(section.canonical, section)
    return index


def search_with_role_sections(
    docs: list[Doc], query: str, scope: Scope | None = None
) -> tuple[SearchOutcome, dict[str, dict[str, Section]]]:
    """`search()` plus a `doc_id -> {role: Section}` map built from the same parse."""
    corpus = ranking_corpus(docs, scope)
    entries = _build_section_index(corpus, docs)
    outcome = _search_core(corpus, docs, query, entries)
    role_map = _role_index_from_entries(entries)
    return outcome, role_map


def role_coverage(docs: list[Doc]) -> dict[str, int]:
    """role -> searchable documents carrying it, with 0 present so "zero" and "never checked"
    differ."""
    searchable = [d for d in docs if d.status not in _NEVER_PRIMARY]
    entries = _build_section_index(searchable, docs)
    role_map = _role_index_from_entries(entries)
    counts = {role: 0 for role in CANONICAL_ROLES}
    for roles in role_map.values():
        for role in roles:
            counts[role] += 1
    return counts


def _date_ordinal(doc: Doc) -> int:
    return doc.date.toordinal()
