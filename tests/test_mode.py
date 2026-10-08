"""US-08: daily or research mode — saved once, recorded per session, and kept apart in the
Gate 1 report so daily sessions never enter the experiment's count."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ACTIVE_CONTENT, DRAFT_CONTENT, FIXTURES, run_tool, write_file
from mcp_harness import _call, _prompts_get_msg, _run, _telemetry_lines, _text

from engmem import gate1, gate1_audit, mcp_server, runtime, settings
from engmem.cli import main
from engmem.settings import Mode, ModeSettingError
from engmem.spine import load_store

ROOT = Path(__file__).resolve().parent.parent
REPORT_TOOL = ROOT / "tools" / "gate1_report.py"
VERIFY_TOOL = ROOT / "tools" / "verify_citations.py"
DRAFT_ID = "20260101-widget-cache"


@pytest.fixture
def mode_file(monkeypatch, tmp_path) -> Path:
    config = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    return config / "engmem" / "mode"


def _save_mode(mode_file: Path, content: bytes) -> None:
    mode_file.parent.mkdir(parents=True, exist_ok=True)
    mode_file.write_bytes(content)


def _store(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    return store


def _draft(mode: str | None, *, prereg: bool = True, extra: str = "") -> str:
    front, _, body = DRAFT_CONTENT.removeprefix("---\n").partition("---\n")
    front = front.replace("mode: daily\n", "")
    if mode is not None:
        front += f"mode: {mode}\n"
    front += extra
    return f"---\n{front}---\n" + (body if prereg else "")


def _active(mode: str | None, *, prereg: bool = True, extra: str = "") -> str:
    front, _, body = ACTIVE_CONTENT.removeprefix("---\n").partition("---\n")
    front = front.replace("mode: daily\n", "")
    if mode is not None:
        front += f"mode: {mode}\n"
    front += extra
    if not prereg:
        head, _, rest = body.partition("## Decision Log")
        body = "\n## Decision Log" + rest
    return f"---\n{front}---\n" + body


def _create(store: Path, content: str) -> tuple[str, bool]:
    result, is_error = _call(
        store, name=mcp_server.CREATE_DRAFT_TOOL_NAME,
        arguments={"id": DRAFT_ID, "content": content},
    )
    return _text(result), is_error


def _complete(store: Path, content: str) -> tuple[str, bool]:
    result, is_error = _call(
        store, name=mcp_server.COMPLETE_DRAFT_TOOL_NAME,
        arguments={"id": DRAFT_ID, "content": content},
    )
    return _text(result), is_error


# --- the setting: where it lives, what it holds, what a broken one does ----------------------


def test_the_mode_file_sits_next_to_the_store_file(mode_file):
    assert settings.mode_setting_file() == mode_file
    assert settings.mode_setting_file().parent == runtime.store_setting_file().parent


@pytest.mark.parametrize("appdata", [True, False], ids=["appdata", "no-appdata"])
def test_on_windows_the_mode_file_lives_under_appdata(monkeypatch, tmp_path, appdata):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("engmem.settings.sys.platform", "win32")
    monkeypatch.setattr("engmem.settings.Path.home", lambda: tmp_path / "home")
    if appdata:
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    else:
        monkeypatch.delenv("APPDATA", raising=False)

    expected = (tmp_path / "roaming") if appdata else tmp_path / "home" / "AppData" / "Roaming"
    assert settings.mode_setting_file() == expected / "engmem" / "mode"


def test_without_a_mode_file_the_mode_is_daily(mode_file, capsys):
    assert settings.effective_mode() is Mode.DAILY
    assert main(["mode", "show"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "mode: daily"
    assert lines[1].startswith("source: default") and str(mode_file) in lines[1]
    assert not mode_file.exists()


@pytest.mark.parametrize("mode", ["daily", "research"])
def test_mode_set_persists_the_choice_for_the_next_command(mode_file, capsys, mode):
    assert main(["mode", "set", mode]) == 0
    capsys.readouterr()

    assert mode_file.read_bytes() == f"{mode}\n".encode()
    assert main(["mode", "show"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"mode: {mode}"
    assert lines[1] == f"source: saved choice ({mode_file})"


@pytest.mark.parametrize(
    "content", [b"research", b"research\r\n", b"\xef\xbb\xbfresearch\n", b"  Research \n"],
    ids=["bare", "crlf", "bom", "case-and-spaces"],
)
def test_a_hand_written_mode_file_is_read(mode_file, content):
    _save_mode(mode_file, content)

    assert settings.effective_mode() is Mode.RESEARCH


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", "empty"),
        (b"   \n", "empty"),
        (b"daily\nresearch\n", "exactly one line"),
        (b"experiment\n", "'experiment'"),
        (b"\xff\xfe", "not valid UTF-8"),
    ],
    ids=["empty", "blank", "two-lines", "unknown-word", "not-utf8"],
)
def test_a_garbled_mode_file_is_a_named_error_never_daily(mode_file, capsys, content, reason):
    _save_mode(mode_file, content)

    with pytest.raises(ModeSettingError, match=reason):
        settings.effective_mode()
    assert main(["mode", "show"]) == 2
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("error: engmem mode")
    assert str(mode_file) in out and reason in out
    assert "mode: daily" not in out


def test_a_directory_at_the_mode_path_is_a_named_error(mode_file, capsys):
    mode_file.mkdir(parents=True)

    assert main(["mode", "show"]) == 2
    assert str(mode_file) in capsys.readouterr().out


def test_mode_set_refuses_an_unknown_mode_and_leaves_the_file(mode_file, capsys):
    _save_mode(mode_file, b"research\n")

    assert main(["mode", "set", "experiment"]) == 2

    out = capsys.readouterr().out
    assert "error:" in out and "daily, research" in out
    assert mode_file.read_bytes() == b"research\n"


def test_mode_set_repairs_a_garbled_file(mode_file, capsys):
    _save_mode(mode_file, b"\xff\xfe")

    assert main(["mode", "set", "daily"]) == 0
    assert settings.effective_mode() is Mode.DAILY


def test_a_garbled_mode_file_does_not_stop_a_search(mode_file, tmp_path, capsys):
    """Search decides nothing by mode, so a broken mode file is not its failure."""
    _save_mode(mode_file, b"experiment\n")
    store = _store(tmp_path)
    write_file(store / "sessions", f"{DRAFT_ID}.md", ACTIVE_CONTENT)

    assert main(["search", "WidgetCache", "--store", str(store)]) == 0
    assert "error:" not in capsys.readouterr().out


# --- the templates: every install target reads the mode before the Pre-reg -----------------


def _installed_start(agent: str, home: Path, project: Path, store: Path) -> str:
    assert main(["install", "--agent", agent, "--store", str(store)]) == 0
    paths = {
        "claude": home / ".claude" / "commands" / "engmem.md",
        "copilot-ide": project / ".github" / "prompts" / "engmem.prompt.md",
        "copilot-cli": home / ".copilot" / "skills" / "engmem" / "SKILL.md",
        "codex": home / ".agents" / "skills" / "engmem" / "SKILL.md",
    }
    return paths[agent].read_text(encoding="utf-8")


@pytest.mark.parametrize("agent", ["claude", "copilot-ide", "copilot-cli", "codex", "mcp"])
def test_every_agent_reads_the_mode_before_any_baseline(agent, env, tmp_path):
    home, project = env
    (project / ".git").mkdir()
    store = tmp_path / "store"
    if agent == "mcp":
        (store / "sessions").mkdir(parents=True)
        _, responses, _ = _run(store, _prompts_get_msg(5, name="engmem"))
        text = responses[0]["result"]["messages"][0]["content"]["text"]
    else:
        text = _installed_start(agent, home, project, store)

    mode_step = text.index("## 0. Mode")
    assert mode_step < text.index("## 1. Pre-reg") < text.index("## 3. Search")
    assert "engmem mode show" in text
    assert "engmem/mode" in text
    assert "mode: daily" in text and "mode: research" in text
    assert "no baseline sub-agent" in text
    assert "baseline_unavailable:" in text


@pytest.mark.parametrize("name", ["engmem.save.md", "engmem.save.quick.md"])
def test_the_save_templates_carry_the_mode_over_unchanged(name):
    text = (ROOT / "src" / "engmem" / "templates" / name).read_text(encoding="utf-8")

    assert "`mode`" in text and "`baseline_unavailable`" in text
    assert "a daily draft has none" in text


@pytest.mark.parametrize("mode", ["daily", "research"])
def test_the_mcp_start_prompt_names_the_mode_saved_now(mode_file, tmp_path, mode):
    """Read on every prompts/get, so a long-running server applies a changed mode to the next
    session (AC-08.4)."""
    store = _store(tmp_path)
    _save_mode(mode_file, b"research\n" if mode == "daily" else b"daily\n")
    mcp_server._handle_prompts_get({"name": "engmem"}, store)
    _save_mode(mode_file, f"{mode}\n".encode())

    result = mcp_server._handle_prompts_get({"name": "engmem"}, store)

    first = result["messages"][0]["content"]["text"].splitlines()[0]
    assert first.startswith(f"engmem mode for this session: {mode}")


def test_the_mcp_start_prompt_names_an_unreadable_mode_and_falls_back_to_daily(mode_file, tmp_path):
    _save_mode(mode_file, b"experiment\n")

    result = mcp_server._handle_prompts_get({"name": "engmem"}, _store(tmp_path))

    first = result["messages"][0]["content"]["text"].splitlines()[0]
    assert "cannot be read" in first and str(mode_file) in first and "daily" in first


def test_only_the_start_prompt_carries_the_mode_line(tmp_path):
    result = mcp_server._handle_prompts_get({"name": "engmem-save"}, _store(tmp_path))

    assert not result["messages"][0]["content"]["text"].startswith("engmem mode")


# --- AC-08.1: daily mode needs no Pre-reg and no baseline ------------------------------------


def test_ac_08_1_a_daily_draft_without_prereg_is_created_and_searched(mode_file, tmp_path):
    store = _store(tmp_path)

    text, is_error = _create(store, _draft("daily", prereg=False))
    assert not is_error, text
    assert "mode: daily" in text and "no Pre-reg" in text

    _, responses, _ = _run(store, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "engmem_search", "arguments": {"query": "widget", "session_id": DRAFT_ID}},
    })
    assert not responses[0]["result"].get("isError")
    assert [row["session_id"] for row in _telemetry_lines(store)] == [DRAFT_ID]


def test_ac_08_1_a_daily_draft_completes_as_a_short_record_without_prereg(mode_file, tmp_path):
    store = _store(tmp_path)
    _create(store, _draft("daily", prereg=False))

    text, is_error = _complete(store, _active("daily", prereg=False))

    assert not is_error, text
    assert load_store(store / "sessions").docs[0].mode == "daily"


def test_ac_08_1_the_start_template_skips_the_baseline_in_daily_mode():
    text = (ROOT / "src" / "engmem" / "templates" / "engmem.start.md").read_text(encoding="utf-8")
    mode_step = text[text.index("## 0. Mode"):text.index("## 1. Pre-reg")]

    assert "**daily**" in mode_step and "skip step 1" in mode_step
    assert "no Pre-reg, no baseline sub-agent" in mode_step


# --- AC-08.2: research mode records the baseline before the first search --------------------


def test_ac_08_2_a_research_baseline_is_on_disk_before_the_attributed_search(mode_file, tmp_path):
    _save_mode(mode_file, b"research\n")
    store = _store(tmp_path)

    text, is_error = _create(store, _draft("research"))
    assert not is_error, text
    assert "mode: research" in text

    on_disk = (store / "sessions" / f"{DRAFT_ID}.md").read_text(encoding="utf-8")
    assert "## Pre-reg" in on_disk and "Naive baseline" in on_disk
    _run(store, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "engmem_search", "arguments": {"query": "widget", "session_id": DRAFT_ID}},
    })
    assert [row["session_id"] for row in _telemetry_lines(store)] == [DRAFT_ID]


def test_ac_08_2_a_research_draft_with_neither_baseline_nor_reason_is_refused(mode_file, tmp_path):
    _save_mode(mode_file, b"research\n")
    store = _store(tmp_path)

    text, is_error = _create(store, _draft("research", prereg=False))

    assert is_error
    assert "## Pre-reg" in text and "baseline_unavailable" in text
    assert not (store / "sessions" / f"{DRAFT_ID}.md").exists()


# --- the recorded mode must be the configured one --------------------------------------------


@pytest.mark.parametrize(
    ("configured", "content", "expected"),
    [
        (None, None, "no `mode`"),
        (b"research\n", None, "no `mode`"),
        (None, "research", "configured mode is daily"),
        (b"research\n", "daily", "configured mode is research"),
        (None, "experiment", "'experiment'"),
    ],
    ids=["missing-daily", "missing-research", "research-under-daily", "daily-under-research",
         "unknown"],
)
def test_create_draft_refuses_a_mode_other_than_the_configured_one(
    mode_file, tmp_path, configured, content, expected
):
    if configured is not None:
        _save_mode(mode_file, configured)
    store = _store(tmp_path)

    text, is_error = _create(store, _draft(content))

    assert is_error and expected in text
    assert "engmem mode" in text
    assert not (store / "sessions" / f"{DRAFT_ID}.md").exists()


def test_an_unreadable_mode_file_still_admits_a_daily_draft_and_refuses_research(
    mode_file, tmp_path
):
    """A daily record is never a wrong observation, so the broken file blocks only research."""
    _save_mode(mode_file, b"experiment\n")
    store = _store(tmp_path)

    text, is_error = _create(store, _draft("research"))
    assert is_error and "cannot be read" in text and str(mode_file) in text
    assert "configured mode is daily" not in text, "the cause is the broken file, not a choice"

    text, is_error = _create(store, _draft("daily", prereg=False))
    assert not is_error, text


# --- AC-08.4: a changed mode applies to the next session and rewrites nothing ---------------


def test_ac_08_4_switching_mode_leaves_existing_documents_untouched(mode_file, tmp_path, capsys):
    _save_mode(mode_file, b"research\n")
    store = _store(tmp_path)
    _create(store, _draft("research"))
    draft = store / "sessions" / f"{DRAFT_ID}.md"
    before = draft.read_bytes()

    assert main(["mode", "set", "daily"]) == 0

    assert draft.read_bytes() == before
    assert load_store(store / "sessions").docs[0].mode == "research"


def test_ac_08_4_the_next_draft_after_a_switch_takes_the_new_mode(mode_file, tmp_path):
    store = _store(tmp_path)
    _save_mode(mode_file, b"research\n")
    assert main(["mode", "set", "daily"]) == 0

    text, is_error = _create(store, _draft("research"))
    assert is_error and "configured mode is daily" in text
    text, is_error = _create(store, _draft("daily", prereg=False))
    assert not is_error, text


def test_ac_08_4_a_research_draft_completes_as_research_after_a_switch(mode_file, tmp_path):
    _save_mode(mode_file, b"research\n")
    store = _store(tmp_path)
    _create(store, _draft("research"))
    _save_mode(mode_file, b"daily\n")

    text, is_error = _complete(store, _active("research"))

    assert not is_error, text
    assert load_store(store / "sessions").docs[0].mode == "research"


@pytest.mark.parametrize(
    ("draft_mode", "content_mode"),
    [("research", "daily"), ("research", None), (None, "daily")],
    ids=["research-to-daily", "research-dropped", "legacy-gains-a-mode"],
)
def test_ac_08_4_completing_a_draft_never_changes_its_mode(
    mode_file, tmp_path, draft_mode, content_mode
):
    store = _store(tmp_path)
    write_file(store / "sessions", f"{DRAFT_ID}.md", _draft(draft_mode))

    text, is_error = _complete(store, _active(content_mode))

    assert is_error and "mode" in text
    assert load_store(store / "sessions").docs[0].status == "draft"


def test_ac_08_4_a_legacy_draft_completes_without_a_mode(tmp_path):
    store = _store(tmp_path)
    write_file(store / "sessions", f"{DRAFT_ID}.md", _draft(None))

    text, is_error = _complete(store, _active(None))

    assert not is_error, text


# --- AC-08.5: no baseline -> the reason is recorded, the work goes on ------------------------


UNAVAILABLE = "baseline_unavailable: the sub-agent failed and the store was already open\n"


def test_ac_08_5_a_research_draft_records_why_the_baseline_is_missing(mode_file, tmp_path):
    _save_mode(mode_file, b"research\n")
    store = _store(tmp_path)

    text, is_error = _create(store, _draft("research", prereg=False, extra=UNAVAILABLE))
    assert not is_error, text

    doc = load_store(store / "sessions").docs[0]
    assert doc.baseline_unavailable == "the sub-agent failed and the store was already open"
    _, responses, _ = _run(store, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "engmem_search", "arguments": {"query": "widget", "session_id": DRAFT_ID}},
    })
    assert not responses[0]["result"].get("isError")

    text, is_error = _complete(store, _active("research", prereg=False, extra=UNAVAILABLE))
    assert not is_error, text


def test_ac_08_5_completing_cannot_drop_the_missing_baseline_record(mode_file, tmp_path):
    _save_mode(mode_file, b"research\n")
    store = _store(tmp_path)
    _create(store, _draft("research", prereg=False, extra=UNAVAILABLE))

    text, is_error = _complete(store, _active("research"))

    assert is_error and "baseline_unavailable" in text


# --- the document field ---------------------------------------------------------------------


def _doc_text(doc_id: str, front: str, body: str = "## Decision Log\n\nBody.\n") -> str:
    return (
        f"---\nid: {doc_id}\ntitle: {doc_id}\ndate: 2026-08-01\nstatus: active\n"
        f"tags: [platform]\nentities: [WidgetCache]\n{front}---\n\n{body}"
    )


@pytest.mark.parametrize(
    ("front", "mode", "unavailable", "warning"),
    [
        ("", None, None, None),
        ("mode: daily\n", "daily", None, None),
        ("mode: Research\n", "research", None, None),
        ("mode: experiment\n", "experiment", None, "mode is not a recognized value"),
        ("baseline_unavailable: no sub-agent\n", None, "no sub-agent", None),
        ("baseline_unavailable: true\n", None, "(no reason stated)", "baseline_unavailable"),
        ("baseline_unavailable: ''\n", None, "(no reason stated)", "baseline_unavailable"),
        ("mode:\nbaseline_unavailable:\n", None, None, None),
    ],
    ids=["legacy", "daily", "case", "unknown", "reason", "bool", "blank", "keys-without-values"],
)
def test_the_loader_reads_mode_and_baseline_unavailable(tmp_path, front, mode, unavailable, warning):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "20260801-a-doc.md", _doc_text("20260801-a-doc", front))

    result = load_store(sessions)

    assert not result.errors
    doc = result.docs[0]
    assert (doc.mode, doc.baseline_unavailable) == (mode, unavailable)
    messages = " ".join(p.message for p in result.warnings)
    if warning is None:
        assert "mode" not in messages and "baseline_unavailable" not in messages
    else:
        assert warning in messages


# --- AC-08.3 and AC-08.5: the report keeps the conditions apart ------------------------------


CITED_ID = "20260101-cited-doc"
QUOTE = "WidgetCache must flush before the deploy"


def _citing(doc_id: str, front: str) -> str:
    body = (
        "## Pre-reg\n\nNaive baseline.\npre-reg source: sub-agent\n\n"
        "## Reuse Log\n\n| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        f'| {CITED_ID} | `flush` "{QUOTE}" | kept it | reuse |\n\n## Search Trace\n\nmiss\n'
    )
    return _doc_text(doc_id, f"repos: [platform-core]\n{front}", body)


def _mixed_store(tmp_path: Path) -> Path:
    store = _store(tmp_path)
    sessions = store / "sessions"
    write_file(sessions, f"{CITED_ID}.md", (
        f"---\nid: {CITED_ID}\ntitle: cited\ndate: 2026-01-01\nstatus: active\n"
        f"tags: [billing]\nentities: [Ledger]\nrepos: [billing-core]\n---\n\n"
        f"## Decision Log\n\n{QUOTE}.\n"
    ))
    write_file(sessions, "20260801-legacy-story.md", _citing("20260801-legacy-story", ""))
    write_file(sessions, "20260802-research-story.md",
               _citing("20260802-research-story", "mode: research\n"))
    write_file(sessions, "20260803-daily-story.md",
               _citing("20260803-daily-story", "mode: daily\n"))
    write_file(sessions, "20260804-incomplete-story.md", _citing(
        "20260804-incomplete-story",
        "mode: research\nbaseline_unavailable: the store was read first\n",
    ))
    write_file(sessions, "20260805-typo-story.md",
               _citing("20260805-typo-story", "mode: reserch\n"))
    return store


def _reason(verdicts: gate1.Verdicts, citing: str) -> str | None:
    row = next(r for r in verdicts.rows if r.citing.id == citing)
    return gate1.exclusion_reason(row)


def test_ac_08_3_legacy_and_research_rows_count_and_the_others_are_named(tmp_path):
    verdicts = gate1.evaluate(_mixed_store(tmp_path))

    assert _reason(verdicts, "20260801-legacy-story") is None
    assert _reason(verdicts, "20260802-research-story") is None
    assert _reason(verdicts, "20260803-daily-story") == (
        "excluded: outside the experiment (mode: daily)"
    )
    assert _reason(verdicts, "20260804-incomplete-story") == (
        "excluded: incomplete observation (baseline_unavailable)"
    )
    assert _reason(verdicts, "20260805-typo-story") == "excluded: mode 'reserch' not recognized"
    counted = {r.citing.id for r in verdicts.rows if gate1.is_primary_candidate(r)}
    assert counted == {"20260801-legacy-story", "20260802-research-story"}


def test_ac_08_3_a_daily_story_is_outside_the_ritual_denominator(tmp_path):
    store = _mixed_store(tmp_path)

    audit = gate1_audit.evaluate(store, gate1.evaluate(store))

    assert audit.daily_docs == ["20260803-daily-story"]
    assert audit.incomplete_docs == ["20260804-incomplete-story"]
    assert audit.unrecognized_mode_docs == ["20260805-typo-story"]
    assert audit.active_count == 2  # the legacy and research stories; the cited doc has no Pre-reg
    assert "20260803-daily-story" not in audit.no_prereg_docs


def test_ac_08_3_a_daily_story_without_prereg_is_counted_as_daily_only(tmp_path):
    store = _store(tmp_path)
    write_file(store / "sessions", "20260803-daily-story.md",
               _doc_text("20260803-daily-story", "mode: daily\n"))

    audit = gate1_audit.evaluate(store, gate1.evaluate(store))

    assert audit.daily_docs == ["20260803-daily-story"]
    assert audit.no_prereg_docs == []


def test_ac_08_3_the_report_prints_each_condition_on_its_own_line(tmp_path):
    proc = run_tool(REPORT_TOOL, _mixed_store(tmp_path))

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "| 20260803-daily-story |" in out
    assert "excluded: outside the experiment (mode: daily)" in out
    assert "2 valid" in out and "of which 2 distant" in out
    assert (
        "1 outside the experiment (mode: daily), 1 incomplete observation "
        "(baseline_unavailable), 0 research without a baseline, 1 mode not recognized"
    ) in out
    assert (
        "sessions outside the experiment (mode: daily), not in the ritual figures: 1 "
        "-- 20260803-daily-story"
    ) in out
    assert (
        "incomplete observations (baseline_unavailable), kept apart from the valid group and "
        "the baseline comparison: 1 -- 20260804-incomplete-story"
    ) in out
    assert "mode not recognized, excluded until corrected: 1 -- 20260805-typo-story" in out
    assert "ritual: 2 started" in out


def test_verify_citations_still_checks_a_daily_story(tmp_path):
    """Whether a quote is real does not depend on the mode it was written in."""
    store = _mixed_store(tmp_path)
    daily = store / "sessions" / "20260803-daily-story.md"
    daily.write_text(daily.read_text(encoding="utf-8").replace(QUOTE, "an invented quote here"),
                     encoding="utf-8")

    proc = run_tool(VERIFY_TOOL, store)

    assert proc.returncode == 1
    assert "20260803-daily-story.md" in proc.stdout


def test_legacy_fixture_documents_are_counted_exactly_as_before():
    """No document written before US-08 carries `mode`; none of them moves."""
    docs = load_store(FIXTURES).docs
    assert docs and all(d.mode is None and d.baseline_unavailable is None for d in docs)
    assert {gate1.observation_of(d) for d in docs} == {gate1.Observation.LEGACY}


def test_every_observation_is_either_counted_or_named():
    names = {o: gate1.observation_reason(o, "x") for o in gate1.Observation}

    assert names[gate1.Observation.LEGACY] is None
    assert names[gate1.Observation.RESEARCH] is None
    assert all(names[o] for o in gate1.Observation if o not in gate1.COUNTED_OBSERVATIONS)
    assert json.dumps(sorted(gate1.COUNTED_OBSERVATIONS)) == '["legacy", "research"]'


# --- ARCH-001: "no reuse" and section counts are story-level figures, counted modes only -----


def _none_doc(doc_id: str, front: str) -> str:
    body = (
        "## Pre-reg\n\nNaive baseline.\npre-reg source: sub-agent\n\n"
        "## Reuse Log\n\nPrior docs used: none.\n\n## Search Trace\n\nmiss\n"
    )
    return _doc_text(doc_id, front, body)


def _none_store(tmp_path: Path, *extra: tuple[str, str]) -> Path:
    store = _store(tmp_path)
    write_file(store / "sessions", "20260801-legacy-none.md", _none_doc("20260801-legacy-none", ""))
    for doc_id, front in extra:
        write_file(store / "sessions", f"{doc_id}.md", _none_doc(doc_id, front))
    return store


KEPT_APART_NONE = (
    ("20260802-daily-none", "mode: daily\n"),
    ("20260803-incomplete-none", "mode: research\nbaseline_unavailable: store read first\n"),
)


def test_a_daily_or_incomplete_none_report_does_not_raise_the_counted_figure(tmp_path):
    store = _none_store(tmp_path, *KEPT_APART_NONE)

    verdicts = gate1.evaluate(store)
    audit = gate1_audit.evaluate(store, verdicts)

    assert verdicts.none_reports == ["20260801-legacy-none"]
    assert verdicts.none_reports_kept_apart == ["20260802-daily-none", "20260803-incomplete-none"]
    assert (audit.none_report_count, audit.none_report_kept_apart) == (1, 2)
    assert (audit.reuse_log_count, audit.reuse_log_kept_apart) == (1, 2)


def test_a_legacy_none_report_still_raises_the_counted_figure(tmp_path):
    store = _none_store(tmp_path, ("20260804-legacy-none-two", ""))

    verdicts = gate1.evaluate(store)
    audit = gate1_audit.evaluate(store, verdicts)

    assert verdicts.none_reports == ["20260801-legacy-none", "20260804-legacy-none-two"]
    assert verdicts.none_reports_kept_apart == []
    assert (audit.none_report_count, audit.reuse_log_count) == (2, 2)


def test_the_report_names_none_reports_kept_apart_beside_the_counted_figure(tmp_path):
    out = run_tool(REPORT_TOOL, _none_store(tmp_path, *KEPT_APART_NONE)).stdout

    kept = " (2 more kept apart by mode -- daily, incomplete, research without a baseline or mode not recognized -- not counted)"
    assert f"documents reporting no reuse: 1{kept}" in out
    assert f"session documents with a Reuse Log section (any status): 1{kept}" in out
    assert f"honestly reporting 'Prior docs used: none.': 1{kept}" in out


def test_without_kept_apart_documents_the_figures_print_as_before(tmp_path):
    out = run_tool(REPORT_TOOL, _none_store(tmp_path)).stdout

    assert "documents reporting no reuse: 1\n" in out
    assert "kept apart by mode --" not in out


# --- ARCH-002: the shell path is unguarded, so the count guards itself -----------------------


def test_a_literally_copied_mode_placeholder_counts_nowhere(tmp_path):
    text = (ROOT / "src" / "engmem" / "templates" / "engmem.start.md").read_text(encoding="utf-8")
    placeholder = next(line.split("#")[0].strip() for line in text.splitlines()
                       if line.startswith("mode: <"))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "20260801-copied.md", _citing("20260801-copied", f"{placeholder}\n"))

    doc = load_store(sessions).docs[0]

    assert gate1.observation_of(doc) not in gate1.COUNTED_OBSERVATIONS


def test_a_research_story_with_neither_prereg_nor_reason_is_not_counted(tmp_path):
    store = _mixed_store(tmp_path)
    write_file(store / "sessions", "20260806-bare-research.md", _doc_text(
        "20260806-bare-research", "repos: [platform-core]\nmode: research\n",
        "## Reuse Log\n\n| prior-doc | taken | impact | classification |\n|---|---|---|---|\n"
        f'| {CITED_ID} | `flush` "{QUOTE}" | kept it | reuse |\n',
    ))

    verdicts = gate1.evaluate(store)
    audit = gate1_audit.evaluate(store, verdicts)

    assert _reason(verdicts, "20260806-bare-research") == (
        "excluded: research session with no Pre-reg and no baseline_unavailable"
    )
    assert audit.missing_baseline_docs == ["20260806-bare-research"]
    assert "20260806-bare-research" not in audit.no_prereg_docs
    assert _reason(verdicts, "20260802-research-story") is None


def test_a_legacy_story_without_prereg_is_still_counted_as_before(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "20260801-old.md", _doc_text("20260801-old", ""))

    assert gate1.observation_of(load_store(sessions).docs[0]) is gate1.Observation.LEGACY


# --- ARCH-003: a stale install is named as the likely cause ---------------------------------


def test_a_missing_mode_points_at_reinstalling_the_templates(mode_file, tmp_path):
    text, is_error = _create(_store(tmp_path), _draft(None))

    assert is_error and "engmem install" in text and "predate modes" in text


# --- ARCH-004: the store's own wording is unchanged by the shared reader --------------------


def test_a_dangling_store_symlink_keeps_the_us05_wording(monkeypatch, tmp_path):
    config = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    (config / "engmem").mkdir(parents=True)
    try:
        (config / "engmem" / "store").symlink_to(tmp_path / "gone")
        (config / "engmem" / "mode").symlink_to(tmp_path / "gone-too")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is not permitted on this platform")

    with pytest.raises(runtime.StoreSettingError, match="to save the store again"):
        runtime.saved_store()
    with pytest.raises(ModeSettingError, match="to save the mode again"):
        settings.saved_mode()


# --- ARCH-005: an empty daily draft is expected, every other empty document is not ---------


@pytest.mark.parametrize(
    ("front", "warned"),
    [
        ("status: draft\nmode: daily\n", False),
        ("status: draft\nmode: research\n", True),
        ("status: draft\n", True),
        ("status: active\nmode: daily\n", True),
    ],
    ids=["daily-draft", "research-draft", "legacy-draft", "daily-active"],
)
def test_only_a_daily_draft_may_be_empty_without_a_warning(tmp_path, front, warned):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_file(sessions, "20260801-empty.md", (
        f"---\nid: 20260801-empty\ntitle: t\ndate: 2026-08-01\n{front}"
        "tags: []\nentities: [X]\n---\n"
    ))

    messages = [p.message for p in load_store(sessions).warnings]

    assert any("document is empty" in m for m in messages) is warned
