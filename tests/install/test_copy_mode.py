"""A copy under the user's home is rendered for the saved mode and says so in its stamp; a project
copy keeps both modes; doctor and `mode set` name the reinstall a stale copy needs
(contracts/install.md, "A home copy carries the saved mode")."""

from __future__ import annotations

from pathlib import Path

import pytest

from engmem import __version__
from engmem.cli import main
from engmem.settings import mode_setting_file
from engmem.template_stamps import WORKFLOW, read_stamp, stamp_line

HOME_COPIES = {
    "claude": Path(".claude") / "commands" / "engmem.md",
    "copilot-cli": Path(".copilot") / "skills" / "engmem" / "SKILL.md",
    "codex": Path(".agents") / "skills" / "engmem" / "SKILL.md",
}


def _install(*args: str) -> int:
    return main(["install", *args])


def _doctor(capsys, agent: str) -> list[str]:
    capsys.readouterr()
    main(["doctor", "--agent", agent])
    return capsys.readouterr().out.splitlines()


@pytest.mark.parametrize(
    "stamp, mode, digest",
    [
        ("<!-- engmem-template: engmem.start v0.1.11 workflow 1 sha256:0123456789abcdef -->",
         None, "0123456789abcdef"),
        ("<!-- engmem-template: engmem.start v0.1.12 workflow 1 mode daily sha256:0123456789abcdef -->",
         "daily", "0123456789abcdef"),
        ("<!-- engmem-template: engmem.start v0.1.4 -->", None, None),
    ],
    ids=["before-modes", "with-mode", "no-workflow"],
)
def test_a_stamp_parses_with_or_without_its_mode(stamp, mode, digest):
    parsed, body = read_stamp(f"---\nname: x\n---\n{stamp}\nbody\n")

    assert (parsed.mode, parsed.digest) == (mode, digest)
    assert body == "---\nname: x\n---\nbody\n"


def test_a_stamp_line_names_the_mode_only_for_a_copy_rendered_for_one():
    assert " mode " not in stamp_line("engmem.start.md", "1", "body")
    assert stamp_line("engmem.start.md", "1", "body", "research").startswith(
        f"<!-- engmem-template: engmem.start v1 workflow {WORKFLOW} mode research sha256:"
    )


@pytest.mark.parametrize("mode", ["daily", "research"])
@pytest.mark.parametrize("agent", sorted(HOME_COPIES))
def test_a_home_copy_is_rendered_and_stamped_for_the_saved_mode(home, tmp_path, agent, mode):
    assert main(["mode", "set", mode]) == 0

    assert _install("--agent", agent, "--store", str(tmp_path / "store")) == 0

    text = (home / HOME_COPIES[agent]).read_text(encoding="utf-8")
    stamp, _ = read_stamp(text)
    assert (stamp.version, stamp.mode) == (__version__, mode)
    assert ("## Daily mode" in text) is (mode == "daily")
    assert ("\n## 1. Pre-reg" in text) is (mode == "research")


@pytest.mark.parametrize(
    "args, path",
    [
        (("--agent", "claude", "--local"), Path(".claude") / "commands" / "engmem.md"),
        (("--agent", "copilot-ide"), Path(".github") / "prompts" / "engmem.prompt.md"),
    ],
    ids=["claude-local", "copilot-ide"],
)
def test_a_project_copy_keeps_both_modes_whatever_the_saved_mode(home, tmp_path, args, path):
    assert main(["mode", "set", "research"]) == 0

    assert _install(*args, "--store", str(tmp_path / "store")) == 0

    text = (Path.cwd() / path).read_text(encoding="utf-8")
    assert read_stamp(text)[0].mode is None
    assert "## Daily mode" in text and "\n## 1. Pre-reg" in text


def test_a_home_copy_installed_while_the_mode_cannot_be_read_keeps_both_modes(home, tmp_path):
    setting = mode_setting_file()
    setting.parent.mkdir(parents=True, exist_ok=True)
    setting.write_text("experiment\n", encoding="utf-8")

    assert _install("--agent", "claude", "--store", str(tmp_path / "store")) == 0

    text = (home / HOME_COPIES["claude"]).read_text(encoding="utf-8")
    assert read_stamp(text)[0].mode is None
    assert "## Daily mode" in text and "\n## 1. Pre-reg" in text


def test_doctor_names_the_reinstall_for_a_copy_of_the_other_mode(
    home, tmp_path, shell_path, capsys
):
    store = tmp_path / "store"
    assert _install("--agent", "claude", "--store", str(store)) == 0
    assert main(["store", "set", str(store)]) == 0
    assert not [line for line in _doctor(capsys, "claude") if "installed for daily mode" in line]

    assert main(["mode", "set", "research"]) == 0
    stale = [line for line in _doctor(capsys, "claude") if "installed for daily mode" in line]

    assert len(stale) == 3
    assert all(line.startswith("warning: template:") for line in stale)
    assert all("the saved mode is research" in line for line in stale)
    assert all("run `engmem install --agent claude`" in line for line in stale)


def test_mode_set_prints_the_reinstall_for_each_agent_installed_for_the_other_mode(
    home, tmp_path, capsys
):
    for agent in ("claude", "codex"):
        assert _install("--agent", agent, "--store", str(tmp_path / "store")) == 0
    capsys.readouterr()

    assert main(["mode", "set", "daily"]) == 0
    assert "engmem install --agent" not in capsys.readouterr().out

    assert main(["mode", "set", "research"]) == 0
    out = capsys.readouterr().out
    assert "run `engmem install --agent claude` to install them for research" in out
    assert "run `engmem install --agent codex` to install them for research" in out
    assert "copilot-cli" not in out
