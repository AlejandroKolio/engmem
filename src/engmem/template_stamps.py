"""The stamp an installed template carries: who wrote it, for which workflow, and whether its
body is still the one engmem wrote (US-26). Rules and their reasons: contracts/install.md,
"An installed template is engmem's until someone edits it"."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# raised when a template from the level before would fail against this runtime; doctor
# reports a template stamped with any other level as incompatible (contracts/doctor.md)
WORKFLOW = 1

# every template engmem v0.1.1-v0.1.4 installed, for every agent: they carry no digest of
# their own, so a body found here is one engmem wrote and nobody edited. All workflow 0
RELEASED_DIGESTS = frozenset({
    "00540d36a859bdd2", "0c1f8ef4f764b3c4", "1c2420cb25713547", "259d0baba554b1fb",
    "28ae6558d34585eb", "3085dd9fac827236", "34915eb84850156b", "39e7dde8c14355f5",
    "3db1218b7263f7e3", "4568dc5f656b9864", "5fbaa2766866332b", "71123c7352af71d3",
    "7f9da7be9fb521ab", "7fc78f41665bfe39", "88fad6dbc6dd4e3a", "90d79212e1aca656",
    "a77a618d8c06180c", "aab9798bc1ee39b2", "b2d5852a5003cc98", "c4e5394c86eb8d0e",
    "c908962a22aef10d",
})

_STAMP_LINE = re.compile(
    r"^<!-- engmem-template: (\S+) v(\S+?)(?: workflow (\d+))?(?: sha256:([0-9a-f]{16}))? -->"
    r"(?:\r\n|\n|$)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Stamp:
    name: str
    version: str
    # 0 for a stamp written before the field existed
    workflow: int
    digest: str | None


def digest(body: str) -> str:
    """The body's identity, whatever line endings the platform wrote it with."""
    return hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:16]


def stamp_line(source_name: str, version: str, body: str) -> str:
    return (
        f"<!-- engmem-template: {source_name.removesuffix('.md')} v{version} "
        f"workflow {WORKFLOW} sha256:{digest(body)} -->"
    )


def read_stamp(text: str) -> tuple[Stamp | None, str]:
    """The file's stamp, and its text without the stamp line: the body the digest covers."""
    match = _STAMP_LINE.search(text)
    if match is None:
        return None, text
    stamp = Stamp(
        name=match.group(1),
        version=match.group(2),
        workflow=int(match.group(3) or 0),
        digest=match.group(4),
    )
    return stamp, text[: match.start()] + text[match.end():]


def written_by_engmem(stamp: Stamp | None, body: str) -> bool:
    """The body is byte for byte one engmem wrote: its own stamp's digest, or a released one."""
    found = digest(body)
    if stamp is not None and stamp.digest is not None:
        return found == stamp.digest
    return found in RELEASED_DIGESTS
