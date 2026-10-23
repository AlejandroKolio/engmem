"""US-28: both save templates write a pitfall as Tried / Failed because / Fixed by / Files, and a
`--role lessons` search shows what was tried."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from support.docs import write_file
from support.paths import REPO_ROOT
from support.search import run_search

from engmem.output import SECTION_SNIPPET_MAX_CHARS

TEMPLATES = REPO_ROOT / "src" / "engmem" / "templates"
FULL = TEMPLATES / "engmem.save.md"
QUICK = TEMPLATES / "engmem.save.quick.md"
LABELS = ("Tried", "Failed because", "Fixed by", "Files")
FIRST_BULLET_RULE = "Nothing precedes the first bullet in the section: search shows its first sentence."
FENCE = re.compile(r"^( *)```\n(\1- Tried:.*?)^\1```$", re.MULTILINE | re.DOTALL)
SNIPPET = re.compile(r"^ +\S.*? {3}\d+\.\d KB {3}(.*)$")
DOC_ID = "20260301-ledger-import"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _lesson(text: str) -> str:
    blocks = FENCE.findall(text)
    assert len(blocks) == 1, f"expected one lesson block, found {len(blocks)}"
    return textwrap.dedent(blocks[0][1])


def _filled(lesson: str, tried: str) -> str:
    values = iter([tried, "the importer held one transaction per file.",
                   "committing every thousand rows.", "src/ledger/importer.py"])
    return re.sub(r"<[^>]+>", lambda _: next(values), lesson)


def _snippet(store: Path, capsys) -> str:
    code, out, _ = run_search(store, "ledger importer", "--role", "lessons", capsys=capsys)
    assert code == 0, out
    lines = [m.group(1) for line in out.splitlines() if (m := SNIPPET.match(line))]
    assert len(lines) == 1, out
    return lines[0]


def _store(tmp_path: Path, lessons: str) -> Path:
    sessions = tmp_path / "store" / "sessions"
    sessions.mkdir(parents=True)
    write_file(sessions, f"{DOC_ID}.md", (
        f"---\nid: {DOC_ID}\ntitle: Ledger import\ndate: 2026-03-01\nstatus: active\n"
        "tags: [ledger]\nentities: [LedgerImporter]\nrelated: []\n---\n\n"
        "## Decision Log\n\nThe ledger importer commits in batches.\n\n"
        f"## Lessons Learned\n\n{lessons}\n"
    ))
    return sessions.parent


def test_both_templates_carry_the_same_four_part_lesson():
    full, quick = _lesson(_text(FULL)), _lesson(_text(QUICK))

    assert full == quick
    assert tuple(re.findall(r"^ *-? *([A-Z][a-z]+(?: [a-z]+)?):", full, re.MULTILINE)) == LABELS


def test_each_template_keeps_its_empty_case_and_the_first_bullet_rule():
    full, quick = (" ".join(_text(path).split()) for path in (FULL, QUICK))

    assert "(`None hit.`)" in full
    assert "otherwise leave that section out." in quick
    assert FIRST_BULLET_RULE in full and FIRST_BULLET_RULE in quick


def test_a_lessons_search_shows_what_was_tried(tmp_path, capsys):
    tried = "importing the whole ledger file in one transaction."
    assert len(f"- Tried: {tried}") < SECTION_SNIPPET_MAX_CHARS
    lesson = _filled(_lesson(_text(FULL)), tried)
    store = _store(tmp_path, lesson + _filled(_lesson(_text(FULL)), "retrying the import."))

    assert _snippet(store, capsys) == f"- Tried: {tried}"


def test_a_tried_line_without_a_full_stop_still_leads_the_snippet(tmp_path, capsys):
    tried = "importing the whole ledger file in one transaction"
    store = _store(tmp_path, _filled(_lesson(_text(FULL)), tried))

    assert _snippet(store, capsys).startswith(f"- Tried: {tried} Failed because:")
