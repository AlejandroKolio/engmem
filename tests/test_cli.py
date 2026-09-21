import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import (
    FIXTURES,
    on_a_shared_runner,
    requires_permission_enforcement,
)

from engmem import cache
from engmem.cli import build_parser, main


@pytest.fixture
def store(tmp_path):
    store_dir = tmp_path / "store"
    sessions_dir = store_dir / "sessions"
    shutil.copytree(FIXTURES, sessions_dir)
    return store_dir


def _run(store, *args, capsys):
    exit_code = main(["search", *args, "--store", str(store)])
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def test_g12_superseded_doc_resolves_to_successor(store, capsys):
    exit_code, out, err = _run(store, "MazeRenderer", capsys=capsys)

    assert exit_code == 0
    assert "maze-render-new" in out
    assert "superseded by maze-render-new" in out
    # maze-render-new must appear as its own scored hit, not merely inside the
    # superseded note referencing it
    assert out.count("maze-render-new") >= 2


def test_g13_draft_excluded_but_counted_in_scoreboard(store, capsys):
    # §8 specifies the query `wip` — a short token exact-matching the draft's own id,
    # the stronger case (the doc WOULD rank were it not a draft)
    exit_code, out, err = _run(store, "wip", capsys=capsys)

    assert exit_code == 0
    assert "wip-something" not in out
    assert "drafts: 1" in out


def test_g14_broken_doc_warns_but_other_results_still_return(store, capsys):
    # "ETAG" yields exactly two hits, both within the top-3 cap, so both MUST show up
    # as real scored results (a "### <id>" header), not merely be mentioned inside
    # another hit's `related:` line.
    exit_code, out, err = _run(store, "ETAG", capsys=capsys)

    assert exit_code == 0
    assert "broken" in err.lower()
    assert "### 1000001-response-cache" in out
    assert "### ttl-etag-revalidation-v2" in out


def test_g15_no_match_reports_plainly_with_exit_zero(store, capsys):
    exit_code, out, err = _run(store, "nonexistent-term-xyz", capsys=capsys)

    assert exit_code == 0
    assert out.strip().splitlines()[0] == "prior context: none found"

    telemetry = json.loads((store / "telemetry.jsonl").read_text().strip().splitlines()[-1])
    assert telemetry["result"] == "miss"


def test_g16_output_size_capped_to_top_three(store, capsys):
    from engmem.output import MAX_OUTPUT_BYTES

    # "platform" matches four docs, all tied at score 1.0 — more than TOP_N, so this
    # pins that exactly three are rendered as scored results, not merely that the
    # (unrelated) output byte cap happens to be respected.
    exit_code, out, err = _run(store, "platform", capsys=capsys)

    assert exit_code == 0
    assert len(out.encode("utf-8")) <= MAX_OUTPUT_BYTES

    result_headers = re.findall(r"^### .+$", out, re.MULTILINE)
    scored_headers = [h for h in result_headers if not h.endswith("(successor)")]
    assert len(scored_headers) == 3, (
        f"expected exactly top-3 scored result headers, got {scored_headers!r}"
    )
    assert "(1 more document(s) matched below the top 3" in out, (
        "the 4th tied match must be reported as withheld, not silently dropped"
    )


MISSING_STORE_CASES = [
    pytest.param(["search", "anything"], id="search"),
    pytest.param(["roles"], id="roles"),
]


@pytest.mark.parametrize("subcommand_args", MISSING_STORE_CASES)
def test_missing_store_is_an_error_on_both_streams(tmp_path, capsys, subcommand_args):
    """A typo'd `--store` used to print "none found" (or list nothing), exit 0, with the
    failure invisible to a reader watching only one stream."""
    missing = tmp_path / "no-such-store"

    exit_code = main([*subcommand_args, "--store", str(missing)])
    captured = capsys.readouterr()

    assert exit_code == 2, "usage/precondition failures are exit 2 by this project's convention"
    assert captured.out.strip() != "", "stdout must not be empty on a terminal failure"
    assert str(missing) in captured.out
    assert str(missing) in captured.err


def test_missing_store_search_does_not_create_a_store_as_a_side_effect(tmp_path, capsys):
    missing = tmp_path / "no-such-store"

    main(["search", "anything", "--store", str(missing)])
    capsys.readouterr()

    assert not missing.exists(), "search must not create a store as a side effect"


def test_empty_but_existing_store_still_reports_none_found(tmp_path, capsys):
    store_dir = tmp_path / "store"
    (store_dir / "sessions").mkdir(parents=True)

    exit_code = main(["search", "anything", "--store", str(store_dir)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "prior context: none found" in captured.out


UNREADABLE_SESSIONS_DIR_CASES = [
    # `Path.glob` swallows the `PermissionError` an unscannable `sessions/` raises, so
    # stdout matched an empty store.
    pytest.param(["search", "hello"], ("failed to load",), id="search"),
    # the same in `roles`, which used to drop `scan_error` on the floor entirely, unlike `search`.
    pytest.param(["roles"], (), id="roles"),
    # and in `backfill`: an unscannable `sessions/` must say so on stdout, not silently backfill
    # a partial store.
    pytest.param(["backfill", "--all"], (), id="backfill"),
]


@requires_permission_enforcement
@pytest.mark.parametrize(
    ("subcommand_args", "extra_stdout_substrings"), UNREADABLE_SESSIONS_DIR_CASES
)
def test_unreadable_sessions_dir_is_not_a_phantom_empty_store(
    tmp_path, capsys, subcommand_args, extra_stdout_substrings
):
    store_dir = tmp_path / "store"
    sessions_dir = store_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "control.md").write_text("# Doc\n\nhello\n", encoding="utf-8")
    os.chmod(sessions_dir, 0o000)
    try:
        exit_code = main([*subcommand_args, "--store", str(store_dir)])
        captured = capsys.readouterr()
    finally:
        os.chmod(sessions_dir, 0o755)

    assert exit_code == 0, "an unreadable sessions/ is a degraded result, not a usage error"
    assert "unknown, not zero" in captured.out, (
        "the terse scoreboard count alone reads like one ordinary bad file, not an "
        "entire directory nobody could scan"
    )
    for substring in extra_stdout_substrings:
        assert substring in captured.out


def test_stderr_distinguishes_errors_from_warnings(store, capsys):
    """An excluded document and a warned-about one printed identically, though §4 classifies
    them differently."""
    (store / "sessions" / "no-entities-doc.md").write_text("""---
id: no-entities-doc
title: No Entities Doc
date: 2026-08-01
task_date: 2026-08-01
status: active
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit: abc
capture_minutes: 1
---

## Pre-reg
""")

    _, _, err = _run(store, "platform", capsys=capsys)

    error_lines = [ln for ln in err.splitlines() if ln.startswith("error: ")]
    warning_lines = [ln for ln in err.splitlines() if ln.startswith("warning: ")]
    assert any("broken" in ln for ln in error_lines)
    assert any("no-entities-doc" in ln for ln in warning_lines)


def _clone_store(store: Path, times: int, originals: list[Path] | None = None) -> int:
    """Replicates the pristine fixtures, never the clones, or the second call multiplies the first
    call's output."""
    sessions = store / "sessions"
    sources = originals if originals is not None else sorted(sessions.glob("*.md"))
    for i in range(times):
        for src in sources:
            content = src.read_text(encoding="utf-8", errors="replace")
            (sessions / f"clone-{i}-{src.name}").write_text(
                content.replace(f"id: {src.stem}", f"id: clone-{i}-{src.stem}"),
                encoding="utf-8",
            )
    return len(list(sessions.glob("*.md")))


def _best_of_three(store: Path, capsys) -> float:
    # noise only ever adds time, so the fastest run is the closest estimate of the real cost
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        main(["search", "platform", "--store", str(store)])
        best = min(best, time.perf_counter() - start)
        capsys.readouterr()
    return best


def _cold_floor(argv: list[str], cache_home: Path, capsys) -> float:
    """Fastest of three cold-cache runs: clearing the cache before each run measures the
    code's own cost rather than a warm cache's, and the floor discards scheduling noise,
    which only ever adds time."""
    best = float("inf")
    for _ in range(3):
        shutil.rmtree(cache_home, ignore_errors=True)
        start = time.perf_counter()
        main(argv)
        best = min(best, time.perf_counter() - start)
        capsys.readouterr()
    return best


@on_a_shared_runner
def test_sc002_search_stays_well_inside_the_no_index_budget(
    store, capsys, tmp_path, monkeypatch
):
    """§3's "no INDEX.md, no cache, no SQLite" holds only while a full scan costs milliseconds;
    this is the tighter of the two tripwires on that bet, and it measures a known machine."""
    # §3 sizes a realistic store at 20-200 docs; 8 fixtures alone would make this
    # bound meaningless, so replicate them up to ~200
    assert _clone_store(store, 25) >= 200

    # redirected away from the developer's own `~/.cache/engmem`; a floor of three cold
    # runs is the code's own cost, not one unlucky scheduling tick on the author's machine
    cache_home = tmp_path / "cache-home"
    monkeypatch.setattr(cache, "cache_root", lambda: cache_home)

    elapsed = _cold_floor(["search", "platform", "--store", str(store)], cache_home, capsys)

    # 0.5, chosen from measurement rather than taste: the floor-of-three steady state on the
    # author's machine is 0.142-0.147s over ten runs, and ~0.30s per run under coverage
    # instrumentation. The previous 0.2 was not merely tight, it was false about this very code
    # under `--cov`, and it measured the machine rather than the engine. §3 budgets a full scan
    # at "milliseconds" and states no number; this bound is the tripwire on that budget, not the
    # budget itself, and anything that grows with document count is caught by the ratio test
    # below regardless of wall clock.
    assert elapsed < 0.5


def test_sc002_search_stays_within_an_order_of_magnitude_everywhere(
    store, capsys, tmp_path, monkeypatch
):
    """The bound above is skipped on a shared runner, so it guards nothing in CI. This one is
    4x looser, runs unconditionally, and exists so an order-of-magnitude regression — the kind
    that would invalidate §3's no-index decision — cannot merge with every leg green."""
    assert _clone_store(store, 25) >= 200

    cache_home = tmp_path / "cache-home"
    monkeypatch.setattr(cache, "cache_root", lambda: cache_home)

    elapsed = _cold_floor(["search", "platform", "--store", str(store)], cache_home, capsys)

    assert elapsed < 2.0


def test_search_cost_stays_linear_in_the_number_of_documents(store, tmp_path, capsys):
    """The shape of the cost curve belongs to the code; a wall-clock bound would only measure the
    machine."""
    originals = sorted((store / "sessions").glob("*.md"))
    small_docs = _clone_store(store, 5, originals)
    small = _best_of_three(store, capsys)

    large_docs = _clone_store(store, 25, originals)
    large = _best_of_three(store, capsys)

    size_ratio = large_docs / small_docs
    time_ratio = large / small

    # 2.0, chosen from measurement rather than taste: honest linear growth measures 4.2x for 4.3x
    # the documents, and a quadratic doing real per-pair work measures 9.5x.
    assert time_ratio < size_ratio * 2.0, (
        f"{small_docs} docs took {small * 1000:.0f}ms, {large_docs} took {large * 1000:.0f}ms — "
        f"{time_ratio:.1f}x the time for {size_ratio:.1f}x the documents. Linear would be "
        f"about {size_ratio:.1f}x, quadratic about {size_ratio ** 2:.0f}x."
    )


def test_failed_documents_are_counted_on_stdout(store, capsys):
    """An agent reading only stdout would otherwise treat a partial store as the whole store."""
    (store / "sessions" / "unparseable.md").write_text(
        "---\nid: unparseable\ntags: [platform\n---\n\n## Pre-reg\n", encoding="utf-8"
    )

    code, out, err = _run(store, "sweeper", capsys=capsys)

    assert code == 0
    assert "failed to load" in out
    assert "unparseable.md" in err, "the name still belongs on stderr"


# store resolution itself is `engmem.runtime`'s own contract and is guarded in
# tests/test_runtime.py; what stays here is the CLI wiring that consumes it.


def test_telemetry_write_failure_still_prints_full_results_and_exits_zero(store, capsys):
    """A telemetry write failure must not discard an answer already printed correctly on
    stdout."""
    (store / "telemetry.jsonl").mkdir()

    exit_code, out, err = _run(store, "platform", capsys=capsys)

    assert exit_code == 0
    assert "docs:" in out, "the scoreboard footer must still be printed"
    assert "note:" in out.lower() and "telemetry" in out.lower(), (
        "a telemetry write failure must be surfaced on stdout, not swallowed"
    )


# --- `engmem mcp` — argument wiring only; the protocol loop itself lives in
# engmem.mcp_server (owned separately) and is never exercised here. --------------


def test_mcp_subcommand_resolves_store_and_calls_serve(tmp_path, monkeypatch):
    pytest.importorskip("engmem.mcp_server")
    store = tmp_path / "store"
    calls = []

    def fake_serve(resolved_store, **kwargs):
        calls.append(resolved_store)
        return 0

    monkeypatch.setattr("engmem.mcp_server.serve", fake_serve)

    exit_code = main(["mcp", "--store", str(store)])

    assert exit_code == 0
    assert calls == [store]


def test_mcp_subcommand_uses_default_store_resolution_when_no_flag(tmp_path, monkeypatch):
    """No `--store` must follow the same `runtime.resolve_store` precedence `search` uses, not a
    hardcoded path."""
    pytest.importorskip("engmem.mcp_server")
    home_store = tmp_path / "engmem-home"
    monkeypatch.setenv("ENGMEM_HOME", str(home_store))
    calls = []
    monkeypatch.setattr(
        "engmem.mcp_server.serve", lambda resolved_store, **kwargs: calls.append(resolved_store) or 0
    )

    exit_code = main(["mcp"])

    assert exit_code == 0
    assert calls == [home_store]


@pytest.mark.parametrize(
    ("store_name", "serve_exit_code"),
    [
        # stdout carries the MCP protocol and nothing else, even for a store that does not
        # exist yet
        pytest.param("no-such-store", 2, id="missing-store"),
        pytest.param("store", 0, id="success"),
    ],
)
def test_mcp_subcommand_prints_nothing_to_stdout_regardless_of_outcome(
    tmp_path, monkeypatch, capsys, store_name, serve_exit_code
):
    pytest.importorskip("engmem.mcp_server")
    monkeypatch.setattr("engmem.mcp_server.serve", lambda resolved_store, **kwargs: serve_exit_code)

    exit_code = main(["mcp", "--store", str(tmp_path / store_name)])
    captured = capsys.readouterr()

    assert exit_code == serve_exit_code
    assert captured.out == ""


# ---------------------------------------------------------------------------
# --session — the Gate 1 join key
# ---------------------------------------------------------------------------


def _last_telemetry(store):
    return json.loads((store / "telemetry.jsonl").read_text().strip().splitlines()[-1])


SESSION_KEY_CASES = [
    pytest.param(["--session", "20260823-cache"], "20260823-cache", id="given"),
    pytest.param([], None, id="absent"),
    # blank is absent, not an unknown session
    pytest.param(["--session", ""], None, id="empty"),
    pytest.param(["--session", "   "], None, id="spaces"),
    pytest.param(["--session", "\t"], None, id="tab"),
    # stripped, so both entry paths produce the same key
    pytest.param(["--session", "  20260823-cache  "], "20260823-cache", id="padded"),
]


@pytest.mark.parametrize(("session_args", "expected_session_id"), SESSION_KEY_CASES)
def test_the_session_flag_decides_the_logged_join_key(
    store, capsys, session_args, expected_session_id
):
    """Absence is an explicit null, never an omitted field: an unattributed row and a row written
    before the field existed must stay distinguishable at review time."""
    exit_code, out, _ = _run(store, "1000001", *session_args, capsys=capsys)

    assert exit_code == 0
    record = _last_telemetry(store)
    assert "session_id" in record
    assert record["session_id"] == expected_session_id
    if expected_session_id is not None:
        assert expected_session_id not in out, (
            "the join key is for the log, not the agent's output"
        )


def test_unknown_session_id_does_not_gate_the_search(store, capsys):
    """A search refused because a draft was named a moment too early would be ritual gating the
    answer (SPEC §6)."""
    exit_code, out, _ = _run(store, "1000001", "--session", "no-such-draft", capsys=capsys)

    assert exit_code == 0
    assert "1000001-response-cache" in out
    assert _last_telemetry(store)["session_id"] == "no-such-draft"


UNATTRIBUTED_NOTE = "note: unattributed search — pass --session <draft-id>"


UNATTRIBUTED_NOTE_CASES = [
    pytest.param(["1000001"], True, id="hit-without-session"),
    pytest.param(["1000001", "--session", "20260823-cache"], False, id="hit-with-session"),
    pytest.param(["1000001", "--session", ""], True, id="empty-session"),
    pytest.param(["1000001", "--session", "   "], True, id="blank-session"),
    # both search branches owe the note, not just the word-ranking one
    pytest.param(["1000001", "--role", "decisions"], True, id="role-search-without-session"),
    pytest.param(["nonexistent-term-xyz"], True, id="no-match-without-session"),
]


@pytest.mark.parametrize(("search_args", "expect_note"), UNATTRIBUTED_NOTE_CASES)
def test_the_unattributed_note_appears_exactly_when_no_usable_session_was_passed(
    store, capsys, search_args, expect_note
):
    exit_code, out, _ = _run(store, *search_args, capsys=capsys)

    assert exit_code == 0
    lines = out.rstrip("\n").splitlines()
    if expect_note:
        assert lines[-1] == UNATTRIBUTED_NOTE
        assert lines[-2].startswith("docs: "), (
            "the note trails the scoreboard, never displaces it"
        )
    else:
        assert UNATTRIBUTED_NOTE not in out


def test_the_unattributed_note_comes_after_the_telemetry_failure_note(store, capsys):
    (store / "telemetry.jsonl").mkdir()

    _, out, _ = _run(store, "1000001", capsys=capsys)

    tail = out.rstrip("\n").splitlines()[-3:]
    assert tail[0].startswith("docs: ")
    assert tail[1].startswith("note: telemetry not recorded")
    assert tail[2] == UNATTRIBUTED_NOTE


def test_the_unattributed_note_does_not_inflate_context_bytes(store, capsys):
    _run(store, "1000001", capsys=capsys)
    without = _last_telemetry(store)["context_bytes"]
    _run(store, "1000001", "--session", "20260823-cache", capsys=capsys)
    with_session = _last_telemetry(store)["context_bytes"]

    assert without == with_session


# ---------------------------------------------------------------------------
# `engmem search --role <role>` — role-addressed retrieval
# ---------------------------------------------------------------------------


def test_search_role_flag_returns_the_requested_role_section(store, capsys):
    """The reader must never mistake a role-filtered block for an ordinary one, so every result
    header carries the role it was filtered to."""
    exit_code, out, err = _run(store, "1000001", "--role", "decisions", capsys=capsys)

    assert exit_code == 0
    # a whole line of its own, which the `[role: decisions]` marker inside a result header
    # cannot satisfy — an `in out` substring check here is pinned by the markers, not by the
    # lead line it is meant to be about
    assert "role: decisions" in out.splitlines()
    assert "1000001-response-cache" in out
    for line in out.splitlines():
        if line.startswith("### "):
            assert "[role: decisions]" in line


def test_search_role_flag_states_when_no_matched_document_has_the_role(store, capsys):
    """An absent role must be reported by name, not folded into a plain, indistinguishable miss."""
    exit_code, out, err = _run(store, "1000001", "--role", "graph", capsys=capsys)

    assert exit_code == 0
    assert "role: graph" in out
    assert "none" in out.lower()


def test_search_unknown_role_exits_two_and_names_the_valid_roles(store, capsys):
    exit_code, out, err = _run(store, "1000001", "--role", "not-a-real-role", capsys=capsys)

    assert exit_code == 2
    assert "not-a-real-role" in out
    assert "not-a-real-role" in err
    assert "decisions" in out  # a known role, named as part of the valid list
    assert "lessons" in out


def test_search_role_flag_logs_role_specific_telemetry(store, capsys):
    _run(store, "1000001", "--role", "decisions", capsys=capsys)

    record = _last_telemetry(store)
    assert record["role"] == "decisions"
    # both fixture docs matched by the numeric id token carry a Decision Log — `surfaced` must
    # reflect the role-filtered documents actually shown, not an arbitrary single one
    assert "1000001-response-cache" in record["surfaced"]
    assert record["surfaced"][0] == "1000001-response-cache", (
        "the higher-scoring document must be surfaced first"
    )


@on_a_shared_runner
def test_search_role_flag_completes_within_the_same_latency_class(
    store, capsys, tmp_path, monkeypatch
):
    """Role-addressed retrieval must not regress search's own latency budget."""
    assert _clone_store(store, 25) >= 200

    # the cache goes to tmp, not the developer's real `~/.cache/engmem`. The assertion is a
    # *ratio* against plain search, which is what "the same latency class" means: a
    # wall-clock constant measured the hardware and whatever else it was doing
    cache_home = tmp_path / "cache-home"
    monkeypatch.setattr(cache, "cache_root", lambda: cache_home)

    plain = _cold_floor(["search", "platform", "--store", str(store)], cache_home, capsys)
    role = _cold_floor(
        ["search", "platform", "--role", "decisions", "--store", str(store)], cache_home, capsys
    )

    assert role < plain * 1.5, (plain, role)


# ---------------------------------------------------------------------------
# `engmem roles` — discoverability of the role vocabulary
# ---------------------------------------------------------------------------


def test_roles_subcommand_lists_the_known_vocabulary_with_store_coverage(store, capsys):
    exit_code = main(["roles", "--store", str(store)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "decisions" in captured.out
    # the fixture store has several Decision Log sections — a populated role
    assert re.search(r"decisions\s+\d*[1-9]\d*\s+document", captured.out)
    # `graph` (Knowledge Graph) has no example in the fixture store — an empty role
    # is worth surfacing before a caller wastes a query asking for it
    assert re.search(r"graph\s+0\s+document", captured.out)


# ---------------------------------------------------------------------------
# `channel` + `context_bytes` — the CLI side of both.
# ---------------------------------------------------------------------------


def test_search_records_cli_as_the_channel(store, capsys):
    _run(store, "1000001", capsys=capsys)

    assert _last_telemetry(store)["channel"] == "cli"


def test_search_context_bytes_matches_the_rendered_result_for_a_hit(store, capsys):
    from engmem.output import render_search_results
    from engmem.scoring import search as run_search
    from engmem.spine import load_store

    _run(store, "1000001", capsys=capsys)

    docs = load_store(store / "sessions").docs
    outcome = run_search(docs, "1000001")
    expected_text = render_search_results(outcome, docs)

    assert _last_telemetry(store)["context_bytes"] == len(expected_text.encode("utf-8"))


def test_search_context_bytes_is_near_zero_for_a_miss(store, capsys):
    from engmem.output import render_no_match

    _run(store, "nonexistent-term-xyz", capsys=capsys)

    record = _last_telemetry(store)
    expected = len(render_no_match().encode("utf-8"))
    assert record["context_bytes"] == expected
    assert record["context_bytes"] < 50, "a miss must put almost nothing in context"


def test_search_context_tokens_estimate_is_derived_from_context_bytes(store, capsys):
    from engmem.telemetry import estimate_tokens

    _run(store, "1000001", capsys=capsys)

    record = _last_telemetry(store)
    assert record["context_tokens_estimate"] == estimate_tokens(record["context_bytes"])


def test_search_role_flag_records_cli_channel_and_context_bytes(store, capsys):
    from engmem.output import render_role_search_results
    from engmem.scoring import search_with_role_sections
    from engmem.spine import load_store

    _run(store, "1000001", "--role", "decisions", capsys=capsys)

    docs = load_store(store / "sessions").docs
    docs_outcome, role_map = search_with_role_sections(docs, "1000001")
    expected_text = render_role_search_results(docs_outcome, role_map, "decisions")
    record = _last_telemetry(store)

    assert record["channel"] == "cli"
    assert record["context_bytes"] == len(expected_text.encode("utf-8"))


# ---------------------------------------------------------------------------
# `engmem telemetry` — the reading surface over `telemetry.jsonl`
# ---------------------------------------------------------------------------


def test_telemetry_subcommand_reports_no_rows_for_a_fresh_store(store, capsys):
    exit_code = main(["telemetry", "--store", str(store)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "0 row" in out


def test_telemetry_subcommand_reports_recorded_rows_split_by_channel(store, capsys):
    _run(store, "1000001", capsys=capsys)
    capsys.readouterr()

    exit_code = main(["telemetry", "--store", str(store)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "cli" in out
    assert "1" in out


# ---------------------------------------------------------------------------
# `--version`
# ---------------------------------------------------------------------------


def test_version_flag_prints_the_version_on_stdout_and_exits_zero(capsys, monkeypatch):
    """`engmem --version` used to answer "the following arguments are required: command", though
    the bug template asks for it — and it must answer without a subcommand or a usable store."""
    from engmem import __version__

    monkeypatch.setenv("ENGMEM_HOME", "/nonexistent/path/that/does/not/exist")

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out
    assert "error" not in captured.out.lower()
    assert captured.err == "", "the version is an answer, not a diagnostic"


NAVIGATION_MISS_CASES = [
    # a navigation miss is the opposite failure to a search miss, and merging them hides the
    # one that decides semantic search
    pytest.param(
        "navigation_miss:\n  - doc: 20260102-cache-warmer\n    query: warm-up on boot\n",
        True,
        id="present",
    ),
    # a store with no recorded misses must not grow a zero-count line that reads like a finding
    pytest.param("", False, id="absent"),
]


@pytest.mark.parametrize(("navigation_miss_frontmatter", "expect_reported"), NAVIGATION_MISS_CASES)
def test_telemetry_reports_navigation_misses_only_when_present(
    tmp_path, capsys, navigation_miss_frontmatter, expect_reported
):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "20260101-widget-cache.md").write_text(
        "---\nid: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        "status: active\ntags: [platform]\nentities: [WidgetCache]\n"
        + navigation_miss_frontmatter
        + "---\n\n## 8. Decision Log\n\nBody.\n",
        encoding="utf-8",
    )

    assert main(["telemetry", "--store", str(tmp_path)]) == 0
    out = capsys.readouterr().out.lower()

    assert ("navigation miss" in out) == expect_reported
    if expect_reported:
        assert "1" in out


def test_search_output_is_utf8_whatever_the_console_code_page_is(tmp_path):
    """`§` exists in neither cp437 nor cp866; four documents, since with one the df ceiling zeroes
    every term and no `§` appears."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "20260101-widget-cache.md").write_text(
        "---\nid: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        "task_date: 2026-01-01\nstatus: active\ntags: [platform]\nentities: [WidgetCache]\n"
        "---\n\n## 8. Decision Log\n\nEviction runs on boot for WidgetCache.\n",
        encoding="utf-8",
    )
    for n in (2, 3, 4):
        (sessions / f"2026010{n}-other.md").write_text(
            f"---\nid: 2026010{n}-other\ntitle: Other {n}\ndate: 2026-01-0{n}\n"
            f"task_date: 2026-01-0{n}\nstatus: active\ntags: [platform]\n"
            f"entities: [SweeperJob{n}]\n---\n\n## 8. Decision Log\n\n"
            f"Unrelated decision number {n} about scheduling.\n",
            encoding="utf-8",
        )

    result = subprocess.run(
        [sys.executable, "-m", "engmem.cli", "search", "WidgetCache eviction",
         "--store", str(tmp_path)],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp437"},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    text = result.stdout.decode("utf-8")  # raises if the process wrote another encoding
    assert "§" in text, "the section locator must survive the console's code page"


@requires_permission_enforcement
def test_telemetry_reports_an_unlistable_sessions_directory(tmp_path, capsys):
    """It read the store directly, so an unlistable `sessions/` rendered byte-identically to a
    store with no navigation misses — the failure `scan_error` exists to prevent."""
    store = tmp_path / "store"
    sessions = store / "sessions"
    sessions.mkdir(parents=True)
    (store / "telemetry.jsonl").write_text("", encoding="utf-8")

    os.chmod(sessions, 0o000)
    try:
        exit_code = main(["telemetry", "--store", str(store)])
        captured = capsys.readouterr()
    finally:
        os.chmod(sessions, 0o755)

    assert exit_code == 0
    # the agent reads stdout only, so the cause has to be there and not only on stderr
    assert "cannot list directory" in captured.out
    assert "navigation misses" not in captured.out


def _make_the_log_unreadable(log: Path) -> None:
    log.write_text('{"result": "hit", "channel": "cli"}\n', encoding="utf-8")
    os.chmod(log, 0o000)


def _make_the_log_undecodable(log: Path) -> None:
    log.write_bytes(b'{"result": "hit", "channel": "cli"}\n\xff\xfe not utf-8\n')


UNUSABLE_TELEMETRY_LOG_CASES = [
    # it crashed with a traceback and exit 1
    pytest.param(
        _make_the_log_unreadable, (), marks=requires_permission_enforcement, id="unreadable"
    ),
    # a non-UTF-8 byte raises `UnicodeDecodeError`, which is a `ValueError` and not an
    # `OSError`, so it walked past the handler and out as a traceback with nothing on stdout
    pytest.param(_make_the_log_undecodable, ("decode",), id="undecodable"),
]


@pytest.mark.parametrize(("make_log", "extra_substrings"), UNUSABLE_TELEMETRY_LOG_CASES)
def test_telemetry_reports_a_log_it_cannot_read_instead_of_zero_rows(
    tmp_path, capsys, make_log, extra_substrings
):
    """`summarize` reads a *missing* file as zero rows, so swallowing either of these would have
    reported "0 row(s)" for a store full of searches."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    log = store / "telemetry.jsonl"
    make_log(log)
    try:
        exit_code = main(["telemetry", "--store", str(store)])
        captured = capsys.readouterr()
    finally:
        os.chmod(log, 0o644)

    assert exit_code == 2
    assert "0 row" not in captured.out, "a log engmem cannot read must never read as an empty one"
    for stream in (captured.out, captured.err):
        assert str(log) in stream and "cannot read" in stream
        for substring in extra_substrings:
            assert substring in stream, "the cause has to be named, not just the path"


def test_telemetry_does_not_blame_the_file_for_a_value_error_it_cannot_explain(
    tmp_path, capsys, monkeypatch
):
    """The boundary catches an undecodable file, not every `ValueError`: a bad row is `summarize`'s
    own business now, so anything else left here would be a bug wearing a "cannot read" message.

    The mock states that contract; what makes it true is
    `test_telemetry_survives_a_malformed_row_and_still_totals_the_rest`, which drives the real
    reader with every row shape known to raise a `ValueError` out of the decoder."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    (store / "telemetry.jsonl").write_text('{"result": "hit", "channel": "cli"}\n', encoding="utf-8")

    def boom(_path):
        raise ValueError("not a decoding problem")

    monkeypatch.setattr("engmem.cli.summarize_telemetry", boom)

    with pytest.raises(ValueError):
        main(["telemetry", "--store", str(store)])

    assert "cannot read" not in capsys.readouterr().out


MALFORMED_TELEMETRY_ROWS = [
    # a wrongly-typed count: `int()` raised out of `summarize`, and the command answered
    # "cannot read <path>" — one bad ROW given a whole-FILE verdict
    pytest.param('{"result": "hit", "channel": "cli", "context_bytes": "oops"}', id="text-count"),
    # valid JSON that is not an object: `.get` raised AttributeError past every handler,
    # exiting 1 with a traceback and nothing on stdout
    pytest.param("123", id="bare-number"),
    pytest.param("[1, 2]", id="bare-list"),
    pytest.param("null", id="bare-null"),
    pytest.param('{"result": "hit", "channel": 123}', id="non-string-channel"),
    # a number longer than CPython will convert: `json.loads` raises a plain `ValueError` from
    # inside the decoder, before the `int()` guard can see it, and only `JSONDecodeError` was
    # caught — so the row escaped as a traceback, exit 1, nothing on stdout
    pytest.param('{"context_bytes": ' + "9" * 5000 + "}", id="oversized-number"),
    # the same escape by another door: nesting deep enough to raise `RecursionError`,
    # which is not a `ValueError` at all
    pytest.param("[" * 200_000 + "]" * 200_000, id="deeply-nested"),
]


@pytest.mark.parametrize("malformed_row", MALFORMED_TELEMETRY_ROWS)
def test_telemetry_survives_a_malformed_row_and_still_totals_the_rest(
    tmp_path, capsys, malformed_row
):
    """A bad *byte* invalidates every offset in the file and is fatal; a bad *row* is not, so it
    is tallied as unreadable and the remaining history is still reported (§5)."""
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    log = store / "telemetry.jsonl"
    log.write_text(
        '{"result": "hit", "channel": "cli", "context_bytes": 100}\n' + malformed_row + "\n",
        encoding="utf-8",
    )

    exit_code = main(["telemetry", "--store", str(store)])

    captured = capsys.readouterr()
    assert exit_code == 0, captured.out
    assert "cannot read" not in captured.out, "one bad row is not a verdict on the file"
    assert "1 row(s)" in captured.out, "the readable row is still counted"
    assert "unreadable" in captured.out, "the bad row is reported, not silently dropped"


# ---------------------------------------------------------------------------
# usage errors — argparse's own failures still owe stdout a line (SPEC §10 VIII)
# ---------------------------------------------------------------------------


USAGE_ERROR_CASES = [
    pytest.param([], "command", id="no-subcommand"),
    pytest.param(["search"], "query", id="missing-positional"),
    pytest.param(["search", "q", "--role"], "role", id="flag-without-value"),
    pytest.param(["backfill"], "--id", id="neither-target-flag"),
    pytest.param(["roles", "--nonsense"], "nonsense", id="unknown-flag"),
    pytest.param(["definitely-not-a-command"], "definitely-not-a-command", id="unknown-command"),
    # "mcp" as a *query* is an ordinary search, and its usage errors still owe stdout a line:
    # the mcp exemption follows the dispatched command, not the mere presence of the word
    pytest.param(["search", "mcp", "--nonsense"], "nonsense", id="mcp-as-a-search-query"),
    # the same rule one step earlier: a misplaced option's VALUE is not the command either, so
    # `mcp` spelled as a --store path leaves the mirror on for the `search` this really is
    pytest.param(["--store", "mcp", "search"], "--store", id="mcp-as-a-misplaced-store-value"),
    # and the skip is the grammar's, not one hardcoded option's
    pytest.param(["--role", "mcp", "search"], "--role", id="mcp-as-a-misplaced-role-value"),
    # `--id` is declared inside backfill's mutually exclusive group, whose `add_argument` is
    # `argparse._ActionsContainer`'s and not the parser's — so it went unrecorded, `mcp` read as
    # the command, and the mirror was switched off for a `backfill` that owed stdout a line
    pytest.param(["--id", "mcp", "backfill", "--all"], "--id", id="mcp-as-a-misplaced-id-value"),
]


@pytest.mark.parametrize(("argv", "expected_substring"), USAGE_ERROR_CASES)
def test_a_usage_error_is_named_on_stdout_as_well_as_stderr(capsys, argv, expected_substring):
    """argparse writes its errors to stderr only, so a mistyped command was invisible to the
    agent, which reads stdout and never reads stderr."""
    with pytest.raises(SystemExit) as exit_info:
        main(argv)

    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert expected_substring in captured.out
    assert captured.out.startswith("error: "), captured.out
    assert captured.err != "", "argparse's own usage text still belongs on stderr"


MCP_USAGE_ERROR_CASES = [
    pytest.param(["mcp", "--nonsense"], id="unknown-flag"),
    pytest.param(["mcp", "extra-positional"], id="unknown-positional"),
    pytest.param(["mcp", "--store"], id="flag-without-value"),
    # the boundary: `--store` before the subcommand is the natural mistake in a hand-edited
    # claude_desktop_config.json args array. The root parser rejects `x` as the command,
    # so mcp's own subparser is never reached and only the root guard keeps stdout clean
    pytest.param(["--store", "x", "mcp"], id="store-flag-before-the-subcommand"),
    # the store path happens to spell a subcommand: read as the command, it made this look like
    # a `search` and put a plain line on the client's protocol channel
    pytest.param(["--store", "search", "mcp"], id="store-value-spelling-a-subcommand"),
    pytest.param(["--store=search", "mcp"], id="same-again-in-the-equals-form"),
]


@pytest.mark.parametrize("argv", MCP_USAGE_ERROR_CASES)
def test_mcp_usage_errors_keep_stdout_empty_for_the_protocol(capsys, argv):
    """§5: on this invocation stdout carries JSON-RPC and nothing else — including before
    parsing has finished, where the *root* parser, not mcp's own, reports the failure."""
    with pytest.raises(SystemExit) as exit_info:
        main(argv)

    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "", "a plain line here is an unparseable frame to the client"
    assert captured.err != "", "the diagnostic still has to exist, on stderr"


def test_mcp_help_is_the_one_thing_that_still_reaches_stdout(capsys):
    """§5 carves `--help` out of the mcp silence rule: argparse's help action prints to stdout
    and exits 0 without starting the protocol loop, and only a human ever types it."""
    with pytest.raises(SystemExit) as exit_info:
        main(["mcp", "--help"])

    captured = capsys.readouterr()
    assert exit_info.value.code == 0
    assert "--store" in captured.out, "the help a human asked for is an answer, not an error"


def _declared_value_taking_options(parser: argparse.ArgumentParser) -> set[str]:
    """Every value-taking option this parser declares, walked through its argument GROUPS —
    a route independent of the one `_Parser.value_taking_options` reads."""
    declared = set()
    for group in (*parser._action_groups, *parser._mutually_exclusive_groups):
        for action in group._group_actions:
            if action.nargs != 0:
                declared.update(action.option_strings)
    return declared


def test_every_value_taking_option_in_the_tree_is_known_to_the_mcp_guard():
    """§5 promises the value-taking set is read off the parsers themselves, so a new option
    cannot silently reopen the gap. It was read off an intercepted `add_argument`, which an
    argument group never calls — a future option declared in a group must fail this."""
    parser = build_parser([])
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    known = parser.value_taking_options.union(
        *(sub.value_taking_options for sub in subparsers.choices.values())
    )

    declared = _declared_value_taking_options(parser)
    for sub in subparsers.choices.values():
        declared |= _declared_value_taking_options(sub)

    assert declared, "the walk found nothing — it is no longer reaching the real grammar"
    assert declared <= known, (
        f"declared but invisible to the mcp guard: {sorted(declared - known)}"
    )
