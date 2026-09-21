"""Unit tests for the sample-completeness audit -- a different question from `gate1.py`'s
per-row verdicts (is the ritual's experimental record complete, not is any one citation valid).
Rationale: contracts/gate1.md, "The audit coverage block."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import requires_permission_enforcement

from engmem import gate1, gate1_audit


def _doc(sessions: Path, doc_id: str, *, status: str = "active", backfilled: bool = False,
         body: str = "## Decision Log\n\nBody.") -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{doc_id}.md").write_text(
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-08-01\nstatus: {status}\n"
        f"backfilled: {str(backfilled).lower()}\n"
        "tags: [platform]\nentities: [WidgetCache]\nrelated: []\n---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _reuse(cited: str, quote: str, *, classification: str = "reuse") -> str:
    return (
        "## Reuse Log\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        f"| {cited} | `x` -- \"{quote}\" | reused it | {classification} |\n"
    )


# the template's own header row + separator, with zero data rows -- `templates/engmem.save.md`,
# "### Reuse Log rules" -- the exact shape for which `gate1.evaluate()` produces neither a row,
# a none-report, nor a conflict
HEADER_ONLY_REUSE = (
    "## Reuse Log\n\n"
    "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
)

# a document joins the ritual population only by carrying a Pre-reg section (contracts/gate1.md,
# "(e) A document with no Pre-reg section never ran the ritual either"); fixtures that exercise a
# different figure prepend this so their document is inside that population
PREREG = "## Pre-reg\n\nBaseline.\n\n"


def _row(jsonl_path: Path, *, session_id, ts: str = "2026-08-15T12:00:00+00:00") -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": ts, "query": "q", "session_id": session_id, "channel": "cli", "n_docs": 1,
            "hits": [], "surfaced": [], "result": "miss",
            "context_bytes": 0, "context_tokens_estimate": 0,
        }) + "\n")


def _evaluate(store: Path, *, since: str | None = None) -> gate1_audit.AuditReport:
    return gate1_audit.evaluate(store, gate1.evaluate(store), since=since)


# ---------------------------------------------------------------------------
# defect (a) -- the denominator for "started the ritual" is draft + active, not active alone;
# the numerator for "completed" is active; superseded is counted in neither
# ---------------------------------------------------------------------------


def test_ritual_counts_separate_draft_active_and_superseded(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "draft-one", status="draft", body=PREREG)
    _doc(sessions, "active-one", status="active", body=PREREG)
    _doc(sessions, "active-two", status="active", body=PREREG)
    _doc(sessions, "superseded-one", status="superseded", body=PREREG)

    report = _evaluate(tmp_path)

    assert report.draft_count == 1
    assert report.active_count == 2
    assert report.superseded_count == 1


# ---------------------------------------------------------------------------
# defect (b) -- telemetry search coverage is two named numbers (rows, distinct sessions),
# never collapsed into one
# ---------------------------------------------------------------------------


def test_telemetry_counts_report_rows_and_distinct_sessions_separately(tmp_path):
    _doc(tmp_path / "sessions", "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    _row(jsonl, session_id="story-a")
    _row(jsonl, session_id="story-a")
    _row(jsonl, session_id="story-b-not-in-store")

    report = _evaluate(tmp_path)

    assert report.telemetry_rows_with_session == 3, "three rows carried a session_id"
    assert report.telemetry_distinct_sessions == 2, "only two distinct ids among them"


def test_a_row_with_no_session_id_does_not_count_toward_either_telemetry_figure(tmp_path):
    _doc(tmp_path / "sessions", "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    _row(jsonl, session_id=None)

    report = _evaluate(tmp_path)

    assert report.telemetry_rows_with_session == 0
    assert report.telemetry_distinct_sessions == 0


# ---------------------------------------------------------------------------
# "sessions with a Reuse Log" / "Prior docs used: none." -- counter 4 reuses
# `gate1.Verdicts.none_reports` rather than re-deriving the none-line check, so it agrees with
# `gate1_report.py`'s own "documents reporting no reuse" figure and inherits the conflict rule
# ---------------------------------------------------------------------------


def test_reuse_log_count_includes_any_status_with_a_section_present(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1")
    _doc(sessions, "has-rows", body=_reuse("widget-cache-v1", "Body."))
    _doc(sessions, "has-none", body="## Reuse Log\n\nPrior docs used: none.")
    _doc(sessions, "has-neither", body="## Decision Log\n\nNo reuse log section at all.")
    _doc(sessions, "has-header-only", body=HEADER_ONLY_REUSE)

    report = _evaluate(tmp_path)

    assert report.reuse_log_count == 3, (
        "widget-cache-v1 and has-neither have no Reuse Log section at all; the other three do, "
        "including the header-only one, by PRESENCE, not by whether gate1.py recognised a row"
    )


# ---------------------------------------------------------------------------
# a `## Reuse Log` holding only the template's own header row + separator is
# PRESENT, not missing. `gate1.evaluate()` recognises neither a row nor a none-sentence in it,
# so a figure derived from `gate1.Verdicts` alone (rather than section presence, `_has_role`)
# reads it backwards: absent from "has a Reuse Log," listed under "missing a Reuse Log."
# ---------------------------------------------------------------------------


def test_a_header_only_reuse_log_counts_as_present_not_missing(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "header-only-story", body=PREREG + HEADER_ONLY_REUSE)

    report = _evaluate(tmp_path)

    assert report.reuse_log_count == 1, "the section is present, even though it holds no rows"
    assert report.active_missing_reuse == [], (
        "a header-only Reuse Log is not missing -- it is present and empty of rows, a different "
        "fact `none_report_count` and `gate1.py`'s row verdicts already cover"
    )


def test_none_report_count_excludes_a_conflicted_document(tmp_path):
    """A document with both the none-sentence and real rows did not honestly report none --
    `gate1.py` already knows this (`Verdicts.conflicts`); the audit must not recount it."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1")
    conflicted_body = (
        "## Reuse Log\n\nPrior docs used: none.\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        "| widget-cache-v1 | `x` -- \"Body.\" | reused it | reuse |\n"
    )
    _doc(sessions, "conflicted", body=conflicted_body)
    _doc(sessions, "clean-none", body="## Reuse Log\n\nPrior docs used: none.")

    report = _evaluate(tmp_path)

    assert report.none_report_count == 1, "the conflicted document must not count as a clean none"
    assert report.reuse_log_count == 2, "the conflicted document still has the section, structurally"


# ---------------------------------------------------------------------------
# active documents missing a Reuse Log / Search Trace / Pre-reg section -- scoped to active
# only, and each gap names the document, not just a count
# ---------------------------------------------------------------------------


def test_active_missing_sections_lists_the_doc_ids(tmp_path):
    sessions = tmp_path / "sessions"
    complete_body = (
        "## Pre-reg\n\nBaseline.\n\n"
        "## Reuse Log\n\nPrior docs used: none.\n\n"
        "## Search Trace\n\nshell\n"
    )
    _doc(sessions, "complete-story", body=complete_body)
    _doc(sessions, "bare-story", body=PREREG + "## Decision Log\n\nNothing else.")

    report = _evaluate(tmp_path)

    assert report.active_missing_reuse == ["bare-story"], "the complete story is not missing either"
    assert report.active_missing_trace == ["bare-story"]


def test_a_drafts_missing_sections_are_not_counted_active_only_is_the_population(tmp_path):
    """A draft has not reached the save ritual yet -- Pre-reg is its only expected section
    (templates/engmem.start.md step 2); flagging it here would be noise, not a finding."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "abandoned-draft", status="draft", body="## Pre-reg\n\nBaseline.")

    report = _evaluate(tmp_path)

    assert report.active_missing_reuse == []
    assert report.active_missing_trace == []


# ---------------------------------------------------------------------------
# defect (c) -- the free cross-check: a document whose Search Trace says shell/paste but for
# which zero telemetry rows carry its id as session_id. `miss` means the search never ran at
# all, so there is nothing to reconstruct and it is not part of the cross-check.
#
# `_trace_value` reads that claim by an exact whole-line match and never out of ordinary prose:
# a line is either EXACTLY `shell`/`paste`/`miss` once whitespace and a trailing `.`/`:` are
# stripped, or it names nothing. contracts/gate1.md, "The telemetry figures", "Recorded
# reversal, the reader."
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc_id, trace_section, row_session_id, flagged",
    [
        pytest.param("orphaned-trace", "## Search Trace\n\nshell\n", None, True,
                     id="shell-with-no-telemetry-row-at-all"),
        pytest.param("traced-story", "## Search Trace\n\nshell\n", "traced-story", False,
                     id="shell-with-a-row-naming-this-document"),
        pytest.param("shadowed-trace", "## Search Trace\n\nshell\n", "a-different-story", True,
                     id="shell-with-a-row-naming-another-document"),
        pytest.param("pasted-story", "## Search Trace\n\npaste\n", None, True,
                     id="paste-with-no-matching-row"),
        pytest.param("skipped-story", "## Search Trace\n\nmiss\n", None, False,
                     id="miss-has-nothing-to-reconstruct"),
        pytest.param(
            "honest-miss",
            "## Search Trace\n\nNo shell was available in this environment, so the search never "
            "ran.\nmiss\n",
            None, False, id="prose-naming-shell-above-a-real-miss-line",
        ),
        pytest.param("prose-story", "## Search Trace\n\nNo shell search was run; miss.\n",
                     None, False, id="one-prose-line-naming-shell-and-miss"),
        pytest.param(
            "mixed-story",
            "## Search Trace\n\nConsidered paste via the user, ended up miss -- shell was "
            "blocked.\n",
            None, False, id="one-prose-line-naming-paste-miss-and-shell",
        ),
        pytest.param(
            "real-shell", "## Search Trace\n\n(A miss would mean no search ran.)\nshell\n",
            None, True, id="prose-naming-miss-above-a-real-shell-line",
        ),
    ],
)
def test_the_cross_check_flags_a_shell_or_paste_trace_with_no_matching_telemetry_row(
    tmp_path, doc_id, trace_section, row_session_id, flagged,
):
    """The join is per document, not "are there any rows at all": a row naming some other
    session leaves this document's claim unevidenced. Both directions of the prose reader are
    here too -- a document that genuinely says `miss` must never be reported as claiming
    `shell`/`paste` because a nearby sentence uses those words, and a document that genuinely ran
    `shell` must still be flagged when an earlier sentence happens to mention `miss`."""
    sessions = tmp_path / "sessions"
    _doc(sessions, doc_id, body=PREREG + trace_section)
    if row_session_id is not None:
        _row(tmp_path / "telemetry.jsonl", session_id=row_session_id)

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == ([doc_id] if flagged else []), (
        "only an affirmative shell/paste claim with no telemetry row to show for it is flagged"
    )


def test_only_the_search_trace_section_can_make_a_provenance_claim(tmp_path):
    """The vocabulary belongs to `## Search Trace` and nowhere else. A bare `shell` line in a
    Decision Log is a note about a shell, not a claim that a search ran -- reading it as one
    would flag a document that never made the claim, and no telemetry row could ever clear it."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "shell-in-the-wrong-section",
         body=PREREG + "## Decision Log\n\nshell\n")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == [], (
        "the trace vocabulary is read out of the Search Trace section, not out of the document"
    )


@pytest.mark.parametrize(
    "trace_body, flagged",
    [
        pytest.param("miss\nshell\n", False, id="miss-first-wins-over-a-later-shell"),
        pytest.param("shell\nmiss\n", True, id="shell-first-wins-over-a-later-miss"),
    ],
)
def test_the_first_vocabulary_line_in_the_search_trace_is_the_claim_not_the_last(
    tmp_path, trace_body, flagged,
):
    """A Search Trace holding two vocabulary lines is ambiguous on its face; the reader resolves
    it by position, and the position is the first. Both orders are here because either one alone
    also passes for a reader that simply prefers `miss`, or simply prefers `shell`."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "two-line-trace", body=f"{PREREG}## Search Trace\n\n{trace_body}")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == (["two-line-trace"] if flagged else [])


# ---------------------------------------------------------------------------
# caveat 1 -- orphan telemetry rows: session_id names no document IN THIS STORE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "session_ids, expected_ids, expected_row_count",
    [
        pytest.param(["story-a"], [], 0, id="every-row-names-a-document-here"),
        pytest.param(["story-a", "ghost-id", "ghost-id"], ["ghost-id"], 2,
                     id="two-rows-name-a-document-that-is-not-here"),
    ],
)
def test_orphan_session_ids_and_row_count_join_against_this_stores_doc_ids(
    tmp_path, session_ids, expected_ids, expected_row_count,
):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    for session_id in session_ids:
        _row(jsonl, session_id=session_id)

    report = _evaluate(tmp_path)

    assert report.orphan_session_ids == expected_ids
    assert report.orphan_row_count == expected_row_count


# ---------------------------------------------------------------------------
# caveat 2 -- the time window: default is all-time (nothing filtered unless asked), and the
# window scopes every telemetry-derived figure, including the cross-check
# ---------------------------------------------------------------------------


def test_default_window_is_all_time_and_does_not_filter_by_ts(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    _row(tmp_path / "telemetry.jsonl", session_id="story-a", ts="2020-01-01T00:00:00+00:00")

    report = _evaluate(tmp_path, since=None)

    assert report.window_label == "all-time"
    assert report.telemetry_rows_with_session == 1, "a very old row is still counted by default"


def test_since_filters_out_rows_before_the_window(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    _row(jsonl, session_id="story-a", ts="2026-08-01T00:00:00+00:00")
    _row(jsonl, session_id="story-a", ts="2026-08-20T00:00:00+00:00")

    report = _evaluate(tmp_path, since="2026-08-10")

    assert report.telemetry_rows_with_session == 1
    assert report.telemetry_distinct_sessions == 1


def test_the_since_bound_is_inclusive_a_row_exactly_on_it_is_inside_the_window(tmp_path):
    """`--since` means "at or after it" (gate1_report.py's own help text) and a bare date
    anchors to midnight UTC, so the row exactly on the bound is in. No other fixture sits on
    the bound, so an exclusive `>` would silently drop each window's first row unnoticed."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    _row(jsonl, session_id="story-a", ts="2026-08-10T00:00:00+00:00")
    _row(jsonl, session_id="story-a", ts="2026-08-09T23:59:59+00:00")

    report = _evaluate(tmp_path, since="2026-08-10")

    assert report.telemetry_rows_with_session == 1, (
        "the row on the bound is in; the row one second before it is out"
    )


def test_since_window_also_scopes_the_unreconstructable_cross_check(tmp_path):
    """A row that happened before the window must not silently cover a document -- the window
    is the scope of the whole audit, not just the row-count line."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a", body=PREREG + "## Search Trace\n\nshell\n")
    _row(tmp_path / "telemetry.jsonl", session_id="story-a", ts="2026-01-01T00:00:00+00:00")

    report = _evaluate(tmp_path, since="2026-08-01")

    assert report.unreconstructable_docs == ["story-a"], (
        "the only covering row falls outside the window, so the document is unreconstructable "
        "within it"
    )


def test_an_unparseable_since_value_falls_back_to_all_time_without_crashing(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    _row(tmp_path / "telemetry.jsonl", session_id="story-a", ts="2020-01-01T00:00:00+00:00")

    report = _evaluate(tmp_path, since="not-a-real-date")

    assert report.since_error is not None
    assert report.window_label.startswith("all-time")
    assert report.telemetry_rows_with_session == 1, "falls back to counting everything"


@pytest.mark.parametrize(
    "since, expected_rows",
    [
        pytest.param(None, 1, id="default-all-time-window-counts-it"),
        pytest.param("2026-08-01", 0, id="a-requested-window-excludes-it"),
    ],
)
def test_a_row_with_an_unparseable_ts_counts_all_time_but_never_inside_a_window(
    tmp_path, since, expected_rows,
):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    _row(tmp_path / "telemetry.jsonl", session_id="story-a", ts="not-a-timestamp")

    report = _evaluate(tmp_path, since=since)

    assert report.telemetry_rows_with_session == expected_rows, (
        "a row whose ts cannot be read must not silently count toward a scoped window"
    )


# ---------------------------------------------------------------------------
# "a session document" is a file `load_store` successfully parses into a `Doc`; a document
# that errors out during parsing is not a session artifact for this block
# ---------------------------------------------------------------------------


def test_a_document_that_fails_to_parse_is_not_counted_in_any_status_figure(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "good-story", status="active", body=PREREG)
    (sessions / "duplicate-id.md").write_text(
        "---\nid: good-story\ntitle: dup\ndate: 2026-08-01\nstatus: active\n"
        "tags: []\nentities: []\nrelated: []\n---\n\n## Pre-reg\n\nBaseline.\n",
        encoding="utf-8",
    )

    report = _evaluate(tmp_path)

    assert report.draft_count + report.active_count + report.superseded_count == 1, (
        "the duplicate-id file is a load error, not a second session document"
    )


# ---------------------------------------------------------------------------
# telemetry.jsonl unreadable or undecodable must not raise out of `evaluate()`.
# `gate1_report.py` exits 0 unconditionally; a torn append or a permissions problem on
# telemetry.jsonl (written by two concurrent processes, cli.py and mcp_server.py) must degrade
# to "telemetry-derived figures unmeasured," never to a crash that also takes the per-row table
# and the §11 endpoint figure with it.
# ---------------------------------------------------------------------------


def _write_undecodable_bytes(jsonl: Path) -> None:
    with open(jsonl, "wb") as f:
        f.write(b"\xff\xfe not valid utf-8\n")


def _deny_read_permission(jsonl: Path) -> None:
    jsonl.write_text('{"session_id": "story-a"}\n', encoding="utf-8")
    os.chmod(jsonl, 0o000)


@pytest.mark.parametrize(
    "break_telemetry",
    [
        pytest.param(_write_undecodable_bytes, id="undecodable-bytes"),
        pytest.param(_deny_read_permission, id="unreadable-file",
                     marks=requires_permission_enforcement),
    ],
)
def test_a_telemetry_file_that_cannot_be_read_sets_telemetry_error_instead_of_raising(
    tmp_path, break_telemetry,
):
    sessions = tmp_path / "sessions"
    # the Search Trace makes the cross-check's own silence observable: computed against the
    # empty read rather than skipped, `unreconstructable_docs` would name this document
    _doc(sessions, "story-a", body=PREREG + "## Search Trace\n\nshell\n")
    jsonl = tmp_path / "telemetry.jsonl"
    break_telemetry(jsonl)
    try:
        report = _evaluate(tmp_path)
    finally:
        os.chmod(jsonl, 0o644)

    assert report.telemetry_error is not None
    assert report.telemetry_rows_with_session == 0
    assert report.telemetry_distinct_sessions == 0
    assert report.orphan_session_ids == []
    assert report.orphan_row_count == 0
    assert report.unreconstructable_docs == []


def test_a_telemetry_error_does_not_affect_non_telemetry_figures(tmp_path):
    """Document status counts and section-presence figures never touch telemetry.jsonl -- an
    unreadable telemetry file must not zero them out too."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "active-story", body=PREREG + "## Reuse Log\n\nPrior docs used: none.")
    _doc(sessions, "draft-story", status="draft", body="## Pre-reg\n\nBaseline.")
    _write_undecodable_bytes(tmp_path / "telemetry.jsonl")

    report = _evaluate(tmp_path)

    assert report.telemetry_error is not None
    assert report.draft_count == 1
    assert report.active_count == 1
    assert report.none_report_count == 1
    assert report.reuse_log_count == 1


def test_a_missing_telemetry_file_is_not_an_error(tmp_path):
    """A missing file is `summarize()`'s and `read_session_rows`'s own "zero rows," never an
    error -- `telemetry_error` must stay `None` (not merely non-crashing) for this case."""
    _doc(tmp_path / "sessions", "story-a")

    report = _evaluate(tmp_path)

    assert report.telemetry_error is None
    assert report.telemetry_rows_with_session == 0


# ---------------------------------------------------------------------------
# a `backfilled: true` document never ran the ritual (ENGMEM-SPEC.md §4: "docs
# written after the fact"); it is excluded from the ritual figures and the missing-section
# lists, and counted on its own.
# ---------------------------------------------------------------------------


def test_backfilled_documents_are_excluded_from_the_ritual_figures(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "ritual-story", status="active", body=PREREG)
    _doc(sessions, "backfilled-note", status="active", backfilled=True, body=PREREG)
    _doc(sessions, "backfilled-draft", status="draft", backfilled=True, body=PREREG)

    report = _evaluate(tmp_path)

    assert report.active_count == 1, "the backfilled active document must not inflate this"
    assert report.draft_count == 0, "the backfilled draft must not inflate this either"
    assert report.backfilled_count == 2, "a non-backfilled document never lands on this figure"


def test_backfilled_documents_are_excluded_from_the_missing_section_lists(tmp_path):
    """The fixture carries a Pre-reg section on purpose: without it the document would
    be excluded by the Pre-reg half of the rule, and this test would stop pinning the backfilled
    half it exists for -- passing even with the `backfilled` exclusion deleted outright."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "backfilled-note", status="active", backfilled=True,
         body=PREREG + "## Decision Log\n\nWritten after the fact, no Reuse Log, no Search Trace.")

    report = _evaluate(tmp_path)

    assert report.active_missing_reuse == [], "a backfilled document was never asked to run the ritual"
    assert report.active_missing_trace == []
    assert report.backfilled_count == 1


# ---------------------------------------------------------------------------
# A document with no Pre-reg section never ran the ritual either -- the save template gained
# `## Pre-reg` after the store's earliest documents were written, and the absence of
# `backfilled: true` on them says nothing to the contrary. Same exclusion as backfilled, from the
# same population, with the missing section recognised directly rather than through a flag
# somebody has to remember to stamp. ENGMEM-SPEC.md section 11, amendment of 2026-09-12.
# ---------------------------------------------------------------------------


def test_a_document_with_no_prereg_section_is_not_a_ritual_document(tmp_path):
    """A document written before the Pre-reg section existed carries no flag to say so, and
    counting it as a ritual start inflates both sides of the ritual figure."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "20260101-widget-cache", status="active",
         body=PREREG + "## Reuse Log\n\nPrior docs used: none.\n\n## Search Trace\n\nmiss\n")
    _doc(sessions, "20250101-pre-ritual-note", status="active", backfilled=False,
         body="## Decision Log\n\nWritten before the Pre-reg section existed.")

    report = _evaluate(tmp_path)

    assert report.active_count == 1, "the Pre-reg-less document must not inflate the ritual figure"
    assert report.draft_count == 0
    assert report.superseded_count == 0
    assert report.no_prereg_docs == ["20250101-pre-ritual-note"], (
        "counted and named on its own figure -- the coverage block exists to show what the "
        "sample is made of, so this document may never be silently dropped"
    )
    assert report.backfilled_count == 0, "it carries no flag; the new figure is what catches it"


def test_a_document_with_no_prereg_section_is_not_listed_as_missing_other_sections(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "20250101-pre-ritual-note", status="active",
         body="## Decision Log\n\nNo ritual structure at all.")

    report = _evaluate(tmp_path)

    assert report.active_missing_reuse == [], (
        "a document outside the ritual population is not a document that failed the ritual"
    )
    assert report.active_missing_trace == []
    assert report.no_prereg_docs == ["20250101-pre-ritual-note"]


@pytest.mark.parametrize(
    "backfilled, body, expected_no_prereg, expected_backfilled_count",
    [
        pytest.param(False, "## Search Trace\n\nshell\n", ["20250101-pre-ritual-note"], 0,
                     id="outside-the-ritual-for-having-no-prereg-section"),
        pytest.param(True, PREREG + "## Search Trace\n\nshell\n", [], 1,
                     id="outside-the-ritual-for-being-backfilled"),
    ],
)
def test_a_document_outside_the_ritual_population_still_answers_for_its_shell_trace(
    tmp_path, backfilled, body, expected_no_prereg, expected_backfilled_count,
):
    """Leaving the ritual population is not leaving the provenance cross-check. That figure
    fires on an affirmative claim a document makes about itself, and the claim is owed evidence
    whoever made it -- the exclusions above are about the ritual, not about honesty."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "20250101-pre-ritual-note", backfilled=backfilled, body=body)

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == ["20250101-pre-ritual-note"]
    assert report.no_prereg_docs == expected_no_prereg
    assert report.backfilled_count == expected_backfilled_count
    assert report.active_count == 0, "still outside the ritual figures"


def test_a_prereg_less_document_of_any_status_is_counted_on_the_new_figure(tmp_path):
    """The figure is scoped to the whole store, not to `active`: a draft or a superseded document
    written before the ritual existed is the same fact about the sample."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "20250101-pre-ritual-draft", status="draft", body="## Decision Log\n\nOld.")
    _doc(sessions, "20250102-pre-ritual-old", status="superseded", body="## Decision Log\n\nOld.")

    report = _evaluate(tmp_path)

    assert report.no_prereg_docs == ["20250101-pre-ritual-draft", "20250102-pre-ritual-old"]
    assert report.draft_count == 0
    assert report.superseded_count == 0


def test_a_backfilled_document_with_no_prereg_section_is_counted_once_as_backfilled(tmp_path):
    """Precedence: the two exclusions overlap, and a document counted on both lines would read
    as two missing documents."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "20250101-pre-ritual-note", status="active", backfilled=True,
         body="## Decision Log\n\nWritten after the fact, no Pre-reg section either.")

    report = _evaluate(tmp_path)

    assert report.backfilled_count == 1
    assert report.no_prereg_docs == [], "already accounted for under the backfilled figure"
    assert report.active_count == 0
