"""Layer 1 on authored instances: the arms stay apart, each follows its declared policy, and the report
shows every arm (contracts/e2e.md). The smoke plan runs by default."""

from __future__ import annotations

import dataclasses
from collections import Counter

import pytest

from agentsim import corpus, instances, phase4, policy
from agentsim.greparms import GUIDANCE, ContentGrepAgent
from agentsim.sandbox import Sandbox
from agentsim.trace import Transcript
from support.platform import on_a_shared_runner

pytestmark = pytest.mark.e2e

SMOKE_BUDGET_SECONDS = 60


@pytest.fixture(scope="module")
def document(tmp_path_factory):
    root = tmp_path_factory.mktemp("phase4")
    with pytest.MonkeyPatch.context() as mp:
        for name in ("HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            mp.setenv(name, str(root / "harness-process-home"))
        mp.delenv("ENGMEM_HOME", raising=False)
        mp.delenv("ENGMEM_STORE", raising=False)
        return phase4.run(root / "out", instances.PROVISIONAL_FILE, smoke=True)


@pytest.fixture(scope="module")
def runs(document):
    return document["sizes"]["4kb"]["runs"]


def _one(runs, task, profile):
    (run,) = [r for r in runs if r["task"] == task and r["profile"] == profile]
    return run


def _calls(run):
    return [s for s in run["steps"] if s["kind"] != "template"]


def test_every_smoke_instance_runs_under_every_arm_but_gn_which_runs_the_handoff_only(runs):
    for task in phase4.SMOKE_INSTANCES:
        assert {r["profile"] for r in runs if r["task"] == task} == set(phase4.SMOKE_PROFILES) - {"grep-notes"}
    assert {r["task"] for r in runs if r["profile"] == "grep-notes"} == {"multi-agent"}


def test_no_grep_arm_or_no_history_ever_reaches_engmem(runs):
    for run in runs:
        if run["condition"] == "engmem":
            continue
        assert not [s for s in run["steps"] if "engmem" in s["action"]], (run["task"], run["profile"])
        assert run["store_written"] is None and run["engmem_observed_bytes"] is None


def test_only_the_guided_arm_reads_engmems_advice(runs):
    for run in runs:
        templates = [s for s in run["steps"] if s["kind"] == "template" and s["action"] == "guidance"]
        assert bool(templates) is (run["profile"] == "grep-guided"), (run["task"], run["profile"])
        if templates:
            assert templates[0]["bytes"] == len(GUIDANCE.encode("utf-8"))


def _reads(run) -> Counter:
    return Counter(s["source"] for s in run["steps"] if s["action"] == "engmem read")


def test_e_reads_each_loaded_document_by_primer_and_decisions_then_the_asked_sections(runs):
    for run in (r for r in runs if r["profile"] == "shell-claude" and not r["outcome"]["skipped"]):
        assert all(2 <= n <= 2 + len(policy.POINTED_SECTIONS) for n in _reads(run).values()), run["task"]
    pointed = _one(runs, "c13-identifier", "shell-claude")
    assert max(_reads(pointed).values()) > 2 and pointed["outcome"]["correct"]


def test_no_history_makes_no_call(runs):
    assert all(not _calls(r) for r in runs if r["profile"] == "no-history")


def test_a_changed_covered_file_is_re_read_by_e_from_its_snapshot_line_and_by_g_plus_from_git_log(runs):
    e = _one(runs, "c11-identifier", "shell-claude")
    assert any(s["action"] == "Read src/meter/FlushPolicy.kt" for s in e["steps"])
    assert e["outcome"]["correct"]
    plus = _one(runs, "c11-identifier", "grep-guided")
    actions = [s["action"] for s in plus["steps"]]
    assert any(a.startswith("git log --oneline") for a in actions) and "Read src/meter/FlushPolicy.kt" in actions
    assert plus["outcome"]["correct"]
    plain = _one(runs, "c11-identifier", "grep-content")
    assert not [s for s in plain["steps"] if s["action"].startswith("git ") or s.get("source", "").startswith("code:")]
    assert plain["outcome"]["correct"] is False and plain["outcome"]["stale_avoided"] is False


def test_gn_finds_the_note_its_earlier_agent_wrote_and_the_other_grep_arms_have_none(runs):
    gn = _one(runs, "multi-agent", "grep-notes")
    assert gn["outcome"]["correct"] and gn["saver"]["profile"] == "grep-notes"
    assert gn["saver"]["save_calls"] == 1 and gn["saver"]["save_tokens"] > 0
    quote = next(t for t in corpus.tasks() if t.saver).quote
    for name in ("grep-content", "grep-guided", "no-history"):
        run = _one(runs, "multi-agent", name)
        assert run["saver"] is None and run["outcome"]["correct"] is False
        assert not any(quote in s.get("source", "") for s in run["steps"])


def test_the_report_shows_every_arm_and_says_the_instances_are_provisional(document):
    text = phase4.render(document)
    assert text.count("PROVISIONAL INSTANCES") == 1
    assert "| class | E | G | G+ | GN | N |" in text
    assert "### E against G+ (the comparison of record)" in text and "### E against G" in text
    assert "### Save cost and break-even" in text
    assert "### Opened the document holding the answer" in text
    assert "### Sensitivity: E graded newest-first" in text
    assert "Daily template reads primer, decisions and the section the primer points to or the task asks about; G+'s guidance names Lessons." in text
    assert "mean over every run" in text
    assert "Layer 1 is scripted" in text
    assert "### Families (Layer 1, descriptive)" in text and "| stale | E − G+ |" in text


def test_every_run_carries_the_turn_accounting(runs):
    for run in runs:
        cost = run["cost"]
        assert cost["turn_tokens"] >= cost["to_answer_tokens"]
        assert cost["turn_tokens_cached"] <= 1.25 * cost["turn_tokens"]
        if run["profile"] == "shell-claude" and not run["outcome"]["skipped"]:
            assert cost["save_turn_tokens_cached"] > 0


@on_a_shared_runner
def test_the_phase4_smoke_plan_stays_within_its_budget(document):
    assert document["seconds"] < SMOKE_BUDGET_SECONDS


def _note(sessions, name: str, body: str) -> None:
    (sessions / f"{name}.md").write_text(f"---\nid: {name}\nstatus: active\n---\n\n{body}\n", encoding="utf-8")


def test_content_grep_judges_relevance_from_the_lines_it_shows(tmp_path):
    box = Sandbox.create(tmp_path / "box")
    _note(box.sessions, "20260101-one-term", "pool only")
    _note(box.sessions, "20260102-two-terms", "pool size is 64")
    task = dataclasses.replace(corpus.tasks()[0], description="pool size", files=())
    transcript = Transcript()
    ContentGrepAgent(box, task, transcript, box.root, guided=False).run()
    reads = [s.action for s in transcript.steps if s.action.startswith("Read ")]
    assert reads == ["Read 20260102-two-terms.md"]
    shown = transcript.steps[0].text
    assert "20260101-one-term.md:6:pool only" in shown


def test_the_guided_arm_opens_the_newest_relevant_note_first(tmp_path):
    box = Sandbox.create(tmp_path / "box")
    _note(box.sessions, "20240101-pool", "pool size is 200\npool size history\npool size again")
    _note(box.sessions, "20260101-pool", "pool size is 64")
    task = dataclasses.replace(corpus.tasks()[0], description="pool size", files=())
    for guided, first in ((True, "Read 20260101-pool.md"), (False, "Read 20240101-pool.md")):
        transcript = Transcript()
        ContentGrepAgent(box, task, transcript, box.root, guided=guided).run()
        reads = [s.action for s in transcript.steps if s.action.startswith("Read ")]
        assert reads[0] == first


def test_content_grep_follows_a_superseded_chain_from_a_relevant_note_past_the_cap(tmp_path):
    box = Sandbox.create(tmp_path / "box")
    for i in range(1, 8):
        status = f"status: superseded\nsuperseded_by: 2025010{i + 1}-link" if i < 7 else "status: active"
        (box.sessions / f"2025010{i}-link.md").write_text(
            f"---\nid: 2025010{i}-link\n{status}\n---\n\n" + ("pool size link" if i == 1 else "renamed") + "\n",
            encoding="utf-8",
        )
    task = dataclasses.replace(corpus.tasks()[0], description="pool size", files=())
    transcript = Transcript()
    ContentGrepAgent(box, task, transcript, box.root, guided=False).run()
    reads = [s.action for s in transcript.steps if s.action.startswith("Read ")]
    assert reads[-1] == "Read 20250107-link.md" and len(reads) == 7
