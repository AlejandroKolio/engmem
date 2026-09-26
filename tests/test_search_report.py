"""The one composer both search channels print from (contracts/output.md)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import FIXTURES

from engmem.search_report import compose, render_result
from engmem.spine import load_store


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
