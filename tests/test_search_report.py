"""The one composer both search channels print from (contracts/output.md)."""

from __future__ import annotations

import errno
import json
import shutil
from pathlib import Path

import pytest

from support.docs import FIXTURES
from support.platform import requires_permission_enforcement

from engmem import mcp_server, search_report
from engmem.cli import main
from engmem.search_report import _assemble, _Gap, _unwritable_note, compose, render_result
from engmem.spine import load_store
from engmem.versions import RetentionError


@pytest.fixture
def store(tmp_path) -> Path:
    shutil.copytree(FIXTURES, tmp_path / "sessions")
    return tmp_path


def _rows(store: Path) -> list[dict]:
    path = store / "telemetry.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("role", [None, "decisions"])
def test_rendering_a_result_touches_nothing_on_disk(store, role):
    """The pure half writes nothing; only `compose` logs, so measuring a result never adds a
    telemetry row to the store it measures."""
    rendered = render_result(load_store(store / "sessions").docs, "platform", role)

    assert rendered.text
    assert not (store / "telemetry.jsonl").exists()


@pytest.mark.parametrize("role", [None, "decisions"])
@pytest.mark.parametrize("channel", ["cli", "mcp"])
def test_composing_logs_one_row_measuring_exactly_the_rendered_result(store, role, channel):
    loaded = load_store(store / "sessions")

    text = compose(store, loaded, [], [], "platform", role, "s1", channel)

    (row,) = _rows(store)
    rendered = render_result(loaded.docs, "platform", role)
    assert row["channel"] == channel
    assert row["session_id"] == "s1"
    assert row["context_bytes"] == len(rendered.text.encode("utf-8"))
    assert rendered.text in text


def test_only_the_unattributed_note_differs_between_channels(store):
    loaded = load_store(store / "sessions")

    cli = compose(store, loaded, [], [], "platform", None, None, "cli")
    mcp = compose(store, loaded, [], [], "platform", None, None, "mcp")

    assert cli.splitlines()[:-1] == mcp.splitlines()[:-1]
    assert "--session" in cli.splitlines()[-1] and "session_id" in mcp.splitlines()[-1]


# ---------------------------------------------------------------------------
# One note for an unwritable store (spec 002 US-3; contracts/output.md)
# ---------------------------------------------------------------------------


def _gap(record, error, ids=()):
    return _Gap(record, error, f"note: own {record} line", ids)


def _denied(name):
    return PermissionError(13, "Permission denied", name)


def test_records_the_store_refused_fold_into_one_note_in_a_fixed_order():
    gaps = [_gap("versions", _denied("versions"), ("'a'", "'b'")),
            _gap("telemetry", _denied("telemetry.jsonl")), _gap("cost", _denied("cost.jsonl"))]

    text = _assemble(["result"], ["docs: 1"], gaps, ["trailer"])

    assert text.splitlines() == [
        "result",
        "docs: 1",
        "note: the store refused writing — telemetry, cost and versions of 'a', 'b' were not "
        "recorded, so a quote from them can only be checked against the current text "
        "([Errno 13] Permission denied: 'versions')",
        "trailer",
    ]


def test_the_fold_names_each_refused_document_once():
    gaps = [_gap("versions", _denied("x"), ("'a'",)),
            _gap("versions", _denied("y"), ("'b'", "'a'")),
            _gap("telemetry", _denied("telemetry.jsonl"))]

    note = _unwritable_note(gaps)

    assert "telemetry and versions of 'a', 'b' were not recorded, so a quote from them" in note


def test_a_read_only_file_system_folds_like_a_denied_write():
    rofs = OSError(errno.EROFS, "Read-only file system", "telemetry.jsonl")

    note = _unwritable_note([_gap("telemetry", rofs), _gap("cost", rofs)])

    assert note.startswith(
        "note: the store refused writing — telemetry and cost were not recorded ("
    )
    assert "a quote" not in note


@pytest.mark.parametrize("winerror", [32, 33])
def test_a_file_another_process_holds_open_is_not_a_refusing_store(winerror):
    in_use = _denied("telemetry.jsonl")
    in_use.winerror = winerror

    assert _unwritable_note([_gap("telemetry", in_use), _gap("cost", in_use)]) is None


def test_another_cause_keeps_its_own_line_where_it_stood():
    gaps = [_gap("versions", RetentionError("'versions' is not a plain directory"), ("'a'",)),
            _gap("telemetry", _denied("telemetry.jsonl")), _gap("cost", _denied("cost.jsonl")),
            _gap("cost", IsADirectoryError(21, "Is a directory", "cost.jsonl"))]

    lines = _assemble(["result"], ["docs: 1"], gaps, []).splitlines()

    assert lines[:3] == ["result", "note: own versions line", "docs: 1"]
    assert lines[3].startswith("note: the store refused writing — telemetry and cost were not")
    assert lines[4:] == ["note: own cost line"]


@pytest.mark.parametrize("gaps", [
    pytest.param([], id="no-gap"),
    pytest.param([("telemetry", _denied("telemetry.jsonl"))], id="one-record-refused"),
    pytest.param([("versions", _denied("a")), ("versions", _denied("b"))], id="one-record-twice"),
])
def test_fewer_than_two_refused_records_print_as_they_always_did(gaps):
    built = [_gap(record, error) for record, error in gaps]
    versions = [g.note for g in built if g.record == "versions"]
    others = [g.note for g in built if g.record != "versions"]

    text = _assemble(["result"], ["docs: 1"], built, ["trailer"])

    assert text.splitlines() == ["result", *versions, "docs: 1", *others, "trailer"]


def test_a_cost_row_written_after_a_fold_measures_the_folded_text(store, capsys, monkeypatch):
    """Telemetry and versions refused, cost written: the one case the row counts the folded line."""

    def refused(*args, **kwargs):
        raise _denied("versions")

    monkeypatch.setattr(search_report, "log_search", lambda *a, **k: _denied("telemetry.jsonl"))
    monkeypatch.setattr(search_report, "retain", refused)

    main(["search", "ResponseCache", "--store", str(store), "--session", "s1"])

    out = capsys.readouterr().out
    assert "note: the store refused writing — telemetry and versions of " in out
    cost_rows = (store / "cost.jsonl").read_text(encoding="utf-8").splitlines()
    (row,) = [json.loads(line) for line in cost_rows]
    assert row["bytes"] == len(out.removesuffix("\n").encode("utf-8"))


@requires_permission_enforcement
@pytest.mark.parametrize("channel", ["cli", "mcp"])
def test_an_unwritable_store_prints_one_note_on_both_channels(store, capsys, channel):
    store.chmod(0o500)
    try:
        if channel == "cli":
            main(["search", "ResponseCache", "--store", str(store)])
            text = capsys.readouterr().out
        else:
            text, _ = mcp_server._run_search_for_tool(store, "ResponseCache")
    finally:
        store.chmod(0o700)

    notes = [line for line in text.splitlines() if line.startswith("note:")]
    assert notes[0].startswith(
        "note: the store refused writing — telemetry, cost and versions of "
        "'1000001-response-cache'"
    )
    assert [n for n in notes[1:] if "unattributed" not in n] == []


@requires_permission_enforcement
def test_a_read_only_server_on_an_unwritable_store_names_telemetry_alone(store):
    store.chmod(0o500)
    try:
        text, _ = mcp_server._run_search_for_tool(store, "ResponseCache", read_only=True)
    finally:
        store.chmod(0o700)

    (note,) = [n for n in text.splitlines() if n.startswith("note:") and "unattributed" not in n]
    assert note.startswith("note: telemetry not recorded (")
