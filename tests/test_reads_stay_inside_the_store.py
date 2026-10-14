"""A link inside the store never hands the agent a file from outside it (US-22)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import requires_symlinks, write_file
from mcp_harness import _run, _text, _tools_call_msg

from engmem import confined, spine
from engmem.cli import main
from engmem.confined import OutsideStoreError
from engmem.spine import load_store
from engmem.staging import version_of

CANARY = "CANARY-OUTSIDE-THE-STORE"
ALLOWED = "20260101-idempotency-adr"
LINKED = "20260102-retry-advice"


def _doc(doc_id: str, decision: str) -> str:
    return (
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-01-01\nstatus: active\n"
        f"entities: [PaymentCommand, IdempotencyKey]\nmode: daily\n---\n\n"
        f"## Decision Log\n\n{decision}\n"
    )


@pytest.fixture
def store(tmp_path) -> Path:
    """A store with one ADR of its own, and an outside file with the opposite advice."""
    sessions = tmp_path / "store" / "sessions"
    sessions.mkdir(parents=True)
    write_file(sessions, f"{ALLOWED}.md", _doc(ALLOWED, "A repeated PaymentCommand must not "
                                                       "repeat its side effect."))
    outside = tmp_path / "outside"
    outside.mkdir()
    write_file(outside, f"{LINKED}.md", _doc(LINKED, f"{CANARY}: retry a PaymentCommand blindly."))
    return tmp_path / "store"


def _outside(store: Path) -> Path:
    return store.parent / "outside"


def _cli(capsys, store: Path, *args: str) -> tuple[int, str]:
    code = main([*args, "--store", str(store)])
    out, err = capsys.readouterr()
    return code, out + err


def _link_document(store: Path) -> None:
    (store / "sessions" / f"{LINKED}.md").symlink_to(_outside(store) / f"{LINKED}.md")


def _link_sessions(store: Path) -> None:
    """`sessions/` itself replaced by a link to a directory that holds both documents."""
    elsewhere = _outside(store)
    os.replace(store / "sessions" / f"{ALLOWED}.md", elsewhere / f"{ALLOWED}.md")
    (store / "sessions").rmdir()
    (store / "sessions").symlink_to(elsewhere, target_is_directory=True)


@pytest.fixture(params=["openat", "checked"])
def reader(request, monkeypatch):
    """Both read paths: `dir_fd` opens where the platform has them, per-part checks otherwise."""
    if request.param == "openat" and not confined._OPENAT:
        pytest.skip("this platform has no dir_fd opens")
    monkeypatch.setattr(confined, "_OPENAT", request.param == "openat")
    return request.param


# ---------------------------------------------------------------------------
# the reader itself
# ---------------------------------------------------------------------------


@requires_symlinks
def test_a_linked_file_is_refused_without_reading_it(store, reader):
    _link_document(store)

    with pytest.raises(OutsideStoreError, match=f"sessions/{LINKED}.md passes through a link"):
        confined.read_bytes(store / "sessions" / f"{LINKED}.md", confined.SESSION_DEPTH)


@requires_symlinks
def test_a_linked_directory_is_refused_for_the_files_below_it_and_its_listing(store, reader):
    _link_sessions(store)

    with pytest.raises(OutsideStoreError, match="passes through a link"):
        confined.read_bytes(store / "sessions" / f"{LINKED}.md", confined.SESSION_DEPTH)
    with pytest.raises(OutsideStoreError, match="passes through a link"):
        confined.list_names(store / "sessions", confined.SESSIONS_DEPTH)


@requires_symlinks
def test_a_linked_store_root_is_the_owners_choice_and_is_followed(store, tmp_path, reader):
    root_link = tmp_path / "synced-store"
    root_link.symlink_to(store, target_is_directory=True)

    data = confined.read_bytes(root_link / "sessions" / f"{ALLOWED}.md", confined.SESSION_DEPTH)

    assert b"must not repeat its side effect" in data


def test_a_directory_in_a_documents_place_is_not_read(store, reader):
    (store / "sessions" / "folder.md").mkdir()

    with pytest.raises(IsADirectoryError):
        confined.read_bytes(store / "sessions" / "folder.md", confined.SESSION_DEPTH)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_in_a_documents_place_is_refused_instead_of_blocking(store):
    os.mkfifo(store / "sessions" / "pipe.md")

    with pytest.raises(OSError, match="Not a regular file"):
        confined.read_bytes(store / "sessions" / "pipe.md", confined.SESSION_DEPTH)


def test_text_reads_translate_line_endings_like_read_text(store, reader):
    path = store / "sessions" / "crlf.md"
    path.write_bytes(b"\xef\xbb\xbfone\r\ntwo\rthree\n")

    assert confined.read_utf8(path, confined.SESSION_DEPTH) == path.read_text(
        encoding="utf-8-sig"
    )


# ---------------------------------------------------------------------------
# AC-22.1 / AC-22.5: search, read and export preview
# ---------------------------------------------------------------------------


@requires_symlinks
def test_search_shows_the_stores_own_adr_and_never_the_linked_advice(store, capsys):
    _link_document(store)

    code, output = _cli(capsys, store, "search", "PaymentCommand IdempotencyKey")

    assert code == 0
    assert ALLOWED in output
    assert CANARY not in output and "retry a PaymentCommand blindly" not in output
    assert f"sessions/{LINKED}.md passes through a link" in output


@requires_symlinks
@pytest.mark.parametrize("command", [["read", LINKED], ["export", LINKED]])
def test_read_and_export_refuse_the_linked_document_and_say_why(store, capsys, command):
    _link_document(store)

    code, output = _cli(capsys, store, *command)

    assert code == 2
    assert CANARY not in output
    assert f"no document with id '{LINKED}' in the store -- its file was not loaded" in output
    assert "passes through a link" in output


@requires_symlinks
def test_a_linked_sessions_directory_yields_nothing_from_behind_it(store, capsys):
    _link_sessions(store)

    code, output = _cli(capsys, store, "search", "PaymentCommand IdempotencyKey")

    assert CANARY not in output and ALLOWED not in output.replace(str(store), "")
    assert "sessions passes through a link" in output
    assert code == 0


# ---------------------------------------------------------------------------
# AC-22.2: retained versions, and the read-only MCP server
# ---------------------------------------------------------------------------


@requires_symlinks
@pytest.mark.parametrize("linked", ["file", "id-directory", "versions-directory"])
def test_a_retained_version_behind_a_link_is_not_served(store, capsys, linked):
    """The link's target hashes to the version it is named by, so only the boundary stops it."""
    outside_copy = _doc(ALLOWED, f"{CANARY}: the cited version said retry blindly.").encode()
    version = version_of(outside_copy)
    copies = _outside(store) / "versions" / ALLOWED
    copies.mkdir(parents=True)
    (copies / f"{version}.md").write_bytes(outside_copy)
    versions = store / "versions"
    if linked == "versions-directory":
        versions.symlink_to(_outside(store) / "versions", target_is_directory=True)
    elif linked == "id-directory":
        versions.mkdir()
        (versions / ALLOWED).symlink_to(copies, target_is_directory=True)
    else:
        (versions / ALLOWED).mkdir(parents=True)
        (versions / ALLOWED / f"{version}.md").symlink_to(copies / f"{version}.md")

    code, output = _cli(capsys, store, "read", f"{ALLOWED}@{version}")

    assert code == 2
    assert CANARY not in output
    assert "passes through a link" in output


@requires_symlinks
def test_the_read_only_mcp_search_never_returns_the_linked_advice(store):
    _link_document(store)

    _code, responses, _raw = _run(
        store,
        _tools_call_msg(1, arguments={"query": "PaymentCommand IdempotencyKey"}),
        read_only=True,
    )
    text = _text(responses[0]["result"])

    assert ALLOWED in text
    assert CANARY not in text and "1 failed to load" in text


@requires_symlinks
def test_the_mcp_read_refuses_the_linked_document_and_says_why(store):
    _link_document(store)

    _code, responses, _raw = _run(
        store, _tools_call_msg(1, name="engmem_read", arguments={"id": LINKED})
    )
    result = responses[0]["result"]

    assert result.get("isError") is True
    assert CANARY not in _text(result)
    assert "passes through a link" in _text(result)


# ---------------------------------------------------------------------------
# AC-22.3: a linked store root
# ---------------------------------------------------------------------------


@requires_symlinks
def test_a_store_chosen_through_a_link_is_searched_as_usual(store, tmp_path, capsys):
    root_link = tmp_path / "synced-store"
    root_link.symlink_to(store, target_is_directory=True)

    code, output = _cli(capsys, root_link, "search", "PaymentCommand IdempotencyKey")

    assert code == 0 and ALLOWED in output and "passes through a link" not in output


@requires_symlinks
def test_a_dangling_store_link_fails_instead_of_falling_back_to_another_store(tmp_path, capsys):
    root_link = tmp_path / "synced-store"
    root_link.symlink_to(tmp_path / "unmounted", target_is_directory=True)

    code, output = _cli(capsys, root_link, "search", "PaymentCommand")

    assert code == 2 and "store not found" in output


# ---------------------------------------------------------------------------
# AC-22.4: the path changes between the listing and the open
# ---------------------------------------------------------------------------


@requires_symlinks
@pytest.mark.parametrize("swapped", ["document", "sessions-directory"])
def test_a_link_swapped_in_after_the_listing_is_refused(store, monkeypatch, reader, swapped):
    """`parse_document` stats a file just before it opens it: the swap lands in between."""
    planted = store / "sessions" / f"{LINKED}.md"
    write_file(store / "sessions", f"{LINKED}.md", _doc(LINKED, "Our own text."))
    real_identity_for = spine.identity_for

    def swap_then_stat(path):
        if path == planted and not os.path.islink(planted):
            planted.unlink()
            if swapped == "document":
                planted.symlink_to(_outside(store) / f"{LINKED}.md")
            else:
                _link_sessions(store)
        return real_identity_for(path)

    monkeypatch.setattr(spine, "identity_for", swap_then_stat)

    result = load_store(store / "sessions")

    assert LINKED not in {d.id for d in result.docs}
    assert any("passes through a link" in p.message for p in result.errors)
    assert not any(CANARY in d.body for d in result.docs)


@requires_symlinks
def test_without_dir_fd_a_swap_between_the_check_and_the_open_is_caught(store, monkeypatch):
    monkeypatch.setattr(confined, "_OPENAT", False)
    target = store / "sessions" / f"{ALLOWED}.md"
    real_check = confined._check_components

    def check_then_swap(path, depth):
        checked = real_check(path, depth)
        target.unlink()
        target.symlink_to(_outside(store) / f"{LINKED}.md")
        return checked

    monkeypatch.setattr(confined, "_check_components", check_then_swap)

    with pytest.raises(OutsideStoreError, match="passes through a link"):
        confined.read_bytes(target, confined.SESSION_DEPTH)


# ---------------------------------------------------------------------------
# AC-22.2: a Windows junction, which `is_symlink()` does not report
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="junctions exist only on Windows")
def test_a_junctioned_sessions_directory_is_refused_on_windows(store, capsys):
    import _winapi

    elsewhere = _outside(store)
    os.replace(store / "sessions" / f"{ALLOWED}.md", elsewhere / f"{ALLOWED}.md")
    (store / "sessions").rmdir()
    _winapi.CreateJunction(str(elsewhere), str(store / "sessions"))

    code, output = _cli(capsys, store, "search", "PaymentCommand IdempotencyKey")

    assert CANARY not in output
    assert "sessions passes through a link or junction" in output
    assert code == 0


# ---------------------------------------------------------------------------
# a document loaded as a plain file, then swapped for a link before a later re-read
# ---------------------------------------------------------------------------


def _loaded_then_linked(store: Path):
    """`ALLOWED` as loaded, after which its file is replaced by a link to the outside file."""
    result = load_store(store / "sessions")
    doc = next(d for d in result.docs if d.id == ALLOWED)
    doc.path.unlink()
    doc.path.symlink_to(_outside(store) / f"{LINKED}.md")
    return result, doc


@requires_symlinks
def test_an_export_rereads_inside_the_store(store):
    from engmem.export import ExportError, prepare_export

    result, _doc = _loaded_then_linked(store)

    with pytest.raises(ExportError, match="passes through a link") as raised:
        prepare_export(store, result, ALLOWED)
    assert CANARY not in str(raised.value)


@requires_symlinks
def test_a_version_is_never_retained_from_behind_a_link(store):
    from engmem.versions import retain

    _result, doc = _loaded_then_linked(store)

    with pytest.raises(OutsideStoreError):
        retain(store, doc)
    assert not (store / "versions").exists() or not [
        p for p in (store / "versions").rglob("*.md") if CANARY.encode() in p.read_bytes()
    ]


@requires_symlinks
def test_a_versions_existing_copy_is_never_compared_through_a_link(store, monkeypatch):
    """A copy already at the version's name is checked before it is reused or repaired."""
    from engmem.versions import retain

    doc = next(d for d in load_store(store / "sessions").docs if d.id == ALLOWED)
    version = version_of(doc.path.read_bytes())
    (store / "versions" / ALLOWED).mkdir(parents=True)
    copy = store / "versions" / ALLOWED / f"{version}.md"
    copy.symlink_to(_outside(store) / f"{LINKED}.md")
    real_is_symlink = Path.is_symlink
    # past the name check, as a link landing right after it would be
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: False if self == copy else real_is_symlink(self)
    )

    with pytest.raises(OutsideStoreError):
        retain(store, doc)


@requires_symlinks
def test_the_current_version_of_a_linked_document_is_unknown_not_its_targets_hash(store):
    from engmem.read_report import _current_version

    _result, doc = _loaded_then_linked(store)

    assert _current_version(doc) is None


@requires_symlinks
def test_the_reuse_log_line_number_is_not_read_through_a_link(store):
    from engmem.gate1 import _line_number

    _result, doc = _loaded_then_linked(store)

    with pytest.raises(OutsideStoreError):
        _line_number(doc.path, "| prior-doc |")


@requires_symlinks
def test_backfill_proposals_reread_inside_the_store(store):
    from engmem.backfill import propose_backfill

    path = store / "sessions" / "20260103-legacy.md"
    path.write_text("# Legacy note\n\nPaymentCommand retries.\n", encoding="utf-8")
    doc = next(d for d in load_store(store / "sessions").docs if d.id == "20260103-legacy")
    path.unlink()
    path.symlink_to(_outside(store) / f"{LINKED}.md")

    with pytest.raises(OutsideStoreError):
        propose_backfill(doc)


@requires_symlinks
def test_a_completion_version_check_does_not_read_through_a_link(store):
    from engmem.drafts import MCP, WriteRefused, refuse_unless_version_read

    _result, doc = _loaded_then_linked(store)
    outside_version = version_of((_outside(store) / f"{LINKED}.md").read_bytes())

    with pytest.raises(WriteRefused, match="could not be re-read"):
        refuse_unless_version_read(
            doc.path, ALLOWED, outside_version, MCP, action="complete", tool="t", redo="r"
        )


@requires_symlinks
def test_supersede_never_rebuilds_from_a_file_swapped_in_after_its_parse(store, monkeypatch):
    from engmem import mcp_server

    path = store / "sessions" / f"{ALLOWED}.md"
    read = version_of(path.read_bytes())
    real_parse = mcp_server.parse_document

    def parse_then_link(target):
        doc = real_parse(target)
        if target == path:
            path.unlink()
            path.symlink_to(_outside(store) / f"{LINKED}.md")
        return doc

    monkeypatch.setattr(mcp_server, "parse_document", parse_then_link)

    _code, responses, _raw = _run(
        store,
        _tools_call_msg(
            1,
            name="engmem_mark_superseded",
            arguments={
                "id": ALLOWED,
                "superseded_by": "20260201-idempotency-adr-v2",
                "expected_version": read,
            },
        ),
    )
    result = responses[0]["result"]

    assert result.get("isError") is True
    assert "passes through a link" in _text(result) and CANARY not in _text(result)
    assert path.is_symlink(), "the link was neither followed nor written over"


# ---------------------------------------------------------------------------
# which Windows reparse points count as links
# ---------------------------------------------------------------------------


# the `stat.IO_REPARSE_TAG_*` names exist only on Windows: junction, symlink, cloud placeholder
@pytest.mark.parametrize(
    "mode, tag, linked",
    [
        pytest.param(stat.S_IFDIR, 0xA0000003, True, id="junction"),
        pytest.param(stat.S_IFREG, 0xA000000C, True, id="symlink-tag"),
        pytest.param(stat.S_IFLNK, 0, True, id="posix-symlink"),
        # a OneDrive Files On-Demand placeholder: a reparse point, but the file itself
        pytest.param(stat.S_IFREG, 0x9000601A, False, id="cloud-placeholder"),
        pytest.param(stat.S_IFREG, 0, False, id="plain-file"),
    ],
)
def test_only_name_surrogate_reparse_points_count_as_links(mode, tag, linked):
    st = SimpleNamespace(
        st_mode=mode | 0o644,
        st_reparse_tag=tag,
        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT if tag else 0,
    )

    assert confined._is_link(st) is linked


def test_without_dir_fd_a_directory_is_refused_before_it_is_opened(store, monkeypatch):
    """Windows opens a directory with PermissionError, so the type is checked before the open."""
    monkeypatch.setattr(confined, "_OPENAT", False)
    folder = store / "sessions" / "folder.md"
    folder.mkdir()

    def no_open(*_args, **_kwargs):
        raise AssertionError("opened before its type was checked")

    monkeypatch.setattr(confined, "open", no_open, raising=False)

    with pytest.raises(IsADirectoryError):
        confined.read_bytes(folder, confined.SESSION_DEPTH)


@requires_symlinks
def test_a_linked_sessions_directory_is_not_walked_for_stray_files(store, capsys):
    """Its subtree is outside the store: naming its files as documents to move leaks them."""
    _link_sessions(store)
    nested = _outside(store) / "private-subfolder"
    nested.mkdir()
    write_file(nested, "PRIVATE-NAME.md", "# private\n")

    _code, output = _cli(capsys, store, "search", "PaymentCommand IdempotencyKey")

    assert "PRIVATE-NAME" not in output and "private-subfolder" not in output
    assert "sessions passes through a link" in output
