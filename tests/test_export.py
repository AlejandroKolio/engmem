"""US-20: one selected document exported as a file, previewed first and written only as previewed,
with the rest of the store left out and unchanged (contracts/export.md)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from conftest import requires_symlinks, write_file
from mcp_harness import _run

from engmem import export, staging
from engmem.cli import main
from engmem.spine import load_store

SELECTED = "20260901-widget-cache"
NEIGHBOUR = "20260801-private-notes"
NEIGHBOUR_TITLE = "Private Planning Notes"
NEIGHBOUR_BODY = "the neighbour's own private text"
BEGIN = "===== export file begins"
END = "===== export file ends ====="


def _doc(sessions: Path, doc_id: str, *, front: str = "", body: str = "", status: str = "active",
         title: str | None = None) -> Path:
    return write_file(
        sessions,
        f"{doc_id}.md",
        f"---\nid: {doc_id}\ntitle: {title or doc_id + ' title'}\ndate: 2026-09-01\n"
        f"status: {status}\ntags: [platform]\nentities: [WidgetCache]\n{front}---\n\n{body}\n",
    )


@pytest.fixture
def store(tmp_path) -> Path:
    sessions = tmp_path / "store" / "sessions"
    sessions.mkdir(parents=True)
    _doc(sessions, NEIGHBOUR, title=NEIGHBOUR_TITLE, body=f"## Decision Log\n\n{NEIGHBOUR_BODY}")
    _doc(sessions, SELECTED, title="Widget cache eviction",
         body="## Decision Log\n\nDecision: LRU eviction.\nSource: platform-core 9c1d2e4")
    return tmp_path / "store"


@pytest.fixture
def out_dir(tmp_path) -> Path:
    directory = tmp_path / "outbox"
    directory.mkdir()
    return directory


def _cli(capsys, store: Path, *args: str) -> tuple[int, str, str]:
    code = main(["export", *args, "--store", str(store)])
    out, err = capsys.readouterr()
    return code, out, err


def _preview(capsys, store: Path, doc_id: str = SELECTED) -> str:
    code, out, err = _cli(capsys, store, doc_id)
    assert code == 0, out + err
    return out


def _shown_file(preview: str) -> bytes:
    """The file text between the preview's markers, as the bytes the file would hold."""
    lines = preview.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(BEGIN))
    end = lines.index(END)
    return ("\n".join(lines[start + 1:end]) + "\n").encode("utf-8")


def _fingerprint(preview: str) -> str:
    return next(line for line in preview.splitlines() if line.startswith("fingerprint: ")).split()[1]


def _excluded(preview: str) -> list[str]:
    return [line for line in preview.splitlines() if line.startswith("  - ")]


def _export(capsys, store: Path, out: Path, doc_id: str = SELECTED) -> bytes:
    fingerprint = _fingerprint(_preview(capsys, store, doc_id))
    code, stdout, stderr = _cli(capsys, store, doc_id, "--out", str(out), "--confirm", fingerprint)
    assert code == 0, stdout + stderr
    return out.read_bytes()


def _rewrite(store: Path, doc_id: str, **kwargs) -> None:
    _doc(store / "sessions", doc_id, **kwargs)


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(root).as_posix(): (None if p.is_dir() else p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


# ---------------------------------------------------------------------------
# AC-20.1 -- the preview shows the whole file, and only the selected document is in it
# ---------------------------------------------------------------------------


def test_the_preview_shows_the_exact_bytes_the_file_gets_and_their_fingerprint(store, out_dir, capsys):
    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md")

    assert written == _shown_file(preview)
    assert hashlib.sha256(written).hexdigest()[:16] == _fingerprint(preview)


def test_the_file_holds_the_selected_document_with_its_metadata_and_nothing_of_its_neighbours(
    store, out_dir, capsys
):
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert written.startswith(f"---\nid: {SELECTED}\ntitle: Widget cache eviction\n")
    assert "Decision: LRU eviction." in written
    assert NEIGHBOUR_BODY not in written and NEIGHBOUR not in written


def test_the_preview_never_shows_a_neighbours_content(store, capsys):
    assert NEIGHBOUR_BODY not in _preview(capsys, store)


# ---------------------------------------------------------------------------
# AC-20.2 -- references to other documents and local paths are cut, and listed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cut", "kind"),
    [
        pytest.param(f"See {NEIGHBOUR} for the background.", NEIGHBOUR, "document id", id="doc-id"),
        pytest.param(
            f"Stored as sessions/{NEIGHBOUR}.md before.", NEIGHBOUR, "document id", id="doc-filename"
        ),
        pytest.param(
            f"Cited {NEIGHBOUR}@0123456789abcdef here.", f"{NEIGHBOUR}@0123456789abcdef",
            "versioned citation", id="doc-id-versioned",
        ),
        pytest.param(
            "Cited 20250101-gone-doc@0123456789abcdef here.", "20250101-gone-doc@0123456789abcdef",
            "versioned citation", id="versioned-citation-of-an-absent-document",
        ),
        pytest.param("As in [[My Private Page]].", "[[My Private Page]]", "wiki link", id="wiki-link"),
        pytest.param(
            f"Discussed in {NEIGHBOUR_TITLE.lower()} earlier.", NEIGHBOUR_TITLE.lower(),
            "document title", id="doc-title-any-case",
        ),
        pytest.param(
            "Log at /Users/someone/work/dump.txt showed it.", "/Users/someone/work/dump.txt",
            "local path", id="posix-home",
        ),
        pytest.param(
            "Log at /home/someone/dump.txt showed it.", "/home/someone/dump.txt", "local path",
            id="linux-home",
        ),
        pytest.param("Scratch in /tmp/run-7/out.log.", "/tmp/run-7/out.log.", "local path", id="tmp"),
        pytest.param(
            r"Notes in C:\Users\someone\notes.md here.", r"C:\Users\someone\notes.md",
            "local path", id="windows-drive",
        ),
        pytest.param(
            "Notes in D:/work/notes.md here.", "D:/work/notes.md", "local path",
            id="windows-drive-forward-slashes",
        ),
        pytest.param(
            r"Share at \\fileserver\team\notes.md here.", r"\\fileserver\team\notes.md",
            "local path", id="unc",
        ),
        pytest.param("Kept in ~/notes/cache.md for now.", "~/notes/cache.md", "local path", id="tilde"),
        pytest.param(
            "Open file:///Users/someone/a.md now.", "file:///Users/someone/a.md", "local path",
            id="file-url",
        ),
        pytest.param(
            f"Draft was {NEIGHBOUR}_v2 then.", NEIGHBOUR, "document id", id="doc-id-underscore",
        ),
        pytest.param(
            "Log at /c/Users/someone/dump.txt showed it.", "/c/Users/someone/dump.txt",
            "local path", id="git-bash-drive",
        ),
        pytest.param(
            r"Notes in \\?\D:\work\notes.md here.", r"\\?\D:\work\notes.md", "local path",
            id="windows-extended-drive",
        ),
        pytest.param(
            r"Share at \\?\UNC\fileserver\team\a.md here.", r"\\?\UNC\fileserver\team\a.md",
            "local path", id="windows-extended-unc",
        ),
        pytest.param(
            "Copied to $HOME/notes/a.md once.", "$HOME/notes/a.md", "local path", id="posix-env-home",
        ),
        pytest.param(
            r"Copied to %USERPROFILE%\notes\a.md once.", r"%USERPROFILE%\notes\a.md",
            "local path", id="windows-env-home",
        ),
    ],
)
def test_a_reference_in_the_body_is_cut_from_the_file_and_named_in_the_preview(
    store, out_dir, capsys, text, cut, kind
):
    _rewrite(store, SELECTED, body=f"## Decision Log\n\n{text}")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert cut not in written
    assert export.MARKER in written
    assert any(line.endswith(f": {kind}: {cut}") and "section 'Decision Log'" in line
               for line in _excluded(preview)), preview


def test_the_preview_names_the_file_line_each_marker_is_on(store, capsys):
    _rewrite(store, SELECTED, body=f"## Decision Log\n\nFirst.\nSee {NEIGHBOUR}.")

    preview = _preview(capsys, store)
    file_lines = _shown_file(preview).decode("utf-8").splitlines()
    line = int(_excluded(preview)[-1].split()[2])

    assert file_lines[line - 1] == f"See {export.MARKER}."


def test_the_home_directory_is_cut_wherever_it_lives(store, out_dir, capsys, monkeypatch):
    home = Path("/srvdata") / "someone"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _rewrite(store, SELECTED, body=f"## Decision Log\n\nNotes in {home}{os.sep}notes.md here.")

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert str(home) not in written and "Notes in [excluded reference] here." in written


def test_the_longer_of_two_matching_titles_is_cut_whole(store, out_dir, capsys):
    _doc(store / "sessions", "20260802-more-notes", title=f"{NEIGHBOUR_TITLE} Extra")
    _rewrite(store, SELECTED, body=f"## Decision Log\n\nSee {NEIGHBOUR_TITLE} Extra again.")

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert f"See {export.MARKER} again." in written


@pytest.mark.parametrize(("front", "kept"), [("", True), ("related: [20260803-tuning]\n", False)],
                         ids=["unreferenced", "related"])
def test_a_one_word_title_is_cut_only_when_the_document_points_at_it(
    store, out_dir, capsys, front, kept
):
    _doc(store / "sessions", "20260803-tuning", title="Tuning")
    _rewrite(store, SELECTED, front=front, body="## Decision Log\n\nTuning came later.")

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert ("Tuning came later." in written) is kept
    assert (f"{export.MARKER} came later." in written) is not kept


def test_a_neighbours_title_inside_the_selected_title_leaves_the_selected_one_whole(
    store, out_dir, capsys
):
    _doc(store / "sessions", "20260807-widget-cache", title="Widget cache")
    _rewrite(store, SELECTED, title="Widget cache eviction", front="tags: [widget cache eviction]\n",
             body="## Decision Log\n\nSee Widget cache eviction. The Widget cache alone is another.")

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert "title: Widget cache eviction\n" in written and "- widget cache eviction\n" in written
    assert f"See Widget cache eviction. The {export.MARKER} alone is another." in written


def test_a_neighbours_title_running_across_the_selected_title_is_cut(store, out_dir, capsys):
    _doc(store / "sessions", "20260809-eviction-policy", title="Cache eviction policy")
    _rewrite(store, SELECTED, title="Widget cache eviction",
             body="## Decision Log\n\nSee Widget cache eviction policy.")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert "cache eviction policy" not in written.casefold()
    assert "title: Widget cache eviction\n" in written
    assert any(line.endswith(": document title: Widget cache eviction policy")
               for line in _excluded(preview)), preview


@pytest.mark.parametrize(
    ("own", "neighbours", "body", "removed"),
    [
        pytest.param(
            "Widget cache", ["Cache migration to Redis cluster"],
            "Widget cache migration to Redis cluster done.",
            "Widget cache migration to Redis cluster", id="tail",
        ),
        pytest.param(
            "Widget cache eviction", ["Cache eviction policy", "Eviction policy for tenants"],
            "Widget cache eviction policy for tenants done.",
            "Widget cache eviction policy for tenants", id="two-crossing",
        ),
        pytest.param(
            "Widget cache",
            ["Cache eviction policy", "Eviction policy rollout", "Policy rollout for tenants"],
            "Widget cache eviction policy rollout for tenants done.",
            "Widget cache eviction policy rollout for tenants", id="chain",
        ),
    ],
)
def test_the_cut_removes_every_neighbour_title_that_runs_on_from_the_selected_title(
    store, out_dir, capsys, own, neighbours, body, removed
):
    for n, title in enumerate(neighbours):
        _doc(store / "sessions", f"20260810-neighbour-{n}", title=title)
    _rewrite(store, SELECTED, title=own, body=f"## Decision Log\n\n{body}")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert f"\n{export.MARKER} done.\n" in written
    assert not any(title.casefold() in written.casefold() for title in neighbours)
    assert [line.split(": ", 2)[2] for line in _excluded(preview)] == [removed]


def test_a_reference_begun_inside_a_cut_is_removed_with_it(store, out_dir, capsys):
    _doc(store / "sessions", "20260811-title-with-id", title="Cache eviction 20260801-private")
    _rewrite(store, SELECTED, title="Widget cache eviction",
             body=f"## Decision Log\n\nSee Widget cache eviction {NEIGHBOUR} now.")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert f"See {export.MARKER} now." in written
    assert [line.split(": ", 2)[2] for line in _excluded(preview)] == [
        f"Widget cache eviction {NEIGHBOUR}"
    ]


def test_a_file_name_that_differs_from_the_id_is_cut_too(store, out_dir, capsys):
    write_file(store / "sessions", "old-name.md",
               "---\nid: 20260808-renamed\ntitle: Renamed\nstatus: active\n---\n\nx\n")
    _rewrite(store, SELECTED, body="## Decision Log\n\nWas sessions/old-name.md once.")

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert "old-name" not in written


@pytest.mark.parametrize(
    ("front", "reuse"),
    [
        pytest.param("related: [scratch]\n", "", id="related"),
        pytest.param("superseded_by: scratch\n", "", id="superseded_by"),
        pytest.param("supersedes: [scratch]\n", "", id="supersedes"),
        pytest.param("navigation_miss:\n  - doc: scratch\n    query: q\n", "", id="navigation_miss"),
        pytest.param("", '\n\n## Reuse Log\n\n| scratch | "quoted text" | k | reuse |', id="reuse-log"),
    ],
)
def test_an_id_of_one_word_is_cut_only_when_the_document_points_at_it(
    store, out_dir, capsys, front, reuse
):
    _doc(store / "sessions", "notes")
    _doc(store / "sessions", "scratch")
    _rewrite(store, SELECTED, front=front,
             body=("## Decision Log\n\nThe notes say so; scratch too, as scratch@0123456789abcdef; "
                   f"the scratchpad and myscratch stay.{reuse}"))

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert (f"The notes say so; {export.MARKER} too, as {export.MARKER}; the scratchpad and "
            "myscratch stay."
            in written)


def test_an_id_is_matched_whatever_its_case(store, out_dir, capsys):
    _doc(store / "sessions", "20260805-Mixed-Case")
    _rewrite(store, SELECTED, body="## Decision Log\n\nSee 20260805-mixed-case.")

    assert "mixed-case" not in _export(capsys, store, out_dir / "shared.md").decode("utf-8")


def test_a_neighbour_with_the_same_title_does_not_cut_the_selected_ones(store, out_dir, capsys):
    _doc(store / "sessions", "20260806-same-title", title="WIDGET CACHE EVICTION")
    _rewrite(store, SELECTED, title="Widget cache eviction", body="## Decision Log\n\nLRU.")

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert "title: Widget cache eviction\n" in written


def test_a_document_engmem_could_not_load_is_still_cut_by_its_name(store, out_dir, capsys):
    write_file(store / "sessions", "20260804-broken-notes.md", "---\nid: [unclosed\n---\n")
    _rewrite(store, SELECTED, body="## Decision Log\n\nSee 20260804-broken-notes.")

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert "20260804-broken-notes" not in written


def test_the_selected_documents_own_id_stays_even_when_it_points_at_itself(store, out_dir, capsys):
    _rewrite(store, SELECTED, front=f"related: [{SELECTED}]\n", body=f"## Decision Log\n\nThis is {SELECTED}.")

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert written.startswith(f"---\nid: {SELECTED}\n") and f"This is {SELECTED}." in written


def test_what_verifies_the_decision_stays(store, out_dir, capsys):
    body = (
        "## Decision Log\n\nSource: platform-core 9c1d2e4, PR https://example.com/org/repo/pull/7\n"
        "The handler serves /api/v1/orders from src/cache/WidgetCache.java.\n"
        "Docs: https://example.com/home/page and src/var/settings.py."
    )
    front = (
        "repos: [platform-core]\nverified_at: {platform-core: 9c1d2e4}\n"
        "covers_files: [src/cache/WidgetCache.java]\npr: 7\nbranch: feature/cache\n"
    )
    _rewrite(store, SELECTED, front=front, body=body)

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert body in written and export.MARKER not in written
    for line in ("- platform-core", "platform-core: 9c1d2e4", "- src/cache/WidgetCache.java",
                 "pr: 7", "branch: feature/cache"):
        assert line in written


@pytest.mark.parametrize(
    ("front", "field", "reason"),
    [
        pytest.param(f"related: [{NEIGHBOUR}]\n", "related", "names other documents", id="related"),
        pytest.param(
            f"navigation_miss:\n  - doc: {NEIGHBOUR}\n    query: private search words\n",
            "navigation_miss", "names other documents", id="navigation_miss",
        ),
        pytest.param(f"supersedes: {NEIGHBOUR}\n", "supersedes", "names other documents", id="supersedes"),
        pytest.param("capture_minutes: 12\n", "capture_minutes", "not on the export list", id="capture"),
        pytest.param("mode: research\n", "mode", "not on the export list", id="mode"),
        pytest.param("context_bytes: 900\n", "context_bytes", "not on the export list", id="unknown"),
    ],
)
def test_a_front_matter_field_off_the_export_list_is_cut_and_named(
    store, out_dir, capsys, front, field, reason
):
    _rewrite(store, SELECTED, front=front, body="## Decision Log\n\nLRU.")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert f"\n{field}:" not in written
    assert any(line.startswith(f"  - front matter {field}: field cut, {reason}")
               for line in _excluded(preview)), preview


def test_a_comment_in_the_front_matter_never_reaches_the_file(store, out_dir, capsys):
    _rewrite(store, SELECTED, front="tags_note: x  # the private reason\n", body="LRU.")

    assert "private reason" not in _export(capsys, store, out_dir / "shared.md").decode("utf-8")


def test_a_kept_field_is_cleaned_of_references_too(store, out_dir, capsys):
    _rewrite(store, SELECTED, front="covers_files: [/Users/someone/repo/src/a.py, src/b.py]\n",
             body="LRU.")

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert "/Users/someone" not in written and "- src/b.py" in written


def test_the_reuse_log_goes_whole_because_it_quotes_other_documents(store, out_dir, capsys):
    reuse = (
        "| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        f"| {NEIGHBOUR}@0123456789abcdef | \"a quote from the neighbour\" | kept | reuse |"
    )
    _rewrite(store, SELECTED,
             body=f"## Decision Log\n\nLRU.\n\n## Reuse Log\n\n{reuse}\n\n## Search Trace\n\nshell")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert "a quote from the neighbour" not in written
    assert f"## Reuse Log\n\n{export.MARKER}\n\n## Search Trace\n\nshell\n" in written
    assert any("Reuse Log: 1 row(s)" in line and "section 'Reuse Log'" in line
               for line in _excluded(preview))


def test_a_setext_reuse_log_heading_is_found_too(store, out_dir, capsys):
    _rewrite(store, SELECTED,
             body='Decision Log\n---\n\nLRU.\n\nReuse Log\n---\n\n| x-y | "a quote from elsewhere" | k | reuse |')

    written = _export(capsys, store, out_dir / "s.md").decode("utf-8")

    assert written.endswith(f"## Decision Log\n\nLRU.\n\n## Reuse Log\n\n{export.MARKER}\n")


@pytest.mark.parametrize("content", ["\n\nPrior docs used: none.", ""], ids=["none-sentence", "empty"])
def test_a_reuse_log_that_names_nothing_is_kept(store, out_dir, capsys, content):
    _rewrite(store, SELECTED, body=f"## Decision Log\n\nLRU.\n\n## Reuse Log{content}")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert written.endswith(f"## Reuse Log{content}\n") and _excluded(preview) == []


def test_a_reuse_log_at_the_end_leaves_the_marker_on_the_last_line(store, out_dir, capsys):
    _rewrite(store, SELECTED, body='## Decision Log\n\nLRU.\n\n## Reuse Log\n\n| x-y | "a quote" | k | reuse |')

    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert written.endswith(f"## Reuse Log\n\n{export.MARKER}\n")


def test_the_preview_lists_the_cuts_in_file_order(store, capsys):
    _rewrite(store, SELECTED, body=(
        '## Decision Log\n\nSee /tmp/x/y.\n\n## Reuse Log\n\n| x-y | "a quote" | k | reuse |'
    ))

    kinds = [line.split(": ")[1] for line in _excluded(_preview(capsys, store))]

    assert kinds == ["local path", "Reuse Log"]


# ---------------------------------------------------------------------------
# document states
# ---------------------------------------------------------------------------


def test_a_draft_is_refused_and_nothing_is_previewed(store, capsys):
    _rewrite(store, SELECTED, status="draft", body="## Decision Log\n\nLRU.")

    code, out, _err = _cli(capsys, store, SELECTED)

    assert code == 2 and "is a draft" in out and BEGIN not in out


def test_a_superseded_document_exports_as_superseded_without_its_successor(store, out_dir, capsys):
    _rewrite(store, SELECTED, status="superseded", front=f"superseded_by: {NEIGHBOUR}\n",
             body="## Decision Log\n\nLRU.")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert "note: this document is superseded" in preview
    assert "status: superseded" in written and NEIGHBOUR not in written


def test_a_document_without_a_title_exports_none_and_says_so(store, out_dir, capsys):
    write_file(store / "sessions", f"{SELECTED}.md", f"---\nid: {SELECTED}\nstatus: active\n---\n")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert written == f"---\nid: {SELECTED}\nstatus: active\n---\n"
    assert "note: front matter does not state title, date" in preview


@pytest.mark.parametrize(
    "replacement", [None, "---\n- a list\n---\n", "---\nid: x\n"], ids=["gone", "list", "unclosed"]
)
def test_a_document_that_changed_under_the_load_is_refused(store, replacement):
    result = load_store(store / "sessions")
    path = store / "sessions" / f"{SELECTED}.md"
    if replacement is None:
        path.unlink()
    else:
        write_file(store / "sessions", path.name, replacement)

    with pytest.raises(export.ExportError, match="cannot read|not a mapping"):
        export.prepare_export(store, result, SELECTED)


def test_the_store_and_the_home_directory_are_cut_wherever_they_live(monkeypatch):
    home = Path("/srvdata") / "someone"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    store = Path("/srvdata") / "engmem-store"

    assert {str(store), str(home)} <= export._home_paths(store)


def test_a_home_directory_at_the_root_cuts_nothing(store, out_dir, capsys, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(Path.cwd().anchor)))
    _rewrite(store, SELECTED, body="## Decision Log\n\nThe handler serves /api/v1/orders.")

    assert "/api/v1/orders." in _export(capsys, store, out_dir / "shared.md").decode("utf-8")


def test_a_blank_field_is_neither_written_nor_listed(store, out_dir, capsys):
    _rewrite(store, SELECTED, front="superseded_by:\npr:\n", body="LRU.")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md").decode("utf-8")

    assert "pr:" not in written and _excluded(preview) == []


def test_an_unknown_id_is_refused(store, capsys):
    code, out, _err = _cli(capsys, store, "20990101-no-such")

    assert code == 2 and "no document with id '20990101-no-such'" in out


# ---------------------------------------------------------------------------
# AC-20.3 -- the confirmed file is the one shown, and the store is unchanged
# ---------------------------------------------------------------------------


def test_previewing_and_exporting_leave_the_store_byte_identical(store, out_dir, capsys):
    before = _tree(store)
    _preview(capsys, store)
    after_preview = _tree(store)
    _export(capsys, store, out_dir / "shared.md")

    assert after_preview == before
    assert _tree(store) == before


@pytest.mark.parametrize(
    "raw", [b"\r\n", b"\xef\xbb\xbf"], ids=["crlf", "bom"],
)
def test_the_file_is_utf8_with_lf_endings_whatever_the_source_used(store, out_dir, capsys, raw):
    path = store / "sessions" / f"{SELECTED}.md"
    text = path.read_bytes()
    path.write_bytes(text.replace(b"\n", b"\r\n") if raw == b"\r\n" else raw + text)

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md")

    assert b"\r" not in written and not written.startswith(b"\xef\xbb\xbf")
    assert hashlib.sha256(written).hexdigest()[:16] == _fingerprint(preview)


def test_a_control_character_is_escaped_in_the_preview_and_the_file_alike(store, out_dir, capsys):
    _rewrite(store, SELECTED, body="## Decision Log\n\nLRU\x1b[31m red.")

    preview = _preview(capsys, store)
    written = _export(capsys, store, out_dir / "shared.md")

    assert b"\x1b" not in written and written == _shown_file(preview)


def test_a_fingerprint_is_accepted_in_any_case_and_with_surrounding_space(store, out_dir, capsys):
    fingerprint = _fingerprint(_preview(capsys, store))

    code, out, _err = _cli(capsys, store, SELECTED, "--out", str(out_dir / "s.md"),
                           "--confirm", f" {fingerprint.upper()} ")

    assert code == 0, out


# ---------------------------------------------------------------------------
# AC-20.4 -- without a matching confirmation nothing is written anywhere
# ---------------------------------------------------------------------------


def test_a_preview_alone_writes_nothing_anywhere(store, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = _tree(tmp_path)

    _preview(capsys, store)

    assert _tree(tmp_path) == before


@pytest.mark.parametrize("flags", [["--out", "s.md"], ["--confirm", "0123456789abcdef"]],
                         ids=["out-only", "confirm-only"])
def test_out_and_confirm_only_work_together(store, tmp_path, capsys, monkeypatch, flags):
    monkeypatch.chdir(tmp_path)
    before = _tree(tmp_path)

    code, out, _err = _cli(capsys, store, SELECTED, *flags)

    assert code == 2 and "go together" in out and _tree(tmp_path) == before


def test_a_document_changed_since_the_preview_is_not_exported(store, out_dir, capsys):
    fingerprint = _fingerprint(_preview(capsys, store))
    _rewrite(store, SELECTED, body=f"## Decision Log\n\nLRU, and now {NEIGHBOUR_BODY}.")

    code, out, _err = _cli(capsys, store, SELECTED, "--out", str(out_dir / "s.md"),
                           "--confirm", fingerprint)

    assert code == 2 and "not the one previewed" in out
    assert list(out_dir.iterdir()) == []


def test_an_existing_file_is_never_replaced(store, out_dir, capsys):
    target = write_file(out_dir, "s.md", "the recipient's own file\n")
    fingerprint = _fingerprint(_preview(capsys, store))

    code, out, _err = _cli(capsys, store, SELECTED, "--out", str(target), "--confirm", fingerprint)

    assert code == 2 and "already exists" in out
    assert target.read_bytes() == b"the recipient's own file\n"
    assert sorted(p.name for p in out_dir.iterdir()) == ["s.md"]


def test_a_file_created_between_the_check_and_the_write_is_not_replaced(
    store, out_dir, capsys, monkeypatch
):
    target = out_dir / "s.md"
    real_stage = staging.stage

    def stage_then_race(path, data):
        staged = real_stage(path, data)
        target.write_bytes(b"theirs\n")
        return staged

    monkeypatch.setattr(export, "stage", stage_then_race)
    fingerprint = _fingerprint(_preview(capsys, store))

    code, out, _err = _cli(capsys, store, SELECTED, "--out", str(target), "--confirm", fingerprint)

    assert code == 2 and "already exists" in out
    assert target.read_bytes() == b"theirs\n" and sorted(p.name for p in out_dir.iterdir()) == ["s.md"]


@requires_symlinks
def test_a_dangling_link_at_the_target_is_not_followed(store, out_dir, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere.md"
    (out_dir / "s.md").symlink_to(elsewhere)
    fingerprint = _fingerprint(_preview(capsys, store))

    code, _out, _err = _cli(capsys, store, SELECTED, "--out", str(out_dir / "s.md"),
                            "--confirm", fingerprint)

    assert code == 2 and not elsewhere.exists()


def _inside_store_paths(store: Path) -> list[Path]:
    return [
        store / "shared.md",
        store / "sessions" / "shared.md",
        store / "sessions" / ".." / "shared.md",
        store / "versions" / SELECTED / "shared.md",
    ]


@pytest.mark.parametrize("index", range(4), ids=["root", "sessions", "dot-dot", "nested"])
def test_a_file_inside_the_store_is_refused(store, capsys, index):
    (store / "versions" / SELECTED).mkdir(parents=True)
    before = _tree(store)
    fingerprint = _fingerprint(_preview(capsys, store))

    code, out, _err = _cli(capsys, store, SELECTED, "--out", str(_inside_store_paths(store)[index]),
                           "--confirm", fingerprint)

    assert code == 2 and "inside the store" in out and _tree(store) == before


@requires_symlinks
def test_a_link_that_leads_into_the_store_is_refused(store, tmp_path, capsys):
    link = tmp_path / "looks-outside"
    link.symlink_to(store / "sessions", target_is_directory=True)
    before = _tree(store)
    fingerprint = _fingerprint(_preview(capsys, store))

    code, out, _err = _cli(capsys, store, SELECTED, "--out", str(link / "shared.md"),
                           "--confirm", fingerprint)

    assert code == 2 and "inside the store" in out and _tree(store) == before


@requires_symlinks
def test_a_store_named_through_a_link_still_owns_its_directories(store, tmp_path, capsys):
    alias = tmp_path / "store-alias"
    alias.symlink_to(store, target_is_directory=True)
    before = _tree(store)
    fingerprint = _fingerprint(_preview(capsys, alias))

    code, out, _err = _cli(capsys, alias, SELECTED, "--out", str(store / "shared.md"),
                           "--confirm", fingerprint)

    assert code == 2 and "inside the store" in out and _tree(store) == before


@requires_symlinks
def test_a_directory_the_store_links_to_is_refused(tmp_path, out_dir, capsys):
    real_sessions = tmp_path / "synced" / "sessions"
    real_sessions.mkdir(parents=True)
    _doc(real_sessions, SELECTED, body="## Decision Log\n\nLRU.")
    store = tmp_path / "linked-store"
    store.mkdir()
    (store / "sessions").symlink_to(real_sessions, target_is_directory=True)
    before = _tree(tmp_path / "synced")
    fingerprint = _fingerprint(_preview(capsys, store))

    code, out, _err = _cli(capsys, store, SELECTED, "--out", str(real_sessions / "copy.md"),
                           "--confirm", fingerprint)

    assert code == 2 and "inside the store" in out and _tree(tmp_path / "synced") == before


def test_a_missing_directory_is_refused_and_not_created(store, tmp_path, capsys):
    fingerprint = _fingerprint(_preview(capsys, store))

    code, out, _err = _cli(capsys, store, SELECTED, "--out", str(tmp_path / "nowhere" / "s.md"),
                           "--confirm", fingerprint)

    assert code == 2 and "does not exist" in out and not (tmp_path / "nowhere").exists()


@pytest.mark.parametrize("step", ["stage", "commit_new"])
def test_a_failed_write_leaves_no_partial_file(store, out_dir, capsys, monkeypatch, step):
    def fail(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(export, step, fail)
    fingerprint = _fingerprint(_preview(capsys, store))

    code, out, _err = _cli(capsys, store, SELECTED, "--out", str(out_dir / "s.md"),
                           "--confirm", fingerprint)

    assert code == 2 and "cannot write" in out and list(out_dir.iterdir()) == []


def test_no_mcp_tool_can_export(store):
    _code, responses, _raw = _run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert not [t["name"] for t in responses[0]["result"]["tools"] if "export" in t["name"]]
