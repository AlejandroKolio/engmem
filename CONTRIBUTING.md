# Contributing

## Getting set up

The project uses [uv](https://docs.astral.sh/uv/). Python 3.11 or newer.

    uv sync --group dev
    uv run pytest -q

The test suite is the whole check — it runs offline, needs no fixtures beyond the
repository, and finishes in seconds.

## Pull requests

Branch, push, open a PR against `main`. One check gates the merge: **`ci`**, which
passes only when the test matrix (Linux, macOS, Windows × Python 3.11–3.13), the
runtime-dependency job and the packaging job all succeed.

Squash merge is the only merge method; write the PR title as the commit message you
want in the history.

## Releases

The version lives in `pyproject.toml` only; `engmem.__version__` reads it from the
installed package metadata. Until 1.0 every release bumps the patch number:

    uv version --bump patch
    git commit -am "release: v$(uv version --short)"
    git tag "v$(uv version --short)" && git push --tags

The tag triggers `release.yml`, which refuses a tag that does not match `pyproject.toml`,
re-runs the suite, builds, and publishes to PyPI.

## What the tests enforce

Some tests exist to protect invariants rather than behaviour. If one of these fails,
the fix is usually the change, not the test.

- **Two runtime dependencies.** The wheel must require `pyyaml` and `markdown-it-py`
  and nothing else. A new runtime import fails `runtime-dependencies` in CI. Dev-only
  tools belong in the `dev` dependency group.
- **No network.** `tests/conftest.py` blocks sockets for the entire suite.
- **stdout is protocol-only.** `test_stdout_is_protocol_only.py` walks the AST of
  `mcp_server` and of every engmem module it imports, directly or not, and fails if any of
  them prints to stdout — the MCP server shares that stream, and one stray `print` in
  `gate1` corrupts every message on it as surely as one in `mcp_server`.
- **Every text open names its encoding.** `test_explicit_encoding.py` reads every
  text-mode `open`, `read_text`, `write_text` and text-mode `subprocess` call in `src/`
  and `tools/` from the syntax tree. The locale's code page is not UTF-8 on Windows or under
  `LANG=C`, and the dynamic check in `test_encoding.py` only sees the paths its script runs.
- **The start template's draft parses as a draft.** `test_lifecycle.py` builds the draft
  from the YAML block in `engmem.start.md` itself, not a copy, so a typo in the template
  cannot publish every new draft as active.
- **Search cost is linear.** The guard in `test_cli.py` counts Python calls into engmem, not
  seconds, over equal steps of cloned documents: linear code adds exactly the same work per
  step, so any per-pair work shows as a non-zero second difference on any machine.
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
