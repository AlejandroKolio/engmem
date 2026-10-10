"""Each repository's snapshot anchor next to the commit this session's checkout is at (US-11), and
whether the record's covered files changed between the two (US-12).
Why each rule is shaped as it is: contracts/provenance.md."""

from __future__ import annotations

import enum
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from engmem.executables import on_path
from engmem.scoring import repo_key
from engmem.spine import Doc, is_commit_id, linked_repos

GIT_LOOKUP_SECONDS = 5.0
# a commit is shown as git abbreviates it
COMMIT_DISPLAY_CHARS = 7
MAX_COVERED_FILES = 100
NO_ANCHOR = "no anchor recorded"
UNREADABLE_FIELD = "verified_at cannot be read (see the load warning)"
_GIT_OUTPUT_BYTES = 64 * 1024
_REASON_MAX_CHARS = 160
# a path in a reason keeps its end, where the checkout's own name is
_PATH_SHOWN_MAX = 60
DUBIOUS_OWNERSHIP = "git refuses this checkout: it is owned by another user"
_COVERED_OUTPUT_BYTES = 8_388_608  # 8 MiB
_UNREADABLE_OUTPUT = "git printed output engmem cannot read"
_FILE_TYPE_BITS = 0o170000
_DIRECTORY_TYPE = 0o040000
# a line break would split one `git cat-file --batch` request into two
_PATH_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
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


class CoveredState(enum.StrEnum):
    """What the covered files did between the anchor and HEAD; CHANGED asks for a review and
    says nothing about whether the decision still holds."""

    UNCHANGED = "unchanged"
    CHANGED = "changed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CoveredChange:
    path: str  # as written in covers_files
    deleted: bool = False


@dataclass(frozen=True)
class CoveredCheck:
    state: CoveredState
    changes: tuple[CoveredChange, ...] = ()  # in covers_files order
    reason: str | None = None  # why the state is UNKNOWN


@dataclass(frozen=True)
class RepoSnapshot:
    repo: str  # as written; "" for a legacy anchor tied to no repository
    anchor: str | None
    state: SnapshotState
    current: str | None = None  # HEAD of this session's checkout, when it is this repository
    reason: str | None = None  # why the state is UNKNOWN
    legacy: bool = False
    covered: CoveredCheck | None = None  # DIFFERS with covered files only


@dataclass(frozen=True)
class Checkout:
    """The git checkout a directory is in: `name` and `head`, or `reason` when there is none."""

    name: str | None = None
    head: str | None = None
    reason: str | None = None
    path: str | None = None  # the checkout's top-level directory, as git printed it
    directory: Path | None = None  # where git was run to find it


class SessionCheckout:
    """The checkout this process runs in, or the one `directory` is in, looked up on first use
    and at most once."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory
        self._checkout: Checkout | None = None
        # a git that timed out or could not start is not asked again in this search
        self._git_failure: str | None = None

    def lookup(self) -> Checkout:
        if self._checkout is None:
            self._checkout = (
                _find_session_checkout() if self._directory is None
                else find_checkout(self._directory)
            )
        return self._checkout

    def covered(self, anchor: str, paths: list[str]) -> CoveredCheck:
        """`paths` between `anchor` and the HEAD `lookup` found; only for a DIFFERS snapshot."""
        found = self.lookup()
        if found.directory is None or found.head is None:
            return _covered_unknown(found.reason or "no checkout here")
        if self._git_failure is not None:
            return _covered_unknown(self._git_failure)
        try:
            return _compare_covered(found.directory, anchor, found.head, paths)
        except _GitUnavailable as exc:
            self._git_failure = exc.reason
            return _covered_unknown(exc.reason)


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


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REPOSITORY_ENV}
    # LC_ALL=C: the reasons below are read from git's own messages, which a locale translates
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANGUAGE="C")
    # a partial clone fetches an object it lacks, over a transport its own config names; with
    # no lazy fetch and an empty protocol allow-list a missing object stays missing
    env.update(GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="")
    return env


def _run_git(git: str, directory: Path) -> tuple[int, str, str]:
    """`(exit code, stdout, stderr)` of `git rev-parse --show-toplevel HEAD` in `directory`."""
    # files, not pipes, for the reason doctor's probes use them (contracts/doctor.md)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        completed = subprocess.run(
            [git, "rev-parse", "--show-toplevel", "HEAD"],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, cwd=directory, env=_git_env(),
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
    return Checkout(name=name, head=head.casefold(), path=lines[0].strip(), directory=directory)


def _covered_unknown(reason: str) -> CoveredCheck:
    return CoveredCheck(CoveredState.UNKNOWN, reason=reason)


def _is_repository_path(path: str) -> bool:
    """A path from the top-level directory, `/`-separated, with nothing for git to reinterpret:
    `<rev>:./x` and `<rev>:../x` are read from the working directory, not the top level."""
    if _PATH_CONTROL_RE.search(path) or path.startswith("/"):
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _directories(path: str) -> list[str]:
    """Every directory a lookup of `path` reads, from the top level ("") down."""
    parts = path.split("/")
    return ["/".join(parts[:depth]) for depth in range(len(parts))]


def _tree_request(commit: str, directory: str) -> str:
    return f"{commit}^{{tree}}" if not directory else f"{commit}:{directory}"


_Record = tuple[bytes, bytes, bytes] | bytes  # (object id, type, content) or b"missing"/...


def _batch_record(output: bytes, pos: int, request: str) -> tuple[_Record, int]:
    """One `git cat-file --batch` answer at `pos`; ValueError when it is not one."""
    end = output.index(b"\n", pos)
    header = output[pos:end]
    pos = end + 1
    for status in (b"missing", b"ambiguous"):
        if header == request.encode("utf-8") + b" " + status:
            return status, pos
    object_id, kind, size_text = header.split(b" ")
    size = int(size_text)
    content = output[pos:pos + size]
    if len(content) != size or output[pos + size:pos + size + 1] != b"\n":
        raise ValueError("truncated object")
    return (object_id, kind, content), pos + size + 1


def _tree_entries(content: bytes, id_bytes: int) -> dict[bytes, tuple[int, bytes]]:
    """`{name: (mode, object id)}` of git's binary tree format: `<mode> <name>\\0<raw id>`. The
    mode is octal text that older tools wrote zero-padded (`040000`), so it is compared as the
    number git reads, never as text; one that is not octal is a ValueError."""
    entries: dict[bytes, tuple[int, bytes]] = {}
    pos = 0
    while pos < len(content):
        space = content.index(b" ", pos)
        nul = content.index(b"\0", space)
        object_id = content[nul + 1:nul + 1 + id_bytes]
        if len(object_id) != id_bytes:
            raise ValueError("truncated tree entry")
        mode_text = content[pos:space]
        if not mode_text or any(digit not in b"01234567" for digit in mode_text):
            raise ValueError("tree entry mode is not octal")
        entries[content[space + 1:nul]] = (int(mode_text, 8), object_id)
        pos = nul + 1 + id_bytes
    return entries


_Trees = dict[tuple[str, str], dict[bytes, tuple[int, bytes]] | None]


class _GitUnavailable(Exception):
    """git could not be asked at all: not on PATH, would not start, or did not answer."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Unreadable(Exception):
    def __init__(self, directory: str, commit: str) -> None:
        super().__init__(directory, commit)
        self.directory = directory
        self.commit = commit


def _entry(trees: _Trees, commit: str, path: str) -> tuple[int, bytes] | None:
    """`path`'s `(mode, object id)` at `commit`, or None when a tree that was read lacks it. A
    tree that could not be read is `_Unreadable`: git reports it as missing too, and a deletion
    is claimed only where absence was seen."""
    parts = path.split("/")
    for depth, part in enumerate(parts):
        directory = "/".join(parts[:depth])
        tree = trees[(commit, directory)]
        if tree is None:
            raise _Unreadable(directory, commit)
        entry = tree.get(part.encode("utf-8"))
        if entry is None:
            return None
        if depth == len(parts) - 1:
            return entry
        if entry[0] & _FILE_TYPE_BITS != _DIRECTORY_TYPE:
            return None
    return None


def _read_trees(
    git: str, directory: Path, anchor: str, head: str, paths: list[str]
) -> _Trees | str:
    """Every tree the lookups of `paths` need at both commits, in one `git cat-file --batch`;
    a str is why there are none."""
    directories = list(dict.fromkeys(d for path in paths for d in _directories(path)))
    keys = [(commit, d) for commit in (anchor, head) for d in directories]
    requests = [f"{anchor}^{{commit}}", *(_tree_request(commit, d) for commit, d in keys)]
    try:
        returncode, stdout, stderr = _run_cat_file(
            git, directory, "".join(f"{request}\n" for request in requests).encode("utf-8")
        )
    except subprocess.TimeoutExpired:
        raise _GitUnavailable(f"git did not answer within {GIT_LOOKUP_SECONDS:g} s") from None
    except OSError as exc:
        raise _GitUnavailable(f"git could not be run ({type(exc).__name__})") from None
    if returncode != 0:
        return f"git cat-file failed: {_git_error(stderr.decode('utf-8', 'replace'), returncode)}"
    if len(stdout) > _COVERED_OUTPUT_BYTES:
        return "the covered directories are too large to compare"
    shown = anchor[:COMMIT_DISPLAY_CHARS]
    try:
        commit, pos = _batch_record(stdout, 0, requests[0])
        if commit == b"missing":
            return f"commit {shown} is not in this checkout"
        if commit == b"ambiguous":
            return f"the anchor {shown} is ambiguous in this checkout"
        if not isinstance(commit, tuple):
            return _UNREADABLE_OUTPUT
        # a branch or tag named like a short anchor wins over the commit in git's lookup
        if not commit[0].decode("ascii").startswith(anchor):
            return f"the anchor {shown} names a branch or tag here, not a commit"
        id_bytes = len(commit[0]) // 2
        trees: _Trees = {}
        for key, request in zip(keys, requests[1:]):
            record, pos = _batch_record(stdout, pos, request)
            is_tree = isinstance(record, tuple) and record[1] == b"tree"
            trees[key] = _tree_entries(record[2], id_bytes) if is_tree else None
        if pos != len(stdout):
            return _UNREADABLE_OUTPUT
    except (ValueError, UnicodeDecodeError):
        return _UNREADABLE_OUTPUT
    return trees


def _run_cat_file(git: str, directory: Path, requests: bytes) -> tuple[int, bytes, bytes]:
    """`(exit code, stdout, stderr)` of `git cat-file --batch` fed `requests`. It reads objects
    and nothing else: no index, so no `core.fsmonitor`, and no diff driver or filter."""
    with (
        tempfile.TemporaryFile() as given,
        tempfile.TemporaryFile() as out,
        tempfile.TemporaryFile() as err,
    ):
        given.write(requests)
        given.seek(0)
        completed = subprocess.run(
            [git, "cat-file", "--batch"],
            stdin=given, stdout=out, stderr=err, cwd=directory, env=_git_env(),
            timeout=GIT_LOOKUP_SECONDS,
        )
        out.seek(0)
        err.seek(0)
        return (
            completed.returncode,
            out.read(_COVERED_OUTPUT_BYTES + 1),
            err.read(_GIT_OUTPUT_BYTES),
        )


def compare_covered(directory: Path, anchor: str, head: str, paths: list[str]) -> CoveredCheck:
    """CHANGED when any covered path differs or is gone at `head`; otherwise UNKNOWN when any
    could not be compared; UNCHANGED only when every one was found at `anchor` and is the same
    at `head`. Every way git can fail is a reason, never an exception."""
    try:
        return _compare_covered(directory, anchor, head, paths)
    except _GitUnavailable as exc:
        return _covered_unknown(exc.reason)


def _compare_covered(directory: Path, anchor: str, head: str, paths: list[str]) -> CoveredCheck:
    # an empty or non-hex commit would turn `<commit>:<dir>` into `:<dir>`, an index read,
    # and reading the index runs the repository's core.fsmonitor
    if not is_commit_id(anchor):
        return _covered_unknown("its anchor is not a commit id")
    if not is_commit_id(head):
        return _covered_unknown("HEAD is not a commit id")
    paths = list(dict.fromkeys(paths))
    if len(paths) > MAX_COVERED_FILES:
        return _covered_unknown(f"the record lists more than {MAX_COVERED_FILES} covered files")
    anchor = anchor.strip().casefold()
    head = head.strip().casefold()
    valid = [path for path in paths if _is_repository_path(path)]
    trees: _Trees | str = {}
    if valid:
        git = on_path("git")
        if git is None:
            raise _GitUnavailable("git is not on PATH")
        trees = _read_trees(git, directory, anchor, head, valid)
        if isinstance(trees, str):
            return _covered_unknown(trees)

    changes: list[CoveredChange] = []
    reasons: list[str] = []
    for path in paths:
        if not _is_repository_path(path):
            reasons.append(f"covered file {_shown_path(path)} is not a path inside the repository")
            continue
        try:
            before = _entry(trees, anchor, path)
            after = _entry(trees, head, path)
        except _Unreadable as exc:
            place = _shown_path(exc.directory) if exc.directory else "the top-level directory"
            reasons.append(f"{place} cannot be read at commit {exc.commit[:COMMIT_DISPLAY_CHARS]}")
            continue
        if before is None:
            reasons.append(f"{_shown_path(path)} is not in commit {anchor[:COMMIT_DISPLAY_CHARS]}")
        elif after is None:
            changes.append(CoveredChange(path, deleted=True))
        elif after != before:
            changes.append(CoveredChange(path))
    if changes:
        return CoveredCheck(CoveredState.CHANGED, tuple(changes))
    if reasons:
        return _covered_unknown(reasons[0])
    return CoveredCheck(CoveredState.UNCHANGED)


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
    covered = [path.strip() for path in doc.covers_files if path.strip()]
    if not covered or checkout is None:
        return result
    return [
        replace(snapshot, covered=_covered_check(doc, covered, len(names), snapshot, checkout))
        if snapshot.state is SnapshotState.DIFFERS else snapshot
        for snapshot in result
    ]


def _covered_check(
    doc: Doc, paths: list[str], repositories: int, snapshot: RepoSnapshot,
    checkout: SessionCheckout,
) -> CoveredCheck:
    """`covers_files` is one flat list, so it is read as the files of the record's one
    repository; with more than one it is never guessed onto the checkout at hand."""
    if doc.repos is None:
        return _covered_unknown("its repository links cannot be read")
    if repositories > 1:
        return _covered_unknown(
            f"covers_files does not say which of the record's {repositories} repositories each "
            "file is in"
        )
    return checkout.covered(snapshot.anchor or "", paths)
