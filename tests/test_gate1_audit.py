"""Unit tests for the sample-completeness audit -- a different question from `gate1.py`'s
per-row verdicts (is the ritual's experimental record complete, not is any one citation valid).
Rationale: contracts/gate1.md, "The audit coverage block."""

from __future__ import annotations

import json
import os
from pathlib import Path

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


# the template's own header row + separator, with zero data rows -- `templates/engmem.save.md`
# lines 162-163 -- the exact shape `gate1.evaluate()` produces neither a row, a none-report, nor
# a conflict for (ARCH-102)
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
# ARCH-102 -- a `## Reuse Log` holding only the template's own header row + separator is
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

    assert report.active_missing_reuse == ["bare-story"]
    assert report.active_missing_trace == ["bare-story"]


def test_a_drafts_missing_sections_are_not_counted_active_only_is_the_population(tmp_path):
    """A draft has not reached the save ritual yet -- Pre-reg is its only expected section
    (templates/engmem.start.md step 2); flagging it here would be noise, not a finding."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "abandoned-draft", status="draft", body="## Pre-reg\n\nBaseline.")

    report = _evaluate(tmp_path)

    assert report.active_missing_reuse == []
    assert report.active_missing_trace == []


def test_nothing_missing_produces_empty_lists(tmp_path):
    sessions = tmp_path / "sessions"
    body = (
        "## Pre-reg\n\nBaseline.\n\n"
        "## Reuse Log\n\nPrior docs used: none.\n\n"
        "## Search Trace\n\nshell\n"
    )
    _doc(sessions, "complete-story", body=body)

    report = _evaluate(tmp_path)

    assert report.active_missing_reuse == []
    assert report.active_missing_trace == []
    assert report.no_prereg_docs == []


# ---------------------------------------------------------------------------
# defect (c) -- the free cross-check: a document whose Search Trace says shell/paste but for
# which zero telemetry rows carry its id as session_id
# ---------------------------------------------------------------------------


def test_a_shell_trace_with_no_matching_telemetry_row_is_unreconstructable(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "orphaned-trace", body=PREREG + "## Search Trace\n\nshell\n")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == ["orphaned-trace"]


def test_a_shell_trace_with_a_matching_row_is_not_flagged(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "traced-story", body=PREREG + "## Search Trace\n\nshell\n")
    _row(tmp_path / "telemetry.jsonl", session_id="traced-story")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == []


def test_a_miss_trace_with_no_telemetry_row_is_not_flagged(tmp_path):
    """`miss` means the search never ran at all -- there is nothing to reconstruct, so this is
    not the gap the cross-check exists to find."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "skipped-story", body=PREREG + "## Search Trace\n\nmiss\n")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == []


def test_a_paste_trace_with_no_matching_row_is_also_flagged(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "pasted-story", body=PREREG + "## Search Trace\n\npaste\n")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == ["pasted-story"]


# ---------------------------------------------------------------------------
# ARCH-107 -- `_trace_value` must never read a provenance claim out of ordinary prose. The
# ARCH-106 word-by-word loosening (reverted) matched the FIRST recognized token anywhere in the
# section, so a sentence merely mentioning "shell" or "miss" could flip the result in either
# direction. The exact whole-line match restored here has no such route: a line is either
# EXACTLY `shell`/`paste`/`miss` (after stripping only a trailing `.`/`:`), or it names nothing.
# ---------------------------------------------------------------------------


def test_a_prose_line_mentioning_shell_around_a_real_miss_line_is_not_read_as_shell(tmp_path):
    """False positive direction: a document that genuinely says `miss` must never be reported as
    claiming `shell`/`paste` because a nearby sentence happens to use those words."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "honest-miss", body=(
        PREREG + "## Search Trace\n\nNo shell was available in this environment, so the search never "
        "ran.\nmiss\n"
    ))

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == [], "a genuine miss must never register as shell/paste"


def test_a_single_prose_line_naming_both_shell_and_miss_resolves_to_neither(tmp_path):
    """The line itself is not `miss` either -- prose mentioning the word is not the value."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "prose-story", body=PREREG + "## Search Trace\n\nNo shell search was run; miss.\n")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == []


def test_a_prose_line_naming_two_different_vocabulary_words_resolves_to_neither(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "mixed-story", body=(
        PREREG + "## Search Trace\n\nConsidered paste via the user, ended up miss -- shell was "
        "blocked.\n"
    ))

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == []


def test_a_prose_line_mentioning_miss_before_a_real_shell_line_still_flags_it(tmp_path):
    """False negative direction: a document that genuinely ran `shell` must still be flagged even
    when an earlier sentence happens to mention `miss`."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "real-shell", body=PREREG + "## Search Trace\n\n(A miss would mean no search ran.)\nshell\n")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == ["real-shell"]


def test_a_prose_line_mentioning_miss_before_a_real_shell_line_variant(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "real-shell-2", body=PREREG + "## Search Trace\n\nNot a miss this time.\nshell\n")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == ["real-shell-2"]


# ---------------------------------------------------------------------------
# caveat 1 -- orphan telemetry rows: session_id names no document IN THIS STORE
# ---------------------------------------------------------------------------


def test_orphan_session_ids_and_row_count_join_against_this_stores_doc_ids(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    _row(jsonl, session_id="story-a")
    _row(jsonl, session_id="ghost-id")
    _row(jsonl, session_id="ghost-id")

    report = _evaluate(tmp_path)

    assert report.orphan_session_ids == ["ghost-id"]
    assert report.orphan_row_count == 2


def test_a_session_id_matching_a_real_document_is_not_an_orphan(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    _row(tmp_path / "telemetry.jsonl", session_id="story-a")

    report = _evaluate(tmp_path)

    assert report.orphan_session_ids == []
    assert report.orphan_row_count == 0


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


def test_a_row_with_an_unparseable_ts_is_still_counted_under_the_default_all_time_window(
    tmp_path,
):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": "not-a-timestamp", "query": "q", "session_id": "story-a", "channel": "cli",
            "n_docs": 1, "hits": [], "surfaced": [], "result": "miss",
        }) + "\n")

    report = _evaluate(tmp_path, since=None)

    assert report.telemetry_rows_with_session == 1


def test_a_row_with_an_unparseable_ts_is_excluded_once_a_window_is_requested(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": "not-a-timestamp", "query": "q", "session_id": "story-a", "channel": "cli",
            "n_docs": 1, "hits": [], "surfaced": [], "result": "miss",
        }) + "\n")

    report = _evaluate(tmp_path, since="2026-08-01")

    assert report.telemetry_rows_with_session == 0, (
        "a row whose ts cannot be read must not silently count toward a scoped window"
    )


# ---------------------------------------------------------------------------
# design decision 4 -- "a session document" is a file `load_store` successfully parses into a
# `Doc`; a document that errors out during parsing is not a session artifact for this block
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
# ARCH-101 -- telemetry.jsonl unreadable or undecodable must not raise out of `evaluate()`.
# `gate1_report.py` exits 0 unconditionally; a torn append or a permissions problem on
# telemetry.jsonl (written by two concurrent processes, cli.py and mcp_server.py) must degrade
# to "telemetry-derived figures unmeasured," never to a crash that also takes the per-row table
# and the §11 endpoint figure with it.
# ---------------------------------------------------------------------------


def test_an_undecodable_telemetry_file_sets_telemetry_error_instead_of_raising(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl, "wb") as f:
        f.write(b"\xff\xfe not valid utf-8\n")

    report = _evaluate(tmp_path)

    assert report.telemetry_error is not None
    assert report.telemetry_rows_with_session == 0
    assert report.telemetry_distinct_sessions == 0
    assert report.orphan_session_ids == []
    assert report.orphan_row_count == 0
    assert report.unreconstructable_docs == []


@requires_permission_enforcement
def test_an_unreadable_telemetry_file_sets_telemetry_error_instead_of_raising(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "story-a")
    jsonl = tmp_path / "telemetry.jsonl"
    jsonl.write_text('{"session_id": "story-a"}\n', encoding="utf-8")
    os.chmod(jsonl, 0o000)
    try:
        report = _evaluate(tmp_path)
    finally:
        os.chmod(jsonl, 0o644)

    assert report.telemetry_error is not None
    assert report.telemetry_rows_with_session == 0


def test_a_telemetry_error_does_not_affect_non_telemetry_figures(tmp_path):
    """Document status counts and section-presence figures never touch telemetry.jsonl -- an
    unreadable telemetry file must not zero them out too."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "active-story", body=PREREG + "## Reuse Log\n\nPrior docs used: none.")
    _doc(sessions, "draft-story", status="draft", body="## Pre-reg\n\nBaseline.")
    jsonl = tmp_path / "telemetry.jsonl"
    with open(jsonl, "wb") as f:
        f.write(b"\xff\xfe not valid utf-8\n")

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
# ARCH-103 -- a `backfilled: true` document never ran the ritual (ENGMEM-SPEC.md §4: "docs
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
    assert report.backfilled_count == 2


def test_backfilled_documents_are_excluded_from_the_missing_section_lists(tmp_path):
    """The fixture carries a Pre-reg section on purpose (ARCH-003): without it the document would
    be excluded by the Pre-reg half of the rule, and this test would stop pinning the backfilled
    half it exists for -- passing even with the `backfilled` exclusion deleted outright."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "backfilled-note", status="active", backfilled=True,
         body=PREREG + "## Decision Log\n\nWritten after the fact, no Reuse Log, no Search Trace.")

    report = _evaluate(tmp_path)

    assert report.active_missing_reuse == [], "a backfilled document was never asked to run the ritual"
    assert report.active_missing_trace == []
    assert report.backfilled_count == 1


def test_a_non_backfilled_document_is_unaffected_by_the_backfilled_exclusion(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "ritual-story", status="active", backfilled=False,
         body=PREREG + "## Decision Log\n\nNo other ritual sections.")

    report = _evaluate(tmp_path)

    assert report.backfilled_count == 0
    assert report.active_missing_reuse == ["ritual-story"]


# ---------------------------------------------------------------------------
# A document with no Pre-reg section never ran the ritual either -- the save template gained
# `## Pre-reg` after the store's earliest documents were written, and the absence of
# `backfilled: true` on them says nothing to the contrary. Same exclusion as backfilled, from the
# same population, with the missing section recognised directly rather than through a flag
# somebody has to remember to stamp. ENGMEM-SPEC.md section 11, amendment of 2026-09-12.
# ---------------------------------------------------------------------------


def test_a_document_with_no_prereg_section_is_not_a_ritual_document(tmp_path):
    """The live defect: 7 pre-ritual documents carrying no flag were counted as ritual starts,
    reading the countable-story figure as 14 where the honest number was 3."""
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


def test_a_prereg_less_document_with_a_shell_trace_is_still_on_the_cross_check(tmp_path):
    """ARCH-001: leaving the ritual population is not leaving the provenance cross-check. That
    figure fires on an affirmative claim a document makes about itself, and the claim is owed
    evidence whoever made it -- the exclusions above are about the ritual, not about honesty."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "20250101-pre-ritual-note", body="## Search Trace\n\nshell\n")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == ["20250101-pre-ritual-note"]
    assert report.no_prereg_docs == ["20250101-pre-ritual-note"], (
        "counted outside the ritual population and still answerable for its own claim"
    )


def test_a_backfilled_document_with_a_shell_trace_is_still_on_the_cross_check(tmp_path):
    """ARCH-001, the other half: a backfilled document that claims a search ran and has no
    telemetry row to show for it is exactly what this figure exists to name."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "20250101-pre-ritual-note", backfilled=True,
         body=PREREG + "## Search Trace\n\nshell\n")

    report = _evaluate(tmp_path)

    assert report.unreconstructable_docs == ["20250101-pre-ritual-note"]
    assert report.backfilled_count == 1
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
    """Precedence: the two exclusions overlap on exactly the documents the user stamped by hand
    on 2026-09-11, and a document counted on both lines would read as two missing documents."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "20250101-pre-ritual-note", status="active", backfilled=True,
         body="## Decision Log\n\nWritten after the fact, no Pre-reg section either.")

    report = _evaluate(tmp_path)

    assert report.backfilled_count == 1
    assert report.no_prereg_docs == [], "already accounted for under the backfilled figure"
    assert report.active_count == 0

