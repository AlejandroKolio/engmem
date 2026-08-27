# Contributing

## Getting set up

The project uses [uv](https://docs.astral.sh/uv/). Python 3.11 or newer.

    uv sync --group dev
    uv run pytest -q

The test suite is the whole check — it runs offline, needs no fixtures beyond the
repository, and finishes in seconds.

## Pull requests

Branch, push, open a PR against `main`. One check gates the merge: **`ci`**, which
passes only when the test matrix (Linux, macOS, Windows × Python 3.11–3.13) and the
runtime-dependency job both succeed.

Squash merge is the only merge method; write the PR title as the commit message you
want in the history.

## What the tests enforce

Some tests exist to protect invariants rather than behaviour. If one of these fails,
the fix is usually the change, not the test.

- **Two runtime dependencies.** The wheel must require `pyyaml` and `markdown-it-py`
  and nothing else. A new runtime import fails `runtime-dependencies` in CI. Dev-only
  tools belong in the `dev` dependency group.
- **No network.** `tests/conftest.py` blocks sockets for the entire suite.
- **stdout is protocol-only.** `test_stdout_is_protocol_only.py` walks the AST and
  fails if anything prints to stdout outside the output layer — the MCP server shares
  that stream, and one stray `print` corrupts every message on it.
- **Docs match the code.** `test_docs_match_the_code.py` checks that names, constants,
  and CLI flags quoted in the documentation still exist.
- **Fixtures land as bytes.** `write_file` in `tests/conftest.py` writes exactly the
  text it is given. Text mode translates every `\n` to `\r\n` on Windows, so a test
  pinning a document's line endings, byte-order mark, or exact bytes was handed a
  document it never asked for — and the failure only ever showed up on the Windows leg.
  Where the bytes matter, write them through `write_file` or `write_bytes`; a bare
  `path.write_text(...)` is fine only when the test does not care what lands on disk.

## Fixtures and examples

Every fixture, example, and document in this repository is invented. Do not add
material from real work — not a class name, not a ticket number, not a paraphrased
incident, in tests or docs alike. If you need a scenario, make one up; the existing
fixtures under `tests/fixtures/sessions/` show the shape.

This is not decorative. The store is designed to hold things people cannot publish,
so the project itself has to demonstrate the discipline it asks for.

## Style

Match the surrounding code. Comments explain why something is not the obvious choice;
anything a reader can get from the code itself does not need a comment.

Documentation is prose, not bullet dumps. If a change alters behaviour described in
`README.md` or `ENGMEM-SPEC.md`, update it in the same PR.
