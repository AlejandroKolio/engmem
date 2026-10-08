"""The one-line setting files in the per-user config directory: the store path and the mode.

Kept apart from `runtime`, whose `fail` writes to stdout, because `engmem mcp` reads the mode
and its stdout is the protocol channel. Rules and reasons: contracts/runtime.md."""

from __future__ import annotations

import enum
import os
import sys
from pathlib import Path

from engmem.staging import commit, discard, read_document, stage


class Mode(enum.StrEnum):
    """How a session is run: `daily` skips the Gate 1 Pre-reg, `research` runs it (US-08)."""

    DAILY = "daily"
    RESEARCH = "research"


DEFAULT_MODE = Mode.DAILY


class ModeSettingError(Exception):
    """The saved mode exists but cannot be used; the message names the file and why."""


def configured(value: str | None) -> str | None:
    """A blank setting means "not set"; the value is never trimmed, so a directory whose name
    really does end in a space still resolves to itself."""
    return value if value and value.strip() else None


def setting_file(name: str) -> Path:
    """`engmem/<name>` in the per-user config directory — see contracts/runtime.md."""
    xdg = configured(os.environ.get("XDG_CONFIG_HOME"))
    if xdg is not None and Path(xdg).is_absolute():
        return Path(xdg) / "engmem" / name
    if sys.platform == "win32":
        appdata = configured(os.environ.get("APPDATA"))
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "engmem" / name
    return Path.home() / ".config" / "engmem" / name


def mode_setting_file() -> Path:
    """Where `engmem mode set` saves the mode, next to the store file."""
    return setting_file("mode")


def read_setting(
    setting: Path, error: type[Exception], repair: str, subject: str
) -> str | None:
    """The file's text, None when it does not exist; any other failure raises `error`."""
    try:
        text, _bom = read_document(setting)
    except FileNotFoundError as exc:
        if setting.is_symlink():
            raise error(
                f"{setting} is a symlink to a missing file — restore its target, or run "
                f"`{repair}` to save {subject} again"
            ) from exc
        return None
    except UnicodeDecodeError as exc:
        raise error(
            f"{setting} is not valid UTF-8 ({exc.reason} at byte {exc.start}) — run "
            f"`{repair}` to rewrite it"
        ) from exc
    except OSError as exc:
        raise error(f"cannot read {setting}: {exc.strerror or exc}") from exc
    return text


def write_setting(setting: Path, value: str) -> Path:
    """One line, staged and replaced atomically, through a symlinked file; OSError and
    UnicodeEncodeError reach the caller."""
    target = Path(os.path.realpath(setting)) if setting.is_symlink() else setting
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = stage(target, f"{value}\n".encode("utf-8"))
    try:
        commit(tmp_path, target)
    except BaseException:
        discard(tmp_path)
        raise
    return setting


def saved_mode() -> Mode | None:
    """The saved mode, None when there is none; a file that exists but cannot be used raises
    `ModeSettingError` rather than reading as daily."""
    setting = mode_setting_file()
    text = read_setting(setting, ModeSettingError, "engmem mode set daily|research", "the mode")
    if text is None:
        return None
    fix = "run `engmem mode set daily|research` to rewrite it, or delete it for daily"
    lines = text.splitlines()
    if len(lines) > 1:
        raise ModeSettingError(f"{setting} must hold exactly one line, the mode — {fix}")
    value = text.strip().casefold()
    if not value:
        raise ModeSettingError(f"{setting} is empty — {fix}")
    try:
        return Mode(value)
    except ValueError:
        raise ModeSettingError(
            f"{setting} holds {text.strip()!r}, not one of {', '.join(Mode)} — {fix}"
        ) from None


def effective_mode() -> Mode:
    """The saved mode, else daily; read on every call so a change applies to the next session."""
    return saved_mode() or DEFAULT_MODE


def save_mode(mode: Mode) -> Path:
    """Writes `mode` as the saved choice and returns the file written."""
    return write_setting(mode_setting_file(), mode.value)
