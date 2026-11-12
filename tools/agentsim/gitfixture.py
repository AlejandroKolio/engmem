"""A checkout with a fixed git history: commit ids computed without git, so a note can carry them."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

AUTHOR = "Sim Agent <agent@example.invalid>"


@dataclass(frozen=True)
class Commit:
    """One commit: every file of the tree at that point, its message and its Unix time (UTC)."""

    files: tuple[tuple[str, str], ...]
    message: str
    timestamp: int


def _object(kind: str, body: bytes) -> bytes:
    return hashlib.sha1(f"{kind} {len(body)}\0".encode("ascii") + body).digest()


def _tree(files: dict[str, bytes]) -> bytes:
    entries: dict[str, tuple[str, bytes]] = {}
    children: dict[str, dict[str, bytes]] = {}
    for path, content in files.items():
        head, _, rest = path.partition("/")
        if rest:
            children.setdefault(head, {})[rest] = content
        else:
            entries[head] = ("100644", _object("blob", content))
    for name, sub in children.items():
        entries[name] = ("40000", _tree(sub))
    ordered = sorted(entries.items(), key=lambda item: item[0] + ("/" if item[1][0] == "40000" else ""))
    body = b"".join(f"{mode} {name}".encode("utf-8") + b"\0" + sha for name, (mode, sha) in ordered)
    return _object("tree", body)


def commit_ids(history: tuple[Commit, ...]) -> list[str]:
    """The id git gives each commit of `history`, made with `AUTHOR` at its timestamp."""
    ids: list[str] = []
    for commit in history:
        tree = _tree({path: text.encode("utf-8") for path, text in commit.files}).hex()
        lines = [f"tree {tree}"]
        if ids:
            lines.append(f"parent {ids[-1]}")
        stamp = f"{commit.timestamp} +0000"
        lines += [f"author {AUTHOR} {stamp}", f"committer {AUTHOR} {stamp}", "", commit.message]
        ids.append(_object("commit", ("\n".join(lines) + "\n").encode("utf-8")).hex())
    return ids
