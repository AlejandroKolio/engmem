"""`tools/baseline_cost.py` — side A of the search-versus-self-grep baseline.

Side A is mechanical: run the query, measure what the agent would receive, check whether
the document the author knows to be right came back. Side B needs a fresh agent session
with no prior knowledge of the corpus, so the tool leaves that column empty rather than
inventing it — the same division of labour as `gate1_report.py`.

Both sides must be counted with `telemetry.estimate_tokens`, or the comparison is between
two different rulers.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "baseline_cost.py"


def _doc(sessions: Path, doc_id: str, entities: str, body: str) -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{doc_id}.md").write_text(
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-08-01\nstatus: active\n"
        f"tags: [platform]\nentities: [{entities}]\n---\n\n## 8. Decision Log\n\n{body}\n",
        encoding="utf-8",
    )


def _run(store: Path, queries: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--store", str(store), "--queries", str(queries)],
        capture_output=True, text=True, timeout=60,
    )


def _queries(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "queries.tsv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_query_whose_expected_document_is_returned_counts_as_found(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "WidgetCache", "Eviction runs on boot.")
    queries = _queries(tmp_path, "WidgetCache\twidget-cache-v1")

    result = _run(tmp_path, queries)

    assert result.returncode == 0, result.stdout
    row = next(l for l in result.stdout.splitlines() if l.startswith("| WidgetCache"))
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert cells[3] == "found"
    assert cells[4] == "", "side B is the human's to fill from a fresh agent session"


def test_a_query_whose_expected_document_is_missing_counts_as_not_found(tmp_path):
    """The interesting half of the table: a query the engine does not answer is exactly
    what side B has to be compared against."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "WidgetCache", "Eviction runs on boot.")
    _doc(sessions, "sweeper-job", "SweeperJob", "Sweeps nightly.")
    queries = _queries(tmp_path, "SweeperJob\twidget-cache-v1")

    result = _run(tmp_path, queries)

    row = next(l for l in result.stdout.splitlines() if l.startswith("| SweeperJob"))
    assert [c.strip() for c in row.strip().strip("|").split("|")][3] == "not found"


def test_token_cost_uses_the_same_estimator_as_telemetry(tmp_path):
    """If side A were counted differently from what `engmem telemetry` reports, the
    baseline would compare the engine against itself measured two ways."""
    from engmem.telemetry import estimate_tokens

    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "WidgetCache", "Eviction runs on boot.")
    queries = _queries(tmp_path, "WidgetCache\twidget-cache-v1")

    result = _run(tmp_path, queries)
    row = next(l for l in result.stdout.splitlines() if l.startswith("| WidgetCache"))
    reported = int([c.strip() for c in row.strip().strip("|").split("|")][2])

    # re-run the same search and estimate its output independently
    search = subprocess.run(
        [sys.executable, "-m", "engmem.cli", "search", "WidgetCache", "--store", str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert reported == estimate_tokens(len(search.stdout.encode("utf-8")))


def test_a_blank_or_commented_line_in_the_queries_file_is_skipped(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "WidgetCache", "Eviction runs on boot.")
    queries = _queries(tmp_path, "# real queries pulled from telemetry.jsonl", "",
                       "WidgetCache\twidget-cache-v1")

    result = _run(tmp_path, queries)

    rows = [l for l in result.stdout.splitlines() if l.startswith("| ") and "---" not in l]
    assert len(rows) == 2, rows  # header + one query


def test_the_footer_totals_side_a_so_the_comparison_has_one_number(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "WidgetCache", "Eviction runs on boot.")
    queries = _queries(tmp_path, "WidgetCache\twidget-cache-v1", "SweeperJob\twidget-cache-v1")

    result = _run(tmp_path, queries)

    assert "2 quer" in result.stdout
    assert "found 1" in result.stdout


def test_store_problems_are_reported_once_and_do_not_break_the_table(tmp_path):
    """A store with one unparseable document would otherwise print the same error for
    every query, shredding the table it exists to produce. Silencing it would be worse —
    a baseline measured against a store that is quietly missing a document is not a
    baseline. So: collected, stated once, table left contiguous."""
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", "WidgetCache", "Eviction runs on boot.")
    (sessions / "broken.md").write_text(
        "---\nid: broken\ntags: [platform\n---\n\n## 8. Decision Log\n\nBody.\n",
        encoding="utf-8",
    )
    queries = _queries(tmp_path, "WidgetCache\twidget-cache-v1", "SweeperJob\twidget-cache-v1")

    result = _run(tmp_path, queries)

    assert result.stdout.count("broken.md") == 1, "one report, not one per query"
    lines = result.stdout.splitlines()
    first = next(i for i, l in enumerate(lines) if l.startswith("| query"))
    assert all(lines[first + n].startswith("|") for n in range(4)), (
        "header, separator and both rows must be contiguous"
    )
