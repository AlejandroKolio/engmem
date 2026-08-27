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

engmem reads and writes markdown on the machine it runs on. It has one runtime
dependency, no server, and makes no network calls — `tests/conftest.py` blocks
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
