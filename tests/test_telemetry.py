import json

import pytest

from conftest import fixture_docs

from engmem.scoring import search
from engmem.spine import load_store
from engmem.telemetry import log_search


def test_log_search_appends_one_json_line_with_expected_schema(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
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


@pytest.mark.parametrize(
    "query, expected_result, expect_empty_hits",
    [
        pytest.param("nonexistent-term-xyz", "miss", True, id="miss"),
        pytest.param("MQ", "ambiguous", False, id="ambiguous"),
    ],
)
def test_log_search_records_the_result_label(tmp_path, query, expected_result, expect_empty_hits):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
    outcome = search(docs, query)

    log_search(jsonl_path, query=query, n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["result"] == expected_result
    if expect_empty_hits:
        assert record["hits"] == []


def test_log_search_counts_superseded_redirect_as_hit(tmp_path):
    """M4: a surfaced redirect did return prior context, so logging it as a miss would understate
    hit rate at Gate 1."""
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
    docs = fixture_docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)
    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_log_search_reports_failure_instead_of_raising_when_path_is_a_directory(tmp_path):
    """D6: a write failure must come back as a value, not abort a search that already printed a
    correct answer."""
    jsonl_path = tmp_path / "telemetry.jsonl"
    jsonl_path.mkdir()
    docs = fixture_docs()
    outcome = search(docs, "1000001")

    error = log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    assert isinstance(error, str) and error, "must return a non-empty reason, not None"


def test_log_search_returns_none_on_success(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
    outcome = search(docs, "1000001")

    error = log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    assert error is None


# ---------------------------------------------------------------------------
# session_id + surfaced — the Gate 1 join key and its scope (P0)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "session_kwargs, expected_session_id",
    [
        pytest.param({"session_id": "20260823-cache-ttl"}, "20260823-cache-ttl", id="given"),
        pytest.param({}, None, id="defaults-to-explicit-null"),
    ],
)
def test_log_search_records_the_session_id(tmp_path, session_kwargs, expected_session_id):
    """An unattributed row and a row written before the field existed must stay distinguishable at
    review time, so absence is an explicit null, not an omitted key."""
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome, **session_kwargs)

    record = json.loads(jsonl_path.read_text().strip())
    assert "session_id" in record
    assert record["session_id"] == expected_session_id


def test_surfaced_lists_the_documents_the_reader_was_actually_shown(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["surfaced"] == [h.doc.id for h in outcome.hits[:3]]
    assert len(record["surfaced"]) <= 3


def test_surfaced_includes_a_superseded_redirect_and_its_successor(tmp_path):
    """A redirect target reaches the agent without ever being a hit, so omitting it would make
    honest reuse look fabricated."""
    docs = fixture_docs()
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
# `log_role_search` — telemetry for role-addressed retrieval (`search --role`, the MCP
# `engmem_search_by_role` tool).
# ---------------------------------------------------------------------------


def test_log_role_search_records_the_role_and_the_role_filtered_surfaced_set(tmp_path):
    from engmem.output import RoleHit
    from engmem.scoring import search_with_role_sections
    from engmem.telemetry import log_role_search

    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
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
# `channel` — distinguishes automatically which surface ran the search (CLI process vs MCP tool
# call).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel_kwargs, expected_channel",
    [
        pytest.param({}, "cli", id="defaults-to-cli-for-backward-compatible-callers"),
        pytest.param({"channel": "mcp"}, "mcp", id="explicit-channel-is-recorded"),
    ],
)
def test_log_search_records_the_channel(tmp_path, channel_kwargs, expected_channel):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome, **channel_kwargs)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["channel"] == expected_channel


def test_log_role_search_records_the_channel_it_was_given(tmp_path):
    from engmem.telemetry import log_role_search

    jsonl_path = tmp_path / "telemetry.jsonl"

    log_role_search(
        jsonl_path, query="1000001", role="decisions", n_docs=9, role_hits=[], channel="mcp"
    )

    record = json.loads(jsonl_path.read_text().strip())
    assert record["channel"] == "mcp"


# ---------------------------------------------------------------------------
# `context_bytes` — exact UTF-8 byte count of the document-derived render the caller actually
# receives (`render_search_results`/`render_role_search_results`/ `render_no_match`'s own return
# value), NOT the scoreboard bookkeeping line, NOT the stray-markdown-files discoverability note,
# and NOT the telemetry failure note itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "context_bytes_kwargs, expected_context_bytes",
    [
        pytest.param({"context_bytes": 321}, 321, id="given"),
        pytest.param({}, 0, id="defaults-to-zero-for-backward-compatible-callers"),
    ],
)
def test_log_search_records_context_bytes(tmp_path, context_bytes_kwargs, expected_context_bytes):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome, **context_bytes_kwargs)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["context_bytes"] == expected_context_bytes


def test_log_role_search_records_context_bytes_when_given(tmp_path):
    from engmem.telemetry import log_role_search

    jsonl_path = tmp_path / "telemetry.jsonl"

    log_role_search(
        jsonl_path, query="1000001", role="decisions", n_docs=9, role_hits=[], context_bytes=55
    )

    record = json.loads(jsonl_path.read_text().strip())
    assert record["context_bytes"] == 55


# ---------------------------------------------------------------------------
# `context_tokens_estimate` — an ESTIMATE, not a measurement (exact tokenisation needs the calling
# model's own tokeniser, which would be a network call this codebase never makes).
# ---------------------------------------------------------------------------


def test_log_search_records_a_token_estimate_derived_from_context_bytes(tmp_path):
    from engmem.telemetry import estimate_tokens

    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
    outcome = search(docs, "1000001")

    log_search(jsonl_path, query="1000001", n_docs=len(docs), outcome=outcome, context_bytes=700)

    record = json.loads(jsonl_path.read_text().strip())
    assert record["context_tokens_estimate"] == estimate_tokens(700)
    assert record["context_tokens_estimate"] > 0


def test_zero_context_bytes_estimates_zero_tokens(tmp_path):
    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
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
    """This corpus tokenises denser than the ~4-characters-per-token prose rule, so the ratio must
    estimate more, not fewer."""
    from engmem.telemetry import estimate_tokens

    prose_rule_of_thumb = 1000 // 4
    assert estimate_tokens(1000) > prose_rule_of_thumb


# ---------------------------------------------------------------------------
# `summarize` — the reading surface's data source: aggregate totals, split by channel, from a
# `telemetry.jsonl` file.
# ---------------------------------------------------------------------------


def test_summarize_missing_file_reports_zero_rows(tmp_path):
    from engmem.telemetry import summarize

    result = summarize(tmp_path / "telemetry.jsonl")

    assert result.total == 0
    assert result.by_channel == []


def test_summarize_splits_totals_by_channel(tmp_path):
    from engmem.telemetry import summarize

    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
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
    docs = fixture_docs()
    log_search(
        jsonl_path, query="1000001", n_docs=len(docs), outcome=search(docs, "1000001"),
        channel="cli", context_bytes=100,
    )
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write("not valid json at all\n")

    result = summarize(jsonl_path)

    assert result.total == 1
    assert result.unreadable == 1


MALFORMED_ROW_CASES = [
    # a field of the wrong type: `int()` raised out of `summarize` and the CLI reported the
    # whole *file* as unreadable, on the strength of one row
    pytest.param('{"result": "hit", "channel": "cli", "context_bytes": "oops"}', id="text-count"),
    pytest.param('{"result": "hit", "channel": "cli", "context_bytes": [1, 2]}', id="list-count"),
    pytest.param('{"result": "hit", "channel": "cli", "context_bytes": Infinity}', id="infinite"),
    # valid JSON that is not an object: `.get` raised AttributeError, out of every handler
    pytest.param("123", id="bare-number"),
    pytest.param("[1, 2]", id="bare-list"),
    pytest.param("null", id="bare-null"),
    # a non-string channel made the by-channel sort compare an int with a str
    pytest.param('{"result": "hit", "channel": 123}', id="non-string-channel"),
]


@pytest.mark.parametrize("malformed_line", MALFORMED_ROW_CASES)
def test_summarize_counts_a_malformed_row_as_unreadable_and_keeps_the_rest(
    tmp_path, malformed_line
):
    """One truncated or half-written append must not cost the reader the whole history — the
    same rule an unparseable line already followed."""
    from engmem.telemetry import summarize

    jsonl_path = tmp_path / "telemetry.jsonl"
    docs = fixture_docs()
    log_search(
        jsonl_path, query="1000001", n_docs=len(docs), outcome=search(docs, "1000001"),
        channel="cli", context_bytes=100,
    )
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(malformed_line + "\n")

    result = summarize(jsonl_path)

    assert result.unreadable == 1
    assert result.total == 1, "the good row is still totalled"
    assert result.overall.context_bytes == 100, "no partial count from the bad row"
    assert {c.channel for c in result.by_channel} == {"cli"}


def test_summarize_treats_a_missing_channel_field_as_unknown(tmp_path):
    """A row written before this field existed must stay visible rather than vanish or crash the
    reader."""
    from engmem.telemetry import summarize

    jsonl_path = tmp_path / "telemetry.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"query": "x", "result": "hit"}) + "\n")

    result = summarize(jsonl_path)

    assert result.total == 1
    by_name = {c.channel: c for c in result.by_channel}
    assert by_name["unknown"].total == 1
