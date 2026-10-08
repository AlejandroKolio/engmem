"""US-10: experience from other repositories is brought in explicitly, and its origin shows."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_file
from mcp_harness import _call, _run, _telemetry_lines, _text, _tools_call_msg
from test_mcp_subprocess import _Client

from engmem.cli import main
from engmem.output import (
    MAX_OUTPUT_BYTES,
    SCOPE_NAME_DISPLAY_MAX,
    SCOPE_NAMES_DISPLAY_MAX,
    SCOREBOARD_RESERVE,
    TRIM_MARKER,
)
from engmem.scoring import Scope, search, search_with_role_sections
from engmem.search_report import render_result
from engmem.spine import load_store
from engmem.telemetry import UNATTRIBUTED_CLI_NOTE, UNATTRIBUTED_MCP_NOTE

ALPHA = "svc-alpha"
BETA = "svc-beta"
GAMMA = "svc-gamma"


def _doc(doc_id: str, body: str, *, repos: str | None, status: str = "active",
         entities: str = "[]") -> str:
    repos_line = f"repos: {repos}\n" if repos is not None else ""
    return (
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-01-01\ntask_date: 2026-01-01\n"
        f"status: {status}\nsuperseded_by:\nbackfilled: false\ntags: []\n"
        f"entities: {entities}\nrelated: []\ncovers_files: []\n{repos_line}---\n\n{body}"
    )


STORE = {
    "3001-alpha-retry": _doc(
        "3001-alpha-retry", "## Decision Log\n\nRetry with a capped backoff.\n",
        repos=f"[{ALPHA}]"),
    "3002-beta-retry": _doc(
        "3002-beta-retry", "## Decision Log\n\nRetry only idempotent calls.\n",
        repos=f"[{BETA}]"),
    "3003-shared-retry": _doc(
        "3003-shared-retry", "## Decision Log\n\nBoth clients share one retry policy.\n",
        repos=f"[{ALPHA}, {BETA}]"),
    # the strongest match in the store, so leaving it out can only be the scope's doing
    "3004-gamma-retry": _doc(
        "3004-gamma-retry", "## Decision Log\n\nRetry retry retry backoff policy.\n",
        repos=f"[{GAMMA}]", entities="[Retry]"),
    "3005-unlinked-retry": _doc(
        "3005-unlinked-retry", "## Decision Log\n\nRetry budgets per tenant.\n", repos=None),
    "3006-alpha-retry-draft": _doc(
        "3006-alpha-retry-draft", "## Decision Log\n\nRetry draft.\n",
        repos=f"[{ALPHA}]", status="draft"),
}

ALPHA_BETA_IDS = {"3001-alpha-retry", "3002-beta-retry", "3003-shared-retry"}
PUBLISHED = 5


@pytest.fixture
def store(tmp_path: Path) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for doc_id, text in STORE.items():
        write_file(sessions, f"{doc_id}.md", text)
    return tmp_path


def _docs(store: Path):
    return load_store(store / "sessions").docs


def _cli(store: Path, capsys, *extra: str, query: str = "Retry") -> str:
    assert main(["search", query, "--store", str(store), *extra]) == 0
    return capsys.readouterr().out


def _mcp(store: Path, arguments: dict, name: str = "engmem_search") -> str:
    result, is_error = _call(store, name=name, arguments={"query": "Retry", **arguments})
    assert not is_error, _text(result)
    return _text(result)


def _shown_ids(output: str) -> list[str]:
    return [line.split()[1] for line in output.splitlines() if line.startswith("### ")]


def _block(output: str, doc_id: str) -> list[str]:
    return next(
        b for b in output.split("\n\n") if b.startswith(f"### {doc_id} ")
    ).splitlines()


def _signature(outcome) -> tuple:
    return tuple(
        (h.doc.id, h.score, tuple(s.section.locator for s in h.section_hits), h.ambiguous)
        for h in outcome.hits
    ), outcome.ambiguous


def _result_lines(output: str, note: str) -> list[str]:
    return [line for line in output.splitlines() if line != note]


# AC-10.1 ---------------------------------------------------------------------------------------


def test_ac_10_1_two_chosen_repositories_rank_only_their_records(store):
    outcome = search(_docs(store), "Retry", Scope(repos=(ALPHA, BETA)))

    assert {h.doc.id for h in outcome.hits} == ALPHA_BETA_IDS
    assert "3004-gamma-retry" in {h.doc.id for h in search(_docs(store), "Retry").hits}, (
        "the third repository must be a real candidate without the scope"
    )


def test_ac_10_1_the_cli_takes_repo_twice_as_a_union(store, capsys):
    out = _cli(store, capsys, "--repo", ALPHA, "--repo", BETA)

    assert set(_shown_ids(out)) == ALPHA_BETA_IDS
    assert out.splitlines()[0] == (
        f"scope: repos {ALPHA}, {BETA} (3 linked document(s) searched; records linked to no "
        "repository are left out)"
    )


def test_ac_10_1_a_union_ranks_exactly_like_a_store_holding_only_the_union(store, tmp_path):
    only_union = tmp_path / "only-union" / "sessions"
    only_union.mkdir(parents=True)
    for doc_id in ALPHA_BETA_IDS:
        write_file(only_union, f"{doc_id}.md", STORE[doc_id])

    scoped = search(_docs(store), "Retry backoff policy", Scope(repos=(ALPHA, BETA)))
    alone = search(load_store(only_union).docs, "Retry backoff policy")

    assert _signature(scoped) == _signature(alone)


def test_ac_10_1_role_sections_follow_the_union(store):
    outcome, role_map = search_with_role_sections(
        _docs(store), "Retry", Scope(repos=(ALPHA, BETA))
    )

    assert {h.doc.id for h in outcome.hits} == ALPHA_BETA_IDS
    assert set(role_map) == ALPHA_BETA_IDS


@pytest.mark.parametrize("names", [
    pytest.param((ALPHA, ALPHA), id="duplicate"),
    pytest.param((ALPHA, ALPHA.upper(), f"  {ALPHA} "), id="case-and-space-variants"),
])
def test_repeated_names_collapse_to_one_repository(store, capsys, names):
    single = _cli(store, capsys, "--repo", ALPHA)
    repeated = _cli(store, capsys, *[arg for name in names for arg in ("--repo", name)])

    assert repeated == single
    rows = _telemetry_lines(store)
    assert rows[0]["scope"] == rows[1]["scope"] == {"repos": [ALPHA], "n_searched": 2}


def test_the_first_spelling_of_a_repeated_name_is_the_one_kept():
    assert Scope(repos=(ALPHA.upper(), ALPHA, BETA, f" {BETA.upper()} ")).repos == (
        ALPHA.upper(), BETA,
    )


def test_a_scope_compares_by_its_names_not_by_its_cached_keys():
    first = Scope(repos=(ALPHA, BETA))
    second = Scope(repos=(ALPHA, BETA))

    assert first == second and hash(first) == hash(second)
    assert first != Scope(repos=(BETA, ALPHA)), "the order given is part of the scope"
    assert first.repo_keys == frozenset({ALPHA, BETA})
    assert Scope(repos=(ALPHA.upper(), f" {BETA} ")).repo_keys == frozenset({ALPHA, BETA})
    assert "repo_keys" not in repr(first)


def test_the_union_keys_a_scope_name_once_not_once_per_document(store, monkeypatch):
    import engmem.scoring as scoring

    docs = _docs(store) * 20
    scope = Scope(repos=tuple(f"other-{n}" for n in range(50)) + (ALPHA,))
    calls = []
    real_repo_key = scoring.repo_key
    monkeypatch.setattr(scoring, "repo_key", lambda name: calls.append(name) or real_repo_key(name))

    corpus = scoring.ranking_corpus(docs, scope)

    assert {doc.id for doc in corpus} == {"3001-alpha-retry", "3003-shared-retry"}
    assert len(calls) <= sum(len(scoring.linked_repos(doc)) for doc in docs), (
        "only each document's own links are keyed per call; the scope's names were keyed once"
    )


# AC-10.2 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [None, "decisions"])
def test_ac_10_2_a_record_in_both_chosen_repositories_appears_once_with_both_links(
    store, capsys, role
):
    role_args = ["--role", role] if role else []
    out = _cli(store, capsys, "--repo", ALPHA, "--repo", BETA, *role_args)

    assert _shown_ids(out).count("3003-shared-retry") == 1
    assert f"repos: {ALPHA}, {BETA}" in _block(out, "3003-shared-retry")
    assert f"repos: {BETA}" in _block(out, "3002-beta-retry")


# AC-10.3 ---------------------------------------------------------------------------------------


def test_ac_10_3_all_repositories_names_the_whole_store_and_every_origin(store, capsys):
    out = _cli(store, capsys, "--all-repos")

    assert out.splitlines()[0] == (
        f"scope: all repositories ({PUBLISHED} document(s) searched; the whole store, records "
        "linked to no repository included)"
    )
    for doc_id in _shown_ids(out):
        assert any(line.startswith("repos: ") for line in _block(out, doc_id)), doc_id
    assert f"repos: {GAMMA}" in _block(out, "3004-gamma-retry")


def test_ac_10_3_all_repositories_shows_an_unlinked_record_as_linked_to_none(store, capsys):
    out = _cli(store, capsys, "--all-repos", query="tenant budgets")

    assert _shown_ids(out) == ["3005-unlinked-retry"]
    assert "repos: none" in _block(out, "3005-unlinked-retry")


@pytest.mark.parametrize("query", ["Retry", "Retry backoff policy", "tenant budgets"])
def test_ac_10_3_all_repositories_ranks_exactly_like_the_default(store, query):
    docs = _docs(store)

    assert _signature(search(docs, query, Scope(all_repos=True))) == _signature(
        search(docs, query)
    )


def test_ac_10_3_a_role_search_over_all_repositories_shows_origins(store, capsys):
    out = _cli(store, capsys, "--all-repos", "--role", "decisions")

    assert out.splitlines()[:2] == [
        f"scope: all repositories ({PUBLISHED} document(s) searched; the whole store, records "
        "linked to no repository included)",
        "role: decisions",
    ]
    assert f"repos: {GAMMA}" in _block(out, "3004-gamma-retry")


def test_ac_10_3_a_miss_over_all_repositories_still_names_the_mode(store, capsys):
    out = _cli(store, capsys, "--all-repos", query="nothing-matches-this")

    assert out.splitlines()[:2] == [
        f"scope: all repositories ({PUBLISHED} document(s) searched; the whole store, records "
        "linked to no repository included)",
        "prior context: none found",
    ]


def test_without_a_flag_the_default_output_is_unchanged(store, capsys):
    out = _cli(store, capsys)

    assert not any(line.startswith(("scope:", "repos:")) for line in out.splitlines())
    (row,) = _telemetry_lines(store)
    assert row["scope"] is None


# AC-10.4 ---------------------------------------------------------------------------------------


SCOPED_CLI_RUNS = [
    ["--repo", BETA],
    ["--repo", ALPHA, "--repo", BETA],
    ["--all-repos"],
    ["--unscoped"],
]


@pytest.mark.parametrize("role", [None, "decisions"])
def test_ac_10_4_a_plain_cli_search_after_scoped_ones_is_the_whole_store_default(
    store, capsys, role
):
    role_args = ["--role", role] if role else []
    before = _cli(store, capsys, *role_args)
    for scoped in SCOPED_CLI_RUNS:
        _cli(store, capsys, *scoped, *role_args)

    after = _cli(store, capsys, *role_args)

    assert after == before
    assert _telemetry_lines(store)[-1]["scope"] is None


SCOPED_MCP_ARGS = [
    {"repo": BETA},
    {"repos": [ALPHA, BETA]},
    {"all_repos": True},
    {"unscoped": True},
]


@pytest.mark.parametrize("name, extra", [
    pytest.param("engmem_search", {}, id="search"),
    pytest.param("engmem_search_by_role", {"role": "decisions"}, id="role"),
])
def test_ac_10_4_one_mcp_server_does_not_carry_a_scope_into_the_next_call(store, name, extra):
    plain = {"query": "Retry", **extra}
    calls = [plain] + [{**plain, **scoped} for scoped in SCOPED_MCP_ARGS] + [plain]
    messages = [_tools_call_msg(i, name=name, arguments=a) for i, a in enumerate(calls, 1)]

    _, responses, _ = _run(store, *messages)
    texts = [_text(r["result"]) for r in responses]

    assert texts[-1] == texts[0]
    assert not texts[-1].startswith("scope:")
    assert [row["scope"] is None for row in _telemetry_lines(store)] == (
        [True] + [False] * len(SCOPED_MCP_ARGS) + [True]
    )


def test_ac_10_4_a_real_mcp_subprocess_does_not_carry_a_scope_either(store):
    client = _Client(store)
    try:
        first = _text(client.tool_call("engmem_search", {"query": "Retry"}))
        client.tool_call("engmem_search", {"query": "Retry", "repos": [BETA]})
        client.tool_call("engmem_search", {"query": "Retry", "all_repos": True})
        last = _text(client.tool_call("engmem_search", {"query": "Retry"}))
    finally:
        client.close()

    assert last == first
    assert "3004-gamma-retry" in _shown_ids(last)


def test_ac_10_4_a_second_scoped_call_does_not_inherit_the_first_scope(store):
    messages = [
        _tools_call_msg(1, arguments={"query": "Retry", "repo": ALPHA}),
        _tools_call_msg(2, arguments={"query": "Retry", "repo": BETA}),
    ]
    _, responses, _ = _run(store, *messages)

    assert set(_shown_ids(_text(responses[1]["result"]))) == {
        "3002-beta-retry", "3003-shared-retry",
    }


# CLI and MCP apply one scope ---------------------------------------------------------------------


PARITY = [
    pytest.param(["--repo", ALPHA, "--repo", BETA], {"repos": [ALPHA, BETA]}, id="repos"),
    pytest.param(["--repo", ALPHA, "--repo", BETA], {"repo": ALPHA, "repos": [BETA]},
                 id="repo-plus-repos"),
    pytest.param(["--repo", ALPHA], {"repos": [ALPHA, ALPHA.upper()]}, id="collapsed"),
    pytest.param(["--all-repos"], {"all_repos": True}, id="all-repos"),
]


@pytest.mark.parametrize("cli_args, mcp_args", PARITY)
@pytest.mark.parametrize("role", [None, "decisions"])
def test_cli_and_mcp_apply_the_same_multi_repository_scope(
    store, capsys, cli_args, mcp_args, role
):
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
def test_both_search_tools_declare_repos_and_all_repos(store, name, read_only):
    _, responses, _ = _run(
        store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, read_only=read_only
    )
    tool = next(t for t in responses[0]["result"]["tools"] if t["name"] == name)
    properties = tool["inputSchema"]["properties"]

    assert properties["repo"]["type"] == "string", "US-09's single name stays a string"
    assert properties["repos"] == {**properties["repos"], "type": "array",
                                   "items": {"type": "string"}}
    assert properties["all_repos"]["type"] == "boolean"
    assert not {"repos", "all_repos"} & set(tool["inputSchema"]["required"])


@pytest.mark.parametrize("arguments", [
    {"repos": None}, {"all_repos": None}, {"all_repos": False},
])
def test_an_mcp_scope_argument_left_null_or_false_is_the_whole_store(store, arguments):
    assert _mcp(store, arguments) == _mcp(store, {})


# usage errors name their cause, on both channels -------------------------------------------------


@pytest.mark.parametrize("args", [
    pytest.param(["--all-repos", "--repo", ALPHA], id="all-repos-and-repo"),
    pytest.param(["--all-repos", "--unscoped"], id="all-repos-and-unscoped"),
    pytest.param(["--repo", ALPHA, "--repo", BETA, "--unscoped"], id="repos-and-unscoped"),
])
def test_two_cli_scopes_at_once_are_refused_by_the_parser(store, capsys, args):
    with pytest.raises(SystemExit) as exc:
        main(["search", "Retry", "--store", str(store), *args])
    out = capsys.readouterr().out

    assert exc.value.code == 2
    assert out.startswith("error:") and "not allowed with" in out
    assert not (store / "telemetry.jsonl").exists()


def test_a_blank_name_among_several_is_refused_not_dropped(store, capsys):
    code = main(["search", "Retry", "--store", str(store), "--repo", ALPHA, "--repo", " "])
    out = capsys.readouterr().out

    assert code == 2
    assert out.startswith("error:") and "blank" in out
    assert not (store / "telemetry.jsonl").exists()


@pytest.mark.parametrize("arguments, cause", [
    pytest.param({"repos": ALPHA}, "'repos' must be a list of strings", id="repos-a-string"),
    pytest.param({"repos": [ALPHA, 5]}, "'repos' must be a list of strings", id="item-number"),
    pytest.param({"repos": []}, "at least one", id="repos-empty"),
    pytest.param({"repos": [ALPHA, "  "]}, "blank", id="repos-blank-item"),
    pytest.param({"repo": ALPHA, "repos": [" "]}, "blank", id="blank-item-next-to-repo"),
    pytest.param({"all_repos": "yes"}, "'all_repos' must be a boolean", id="all-repos-not-bool"),
    pytest.param({"repos": [ALPHA], "unscoped": True}, "pass one", id="repos-and-unscoped"),
    pytest.param({"all_repos": True, "repo": ALPHA}, "pass one", id="all-repos-and-repo"),
    pytest.param({"all_repos": True, "repos": [ALPHA]}, "pass one", id="all-repos-and-repos"),
    pytest.param({"all_repos": True, "unscoped": True}, "pass one", id="all-repos-and-unscoped"),
])
@pytest.mark.parametrize("name, extra", [
    pytest.param("engmem_search", {}, id="search"),
    pytest.param("engmem_search_by_role", {"role": "decisions"}, id="role"),
])
def test_a_malformed_mcp_multi_repository_scope_is_invalid_params(
    store, arguments, cause, name, extra
):
    message = _tools_call_msg(
        1, name=name, arguments={"query": "Retry", **extra, **arguments}
    )
    _, responses, _ = _run(store, message)

    assert responses[0]["error"]["code"] == -32602
    assert cause in responses[0]["error"]["message"]
    assert not (store / "telemetry.jsonl").exists()


@pytest.mark.parametrize("name", ["engmem_search", "engmem_search_by_role"])
def test_repos_declares_that_it_is_never_empty(store, name):
    _, responses, _ = _run(store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool = next(t for t in responses[0]["result"]["tools"] if t["name"] == name)

    assert tool["inputSchema"]["properties"]["repos"]["minItems"] == 1


@pytest.mark.parametrize("arguments, named", [
    pytest.param({"repo": None, "repos": [ALPHA], "unscoped": True},
                 "'repos' and 'unscoped'", id="null-repo-next-to-repos"),
    pytest.param({"repo": ALPHA, "repos": None, "all_repos": True},
                 "'repo' and 'all_repos'", id="null-repos-next-to-repo"),
    pytest.param({"repo": ALPHA, "repos": [BETA], "unscoped": True},
                 "'repo'/'repos' and 'unscoped'", id="both-present"),
])
def test_a_scope_conflict_names_only_the_arguments_given_a_value(store, arguments, named):
    message = _tools_call_msg(1, name="engmem_search", arguments={"query": "Retry", **arguments})
    _, responses, _ = _run(store, message)

    assert responses[0]["error"]["code"] == -32602
    assert responses[0]["error"]["message"] == (
        f"invalid params: {named} are different scopes — pass one"
    )


def test_the_scope_value_object_refuses_two_modes_or_none():
    for kwargs in (
        {"repos": (ALPHA,), "all_repos": True},
        {"unscoped": True, "all_repos": True},
        {"repos": (ALPHA,), "unscoped": True},
        {"repos": (ALPHA, " ")},
        {},
    ):
        with pytest.raises(ValueError):
            Scope(**kwargs)
    with pytest.raises(TypeError):
        Scope(repos=ALPHA)


# telemetry records the scope --------------------------------------------------------------------


@pytest.mark.parametrize("cli_args, expected", [
    pytest.param(["--repo", ALPHA, "--repo", BETA],
                 {"repos": [ALPHA, BETA], "n_searched": 3}, id="union"),
    pytest.param(["--repo", BETA, "--repo", ALPHA],
                 {"repos": [BETA, ALPHA], "n_searched": 3}, id="order-as-passed"),
    pytest.param(["--all-repos"], {"all_repos": True, "n_searched": PUBLISHED}, id="all-repos"),
    pytest.param(["--all-repos", "--role", "decisions"],
                 {"all_repos": True, "n_searched": PUBLISHED}, id="all-repos-role"),
])
def test_the_telemetry_row_records_the_multi_repository_scope(store, capsys, cli_args, expected):
    _cli(store, capsys, *cli_args)

    (row,) = _telemetry_lines(store)
    assert row["scope"] == expected
    assert row["n_docs"] == len(STORE), "n_docs stays the whole store"


def test_a_capped_row_keeps_its_session_and_measures_the_same_result(store, capsys):
    names = [ALPHA] + [f"other-{n}-" + "r" * 200 for n in range(80)]
    query = "Retry " + "x" * 3000
    args = [arg for name in names for arg in ("--repo", name)]
    out = _cli(store, capsys, *args, "--session", "sess-7", query=query)

    (row,) = _telemetry_lines(store)
    rendered = render_result(_docs(store), query, None, Scope(repos=tuple(names)))
    assert row["session_id"] == "sess-7"
    assert row["context_bytes"] == len(rendered.text.encode("utf-8"))
    assert rendered.text in out
    assert (row["query_truncated"], row["scope"]["repos_truncated"]) == (True, True)
    assert row["scope"]["repos_total"] == len(names)
    assert row["scope"]["n_searched"] == 2


# the output budget still holds ------------------------------------------------------------------


def _many_names(count: int, width: int) -> list[str]:
    return [f"{n:04d}-" + "r" * (width - 5) for n in range(count)]


def test_many_long_names_cannot_inflate_a_miss(store, capsys):
    names = _many_names(400, 3 * SCOPE_NAME_DISPLAY_MAX)
    out = _cli(store, capsys, *[arg for name in names for arg in ("--repo", name)])

    lead = out.splitlines()[0]
    listed = lead.removeprefix("scope: repos ").split(" (0 linked")[0]
    assert len(listed) <= SCOPE_NAMES_DISPLAY_MAX + len(" and 400 more")
    assert listed.endswith(" more")
    assert "prior context: none found" in out
    assert len(out.encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_the_first_name_is_always_shown_even_at_full_width(store, capsys):
    long_name = "x" * 5000
    out = _cli(store, capsys, "--repo", long_name, "--repo", BETA)

    assert out.splitlines()[0].startswith(
        "scope: repos " + "x" * (SCOPE_NAME_DISPLAY_MAX - 1) + "…, "
    )


def test_a_list_that_fits_is_shown_whole(store, capsys):
    out = _cli(store, capsys, "--repo", ALPHA, "--repo", BETA, "--repo", GAMMA)

    assert out.splitlines()[0].startswith(f"scope: repos {ALPHA}, {BETA}, {GAMMA} (4 linked")


def test_a_wide_multi_repository_result_stays_inside_the_budget(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    names = _many_names(60, 80)
    wide = ", ".join(names)
    for n in range(1, 6):
        write_file(sessions, f"310{n}-wide.md", _doc(
            f"310{n}-wide", "## Decision Log\n\nRetry.\n\n## Landmines\n\nNone.\n",
            repos=f"[{wide}]", entities="[Retry]"))

    rendered = render_result(
        load_store(sessions).docs, "Retry", None, Scope(repos=tuple(names))
    )

    assert TRIM_MARKER in rendered.text, "the scenario must overflow the budget"
    assert len(rendered.text.encode("utf-8")) <= MAX_OUTPUT_BYTES - SCOREBOARD_RESERVE
    assert rendered.text.startswith("scope: repos ")
