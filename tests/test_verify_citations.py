"""`tools/verify_citations.py` — the check the save template already promises.

`engmem.save.md`'s Reuse Log rules say a quote "is checked mechanically against the cited
file, so an approximate quote reads as a fabricated one." Nothing performed that check, so
the sentence was a claim about a tool that did not exist — in the layer the project calls
its product. These tests are the check's contract.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

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


def _run(store: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--store", str(store)],
        capture_output=True, text=True, timeout=30,
    )


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
    """The failure this exists to catch: a quote that reads plausibly but was never
    written in the document it is attributed to."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot, not lazily.")
    _doc(sessions, "widget-cache-v2", _cited("Eviction is performed lazily on first read."))

    result = _run(tmp_path)

    assert result.returncode != 0
    assert "widget-cache-v2.md" in result.stdout
    assert "widget-cache-v1" in result.stdout, "the report must name the document cited"


def test_whitespace_and_line_wrapping_do_not_count_as_a_mismatch(tmp_path):
    """A quote copied out of a wrapped paragraph carries a newline where the reader saw a
    space. Failing on that would train people to ignore the checker."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot,\nnot lazily.")
    _doc(sessions, "widget-cache-v2", _cited("Eviction runs on boot, not lazily."))

    assert _run(tmp_path).returncode == 0


def test_a_row_with_no_detectable_quote_is_reported(tmp_path):
    """The template forbids a quoteless row outright. Undelimited prose cannot be checked,
    so it is reported rather than silently accepted."""
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
    """The quote is genuine, so this is not a fabrication — it is the other way a Reuse
    Log row goes wrong: real reuse of knowledge that has since been replaced. After the
    session ends the two are indistinguishable, so it has to be surfaced here."""
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
    """Exit code means "a citation could not be verified". This one verified fine. Making
    it fail too would blur the two, and a checker that cries about both is one people stop
    running."""
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

    assert "0 unverifiable" in out
    assert "1 " in out and "superseded" in out



def test_the_report_is_printable_on_a_windows_console(tmp_path):
    """Windows writes stdout in the locale code page, not UTF-8. A character outside it
    raises `UnicodeEncodeError` inside the tool — so the run fails with no report at all,
    which is the least useful way for a checker to break."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "widget-cache-v2", _cited("Never written anywhere."))

    output = _run(tmp_path).stdout
    assert "quote not found" in output, "the case under test must actually report something"

    output.encode("cp1252")  # raises if a character cannot reach a Windows console
