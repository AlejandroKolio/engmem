# Contract: `engmem mcp` stdio server

Source: `src/engmem/mcp_server.py`. Author-approved addition beyond
`ENGMEM-SPEC.md` §11 (see §5 `engmem mcp`) — Claude Desktop cannot run shell
commands, so the CLI alone is unreachable from it.

## Transport and the stdout-purity rule

Newline-delimited JSON-RPC 2.0 on stdio, one JSON object per line. `serve()`
is the loop and the only writer of `stdout`; every write goes through
`_write`, which relies on `json.dumps` escaping embedded newlines so a
multi-line tool result stays one stdout line. `stdout` carries the protocol
and nothing else — no banner, no confirmation, no stray print. All
diagnostics go to `sys.stderr`. This is the opposite convention from the CLI,
where `runtime.fail` deliberately writes to both streams; `fail`-style
dual-stream writes must never happen on this path.

## Protocol-level fault vs. tool-level failure

A malformed request (missing/wrong-typed required field, unknown method,
wrong `jsonrpc` version) is a JSON-RPC error response (`_ProtocolError`,
codes per the 2.0 spec). A request that conforms to its schema but fails at
runtime — a missing store, a bad query result, a write tool's business-rule
refusal — is an ordinary tool result with `isError: true` (`_ToolError`).
Conflating the two would either surface a normal "store not found" as a
protocol fault a client can't render sensibly, or silently coerce a
malformed request (e.g. a non-string `session_id`) into something that looks
like it worked.

An id-less request-shaped message (a method not in `_NOTIFICATION_METHODS`)
is unanswerable — its handler is skipped entirely rather than doing
(possibly expensive) work whose result is guaranteed to be discarded.

`_dispatch`'s `except Exception` is a last-resort guard: it exists so one
unhandled bug in a handler reports an internal-error response instead of
killing the stdio loop for every other request still coming.

## Write tools: security contract

Every write target MUST resolve inside the resolved store's `sessions/`
directory. Order matters:

1. `doc_id` is validated against `spine.validate_doc_id` *before* it ever
   touches a `Path` — that regex alone (ASCII lowercase/digits/hyphens, 2+
   hyphen-separated parts, never starting with `.`) already rejects `..`
   traversal, an absolute path, a NUL byte, and every reserved device name.
2. `_resolve_sessions_dir` resolves both the store root and `sessions/` and
   rejects a mismatch, catching `sessions/` itself being a symlink pointing
   outside the store — a raw string comparison would miss that.
3. `_resolve_write_target` separately refuses to write through an existing
   target that is itself a symlink, checked *before* `Path.resolve()` would
   follow it to its target and compare against the wrong location.

Every write is staged to a sibling `.md.tmp` file (excluded from every scan
by its suffix, so `load_store`/`stray_documents` never see it mid-write),
parsed with the same `spine._parse_one` `load_store` itself uses, and only
`os.replace`d over the target — atomic on POSIX — after every handler-specific
check passes. On any failure the staged file is discarded, never left to be
mistaken for a real document.

The three tools' own rules:
- `engmem_create_draft` never overwrites: fails if the target already exists.
- `engmem_complete_draft` requires the existing file to have `status: draft`
  and the new content to have `status: active` with a matching `id` — it can
  never touch an already-active or superseded document.
- `engmem_mark_superseded` requires the existing file to have `status: active`
  and patches only the `status`/`superseded_by` lines in place
  (`_patch_front_matter_line`), leaving the rest of the document
  byte-for-byte untouched — retyping the whole document to flip two fields
  would risk silent drift in everything else.

## Broken pipe handling

A `BrokenPipeError` while writing to `stdout` means the client is already
gone — reported on stderr and the loop exits 0, a clean teardown, not a
crash. On the real process stdout only, `_suppress_late_broken_pipe`
redirects the stdout file descriptor to `os.devnull` first, because Python's
interpreter-shutdown flush of `sys.stdout` would otherwise raise the same
error a second time from inside `atexit`, printing an unwanted traceback. An
injected stream (tests, or any other caller) carries no such risk and is left
alone.
