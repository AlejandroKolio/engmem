"""A throwaway machine for one profile: its own HOME, XDG and APPDATA roots, store and checkouts."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agentsim.gitfixture import Commit, commit_ids
from engmem import runtime

PROTECT_ENV = "ENGMEM_E2E_PROTECT"
_DROPPED_ENV = (
    "ENGMEM_STORE",
    "ENGMEM_HOME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "VIRTUAL_ENV",
)
LIVE_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "COLORTERM",
    "TZ",
)
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Sim Agent",
    "GIT_AUTHOR_EMAIL": "agent@example.invalid",
    "GIT_COMMITTER_NAME": "Sim Agent",
    "GIT_COMMITTER_EMAIL": "agent@example.invalid",
}
ENGMEM_BIN = Path(sys.executable).parent / ("engmem.exe" if sys.platform == "win32" else "engmem")
SUBPROCESS_TIMEOUT = 120
TIMED_OUT = 124


def _text(value) -> str:
    """Output a timed-out process left, whether the runtime hands it over as bytes or text."""
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def protected_roots(environ: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """The real store engmem would use here, its default, ENGMEM_STORE, and `PROTECT_ENV`'s list."""
    environ = os.environ if environ is None else environ
    roots = [runtime.default_store()]
    try:
        roots.append(runtime.locate_store(None).path)
    except runtime.StoreSettingError:
        pass
    for name in ("ENGMEM_HOME", "ENGMEM_STORE"):
        value = runtime.store_path_of(environ.get(name))
        if value is not None:
            roots.append(value)
    roots += [Path(part).expanduser() for part in environ.get(PROTECT_ENV, "").split(os.pathsep) if part.strip()]
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


FORBIDDEN_ROOTS = protected_roots()


class ForbiddenPath(RuntimeError):
    """A sandbox path resolved into a protected root."""


def _folded(path: Path) -> tuple[str, ...]:
    return tuple(os.path.normcase(part).casefold() for part in path.parts)


def _nearest_existing(path: Path) -> Path | None:
    return next((p for p in (path, *path.parents) if p.exists()), None)


def _inside(path: Path, root: Path) -> bool:
    """Ignoring case, so a case-insensitive filesystem cannot alias a root; then by file identity."""
    folded, root_folded = _folded(path), _folded(root)
    if folded[: len(root_folded)] == root_folded:
        return True
    if not root.exists():
        return False
    existing = _nearest_existing(path)
    if existing is None:
        return False
    return any(os.path.samefile(candidate, root) for candidate in (existing, *existing.parents))


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALLOWED_ROOTS: list[Path] = [(REPO_ROOT / ".tmp").resolve(), Path(tempfile.gettempdir()).resolve()]


def _refuse_protected(resolved: Path) -> None:
    for root in FORBIDDEN_ROOTS:
        if _inside(resolved, root):
            raise ForbiddenPath(f"refusing to use {resolved}: it is inside a protected root")


def allow(path: Path) -> Path:
    """Adds `path` to the allowed roots, as `--allow-out` asks; a protected root is still refused."""
    resolved = path.resolve()
    _refuse_protected(resolved)
    if resolved not in ALLOWED_ROOTS:
        ALLOWED_ROOTS.append(resolved)
    return resolved


SANDBOX_ROOT_BYTES = 160


class PathTooLong(RuntimeError):
    """A base directory too long to pad a sandbox root to `SANDBOX_ROOT_BYTES`."""


def path_bytes(path: Path) -> int:
    return len(str(path).encode("utf-8"))


def padded_root(base: Path, name: str, length: int = SANDBOX_ROOT_BYTES) -> Path:
    """`base/name-ppp…`, resolved, exactly `length` bytes long: engmem prints absolute paths and trims its
    output by bytes, so every sandbox gets paths of one length wherever `base` is."""
    plain = base.resolve() / f"{name}-"
    missing = length - path_bytes(plain)
    if missing < 0:
        raise PathTooLong(
            f"refusing {base}: a sandbox root under it would be {path_bytes(plain)} bytes, over the fixed "
            f"{length}; choose a shorter --out"
        )
    return plain.parent / f"{plain.name}{'p' * missing}"


def check_allowed(path: Path) -> Path:
    """`path` resolved; `ForbiddenPath` unless it is inside an allowed root and outside every protected one."""
    resolved = path.resolve()
    _refuse_protected(resolved)
    if not any(_inside(resolved, root) for root in ALLOWED_ROOTS):
        raise ForbiddenPath(
            f"refusing to use {resolved}: only the repository's .tmp/ and the system temp directory "
            "are allowed without --allow-out"
        )
    return resolved


@dataclass(frozen=True)
class Completed:
    """One finished subprocess: what it printed and how long it took."""

    stdout: str
    stderr: str
    returncode: int
    seconds: float


@dataclass
class Sandbox:
    """Directories and environment of one simulated machine; nothing points outside `root`."""

    root: Path
    env: dict[str, str] = field(default_factory=dict)
    has_git: bool = False
    inherit_env: bool = True

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def store(self) -> Path:
        return self.root / "store"

    @property
    def sessions(self) -> Path:
        return self.store / "sessions"

    def checkout(self, repo: str) -> Path:
        return self.root / "work" / repo

    @classmethod
    def create(cls, root: Path, *, inherit_env: bool = True, pass_env: tuple[str, ...] = ()) -> "Sandbox":
        """`inherit_env` False starts from `LIVE_ENV_ALLOWLIST` plus `pass_env`, as live runs do."""
        root = check_allowed(root)
        sandbox = cls(root=root, inherit_env=inherit_env)
        for directory in (
            sandbox.home,
            sandbox.sessions,
            root / "work",
            root / "xdg" / "config",
            root / "xdg" / "cache",
            root / "xdg" / "data",
            root / "xdg" / "state",
            root / "xdg" / "runtime",
            root / "xdg" / "data-dirs",
            root / "xdg" / "config-dirs",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        gitconfig = root / "gitconfig"
        gitconfig.write_text("", encoding="utf-8")
        sandbox.env = sandbox._environment(gitconfig, pass_env)
        sandbox.has_git = shutil.which("git", path=sandbox.env["PATH"]) is not None
        return sandbox

    def _environment(self, gitconfig: Path, pass_env: tuple[str, ...]) -> dict[str, str]:
        if self.inherit_env:
            env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
        else:
            kept = (*LIVE_ENV_ALLOWLIST, *pass_env)
            env = {k: v for k, v in os.environ.items() if k in kept and k not in _DROPPED_ENV}
        env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            HOMEDRIVE=self.home.drive or "",
            HOMEPATH=str(self.home)[len(self.home.drive):],
            APPDATA=str(self.home / "AppData" / "Roaming"),
            LOCALAPPDATA=str(self.home / "AppData" / "Local"),
            XDG_CONFIG_HOME=str(self.root / "xdg" / "config"),
            XDG_CACHE_HOME=str(self.root / "xdg" / "cache"),
            XDG_DATA_HOME=str(self.root / "xdg" / "data"),
            XDG_STATE_HOME=str(self.root / "xdg" / "state"),
            XDG_RUNTIME_DIR=str(self.root / "xdg" / "runtime"),
            XDG_DATA_DIRS=str(self.root / "xdg" / "data-dirs"),
            XDG_CONFIG_DIRS=str(self.root / "xdg" / "config-dirs"),
            GIT_CONFIG_GLOBAL=str(gitconfig),
            GIT_CONFIG_NOSYSTEM="1",
            PYTHONIOENCODING="utf-8",
            PATH=str(ENGMEM_BIN.parent) + os.pathsep + os.environ.get("PATH", ""),
            **_GIT_IDENTITY,
        )
        return env

    def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        cwd: Path | None = None,
        timeout: float = SUBPROCESS_TIMEOUT,
        extra_env: Mapping[str, str] | None = None,
    ) -> Completed:
        """Runs `argv` inside the sandbox; a failure to start or a timeout is reported, never raised."""
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                argv,
                input=stdin,
                cwd=cwd or self.root,
                env={**self.env, **(extra_env or {})},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            partial = _text(exc.stdout)
            note = f"timed out after {timeout} s"
            return Completed(partial, _text(exc.stderr) + note, TIMED_OUT, time.perf_counter() - started)
        except OSError as exc:
            return Completed("", str(exc), 127, time.perf_counter() - started)
        return Completed(proc.stdout, proc.stderr, proc.returncode, time.perf_counter() - started)

    def engmem(self, *args: str, stdin: str | None = None, cwd: Path | None = None) -> Completed:
        return self.run([str(ENGMEM_BIN), *args], stdin=stdin, cwd=cwd)

    def setup_engmem(self, *args: str, cwd: Path | None = None) -> Completed:
        """An engmem call made by the harness, not the agent: it must succeed."""
        done = self.engmem(*args, cwd=cwd)
        if done.returncode != 0:
            raise RuntimeError(f"setup `engmem {' '.join(args)}` failed: {done.stdout}{done.stderr}")
        return done

    def ensure_checkout(self, repo: str, history: tuple[Commit, ...] = ()) -> Path:
        """The agent's working repository, a git checkout with one commit when git is present, or
        with `history` replayed commit by commit so its ids match `gitfixture.commit_ids`."""
        path = self.checkout(repo)
        if path.is_dir():
            return path
        path.mkdir(parents=True)
        if history:
            self._replay(path, history)
            return path
        (path / "README.md").write_text(f"# {repo}\n", encoding="utf-8")
        if not self.has_git:
            return path
        for argv in (
            ["git", "init", "-q"],
            ["git", "add", "README.md"],
            ["git", "commit", "-q", "-m", "initial"],
        ):
            done = self.run(argv, cwd=path)
            if done.returncode != 0:
                raise RuntimeError(f"setup `{' '.join(argv)}` failed: {done.stderr}")
        return path

    def _replay(self, path: Path, history: tuple[Commit, ...]) -> None:
        if not self.has_git:
            raise RuntimeError(f"the checkout {path.name} needs git for its history")
        self._git(path, ["git", "init", "-q"])
        for commit in history:
            wanted = dict(commit.files)
            for existing in [p for p in path.rglob("*") if p.is_file() and ".git" not in p.parts]:
                if existing.relative_to(path).as_posix() not in wanted:
                    existing.unlink()
            for relative, text in commit.files:
                target = path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8", newline="\n")
            stamp = f"{commit.timestamp} +0000"
            dates = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
            self._git(path, ["git", "add", "-A"])
            self._git(path, ["git", "commit", "-q", "-m", commit.message], dates)
        expected = commit_ids(history)[-1]
        head = self._git(path, ["git", "rev-parse", "HEAD"]).stdout.strip()
        if head != expected:
            raise RuntimeError(f"the checkout {path.name} is at {head}, not the expected {expected}")

    def _git(self, path: Path, argv: list[str], extra_env: Mapping[str, str] | None = None) -> Completed:
        done = self.run(argv, cwd=path, extra_env=extra_env)
        if done.returncode != 0:
            raise RuntimeError(f"setup `{' '.join(argv)}` failed: {done.stderr}")
        return done

    def reset_store(self, sessions: Path) -> None:
        """The store as the corpus left it: every earlier run's documents and logs removed."""
        for entry in self.store.iterdir():
            if entry.name == ".git":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        shutil.copytree(sessions, self.sessions)
