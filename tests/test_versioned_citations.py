"""US-13: a Reuse Log quote is checked against the version it was taken from, and the source's
standing today is reported beside it, never in its place (contracts/gate1.md, "Versioned
citations")."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import (
    ACTIVE_CONTENT,
    DRAFT_CONTENT,
    requires_permission_enforcement,
    requires_symlinks,
    run_tool,
)
from mcp_harness import _call, _run, _text, _tools_call_msg

from engmem import gate1, mcp_server, versions
from engmem.cli import main
from engmem.spine import load_store, parse_document
from engmem.staging import version_of

REPO = Path(__file__).resolve().parent.parent
VERIFY = REPO / "tools" / "verify_citations.py"
REPORT = REPO / "tools" / "gate1_report.py"

V1_SENTENCE = "Widget flush runs eagerly on every boot."
V2_SENTENCE = "Widget flush now runs lazily on first use."


def _source(sessions: Path, body_sentence: str, *, status: str = "active",
            superseded_by: str = "") -> Path:
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / "20260101-widget-cache.md"
    path.write_text(
        "---\nid: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        f"status: {status}\nsuperseded_by: {superseded_by}\ntags: [platform]\n"
        "entities: [WidgetCache]\nrepos: [platform-core]\nrelated: []\n---\n\n"
        f"## 8. Decision Log\n\n{body_sentence}\n",
        encoding="utf-8",
    )
    return path


def _citing(sessions: Path, prior_doc: str, quote: str) -> None:
    (sessions / "20260901-story.md").write_text(
        "---\nid: 20260901-story\ntitle: Story\ndate: 2026-09-01\nstatus: active\n"
        "tags: [sweeper]\nentities: [Sweeper]\nrepos: [sweeper-svc]\nrelated: []\n---\n\n"
        "## 16. Reuse Log\n\n| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        f'| {prior_doc} | flush order: "{quote}" | kept it | reuse |\n',
        encoding="utf-8",
    )


def _retain_current(store: Path) -> str:
    return versions.retain(store, parse_document(store / "sessions" / "20260101-widget-cache.md"))


def _one_row(store: Path) -> gate1.RowVerdict:
    rows = [r for r in gate1.evaluate(store).rows if r.citing.id == "20260901-story"]
    assert len(rows) == 1, rows
    return rows[0]


@pytest.fixture
def edited(tmp_path) -> tuple[Path, str]:
    """A store whose source was cited at V1 and has since been edited to V2."""
    sessions = tmp_path / "sessions"
    _source(sessions, V1_SENTENCE)
    v1 = _retain_current(tmp_path)
    _source(sessions, V2_SENTENCE)
    return tmp_path, v1


# --- retention ----------------------------------------------------------------------------------


def test_a_retained_copy_holds_the_exact_bytes_and_is_named_by_their_version(tmp_path):
    path = _source(tmp_path / "sessions", V1_SENTENCE)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    version = _retain_current(tmp_path)

    copy = versions.retained_path(tmp_path, "20260101-widget-cache", version)
    assert copy.read_bytes() == path.read_bytes()
    assert version_of(copy.read_bytes()) == version
    assert _retain_current(tmp_path) == version
    assert sorted(p.name for p in copy.parent.iterdir()) == [copy.name]


def test_a_document_changed_since_it_was_read_is_not_retained(tmp_path):
    path = _source(tmp_path / "sessions", V1_SENTENCE)
    doc = parse_document(path)
    _source(tmp_path / "sessions", V2_SENTENCE + " And more.")

    with pytest.raises(versions.RetentionError, match="changed on disk"):
        versions.retain(tmp_path, doc)
    assert list(tmp_path.rglob("*.md")) == [tmp_path / "sessions" / "20260101-widget-cache.md"]


@requires_symlinks
def test_retention_never_writes_through_a_symlinked_versions_directory(tmp_path):
    _source(tmp_path / "sessions", V1_SENTENCE)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / versions.VERSIONS_DIR).symlink_to(outside, target_is_directory=True)

    with pytest.raises(versions.RetentionError, match="not a plain directory"):
        _retain_current(tmp_path)
    assert list(outside.iterdir()) == []


# --- the search shows the reference and retains what it shows -------------------------------------


def test_a_search_names_the_reference_to_cite_and_retains_that_version(tmp_path, capsys):
    path = _source(tmp_path / "sessions", V1_SENTENCE)
    expected = f"20260101-widget-cache@{version_of(path.read_bytes())}"

    main(["search", "widget flush", "--store", str(tmp_path)])

    out = capsys.readouterr().out
    assert f"cite as (Reuse Log prior-doc): {expected}" in out
    reference_version = expected.rsplit("@", 1)[1]
    assert versions.retained_path(tmp_path, "20260101-widget-cache", reference_version).is_file()


def test_the_mcp_search_and_the_role_search_name_the_reference_too(tmp_path):
    path = _source(tmp_path / "sessions", V1_SENTENCE)
    version = version_of(path.read_bytes())
    expected = f"cite as (Reuse Log prior-doc): 20260101-widget-cache@{version}"

    _, responses, _ = _run(
        tmp_path,
        _tools_call_msg(1, arguments={"query": "widget flush"}),
        _tools_call_msg(2, name=mcp_server.ROLE_TOOL_NAME,
                        arguments={"query": "widget flush", "role": "decisions"}),
    )

    assert all(expected in _text(r["result"]) for r in responses)


def test_a_draft_is_never_shown_and_never_retained(tmp_path, capsys):
    _source(tmp_path / "sessions", V1_SENTENCE, status="draft")

    main(["search", "widget flush", "--store", str(tmp_path)])

    assert "cite as" not in capsys.readouterr().out
    assert not (tmp_path / versions.VERSIONS_DIR).exists()


def test_the_versions_directory_is_never_loaded_or_searched(tmp_path, capsys):
    """A retained copy holds the old text: found by a search, it would resurrect an edit away."""
    _source(tmp_path / "sessions", "Quaternion tables decide it.")
    _retain_current(tmp_path)
    _source(tmp_path / "sessions", V2_SENTENCE)

    main(["search", "quaternion", "--store", str(tmp_path)])

    out = capsys.readouterr().out
    assert "prior context: none found" in out
    assert "sit outside the searched set" not in out
    assert [d.id for d in load_store(tmp_path / "sessions").docs] == ["20260101-widget-cache"]


def test_the_reference_is_not_in_context_bytes(tmp_path):
    """The `cite as` line is store housekeeping like the stray note: telemetry measures the
    rendered result the agent reads for content, unchanged by US-13."""
    _source(tmp_path / "sessions", V1_SENTENCE)

    _, responses, _ = _run(tmp_path, _tools_call_msg(1, arguments={"query": "widget flush"}))

    text = _text(responses[0]["result"])
    rendered = text.split("\ncite as")[0]
    row = (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")
    assert f'"context_bytes": {len(rendered.encode("utf-8"))}' in row


# --- AC-13.1 / AC-13.2: the cited version is the evidence -----------------------------------------


def test_ac_13_2_an_old_quote_is_still_verified_against_the_version_it_cites(edited):
    store, v1 = edited
    _citing(store / "sessions", f"20260101-widget-cache@{v1}", V1_SENTENCE)

    row = _one_row(store)

    assert row.integrity == gate1.Integrity.VERIFIED
    assert row.cited_version == v1
    assert gate1.exclusion_reason(row) is None


def test_ac_13_2_the_current_text_is_never_substituted_for_the_cited_version(edited):
    store, v1 = edited
    _citing(store / "sessions", f"20260101-widget-cache@{v1}", V2_SENTENCE)

    row = _one_row(store)

    assert row.integrity == gate1.Integrity.QUOTE_NOT_FOUND
    assert row.unfound_quotes == (V2_SENTENCE,)


def test_ac_13_1_the_reference_names_one_version_exactly(edited):
    store, v1 = edited
    v2 = _retain_current(store)
    _citing(store / "sessions", f"20260101-widget-cache@{v2}", V1_SENTENCE)

    assert v1 != v2
    assert _one_row(store).integrity == gate1.Integrity.QUOTE_NOT_FOUND


# --- AC-13.3: unavailable or not intact is unverified, with its reason ----------------------------


def _corrupt(store: Path, v1: str) -> None:
    copy = versions.retained_path(store, "20260101-widget-cache", v1)
    copy.write_bytes(copy.read_bytes().replace(b"eagerly", b"lazily!"))


def _delete(store: Path, v1: str) -> None:
    versions.retained_path(store, "20260101-widget-cache", v1).unlink()


@pytest.mark.parametrize("damage, reason", [
    pytest.param(_delete, "not retained", id="deleted"),
    pytest.param(_corrupt, "fails its integrity check", id="corrupted"),
])
def test_ac_13_3_a_missing_or_altered_version_is_unverified_with_a_reason(edited, damage, reason):
    store, v1 = edited
    damage(store, v1)
    _citing(store / "sessions", f"20260101-widget-cache@{v1}", V1_SENTENCE)

    row = _one_row(store)

    assert row.integrity == gate1.Integrity.VERSION_UNAVAILABLE
    assert reason in row.version_problem
    assert gate1.exclusion_reason(row).startswith("excluded: cited version cannot be checked -- ")
    assert reason in gate1.exclusion_reason(row)


def test_ac_13_3_a_corrupted_copy_is_not_rescued_by_the_current_text(edited):
    """The altered copy says what V2 says: verifying against it would be a false success."""
    store, v1 = edited
    copy = versions.retained_path(store, "20260101-widget-cache", v1)
    copy.write_bytes((store / "sessions" / "20260101-widget-cache.md").read_bytes())
    _citing(store / "sessions", f"20260101-widget-cache@{v1}", V2_SENTENCE)

    assert _one_row(store).integrity == gate1.Integrity.VERSION_UNAVAILABLE


@pytest.mark.parametrize("version", ["abc", "0123456789ABCDEF", "0123456789abcdef0", ""])
def test_ac_13_3_a_malformed_version_is_unverified_not_a_bare_id(tmp_path, version):
    _source(tmp_path / "sessions", V1_SENTENCE)
    _citing(tmp_path / "sessions", f"20260101-widget-cache@{version}", V1_SENTENCE)

    row = _one_row(tmp_path)

    assert row.integrity == gate1.Integrity.VERSION_UNAVAILABLE
    assert "is not a version" in row.version_problem


# --- AC-13.4: accuracy for V1 and the source's status now, side by side -------------------------


def test_ac_13_4_a_superseded_source_reports_both_facts(edited):
    store, v1 = edited
    _source(store / "sessions", V2_SENTENCE, status="superseded",
            superseded_by="20261001-widget-cache-v2")
    _citing(store / "sessions", f"20260101-widget-cache@{v1}", V1_SENTENCE)

    row = _one_row(store)

    assert row.integrity == gate1.Integrity.VERIFIED
    assert row.staleness == gate1.Staleness.CITED_SUPERSEDED

    result = run_tool(VERIFY, store)
    assert result.returncode == 0, result.stdout
    assert (
        f"cites 20260101-widget-cache@{v1} (quote verified against that version), "
        "superseded by 20261001-widget-cache-v2"
    ) in result.stdout


# --- AC-13.5: a legacy row is marked, and still checked as before -------------------------------


def test_ac_13_5_a_legacy_row_is_checked_against_the_current_text_and_marked(edited):
    store, _v1 = edited
    _citing(store / "sessions", "20260101-widget-cache", V2_SENTENCE)

    row = _one_row(store)
    assert (row.integrity, row.cited_version) == (gate1.Integrity.VERIFIED, None)

    verify = run_tool(VERIFY, store)
    assert verify.returncode == 0
    assert "1 row(s) carry no version reference (legacy)" in verify.stdout

    report = run_tool(REPORT, store)
    assert (
        "version references: 0 row(s) cite a version and are checked against it, 1 legacy row(s) "
        "cite none and are checked against the cited document's current text"
    ) in report.stdout


def test_ac_13_5_a_legacy_row_is_never_given_the_current_version(edited):
    store, _v1 = edited
    _citing(store / "sessions", "20260101-widget-cache", V1_SENTENCE)
    citing_before = (store / "sessions" / "20260901-story.md").read_bytes()

    row = _one_row(store)
    run_tool(VERIFY, store)
    run_tool(REPORT, store)

    assert row.cited_version is None
    assert row.integrity == gate1.Integrity.QUOTE_NOT_FOUND
    assert (store / "sessions" / "20260901-story.md").read_bytes() == citing_before


def test_an_id_spelled_with_the_separator_still_resolves_as_a_bare_id(tmp_path):
    """Every row that resolved before versions existed resolves the same way."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "odd.md").write_text(
        f"---\nid: odd@doc\ntitle: Odd\ndate: 2026-01-01\nstatus: active\n---\n\n{V1_SENTENCE}\n",
        encoding="utf-8",
    )
    _citing(sessions, "odd@doc", V1_SENTENCE)

    row = _one_row(tmp_path)

    assert (row.cited_id, row.cited_version, row.integrity) == ("odd@doc", None, "verified")


# --- the CLI tools ------------------------------------------------------------------------------


def test_verify_citations_fails_an_unavailable_version_and_names_why(edited):
    store, v1 = edited
    _delete(store, v1)
    _citing(store / "sessions", f"20260101-widget-cache@{v1}", V1_SENTENCE)

    result = run_tool(VERIFY, store)

    assert result.returncode == 1
    assert (
        f"cited version 20260101-widget-cache@{v1} cannot be checked — not retained"
    ) in result.stdout
    assert "the current text is not used instead" in result.stdout


def test_gate1_report_names_the_cited_version_and_its_state(edited):
    store, v1 = edited
    _delete(store, v1)
    _citing(store / "sessions", f"20260101-widget-cache@{v1}", V1_SENTENCE)

    out = run_tool(REPORT, store).stdout

    row = next(line for line in out.splitlines() if line.startswith("| 20260901-story |"))
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert cells[1] == f"20260101-widget-cache@{v1}"
    assert cells[3] == "version_unavailable"
    assert cells[7].startswith("excluded: cited version cannot be checked -- not retained")
    assert ", 1 cited version unavailable" in out
    assert "version references: 1 row(s) cite a version and are checked against it, 0 legacy" in out


# --- engmem_complete_draft: refuse a malformed reference, note the rest ---------------------------


def _complete(store: Path, prior_doc: str, quote: str) -> tuple[str, bool]:
    (store / "sessions" / "20260101-story-doc.md").write_text(
        DRAFT_CONTENT.replace("20260101-widget-cache", "20260101-story-doc"), encoding="utf-8"
    )
    content = ACTIVE_CONTENT.replace("20260101-widget-cache", "20260101-story-doc").replace(
        "## Reuse Log\n\nPrior docs used: none.\n\n",
        "## Reuse Log\n\n| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        f'| {prior_doc} | flush order: "{quote}" | kept it | reuse |\n\n',
    )
    result, is_error = _call(store, name=mcp_server.COMPLETE_DRAFT_TOOL_NAME,
                             arguments={"id": "20260101-story-doc", "content": content})
    return _text(result), is_error


def test_complete_draft_refuses_a_malformed_version_reference(edited):
    store, _v1 = edited

    text, is_error = _complete(store, "20260101-widget-cache@not-a-version", V1_SENTENCE)

    assert is_error
    assert "version 'not-a-version' is not one a search showed" in text
    assert parse_document(store / "sessions" / "20260101-story-doc.md").status == "draft"


def test_complete_draft_accepts_a_retained_version_without_a_note(edited):
    store, v1 = edited

    text, is_error = _complete(store, f"20260101-widget-cache@{v1}", V1_SENTENCE)

    assert not is_error
    assert "warning" not in text.lower() and "note:" not in text


def test_complete_draft_warns_when_the_cited_version_is_unavailable(edited):
    store, v1 = edited
    _delete(store, v1)

    text, is_error = _complete(store, f"20260101-widget-cache@{v1}", V1_SENTENCE)

    assert not is_error
    assert f"cited version 20260101-widget-cache@{v1} cannot be checked — not retained" in text


def test_complete_draft_notes_a_row_without_a_version(edited):
    store, _v1 = edited

    text, is_error = _complete(store, "20260101-widget-cache", V2_SENTENCE)

    assert not is_error
    assert "note: 1 Reuse Log row(s) name no version" in text
    assert "20260101-story-doc.md:" in text


def test_a_draft_successor_named_by_a_redirect_is_not_retained(tmp_path, capsys):
    sessions = tmp_path / "sessions"
    _source(sessions, V1_SENTENCE, status="superseded", superseded_by="20261001-widget-cache-v2")
    (sessions / "20261001-widget-cache-v2.md").write_text(
        "---\nid: 20261001-widget-cache-v2\ntitle: Widget cache v2\ndate: 2026-10-01\n"
        "status: draft\ntags: [platform]\nentities: [WidgetCache]\n---\n\nWidget flush draft.\n",
        encoding="utf-8",
    )

    main(["search", "widget flush", "--store", str(tmp_path)])

    out = capsys.readouterr().out
    assert "successor is still a draft" in out
    assert not (tmp_path / versions.VERSIONS_DIR / "20261001-widget-cache-v2").exists()


def test_a_store_of_legacy_rows_reports_as_it_did_plus_one_line(tmp_path):
    """The integrity line keeps its pre-US-13 text; the version line is the only addition."""
    sessions = tmp_path / "sessions"
    _source(sessions, V1_SENTENCE)
    _citing(sessions, "20260101-widget-cache", V1_SENTENCE)
    with (sessions / "20260901-story.md").open("a", encoding="utf-8") as story:
        story.write('| nowhere-doc | x: "Some quoted text." | y | reuse |\n')

    out = run_tool(REPORT, tmp_path).stdout

    assert (
        "citation integrity: 1 verified, 0 no quote, 1 cited doc not in store, 0 quote not found\n"
    ) in out
    assert "version references: 0 row(s) cite a version and are checked against it, 1 legacy" in out
    assert "@" not in out.split("\n\n")[0]


@requires_symlinks
def test_a_version_that_cannot_be_retained_is_named_and_not_offered(tmp_path, capsys):
    """A reference is offered only once its copy exists, so a cited version is always one engmem
    can check later; the search itself still answers."""
    _source(tmp_path / "sessions", V1_SENTENCE)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / versions.VERSIONS_DIR).symlink_to(outside, target_is_directory=True)

    exit_code = main(["search", "widget flush", "--store", str(tmp_path)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "### 20260101-widget-cache" in out
    assert "cite as" not in out
    assert "note: version of '20260101-widget-cache' not retained" in out
    assert "can only be checked against the current text" in out


@pytest.mark.parametrize("superseded", [False, True], ids=["active", "superseded-redirect"])
def test_a_related_document_the_result_gives_a_path_for_is_offered_too(tmp_path, capsys,
                                                                       superseded):
    """`/engmem` loads each hit's `related` documents at depth 1, so they are cited too; a
    superseded one is shown as its successor's path, so the successor is the one offered."""
    sessions = tmp_path / "sessions"
    _source(sessions, V1_SENTENCE)
    hit = sessions / "20260101-widget-cache.md"
    hit.write_text(hit.read_text(encoding="utf-8").replace(
        "related: []", "related: [20260202-warmer]"), encoding="utf-8")
    for doc_id, status, successor in [
        ("20260202-warmer", "superseded" if superseded else "active", "20260303-warmer-v2"),
        ("20260303-warmer-v2", "active", ""),
    ]:
        (sessions / f"{doc_id}.md").write_text(
            f"---\nid: {doc_id}\ntitle: Warmer\ndate: 2026-02-02\nstatus: {status}\n"
            f"superseded_by: {successor if status == 'superseded' else ''}\ntags: [ops]\n"
            "entities: [Warmer]\n---\n\nThe warmer is unrelated to the query.\n",
            encoding="utf-8",
        )
    offered = "20260303-warmer-v2" if superseded else "20260202-warmer"
    passed_over = "20260202-warmer" if superseded else "20260303-warmer-v2"

    main(["search", "widget flush", "--store", str(tmp_path)])

    cite = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("cite as"))
    version = version_of((sessions / f"{offered}.md").read_bytes())
    assert f"{offered}@{version}" in cite
    assert f"{passed_over}@" not in cite


# --- review round 2 -----------------------------------------------------------------------------


def _shown_reference(out: str) -> str | None:
    return next((line for line in out.splitlines() if line.startswith("cite as")), None)


@pytest.mark.parametrize("call", [
    pytest.param(lambda: _tools_call_msg(1, arguments={"query": "widget flush"}), id="search"),
    pytest.param(lambda: _tools_call_msg(1, name=mcp_server.ROLE_TOOL_NAME,
                                         arguments={"query": "widget flush", "role": "decisions"}),
                 id="role-search"),
])
def test_d2_a_read_only_server_keeps_no_copy_and_offers_no_reference(tmp_path, call):
    _source(tmp_path / "sessions", V1_SENTENCE)

    _, responses, _ = _run(tmp_path, call(), read_only=True)

    text = _text(responses[0]["result"])
    assert "### 20260101-widget-cache" in text
    assert "cite as" not in text and "not retained" not in text
    assert not (tmp_path / versions.VERSIONS_DIR).exists()


def test_arch_001_an_altered_existing_copy_is_repaired_by_the_next_search(tmp_path, capsys):
    path = _source(tmp_path / "sessions", V1_SENTENCE)
    v1 = _retain_current(tmp_path)
    copy = versions.retained_path(tmp_path, "20260101-widget-cache", v1)
    copy.write_bytes(b"garbage")

    main(["search", "widget flush", "--store", str(tmp_path)])

    assert f"20260101-widget-cache@{v1}" in _shown_reference(capsys.readouterr().out)
    assert copy.read_bytes() == path.read_bytes()
    _citing(tmp_path / "sessions", f"20260101-widget-cache@{v1}", V1_SENTENCE)
    assert _one_row(tmp_path).integrity == gate1.Integrity.VERIFIED


def _directory_at(target: Path) -> None:
    target.mkdir()


def _dangling_symlink_at(target: Path) -> None:
    target.symlink_to(target.with_name("nowhere.md"))


@pytest.mark.parametrize("occupy", [
    pytest.param(_directory_at, id="directory"),
    pytest.param(_dangling_symlink_at, id="dangling-symlink", marks=requires_symlinks),
])
def test_arch_001_an_entry_that_is_not_a_copy_gets_no_reference(tmp_path, capsys, occupy):
    path = _source(tmp_path / "sessions", V1_SENTENCE)
    version = version_of(path.read_bytes())
    target = versions.retained_path(tmp_path, "20260101-widget-cache", version)
    target.parent.mkdir(parents=True)
    occupy(target)

    main(["search", "widget flush", "--store", str(tmp_path)])

    out = capsys.readouterr().out
    assert _shown_reference(out) is None
    assert "exists and is not a regular file" in out


def test_arch_001_a_stale_create_lock_without_hard_links_gets_no_reference(
    tmp_path, capsys, monkeypatch
):
    import errno

    from engmem import staging

    def no_hard_links(_src, _dst):
        raise PermissionError(errno.EPERM, "hard links not supported")

    monkeypatch.setattr(staging.os, "link", no_hard_links)
    path = _source(tmp_path / "sessions", V1_SENTENCE)
    version = version_of(path.read_bytes())
    target = versions.retained_path(tmp_path, "20260101-widget-cache", version)
    target.parent.mkdir(parents=True)
    target.with_name(f".{target.name}.create-lock").mkdir()

    main(["search", "widget flush", "--store", str(tmp_path)])

    out = capsys.readouterr().out
    assert _shown_reference(out) is None
    assert "could not be created" in out
    assert not os.path.lexists(target)


def test_arch_002_copies_are_exempt_from_line_ending_conversion(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    subprocess.run([git, "init", "-q", str(tmp_path)], check=True, timeout=30)
    _source(tmp_path / "sessions", V1_SENTENCE)
    version = _retain_current(tmp_path)

    copy = versions.retained_path(tmp_path, "20260101-widget-cache", version)
    result = subprocess.run(
        [git, "-C", str(tmp_path), "check-attr", "text", "--",
         str(copy.relative_to(tmp_path).as_posix())],
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert result.stdout.strip().endswith("text: unset")


def test_arch_002_a_users_own_gitattributes_is_left_alone(tmp_path):
    root = tmp_path / versions.VERSIONS_DIR
    root.mkdir()
    (root / ".gitattributes").write_bytes(b"*.md binary\n")
    _source(tmp_path / "sessions", V1_SENTENCE)

    _retain_current(tmp_path)

    assert (root / ".gitattributes").read_bytes() == b"*.md binary\n"


@requires_permission_enforcement
def test_arch_005_an_unwritable_store_says_so_once(tmp_path, capsys):
    sessions = tmp_path / "sessions"
    _source(sessions, V1_SENTENCE)
    (sessions / "20260202-widget-flush.md").write_text(
        "---\nid: 20260202-widget-flush\ntitle: Widget flush\ndate: 2026-02-02\nstatus: active\n"
        "tags: [platform]\nentities: [WidgetCache]\n---\n\nWidget flush ordering again.\n",
        encoding="utf-8",
    )
    tmp_path.chmod(0o500)
    try:
        main(["search", "widget flush", "--store", str(tmp_path)])
    finally:
        tmp_path.chmod(0o700)

    out = capsys.readouterr().out
    notes = [line for line in out.splitlines() if "not retained" in line]
    assert len(notes) == 1, notes
    assert "'20260101-widget-cache'" in notes[0] and "'20260202-widget-flush'" in notes[0]


def test_arch_006_an_existing_id_spelled_with_the_separator_is_not_refused(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "odd.md").write_text(
        f"---\nid: odd@doc\ntitle: Odd\ndate: 2026-01-01\nstatus: active\n---\n\n{V1_SENTENCE}\n",
        encoding="utf-8",
    )

    text, is_error = _complete(tmp_path, "odd@doc", V1_SENTENCE)

    assert not is_error, text


def test_a_superseded_document_named_only_by_a_redirect_is_not_offered(tmp_path, capsys):
    sessions = tmp_path / "sessions"
    _source(sessions, V1_SENTENCE, status="superseded", superseded_by="20261001-widget-cache-v2")
    (sessions / "20261001-widget-cache-v2.md").write_text(
        "---\nid: 20261001-widget-cache-v2\ntitle: Widget cache v2\ndate: 2026-10-01\n"
        "status: active\ntags: [ops]\nentities: [Cache]\n---\n\nNothing in common.\n",
        encoding="utf-8",
    )

    main(["search", "widget flush", "--store", str(tmp_path)])

    reference = _shown_reference(capsys.readouterr().out) or ""
    assert "20260101-widget-cache@" not in reference
    assert not (tmp_path / versions.VERSIONS_DIR / "20260101-widget-cache").exists()


def test_arch_003_a_missing_citation_is_named_as_the_row_wrote_it(tmp_path):
    reference = "20260101-nowhere@0123456789abcdef"
    _source(tmp_path / "sessions", V1_SENTENCE)
    _citing(tmp_path / "sessions", reference, V1_SENTENCE)

    verify = run_tool(VERIFY, tmp_path).stdout
    text, _ = _complete(tmp_path, reference, V1_SENTENCE)

    assert f"cited document {reference} is not in the store" in verify
    assert f"cited document {reference} is not in the store" in text
