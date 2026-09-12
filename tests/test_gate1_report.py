"""The tool decides what is mechanically decidable and hands the verdict to the human, per row --
except a terminally excluded row, whose verdict cell the tool fills in itself (contracts/gate1.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import requires_permission_enforcement, run_tool

from engmem import gate1

TOOL = Path(__file__).resolve().parent.parent / "tools" / "gate1_report.py"

# | story | cited | quote | integrity | classification | distance | staleness | verdict |
STORY, CITED, QUOTE, INTEGRITY, CLASSIFICATION, DISTANCE, STALENESS, VERDICT = range(8)


def _doc(sessions: Path, doc_id: str, *, status="active", tags="[platform]",
         repos="[platform-core]", related="", superseded_by="", backfilled=False,
         body="## 8. Decision Log\n\nBody.") -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{doc_id}.md").write_text(
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-08-01\nstatus: {status}\n"
        f"superseded_by: {superseded_by}\nbackfilled: {str(backfilled).lower()}\n"
        f"tags: {tags}\nentities: [WidgetCache]\n"
        f"repos: {repos}\nrelated: [{related}]\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _reuse(cited: str, quote: str, *, classification: str = "reuse") -> str:
    return (
        "## 16. Reuse Log\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        f"| {cited} | `WidgetCache.flush()` — \"{quote}\" | reused the shape | "
        f"{classification} |\n"
    )


def _run(store: Path) -> "subprocess.CompletedProcess[str]":
    return run_tool(TOOL, store)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _row_for(out: str, cited_id: str) -> list[str]:
    line = next(l for l in out.splitlines() if f"| {cited_id} |" in l)
    return _cells(line)


def test_every_valid_reuse_row_appears_with_an_empty_verdict_column(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout
    cells = _row_for(result.stdout, "widget-cache-v1")
    assert cells[STORY] == "sweeper-job", "the citing story must be named first"
    assert cells[CITED] == "widget-cache-v1", "then the document it cited"
    assert cells[INTEGRITY] == "verified"
    assert cells[CLASSIFICATION] == "reuse"
    assert cells[DISTANCE] in ("distant", "adjacent")
    assert cells[VERDICT] == "", "the verdict column is left for the human"


def test_a_cited_doc_sharing_a_tag_is_adjacent(tmp_path):
    """Adjacent means the author would plausibly have found it anyway, which is not what the
    retrieval layer claims."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", tags="[platform]",
         body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "widget-cache-v2", tags="[platform]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    assert "adjacent" in _run(tmp_path).stdout


def test_a_cited_doc_sharing_no_tag_repo_or_related_edge_is_distant(tmp_path):
    """The primary endpoint counts exactly these."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", tags="[platform]", repos="[platform-core]",
         body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    assert "| distant |" in out
    assert "rows: 1" in out
    assert "1 valid (the last column above is theirs to fill), of which 1 distant" in out
    assert "documents reporting no reuse: 0" in out


def test_a_related_edge_makes_it_adjacent(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", tags="[platform]", repos="[platform-core]",
         body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         related="widget-cache-v1", body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    assert "adjacent" in _run(tmp_path).stdout


def test_prior_docs_used_none_produces_no_row_but_is_counted(tmp_path):
    """A store that honestly reports no reuse must be visible as zero, not as an empty screen."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 16. Reuse Log\n\nPrior docs used: none.")

    result = _run(tmp_path)

    assert result.returncode == 0
    assert "rows: 0" in result.stdout
    assert "documents reporting no reuse: 1" in result.stdout
    assert "widget-cache-v1 |" not in result.stdout


# ---------------------------------------------------------------------------
# requirement 1 -- the citation-integrity checks now decide the count, not just print a placeholder
# ---------------------------------------------------------------------------


def test_a_quoteless_row_is_named_no_quote_and_excluded_not_counted_distant(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", tags="[sweeper]", repos="[sweeper-svc]",
         body="## 8. Decision Log\n\nBody.")
    _doc(sessions, "sweeper-job", tags="[different]", repos="[different-svc]",
         body="## 16. Reuse Log\n\n| prior-doc | taken | impact | classification |\n"
              "|---|---|---|---|\n| widget-cache-v1 | reused the flush shape | saved a round | "
              "reuse |\n")

    out = _run(tmp_path).stdout

    cells = _row_for(out, "widget-cache-v1")
    assert cells[INTEGRITY] == "no_quote"
    assert cells[DISTANCE] == "n/a", "axis A owns the integrity fact; axis C is not computed for it"
    assert cells[VERDICT] == "excluded: no quote in the `taken` cell"
    assert "0 valid (the last column above is theirs to fill), of which 0 distant" in out


def test_a_citation_of_a_document_not_in_the_store_is_excluded_not_counted_distant(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "sweeper-job", body=_reuse("nowhere", "Eviction runs on boot here."))

    out = _run(tmp_path).stdout

    cells = _row_for(out, "nowhere")
    assert cells[INTEGRITY] == "cited_missing"
    assert cells[VERDICT] == "excluded: cited document not in store"
    assert "0 distant" in out


# ---------------------------------------------------------------------------
# requirement 2 -- classification (cells[3] of the Reuse Log row) is read
# ---------------------------------------------------------------------------


def test_a_harmful_row_is_verified_but_excluded_by_classification(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot.", classification="harmful"))

    out = _run(tmp_path).stdout

    cells = _row_for(out, "widget-cache-v1")
    assert cells[INTEGRITY] == "verified"
    assert cells[CLASSIFICATION] == "harmful"
    assert cells[VERDICT] == "excluded: classification harmful"
    assert "0 valid (the last column above is theirs to fill), of which 0 distant" in out


# ---------------------------------------------------------------------------
# requirement 3 -- supersession is surfaced, not silently discarded or auto-excluding
# ---------------------------------------------------------------------------


def test_a_superseded_citation_is_flagged_stale_and_still_counts(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", status="superseded", superseded_by="widget-cache-v2",
         body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "widget-cache-v2", body="## 8. Decision Log\n\nEviction is lazy now.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    cells = _row_for(out, "widget-cache-v1")
    assert cells[STALENESS] == "cited_superseded"
    assert cells[DISTANCE] == "distant"
    assert cells[VERDICT] == "", "staleness is a flag for the human, not an automatic exclusion"


# ---------------------------------------------------------------------------
# ARCH-002 -- the summary must name how many of the counted distant events cite a
# superseded document, so the staleness backstop does not depend on reading every row
# ---------------------------------------------------------------------------


def test_the_summary_names_how_many_distant_rows_cite_a_superseded_document(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", status="superseded", superseded_by="widget-cache-v2",
         body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "widget-cache-v2", body="## 8. Decision Log\n\nEviction is lazy now.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    assert "of the 1 distant, 1 cite a superseded document" in out


def test_the_summary_reports_zero_stale_distant_rows_when_none_are_stale(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    assert "of the 1 distant, 0 cite a superseded document" in out


def test_a_stale_citation_excluded_from_the_count_does_not_inflate_the_stale_distant_figure(
    tmp_path,
):
    """ARCH-007: a row can be verified, cite a superseded document, and still not be a valid
    reuse candidate (here: classification harmful). The stale-distant figure must be scoped to
    the counted `distant` population, not to every row that happens to carry a staleness flag --
    otherwise the summary can print M > N against the very endpoint figure it exists to qualify."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", status="superseded", superseded_by="widget-cache-v2",
         body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "widget-cache-v2", body="## 8. Decision Log\n\nEviction is lazy now.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot.", classification="harmful"))

    out = _run(tmp_path).stdout

    assert "of which 0 distant" in out, "the harmful row must not be a primary candidate"
    assert "of the 0 distant, 0 cite a superseded document" in out, (
        "a row excluded from the count must not inflate the stale-distant figure"
    )


# ---------------------------------------------------------------------------
# requirement 4 -- dogfooding (a story about this repository) is excluded
# ---------------------------------------------------------------------------


def test_a_story_about_this_repository_is_excluded_as_dogfooding(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[engmem]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    cells = _row_for(out, "widget-cache-v1")
    assert cells[VERDICT] == "excluded: dogfooding (story about this repository)"
    assert "1 dogfooding (story about this repository)" in out


# ---------------------------------------------------------------------------
# requirement 5 -- unparseable front matter is undecidable, never distant
# ---------------------------------------------------------------------------


def test_the_flow_sequence_edge_case_no_longer_diverges_from_spine(tmp_path):
    """ARCH-004 regression: this document's front matter has always loaded fine via spine; a
    weaker private boundary search in `_repos` used to score it undecidable anyway."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "widget-cache-v1.md").write_text(
        "---\nid: widget-cache-v1\ntitle: widget-cache-v1\ndate: 2026-08-01\nstatus: active\n"
        "tags: [platform,\n---not-a-delimiter]\nentities: [WidgetCache]\nrepos: [platform-core]\n"
        "related: []\n---\n\n## 8. Decision Log\n\nEviction runs on boot.\n",
        encoding="utf-8",
    )
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    cells = _row_for(out, "widget-cache-v1")
    assert cells[DISTANCE] == "distant", "the real repos differ; no false undecidable negative"


def test_unparseable_front_matter_is_undecidable_never_distant(tmp_path):
    """The only genuinely unreadable `repos` value left after ARCH-004: a mapping, neither a
    list nor a string, which `spine._coerce_list_field` also cannot read (ARCH-001)."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "widget-cache-v1.md").write_text(
        "---\nid: widget-cache-v1\ntitle: widget-cache-v1\ndate: 2026-08-01\nstatus: active\n"
        "tags: [platform]\nentities: [WidgetCache]\nrepos: {a: b}\nrelated: []\n---\n\n"
        "## 8. Decision Log\n\nEviction runs on boot.\n",
        encoding="utf-8",
    )
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    cells = _row_for(out, "widget-cache-v1")
    assert cells[DISTANCE] == "undecidable"
    assert cells[VERDICT] == "", "undecidable stays open for the human, like adjacent"
    assert "of which 0 distant" in out


# ---------------------------------------------------------------------------
# ARCH-001 -- a bare scalar `repos` value must degrade to a one-element set, pinned at the
# printed table since the distant figure is what section 11 counts
# ---------------------------------------------------------------------------


def test_a_scalar_repos_value_makes_dogfooding_reachable_in_the_report(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", repos="engmem",
         body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="engmem",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    cells = _row_for(out, "widget-cache-v1")
    assert cells[VERDICT] == "excluded: dogfooding (story about this repository)"
    assert "1 dogfooding (story about this repository)" in out


def test_a_scalar_repos_value_still_makes_a_shared_repo_adjacent_in_the_report(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", tags="[unrelated]", repos="platform-core",
         body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="platform-core",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    cells = _row_for(out, "widget-cache-v1")
    assert cells[DISTANCE] == "adjacent"


# ---------------------------------------------------------------------------
# draft/superseded citing documents' own Reuse Log rows are excluded from the count
# ---------------------------------------------------------------------------


def test_a_drafts_own_reuse_row_is_visible_but_excluded(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", status="draft", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    cells = _row_for(out, "widget-cache-v1")
    assert cells[VERDICT] == "excluded: citing document status is draft"
    assert "1 citing document not active (draft/superseded)" in out


# ---------------------------------------------------------------------------
# "Prior docs used: none." conflicting with real rows
# ---------------------------------------------------------------------------


def test_a_conflicting_reuse_log_is_reported_and_its_rows_still_appear(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    body = (
        "## 16. Reuse Log\n\nPrior docs used: none.\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        "| widget-cache-v1 | `WidgetCache.flush()` — \"Eviction runs on boot.\" | "
        "reused the shape | reuse |\n"
    )
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]", body=body)

    out = _run(tmp_path).stdout

    assert "rows: 1" in out, "the row must not be silently dropped by the conflicting sentence"
    assert "documents reporting no reuse: 0" in out, "a conflict is not an honest none-report"
    assert "sweeper-job: 1 row(s)" in out


# ---------------------------------------------------------------------------
# ARCH-003 -- the printed distant figure must be sourced from gate1.is_primary_candidate,
# not a second, independently-maintained definition of the same rule
# ---------------------------------------------------------------------------


def test_the_printed_distant_figure_equals_sum_of_is_primary_candidate(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "widget-cache-v2", tags="[unrelated]", repos="[other-repo]",
         body="## 8. Decision Log\n\nA second, unrelated fact.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))
    _doc(sessions, "harmful-story", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v2", "A second, unrelated fact.", classification="harmful"))

    out = _run(tmp_path).stdout
    verdicts = gate1.evaluate(tmp_path)
    expected = sum(1 for row in verdicts.rows if gate1.is_primary_candidate(row))

    assert expected == 1, "the harmful row must not inflate the figure this test pins"
    assert f"of which {expected} distant" in out


# ---------------------------------------------------------------------------
# ARCH-006 -- the excluded-rows total is a true count; the per-axis breakdown may overlap
# ---------------------------------------------------------------------------


def test_a_row_excluded_on_two_axes_at_once_is_not_double_counted_in_the_total(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", status="draft", tags="[sweeper]", repos="[engmem]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    out = _run(tmp_path).stdout

    assert "rows excluded from the count: 1" in out, "one row, excluded on two axes at once"
    assert "1 dogfooding (story about this repository)" in out
    assert "1 citing document not active (draft/superseded)" in out


# ---------------------------------------------------------------------------
# Audit coverage block -- sample completeness, not row validity. Rationale and the six
# counters plus the cross-check: contracts/gate1.md, "The audit coverage block."
# ---------------------------------------------------------------------------


def _telemetry_row(store: Path, *, session_id, ts: str = "2026-08-15T12:00:00+00:00") -> None:
    jsonl_path = store / "telemetry.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": ts, "query": "q", "session_id": session_id, "channel": "cli", "n_docs": 1,
            "hits": [], "surfaced": [], "result": "miss",
            "context_bytes": 0, "context_tokens_estimate": 0,
        }) + "\n")


def _run_since(store: Path, since: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, str(TOOL), "--store", str(store), "--since", since],
        capture_output=True, text=True, timeout=30,
    )


def test_the_audit_block_header_and_default_window_are_printed(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nBody.")

    out = _run(tmp_path).stdout

    assert "=== Audit coverage" in out
    assert "window: all-time" in out


def test_the_audit_block_names_both_started_and_completed_the_ritual(tmp_path):
    """Defect (a): the denominator is draft + active, not active alone -- an abandoned session
    leaves exactly a draft, which is the population this block exists to notice."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## Pre-reg\n\nBaseline.")
    _doc(sessions, "abandoned-draft", status="draft", body="## Pre-reg\n\nBaseline.")

    out = _run(tmp_path).stdout

    assert "ritual: 2 started (draft: 1 + active: 1) -> 1 completed (status: active)" in out


def test_the_audit_block_excludes_superseded_from_both_ritual_figures(tmp_path):
    """A superseded document did complete the ritual once, but this counter answers "is the
    ritual currently incomplete" -- a fixture with all three statuses present is the only one
    that can catch a regression that quietly folds `superseded` into `started`."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", status="superseded", superseded_by="widget-cache-v2",
         body="## Pre-reg\n\nBaseline.")
    _doc(sessions, "widget-cache-v2", body="## Pre-reg\n\nBaseline v2.")
    _doc(sessions, "abandoned-draft", status="draft", body="## Pre-reg\n\nBaseline.")

    out = _run(tmp_path).stdout

    assert "ritual: 2 started (draft: 1 + active: 1) -> 1 completed (status: active)" in out
    assert "1 superseded document(s) counted in neither figure" in out


def test_the_audit_block_names_telemetry_rows_and_distinct_sessions_as_two_numbers(tmp_path):
    """Defect (b): a row count and a session count are two different signals -- collapsing them
    would report 40 "sessions" for 8."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nBody.")
    _telemetry_row(tmp_path, session_id="widget-cache-v1")
    _telemetry_row(tmp_path, session_id="widget-cache-v1")

    out = _run(tmp_path).stdout

    assert "2 row(s) across 1 distinct session(s)" in out


def test_the_audit_block_lists_active_documents_missing_sections_by_id(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "bare-story", body="## Pre-reg\n\nBaseline.\n\n## 8. Decision Log\n\nNothing else.")

    out = _run(tmp_path).stdout

    assert "active documents missing a Reuse Log section: 1 -- bare-story" in out
    assert "active documents missing a Search Trace section: 1 -- bare-story" in out


def test_the_audit_block_names_documents_with_no_prereg_section_by_id(tmp_path):
    """The live defect: a document written before the Pre-reg section existed was counted as a
    ritual start. It is excluded now, and named on its own figure rather than dropped."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "20260101-widget-cache",
         body="## Pre-reg\n\nBaseline.\n\n## 8. Decision Log\n\nBody.")
    _doc(sessions, "20250101-pre-ritual-note", body="## 8. Decision Log\n\nOlder than the ritual.")

    out = _run(tmp_path).stdout

    assert "ritual: 1 started (draft: 0 + active: 1) -> 1 completed (status: active)" in out
    assert (
        "1 document(s) with no Pre-reg section also counted in neither figure -- written "
        "before the ritual existed, or outside it" in out
    )
    assert (
        "session documents with no Pre-reg section, excluded from the ritual population (any "
        "status; a backfilled document is counted on the backfilled figure instead): 1 -- "
        "20250101-pre-ritual-note" in out
    )


def test_the_audit_block_flags_a_document_whose_search_trace_has_no_matching_telemetry_row(
    tmp_path,
):
    """Defect (c), the free cross-check: a Search Trace of shell/paste with zero telemetry rows
    naming this document's id means the retrieval provenance cannot be reconstructed."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "orphaned-trace",
         body="## Pre-reg\n\nBaseline.\n\n## Search Trace\n\nshell\n")

    out = _run(tmp_path).stdout

    assert (
        "documents whose Search Trace says shell/paste but carry zero matching telemetry "
        "rows (retrieval provenance cannot be reconstructed): 1 -- orphaned-trace" in out
    )


def test_the_orphan_row_line_names_the_load_bearing_scope_qualifier(tmp_path):
    """Caveat 1: `--session` is deliberately unvalidated, so "matches no document" must say
    *in this store* -- the qualifier is load-bearing, not decoration."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nBody.")
    _telemetry_row(tmp_path, session_id="ghost-session")

    out = _run(tmp_path).stdout

    assert "session_id matches no document in this store: 1 row(s) across 1 session_id(s)" in out
    assert "ghost-session" in out


def test_the_two_structural_caveats_are_always_printed(tmp_path):
    """Stated in the output rather than fixed -- both caveats belong on every run, not only when
    a figure they qualify is nonzero."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nBody.")

    out = _run(tmp_path).stdout

    assert "deliberately unvalidated" in out
    assert "append-only with no rotation" in out


def test_since_narrows_the_telemetry_window_and_the_line_says_so(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nBody.")
    _telemetry_row(tmp_path, session_id="widget-cache-v1", ts="2026-08-01T00:00:00+00:00")
    _telemetry_row(tmp_path, session_id="widget-cache-v1", ts="2026-08-20T00:00:00+00:00")

    out = _run_since(tmp_path, "2026-08-10").stdout

    assert "window: since 2026-08-10T00:00:00+00:00" in out
    assert "1 row(s) across 1 distinct session(s)" in out


def test_an_unparseable_since_value_does_not_fail_the_run(tmp_path):
    """gate1_report.py exits 0 unconditionally, pinned for the base report; the audit block's
    own new flag must not grow an exit path the contract already forbids."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nBody.")

    result = _run_since(tmp_path, "not-a-real-date")

    assert result.returncode == 0
    assert "all-time (--since" in result.stdout


# ---------------------------------------------------------------------------
# ARCH-101 -- a torn append or a permissions problem on telemetry.jsonl must not take down the
# per-row table or the §11 endpoint figure it is printed alongside. gate1_report.py still exits
# 0; telemetry-derived audit figures are named UNMEASURED, never printed as a false zero.
# ---------------------------------------------------------------------------


def test_invalid_utf8_in_telemetry_does_not_fail_the_run_and_the_table_survives(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))
    jsonl = tmp_path / "telemetry.jsonl"
    with open(jsonl, "wb") as f:
        f.write(b"\xff\xfe not valid utf-8\n")

    result = _run(tmp_path)

    assert result.returncode == 0
    assert "| sweeper-job | widget-cache-v1 |" in result.stdout, "the per-row table must survive"
    assert "of which 1 distant" in result.stdout, "the §11 endpoint figure must survive"
    assert "telemetry.jsonl: UNREADABLE" in result.stdout
    assert "UNMEASURED -- telemetry.jsonl unreadable" in result.stdout
    assert " 0 row(s) across 0 distinct session(s)" not in result.stdout, (
        "an unmeasured figure must never be printed as a literal zero"
    )


@requires_permission_enforcement
def test_an_unreadable_telemetry_file_does_not_fail_the_run_and_the_table_survives(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))
    jsonl = tmp_path / "telemetry.jsonl"
    jsonl.write_text('{"session_id": "sweeper-job"}\n', encoding="utf-8")
    os.chmod(jsonl, 0o000)
    try:
        result = _run(tmp_path)
    finally:
        os.chmod(jsonl, 0o644)

    assert result.returncode == 0
    assert "| sweeper-job | widget-cache-v1 |" in result.stdout
    assert "of which 1 distant" in result.stdout
    assert "telemetry.jsonl: UNREADABLE" in result.stdout
    assert "UNMEASURED -- telemetry.jsonl unreadable" in result.stdout


# ---------------------------------------------------------------------------
# ARCH-102 -- a `## Reuse Log` holding only the template's own header row + separator
# (templates/engmem.save.md:162-163) is PRESENT, not missing.
# ---------------------------------------------------------------------------


def test_a_header_only_reuse_log_is_present_not_missing_in_the_report(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "header-only-story",
         body="## Pre-reg\n\nBaseline.\n\n## Reuse Log\n\n| prior-doc | taken | impact | classification |\n|---|---|---|---|\n")

    out = _run(tmp_path).stdout

    assert "session documents with a Reuse Log section (any status): 1" in out
    assert "active documents missing a Reuse Log section: 0" in out


# ---------------------------------------------------------------------------
# ARCH-103 -- a `backfilled: true` document never ran the ritual and must not inflate the
# ritual-started/completed figures or appear in the missing-section lists.
# ---------------------------------------------------------------------------


def test_a_backfilled_document_does_not_inflate_the_ritual_figures(tmp_path):
    sessions = tmp_path / "sessions"
    complete_body = (
        "## Pre-reg\n\nBaseline.\n\n"
        "## Reuse Log\n\nPrior docs used: none.\n\n"
        "## Search Trace\n\nshell\n"
    )
    _doc(sessions, "ritual-story", body=complete_body)
    _doc(sessions, "backfilled-note", backfilled=True,
         body="## 8. Decision Log\n\nWritten after the fact, no ritual structure.")

    out = _run(tmp_path).stdout

    assert "ritual: 1 started (draft: 0 + active: 1) -> 1 completed (status: active)" in out
    assert "1 backfilled document(s) also counted in neither figure" in out

    audit_block = out.split("=== Audit coverage")[1]
    assert "active documents missing a Reuse Log section: 0" in audit_block
    assert "active documents missing a Search Trace section: 0" in audit_block
    assert (
        "session documents with no Pre-reg section, excluded from the ritual population (any "
        "status; a backfilled document is counted on the backfilled figure instead): 0"
        in audit_block
    ), "a backfilled document is counted once, under the backfilled figure, not twice"
    assert "backfilled-note" not in audit_block, (
        "a backfilled document was never asked to run the ritual, so it must not appear on "
        "any missing-section line"
    )


# ---------------------------------------------------------------------------
# ARCH-107 -- end-to-end: `_trace_value` must never read a provenance claim out of ordinary
# prose in the printed "retrieval provenance cannot be reconstructed" line.
# ---------------------------------------------------------------------------


def test_a_document_that_genuinely_reports_miss_never_appears_on_the_provenance_line(tmp_path):
    """False positive direction: a document whose Search Trace says `miss`, even alongside a
    sentence that happens to use the word `shell`, must not be counted as an unreconstructable
    shell/paste claim."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "honest-miss", body=(
        "## Pre-reg\n\nBaseline.\n\n## Search Trace\n\nNo shell was available in this environment, so the search never "
        "ran.\nmiss\n"
    ))

    out = _run(tmp_path).stdout

    provenance_line = next(
        l for l in out.splitlines() if "retrieval provenance cannot be reconstructed" in l
    )
    assert provenance_line.endswith(": 0")
    assert "honest-miss" not in provenance_line


def test_a_document_that_genuinely_ran_shell_still_appears_despite_a_nearby_miss_mention(tmp_path):
    """False negative direction: a document whose Search Trace really is `shell`, with zero
    matching telemetry rows, must still be flagged even when an earlier line mentions `miss`."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "real-shell",
         body="## Pre-reg\n\nBaseline.\n\n## Search Trace\n\n(A miss would mean no search ran.)\nshell\n")

    out = _run(tmp_path).stdout

    audit_block = out.split("=== Audit coverage")[1]
    assert (
        "retrieval provenance cannot be reconstructed): 1 -- real-shell" in audit_block
    )


def test_a_line_naming_two_vocabulary_words_at_once_does_not_resolve_to_either(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "mixed-story", body=(
        "## Pre-reg\n\nBaseline.\n\n## Search Trace\n\nConsidered paste via the user, ended up miss -- shell was "
        "blocked.\n"
    ))

    out = _run(tmp_path).stdout

    provenance_line = next(
        l for l in out.splitlines() if "retrieval provenance cannot be reconstructed" in l
    )
    assert provenance_line.endswith(": 0")
    assert "mixed-story" not in provenance_line



def test_a_backfilled_document_with_a_shell_trace_still_appears_on_the_provenance_line(tmp_path):
    """ARCH-001: the ritual exclusions do not reach this figure. A document claiming a search ran,
    with no telemetry row naming it, is named here whoever wrote it and whenever."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "20250101-pre-ritual-note", backfilled=True,
         body="## Pre-reg\n\nBaseline.\n\n## Search Trace\n\nshell\n")

    out = _run(tmp_path).stdout

    audit_block = out.split("=== Audit coverage")[1]
    assert "ritual: 0 started (draft: 0 + active: 0) -> 0 completed (status: active)" in audit_block
    assert (
        "retrieval provenance cannot be reconstructed): 1 -- 20250101-pre-ritual-note"
        in audit_block
    )
