"""The ordinary capture and save go through engmem's one writer, on the CLI as over MCP (US-24)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
from importlib import resources
from pathlib import Path

import pytest

from support import rpc
from support.docs import write_file

from engmem import drafts
from engmem.cli import main
from engmem.spine import load_store
from engmem.staging import version_of

DOC_ID = "20260110-api-contract"
DRAFT = (
    f"---\nid: {DOC_ID}\ntitle: API contract\ndate: 2026-01-10\ntask_date: 2026-01-10\n"
    "status: draft\nsuperseded_by:\nbackfilled: false\ntags: []\nentities: []\nrelated: []\n"
    "covers_files: []\nrepos: [orders-api]\nverified_at:\ncapture_minutes:\nmode: daily\n---\n"
)
ACTIVE = (
    f"---\nid: {DOC_ID}\ntitle: Orders API keeps reference optional\ndate: 2026-01-10\n"
    "task_date: 2026-01-10\nstatus: active\nsuperseded_by:\nbackfilled: false\ntags: [api]\n"
    "entities: [OrderRequest, ReferenceField]\nrelated: []\ncovers_files: []\n"
    "repos: [orders-api]\nverified_at:\ncapture_minutes:\nmode: daily\n---\n\n"
    "## Decision Log\n\nOrderRequest keeps reference optional: old clients omit it.\n\n"
    "## Reuse Log\n\nPrior docs used: none.\n"
)
TEMPLATES = ("engmem.start.md", "engmem.save.md", "engmem.save.quick.md")


def _cli(capsys, monkeypatch, store: Path, stdin: str | bytes | None, *args: str) -> tuple[int, str]:
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    fake = io.TextIOWrapper(io.BytesIO(data or b""), encoding="utf-8")
    fake.isatty = lambda: data is None  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", fake)
    code = main([*args, "--store", str(store)])
    out, err = capsys.readouterr()
    return code, out + err


def _version_in(output: str) -> str:
    return output.split("version: ", 1)[1].split(")", 1)[0]


def _template(name: str) -> str:
    text = (resources.files("engmem") / "templates" / name).read_text(encoding="utf-8")
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# AC-24.1: the installed workflow creates and completes through engmem
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", TEMPLATES)
def test_no_template_tells_the_agent_to_write_the_document_itself(name):
    text = _template(name)

    assert "If you can write files directly" not in text
    assert "If you can edit files directly: replace" not in text
    assert "replace `sessions/<id>.md` with" not in text
    assert "write `sessions/<id>.md` yourself" in text or "Do not write `sessions/<id>.md`" in text


def test_the_start_template_creates_through_the_cli_or_mcp_and_records_the_repo():
    text = _template("engmem.start.md")

    assert "pipe that same text to `engmem draft create <id>`" in text
    assert "`engmem_create_draft` MCP tool" in text
    assert "repos: [<repository>]" in text and "mode: <daily|research from step 0>" in text
    assert "create no draft and write no file yourself" in text


@pytest.mark.parametrize("name", ["engmem.save.md", "engmem.save.quick.md"])
def test_the_save_templates_complete_with_the_version_create_gave(name):
    text = _template(name)

    assert "engmem draft complete <id> --expected-version <the version create gave>" in text
    assert "`engmem_complete_draft`" in text and "expected_version" in text


def test_a_cli_session_is_created_and_completed_through_the_one_writer(store, capsys, monkeypatch):
    code, created = _cli(capsys, monkeypatch, store, DRAFT, "draft", "create", DOC_ID)
    assert code == 0, created
    version = _version_in(created)
    assert version == version_of(DRAFT.encode("utf-8"))
    assert f"pass --session {DOC_ID} on every engmem search" in created
    assert "--expected-version to engmem draft complete" in created
    draft = load_store(store / "sessions").docs[0]
    assert (draft.id, draft.status, draft.mode, draft.repos) == (
        DOC_ID, "draft", "daily", ["orders-api"]
    )

    code, completed = _cli(
        capsys, monkeypatch, store, ACTIVE, "draft", "complete", DOC_ID,
        "--expected-version", version,
    )

    assert code == 0, completed
    assert "(status: draft -> active)" in completed
    assert (store / "sessions" / f"{DOC_ID}.md").read_bytes() == ACTIVE.encode("utf-8")


# ---------------------------------------------------------------------------
# AC-24.2: two creators of one id
# ---------------------------------------------------------------------------


def test_of_two_creators_of_one_id_exactly_one_wins_and_the_other_overwrites_nothing(store):
    contents = [DRAFT.replace("title: API contract", f"title: creator {n}") for n in range(2)]
    results: list[str | Exception] = [None, None]
    barrier = threading.Barrier(2)

    def create(n: int) -> None:
        barrier.wait()
        try:
            results[n] = drafts.create_draft(store, DOC_ID, contents[n], drafts.CLI)
        except drafts.WriteRefused as exc:
            results[n] = exc

    threads = [threading.Thread(target=create, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    won = [n for n in range(2) if isinstance(results[n], str)]
    lost = [n for n in range(2) if isinstance(results[n], drafts.WriteRefused)]
    assert len(won) == 1 and len(lost) == 1
    assert "never overwrites" in str(results[lost[0]])
    assert (store / "sessions" / f"{DOC_ID}.md").read_text(encoding="utf-8") == contents[won[0]]


def test_creators_of_different_ids_keep_each_others_documents(store):
    ids = [f"20260110-api-contract-{n}" for n in range(8)]

    def create(doc_id: str) -> None:
        drafts.create_draft(store, doc_id, DRAFT.replace(DOC_ID, doc_id), drafts.CLI)

    threads = [threading.Thread(target=create, args=(doc_id,)) for doc_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(d.id for d in load_store(store / "sessions").docs) == ids


def test_the_cli_refuses_a_second_create_and_names_its_own_commands(store, capsys, monkeypatch):
    _cli(capsys, monkeypatch, store, DRAFT, "draft", "create", DOC_ID)

    code, output = _cli(capsys, monkeypatch, store, DRAFT.replace("API", "Other"),
                        "draft", "create", DOC_ID)

    assert code == 2
    assert "engmem draft create never overwrites. Use engmem draft complete" in output
    assert "engmem_create_draft" not in output
    assert "title: API contract" in (store / "sessions" / f"{DOC_ID}.md").read_text("utf-8")


# ---------------------------------------------------------------------------
# AC-24.3: what was confirmed is what is saved
# ---------------------------------------------------------------------------


def test_a_draft_changed_since_create_is_refused_with_its_current_text(store, capsys, monkeypatch):
    _code, created = _cli(capsys, monkeypatch, store, DRAFT, "draft", "create", DOC_ID)
    version = _version_in(created)
    changed = DRAFT.replace("title: API contract", "title: API contract, edited by a colleague")
    write_file(store / "sessions", f"{DOC_ID}.md", changed)

    code, output = _cli(capsys, monkeypatch, store, ACTIVE, "draft", "complete", DOC_ID,
                        "--expected-version", version)

    assert code == 2
    assert "it changed since you read it" in output
    assert f"run engmem draft complete again with --expected-version {version_of(changed.encode())}" in output
    assert "edited by a colleague" in output
    assert load_store(store / "sessions").docs[0].status == "draft"


def test_a_declined_preview_publishes_nothing_in_the_quick_template():
    text = _template("engmem.save.quick.md")
    declined = text[text.index("**Declined**"):text.index("To publish,")]

    assert "publish nothing" in declined and "Do not run `engmem draft complete`" in declined
    assert "it is a different document from the one confirmed" in text


# ---------------------------------------------------------------------------
# AC-24.4: no false "saved", and no silent fallback
# ---------------------------------------------------------------------------


def test_an_interrupted_completion_leaves_the_draft_and_says_nothing_was_saved(
    store, capsys, monkeypatch
):
    _code, created = _cli(capsys, monkeypatch, store, DRAFT, "draft", "create", DOC_ID)

    def failing_commit(_tmp, _target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(drafts, "commit", failing_commit)
    code, output = _cli(capsys, monkeypatch, store, ACTIVE, "draft", "complete", DOC_ID,
                        "--expected-version", _version_in(created))

    assert code == 2 and "No space left on device" in output and "completed" not in output
    assert load_store(store / "sessions").docs[0].status == "draft"
    assert sorted(p.name for p in (store / "sessions").iterdir()) == [f"{DOC_ID}.md"]


@pytest.mark.parametrize(
    "stdin, reason",
    [
        pytest.param(None, "pass the document on stdin", id="terminal"),
        pytest.param(b"---\ntitle: \xff\n---\n", "is not UTF-8", id="not-utf8"),
        pytest.param(b"", "stdin is empty", id="empty"),
        pytest.param(b"  \n\n", "stdin is empty", id="blank"),
    ],
)
def test_no_document_on_stdin_writes_nothing(store, capsys, monkeypatch, stdin, reason):
    code, output = _cli(capsys, monkeypatch, store, stdin, "draft", "create", DOC_ID)

    assert code == 2 and reason in output and "nothing was written" in output
    assert list((store / "sessions").iterdir()) == []


@pytest.mark.parametrize("name", ["engmem.save.md", "engmem.save.quick.md"])
def test_without_the_managed_path_the_save_templates_write_only_on_an_explicit_ask(name):
    text = _template(name)

    assert "tell the user" in text.lower() and "not saved" in text
    assert "explicitly asks" in text and "outside engmem's checks" in text
    assert "engmem store show" in text, "the hand-write case names where the store is"


def test_the_quick_fallback_keeps_the_confirmed_text_and_names_the_next_step():
    text = _template("engmem.save.quick.md")

    assert "Keep the confirmed document in your reply" in text
    assert "`/engmem.save.quick` can finish once engmem can write" in text


def test_the_cli_reads_stdin_as_a_real_process(store):
    """The commands above stub stdin; this one pipes it, as a shell heredoc does."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    run = subprocess.run(
        [sys.executable, "-m", "engmem.cli", "draft", "create", DOC_ID, "--store", str(store)],
        input=DRAFT.encode("utf-8"), capture_output=True, env=env, check=False,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert (store / "sessions" / f"{DOC_ID}.md").read_bytes() == DRAFT.encode("utf-8")


# ---------------------------------------------------------------------------
# AC-24.5: the next session finds the completed record and reads its version
# ---------------------------------------------------------------------------


def test_the_next_session_finds_the_completed_record_and_reads_that_version(
    store, capsys, monkeypatch
):
    _code, created = _cli(capsys, monkeypatch, store, DRAFT, "draft", "create", DOC_ID)
    _cli(capsys, monkeypatch, store, ACTIVE, "draft", "complete", DOC_ID,
         "--expected-version", _version_in(created))
    version = version_of(ACTIVE.encode("utf-8"))

    _code, found = _cli(capsys, monkeypatch, store, None, "search", "OrderRequest reference")
    _code, read = _cli(capsys, monkeypatch, store, None, "read", f"{DOC_ID}@{version}")

    assert f"### {DOC_ID}" in found and f"{DOC_ID}@{version}" in found
    assert "OrderRequest keeps reference optional" in read


def test_the_mcp_tools_keep_their_own_wording_through_the_shared_writer(store):
    _code, responses, _raw = rpc.run(
        store, rpc.tools_call_msg(1, name="engmem_create_draft",
                                  arguments={"id": DOC_ID, "content": DRAFT}),
    )
    text = rpc.text(responses[0]["result"])

    assert f'pass session_id: "{DOC_ID}" on every engmem_search' in text
    assert "expected_version to engmem_complete_draft" in text


def test_an_interrupted_create_reports_no_draft_and_leaves_none(store, capsys, monkeypatch):
    def interrupted(_tmp, _target):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(drafts, "commit_new", interrupted)
    code, output = _cli(capsys, monkeypatch, store, DRAFT, "draft", "create", DOC_ID)

    assert code == 2 and "Input/output error" in output and "created" not in output
    assert list((store / "sessions").iterdir()) == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_pipes_with_a_quoted_delimiter_and_a_windows_safe_encoding(name):
    """An unquoted heredoc expands `$x` and runs backtick spans, which documents are full of;
    Windows PowerShell 5.1 pipes every non-ASCII character to a program as `?`."""
    text = _template(name)

    assert "<<'EOF'" in text and "quoted delimiter" in text
    assert "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)" in text
    assert "`@'` … `'@ |" in text
    assert "prefer it to the shell when you have both" in text.lower()


def test_the_guide_example_runs_in_a_posix_shell_and_says_how_in_powershell():
    readme = (Path(__file__).resolve().parent.parent / "docs" / "guide.md").read_text(
        encoding="utf-8"
    )

    assert "engmem draft create <id> <<'EOF'" in readme
    assert "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)" in readme


def test_a_refusal_fixed_after_confirmation_is_shown_again_before_it_is_published():
    """AC-24.3: a fix to a refused row or field changes what the user confirmed."""
    text = _template("engmem.save.quick.md")

    assert "Publish only the document the user confirmed" in text
    assert "If the call refuses, whatever the reason" in text
    assert "show the changed document to the user again and publish only after a new confirmation" in text


def test_the_start_template_skips_the_draft_only_on_a_refusal_it_cannot_fix():
    text = _template("engmem.start.md")

    assert "it fails for any reason other than the two above" in text
    assert "[] if unknown" in text


@pytest.mark.parametrize(
    "surface, wording", [(drafts.MCP, "'id' argument"), (drafts.CLI, "DOC_ID")]
)
def test_an_id_mismatch_names_the_argument_in_each_surfaces_terms(store, surface, wording):
    with pytest.raises(drafts.WriteRefused) as raised:
        drafts.create_draft(store, "20260110-other", DRAFT, surface)

    assert f"does not match the {wording} '20260110-other'" in str(raised.value)
