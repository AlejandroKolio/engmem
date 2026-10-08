"""Shared ground for CLI entry points: store location and failure reporting."""

from __future__ import annotations

import enum
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from engmem.staging import commit, discard, read_document, stage


class StoreSource(enum.StrEnum):
    FLAG = "--store"
    ENV = "ENGMEM_HOME"
    SAVED = "saved choice"
    DEFAULT = "default"


@dataclass(frozen=True)
class StoreChoice:
    path: Path
    source: StoreSource


class StoreSettingError(Exception):
    """The saved store choice exists but cannot be used; the message names the file and why."""


def default_store() -> Path:
    return _absolute(_expand_home(Path("~")) / "Developer" / "engmem")


def _configured(value: str | None) -> str | None:
    """A blank setting means "not set"; the value is never trimmed, so a directory whose name
    really does end in a space still resolves to itself."""
    return value if value and value.strip() else None


def _expand_home(path: Path) -> Path:
    # Path.expanduser() raises RuntimeError instead of falling back when a leading `~` or
    # `~user` cannot be resolved (HOME unset with no passwd entry for the uid; a `~user` that
    # doesn't exist) — left untouched here so it becomes an ordinary relative path the
    # "store not found" diagnostic can still name, instead of an uncaught traceback that
    # leaves stdout empty. See contracts/runtime.md.
    try:
        return path.expanduser()
    except RuntimeError:
        return path


def _absolute(path: Path) -> Path:
    # Path.absolute() gained the branch below only in Python 3.13 (bpo-89812); before that, a
    # Windows drive-relative path such as "C:notes" comes back unchanged — still relative —
    # whenever the process's cwd sits on a different drive, because the pre-3.13 join reparses
    # `[cwd] + parts` from scratch and lets the bare drive-letter part re-anchor the result.
    # Hand-ported so every supported interpreter behaves the same way. See contracts/runtime.md.
    if path.is_absolute():
        return path
    cwd = os.path.abspath(path.drive) if path.drive else os.getcwd()
    return path.__class__(cwd) / path


def store_setting_file() -> Path:
    """Where `engmem store set` saves the choice — see contracts/runtime.md for the location."""
    xdg = _configured(os.environ.get("XDG_CONFIG_HOME"))
    if xdg is not None and Path(xdg).is_absolute():
        return Path(xdg) / "engmem" / "store"
    if sys.platform == "win32":
        appdata = _configured(os.environ.get("APPDATA"))
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "engmem" / "store"
    return Path.home() / ".config" / "engmem" / "store"


def saved_store() -> Path | None:
    """The saved choice, None when there is none; a file that exists but cannot be used raises
    `StoreSettingError` rather than reading as "no choice" (AC-05.3)."""
    setting = store_setting_file()
    try:
        text, _bom = read_document(setting)
    except FileNotFoundError as exc:
        if setting.is_symlink():
            raise StoreSettingError(
                f"{setting} is a symlink to a missing file — restore its target, or run "
                f"`engmem store set PATH` to save the store again"
            ) from exc
        return None
    except UnicodeDecodeError as exc:
        raise StoreSettingError(
            f"{setting} is not valid UTF-8 ({exc.reason} at byte {exc.start}) — run "
            f"`engmem store set PATH` to rewrite it"
        ) from exc
    except OSError as exc:
        raise StoreSettingError(f"cannot read {setting}: {exc.strerror or exc}") from exc
    return _parse_setting(setting, text)


def _parse_setting(setting: Path, text: str) -> Path:
    # one line, its own line ending optional; the path itself is never trimmed (see the blank
    # rule above), so only a line break is taken off the end
    value = text.removesuffix("\n").removesuffix("\r")
    fix = "run `engmem store set PATH` to rewrite it, or delete it to use the default"
    if "\n" in value or "\r" in value:
        raise StoreSettingError(f"{setting} must hold exactly one line, the store path — {fix}")
    if not value.strip():
        raise StoreSettingError(f"{setting} is empty — {fix}")
    if "\0" in value:
        raise StoreSettingError(f"{setting} contains a NUL byte — {fix}")
    path = _expand_home(Path(value))
    if not path.is_absolute():
        raise StoreSettingError(
            f"{setting} holds {value!r}, which is not an absolute path — every command would "
            f"read it against its own working directory; {fix}"
        )
    return path


def save_store(store: Path) -> Path:
    """Writes `store` as the saved choice, atomically, and returns the file written; OSError and
    UnicodeEncodeError reach the caller."""
    setting = store_setting_file()
    target = Path(os.path.realpath(setting)) if setting.is_symlink() else setting
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = stage(target, f"{store}\n".encode("utf-8"))
    try:
        commit(tmp_path, target)
    except BaseException:
        discard(tmp_path)
        raise
    return setting


def store_path_of(value: str | None) -> Path | None:
    """What a `--store`/`ENGMEM_HOME` value names: None when blank, `~` expanded, not yet made
    absolute — the one rule `engmem mcp` and `engmem store show` must both apply."""
    configured = _configured(value)
    return None if configured is None else _expand_home(Path(configured))


def locate_store(explicit: str | None) -> StoreChoice:
    """The store and where it came from: `--store`, `ENGMEM_HOME`, the saved choice, the default.
    The saved choice is read only when nothing overrides it."""
    # expanded because pathlib never expands a literal `~` out of argv or the environment,
    # absolute because the answer outlives this process — see contracts/runtime.md
    for value, source in (
        (explicit, StoreSource.FLAG),
        (os.environ.get("ENGMEM_HOME"), StoreSource.ENV),
    ):
        path = store_path_of(value)
        if path is not None:
            return StoreChoice(_absolute(path), source)
    saved = saved_store()
    if saved is not None:
        return StoreChoice(saved, StoreSource.SAVED)
    return StoreChoice(default_store(), StoreSource.DEFAULT)


def source_description(choice: StoreChoice) -> str:
    """Where `choice` came from, naming the setting file for the two answers it decides."""
    setting = store_setting_file()
    if choice.source is StoreSource.SAVED:
        return f"saved choice ({setting})"
    if choice.source is StoreSource.DEFAULT:
        return f"default (no saved choice in {setting})"
    return str(choice.source)


def resolve_store(explicit: str | None) -> Path:
    return locate_store(explicit).path


def force_utf8_streams() -> None:
    """Locators carry `§`, which cp437 and cp866 cannot encode; the tools print them too."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def fail(message: str) -> None:
    # Both streams: the consuming agent reads stdout and never stderr (ENGMEM-SPEC.md §5,
    # §10 principle VIII), so a failure named on stderr alone is a swallowed error.
    print(f"error: {message}", file=sys.stderr)
    print(f"error: {message}")
