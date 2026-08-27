"""The tool decides what is mechanically decidable and hands the verdict to the human, per row."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import run_tool

TOOL = Path(__file__).resolve().parent.parent / "tools" / "gate1_report.py"


def _doc(sessions: Path, doc_id: str, *, tags="[platform]", repos="[platform-core]",
         related="", body="## 8. Decision Log\n\nBody.") -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{doc_id}.md").write_text(
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-08-01\nstatus: active\n"
        f"tags: {tags}\nentities: [WidgetCache]\nrepos: {repos}\nrelated: [{related}]\n"
        f"---\n\n{body}\n",
        encoding="utf-8",
    )


def _reuse(cited: str, quote: str) -> str:
    return (
        "## 16. Reuse Log\n\n"
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        f"| {cited} | `WidgetCache.flush()` — \"{quote}\" | reused the shape | reuse |\n"
    )


def _run(store: Path) -> "subprocess.CompletedProcess[str]":
    return run_tool(TOOL, store)


def test_every_reuse_row_appears_with_an_empty_verdict_column(tmp_path):
    sessions = tmp_path / "sessions"
    _doc(sessions, "widget-cache-v1", body="## 8. Decision Log\n\nEviction runs on boot.")
    _doc(sessions, "sweeper-job", tags="[sweeper]", repos="[sweeper-svc]",
         body=_reuse("widget-cache-v1", "Eviction runs on boot."))

    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout
    row = next(l for l in result.stdout.splitlines() if "widget-cache-v1" in l and "|" in l)
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert cells[0] == "sweeper-job", "the citing story must be named first"
    assert cells[1] == "widget-cache-v1", "then the document it cited"
    assert cells[3] in ("distant", "adjacent")
    assert cells[4] == "", "the verdict column is left for the human"


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
    assert "reuse rows: 1   distant: 1   documents reporting no reuse: 0" in out


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
    assert "reuse rows: 0" in result.stdout
    assert "documents reporting no reuse: 1" in result.stdout
    assert "widget-cache-v1 |" not in result.stdout
