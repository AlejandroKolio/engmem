import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import on_a_shared_runner, redirect_home, requires_unreadable_paths

from engmem.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"


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


def test_missing_store_is_an_error_not_a_phantom_empty_store(tmp_path, capsys):
    """Review H4: a typo'd --store used to print "prior context: none found", exit 0,
    and materialise a stray telemetry.jsonl at the wrong path — indistinguishable from
    a genuinely empty store. Constitution VIII: fail loud, name the cause."""
    missing = tmp_path / "no-such-store"

    exit_code = main(["search", "anything", "--store", str(missing)])
    captured = capsys.readouterr()

    assert exit_code == 2, "usage/precondition failures are exit 2 by this project's convention"
    assert str(missing) in captured.err
    assert not missing.exists(), "search must not create a store as a side effect"


def test_empty_but_existing_store_still_reports_none_found(tmp_path, capsys):
    store_dir = tmp_path / "store"
    (store_dir / "sessions").mkdir(parents=True)

    exit_code = main(["search", "anything", "--store", str(store_dir)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "prior context: none found" in captured.out


@requires_unreadable_paths
def test_unreadable_sessions_dir_is_not_a_phantom_empty_store(tmp_path, capsys):
    """D2: `Path.glob` swallows the `PermissionError` an unscannable `sessions/`
    raises and yields an empty iterator — `sessions_dir.is_dir()` is still true, so
    the "store not found" branch never fires either, and stdout used to come out
    byte-identical to a store that genuinely holds zero documents."""
    store_dir = tmp_path / "store"
    sessions_dir = store_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "control.md").write_text("# Doc\n\nhello\n", encoding="utf-8")
    os.chmod(sessions_dir, 0o000)
    try:
        exit_code = main(["search", "hello", "--store", str(store_dir)])
        captured = capsys.readouterr()
    finally:
        os.chmod(sessions_dir, 0o755)

    assert exit_code == 0, "an unreadable sessions/ is a degraded result, not a usage error"
    assert "failed to load" in captured.out
    assert "unknown, not zero" in captured.out, (
        "the terse scoreboard count alone reads like one ordinary bad file, not an "
        "entire directory nobody could scan"
    )


def test_stderr_distinguishes_errors_from_warnings(store, capsys):
    """Review M8: broken.md (invalid YAML, doc excluded) and an empty-entities doc
    (processed with a warning) printed identically; §4 classifies them differently."""
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


@on_a_shared_runner
def test_sc002_search_completes_within_200ms_at_realistic_scale(store, capsys):
    # §3 sizes a realistic store at 20-200 docs; 8 fixtures alone would make this
    # bound meaningless (review M11), so replicate them up to ~200
    sessions = store / "sessions"
    originals = sorted(sessions.glob("*.md"))
    for i in range(25):
        for src in originals:
            clone = sessions / f"clone-{i}-{src.name}"
            content = src.read_text(encoding="utf-8", errors="replace")
            clone.write_text(
                content.replace(f"id: {src.stem}", f"id: clone-{i}-{src.stem}"),
                encoding="utf-8",
            )

    assert len(list(sessions.glob("*.md"))) >= 200

    start = time.perf_counter()
    main(["search", "platform", "--store", str(store)])
    elapsed = time.perf_counter() - start
    capsys.readouterr()

    assert elapsed < 0.2


def _clone_store(store: Path, times: int, originals: list[Path] | None = None) -> int:
    """Grows a store by replicating `originals` — the pristine fixtures, never the clones,
    or the second call multiplies the first call's output instead of the fixtures."""
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


def test_search_cost_stays_linear_in_the_number_of_documents(store, tmp_path, capsys):
    """The hardware-independent half of the latency budget, and the one worth having in CI:
    a wall-clock bound measures the machine, but the *shape* of the curve is the code's.
    Measured on the author's machine the cost is flat at ~0.47 ms per document from 48 to
    408 of them; an accidental quadratic would show as roughly the square of the size
    ratio instead."""
    originals = sorted((store / "sessions").glob("*.md"))
    small_docs = _clone_store(store, 5, originals)
    small = _best_of_three(store, capsys)

    large_docs = _clone_store(store, 25, originals)
    large = _best_of_three(store, capsys)

    size_ratio = large_docs / small_docs
    time_ratio = large / small

    # 2.0, chosen from measurement rather than taste: honest linear growth measures 4.2x
    # for 4.3x the documents, and a quadratic doing real per-pair work measures 9.5x. The
    # threshold sits at 8.6x — clear of the first, under the second.
    assert time_ratio < size_ratio * 2.0, (
        f"{small_docs} docs took {small * 1000:.0f}ms, {large_docs} took {large * 1000:.0f}ms — "
        f"{time_ratio:.1f}x the time for {size_ratio:.1f}x the documents. Linear would be "
        f"about {size_ratio:.1f}x, quadratic about {size_ratio ** 2:.0f}x."
    )


def test_failed_documents_are_counted_on_stdout(store, capsys):
    """stderr names the broken file; stdout must at least admit it exists, or an agent
    reading only stdout treats a partial store as the whole store."""
    (store / "sessions" / "unparseable.md").write_text(
        "---\nid: unparseable\ntags: [platform\n---\n\n## Pre-reg\n", encoding="utf-8"
    )

    code, out, err = _run(store, "sweeper", capsys=capsys)

    assert code == 0
    assert "failed to load" in out
    assert "unparseable.md" in err, "the name still belongs on stderr"


def test_tilde_in_store_path_is_expanded(tmp_path, monkeypatch):
    """A quoted `--store "~/notes"` reaches argv with a literal tilde; without
    expansion engmem silently creates a directory named `~` under the cwd."""
    from engmem.runtime import resolve_store

    redirect_home(monkeypatch, tmp_path)
    resolved = resolve_store("~/notes")

    assert resolved.is_absolute()
    assert "~" not in str(resolved)
    assert resolved == tmp_path / "notes"


def test_tilde_in_engmem_home_is_expanded(tmp_path, monkeypatch):
    from engmem.runtime import resolve_store

    redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGMEM_HOME", "~/notes")

    assert resolve_store(None) == tmp_path / "notes"


def test_telemetry_write_failure_still_prints_full_results_and_exits_zero(store, capsys):
    """D6: a telemetry write failure must not crash the process after search already
    printed a correct answer on stdout — an agent or wrapper gating on exit status
    would discard a perfectly good result, and one reading only stdout would never
    learn the measurement instrument stopped recording."""
    (store / "telemetry.jsonl").mkdir()

    exit_code, out, err = _run(store, "platform", capsys=capsys)

    assert exit_code == 0
    assert "docs:" in out, "the scoreboard footer must still be printed"
    assert "note:" in out.lower() and "telemetry" in out.lower(), (
        "a telemetry write failure must be surfaced on stdout, not swallowed"
    )


def test_store_not_found_error_is_also_printed_on_stdout(tmp_path, capsys):
    """D14: the consumer reads stdout and never stderr; a store-not-found error left
    only on stderr reads as an empty, silent run — indistinguishable from a genuine
    miss carrying zero information (verified: 0 bytes on stdout before the fix)."""
    missing = tmp_path / "no-such-store"

    exit_code = main(["search", "anything", "--store", str(missing)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out.strip() != "", "stdout must not be empty on a terminal failure"
    assert str(missing) in captured.out


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
    """No --store must go through the same `_resolve_store` precedence (ENGMEM_HOME,
    then the default) that `search` uses, not a hardcoded path."""
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


def test_mcp_subcommand_propagates_serve_exit_code(tmp_path, monkeypatch):
    pytest.importorskip("engmem.mcp_server")
    monkeypatch.setattr("engmem.mcp_server.serve", lambda resolved_store, **kwargs: 3)

    exit_code = main(["mcp", "--store", str(tmp_path / "store")])

    assert exit_code == 3


def test_mcp_subcommand_prints_nothing_to_stdout_even_for_a_missing_store(
    tmp_path, monkeypatch, capsys
):
    """The rule that breaks everything if violated: on this path stdout carries the
    MCP JSON-RPC protocol and nothing else. Even a store that does not exist yet must
    not provoke a banner, confirmation, or `_fail()`-style stdout line from the CLI
    wiring itself — any diagnosis of a missing store is `serve`'s job, on stderr."""
    pytest.importorskip("engmem.mcp_server")
    missing_store = tmp_path / "no-such-store"
    monkeypatch.setattr("engmem.mcp_server.serve", lambda resolved_store, **kwargs: 2)

    exit_code = main(["mcp", "--store", str(missing_store)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""


def test_mcp_subcommand_prints_nothing_to_stdout_on_success(tmp_path, monkeypatch, capsys):
    pytest.importorskip("engmem.mcp_server")
    monkeypatch.setattr("engmem.mcp_server.serve", lambda resolved_store, **kwargs: 0)

    exit_code = main(["mcp", "--store", str(tmp_path / "store")])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == ""




# ---------------------------------------------------------------------------
# --session — the Gate 1 join key (P0)
# ---------------------------------------------------------------------------


def _last_telemetry(store):
    return json.loads((store / "telemetry.jsonl").read_text().strip().splitlines()[-1])


def test_session_flag_is_recorded_in_telemetry(store, capsys):
    exit_code, out, _ = _run(store, "1000001", "--session", "20260823-cache", capsys=capsys)

    assert exit_code == 0
    assert _last_telemetry(store)["session_id"] == "20260823-cache"
    assert "20260823-cache" not in out, "the join key is for the log, not the agent's output"


def test_search_without_session_flag_logs_an_explicit_null(store, capsys):
    _run(store, "1000001", capsys=capsys)

    record = _last_telemetry(store)
    assert "session_id" in record and record["session_id"] is None


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_blank_session_is_treated_as_absent_not_as_an_unknown_session(store, capsys, raw):
    exit_code, _, _ = _run(store, "1000001", "--session", raw, capsys=capsys)

    assert exit_code == 0
    assert _last_telemetry(store)["session_id"] is None


def test_session_id_is_stripped_so_both_entry_paths_produce_the_same_key(store, capsys):
    _run(store, "1000001", "--session", "  20260823-cache  ", capsys=capsys)

    assert _last_telemetry(store)["session_id"] == "20260823-cache"


def test_unknown_session_id_does_not_gate_the_search(store, capsys):
    """The CLI does not know which ids exist, and a search refused because a draft was
    named a moment too early would be the ritual gating the answer (SPEC §6)."""
    exit_code, out, _ = _run(store, "1000001", "--session", "no-such-draft", capsys=capsys)

    assert exit_code == 0
    assert "1000001-response-cache" in out
    assert _last_telemetry(store)["session_id"] == "no-such-draft"


# ---------------------------------------------------------------------------
# `engmem search --role <role>` — role-addressed retrieval
# ---------------------------------------------------------------------------


def test_search_role_flag_returns_the_requested_role_section(store, capsys):
    exit_code, out, err = _run(store, "1000001", "--role", "decisions", capsys=capsys)

    assert exit_code == 0
    assert "role: decisions" in out
    assert "1000001-response-cache" in out


def test_search_role_flag_never_reads_as_an_ordinary_result(store, capsys):
    """The reader must never mistake a role-filtered block for an ordinary one — every
    role-filtered result names the role somewhere on stdout."""
    exit_code, out, err = _run(store, "1000001", "--role", "decisions", capsys=capsys)

    assert exit_code == 0
    for line in out.splitlines():
        if line.startswith("### "):
            assert "[role: decisions]" in line


def test_search_role_flag_states_when_no_matched_document_has_the_role(store, capsys):
    """The 1000001 fixture doc has no Knowledge Graph section — the role must be
    reported as absent by name, not folded into a plain, indistinguishable miss."""
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
    # both fixture docs matched by the numeric id token carry a Decision Log —
    # `surfaced` must reflect the role-filtered documents actually shown, not an
    # arbitrary single one
    assert "1000001-response-cache" in record["surfaced"]
    assert record["surfaced"][0] == "1000001-response-cache", (
        "the higher-scoring document must be surfaced first"
    )


@on_a_shared_runner
def test_search_role_flag_completes_within_the_same_latency_class(store, capsys):
    """Role-addressed retrieval must not regress search's own latency budget — see
    test_sc002_search_completes_within_200ms_at_realistic_scale for the ordinary path."""
    sessions = store / "sessions"
    originals = sorted(sessions.glob("*.md"))
    for i in range(25):
        for src in originals:
            clone = sessions / f"clone-{i}-{src.name}"
            content = src.read_text(encoding="utf-8", errors="replace")
            clone.write_text(
                content.replace(f"id: {src.stem}", f"id: clone-{i}-{src.stem}"),
                encoding="utf-8",
            )

    assert len(list(sessions.glob("*.md"))) >= 200

    start = time.perf_counter()
    main(["search", "platform", "--role", "decisions", "--store", str(store)])
    elapsed = time.perf_counter() - start
    capsys.readouterr()

    assert elapsed < 0.2


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


def test_roles_subcommand_missing_store_is_an_error(tmp_path, capsys):
    missing = tmp_path / "no-such-store"

    exit_code = main(["roles", "--store", str(missing)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert str(missing) in captured.out
    assert str(missing) in captured.err


@requires_unreadable_paths
def test_roles_unreadable_sessions_dir_is_not_a_phantom_empty_store(tmp_path, capsys):
    """D2, same gap `search` already closed: an unscannable `sessions/` must not
    read as a store with zero documents — `roles` used to drop `scan_error` on the
    floor entirely (stderr-only, via the ordinary `result.errors` loop), unlike
    `search`, which prints it to stdout too."""
    store_dir = tmp_path / "store"
    sessions_dir = store_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "control.md").write_text("# Doc\n\nhello\n", encoding="utf-8")
    os.chmod(sessions_dir, 0o000)
    try:
        exit_code = main(["roles", "--store", str(store_dir)])
        captured = capsys.readouterr()
    finally:
        os.chmod(sessions_dir, 0o755)

    assert exit_code == 0
    assert "unknown, not zero" in captured.out


@requires_unreadable_paths
def test_backfill_unreadable_sessions_dir_is_not_a_phantom_empty_store(tmp_path, capsys):
    """Same D2 gap, closed the same way for `backfill` (shares `_load_sessions`
    with `search`/`roles`): an unscannable `sessions/` must say so on stdout,
    not silently backfill against whatever the store happened to load."""
    store_dir = tmp_path / "store"
    sessions_dir = store_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "control.md").write_text("# Doc\n\nhello\n", encoding="utf-8")
    os.chmod(sessions_dir, 0o000)
    try:
        exit_code = main(["backfill", "--all", "--store", str(store_dir)])
        captured = capsys.readouterr()
    finally:
        os.chmod(sessions_dir, 0o755)

    assert exit_code == 0
    assert "unknown, not zero" in captured.out


# ---------------------------------------------------------------------------
# `channel` + `context_bytes` — the CLI side of both. `channel` must always
# read "cli" for a search run through this entry point; `context_bytes` must
# be the exact UTF-8 byte count of the document-derived render the reader was
# actually shown (`render_search_results`/`render_no_match`/
# `render_role_search_results`'s own return value) — not the scoreboard line,
# not the stray-markdown-files note, both printed separately and unrelated to
# prior-document content.
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


def test_version_flag_prints_the_version_on_stdout_and_exits_zero(capsys):
    """`engmem --version` used to answer "the following arguments are required: command"
    — an error for a question the tool can obviously answer. The bug report template asks
    reporters for their version, so this has to work without a store or a subcommand."""
    from engmem import __version__

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out
    assert captured.err == "", "the version is an answer, not a diagnostic"


def test_version_needs_no_store_and_no_subcommand(capsys, monkeypatch):
    monkeypatch.setenv("ENGMEM_HOME", "/nonexistent/path/that/does/not/exist")

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert "error" not in capsys.readouterr().out.lower()


def test_telemetry_reports_navigation_misses_separately_from_search_misses(tmp_path, capsys):
    """`result: miss` means the search ran and found nothing. A navigation miss is the
    opposite failure — the document was there and the search did not surface it. Counting
    them together would hide the one that decides whether semantic search is ever needed."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "20260101-widget-cache.md").write_text(
        "---\nid: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        "status: active\ntags: [platform]\nentities: [WidgetCache]\n"
        "navigation_miss:\n  - doc: 20260102-cache-warmer\n    query: warm-up on boot\n"
        "---\n\n## 8. Decision Log\n\nBody.\n",
        encoding="utf-8",
    )

    assert main(["telemetry", "--store", str(tmp_path)]) == 0
    out = capsys.readouterr().out

    assert "navigation miss" in out.lower()
    assert "1" in out


def test_telemetry_says_nothing_about_navigation_misses_when_there_are_none(tmp_path, capsys):
    """The common case stays quiet: a store with no recorded misses must not grow a
    zero-count line that reads like a finding."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "20260101-widget-cache.md").write_text(
        "---\nid: 20260101-widget-cache\ntitle: Widget cache\ndate: 2026-01-01\n"
        "status: active\ntags: [platform]\nentities: [WidgetCache]\n---\n\n"
        "## 8. Decision Log\n\nBody.\n",
        encoding="utf-8",
    )

    assert main(["telemetry", "--store", str(tmp_path)]) == 0
    assert "navigation miss" not in capsys.readouterr().out.lower()


def test_search_output_is_utf8_whatever_the_console_code_page_is(tmp_path):
    """Section locators print `§`, which does not exist in cp437 or cp866 — code pages a
    Windows console still commonly uses. Left to the ambient encoding the search dies with
    `UnicodeEncodeError` and the user gets a traceback instead of their results.

    Four documents, not one: with a single document every term sits in 100% of the corpus
    and the document-frequency ceiling zeroes it, so no section ever matches and the `§`
    the test is about never appears."""
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
