"""The tool decides what is mechanically decidable and hands the verdict to the human, per row --
except a terminally excluded row, whose verdict cell the tool fills in itself (contracts/gate1.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
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


def _run(store: Path, env: dict[str, str] | None = None) -> "subprocess.CompletedProcess[str]":
    return run_tool(TOOL, store, env)


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


@pytest.mark.parametrize(
    "cited_kwargs, citing_kwargs",
    [
        pytest.param(
            dict(tags="[platform]", repos="[platform-core]"),
            dict(tags="[platform]", repos="[sweeper-svc]"),
            id="shared-tag",
        ),
        pytest.param(
            dict(tags="[platform]", repos="[platform-core]"),
            dict(tags="[sweeper]", repos="[sweeper-svc]", related="widget-cache-v1"),
            id="related-edge",
        ),
        pytest.param(
            dict(tags="[platform]", repos="[platform-core]", related="sweeper-job"),
            dict(tags="[sweeper]", repos="[sweeper-svc]"),
            id="related-edge-declared-only-by-the-cited-document",
        ),
        pytest.param(
            dict(tags="[unrelated]", repos="platform-core"),
            dict(tags="[sweeper]", repos="platform-core"),
            id="shared-repo-from-a-scalar-repos-value",
        ),
    ],
)
def test_a_shared_edge_makes_the_cited_document_adjacent(tmp_path, cited_kwargs, citing_kwargs):
    """Adjacent means the author would plausibly have found it anyway, which is not what the
    retrieval layer claims. A bare scalar `repos:` value degrades to a one-element set, so it
    still shares a repo. The related edge is symmetric: whichever of the two documents happens
    to name the other, the pair is adjacent -- one author writing the link is the whole fact,
    and which side wrote it says nothing about how findable the cited document was."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.",
         **cited_kwargs)
    _doc(sessions, "sweeper-job", body=_reuse("widget-cache-v1", "Eviction runs on boot."),
         **citing_kwargs)

    cells = _row_for(_run(tmp_path).stdout, "widget-cache-v1")

    assert cells[DISTANCE] == "adjacent"


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
# a terminally excluded row states its reason in the verdict cell the tool fills in itself, and
# the summary counts it on the axis that excluded it -- integrity, the citing document's status,
# dogfooding, classification (contracts/gate1.md, "Who fills the last column")
# ---------------------------------------------------------------------------


QUOTELESS_REUSE = (
    "## 16. Reuse Log\n\n| prior-doc | taken | impact | classification |\n"
    "|---|---|---|---|\n| widget-cache-v1 | reused the flush shape | saved a round | "
    "reuse |\n"
)

DISTANT_CITING = dict(tags="[sweeper]", repos="[sweeper-svc]")


@pytest.mark.parametrize(
    "citing_kwargs, citing_body, cited_id, expected_cells, summary_line",
    [
        pytest.param(
            DISTANT_CITING, QUOTELESS_REUSE, "widget-cache-v1",
            {INTEGRITY: "no_quote", DISTANCE: "n/a",
             VERDICT: "excluded: no quote in the `taken` cell"},
            "0 valid (the last column above is theirs to fill), of which 0 distant",
            id="no-quote-in-the-taken-cell",
        ),
        pytest.param(
            DISTANT_CITING, _reuse("nowhere", "Eviction runs on boot here."), "nowhere",
            {INTEGRITY: "cited_missing", DISTANCE: "n/a",
             VERDICT: "excluded: cited document not in store"},
            "0 valid (the last column above is theirs to fill), of which 0 distant",
            id="cited-document-not-in-store",
        ),
        pytest.param(
            DISTANT_CITING,
            _reuse("widget-cache-v1", "Eviction runs on boot.", classification="harmful"),
            "widget-cache-v1",
            {INTEGRITY: "verified", CLASSIFICATION: "harmful",
             VERDICT: "excluded: classification harmful"},
            "0 valid (the last column above is theirs to fill), of which 0 distant",
            id="classification-harmful",
        ),
        pytest.param(
            dict(tags="[sweeper]", repos="[engmem]"),
            _reuse("widget-cache-v1", "Eviction runs on boot."), "widget-cache-v1",
            {VERDICT: "excluded: dogfooding (story about this repository)"},
            "1 dogfooding (story about this repository)",
            id="dogfooding",
        ),
        pytest.param(
            dict(tags="[sweeper]", repos="engmem"),
            _reuse("widget-cache-v1", "Eviction runs on boot."), "widget-cache-v1",
            {VERDICT: "excluded: dogfooding (story about this repository)"},
            "1 dogfooding (story about this repository)",
            id="dogfooding-from-a-scalar-repos-value",
        ),
        pytest.param(
            dict(status="draft", tags="[sweeper]", repos="[sweeper-svc]"),
            _reuse("widget-cache-v1", "Eviction runs on boot."), "widget-cache-v1",
            {VERDICT: "excluded: citing document status is draft"},
            "1 citing document not active (draft/superseded)",
            id="citing-document-is-a-draft",
        ),
    ],
)
def test_an_excluded_row_names_its_reason_in_the_verdict_cell_and_on_the_summary(
    tmp_path, citing_kwargs, citing_body, cited_id, expected_cells, summary_line,
):
    """An excluded row stays visible in the table -- the exclusion is printed, never a silent
    drop. `distance` is `n/a` wherever integrity already decided the row: axis A owns the
    integrity fact, and axis C is not computed for it."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", body=citing_body, **citing_kwargs)

    out = _run(tmp_path).stdout

    cells = _row_for(out, cited_id)
    for column, value in expected_cells.items():
        assert cells[column] == value
    assert summary_line in out


# ---------------------------------------------------------------------------
# supersession is surfaced, not silently discarded or auto-excluding; the summary also names
# how many of the counted distant events cite a superseded document, so the staleness backstop
# does not depend on reading every row
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
    """A row can be verified, cite a superseded document, and still not be a valid
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
# unparseable front matter is undecidable, never distant
# ---------------------------------------------------------------------------


def test_the_flow_sequence_edge_case_no_longer_diverges_from_spine(tmp_path):
    """Regression: this document's front matter has always loaded fine via spine; a
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
    """The only genuinely unreadable `repos` value left after the boundary-search fix above: a
    mapping, neither a list nor a string, which `spine._coerce_list_field` also cannot read."""
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
# the printed distant figure must be sourced from gate1.is_primary_candidate,
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
# the excluded-rows total is a true count; the per-axis breakdown may overlap
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
    """`conftest.run_tool` without the one flag this block needs, and decoding the same way."""
    return subprocess.run(
        [sys.executable, str(TOOL), "--store", str(store), "--since", since],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_the_audit_block_header_and_default_window_are_printed(tmp_path):
    """This store has no telemetry and one document with no Search Trace, so both id-suffixed
    lines render their empty case here -- each a bare count built inline rather than through
    `_missing_line`, so the trailing ` -- ids` suffix must be absent, not merely correct when
    there are ids."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nBody.")

    out = _run(tmp_path).stdout

    assert "=== Audit coverage" in out
    assert "window: all-time" in out
    provenance_line = next(
        l for l in out.splitlines() if "retrieval provenance cannot be reconstructed" in l
    )
    assert provenance_line == (
        "documents whose Search Trace says shell/paste but carry zero matching telemetry "
        "rows (retrieval provenance cannot be reconstructed): 0"
    ), "nothing trails the zero -- no dangling separator"
    orphan_line = next(
        l for l in out.splitlines() if "matches no document in this store" in l
    )
    assert orphan_line == (
        "telemetry rows whose session_id matches no document in this store: 0 row(s) across "
        "0 session_id(s)"
    ), "the orphan line builds its id suffix inline too -- same empty case, same rule"


def test_the_audit_block_excludes_superseded_from_both_ritual_figures(tmp_path):
    """Defect (a): the started denominator is draft + active, not active alone -- an abandoned
    session leaves exactly a draft, which is the population this block exists to notice. A
    superseded document did complete the ritual once, but this counter answers "is the ritual
    currently incomplete" -- a fixture with all three statuses present is the only one that can
    catch a regression that quietly folds `superseded` into `started`."""
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
    would report one number where the reader needs both."""
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
    """A document written before the Pre-reg section existed was counted as a ritual start. It
    is excluded now, and named on its own figure rather than dropped."""
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
# a torn append or a permissions problem on telemetry.jsonl must not take down the
# per-row table or the §11 endpoint figure it is printed alongside. gate1_report.py still exits
# 0; telemetry-derived audit figures are named UNMEASURED, never printed as a false zero.
# ---------------------------------------------------------------------------


def _write_undecodable_bytes(jsonl: Path) -> None:
    with open(jsonl, "wb") as f:
        f.write(b"\xff\xfe not valid utf-8\n")


def _deny_read_permission(jsonl: Path) -> None:
    jsonl.write_text('{"session_id": "sweeper-job"}\n', encoding="utf-8")
    os.chmod(jsonl, 0o000)


@pytest.mark.parametrize(
    "break_telemetry",
    [
        pytest.param(_write_undecodable_bytes, id="undecodable-bytes"),
        pytest.param(_deny_read_permission, id="unreadable-file",
                     marks=requires_permission_enforcement),
    ],
)
def test_broken_telemetry_does_not_fail_the_run_and_the_table_survives(tmp_path, break_telemetry):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))
    jsonl = tmp_path / "telemetry.jsonl"
    break_telemetry(jsonl)
    try:
        result = _run(tmp_path)
    finally:
        os.chmod(jsonl, 0o644)

    assert result.returncode == 0
    assert "| sweeper-job | widget-cache-v1 |" in result.stdout, "the per-row table must survive"
    assert "of which 1 distant" in result.stdout, "the §11 endpoint figure must survive"
    assert "telemetry.jsonl: UNREADABLE" in result.stdout
    assert "UNMEASURED -- telemetry.jsonl unreadable" in result.stdout
    assert " 0 row(s) across 0 distinct session(s)" not in result.stdout, (
        "an unmeasured figure must never be printed as a literal zero"
    )


# ---------------------------------------------------------------------------
# a `## Reuse Log` holding only the template's own header row + separator
# (templates/engmem.save.md, "### Reuse Log rules") is PRESENT, not missing.
# ---------------------------------------------------------------------------


def test_a_header_only_reuse_log_is_present_not_missing_in_the_report(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "header-only-story",
         body="## Pre-reg\n\nBaseline.\n\n## Reuse Log\n\n| prior-doc | taken | impact | classification |\n|---|---|---|---|\n")

    out = _run(tmp_path).stdout

    assert "session documents with a Reuse Log section (any status): 1" in out
    assert "active documents missing a Reuse Log section: 0" in out


# ---------------------------------------------------------------------------
# a `backfilled: true` document never ran the ritual and must not inflate the
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


def test_a_store_with_no_sessions_directory_is_named_before_the_table(tmp_path):
    """Exit 0 stays (contracts/gate1.md), so stdout is the only place a wrong --store can show."""
    result = _run(tmp_path / "no-such-store")

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0].startswith("error: ") and "unknown, not zero" in lines[0], result.stdout
    assert lines[1].startswith("| story | cited |")


def test_a_missing_store_is_named_on_a_legacy_console_code_page(tmp_path):
    """The error line carries an em dash cp437 cannot encode; it must still reach stdout."""
    result = _run(tmp_path / "no-such-store", env={"PYTHONIOENCODING": "cp437"})

    assert result.returncode == 0, result.stderr
    first = result.stdout.splitlines()[0]
    assert first.startswith("error: ") and "unknown, not zero" in first, result.stdout


def test_a_citing_document_that_fails_to_load_is_named_and_counted_nowhere(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    sessions.joinpath("widget-cache-v2.md").write_text(
        "---\nid: widget-cache-v2\ntags: [platform\n---\n\n"
        + _reuse("widget-cache-v1", "Eviction runs on boot."),
        encoding="utf-8",
    )

    result = _run(tmp_path)

    assert result.returncode == 0
    assert result.stdout.startswith("error: widget-cache-v2.md: "), result.stdout
    # the YAML reason runs over several lines; the whole of it still comes before the table
    before_table, _, after = result.stdout.partition("| story | cited |")
    assert "widget-cache-v2.md" in before_table and after
    assert "rows: 0" in result.stdout.splitlines()
