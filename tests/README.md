# Adding a test

- **Fixtures** (`conftest.py`): `store` is an empty store (`store/sessions/`), `fixture_store` a copy of `fixtures/sessions/`, `env` a redirected HOME plus a project cwd. A module that needs another layout defines its own `store`, which wins.
- **Helpers** live in `support/`: `search` (`run_search`, `cli_search`, `mcp_search`), `rpc` (the in-process MCP harness and `McpClient`; import the module, `from support import rpc`), `docs` (`ACTIVE_CONTENT`, `DRAFT_CONTENT`, `write_file`, `replaced`), `fs` (`entry_names`, `without_access`), `platform` (skip markers), `paths` (`TESTS_DIR`, `REPO_ROOT`; never build paths from `__file__`).
- **Variants** of a shared document go through `docs.replaced`, which fails if the text it replaces is gone. A document whose exact bytes are the point of the test (CRLF, BOM) stays a literal.
- **Never import another test module**; shared code belongs in `support/`. Basenames must stay unique across `tests/` and its folders.
- `conftest.py` also holds the autouse safety net (no network, real user files untouched, isolated cache); do not work around it.
