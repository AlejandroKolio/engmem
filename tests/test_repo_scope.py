"""US-09: a search can be limited to one repository, by the explicit `repos` link only."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_file
from mcp_harness import _call, _run, _telemetry_lines, _text

from engmem.cli import main
from engmem.output import (
    MAX_OUTPUT_BYTES,
    SCOPE_NAME_DISPLAY_MAX,
    SCOREBOARD_RESERVE,
    TRIM_MARKER,
)
from engmem.scoring import Scope, search, search_with_role_sections
from engmem.search_report import render_result
from engmem.spine import load_store, parse_document
from engmem.telemetry import UNATTRIBUTED_CLI_NOTE, UNATTRIBUTED_MCP_NOTE

ALPHA = "svc-alpha"
BETA = "svc-beta"


def _doc(doc_id: str, body: str, *, repos: str | None, tags: str = "[]",
         title: str | None = None, status: str = "active", entities: str = "[]") -> str:
    repos_line = f"repos: {repos}\n" if repos is not None else ""
    return (
        f"---\nid: {doc_id}\ntitle: {title or doc_id}\ndate: 2026-01-01\n"
        f"task_date: 2026-01-01\nstatus: {status}\nsuperseded_by:\nbackfilled: false\n"
        f"tags: {tags}\nentities: {entities}\nrelated: []\ncovers_files: []\n{repos_line}---\n\n"
        f"{body}"
    )


STORE = {
    "2001-alpha-settings": _doc(
        "2001-alpha-settings",
        "## Decision Log\n\nSettings are read once at boot.\n",
        repos=f"[{ALPHA}]",
    ),
    "2002-beta-settings": _doc(
        "2002-beta-settings",
        "## Decision Log\n\nSettings reload on every request.\n",
        repos=f"[{BETA}]",
    ),
    "2003-shared-settings": _doc(
        "2003-shared-settings",
        "## Decision Log\n\nBoth services share one Settings schema.\n",
        repos=f"[{ALPHA}, {BETA}]",
    ),
    # bait for guessing: the repository's name sits in the title and the tags, never in `repos`
    "2004-unlinked-settings": _doc(
        "2004-unlinked-settings",
        "## Decision Log\n\nSettings moved to a vault.\n",
        repos=None, tags=f"[{ALPHA}]", title=f"{ALPHA} Settings",
    ),
    "2005-unreadable-settings": _doc(
        "2005-unreadable-settings",
        "## Decision Log\n\nSettings are validated against a schema.\n",
        repos="{alpha: beta}",
    ),
    "2006-alpha-settings-draft": _doc(
        "2006-alpha-settings-draft",
        "## Decision Log\n\nSettings draft text.\n",
        repos=f"[{ALPHA}]", status="draft",
    ),
}

ALPHA_IDS = {"2001-alpha-settings", "2003-shared-settings"}
UNSCOPED_IDS = {"2004-unlinked-settings", "2005-unreadable-settings"}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for doc_id, text in STORE.items():
        write_file(sessions, f"{doc_id}.md", text)
    return tmp_path


def _docs(store: Path):
    return load_store(store / "sessions").docs


def _cli(store: Path, capsys, *extra: str, query: str = "Settings") -> str:
    assert main(["search", query, "--store", str(store), *extra]) == 0
    return capsys.readouterr().out


def _mcp(store: Path, arguments: dict, name: str = "engmem_search") -> str:
    result, is_error = _call(store, name=name, arguments={"query": "Settings", **arguments})
    assert not is_error, _text(result)
    return _text(result)


def _shown_ids(output: str) -> list[str]:
    return [line.split()[1] for line in output.splitlines() if line.startswith("### ")]


def _block(output: str, doc_id: str) -> list[str]:
    blocks = output.split("\n\n")
    return next(b for b in blocks if b.startswith(f"### {doc_id} ")).splitlines()


# AC-09.1 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [ALPHA, ALPHA.upper(), f"  {ALPHA} "])
def test_ac_09_1_a_repo_scope_returns_only_records_linked_to_it(store, name):
    outcome = search(_docs(store), "Settings", Scope(repos=(name.strip(),)))

    assert {h.doc.id for h in outcome.hits} == ALPHA_IDS


def test_ac_09_1_a_scalar_repos_value_is_a_link(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "2101-scalar.md", _doc(
        "2101-scalar", "## Decision Log\n\nSettings.\n", repos=ALPHA, entities="[Settings]"))

    outcome = search(load_store(sessions).docs, "Settings", Scope(repos=(ALPHA,)))

    assert [h.doc.id for h in outcome.hits] == ["2101-scalar"]


def test_ac_09_1_the_cli_returns_only_records_linked_to_the_repo(store, capsys):
    out = _cli(store, capsys, "--repo", ALPHA)

    assert set(_shown_ids(out)) == ALPHA_IDS
    assert out.splitlines()[0].startswith(f"scope: repo {ALPHA} (2 ")


def test_ac_09_1_a_role_search_obeys_the_repo_scope(store, capsys):
    out = _cli(store, capsys, "--role", "decisions", "--repo", ALPHA)

    assert set(_shown_ids(out)) == ALPHA_IDS
    assert "role: decisions" in out


def test_a_repo_with_no_linked_record_says_so_rather_than_reading_as_an_empty_store(
    store, capsys
):
    out = _cli(store, capsys, "--repo", "svc-gamma")

    assert out.splitlines()[0].startswith("scope: repo svc-gamma (0 ")
    assert "prior context: none found" in out
    assert "docs: 6" in out, "the scoreboard still describes the whole store"


# ranking statistics come from the scope ----------------------------------------------------------


def _signature(outcome) -> tuple:
    return tuple(
        (h.doc.id, h.score, tuple(s.section.locator for s in h.section_hits), h.ambiguous)
        for h in outcome.hits
    ), outcome.ambiguous


def test_a_scoped_search_ranks_exactly_like_a_store_holding_only_the_scope(store, tmp_path):
    only_alpha = tmp_path / "only-alpha" / "sessions"
    only_alpha.mkdir(parents=True)
    for doc_id in ALPHA_IDS:
        write_file(only_alpha, f"{doc_id}.md", STORE[doc_id])

    scoped = search(_docs(store), "Settings schema boot", Scope(repos=(ALPHA,)))
    alone = search(load_store(only_alpha).docs, "Settings schema boot")

    assert _signature(scoped) == _signature(alone)


def test_another_repositorys_text_cannot_push_a_term_past_the_df_ceiling(tmp_path):
    """Store-wide statistics would drop `flag` from body scoring (4 of 5 sections hold it) and
    the alpha record would vanish from its own repository's search."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "2201-alpha.md", _doc(
        "2201-alpha", "## Decision Log\n\nThe flag is read at boot.\n\n## Landmines\n\nNone.\n",
        repos=f"[{ALPHA}]"))
    write_file(sessions, "2202-beta.md", _doc(
        "2202-beta", "## A\n\nflag\n\n## B\n\nflag\n\n## C\n\nflag\n", repos=f"[{BETA}]"))
    docs = load_store(sessions).docs
    assert "2201-alpha" not in {h.doc.id for h in search(docs, "flag").hits}, (
        "the scenario must reproduce the whole-store false negative"
    )

    outcome = search(docs, "flag", Scope(repos=(ALPHA,)))

    assert [h.doc.id for h in outcome.hits] == ["2201-alpha"]


def test_another_repositorys_entity_cannot_make_a_scoped_short_query_ambiguous(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "2301-alpha.md", _doc(
        "2301-alpha", "## Notes\n\nCache.\n", repos=f"[{ALPHA}]", entities="[WidgetCache, WC]"))
    write_file(sessions, "2302-beta.md", _doc(
        "2302-beta", "## Notes\n\nCount.\n", repos=f"[{BETA}]", entities="[WorkerCount, WC]"))
    docs = load_store(sessions).docs
    assert search(docs, "WC").ambiguous, "the scenario must be ambiguous across the store"

    outcome = search(docs, "WC", Scope(repos=(ALPHA,)))

    assert not outcome.ambiguous
    assert [h.doc.id for h in outcome.hits] == ["2301-alpha"]


# AC-09.2 ---------------------------------------------------------------------------------------


def test_ac_09_2_a_story_linked_to_two_repos_appears_once_with_both_links(store, capsys):
    out = _cli(store, capsys, "--repo", ALPHA)

    assert _shown_ids(out).count("2003-shared-settings") == 1
    assert f"repos: {ALPHA}, {BETA}" in _block(out, "2003-shared-settings")
    assert f"repos: {ALPHA}" in _block(out, "2001-alpha-settings")


def test_ac_09_2_the_role_search_shows_the_links_too(store, capsys):
    out = _cli(store, capsys, "--role", "decisions", "--repo", BETA)

    assert f"repos: {ALPHA}, {BETA}" in _block(out, "2003-shared-settings")


# AC-09.3 ---------------------------------------------------------------------------------------


def test_ac_09_3_title_and_tags_do_not_stand_in_for_a_repo_link(store):
    docs = _docs(store)
    assert "2004-unlinked-settings" in {h.doc.id for h in search(docs, ALPHA).hits}, (
        "the bait must match the repository's name through title and tags"
    )

    outcome = search(docs, f"{ALPHA} Settings", Scope(repos=(ALPHA,)))

    assert not UNSCOPED_IDS & {h.doc.id for h in outcome.hits}


def test_ac_09_3_records_without_a_readable_link_are_found_under_unscoped(store, capsys):
    out = _cli(store, capsys, "--unscoped")

    assert set(_shown_ids(out)) == UNSCOPED_IDS
    assert out.splitlines()[0].startswith("scope: unscoped (2 ")
    assert "repos: none" in _block(out, "2004-unlinked-settings")
    assert "repos: unreadable" in " ".join(_block(out, "2005-unreadable-settings"))


@pytest.mark.parametrize("repos", ["[]", "['', '  ']"])
def test_an_empty_or_blank_repos_list_is_no_link(tmp_path, repos):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "2401-empty.md", _doc(
        "2401-empty", "## Decision Log\n\nSettings.\n", repos=repos, entities="[Settings]"))
    docs = load_store(sessions).docs

    assert search(docs, "Settings", Scope(repos=(ALPHA,))).hits == []
    assert [h.doc.id for h in search(docs, "Settings", Scope(unscoped=True)).hits] == [
        "2401-empty"
    ]


# the default is the whole store, unchanged --------------------------------------------------------


def test_without_a_scope_the_whole_store_is_searched_and_nothing_new_is_printed(store, capsys):
    out = _cli(store, capsys)

    assert set(_shown_ids(out)) <= set(STORE) and len(_shown_ids(out)) == 3
    assert "matched below the top 3" in out, "every published record matched"
    assert not any(line.startswith(("scope:", "repos:")) for line in out.splitlines())


def test_without_a_scope_the_ranking_is_the_unscoped_ranking(store):
    docs = _docs(store)

    assert _signature(search(docs, "Settings")) == _signature(search(docs, "Settings", None))


# AC-09.4 ---------------------------------------------------------------------------------------


SCOPES = [
    pytest.param(["--repo", ALPHA], {"repo": ALPHA}, id="repo"),
    pytest.param(["--unscoped"], {"unscoped": True}, id="unscoped"),
    pytest.param([], {}, id="whole-store"),
]


def _result_lines(output: str, note: str) -> list[str]:
    return [line for line in output.splitlines() if line != note]


@pytest.mark.parametrize("cli_args, mcp_args", SCOPES)
@pytest.mark.parametrize("role", [None, "decisions"])
def test_ac_09_4_cli_and_mcp_apply_the_same_scope(store, capsys, cli_args, mcp_args, role):
    role_cli = ["--role", role] if role else []
    tool, role_mcp = ("engmem_search_by_role", {"role": role}) if role else ("engmem_search", {})

    cli_out = _cli(store, capsys, *cli_args, *role_cli)
    mcp_out = _mcp(store, {**mcp_args, **role_mcp}, name=tool)

    assert _result_lines(cli_out, UNATTRIBUTED_CLI_NOTE) == _result_lines(
        mcp_out, UNATTRIBUTED_MCP_NOTE
    )
    cli_row, mcp_row = _telemetry_lines(store)
    assert cli_row["scope"] == mcp_row["scope"]


@pytest.mark.parametrize("name", ["engmem_search", "engmem_search_by_role"])
@pytest.mark.parametrize("read_only", [False, True])
def test_ac_09_4_both_search_tools_declare_the_scope_arguments(store, name, read_only):
    _, responses, _ = _run(
        store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, read_only=read_only
    )
    tool = next(t for t in responses[0]["result"]["tools"] if t["name"] == name)
    schema = tool["inputSchema"]

    assert schema["properties"]["repo"]["type"] == "string"
    assert schema["properties"]["unscoped"]["type"] == "boolean"
    assert not {"repo", "unscoped"} & set(schema["required"])


# usage errors name their cause, on both channels -------------------------------------------------


@pytest.mark.parametrize("args, cause", [
    pytest.param(["--repo", "  "], "blank", id="blank-repo"),
])
def test_a_malformed_cli_scope_is_a_usage_error_on_stdout(store, capsys, args, cause):
    code = main(["search", "Settings", "--store", str(store), *args])
    out = capsys.readouterr().out

    assert code == 2
    assert out.startswith("error:") and cause in out
    assert not (store / "telemetry.jsonl").exists(), "a refused search records no row"


def test_a_repo_and_unscoped_together_are_refused_by_the_parser(store, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["search", "Settings", "--store", str(store), "--repo", ALPHA, "--unscoped"])
    out = capsys.readouterr().out

    assert exc.value.code == 2
    assert out.startswith("error:") and "not allowed with" in out
    assert not (store / "telemetry.jsonl").exists()


@pytest.mark.parametrize("arguments, cause", [
    pytest.param({"repo": 5}, "'repo' must be a string", id="repo-not-a-string"),
    pytest.param({"repo": "  "}, "blank", id="blank-repo"),
    pytest.param({"unscoped": "yes"}, "'unscoped' must be a boolean", id="unscoped-not-bool"),
    pytest.param({"repo": ALPHA, "unscoped": True}, "pass one", id="repo-and-unscoped"),
])
@pytest.mark.parametrize("name, extra", [
    pytest.param("engmem_search", {}, id="search"),
    pytest.param("engmem_search_by_role", {"role": "decisions"}, id="role"),
])
def test_a_malformed_mcp_scope_is_invalid_params(store, arguments, cause, name, extra):
    message = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": {"query": "Settings", **extra, **arguments}},
    }
    _, responses, _ = _run(store, message)

    assert responses[0]["error"]["code"] == -32602
    assert cause in responses[0]["error"]["message"]
    assert not (store / "telemetry.jsonl").exists()


@pytest.mark.parametrize("arguments", [{"repo": None}, {"unscoped": None}, {"unscoped": False}])
def test_an_mcp_scope_argument_left_null_or_false_is_the_whole_store(store, arguments):
    assert _mcp(store, arguments) == _mcp(store, {})


# telemetry records the scope --------------------------------------------------------------------


@pytest.mark.parametrize("cli_args, expected", [
    pytest.param([], None, id="whole-store"),
    pytest.param(["--repo", ALPHA.upper()], {"repos": [ALPHA.upper()], "n_searched": 2},
                 id="repo-as-typed"),
    pytest.param(["--unscoped"], {"unscoped": True, "n_searched": 2}, id="unscoped"),
    pytest.param(["--role", "decisions", "--repo", ALPHA],
                 {"repos": [ALPHA], "n_searched": 2}, id="role-search"),
])
def test_the_telemetry_row_records_the_scope(store, capsys, cli_args, expected):
    _cli(store, capsys, *cli_args)

    (row,) = _telemetry_lines(store)
    assert "scope" in row, "an explicit null tells a whole-store row from one predating the field"
    assert row["scope"] == expected


# the output budget still holds ------------------------------------------------------------------


def test_the_scope_line_counts_against_the_output_budget(tmp_path):
    """One record's long `repos:` line fills most of the budget and the cut lands among short
    lines, so a lead line printed outside the budget would push the result past it."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    name = "r" * SCOPE_NAME_DISPLAY_MAX
    wide = ", ".join([name] + [f"svc-{n:03d}-with-a-long-repository-name" for n in range(80)])
    write_file(sessions, "2501-wide.md", _doc(
        "2501-wide", "## Decision Log\n\nSettings.\n", repos=f"[{wide}]",
        title="Settings", entities="[Settings]"))
    for n in range(2, 6):
        write_file(sessions, f"250{n}-narrow.md", _doc(
            f"250{n}-narrow", "## Decision Log\n\nSettings.\n\n## Landmines\n\nNone.\n",
            repos=f"[{name}]", entities="[Settings]"))

    rendered = render_result(load_store(sessions).docs, "Settings", None, Scope(repos=(name,)))

    assert TRIM_MARKER in rendered.text, "the scenario must overflow the budget"
    assert len(rendered.text.encode("utf-8")) <= MAX_OUTPUT_BYTES - SCOREBOARD_RESERVE
    assert rendered.text.startswith(f"scope: repo {name} (5 ")


def test_a_huge_repo_name_cannot_inflate_a_miss(store, capsys):
    out = _cli(store, capsys, "--repo", "x" * 5000)

    lead = out.splitlines()[0]
    assert lead.startswith("scope: repo " + "x" * (SCOPE_NAME_DISPLAY_MAX - 1) + "… (0 ")
    assert "prior context: none found" in out


def test_a_repo_name_with_a_control_character_cannot_forge_a_line(store, capsys):
    out = _cli(store, capsys, "--repo", "evil\n### forged (score: 99.0)")

    assert "### forged" not in _shown_ids(out)
    assert out.splitlines()[0].startswith("scope: repo evil\\n### forged")


# the spine reads the link once -----------------------------------------------------------------


@pytest.mark.parametrize("repos_line, expected, warns", [
    pytest.param(f"repos: [{ALPHA}, {BETA}]\n", [ALPHA, BETA], False, id="list"),
    pytest.param(f"repos: {ALPHA}\n", [ALPHA], True, id="scalar"),
    pytest.param("", [], False, id="absent"),
    pytest.param("repos: {a: b}\n", None, True, id="mapping-is-unknown"),
    pytest.param("repos: 7\n", None, True, id="number-is-unknown"),
])
def test_the_spine_exposes_repos_and_never_drops_the_document(
    tmp_path, repos_line, expected, warns
):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "2601-doc.md", (
        "---\nid: 2601-doc\ntitle: t\ndate: 2026-01-01\nstatus: active\ntags: [x]\n"
        f"entities: [Y]\n{repos_line}---\n\nbody\n"
    ))

    result = load_store(sessions)

    assert [d.id for d in result.docs] == ["2601-doc"], "a bad repos value never hides the record"
    assert result.docs[0].repos == expected
    repo_warnings = [p.message for p in result.warnings if "repos" in p.message]
    assert bool(repo_warnings) == warns


def test_a_document_without_front_matter_has_no_repo_link(tmp_path):
    path = tmp_path / "no-front-matter.md"
    path.write_text("# Just a heading\n\nNo front matter block at all.\n", encoding="utf-8")

    assert parse_document(path).repos == []


def test_role_sections_and_ranking_share_the_scope(store):
    outcome, role_map = search_with_role_sections(_docs(store), "Settings", Scope(repos=(BETA,)))

    assert {h.doc.id for h in outcome.hits} == {"2002-beta-settings", "2003-shared-settings"}
    assert set(role_map) == {"2002-beta-settings", "2003-shared-settings"}


def test_scope_rejects_a_blank_name_or_two_scopes_at_once():
    with pytest.raises(ValueError):
        Scope(repos=(" ",))
    with pytest.raises(ValueError):
        Scope(repos=(ALPHA,), unscoped=True)
    with pytest.raises(ValueError):
        Scope()


def test_a_successor_outside_the_scope_shows_where_it_lives(tmp_path, capsys):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    old = _doc("2701-old", "## Decision Log\n\nSettings v1.\n", repos=f"[{ALPHA}]",
               entities="[Settings]").replace("superseded_by:", "superseded_by: 2702-new")
    write_file(sessions, "2701-old.md", old.replace("status: active", "status: superseded"))
    write_file(sessions, "2702-new.md", _doc(
        "2702-new", "## Decision Log\n\nSettings v2.\n", repos=f"[{BETA}]"))

    out = _cli(tmp_path, capsys, "--repo", ALPHA)

    assert "2701-old: superseded by 2702-new" in out
    assert f"repos: {BETA}" in _block(out, "2702-new")


def test_a_scoped_role_miss_names_the_scope(store, capsys):
    out = _cli(store, capsys, "--role", "decisions", "--repo", "svc-gamma")

    assert out.splitlines()[:3] == [
        "scope: repo svc-gamma (0 linked document(s) searched; records linked to no "
        "repository are left out)",
        "role: decisions",
        "prior context: none found — no document matched the query",
    ]
