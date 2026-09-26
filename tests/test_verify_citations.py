"""The save template promised a mechanical quote check that nothing performed; these tests are its
contract."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import run_tool


TOOL = Path(__file__).resolve().parent.parent / "tools" / "verify_citations.py"


def _doc(sessions: Path, doc_id: str, body: str, *, status: str = "active",
         superseded_by: str = "") -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{doc_id}.md").write_text(
        f"---\nid: {doc_id}\ntitle: Widget cache\ndate: 2026-08-01\nstatus: {status}\n"
        f"superseded_by: {superseded_by}\n"
        f"tags: [platform]\nentities: [WidgetCache]\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _run(store: Path, env: dict[str, str] | None = None) -> "subprocess.CompletedProcess[str]":
    return run_tool(TOOL, store, env)


def _cited(quote: str) -> str:
    return (
        "## 16. Reuse Log\n\n"
        "| prior-doc | taken | impact | classification |\n"
        "|---|---|---|---|\n"
        f"| widget-cache-v1 | `WidgetCache.flush()` — \"{quote}\" | reused the shape | reuse |\n"
    )


def test_a_quote_that_is_in_the_cited_document_passes(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot, not lazily.")
    _doc(sessions, "widget-cache-v2", _cited("Eviction runs on boot, not lazily."))

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout
    assert "widget-cache-v2" not in result.stdout


def test_a_quote_that_is_not_in_the_cited_document_is_reported(tmp_path):
    """The failure this exists to catch: a quote that reads plausibly but was never written where
    it is attributed."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot, not lazily.")
    _doc(sessions, "widget-cache-v2", _cited("Eviction is performed lazily on first read."))

    result = _run(tmp_path)

    assert result.returncode != 0
    assert "widget-cache-v2.md" in result.stdout
    assert "widget-cache-v1" in result.stdout, "the report must name the document cited"


def test_whitespace_and_line_wrapping_do_not_count_as_a_mismatch(tmp_path):
    """A quote copied from a wrapped paragraph carries a newline where the reader saw a space."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot,\nnot lazily.")
    _doc(sessions, "widget-cache-v2", _cited("Eviction runs on boot, not lazily."))

    assert _run(tmp_path).returncode == 0


def test_a_row_with_no_detectable_quote_is_reported(tmp_path):
    """Undelimited prose cannot be checked, so a quoteless row is reported rather than silently
    accepted."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.")
    _doc(
        sessions,
        "widget-cache-v2",
        "## 16. Reuse Log\n\n| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        "| widget-cache-v1 | reused the flush shape | saved a round | reuse |\n",
    )

    result = _run(tmp_path)

    assert result.returncode != 0
    assert "no quote" in result.stdout.lower()


def test_prior_docs_used_none_is_clean(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v2", "## 16. Reuse Log\n\nPrior docs used: none.")

    assert _run(tmp_path).returncode == 0


def test_a_cited_document_that_is_not_in_the_store_is_reported(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v2", _cited("Eviction runs on boot."))

    result = _run(tmp_path)

    assert result.returncode != 0
    assert "widget-cache-v1" in result.stdout


def test_a_clean_store_says_so_on_stdout(tmp_path):
    """Silence on success would be indistinguishable from the tool failing to run."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v2", "## 16. Reuse Log\n\nPrior docs used: none.")

    result = _run(tmp_path)

    assert result.stdout.strip(), "a clean run must still report that it ran"
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# citing a document that has since been superseded
# ---------------------------------------------------------------------------


def test_a_row_citing_a_superseded_document_is_flagged_with_its_successor(tmp_path):
    """The quote is genuine — this is real reuse of knowledge since replaced, indistinguishable
    once the session ends."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.",
         status="superseded", superseded_by="widget-cache-v2")
    _doc(sessions, "widget-cache-v2", "## 8. Decision Log\n\nEviction is lazy now.")
    _doc(sessions, "sweeper-job", _cited("Eviction runs on boot."))

    result = _run(tmp_path)

    assert "widget-cache-v1" in result.stdout
    assert "widget-cache-v2" in result.stdout, "the successor must be named"
    assert "superseded" in result.stdout.lower()


def test_a_superseded_citation_alone_does_not_fail_the_run(tmp_path):
    """This citation verified fine, and a checker that cries about both kinds of problem is one
    people stop running."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.",
         status="superseded", superseded_by="widget-cache-v2")
    _doc(sessions, "widget-cache-v2", "## 8. Decision Log\n\nEviction is lazy now.")
    _doc(sessions, "sweeper-job", _cited("Eviction runs on boot."))

    assert _run(tmp_path).returncode == 0


def test_a_bad_quote_still_fails_even_alongside_a_superseded_citation(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.",
         status="superseded", superseded_by="widget-cache-v2")
    _doc(sessions, "widget-cache-v2", "## 8. Decision Log\n\nEviction is lazy now.")
    _doc(sessions, "sweeper-job", _cited("Something never written anywhere."))

    result = _run(tmp_path)

    assert result.returncode != 0
    assert "quote not found" in result.stdout


def test_the_footer_counts_the_two_kinds_separately(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.",
         status="superseded", superseded_by="widget-cache-v2")
    _doc(sessions, "widget-cache-v2", "## 8. Decision Log\n\nEviction is lazy now.")
    _doc(sessions, "sweeper-job", _cited("Eviction runs on boot."))

    out = _run(tmp_path).stdout

    assert "0 unverifiable citation(s), 1 citing a superseded document" in out



# ---------------------------------------------------------------------------
# "Prior docs used: none." conflicting with real rows -- reported, never silently resolved
# ---------------------------------------------------------------------------


def test_a_conflicting_reuse_log_is_named_and_its_rows_are_still_checked(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.")
    body = (
        "## 16. Reuse Log\n\nPrior docs used: none.\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        '| widget-cache-v1 | `WidgetCache.flush()` -- "Eviction runs on boot." | reused the shape | reuse |\n'
    )
    _doc(sessions, "widget-cache-v2", body)

    result = _run(tmp_path)

    assert result.returncode == 0, "the row inside the conflict verified fine"
    assert "widget-cache-v2: Reuse Log has both" in result.stdout
    assert "1 with a conflicting Reuse Log" in result.stdout


def test_a_bad_quote_inside_a_conflicting_reuse_log_still_fails_the_run(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.")
    body = (
        "## 16. Reuse Log\n\nPrior docs used: none.\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        '| widget-cache-v1 | `WidgetCache.flush()` -- "Something never written anywhere." | reused the shape | reuse |\n'
    )
    _doc(sessions, "widget-cache-v2", body)

    result = _run(tmp_path)

    assert result.returncode != 0, "a conflict must not mask a real unverifiable quote"
    assert "quote not found" in result.stdout


# ---------------------------------------------------------------------------
# a draft or superseded citing document's own citations are still checked -- the exclusion in
# gate1_report.py's counted population does not extend to this tool's mechanical check
# ---------------------------------------------------------------------------


def test_a_drafts_own_citations_are_still_checked(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "widget-cache-v2", _cited("Something never written anywhere."), status="draft")

    result = _run(tmp_path)

    assert result.returncode != 0, "a fabricated quote must be caught before the draft goes active"
    assert "quote not found" in result.stdout


# ---------------------------------------------------------------------------
# classification and dogfooding are gate1_report.py's concerns -- this tool's exit condition
# does not grow to include them
# ---------------------------------------------------------------------------


def test_a_harmful_classification_does_not_fail_the_run(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.")
    body = (
        "## 16. Reuse Log\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        '| widget-cache-v1 | `x` -- "Eviction runs on boot." | led astray | harmful |\n'
    )
    _doc(sessions, "widget-cache-v2", body)

    assert _run(tmp_path).returncode == 0, "a real, verified quote -- classification is not this tool's job"


def test_the_report_is_printable_on_a_windows_console(tmp_path):
    """Windows writes stdout in the locale code page, so a character outside it fails the run with
    no report at all."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "widget-cache-v2", _cited("Never written anywhere."))

    output = _run(tmp_path).stdout
    assert "quote not found" in output, "the case under test must actually report something"

    output.encode("cp1252")  # raises if a character cannot reach a Windows console


def _broken(sessions: Path, doc_id: str, body: str) -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{doc_id}.md").write_text(
        f"---\nid: {doc_id}\ntitle: Widget cache\ntags: [platform\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_a_store_with_no_sessions_directory_fails_loudly(tmp_path):
    """A typo'd --store used to print "0 unverifiable citation(s)" and exit 0 -- a clean pass for
    a store that was never read."""
    result = _run(tmp_path / "no-such-store")

    assert result.returncode == 1
    first = result.stdout.splitlines()[0]
    assert first.startswith("error: ") and "unknown, not zero" in first, result.stdout


def test_a_missing_store_fails_loudly_on_a_legacy_console_code_page(tmp_path):
    """The error line carries an em dash cp437 cannot encode; it must still reach stdout."""
    result = _run(tmp_path / "no-such-store", env={"PYTHONIOENCODING": "cp437"})

    assert result.returncode == 1, result.stderr
    first = result.stdout.splitlines()[0]
    assert first.startswith("error: ") and "unknown, not zero" in first, result.stdout


def test_a_citing_document_that_fails_to_load_fails_the_run_by_name(tmp_path):
    """Its fabricated quote cannot be checked, so the run cannot pass; before, the document simply
    vanished from the count."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot, not lazily.")
    _broken(sessions, "widget-cache-v2", _cited("Eviction is performed lazily on first read."))

    result = _run(tmp_path)

    assert result.returncode == 1
    assert result.stdout.startswith("error: widget-cache-v2.md: "), result.stdout
    assert "verify_citations: 0 unverifiable citation(s)" in result.stdout


def test_a_duplicate_id_fails_the_run_by_name(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot, not lazily.")
    (sessions / "widget-cache-copy.md").write_bytes((sessions / "widget-cache-v1.md").read_bytes())

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "error: duplicate id 'widget-cache-v1'" in result.stdout
