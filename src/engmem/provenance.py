"""Each repository's snapshot anchor next to the commit this session's checkout is at (US-11).
Why each rule is shaped as it is: contracts/provenance.md."""

from __future__ import annotations

import enum
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from engmem.executables import on_path
from engmem.scoring import repo_key
from engmem.spine import Doc, is_commit_id, linked_repos

GIT_LOOKUP_SECONDS = 5.0
NO_ANCHOR = "no anchor recorded"
UNREADABLE_FIELD = "verified_at cannot be read (see the load warning)"
_GIT_OUTPUT_BYTES = 64 * 1024
_REASON_MAX_CHARS = 160
# a path in a reason keeps its end, where the checkout's own name is
_PATH_SHOWN_MAX = 60
DUBIOUS_OWNERSHIP = "git refuses this checkout: it is owned by another user"
# variables that point git at a repository other than the one the directory is in: a hook or a
# wrapper exporting GIT_DIR would otherwise answer for that repository, not for this checkout
_REPOSITORY_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


class SnapshotState(enum.StrEnum):
    """US-12 refines DIFFERS with the covered-files check; it never turns one into a verdict on
    the text."""

    MATCH = "match"
    DIFFERS = "differs"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RepoSnapshot:
    repo: str  # as written; "" for a legacy anchor tied to no repository
    anchor: str | None
    state: SnapshotState
    current: str | None = None  # HEAD of this session's checkout, when it is this repository
    reason: str | None = None  # why the state is UNKNOWN
    legacy: bool = False


@dataclass(frozen=True)
class Checkout:
    """The git checkout a directory is in: `name` and `head`, or `reason` when there is none."""

    name: str | None = None
    head: str | None = None
    reason: str | None = None
    path: str | None = None  # the checkout's top-level directory, as git printed it


class SessionCheckout:
    """The checkout this process runs in, looked up on first use and at most once."""

    def __init__(self) -> None:
        self._checkout: Checkout | None = None

    def lookup(self) -> Checkout:
        if self._checkout is None:
            self._checkout = _find_session_checkout()
        return self._checkout


def _find_session_checkout() -> Checkout:
    try:
        directory = Path.cwd()
    except OSError as exc:
        return Checkout(
            reason=f"engmem's working directory cannot be read ({type(exc).__name__})"
        )
    return find_checkout(directory)


def _shown_path(path: str) -> str:
    return path if len(path) <= _PATH_SHOWN_MAX else "…" + path[-(_PATH_SHOWN_MAX - 1):]


def _git_error(stderr: str, returncode: int) -> str:
    """git's own error line. Never the hint lines around it: they are advice to the user, and
    one of them is a `git config` command that turns off git's ownership check (ARCH-001)."""
    for raw in stderr.splitlines():
        line = raw.strip()
        if line.startswith(("fatal:", "error:")):
            return line if len(line) <= _REASON_MAX_CHARS else line[: _REASON_MAX_CHARS - 1] + "…"
    return f"git exited with code {returncode}"


def _run_git(git: str, directory: Path) -> tuple[int, str, str]:
    """`(exit code, stdout, stderr)` of `git rev-parse --show-toplevel HEAD` in `directory`."""
    env = {k: v for k, v in os.environ.items() if k not in _REPOSITORY_ENV}
    # LC_ALL=C: the reasons below are read from git's own messages, which a locale translates
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANGUAGE="C")
    # files, not pipes, for the reason doctor's probes use them (contracts/doctor.md)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        completed = subprocess.run(
            [git, "rev-parse", "--show-toplevel", "HEAD"],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, cwd=directory, env=env,
            timeout=GIT_LOOKUP_SECONDS,
        )
        out.seek(0)
        err.seek(0)
        return (
            completed.returncode,
            out.read(_GIT_OUTPUT_BYTES).decode("utf-8", "replace"),
            err.read(_GIT_OUTPUT_BYTES).decode("utf-8", "replace"),
        )


def find_checkout(directory: Path) -> Checkout:
    """Runs git once; every way it can fail is a reason, never an exception."""
    git = on_path("git")
    if git is None:
        return Checkout(reason="git is not on PATH")
    try:
        returncode, stdout, stderr = _run_git(git, directory)
    except subprocess.TimeoutExpired:
        return Checkout(reason=f"git did not answer within {GIT_LOOKUP_SECONDS:g} s")
    except OSError as exc:
        return Checkout(reason=f"git could not be run ({type(exc).__name__})")
    lines = stdout.splitlines()
    name = Path(lines[0]).name if lines and lines[0].strip() else None
    if returncode != 0:
        if "detected dubious ownership" in stderr:
            return Checkout(reason=DUBIOUS_OWNERSHIP)
        if "not a git repository" in stderr:
            return Checkout(
                reason=f"engmem's working directory {_shown_path(str(directory))} is not in a "
                "git checkout"
            )
        if name is not None and "unknown revision" in stderr:
            return Checkout(name=name, reason=f"the checkout {name} has no commit at HEAD yet")
        return Checkout(reason=f"git rev-parse failed: {_git_error(stderr, returncode)}")
    head = lines[1].strip() if len(lines) > 1 else ""
    if name is None or not is_commit_id(head):
        return Checkout(reason="git printed no checkout and commit")
    return Checkout(name=name, head=head.casefold(), path=lines[0].strip())


def _attributed_anchors(doc: Doc) -> tuple[dict[str, tuple[str, str | None]], str | None]:
    """`({repo_key: (name, anchor)}, why the legacy anchor is tied to no repository)`."""
    if doc.verified_at:
        anchors: dict[str, tuple[str, str | None]] = {}
        for name, anchor in doc.verified_at.items():
            anchors.setdefault(repo_key(name), (name, anchor))
        return anchors, None
    # None: written, but not as text (a load warning names it)
    legacy = None if doc.verified_at_commit is None else doc.verified_at_commit.strip()
    if doc.verified_at is None or legacy == "":
        return {}, None
    if doc.repos is None:
        return {}, "its repository links cannot be read"
    names = {repo_key(name): name for name in linked_repos(doc)}
    if len(names) == 1:
        ((key, name),) = names.items()
        return {key: (name, legacy)}, None
    if not names:
        return {}, "the record names no repository"
    return {}, f"the record names {len(names)} repositories"


def _repo_snapshot(
    name: str, anchor: str | None, checkout: SessionCheckout | None, *, unreadable_field: bool
) -> RepoSnapshot:
    def unknown(reason: str) -> RepoSnapshot:
        return RepoSnapshot(name, anchor, SnapshotState.UNKNOWN, reason=reason)

    if unreadable_field:
        return unknown(UNREADABLE_FIELD)
    if anchor is None:
        return unknown("its anchor is not text")
    if not anchor:
        return unknown(NO_ANCHOR)
    if not is_commit_id(anchor):
        return unknown("its anchor is not a commit id")
    if checkout is None:
        return unknown("no checkout was looked up")
    found = checkout.lookup()
    if found.head is None:
        return unknown(found.reason or "no checkout here")
    if repo_key(found.name or "") != repo_key(name):
        return unknown(
            f"no checkout of it here, engmem's working directory is in "
            f"{_shown_path(found.path or found.name or '')}"
        )
    if found.head.startswith(anchor.strip().casefold()):
        return RepoSnapshot(name, anchor, SnapshotState.MATCH, current=found.head)
    return RepoSnapshot(name, anchor, SnapshotState.DIFFERS, current=found.head)


def snapshots(doc: Doc, checkout: SessionCheckout | None) -> list[RepoSnapshot]:
    """One entry per linked or anchored repository, in `repos` order then `verified_at` order,
    plus a legacy anchor that names no repository; empty when the record states neither."""
    anchors, legacy_reason = _attributed_anchors(doc)
    names: dict[str, str] = {}
    for name in linked_repos(doc):
        names.setdefault(repo_key(name), name)
    for key, (name, _anchor) in anchors.items():
        names.setdefault(key, name)

    result = [
        _repo_snapshot(
            name, anchors.get(key, (name, ""))[1], checkout,
            unreadable_field=doc.verified_at is None,
        )
        for key, name in names.items()
    ]
    if doc.verified_at is None and not result:
        result.append(RepoSnapshot("", None, SnapshotState.UNKNOWN, reason=UNREADABLE_FIELD))
    if legacy_reason is not None:
        legacy = doc.verified_at_commit
        result.append(
            RepoSnapshot(
                "", None if legacy is None else legacy.strip(), SnapshotState.UNKNOWN,
                reason="its anchor is not text" if legacy is None else legacy_reason,
                legacy=True,
            )
        )
    return result
