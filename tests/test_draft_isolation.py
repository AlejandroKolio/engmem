"""US-01: a draft takes no part in ordinary search — not in its results and not in its ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_file
from mcp_harness import _call, _text

from engmem.cli import main
from engmem.scoring import search, search_with_role_sections
from engmem.spine import load_store

QUERY = "idempotency retry"


def _doc(doc_id: str, status: str, title: str, body: str, entities: str = "[]") -> str:
    return (
        f"---\nid: {doc_id}\ntitle: {title}\ndate: 2026-01-01\ntask_date: 2026-01-01\n"
        f"status: {status}\nsuperseded_by:\nbackfilled: false\ntags: []\n"
        f"entities: {entities}\nrelated: []\ncovers_files: []\n---\n\n{body}"
    )


PUBLISHED = {
    "1001-payment-retry": _doc(
        "1001-payment-retry", "active", "Payment retry handling",
        "## Decision Log\n\nAn idempotency key guards every retry of a charge.\n\n"
        "## Landmines\n\nThe gateway times out under load.\n",
    ),
    "1002-order-export": _doc(
        "1002-order-export", "active", "Order export job",
        "## Decision Log\n\nExport retries back off exponentially.\n\n"
        "## Cold-start primer\n\nThe exporter writes one file per order batch.\n",
    ),
    "1003-ledger-sync": _doc(
        "1003-ledger-sync", "active", "Ledger sync",
        "## Decision Log\n\nLedger rows carry an idempotency token from the source.\n\n"
        "## Landmines\n\nClock skew reorders ledger rows.\n",
    ),
}

DRAFT_ID = "1099-wip-retries"

DRAFT_V1 = _doc(
    DRAFT_ID, "draft", "Idempotency retry draft",
    "## Decision Log\n\nidempotency retry idempotency retry.\n\n"
    "## Landmines\n\nretry idempotency everywhere.\n\n"
    "## Cold-start primer\n\nidempotency.\n",
    entities="[IdempotencyKey, RetryPolicy]",
)

DRAFT_V2 = _doc(
    DRAFT_ID, "draft", "Retry draft rewritten",
    "## Decision Log\n\n" + "A long unrelated paragraph about something else. " * 40 + "\n\n"
    "## Landmines\n\nretry retry retry.\n",
)


def _write_store(root: Path, extra: dict[str, str]) -> Path:
    sessions = root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for path in sessions.glob("*.md"):
        path.unlink()
    for doc_id, text in {**PUBLISHED, **extra}.items():
        write_file(sessions, f"{doc_id}.md", text)
    return sessions


def _role_search(docs, query):
    return search_with_role_sections(docs, query)[0]


def _published_signature(sessions: Path, query: str, ranker=search) -> tuple:
    """Everything a published result exposes that ranking decides."""
    outcome = ranker(load_store(sessions).docs, query)
    hits = tuple(
        (
            h.doc.id,
            h.score,
            tuple(sorted((k, tuple(v)) for k, v in h.matched_fields.items())),
            tuple((s.section.locator, s.score) for s in h.section_hits),
            h.ambiguous,
        )
        for h in outcome.hits
    )
    notes = tuple((n.doc.id, n.score) for n in outcome.superseded_notes)
    return hits, notes, outcome.ambiguous


@pytest.mark.parametrize(
    "before,after",
    [
        pytest.param({}, {DRAFT_ID: DRAFT_V1}, id="draft_added"),
        pytest.param({DRAFT_ID: DRAFT_V1}, {DRAFT_ID: DRAFT_V2}, id="draft_changed"),
        pytest.param({DRAFT_ID: DRAFT_V1}, {}, id="draft_removed"),
    ],
)
@pytest.mark.parametrize("query", [QUERY, "idempotency", "retry", "ledger idempotency"])
@pytest.mark.parametrize(
    "ranker",
    [pytest.param(search, id="search"), pytest.param(_role_search, id="role_search")],
)
def test_ac_01_1_a_draft_does_not_move_published_results(
    tmp_path, before, after, query, ranker
):
    sessions = _write_store(tmp_path, before)
    expected = _published_signature(sessions, query, ranker)
    assert expected[0], "the scenario must have published hits to protect"

    _write_store(tmp_path, after)

    assert _published_signature(sessions, query, ranker) == expected


def _cli_search(store: Path, query: str, capsys) -> str:
    assert main(["search", query, "--store", str(store)]) == 0
    return capsys.readouterr().out


def _mcp_search(store: Path, query: str, capsys) -> str:
    result, is_error = _call(store, name="engmem_search", arguments={"query": query})
    assert not is_error
    return _text(result)


CHANNELS = [
    pytest.param(_cli_search, id="cli"),
    pytest.param(_mcp_search, id="mcp"),
]


def _without_scoreboard(output: str) -> list[str]:
    return [line for line in output.splitlines() if not line.startswith("docs: ")]


def _scoreboard(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("docs: "))


@pytest.mark.parametrize("run", CHANNELS)
def test_ac_01_1_only_the_scoreboard_moves_when_a_draft_is_added(tmp_path, run, capsys):
    _write_store(tmp_path, {})
    before = run(tmp_path, QUERY, capsys)

    _write_store(tmp_path, {DRAFT_ID: DRAFT_V1})
    after = run(tmp_path, QUERY, capsys)

    assert _without_scoreboard(after) == _without_scoreboard(before)
    assert "drafts: 0" in _scoreboard(before)
    assert "drafts: 1" in _scoreboard(after)


@pytest.mark.parametrize("run", CHANNELS)
def test_ac_01_2_a_term_only_a_draft_holds_finds_nothing(tmp_path, run, capsys):
    secret = "zanzibarquokka"
    draft = _doc(
        DRAFT_ID, "draft", f"Draft about {secret}",
        f"## Decision Log\n\nThe {secret} sentence lives only in this draft.\n",
        entities=f"[{secret}]",
    )
    _write_store(tmp_path, {DRAFT_ID: draft})

    out = run(tmp_path, secret, capsys)

    assert "prior context: none found" in out
    assert DRAFT_ID not in out
    assert "lives only in this draft" not in out
    assert "drafts: 1" in out


def test_ac_01_3_a_published_draft_is_found_and_ranked_as_published(tmp_path):
    sessions = _write_store(tmp_path, {DRAFT_ID: DRAFT_V1})
    assert DRAFT_ID not in [h.doc.id for h in search(load_store(sessions).docs, DRAFT_ID).hits]

    published = DRAFT_V1.replace("status: draft", "status: active")
    _write_store(tmp_path, {DRAFT_ID: published})
    hits = search(load_store(sessions).docs, DRAFT_ID).hits

    born_active = tmp_path / "born-active"
    reference = search(load_store(_write_store(born_active, {DRAFT_ID: published})).docs, DRAFT_ID)
    assert hits[0].doc.id == DRAFT_ID
    assert [(h.doc.id, h.score) for h in hits] == [
        (h.doc.id, h.score) for h in reference.hits
    ]


def test_a_published_draft_takes_part_in_the_ranking_of_others(tmp_path):
    """Isolation ends at publication: an active document shares corpus statistics again."""
    sessions = _write_store(tmp_path, {})
    without = _published_signature(sessions, "idempotency")

    _write_store(tmp_path, {DRAFT_ID: DRAFT_V1.replace("status: draft", "status: active")})

    assert _published_signature(sessions, "idempotency") != without


AUDIT_ACTIVE = _doc(
    "2001-charge-retry", "active", "Charge retry",
    "## Decision Log\n\nEvery charge carries an idempotency key.\n\n"
    "## Landmines\n\nThe gateway times out under load.\n",
)

AUDIT_DRAFT = _doc(
    "2002-wip-charges", "draft", "Charges in progress",
    "## Decision Log\n\nidempotency again.\n\n"
    "## Landmines\n\nidempotency once more.\n\n"
    "## Cold-start primer\n\nidempotency everywhere.\n",
)


@pytest.mark.parametrize("run", CHANNELS)
def test_ac_01_4_audit_fixture_draft_does_not_hide_the_active_record(tmp_path, run, capsys):
    """The audit's case: 1 of 2 sections is under the DF ceiling; counting the draft's 3 put
    the term at 4 of 5 and dropped it from body scoring, so the active record vanished."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "2001-charge-retry.md", AUDIT_ACTIVE)
    assert "2001-charge-retry" in run(tmp_path, "idempotency", capsys)

    write_file(sessions, "2002-wip-charges.md", AUDIT_DRAFT)
    out = run(tmp_path, "idempotency", capsys)

    assert "### 2001-charge-retry" in out
    assert "2002-wip-charges" not in out
