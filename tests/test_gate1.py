"""Unit tests for the shared verdict core -- exercised directly, not through a subprocess.

`tools/verify_citations.py` and `tools/gate1_report.py` each render a slice of `Verdicts`;
what each slice must contain is pinned in `tests/test_verify_citations.py` and
`tests/test_gate1_report.py`. This file pins the verdict model itself."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from engmem import gate1


def _doc(sessions: Path, doc_id: str, *, status: str = "active", tags="[platform]",
          repos: str | None = "[platform-core]", related: str = "", superseded_by: str = "",
          body: str = "## 8. Decision Log\n\nBody.") -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    repos_line = f"repos: {repos}\n" if repos is not None else ""
    (sessions / f"{doc_id}.md").write_text(
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-08-01\nstatus: {status}\n"
        f"superseded_by: {superseded_by}\ntags: {tags}\nentities: [WidgetCache]\n"
        f"{repos_line}related: [{related}]\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _reuse(cited: str, quote: str, *, classification: str = "reuse") -> str:
    return (
        "## 16. Reuse Log\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        f"| {cited} | `WidgetCache.flush()` -- \"{quote}\" | reused the shape | "
        f"{classification} |\n"
    )


def _one_row(store: Path) -> gate1.RowVerdict:
    verdicts = gate1.evaluate(store)
    assert len(verdicts.rows) == 1, verdicts.rows
    return verdicts.rows[0]


# ---------------------------------------------------------------------------
# axis A -- citation integrity, evaluated no_quote -> cited_missing -> quote_not_found -> verified
# ---------------------------------------------------------------------------


def test_a_row_with_no_quote_is_no_quote_not_an_indexerror(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1")
    _doc(sessions, "story", body="## 16. Reuse Log\n\n| prior-doc | taken | impact | "
         "classification |\n|---|---|---|---|\n| widget-cache-v1 | no quote here | x | reuse |\n")

    row = _one_row(tmp_path)

    assert row.integrity == "no_quote"
    assert gate1.exclusion_reason(row) == "excluded: no quote in the `taken` cell"


def test_a_citation_of_a_document_not_in_the_store_is_cited_missing(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story", body=_reuse("nowhere", "Some quoted text here."))

    row = _one_row(tmp_path)

    assert row.integrity == "cited_missing"
    assert row.cited is None
    assert row.staleness is None, "staleness cannot be known for a document that isn't in the store"


def test_a_quote_absent_from_the_cited_body_is_quote_not_found(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nSomething else entirely.")
    _doc(sessions, "story", body=_reuse("widget-cache-v1", "Never written anywhere."))

    row = _one_row(tmp_path)

    assert row.integrity == "quote_not_found"
    assert row.unfound_quotes == ("Never written anywhere.",)
    assert row.distance is None, "distance is axis C, only meaningful once axis A is verified"


def test_a_matching_quote_is_verified(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    row = _one_row(tmp_path)

    assert row.integrity == "verified"
    assert row.distance == "distant"


# ---------------------------------------------------------------------------
# axis B -- classification (cells[3])
# ---------------------------------------------------------------------------


def test_harmful_classification_is_read_and_excludes_the_row(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot.", classification="harmful"))

    row = _one_row(tmp_path)

    assert row.classification == "harmful"
    assert row.integrity == "verified", "harmful is a classification fact, not a citation defect"
    assert gate1.exclusion_reason(row) == "excluded: classification harmful"
    assert not gate1.is_valid(row)


def test_anti_reuse_classification_is_read_and_excludes_the_row(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot.", classification="anti-reuse"))

    row = _one_row(tmp_path)

    assert row.classification == "anti-reuse"
    assert row.integrity == "verified", "anti-reuse is a classification fact, not a citation defect"
    assert row.distance == "distant", "the exclusion must hold even where the row would otherwise count"
    assert gate1.exclusion_reason(row) == "excluded: classification anti-reuse"
    assert not gate1.is_valid(row)
    assert not gate1.is_primary_candidate(row), "a genuine influence event still isn't a reuse event"


def test_a_three_column_row_has_a_missing_classification_not_an_indexerror(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body="## 16. Reuse Log\n\n| prior-doc | taken | impact |\n|---|---|---|\n"
              "| widget-cache-v1 | `x` -- \"Eviction runs on boot.\" | reused it |\n")

    row = _one_row(tmp_path)

    assert row.classification == "missing"
    assert gate1.exclusion_reason(row) == "excluded: classification cell missing"


def test_a_blank_classification_cell_is_missing_not_reuse(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot.", classification=""))

    row = _one_row(tmp_path)

    assert row.classification == "missing"


@pytest.mark.parametrize("written", ["resue", "missing", "unrecognized"])
def test_a_classification_outside_the_three_writable_ones_is_unrecognized(tmp_path, written):
    """`missing` and `unrecognized` are this module's own readings of the cell; a human who
    writes either word has still not written one of the three (VALID_CLASSIFICATIONS)."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot.", classification=written))

    row = _one_row(tmp_path)

    assert row.classification == gate1.Classification.UNRECOGNIZED
    assert row.classification_raw == written
    assert gate1.exclusion_reason(row) == f"excluded: classification {written!r} not recognized"


# ---------------------------------------------------------------------------
# axis D -- staleness, orthogonal to A and C
# ---------------------------------------------------------------------------


def test_a_superseded_citation_is_flagged_stale_but_can_still_be_a_valid_candidate(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", status="superseded", superseded_by="widget-cache-v2",
         body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "widget-cache-v2", body="## 8. Decision Log\n\nEviction is lazy now.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    row = _one_row(tmp_path)

    assert row.staleness == "cited_superseded"
    assert row.integrity == "verified"
    assert row.distance == "distant"
    assert gate1.is_primary_candidate(row), "staleness must not silently exclude -- a human decides"


def test_an_active_cited_document_is_cited_active(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    assert _one_row(tmp_path).staleness == "cited_active"


# ---------------------------------------------------------------------------
# axis C -- distance, including the undecidable case (requirement 5)
# ---------------------------------------------------------------------------


def test_the_flow_sequence_edge_case_no_longer_diverges_from_spine(tmp_path):
    """ARCH-004 regression: `_repos` used to find the front-matter boundary with its own
    substring search, which stopped early on a line beginning `---` inside a legitimately
    continued flow sequence, while `spine.split_front_matter`'s whole-line search correctly
    skipped past it. `_repos` now reuses `spine.split_front_matter` directly, so this
    document -- which spine has always read fine -- must resolve on its REAL
    `repos: [platform-core]`, not read as unparseable."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "widget-cache-v1.md").write_text(
        "---\nid: widget-cache-v1\ntitle: widget-cache-v1\ndate: 2026-08-01\nstatus: active\n"
        "tags: [platform,\n---not-a-delimiter]\nentities: [WidgetCache]\nrepos: [platform-core]\n"
        "related: []\n---\n\n## 8. Decision Log\n\nEviction runs on boot.\n",
        encoding="utf-8",
    )
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    row = _one_row(tmp_path)

    assert row.integrity == "verified", "spine's own parser must still load the document"
    assert row.distance == "distant", "the real repos differ; no false undecidable negative"


def test_unparseable_front_matter_is_undecidable_never_distant(tmp_path):
    """ARCH-001/ARCH-004: after the flow-sequence divergence above was removed at its source,
    the only genuinely unreadable `repos` value left is one `spine._coerce_list_field` also
    cannot read -- a mapping here, neither a list nor a string. The document still loads fine
    (`repos` is not a spine field), so this is not a load failure."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "widget-cache-v1.md").write_text(
        "---\nid: widget-cache-v1\ntitle: widget-cache-v1\ndate: 2026-08-01\nstatus: active\n"
        "tags: [platform]\nentities: [WidgetCache]\nrepos: {a: b}\nrelated: []\n---\n\n"
        "## 8. Decision Log\n\nEviction runs on boot.\n",
        encoding="utf-8",
    )
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    row = _one_row(tmp_path)

    assert row.integrity == "verified", "spine's own parser must still load the document"
    assert row.distance == "undecidable"
    assert gate1.is_valid(row), "undecidable stays open for the human, like adjacent"
    assert not gate1.is_primary_candidate(row), "only distant counts toward the primary endpoint"


# ---------------------------------------------------------------------------
# ARCH-001 -- a bare scalar `repos` value must degrade to a one-element set, like every other
# list field (spine._coerce_list_field), not silently read as "no repos"
# ---------------------------------------------------------------------------


def test_a_scalar_repos_value_makes_dogfooding_reachable(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", repos="engmem",
         body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    _doc(sessions, "story", tags="[sweeper]", repos="engmem",
         body=_reuse("widget-cache-v1", "Widget flush runs eagerly."))

    row = _one_row(tmp_path)

    assert row.dogfooding
    assert gate1.exclusion_reason(row) == "excluded: dogfooding (story about this repository)"


def test_a_scalar_repos_value_still_makes_a_shared_repo_adjacent(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", tags="[unrelated]", repos="platform-core",
         body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    _doc(sessions, "story", tags="[sweeper]", repos="platform-core",
         body=_reuse("widget-cache-v1", "Widget flush runs eagerly."))

    row = _one_row(tmp_path)

    assert row.distance == "adjacent", "a scalar repos value must not read as sharing nothing"


def test_an_adjacent_row_stays_open_for_the_human_not_auto_excluded(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", tags="[platform]", body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    _doc(sessions, "story", tags="[platform]", body=_reuse("widget-cache-v1", "Widget flush runs eagerly."))

    row = _one_row(tmp_path)

    assert row.distance == "adjacent"
    assert gate1.exclusion_reason(row) is None, "adjacent is not a terminal exclusion"
    assert gate1.is_valid(row)
    assert not gate1.is_primary_candidate(row)


# ---------------------------------------------------------------------------
# requirement 4 -- dogfooding
# ---------------------------------------------------------------------------


def test_a_story_about_this_repo_is_excluded_as_dogfooding(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    _doc(sessions, "story", tags="[sweeper]", repos="[engmem]",
         body=_reuse("widget-cache-v1", "Widget flush runs eagerly."))

    row = _one_row(tmp_path)

    assert row.dogfooding
    assert gate1.exclusion_reason(row) == "excluded: dogfooding (story about this repository)"


def test_repos_naming_a_different_repo_is_not_dogfooding(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Widget flush runs eagerly."))

    assert not _one_row(tmp_path).dogfooding


def test_repos_on_a_document_with_no_front_matter_at_all_is_an_empty_set(tmp_path):
    """A document without a `---` block still loads (ENGMEM-SPEC.md section 4); `_repos` must
    read that the same way it reads front matter that parsed but named no `repos` -- `set()`,
    not `None` -- since nothing here is actually unparseable."""
    path = tmp_path / "no-front-matter.md"
    path.write_text("# Just a heading\n\nNo front matter block at all.\n", encoding="utf-8")

    assert gate1._repos(path) == set()


# ---------------------------------------------------------------------------
# draft/superseded citing documents -- their own Reuse Log rows
# ---------------------------------------------------------------------------


def test_a_draft_documents_own_reuse_row_is_excluded_from_the_count(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    _doc(sessions, "story", status="draft", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Widget flush runs eagerly."))

    row = _one_row(tmp_path)

    assert gate1.exclusion_reason(row) == "excluded: citing document status is draft"


def test_a_superseded_documents_own_reuse_row_is_excluded_from_the_count(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    _doc(sessions, "story", status="superseded", superseded_by="story-v2",
         tags="[sweeper]", repos="[sweeper-svc]", body=_reuse("widget-cache-v1", "Widget flush runs eagerly."))
    _doc(sessions, "story-v2", tags="[sweeper]", repos="[sweeper-svc]")

    row = _one_row(tmp_path)

    assert gate1.exclusion_reason(row) == "excluded: citing document status is superseded"


# ---------------------------------------------------------------------------
# "Prior docs used: none." conflicting with real rows
# ---------------------------------------------------------------------------


def test_none_sentence_alongside_real_rows_is_a_conflict_and_rows_are_still_processed(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    body = (
        "## 16. Reuse Log\n\nPrior docs used: none.\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        "| widget-cache-v1 | `x` -- \"Widget flush runs eagerly.\" | reused it | reuse |\n"
    )
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]", body=body)

    verdicts = gate1.evaluate(tmp_path)

    assert verdicts.conflicts == [gate1.DocConflict(doc_id="story", row_count=1)]
    assert verdicts.none_reports == [], "a conflicted document did not honestly report none"
    assert len(verdicts.rows) == 1, "the real row must not be silently dropped"
    assert verdicts.rows[0].integrity == "verified"


def test_a_clean_none_report_produces_no_row_and_no_conflict(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 16. Reuse Log\n\nPrior docs used: none.")

    verdicts = gate1.evaluate(tmp_path)

    assert verdicts.none_reports == ["widget-cache-v1"]
    assert verdicts.conflicts == []
    assert verdicts.rows == []


# ---------------------------------------------------------------------------
# is_valid / is_primary_candidate composition
# ---------------------------------------------------------------------------


def test_primary_candidate_requires_valid_and_distant(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", tags="[platform]", repos="[platform-core]",
         body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Widget flush runs eagerly."))

    row = _one_row(tmp_path)

    assert gate1.is_valid(row)
    assert gate1.is_primary_candidate(row)
    assert gate1.exclusion_reason(row) is None


# ---------------------------------------------------------------------------
# the axes are enums -- a misspelled value can never read as "eligible"
# ---------------------------------------------------------------------------


def _eligible_row(store: Path) -> gate1.RowVerdict:
    sessions = store / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nWidget flush runs eagerly.")
    _doc(sessions, "story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Widget flush runs eagerly."))

    row = _one_row(store)

    assert gate1.exclusion_reason(row) is None, "the row every case below mutates starts eligible"
    return row


@pytest.mark.parametrize("axis, misspelling", [
    (gate1.Integrity, "cited_misssing"),
    (gate1.Classification, "resue"),
    (gate1.Distance, "adjacnet"),
    (gate1.Staleness, "cited_supersedded"),
])
def test_a_misspelled_axis_value_cannot_be_constructed(axis, misspelling):
    with pytest.raises(ValueError):
        axis(misspelling)


@pytest.mark.parametrize(
    "integrity", [i for i in gate1.Integrity if i != gate1.Integrity.VERIFIED]
)
def test_every_integrity_short_of_verified_excludes_the_row(tmp_path, integrity):
    row = replace(_eligible_row(tmp_path), integrity=integrity)

    assert gate1.exclusion_reason(row) is not None, f"{integrity} fell through to eligible"


@pytest.mark.parametrize(
    "classification", [c for c in gate1.Classification if c != gate1.Classification.REUSE]
)
def test_every_classification_other_than_reuse_excludes_the_row(tmp_path, classification):
    row = replace(_eligible_row(tmp_path), classification=classification,
                  classification_raw="resue")

    assert gate1.exclusion_reason(row) is not None, f"{classification} fell through to eligible"


@pytest.mark.parametrize("member", [
    *gate1.Integrity, *gate1.Classification, *gate1.Distance, *gate1.Staleness,
])
def test_an_axis_member_renders_as_its_bare_value(member):
    assert str(member) == member.value
    assert f"{member}" == member.value


def test_the_report_table_joins_axes_as_bare_values(tmp_path):
    """The members are `str` subclasses, which `gate1_report.py`'s `" | ".join(...)` needs --
    without that inheritance the join raises TypeError."""
    row = _eligible_row(tmp_path)

    cells = " | ".join([row.integrity, row.classification, row.distance, row.staleness])

    assert cells == "verified | reuse | distant | cited_active"


# ---------------------------------------------------------------------------
# exclusion_reason's axis order (contracts/gate1.md, ARCH-005) -- first applicable wins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row_fields, citing_status, expected", [
    ({"integrity": gate1.Integrity.NO_QUOTE}, "draft",
     "excluded: no quote in the `taken` cell"),
    ({"dogfooding": True}, "draft",
     "excluded: citing document status is draft"),
    ({"classification": gate1.Classification.HARMFUL}, "draft",
     "excluded: citing document status is draft"),
    ({"classification": gate1.Classification.HARMFUL, "dogfooding": True}, "active",
     "excluded: dogfooding (story about this repository)"),
])
def test_a_row_failing_two_axes_reports_the_earlier_axis(tmp_path, row_fields, citing_status,
                                                         expected):
    eligible = _eligible_row(tmp_path)
    row = replace(eligible, citing=replace(eligible.citing, status=citing_status), **row_fields)

    assert gate1.exclusion_reason(row) == expected
