import json
from pathlib import Path

from engmem.scoring import search
from engmem.spine import load_store
from engmem.telemetry import log_search

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"


def _docs():
    return load_store(FIXTURES).docs


def test_log_search_appends_one_json_line_with_expected_schema(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["query"] == "1000001"
    assert record["n_docs"] == len(docs)
    assert record["result"] == "hit"
    assert isinstance(record["hits"], list)
    assert record["hits"][0]["id"] == "1000001-response-cache"
    assert "score" in record["hits"][0]
    assert "ts" in record


def test_log_search_records_miss(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "nonexistent-term-xyz")

    log_search(jsonl_path, query="nonexistent-term-xyz", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["result"] == "miss"
    assert record["hits"] == []


def test_log_search_records_ambiguous(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "MQ")

    log_search(jsonl_path, query="MQ", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["result"] == "ambiguous"


def test_log_search_counts_superseded_redirect_as_hit(tmp_path):
    """Author decision (review M4): a search that surfaced a superseded redirect DID
    return prior context — logging it as miss would understate hit-rate at Gate 1."""
    doc_dir = tmp_path / "sessions"
    doc_dir.mkdir()
    (doc_dir / "old-doc.md").write_text("""---
id: old-doc
title: Old Doc
date: 2026-05-01
task_date: 2026-05-01
status: superseded
superseded_by: new-doc
backfilled: false
tags: []
entities: [UniqueThing]
related: []
covers_files: []
verified_at_commit: abc
capture_minutes: 1
---

## Cold-start primer

Old.
""")
    from engmem.spine import load_store

    docs = load_store(doc_dir).docs
    outcome = search(docs, "UniqueThing")
    assert outcome.hits == []
    assert outcome.superseded_notes

    jsonl_path = tmp_path / "telemetry.jsonl"
    log_search(jsonl_path, query="UniqueThing", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["result"] == "hit"


def test_log_search_appends_without_truncating_prior_lines(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)
    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_log_search_reports_failure_instead_of_raising_when_path_is_a_directory(tmp_path):
    """D6: telemetry is the experiment's own measurement instrument (ENGMEM-SPEC.md
    §1); a write failure must come back to the caller as a value, not escape as an
    uncaught exception that would abort search after it already printed a correct
    answer."""
    jsonl_path = tmp_path / "telemetry.jsonl"
    jsonl_path.mkdir()
    docs = _docs()
    outcome = search(docs, "1000001")

    error = log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    assert isinstance(error, str) and error, "must return a non-empty reason, not None"


def test_log_search_returns_none_on_success(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    error = log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    assert error is None


# ---------------------------------------------------------------------------
# session_id + surfaced — the Gate 1 join key and its scope (P0)
# ---------------------------------------------------------------------------


def test_log_search_records_the_session_id_it_was_given(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(
        jsonl_path,
        query="1000001",
        n_docs=len(docs),
        outcome=outcome,
        session_id="20260823-cache-ttl",
    )

    record = json.loads(jsonl_path.read_text().strip())
    assert record["session_id"] == "20260823-cache-ttl"


def test_log_search_writes_an_explicit_null_session_when_none_was_given(tmp_path):
    """An unattributed row (the flag was not passed) and a row written before the field
    existed must be distinguishable at review time — an omitted key cannot do that."""
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert "session_id" in record
    assert record["session_id"] is None


def test_surfaced_lists_the_documents_the_reader_was_actually_shown(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["surfaced"] == [h.doc.id for h in outcome.hits[:3]]
    assert len(record["surfaced"]) <= 3


def test_surfaced_includes_a_superseded_redirect_and_its_successor(tmp_path):
    """The reverse check at Gate 1 asks whether a cited document could have been seen.
    A redirect target reaches the agent without ever being a hit, so leaving it out of
    `surfaced` would make an honest Reuse Log row look fabricated."""
    docs = _docs()
    outcome = search(docs, "MazeRenderer")
    assert outcome.superseded_notes, "fixture must produce a redirect"

    jsonl_path = tmp_path / "telemetry.jsonl"
    log_search(jsonl_path, query="MazeRenderer", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    note = outcome.superseded_notes[0]
    assert note.doc.id in record["surfaced"]
    assert note.successor.id in record["surfaced"]
    assert len(record["surfaced"]) == len(set(record["surfaced"])), "no duplicate ids"


# ---------------------------------------------------------------------------
# `log_role_search` — telemetry for role-addressed retrieval (`search --role`,
# the MCP `engmem_search_by_role` tool). A distinct function rather than an
# overload of `log_search`: the documents actually shown by a role search are
# the role-filtered subset (`output.select_role_hits`'s `kept`), not the
# underlying word-ranked `SearchOutcome.hits` — reusing `log_search` as-is would
# misreport `surfaced` as documents the role search may have skipped outright.
# ---------------------------------------------------------------------------


def test_log_role_search_records_the_role_and_the_role_filtered_surfaced_set(tmp_path):
    from engmem.output import RoleHit
    from engmem.scoring import search_with_role_sections
    from engmem.telemetry import log_role_search

    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome, role_map = search_with_role_sections(docs, "1000001")
    section = role_map["1000001-response-cache"]["decisions"]
    role_hits = [RoleHit(doc=docs[0], score=9.9, section=section)]
    # pick the actual doc object matching the id used above
    doc = next(d for d in docs if d.id == "1000001-response-cache")
    role_hits = [RoleHit(doc=doc, score=9.9, section=section)]

    error = log_role_search(
        jsonl_path,
        query="1000001",
        role="decisions",
        n_docs=len(docs),
        role_hits=role_hits,
    )

    assert error is None
    record = json.loads(jsonl_path.read_text().strip())
    assert record["role"] == "decisions"
    assert record["query"] == "1000001"
    assert record["surfaced"] == ["1000001-response-cache"]
    assert record["hits"] == [{"id": "1000001-response-cache", "score": 9.9}]
    assert record["result"] == "hit"
    assert "session_id" in record and record["session_id"] is None


def test_log_role_search_records_miss_when_no_document_carried_the_role(tmp_path):
    from engmem.telemetry import log_role_search

    jsonl_path = tmp_path / "telemetry.jsonl"

    error = log_role_search(
        jsonl_path, query="1000001", role="graph", n_docs=9, role_hits=[]
    )

    assert error is None
    record = json.loads(jsonl_path.read_text().strip())
    assert record["result"] == "miss"
    assert record["surfaced"] == []


def test_log_role_search_records_the_session_id_it_was_given(tmp_path):
    from engmem.telemetry import log_role_search

    jsonl_path = tmp_path / "telemetry.jsonl"

    log_role_search(
        jsonl_path,
        query="1000001",
        role="decisions",
        n_docs=9,
        role_hits=[],
        session_id="20260823-cache-ttl",
    )

    record = json.loads(jsonl_path.read_text().strip())
    assert record["session_id"] == "20260823-cache-ttl"


def test_log_role_search_reports_failure_instead_of_raising(tmp_path):
    from engmem.telemetry import log_role_search

    jsonl_path = tmp_path / "telemetry.jsonl"
    jsonl_path.mkdir()

    error = log_role_search(
        jsonl_path, query="1000001", role="decisions", n_docs=9, role_hits=[]
    )

    assert isinstance(error, str) and error


# ---------------------------------------------------------------------------
# `channel` — distinguishes automatically which surface ran the search (CLI
# process vs MCP tool call). Deliberately NOT the `Search Trace` vocabulary
# (`shell | paste | miss`, ENGMEM-SPEC.md §4): that field is the AGENT's own
# narrative self-report, written once into a saved document's body, about
# whether IT ran the shell command or a human pasted output for it — "paste"
# still means an ordinary CLI invocation happened somewhere, just not by the
# agent's own hand, so it cannot be relabelled "mcp" without lying about what
# actually ran. `channel` here is machine-recorded per telemetry row, at the
# moment of the request, by the code that IS that surface — it always reflects
# the true invocation path, and every CLI search and Desktop search stays a
# distinguishable, joinable population instead of merging into one.
# ---------------------------------------------------------------------------


def test_log_search_defaults_channel_to_cli_for_backward_compatible_callers(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["channel"] == "cli"


def test_log_search_records_the_channel_it_was_given(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome, channel="mcp")

    record = json.loads(jsonl_path.read_text().strip())
    assert record["channel"] == "mcp"


def test_log_role_search_records_the_channel_it_was_given(tmp_path):
    from engmem.telemetry import log_role_search

    jsonl_path = tmp_path / "telemetry.jsonl"

    log_role_search(
        jsonl_path, query="1000001", role="decisions", n_docs=9, role_hits=[], channel="mcp"
    )

    record = json.loads(jsonl_path.read_text().strip())
    assert record["channel"] == "mcp"


# ---------------------------------------------------------------------------
# `context_bytes` — exact UTF-8 byte count of the document-derived render the
# caller actually receives (`render_search_results`/`render_role_search_results`/
# `render_no_match`'s own return value), NOT the scoreboard bookkeeping line,
# NOT the stray-markdown-files discoverability note, and NOT the telemetry
# failure note itself. All three of those are about the STORE, not about prior
# documents, and none of them exists at all on some calls — folding them in
# would make `context_bytes` mean a different thing depending on which notes
# happened to fire that run, on top of double-charging document content for
# housekeeping text a caller would still see even from an empty store.
# ---------------------------------------------------------------------------


def test_log_search_records_context_bytes_when_given(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome, context_bytes=321)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["context_bytes"] == 321


def test_log_search_context_bytes_defaults_to_zero_for_backward_compatible_callers(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["context_bytes"] == 0


def test_log_role_search_records_context_bytes_when_given(tmp_path):
    from engmem.telemetry import log_role_search

    jsonl_path = tmp_path / "telemetry.jsonl"

    log_role_search(
        jsonl_path, query="1000001", role="decisions", n_docs=9, role_hits=[], context_bytes=55
    )

    record = json.loads(jsonl_path.read_text().strip())
    assert record["context_bytes"] == 55


# ---------------------------------------------------------------------------
# `context_tokens_estimate` — an ESTIMATE, not a measurement (exact
# tokenisation needs the calling model's own tokeniser, which would be a
# network call this codebase never makes). Named with an explicit `_estimate`
# suffix so it cannot be mistaken for a real count at analysis time, and
# derived from `context_bytes` via `engmem.telemetry.estimate_tokens` — see
# that function's docstring for the ratio, its calibration, and its stated
# error direction (a floor, not a ceiling).
# ---------------------------------------------------------------------------


def test_log_search_records_a_token_estimate_derived_from_context_bytes(tmp_path):
    from engmem.telemetry import estimate_tokens

    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome, context_bytes=700)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["context_tokens_estimate"] == estimate_tokens(700)
    assert record["context_tokens_estimate"] > 0


def test_zero_context_bytes_estimates_zero_tokens(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["context_tokens_estimate"] == 0


def test_estimate_tokens_rounds_up_so_any_nonzero_bytes_estimate_at_least_one_token():
    from engmem.telemetry import estimate_tokens

    assert estimate_tokens(0) == 0
    assert estimate_tokens(1) == 1
    assert estimate_tokens(-5) == 0, "a negative byte count is nonsensical, never negative tokens"


def test_estimate_tokens_uses_a_ratio_denser_than_the_plain_english_prose_rule_of_thumb():
    """The commonly-quoted rule of thumb for English prose is ~4 characters per
    token. This corpus is YAML front matter, section locators, doc ids and tables
    mixed with prose, which tokenises denser — the chosen ratio must estimate MORE
    tokens for the same byte count than the plain-prose rule would, not fewer."""
    from engmem.telemetry import estimate_tokens

    prose_rule_of_thumb = 1000 // 4
    assert estimate_tokens(1000) > prose_rule_of_thumb


# ---------------------------------------------------------------------------
# `summarize` — the reading surface's data source: aggregate totals, split by
# channel, from a `telemetry.jsonl` file. Never raises; a missing file reads
# as zero rows, and a line that fails to parse is counted separately rather
# than either crashing the read or silently vanishing.
# ---------------------------------------------------------------------------


def test_summarize_missing_file_reports_zero_rows(tmp_path):
    from engmem.telemetry import summarize

    result = summarize(tmp_path / "telemetry.jsonl")

    assert result.total == 0
    assert result.by_channel == []


def test_summarize_splits_totals_by_channel(tmp_path):
    from engmem.telemetry import summarize

    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    log_search(
        jsonl_path, query="1000001", n_docs=len(docs), outcome=search(docs, "1000001"),
        channel="cli", context_bytes=100,
    )
    log_search(
        jsonl_path, query="1000001", n_docs=len(docs), outcome=search(docs, "1000001"),
        channel="mcp", context_bytes=200,
    )
    log_search(
        jsonl_path, query="nonexistent-term-xyz", n_docs=len(docs),
        outcome=search(docs, "nonexistent-term-xyz"), channel="mcp", context_bytes=10,
    )

    result = summarize(jsonl_path)

    assert result.total == 3
    by_name = {c.channel: c for c in result.by_channel}
    assert by_name["cli"].total == 1
    assert by_name["cli"].hits == 1
    assert by_name["cli"].context_bytes == 100
    assert by_name["mcp"].total == 2
    assert by_name["mcp"].hits == 1
    assert by_name["mcp"].misses == 1
    assert by_name["mcp"].context_bytes == 210
    assert result.overall.total == 3
    assert result.overall.hits == 2
    assert result.overall.misses == 1
    assert result.overall.context_bytes == 310


def test_summarize_counts_unreadable_lines_separately_without_raising(tmp_path):
    from engmem.telemetry import summarize

    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = _docs()
    log_search(
        jsonl_path, query="1000001", n_docs=len(docs), outcome=search(docs, "1000001"),
        channel="cli", context_bytes=100,
    )
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write("not valid json at all\n")

    result = summarize(jsonl_path)

    assert result.total == 1
    assert result.unreadable == 1


def test_summarize_treats_a_missing_channel_field_as_unknown(tmp_path):
    """A row written before this field existed must stay visible in the summary
    instead of vanishing or crashing the reader."""
    from engmem.telemetry import summarize

    jsonl_path = tmp_path / "telemetry.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"query": "x", "result": "hit"}) + "\n")

    result = summarize(jsonl_path)

    assert result.total == 1
    by_name = {c.channel: c for c in result.by_channel}
    assert by_name["unknown"].total == 1
