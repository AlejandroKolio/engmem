"""A completion, supersede or backfill rewrites only the version its caller read (US-25)."""

from __future__ import annotations

import io
import re
import sys
import threading
from pathlib import Path

import pytest

from support import rpc
from support.docs import ACTIVE_CONTENT, DRAFT_CONTENT

from engmem import backfill, drafts, mcp_server, staging
from engmem.backfill import BackfillWriteError, apply_backfill, propose_backfill
from engmem.cli import main
from engmem.spine import load_store, parse_document
from engmem.staging import version_of

SUPERSEDE_TOOL = mcp_server.MARK_SUPERSEDED_TOOL_NAME
COMPLETE_TOOL = mcp_server.COMPLETE_DRAFT_TOOL_NAME
POLICY_ID = "20260105-retry-policy"
SUCCESSOR_ID = "20260120-retry-policy-v2"
DRAFT_ID = "20260101-widget-cache"
CURRENT_VERSION_RE = re.compile(r'expected_version: "([0-9a-f]+)"')
POLICY = (
    f"---\nid: {POLICY_ID}\ntitle: Payment retry policy\ndate: 2026-01-05\n"
    "task_date: 2026-01-05\nstatus: active\nsuperseded_by:\nbackfilled: false\n"
    "tags: [payments]\nentities: [PaymentRetrier]\nrelated: []\ncovers_files: []\n"
    "repos: [payments-api]\nverified_at:\ncapture_minutes:\nmode: daily\n---\n\n"
    "## Decision Log\n\nPaymentRetrier retries a declined charge three times with backoff.\n"
)
PROHIBITION = (
    "\nNever retry a charge the card network declined as fraud: a retry is a second "
    "fraud signal.\n"
)
# active, but missing the fields `engmem backfill` derives
LEGACY_POLICY = (
    f"---\nid: {POLICY_ID}\ntitle: Payment retry policy\ndate: 2026-01-05\nstatus: active\n"
    "---\n\n## Search Keywords\n\n- **Classes:** `PaymentRetrier`\n\n"
    "## Decision Log\n\nPaymentRetrier retries a declined charge three times.\n"
)


@pytest.fixture
def store(tmp_path) -> Path:
    (tmp_path / "sessions").mkdir()
    return tmp_path


def _policy(store: Path, content: str = POLICY) -> Path:
    path = store / "sessions" / f"{POLICY_ID}.md"
    path.write_bytes(content.encode("utf-8"))
    return path


def _supersede(store: Path, expected_version: str | None) -> tuple[str, bool]:
    arguments = {"id": POLICY_ID, "superseded_by": SUCCESSOR_ID}
    if expected_version is not None:
        arguments["expected_version"] = expected_version
    return mcp_server._handle_mark_superseded(arguments, store)


def _sessions_files(store: Path) -> list[str]:
    return sorted(p.name for p in (store / "sessions").iterdir())


# ---------------------------------------------------------------------------
# AC-25.1, AC-25.2: supersede names the version read
# ---------------------------------------------------------------------------


def test_superseding_the_version_read_marks_it_superseded(store):
    path = _policy(store)

    text, is_error = _supersede(store, version_of(POLICY.encode("utf-8")))

    assert not is_error, text
    assert parse_document(path).superseded_by == SUCCESSOR_ID


def test_superseding_a_version_a_colleague_has_moved_past_is_refused_with_the_current_one(store):
    """AC-25.1: the successor was built from V1; V2 survives, and the refusal is the re-read."""
    path = _policy(store)
    v1 = version_of(POLICY.encode("utf-8"))
    v2_bytes = (POLICY + PROHIBITION).encode("utf-8")
    path.write_bytes(v2_bytes)

    text, is_error = _supersede(store, v1)

    assert is_error
    assert "changed since you read it" in text and "nothing was written" in text
    assert CURRENT_VERSION_RE.search(text).group(1) == version_of(v2_bytes)
    assert PROHIBITION.strip() in text, "the refusal must carry what the colleague added"
    assert path.read_bytes() == v2_bytes
    assert _sessions_files(store) == [f"{POLICY_ID}.md"], "no staged file or lock left behind"

    retried, is_error = _supersede(store, CURRENT_VERSION_RE.search(text).group(1))
    assert not is_error, retried


@pytest.mark.parametrize("missing", [None, "", "  "], ids=["absent", "empty", "blank"])
def test_a_supersede_without_a_version_is_refused_with_the_current_one(store, missing):
    """AC-25.2 for supersede: no read is not a match, and the refusal is enough to retry."""
    path = _policy(store)

    text, is_error = _supersede(store, missing)

    assert is_error
    assert "no expected_version was given" in text
    assert CURRENT_VERSION_RE.search(text).group(1) == version_of(POLICY.encode("utf-8"))
    assert path.read_bytes() == POLICY.encode("utf-8")


def test_a_non_string_supersede_version_is_invalid_params(store):
    path = _policy(store)

    _, responses, _ = rpc.run(
        store,
        rpc.tools_call_msg(
            1,
            name=SUPERSEDE_TOOL,
            arguments={"id": POLICY_ID, "superseded_by": SUCCESSOR_ID, "expected_version": 7},
        ),
    )

    assert responses[0]["error"]["code"] == mcp_server.INVALID_PARAMS
    assert "expected_version" in responses[0]["error"]["message"]
    assert path.read_bytes() == POLICY.encode("utf-8")


def test_a_draft_named_by_its_version_is_still_not_superseded(store):
    """The status check runs first: naming a draft's own version does not get it past."""
    path = store / "sessions" / f"{DRAFT_ID}.md"
    path.write_bytes(DRAFT_CONTENT.encode("utf-8"))

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments=rpc.versioned(store, {"id": DRAFT_ID, "superseded_by": SUCCESSOR_ID}),
    )

    assert is_error
    assert "not 'active'" in rpc.text(result)
    assert path.read_bytes() == DRAFT_CONTENT.encode("utf-8")


def _cli(capsys, monkeypatch, store: Path, stdin: str, *args: str) -> tuple[int, str]:
    fake = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
    fake.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", fake)
    code = main([*args, "--store", str(store)])
    out, err = capsys.readouterr()
    return code, out + err


@pytest.mark.parametrize(
    "flag", [(), ("--expected-version", "")], ids=["absent", "empty"]
)
def test_a_cli_completion_without_a_version_exits_2_with_the_current_one(
    store, capsys, monkeypatch, flag
):
    """AC-25.2 on the CLI: a refusal naming the version, not argparse's usage line."""
    path = store / "sessions" / f"{DRAFT_ID}.md"
    path.write_bytes(DRAFT_CONTENT.encode("utf-8"))

    code, output = _cli(
        capsys, monkeypatch, store, ACTIVE_CONTENT, "draft", "complete", DRAFT_ID, *flag
    )

    assert code == 2
    assert "no --expected-version was given" in output
    assert f"--expected-version {version_of(DRAFT_CONTENT.encode('utf-8'))}" in output
    assert path.read_bytes() == DRAFT_CONTENT.encode("utf-8")


# ---------------------------------------------------------------------------
# AC-25.3: a backfill and a supersede of one document run one after the other
# ---------------------------------------------------------------------------


def _legacy(store: Path):
    path = _policy(store, LEGACY_POLICY)
    doc = next(d for d in load_store(store / "sessions").docs if d.id == POLICY_ID)
    return path, doc, propose_backfill(doc)


def test_a_backfill_waits_out_a_held_lock_then_refuses_by_name(store, monkeypatch):
    monkeypatch.setattr(staging, "_LOCK_WAIT_SECONDS", 0.05)
    path, doc, proposal = _legacy(store)
    lock = path.with_name(f".{path.name}.write-lock")
    lock.mkdir()

    with pytest.raises(BackfillWriteError, match=re.escape(lock.name)):
        apply_backfill(doc, proposal)

    assert path.read_bytes() == LEGACY_POLICY.encode("utf-8")
    assert lock.is_dir(), "another holder's lock must not be removed"


def _pausing(module, name: str, thread_name: str):
    """`module.name` that stops in thread `thread_name` until released, just before it runs."""
    real = getattr(module, name)
    paused, release = threading.Event(), threading.Event()

    def pause(*args, **kwargs):
        if threading.current_thread().name == thread_name:
            paused.set()
            assert release.wait(10)
        return real(*args, **kwargs)

    return pause, paused, release


def _race(first, second) -> dict[str, object]:
    """Runs `first` until it pauses in its commit, starts `second`, then lets `first` go."""
    results: dict[str, object] = {}

    def run(name: str, call) -> None:
        try:
            results[name] = call()
        except BackfillWriteError as exc:
            results[name] = exc

    first_call, paused, release = first
    a = threading.Thread(target=run, args=("first", first_call), name="first")
    b = threading.Thread(target=run, args=("second", second), name="second")
    a.start()
    assert paused.wait(10)
    b.start()
    # long enough for `second` to reach the lock, well inside its wait
    b.join(0.3)
    release.set()
    a.join()
    b.join()
    return results


def test_a_supersede_committing_first_makes_the_backfill_refuse(store, monkeypatch):
    path, doc, proposal = _legacy(store)
    read = version_of(LEGACY_POLICY.encode("utf-8"))
    pause, paused, release = _pausing(mcp_server, "commit", "first")
    monkeypatch.setattr(mcp_server, "commit", pause)

    results = _race(
        (lambda: _supersede(store, read), paused, release),
        lambda: apply_backfill(doc, proposal),
    )

    text, is_error = results["first"]
    assert not is_error, text
    assert isinstance(results["second"], BackfillWriteError), results["second"]
    assert "changed on disk" in str(results["second"])
    after = parse_document(path)
    assert after.status == "superseded" and after.superseded_by == SUCCESSOR_ID
    assert after.backfilled is not True, "the backfill must not have written over the supersede"
    assert _sessions_files(store) == [f"{POLICY_ID}.md"]


def test_a_backfill_committing_first_makes_the_supersede_refuse(store, monkeypatch):
    path, doc, proposal = _legacy(store)
    read = version_of(LEGACY_POLICY.encode("utf-8"))
    pause, paused, release = _pausing(backfill, "commit", "first")
    monkeypatch.setattr(backfill, "commit", pause)

    results = _race(
        (lambda: apply_backfill(doc, proposal), paused, release),
        lambda: _supersede(store, read),
    )

    assert str(results["first"]).startswith("backfilled"), results["first"]
    text, is_error = results["second"]
    assert is_error
    assert "changed since you read it" in text
    after = parse_document(path)
    assert after.status == "active" and after.backfilled is True
    assert _sessions_files(store) == [f"{POLICY_ID}.md"]


# ---------------------------------------------------------------------------
# AC-25.4: an edit landing after the staging is caught by the last check
# ---------------------------------------------------------------------------


EDIT = "\nA hand edit landed after staging.\n"


def _edit_after_staging(monkeypatch, target: Path) -> None:
    real = drafts.stage_content

    def stage_then_edit(path: Path, content: bytes):
        staged = real(path, content)
        with target.open("ab") as f:
            f.write(EDIT.encode("utf-8"))
        return staged

    monkeypatch.setattr(drafts, "stage_content", stage_then_edit)


def test_a_completion_refuses_an_edit_made_after_it_staged(store, monkeypatch):
    path = store / "sessions" / f"{DRAFT_ID}.md"
    path.write_bytes(DRAFT_CONTENT.encode("utf-8"))
    arguments = rpc.versioned(store, {"id": DRAFT_ID, "content": ACTIVE_CONTENT})
    _edit_after_staging(monkeypatch, path)

    text, is_error = mcp_server._handle_complete_draft(arguments, store)

    assert is_error
    assert "changed on disk" in text
    assert path.read_bytes() == (DRAFT_CONTENT + EDIT).encode("utf-8"), "the edit stays"
    assert _sessions_files(store) == [f"{DRAFT_ID}.md"], "no staged file or lock left behind"


def test_a_supersede_refuses_an_edit_made_after_it_staged(store, monkeypatch):
    path = _policy(store)
    _edit_after_staging(monkeypatch, path)

    text, is_error = _supersede(store, version_of(POLICY.encode("utf-8")))

    assert is_error
    assert "changed on disk" in text
    assert path.read_bytes() == (POLICY + EDIT).encode("utf-8"), "the edit stays"
    assert _sessions_files(store) == [f"{POLICY_ID}.md"], "no staged file or lock left behind"


# ---------------------------------------------------------------------------
# AC-25.5: a colleague's new prohibition stays the active, findable policy
# ---------------------------------------------------------------------------


def test_a_prohibition_added_after_the_read_stays_active_and_found(store):
    _policy(store)
    read_text = rpc.text(rpc.call(store, name=mcp_server.READ_TOOL_NAME, arguments={"id": POLICY_ID})[0])
    cited = re.search(rf"cite as \(Reuse Log prior-doc\): {POLICY_ID}@([0-9a-f]+)", read_text)
    assert cited, read_text
    # the colleague adds the prohibition after the agent read the policy
    path = store / "sessions" / f"{POLICY_ID}.md"
    path.write_bytes((POLICY + PROHIBITION).encode("utf-8"))

    result, is_error = rpc.call(
        store,
        name=SUPERSEDE_TOOL,
        arguments={
            "id": POLICY_ID,
            "superseded_by": SUCCESSOR_ID,
            "expected_version": cited.group(1),
        },
    )

    assert is_error, rpc.text(result)
    assert PROHIBITION.strip() in rpc.text(result)
    assert parse_document(path).status == "active"
    # the next fresh session, searching its own repository
    found, is_error = rpc.call(
        store,
        name=mcp_server.TOOL_NAME,
        arguments={"query": "PaymentRetrier fraud retry", "repo": "payments-api"},
    )
    assert not is_error, rpc.text(found)
    assert POLICY_ID in rpc.text(found)
    read_again = rpc.text(rpc.call(store, name=mcp_server.READ_TOOL_NAME, arguments={"id": POLICY_ID})[0])
    assert "Never retry a charge the card network declined as fraud" in read_again


# ---------------------------------------------------------------------------
# the installed workflow passes the versions
# ---------------------------------------------------------------------------


def _save_template() -> str:
    from importlib import resources

    text = (resources.files("engmem") / "templates" / "engmem.save.md").read_text(encoding="utf-8")
    return " ".join(text.split())


def test_the_save_workflow_supersedes_the_version_it_read():
    text = _save_template()
    step = text.split("## 3. Supersede check", 1)[1].split("## 4.", 1)[0]

    assert step.index("engmem_mark_superseded") < step.index("edit files directly"), (
        "the checked path is offered first"
    )
    assert "prefer it to editing the file when you have both" in step
    built_on = step.index("the version of the earlier document this document was built on")
    assert "`<id>@<version>` row in your Reuse Log" in step
    assert "Do not read the earlier document again to get a newer version" in step
    fallback = step.index("Only if you have no such version, read it now with `engmem_read`")
    assert built_on < fallback, "a fresh read is the fallback, never the first source"
    assert "call again with that version only if this document still replaces it" in step


def test_the_save_workflow_never_drops_the_completion_version():
    text = _save_template()

    assert "Leave the version out" not in text
    # empty, not omitted: a client enforcing the schema's `required` would never send the call
    assert "If you no longer have the version, pass an empty `expected_version`" in text
    assert "call without it" not in text
