# Security Policy

## Supported versions

engmem is pre-1.0. Only the latest commit on `main` is supported; fixes land there
and are not backported.

## Reporting a vulnerability

Report privately through GitHub: **Security → Advisories → Report a vulnerability**
on this repository. Please do not open a public issue for anything exploitable.

Include what you did, what happened, and what you expected. A failing test or a
minimal store that reproduces the problem is worth more than a description.

Expect an acknowledgement within a week. If a report is valid, the advisory stays
private until a fix is on `main`.

## Threat model

engmem reads and writes markdown on the machine it runs on. It has two runtime
dependencies (`pyyaml` and `markdown-it-py`), no server, and makes no network calls — `tests/conftest.py` blocks
sockets for the whole suite so a new call cannot be added unnoticed.

That shapes what counts as a vulnerability here. Anything that moves store content
off the machine, or reaches outside the configured store directory, is in scope:

- a search, install, or MCP path that opens a socket
- a path traversal that reads or writes outside the store
- a document whose contents can execute code when parsed or rendered
- a write tool that modifies a file the user did not point it at

Out of scope: what you choose to put in your own store, and the contents of a store
you have deliberately committed to a repository. engmem cannot tell a public note
from a private one — that judgement is yours, before the file is written.

## Windows is checked less than Linux and macOS

CI runs the whole suite on Windows, but about fifty of its tests are skipped there, and some
guarantees are weaker there or absent:

- **No `dir_fd` opens.** On Linux and macOS a read or an append inside the store opens each
  path component relative to the one before, with `O_NOFOLLOW`, so a link swapped in at any
  depth makes the open fail. Windows has no such open. engmem checks each component with
  `lstat`, opens the file by name, and afterwards confirms the name is still the file it
  opened. Two races remain: a directory above a file swapped for a junction between the check
  and the open is read through, and a link swapped in for a log that does not exist yet can
  leave an empty file outside the store. Tests of the `dir_fd` path are skipped there; the
  fallback path itself runs on every platform, switched on by the tests.
- **No private file modes.** On Linux and macOS a new document, its staged copy and a Claude
  Desktop config engmem creates are `0600`, and a rewrite keeps the file's mode. On Windows
  `chmod` only sets the read-only bit, so these files get the permissions of their folder.
  Those tests are skipped there.
- **No unwritable-store tests.** The tests that make a store, a log or a config unwritable and
  check what engmem reports are skipped, because `chmod` there only sets the read-only bit,
  which stops neither reads nor creating files in a directory. The code paths exist; they are
  not exercised on Windows.
- **Appends take a lock.** On Windows `O_APPEND` is a seek and a write, so log appends run under
  a lock directory in the store; a lock left by a killed process blocks that log until it is
  removed by hand.

Symlinks and junctions themselves are tested on Windows. The details and the reasons are in
`docs/design/contracts/runtime.md`, "Reads stay inside the store" and "Logs are written inside
the store". A report that turns either race into a read or a write outside the store is in
scope.
