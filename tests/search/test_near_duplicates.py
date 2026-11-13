"""Active notes that share a title and entities are one subject over time: a search shows the
newest in their place and names the others (contracts/output.md, "The newest of near-duplicates")."""

from __future__ import annotations

import re

import pytest

from support.docs import write_file
from support.search import cli_search, mcp_search

from engmem.output import TOP_N, render_search_results, surfaced_ids
from engmem.scoring import search
from engmem.spine import load_store
from engmem.versions import VERSIONS_DIR


def _store(tmp_path, *rows):
    """Each row is `(id, date, body)` or a dict naming `title`, `entities`, `status` and
    `superseded_by` too; the default title and entities are one subject's."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    for row in rows:
        spec = row if isinstance(row, dict) else dict(zip(("id", "date", "body"), row))
        spec = {"title": "Quokka limit", "entities": "[QuokkaGate]", "status": "active",
                "superseded_by": "", **spec}
        _write(sessions, spec)
    for n in range(12):
        _write(sessions, {"id": f"filler-{n}", "date": "2022-01-01", "title": f"Filler {n}",
                          "entities": "[Filler]", "status": "active", "superseded_by": "",
                          "body": "Unrelated notes about the deploy window."})
    return load_store(sessions).docs


def _write(sessions, spec):
    write_file(sessions, f"{spec['id']}.md", (
        f"---\nid: {spec['id']}\ntitle: {spec['title']}\ndate: {spec['date']}\n"
        f"task_date: {spec['date']}\nstatus: {spec['status']}\nsuperseded_by: {spec['superseded_by']}\n"
        f"tags: []\nentities: {spec['entities']}\nrepos: {spec.get('repos', '[]')}\nrelated: []\n---\n\n"
        f"## Decision Log\n\n{spec['body']}\n"
    ))


def _shown(text: str) -> list[str]:
    return re.findall(r"^### (\S+)", text, re.MULTILINE)


def _render(docs, query="quokka limit"):
    outcome = search(docs, query)
    return render_search_results(outcome, docs), outcome


def test_the_newest_of_near_duplicates_is_shown_in_place_of_the_best_scored_one(tmp_path):
    docs = _store(
        tmp_path,
        ("q-2024", "2024-03-01", "The quokka limit is 10. Quokka limit, quokka limit, quokka limit."),
        ("q-2025", "2025-03-01", "The quokka limit is 20. Quokka limit again."),
        ("q-2026", "2026-03-01", "The limit is 30."),
        {"id": "other", "date": "2023-01-01", "title": "Wallaby gate", "entities": "[WallabyGate]",
         "body": "The quokka limit is tracked elsewhere; quokka limit notes."},
    )

    text, outcome = _render(docs)

    assert outcome.hits[0].doc.id == "q-2024"
    assert _shown(text)[0] == "q-2026"
    assert "### q-2024" not in text and "### q-2025" not in text
    assert "newest of 3 notes with this title and entities; older: q-2025 (2025-03-01), q-2024 (2024-03-01)\n" in text
    assert "other" in _shown(text)


def test_notes_with_another_title_or_other_entities_are_not_near_duplicates(tmp_path):
    docs = _store(
        tmp_path,
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        {"id": "q-title", "date": "2025-03-01", "title": "Quokka limit for exports", "body": "Quokka limit 20."},
        {"id": "q-entity", "date": "2026-03-01", "entities": "[QuokkaExporter]", "body": "Quokka limit 30."},
    )

    text, _ = _render(docs)

    assert sorted(_shown(text)) == ["q-2024", "q-entity", "q-title"]
    assert "newest of" not in text


def test_a_title_differing_only_in_case_and_spacing_is_the_same_title(tmp_path):
    docs = _store(
        tmp_path,
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        {"id": "q-2025", "date": "2025-03-01", "title": "quokka   LIMIT", "body": "Quokka limit 20."},
    )

    text, _ = _render(docs)

    assert _shown(text) == ["q-2025"]
    assert "older: q-2024" in text


def test_a_note_without_entities_is_never_folded(tmp_path):
    docs = _store(
        tmp_path,
        {"id": "q-2024", "date": "2024-03-01", "entities": "[]", "body": "The quokka limit is 10."},
        {"id": "q-2025", "date": "2025-03-01", "entities": "[]", "body": "The quokka limit is 20."},
    )

    text, _ = _render(docs)

    assert sorted(_shown(text)) == ["q-2024", "q-2025"]


def test_a_redirect_ending_at_an_older_near_duplicate_points_at_the_newest(tmp_path):
    docs = _store(
        tmp_path,
        {"id": "q-2023", "date": "2023-03-01", "status": "superseded", "superseded_by": "q-2024",
         "body": "The quokka limit is 5. Quokka limit, quokka limit, quokka limit, quokka limit."},
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        ("q-2026", "2026-03-01", "The quokka limit is 30."),
    )

    text, outcome = _render(docs)

    assert "q-2023: superseded by q-2024 (older than q-2026)" in text
    assert _shown(text) == ["q-2026"]
    assert "older: q-2024 (2024-03-01)\n" in text
    assert surfaced_ids(outcome) == ["q-2026", "q-2023", "q-2024"]


@pytest.mark.parametrize("channel", [pytest.param(cli_search, id="cli"), pytest.param(mcp_search, id="mcp")])
def test_an_older_note_a_redirect_ends_at_gets_no_cite_as_and_no_retained_version(tmp_path, capsys, channel):
    _store(
        tmp_path,
        {"id": "q-2023", "date": "2023-03-01", "status": "superseded", "superseded_by": "q-2024",
         "body": "The quokka limit is 5. Quokka limit, quokka limit, quokka limit, quokka limit."},
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        ("q-2026", "2026-03-01", "The quokka limit is 30."),
    )

    text = channel(tmp_path, "quokka limit", capsys)

    (cite,) = [line for line in text.splitlines() if line.startswith("cite as (Reuse Log prior-doc): ")]
    assert [ref.split("@")[0] for ref in cite.split(": ", 1)[1].split(", ")] == ["q-2026"]
    assert (tmp_path / VERSIONS_DIR / "q-2026").is_dir()
    assert not (tmp_path / VERSIONS_DIR / "q-2024").exists()


def test_a_redirect_ending_at_the_shown_newest_reads_as_before(tmp_path):
    docs = _store(
        tmp_path,
        {"id": "q-2023", "date": "2023-03-01", "status": "superseded", "superseded_by": "q-2026",
         "body": "The quokka limit is 5. Quokka limit, quokka limit, quokka limit, quokka limit."},
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        ("q-2026", "2026-03-01", "The quokka limit is 30."),
    )

    text, _ = _render(docs)

    assert "q-2023: superseded by q-2026\n" in text + "\n"
    assert _shown(text) == ["q-2026"]


def test_a_redirect_that_leaves_the_group_keeps_its_line_and_successor(tmp_path):
    docs = _store(
        tmp_path,
        {"id": "q-2023", "date": "2023-03-01", "status": "superseded", "superseded_by": "renamed",
         "body": "The quokka limit is 5. Quokka limit, quokka limit, quokka limit, quokka limit."},
        {"id": "renamed", "date": "2025-06-01", "title": "Wombat ceiling", "entities": "[WombatGate]",
         "body": "The ceiling is 40."},
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        ("q-2026", "2026-03-01", "The quokka limit is 30."),
    )

    text, _ = _render(docs)

    assert "q-2023: superseded by renamed" in text
    assert "### renamed (successor)" in text
    assert "newest of 2 notes with this title and entities; older: q-2024 (2024-03-01)\n" in text


def test_the_withheld_count_leaves_out_the_named_notes(tmp_path):
    rows = [(f"q-{year}", f"{year}-03-01", f"The quokka limit is {year}.") for year in range(2018, 2026)]
    others = [
        {"id": f"other-{n}", "date": "2022-01-01", "title": f"Quokka limit review {n}", "entities": f"[Other{n}]",
         "body": "A quokka limit was discussed."}
        for n in range(TOP_N + 1)
    ]
    docs = _store(tmp_path, *rows, *others)

    text, outcome = _render(docs)

    assert len(outcome.hits) == 8 + TOP_N + 1
    assert _shown(text)[0] == "q-2025" and len(_shown(text)) == TOP_N
    assert f"(2 more document(s) matched below the top {TOP_N}" in text
    assert ("newest of 8 notes with this title and entities; older: q-2024 (2024-03-01), q-2023 (2023-03-01), "
            "q-2022 (2022-03-01) and 4 more\n") in text


def test_folded_notes_are_named_but_not_surfaced(tmp_path):
    docs = _store(
        tmp_path,
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        ("q-2026", "2026-03-01", "The quokka limit is 30."),
    )

    _, outcome = _render(docs)

    assert surfaced_ids(outcome) == ["q-2026"]


def test_notes_of_one_date_keep_the_best_scored_one(tmp_path):
    docs = _store(
        tmp_path,
        ("q-a", "2026-03-01", "The quokka limit is 10."),
        ("q-b", "2026-03-01", "The quokka limit is 20. Quokka limit, quokka limit, quokka limit."),
    )

    text, outcome = _render(docs)

    assert _shown(text) == [outcome.hits[0].doc.id]


def test_a_weak_candidate_never_folds_into_a_reliable_find(tmp_path):
    from engmem.scoring import Hit, SearchOutcome, Strength

    docs = _store(
        tmp_path,
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        ("q-2026", "2026-03-01", "The quokka limit is 30."),
    )
    by_id = {doc.id: doc for doc in docs}
    outcome = SearchOutcome(
        hits=[
            Hit(doc=by_id["q-2024"], score=5.0, strength=Strength(False, ("quokka", "limit"), 2)),
            Hit(doc=by_id["q-2026"], score=1.0, strength=Strength(True, ("quokka",), 2)),
        ],
        ambiguous=False,
        superseded_notes=[],
    )

    text = render_search_results(outcome, docs)

    assert _shown(text) == ["q-2024", "q-2026"]
    assert "newest of" not in text


def test_a_redirect_must_outrank_the_place_the_last_group_holds_not_its_newest_note(tmp_path):
    from engmem.scoring import Hit, SearchOutcome, Strength, SupersededNote

    docs = _store(
        tmp_path,
        {"id": "a", "date": "2026-01-01", "title": "A", "entities": "[A]", "body": "a"},
        {"id": "b", "date": "2026-01-01", "title": "B", "entities": "[B]", "body": "b"},
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        ("q-2026", "2026-03-01", "The quokka limit is 30."),
        {"id": "stale", "date": "2023-01-01", "title": "Stale", "entities": "[S]", "status": "superseded",
         "superseded_by": "a", "body": "s"},
    )
    by_id = {doc.id: doc for doc in docs}
    reliable = Strength(False, ("quokka", "limit"), 2)
    outcome = SearchOutcome(
        hits=[Hit(doc=by_id[i], score=s, strength=reliable)
              for i, s in (("a", 9.0), ("b", 8.0), ("q-2024", 7.0), ("q-2026", 1.0))],
        ambiguous=False,
        superseded_notes=[SupersededNote(doc=by_id["stale"], successor=by_id["a"], score=5.0, strength=reliable)],
    )

    text = render_search_results(outcome, docs)

    assert _shown(text) == ["a", "b", "q-2026"]
    assert "superseded by" not in text


def test_notes_linked_to_other_repositories_are_not_near_duplicates(tmp_path):
    docs = _store(
        tmp_path,
        {"id": "q-billing", "date": "2024-03-01", "repos": "[billing]", "body": "The quokka limit is 10."},
        {"id": "q-search", "date": "2026-03-01", "repos": "[search]", "body": "The quokka limit is 30."},
    )

    text, _ = _render(docs)

    assert sorted(_shown(text)) == ["q-billing", "q-search"]
    assert "newest of" not in text


def test_repository_names_compare_as_a_scope_compares_them(tmp_path):
    docs = _store(
        tmp_path,
        {"id": "q-2024", "date": "2024-03-01", "repos": "[Billing]", "body": "The quokka limit is 10."},
        {"id": "q-2026", "date": "2026-03-01", "repos": "[' billing ']", "body": "The quokka limit is 30."},
    )

    text, _ = _render(docs)

    assert _shown(text) == ["q-2026"]


def test_the_older_line_dates_each_named_note_newest_first_whatever_their_rank(tmp_path):
    docs = _store(
        tmp_path,
        ("q-2021", "2021-06-19", "The quokka limit is 1. Quokka limit, quokka limit, quokka limit, quokka limit."),
        ("q-2025", "2025-08-04", "The quokka limit is 5."),
        ("q-2023", "2023-01-02", "The quokka limit is 3. Quokka limit, quokka limit."),
        ("q-2026", "2026-01-01", "The limit is 6."),
    )

    text, outcome = _render(docs)

    assert [hit.doc.id for hit in outcome.hits][:3] == ["q-2021", "q-2023", "q-2025"]
    assert "older: q-2025 (2025-08-04), q-2023 (2023-01-02), q-2021 (2021-06-19)\n" in text


SESSION = "20260901-task"


def _session_searched_and_read_an_older_note(tmp_path, capsys) -> None:
    from engmem.cli import main

    _store(
        tmp_path,
        ("q-2024", "2024-03-01", "The quokka limit is 10."),
        ("q-2026", "2026-03-01", "The quokka limit is 30."),
        {"id": SESSION, "date": "2026-09-01", "title": "Task", "entities": "[Task]", "status": "draft",
         "body": "Draft."},
    )
    store = str(tmp_path)
    assert main(["search", "quokka limit", "--session", SESSION, "--store", store]) == 0
    assert main(["read", "q-2024", "--session", SESSION, "--store", store]) == 0
    capsys.readouterr()


def test_a_note_only_the_older_line_names_is_read_not_surfaced_and_never_called_unshown(tmp_path, capsys):
    from engmem.cli import main

    _session_searched_and_read_an_older_note(tmp_path, capsys)

    assert main(["feedback", "summary", "--session", SESSION, "--store", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert ("read, not surfaced: q-2024 -- read through engmem; no search of this session showed it "
            "as a result or a redirect, so it is not a find and cannot be assessed") in out.splitlines()
    assert "shown by no search" not in out
    assert main(["feedback", "record", SESSION, "q-2024", "helped", "--store", str(tmp_path)]) != 0
    refusal = capsys.readouterr()
    assert "is not a result or redirect of any search" in refusal.out + refusal.err
