"""US-02: exact lexical search over Unicode text, under one normalisation rule set
(contracts/scoring.md, "Tokenisation")."""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

import pytest

from conftest import write_file
from mcp_harness import _call, _text

from engmem import cache
from engmem.cli import main
from engmem.scoring import (
    _MARK_PLANES,
    _camel_expansion,
    _mark_ranges,
    _token_run_re,
    search,
    tokenize_raw,
)
from engmem.spine import load_store


def _doc(doc_id: str, title: str, body: str, entities: str = "[]") -> str:
    return (
        f"---\nid: {doc_id}\ntitle: {title}\ndate: 2026-01-01\ntask_date: 2026-01-01\n"
        f"status: active\nsuperseded_by:\nbackfilled: false\ntags: []\n"
        f"entities: {entities}\nrelated: []\ncovers_files: []\n---\n\n{body}"
    )


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


# (language, unique term, where it lives). Every term is absent from every other document, so
# "found" can only mean the tokenizer kept the whole word on both sides.
MATRIX = [
    ("en", "reconciliation", "title"),
    ("ru", "идемпотентность", "body"),
    ("uk", "ідемпотентність", "title"),
    ("de", "Zahlungsstornierung", "body"),
    ("fr", "annulation", "title"),
    ("es", "cancelación", "body"),
    ("pt", "reconciliação", "title"),
    ("nl", "coördinatie", "body"),
    ("pl", "anulowanie", "title"),
    ("pl-diacritics", "zamówienie", "body"),
    ("el", "ακύρωση", "body"),
]

FILLER = "## Landmines\n\nThe gateway times out under load.\n"


def _matrix_doc(lang: str, term: str, where: str) -> str:
    doc_id = f"3000-{lang}"
    if where == "title":
        return _doc(doc_id, f"Notes {term}", f"## Decision Log\n\nNothing notable.\n\n{FILLER}")
    return _doc(doc_id, "Notes", f"## Decision Log\n\nWe chose {term} here.\n\n{FILLER}")


def _write_matrix_store(root: Path) -> Path:
    sessions = root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for lang, term, where in MATRIX:
        write_file(sessions, f"3000-{lang}.md", _matrix_doc(lang, term, where))
    return sessions


def _token(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


@pytest.mark.parametrize("lang,term,where", [pytest.param(*row, id=row[0]) for row in MATRIX])
def test_ac_02_1_a_matrix_term_finds_its_record_and_names_the_match(tmp_path, lang, term, where):
    docs = load_store(_write_matrix_store(tmp_path)).docs

    hits = search(docs, term).hits

    assert [h.doc.id for h in hits] == [f"3000-{lang}"]
    expected_place = "title" if where == "title" else "§1-decision-log"
    assert hits[0].matched_fields == {expected_place: [_token(term)]}


def _cli_search(store: Path, query: str, capsys) -> str:
    assert main(["search", query, "--store", str(store)]) == 0
    return capsys.readouterr().out


def _mcp_search(store: Path, query: str, capsys) -> str:
    result, is_error = _call(store, name="engmem_search", arguments={"query": query})
    assert not is_error
    return _text(result)


def _result_lines(output: str) -> list[str]:
    return [
        line for line in output.splitlines() if not line.startswith(("docs: ", "note: "))
    ]


@pytest.mark.parametrize("lang,term,where", [pytest.param(*row, id=row[0]) for row in MATRIX])
def test_ac_02_1_and_02_5_cli_and_mcp_render_the_same_unicode_match(
    tmp_path, capsys, lang, term, where
):
    _write_matrix_store(tmp_path)

    cli = _cli_search(tmp_path, term, capsys)
    mcp = _mcp_search(tmp_path, term, capsys)

    assert f"### 3000-{lang} " in cli
    place = "title" if where == "title" else "§1-decision-log"
    assert f"matched: {place}={_token(term)}" in cli
    assert _result_lines(cli) == _result_lines(mcp)


@pytest.mark.parametrize(
    "document_text,query",
    [
        pytest.param("Отмена заказа", "ОТМЕНА, заказа!", id="ru_case_and_punctuation"),
        pytest.param("ІДЕМПОТЕНТНІСТЬ", "ідемпотентність?", id="uk_case"),
        pytest.param("Straße", "STRASSE", id="de_sharp_s_full_case_folding"),
        pytest.param("«Annulation»", "ANNULATION.", id="fr_guillemets"),
        pytest.param("¡Cancelación!", "CANCELACIÓN", id="es_inverted_marks"),
        pytest.param("Reconciliação;", "RECONCILIAÇÃO", id="pt_case"),
        pytest.param("(Coördinatie)", "coördinatie", id="nl_parentheses"),
        pytest.param("ZAMÓWIENIE", "zamówienie,", id="pl_case"),
        pytest.param("λάθος", "ΛΆΘΟΣ", id="el_final_sigma"),
    ],
)
def test_ac_02_2_case_and_surrounding_punctuation_do_not_matter(tmp_path, document_text, query):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(
        sessions, "4000-pair.md",
        _doc("4000-pair", "Notes", f"## Decision Log\n\n{document_text}\n\n{FILLER}"),
    )
    write_file(sessions, "4001-other.md", _doc("4001-other", "Other", FILLER))

    hits = search(load_store(sessions).docs, query).hits

    assert [h.doc.id for h in hits] == ["4000-pair"]
    assert hits[0].matched_fields["§1-decision-log"] == [_token(w) for w in tokenize_raw(query)]


APOSTROPHES = {
    "ascii": "'",
    "right_quote": "\u2019",
    "modifier_letter": "\u02bc",
    "turned_comma": "\u02bb",
    "reversed_comma": "\u02bd",
}


@pytest.mark.parametrize("word", ["об{}єкт", "don{}t"], ids=["uk", "en"])
@pytest.mark.parametrize("query_mark", APOSTROPHES.values(), ids=APOSTROPHES.keys())
@pytest.mark.parametrize("document_mark", APOSTROPHES.values(), ids=APOSTROPHES.keys())
def test_ac_02_2_every_apostrophe_form_separates_alike(tmp_path, word, document_mark, query_mark):
    """U+02BC is a letter (Lm), so `обʼєкт` stayed one token while `об'єкт` split in two, and the
    two spellings of one Ukrainian word could not find each other."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(
        sessions, "4100-apostrophe.md",
        _doc("4100-apostrophe", "Notes",
             f"## Decision Log\n\n{word.format(document_mark)}\n\n{FILLER}"),
    )
    write_file(sessions, "4101-other.md", _doc("4101-other", "Other", FILLER))

    query = word.format(query_mark)
    hits = search(load_store(sessions).docs, query).hits

    assert tokenize_raw(query) == word.split("{}")
    assert [h.doc.id for h in hits] == ["4100-apostrophe"]


def test_the_double_apostrophe_letter_separates_too():
    assert tokenize_raw("a\u02eeb") == ["a", "b"]


@pytest.mark.parametrize("document_form", [_nfc, _nfd], ids=["doc_nfc", "doc_nfd"])
@pytest.mark.parametrize("query_form", [_nfc, _nfd], ids=["query_nfc", "query_nfd"])
def test_ac_02_3_canonically_equivalent_spellings_match(tmp_path, document_form, query_form):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(
        sessions, "5000-cafe.md",
        _doc("5000-cafe", "Notes", f"## Decision Log\n\nThe {document_form('café')} job.\n\n" + FILLER),
    )

    hits = search(load_store(sessions).docs, query_form("café")).hits

    assert [h.doc.id for h in hits] == ["5000-cafe"]
    assert hits[0].matched_fields == {"§1-decision-log": [_nfc("café")]}


def test_ac_02_3_diacritics_stay_significant(tmp_path):
    """Diacritic-insensitive matching is a separate slice: `cafe` is a different word."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(
        sessions, "5000-coffee.md",
        _doc("5000-coffee", "Notes", f"## Decision Log\n\ncafé\n\n{FILLER}"),
    )
    docs = load_store(sessions).docs

    assert [h.doc.id for h in search(docs, "café").hits] == ["5000-coffee"]
    assert search(docs, "cafe").hits == []


def test_ac_02_3_a_combining_mark_without_a_precomposed_form_stays_in_the_word(tmp_path):
    """`и` + U+0301 has no precomposed code point, so NFKC leaves the mark standing; splitting
    on it cut the word in two."""
    stressed = "за́мок"
    assert tokenize_raw(f"Это {stressed}.") == ["Это", stressed]

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(
        sessions, "5001-stress.md",
        _doc("5001-stress", "Notes", f"## Decision Log\n\n{stressed}\n\n{FILLER}"),
    )
    write_file(sessions, "5002-plain.md", _doc("5002-plain", "Notes", "## Decision Log\n\nмок\n"))

    hits = search(load_store(sessions).docs, stressed).hits

    assert [h.doc.id for h in hits] == ["5001-stress"]
    assert hits[0].matched_fields == {"§1-decision-log": [stressed]}


MIXED_BODY = (
    "## Decision Log\n\nPaymentService calls CacheMémoire before the ακύρωση step.\n\n"
    f"{FILLER}"
)


@pytest.fixture
def mixed_docs(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "6000-mixed.md", _doc("6000-mixed", "Notes", MIXED_BODY))
    write_file(
        sessions, "6001-moire.md",
        _doc("6001-moire", "Notes", "## Decision Log\n\nA moire pattern on the panel.\n"),
    )
    write_file(sessions, "6002-other.md", _doc("6002-other", "Other", FILLER))
    return load_store(sessions).docs


@pytest.mark.parametrize(
    "query,expected_matched",
    [
        pytest.param("CacheMémoire", ["cachemémoire", "cache", "mémoire"], id="exact_mixed"),
        pytest.param("mémoire", ["mémoire"], id="mixed_fragment"),
        pytest.param("PaymentService", ["paymentservice", "payment", "service"], id="ascii_exact"),
        pytest.param("payment service", ["payment", "service"], id="ascii_fragments"),
        pytest.param("ακύρωση PaymentService", None, id="mixed_query"),
    ],
)
def test_ac_02_4_mixed_identifiers_and_queries_match(mixed_docs, query, expected_matched):
    hits = search(mixed_docs, query).hits

    assert [h.doc.id for h in hits] == ["6000-mixed"]
    matched = hits[0].matched_fields["§1-decision-log"]
    if expected_matched is not None:
        assert matched == expected_matched
    else:
        assert {"ακύρωση", "paymentservice"} <= set(matched)


def test_ac_02_4_diacritics_do_not_invent_ascii_fragments(mixed_docs):
    assert _camel_expansion("CacheMémoire") == (["cache", "mémoire"], "cm")
    for invented in ("moire", "cachem"):
        assert "6000-mixed" not in [h.doc.id for h in search(mixed_docs, invented).hits]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ResponseCacheController", (["response", "cache", "controller"], "rcc")),
        ("HTTPServer2Go", (["http", "server", "2", "go"], "hs2g")),
        ("MQSweeper", (["mq", "sweeper"], "ms")),
        ("XMLHttpRequest", (["xml", "http", "request"], "xhr")),
        ("iOS", (["i", "os"], "io")),
        ("foo123bar", (["foo", "123", "bar"], "f1b")),
        ("ETAG", None),
        ("ОтменаЗаказа", (["отмена", "заказа"], "оз")),
        ("ΑΚΎΡΩΣΗ", None),
    ],
)
def test_ac_02_5_identifier_splitting_keeps_its_ascii_rules(raw, expected):
    assert _camel_expansion(raw) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("snake_case_name", ["snake", "case", "name"]),
        ("v2.1-rc3", ["v2", "1", "rc3"]),
        ("x_y-z.w", ["x", "y", "z", "w"]),
        ("ﬁlter ＭＱ", ["filter", "MQ"]),
    ],
)
def test_ac_02_5_ascii_separators_are_unchanged(text, expected):
    assert tokenize_raw(text) == expected


def _stale_entry_for(path: Path) -> None:
    """Rewrites `path`'s cache entry to what the pre-US-02 tokenizer stored for it."""
    entry_path = cache._entry_path(path)[0]
    record = json.loads(entry_path.read_text(encoding="utf-8"))
    for section in record["payload"]["sections"]:
        for table in ("literal_tf", "derived_tf"):
            section[table] = {t: n for t, n in section[table].items() if t.isascii()}
    # the literal version this change bumped past, not "current minus one"
    record["format_version"] = 3
    entry_path.write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("run", [_cli_search, _mcp_search], ids=["cli", "mcp"])
def test_ac_02_6_a_pre_upgrade_cache_entry_is_not_served(tmp_path, run, capsys):
    sessions = _write_matrix_store(tmp_path)
    ru_doc = sessions / "3000-ru.md"
    assert "### 3000-ru " in run(tmp_path, "идемпотентность", capsys)
    _stale_entry_for(ru_doc)

    out = run(tmp_path, "идемпотентность", capsys)

    assert "### 3000-ru " in out
    record = json.loads(cache._entry_path(ru_doc)[0].read_text(encoding="utf-8"))
    assert record["format_version"] == cache.CACHE_FORMAT_VERSION
    assert "идемпотентность" in record["payload"]["sections"][0]["literal_tf"]


def test_ac_02_6_the_cache_version_names_the_unicode_database(tmp_path):
    """NFKC, casefold and the letter/mark classes all come from `unicodedata`; tokens one Python
    derived must not be served to another with a different Unicode version."""
    assert unicodedata.unidata_version in str(cache.CACHE_FORMAT_VERSION)


def test_the_mark_scan_covers_every_plane_with_marks():
    """The token pattern scans only `_MARK_PLANES` for marks, to keep its per-process build cheap;
    a Unicode version that adds a mark in another plane must fail here, not split words."""
    planes = (range(p << 16, (p + 1) << 16) for p in _MARK_PLANES)
    assert _mark_ranges(c for plane in planes for c in plane) == _mark_ranges(
        range(sys.maxunicode + 1)
    )


def test_the_token_pattern_is_exactly_letters_numbers_and_marks():
    """Every code point, every plane: `\\w` supplies L and N, the scan supplies M, and only `_`
    and the apostrophe letters are carved out."""
    pattern = _token_run_re()
    carved_out = {"_", "\u02bc", "\u02bb", "\u02bd", "\u02ee"}
    mismatched = [
        hex(cp)
        for cp in range(sys.maxunicode + 1)
        if (pattern.fullmatch(chr(cp)) is not None)
        != (unicodedata.category(chr(cp))[0] in "LNM" and chr(cp) not in carved_out)
    ]
    assert mismatched == []
