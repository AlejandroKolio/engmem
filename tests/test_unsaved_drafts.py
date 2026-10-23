"""US-29: `engmem draft status` names the unsaved drafts linked to the repository the user is in;
with `--hook` it prints what a Claude Code SessionStart hook reads, or nothing, and never fails."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from support.docs import write_file
from support.git import git, requires_git

from engmem import unsaved_drafts
from engmem.cli import main

pytestmark = [requires_git, pytest.mark.usefixtures("hermetic_git")]

REPO = "svc-alpha"


def _doc(store: Path, doc_id: str, *, repos: str, status: str = "draft", date: str = "2026-09-01") -> None:
    write_file(store / "sessions", f"{doc_id}.md", (
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: {date}\nstatus: {status}\n"
        f"tags: []\nentities: []\nrepos: {repos}\nrelated: []\n---\n"
    ))


@pytest.fixture
def checkout(tmp_path, monkeypatch) -> Path:
    checkout = tmp_path / REPO
    checkout.mkdir()
    git(checkout, "init", "-q")
    monkeypatch.chdir(checkout)
    return checkout


def _status(capsys, store: Path, *extra: str) -> tuple[int, str, str]:
    code = main(["draft", "status", *extra, "--store", str(store)])
    out, err = capsys.readouterr()
    return code, out, err


def _hook(capsys, store: Path) -> dict | None:
    code, out, err = _status(capsys, store, "--hook")
    assert code == 0 and not err, (out, err)
    return json.loads(out) if out else None


def test_drafts_of_this_repository_are_named_newest_first(checkout, store, capsys):
    _doc(store, "a-old", repos=f"[{REPO}]", date="2026-08-01")
    _doc(store, "b-same-day", repos="[SVC-Alpha]")
    _doc(store, "c-same-day", repos="[' svc-alpha ']")
    _doc(store, "elsewhere", repos="[svc-beta]")
    _doc(store, "unlinked", repos="[]")
    _doc(store, "published", repos=f"[{REPO}]", status="active")
    write_file(store / "sessions", "draft-format.md", (
        "---\nid: draft-format\ntitle: Draft format decision\ndate: 2026-09-02\n"
        f"status: active\ntags: []\nentities: []\nrepos: [{REPO}]\nrelated: []\n---\n"
    ))

    code, out, _ = _status(capsys, store)

    assert code == 0
    assert out == (
        "engmem: unsaved draft(s) c-same-day, b-same-day, a-old — finish with "
        "/engmem.save.quick or leave them\n"
    )


def test_a_draft_status_in_any_spelling_is_found(checkout, store, capsys):
    write_file(store / "sessions", "quoted.md", (
        f"---\nid: quoted\ntitle: q\ndate: 2026-09-01\nstatus: \"Draft\"  # not saved yet\n"
        f"tags: []\nentities: []\nrepos: [{REPO}]\nrelated: []\n---\n"
    ))

    assert "quoted" in _status(capsys, store)[1]


def test_more_than_three_drafts_name_three_and_count_the_rest(checkout, store, capsys):
    for day in range(1, 5):
        _doc(store, f"d{day}", repos=f"[{REPO}]", date=f"2026-09-0{day}")

    assert "d4, d3, d2 +1 more —" in _status(capsys, store)[1]


def test_the_hook_prints_a_line_for_the_user_and_context_for_the_agent(checkout, store, capsys):
    _doc(store, "d1", repos=f"[{REPO}]")

    assert _hook(capsys, store) == {
        "systemMessage": "engmem: unsaved draft(s) d1 — finish with /engmem.save.quick or leave them",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "engmem: unsaved session drafts for this repository: d1. Offer to finish one "
                "with /engmem.save.quick, or leave them."
            ),
        },
    }


def test_with_no_draft_the_hook_is_silent_and_a_person_is_told(checkout, store, capsys):
    _doc(store, "published", repos=f"[{REPO}]", status="active")

    assert _hook(capsys, store) is None
    assert _status(capsys, store)[1] == f"engmem: no unsaved drafts for {REPO}\n"


def test_outside_a_checkout_the_hook_is_silent_and_a_person_gets_the_reason(
    tmp_path, store, monkeypatch, capsys
):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    assert _hook(capsys, store) is None
    code, out, _ = _status(capsys, store)
    assert code == 0 and "is not in a git checkout" in out


def _break_the_store(store: Path) -> list[str]:
    (store / "sessions").rmdir()
    (store / "sessions").write_text("not a directory", encoding="utf-8")
    return ["--store", str(store)]


def _break_the_store_choice(store: Path) -> list[str]:
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "engmem"
    config.mkdir(parents=True, exist_ok=True)
    (config / "store").write_text("relative/store\n", encoding="utf-8")
    return []


@pytest.mark.parametrize("breaks", [_break_the_store, _break_the_store_choice], ids=["store", "choice"])
def test_a_broken_store_never_reaches_the_hook_and_fails_a_person(checkout, store, capsys, breaks):
    store_args = breaks(store)

    assert main(["draft", "status", "--hook", *store_args]) == 0
    assert capsys.readouterr() == ("", "")
    assert main(["draft", "status", *store_args]) == 2


def test_an_unexpected_error_never_reaches_the_hook(checkout, store, monkeypatch, capsys):
    _doc(store, "d1", repos=f"[{REPO}]")

    def boom(*_args):
        raise RuntimeError("a bug")

    monkeypatch.setattr(unsaved_drafts, "unsaved_drafts", boom)

    assert _hook(capsys, store) is None


def test_ids_are_escaped(checkout, store, capsys):
    write_file(store / "sessions", "esc.md", (
        f"---\nid: \"esc\\x1b[2J\"\ntitle: e\ndate: 2026-09-01\nstatus: draft\n"
        f"tags: []\nentities: []\nrepos: [{REPO}]\nrelated: []\n---\n"
    ))

    out = _status(capsys, store)[1]

    assert "\x1b" not in out and "esc\\x1b[2J" in out


class _Unreadable(io.StringIO):
    def read(self, *_args):
        raise AssertionError("the hook read stdin")

    readline = read


def test_the_hook_never_reads_stdin_and_writes_nothing(checkout, store, monkeypatch, capsys):
    _doc(store, "d1", repos=f"[{REPO}]")
    monkeypatch.setattr(sys, "stdin", _Unreadable('{"transcript_path": "/x"}'))
    before = {p: p.stat().st_mtime_ns for p in store.rglob("*")}

    assert _hook(capsys, store) is not None

    assert {p: p.stat().st_mtime_ns for p in store.rglob("*")} == before
