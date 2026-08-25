"""Deterministic search ranking: spine-field matching (`ENGMEM-SPEC.md` §7)
plus BM25 over body sections and role-addressed retrieval. Tuning rationale:
`docs/design/contracts/scoring.md`.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from engmem import cache
from engmem.sections import CANONICAL_ROLES, Section, split_sections
from engmem.spine import Doc

FIELD_WEIGHTS = {"id": 5, "entities": 3, "title": 2, "tags": 1}

# see contracts/scoring.md — a lone slug word is as informative as a title word
ID_SLUG_WEIGHT = FIELD_WEIGHTS["title"]

# BM25 over section bodies; k1/b tuned for this corpus's section-length
# distribution — see contracts/scoring.md
BM25_K1 = 1.2
BM25_B = 0.6
# a term in over half the corpus's sections is dropped from body scoring
# entirely — self-tuning stopword substitute; see contracts/scoring.md
DF_CEILING_RATIO = 0.5

_RAW_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")
_CAMEL_SPLIT_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def normalize_token(token: str) -> str:
    return unicodedata.normalize("NFKC", token).casefold()


def tokenize_raw(text: str) -> list[str]:
    # NFKC before splitting: an ASCII-only split would drop fullwidth forms
    # and mangle ligatures. Casefold stays per-token so CamelCase splitting
    # still sees the original case.
    normalized = unicodedata.normalize("NFKC", text)
    return [t for t in _RAW_SPLIT_RE.split(normalized) if t]


def camel_fragments(raw_token: str) -> list[str] | None:
    parts = _CAMEL_SPLIT_RE.findall(raw_token)
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
    """(original_tokens, all_tokens_including_derived_fragments)."""
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


def _build_query_tokens(query: str) -> list[_QueryToken]:
    tokens: list[_QueryToken] = []
    for raw in tokenize_raw(query):
        orig = normalize_token(raw)
        restricted = _is_restricted(orig)
        if restricted:
            tokens.append(_QueryToken(orig, True))
            continue

        tokens.append(_QueryToken(orig, False))
        expansion = _camel_expansion(raw)
        if expansion:
            parts_norm, acronym = expansion
            for p in parts_norm:
                tokens.append(_QueryToken(p, False))
            if acronym:
                tokens.append(_QueryToken(acronym, False))
    return tokens


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


@dataclass
class SupersededNote:
    doc: Doc
    successor: Doc | None
    score: float


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


def _spine_score(query_tokens: list[_QueryToken], doc: Doc) -> tuple[float, dict[str, list[str]]]:
    """Scores `doc` against `query_tokens` on the four spine fields only —
    body scoring is separate (`_body_scores_from_entries`), added by the
    caller. An unmatched document is (0.0, {}), never None."""
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
    """Two token-frequency counters for a chunk of body text: literal (as
    written) and derived (literal plus CamelCase fragments/acronyms), needed
    for BM25 term frequency rather than mere membership."""
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


def _section_entry_from_payload(doc_id: str, data: dict) -> _SectionEntry:
    section = Section(
        anchor=data["anchor"],
        heading=data["heading"],
        body=data["body"],
        size_bytes=data["size_bytes"],
        level=data["level"],
        canonical=data["canonical"],
        index=data["index"],
    )
    literal_tf = Counter(data["literal_tf"])
    derived_tf = Counter(data["derived_tf"])
    return _SectionEntry(
        doc_id=doc_id,
        section=section,
        literal_tf=literal_tf,
        derived_tf=derived_tf,
        length=sum(literal_tf.values()),
    )


def _entries_from_cache(doc: Doc, cached_payload: dict) -> list[_SectionEntry] | None:
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
    """Section-level token index for one document, from the on-disk cache
    when unchanged since last cached, else computed fresh and re-cached."""
    identity = cache.identity_for(doc.path)
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


def _build_section_index(docs: list[Doc]) -> list[_SectionEntry]:
    cache.prune_orphans([doc.path for doc in docs])
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
    query_texts: list[str],
    entry: _SectionEntry,
    df_literal: Counter[str],
    df_derived: Counter[str],
    n: int,
    avgdl: float,
) -> tuple[float, list[str]]:
    score = 0.0
    matched: list[str] = []
    for text in query_texts:
        restricted = _is_restricted(text)
        # restricted tokens may only match literal text, never a CamelCase
        # fragment, or e.g. "MQ" would match any word starting with those letters
        tf_map = entry.literal_tf if restricted else entry.derived_tf
        tf = tf_map.get(text, 0)
        if tf == 0:
            continue
        df_map = df_literal if restricted else df_derived
        df = df_map.get(text, 0)
        if df == 0 or df / n > DF_CEILING_RATIO:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * entry.length / avgdl)
        score += idf * tf * (BM25_K1 + 1) / denom
        matched.append(text)
    return score, matched


def _body_scores_from_entries(
    entries: list[_SectionEntry], query_tokens: list[_QueryToken]
) -> dict[str, tuple[float, list[SectionHit]]]:
    """Per document, (best-section BM25 score, every matched section
    best-first). A document's body score is its single best section (`max`,
    not `sum`) — stops a document winning purely by section count."""
    if not query_tokens:
        return {}

    n = len(entries)
    if n == 0:
        return {}
    avgdl = sum(e.length for e in entries) / n
    if avgdl == 0:
        return {}
    df_literal, df_derived = _document_frequencies(entries)

    query_texts: list[str] = []
    seen: set[str] = set()
    for qt in query_tokens:
        if qt.text not in seen:
            seen.add(qt.text)
            query_texts.append(qt.text)

    by_doc: dict[str, list[SectionHit]] = {}
    for entry in entries:
        score, matched = _bm25_entry_score(query_texts, entry, df_literal, df_derived, n, avgdl)
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
    """The longer entity (if any) whose CamelCase acronym equals `token`,
    normalized, as this doc's ambiguity-cluster key — see contracts/scoring.md."""
    for entity in doc.entities:
        for raw in tokenize_raw(entity):
            expansion = _camel_expansion(raw)
            if expansion is None:
                continue
            _, acronym = expansion
            if acronym == token:
                return normalize_token(raw)
    return None


def search(docs: list[Doc], query: str) -> SearchOutcome:
    query_tokens = _build_query_tokens(query)
    entries = _build_section_index(docs)
    return _search_core(docs, query_tokens, entries)


def _search_core(
    docs: list[Doc], query_tokens: list[_QueryToken], entries: list[_SectionEntry]
) -> SearchOutcome:
    """`search()`'s ranking, factored out so `search_with_role_sections` can
    share one section-index build instead of parsing the corpus twice."""
    body_scores = _body_scores_from_entries(entries, query_tokens)

    scored: list[Hit] = []
    for doc in docs:
        spine_value, matched_fields = _spine_score(query_tokens, doc)
        body_value, section_hits = body_scores.get(doc.id, (0.0, []))

        if not matched_fields and body_value <= 0:
            continue

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
            )
        )

    # -score, -date, id: a full deterministic order. BM25 scores shift with
    # corpus size, so golden tests pin order/membership, never an absolute score.
    scored.sort(key=lambda h: (-h.score, -_date_ordinal(h.doc), h.doc.id))

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
            clustered_ids.add(h.doc.id)
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

    active_by_id = {d.id: d for d in docs}
    hits: list[Hit] = []
    superseded_notes: list[SupersededNote] = []

    for h in scored:
        if h.doc.status == "draft":
            continue
        if h.doc.status == "superseded":
            successor = active_by_id.get(h.doc.superseded_by) if h.doc.superseded_by else None
            superseded_notes.append(
                SupersededNote(doc=h.doc, successor=successor, score=h.score)
            )
            continue
        hits.append(h)

    return SearchOutcome(hits=hits, ambiguous=ambiguous, superseded_notes=superseded_notes)


def _role_index_from_entries(entries: list[_SectionEntry]) -> dict[str, dict[str, Section]]:
    """doc_id -> {canonical role: Section}, from the section index a search
    already parsed — no second `split_sections` pass, no second cache read."""
    index: dict[str, dict[str, Section]] = {}
    for entry in entries:
        canonical = entry.section.canonical
        if canonical is None:
            continue
        # a doc is not expected to carry the same role twice; ties keep the
        # first in document order rather than being silently overwritten
        index.setdefault(entry.doc_id, {}).setdefault(canonical, entry.section)
    return index


def search_with_role_sections(
    docs: list[Doc], query: str
) -> tuple[SearchOutcome, dict[str, dict[str, Section]]]:
    """Same ranked `SearchOutcome` as `search()`, plus a `doc_id -> {role:
    Section}` map built from the same section-index parse — no second corpus
    scan (`ENGMEM-SPEC.md` §5, role-addressed retrieval)."""
    query_tokens = _build_query_tokens(query)
    entries = _build_section_index(docs)
    outcome = _search_core(docs, query_tokens, entries)
    role_map = _role_index_from_entries(entries)
    return outcome, role_map


def role_coverage(docs: list[Doc]) -> dict[str, int]:
    """role -> number of searchable (active, non-draft, non-superseded)
    documents carrying it. Every `CANONICAL_ROLES` name is present with 0 for
    an unrepresented role, so "zero" and "never checked" stay distinguishable."""
    searchable = [d for d in docs if d.status not in ("draft", "superseded")]
    entries = _build_section_index(searchable)
    role_map = _role_index_from_entries(entries)
    counts = {role: 0 for role in CANONICAL_ROLES}
    for roles in role_map.values():
        for role in roles:
            counts[role] += 1
    return counts


def _date_ordinal(doc: Doc) -> int:
    return doc.date.toordinal()
