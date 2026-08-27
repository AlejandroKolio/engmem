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

Both directions are UTF-8, as the stdio transport requires, and `serve` pins
that itself rather than inheriting it. Outbound needs nothing: `json.dumps`
defaults to `ensure_ascii=True`, so every frame is ASCII whatever encoding
`stdout` ended up with. Inbound is the half that had to be pinned —
`sys.stdin`'s error handler is environment-dependent (`surrogateescape` under
UTF-8 mode or a C locale, `strict` under a plain `en_US.UTF-8`), and under
`strict` a single undecodable byte from the client raised `UnicodeDecodeError`
out of the read loop and ended the session for every request still coming.
`_use_utf8_transport` reconfigures `stdin` to `utf-8`/`surrogateescape` before
the loop starts, so such a byte becomes a surrogate the envelope check below
refuses by name, identically on every host.

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

A wrongly typed `name` in `tools/call` or `prompts/get` is `-32602`, reported
as the unknown tool/prompt it is. `prompts/get` checks `isinstance(name, str)`
before the lookup and that guard is load-bearing, not decoration: an object or
array `name` is unhashable, so `PROMPT_TEMPLATES[name]` raises `TypeError`, and
the last-resort guard would return the client's own malformed request as a
`-32603` server bug. `tools/call` compares against a tuple, which needs no
hashing, so it was never exposed the same way.

One runtime fault is deliberately *not* a tool result: an `OSError` out of
`staging.stage` or `staging.commit` — a full disk, a revoked permission. It
reaches `_dispatch`'s last-resort guard and returns `-32603`.

Not because the document's state is unknown; it is known exactly. `os.replace`
is the last statement in `commit` and is atomic, and everything before it
either reads the target or touches the temp file, so a raising `stage`/`commit`
leaves the target untouched. The distinction from a `_ToolError` is who can act
on it: a missing store or a business-rule refusal is configuration or intent the
model can restate and retry, which is what `isError` is for, while a full disk
is a host-environment fault where an `isError` result would invite a retry that
cannot succeed. Note the boundary is `stage`/`commit` and not "the staged write"
at large — an `OSError` from re-parsing the staged file *is* a tool result.

The staged file is discarded on the way out either way. `discard` is
best-effort, so what actually guarantees nothing is mistaken for a document is
the `.tmp` suffix: `load_store` and `stray_documents` select on `.md`, and the
suffix keeps the name out of that.

## Envelope checks before any handler runs

Two things are rejected on the envelope, so no handler ever sees them:

- **An unpaired surrogate anywhere in the message** (`_has_unpaired_surrogate`)
  is `-32600`, with a null `id`. `\ud800` is well-formed JSON *syntax* and
  `json.loads` accepts it, but the `str` it produces cannot be encoded as UTF-8
  at all, and every consumer downstream fails differently: `content.encode` in
  a write tool raises (surfacing as a `-32603` that blames the server for the
  client's message), while `json.dumps` echoes the escape straight back into a
  frame a strict client rejects — breaking the one stdout guarantee this file
  opens with. One check on the whole decoded message covers `id`, `method`,
  every argument and every key at once, so a field added later cannot miss it.
  The `id` is not echoed because the `id` may itself be the offending string.
  The walk is iterative: the decoder already accepted this nesting depth, and a
  recursive walk of the same structure could raise `RecursionError` where it
  did not. A valid surrogate *pair* is a normal astral character — the decoder
  joins it into one code point, which encodes fine and is not touched.
- **A non-conforming `id`** is `-32600`, also with a null `id`. JSON-RPC 2.0
  allows string, number, or null. Booleans are excluded by name, because
  `isinstance(True, int)` is true and a plain `(str, int, float)` check would
  wave `id: true` through and echo it back.

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

Points 2 and 3 are the project's rules, not just this tool's: `engmem backfill`
makes the same two checks before it writes, in `cli._sessions_is_contained` and
in `apply_backfill` — see `contracts/backfill.md`, "What the replaced file
inherits". Point 1 is specific to the MCP tools, which take a `doc_id` from a
caller; `backfill` only ever writes to a path `load_store` already found.

`engmem_create_draft` is the only path in the project that creates a document
that did not exist, so it is the only one that can set a new document's mode:
there is none to restore, and it keeps the staged `0600`. `engmem_complete_draft`
then restores whatever it finds, so an MCP-authored document stays `0600` for
life, where a hand-written one keeps whatever its author gave it.

Every write is staged by `engmem/staging.py`, shared with `engmem backfill`
(see `contracts/backfill.md`), to a hidden sibling named
`.<target>.<hex>.tmp` — hidden and uuid-suffixed so a leftover from a crashed
run can neither be read as a document nor collide with a live one. No scan
sees it: `load_store` and `stray_documents` both select on `.md`, which that
suffix keeps it out of. It is parsed with the same `spine.parse_document`
`load_store` itself uses, and only `os.replace`d over the target — atomic on
POSIX — after every handler-specific check passes. On any failure the staged
file is discarded on the way out.

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

### `engmem_mark_superseded`: two reads of one document

This is the only write tool that rebuilds a file from what is already on
disk, and it needs the document twice in two different shapes: the
validating `parse_document` (which normalizes the BOM and the line endings)
to confirm `status: active`, and `staging.read_document` for the raw bytes
the new content is spliced into. The validating parse runs **first**, and it
stats before it reads, so its `Doc.source_identity` covers the raw re-read as
well; the identity is compared against a fresh `identity_for(target)` after
that read. A writer landing anywhere across the span is refused.

The raw re-read can also fail outright, and both of its failure modes are
properties of the document rather than of the server: the file may have
become unreadable (`OSError`), or it may no longer decode as UTF-8, since
`read_document` decodes what it reads and a writer can replace the bytes
between the two reads (`UnicodeDecodeError`, a `ValueError`). Both are
refused as an `isError` tool result saying the document could not be re-read
and nothing was written — never allowed to escape as a `-32603 internal
error` with raw codec text in it, the same rule the anchor case below states.

Without it the failure is silent rather than loud: the replacing document
still carries `status: active` and the expected front matter, so the staged
re-parse passes and the newer version is overwritten by the copy read before
it. This is `apply_backfill`'s rule (`contracts/backfill.md`, "The proposal
has to still describe the document"), applied to a window that is short
rather than human-length — a short window is not a closed one.

The related `engmem_create_draft` exists()-then-`os.replace` window is *not*
closed by this: closing it needs an exclusive-create commit path in
`staging.py`. Until then that tool's "never overwrites" is a check, not a
guarantee against a concurrent creator.

### `_patch_front_matter_line`: what it refuses

The patcher composes the front matter with PyYAML rather than matching a
regex — see the note in `contracts/backfill.md` on why a column-0 `key:` is
not a declaration test. It runs once per patched key, so the second call
composes text the first one rewrote, and it refuses rather than guessing
whenever the key is not a line it can own:

- a root flow mapping (`{status: active}`), where no key has a line at all;
- more than one `key:` line;
- a present key that is indented, or whose value spans lines;
- front matter that no longer composes. Rewriting `status: &st active` to
  `status: superseded` drops the anchor, leaving a `*st` elsewhere in the
  block undefined — the second compose then raises. That is a property of
  the document, so it is refused as an `isError` tool result naming the
  anchor, never allowed to escape the handler as a `-32603 internal error`
  with raw parser text in it.

Every one of these is refused before anything is staged, so the document is
left byte-identical and no `.tmp` is created.

## Broken pipe handling

A `BrokenPipeError` while writing to `stdout` means the client is already
gone — reported on stderr and the loop exits 0, a clean teardown, not a
crash.

An earlier version also redirected the stdout file descriptor to `os.devnull`
on the way out, guarding against the interpreter-shutdown flush raising the
same error a second time and printing a traceback. It was removed: measured
end to end against a real closed pipe, exit status, stdout and stderr are
identical with and without it, so it guarded nothing that could be observed.
`test_broken_pipe_on_the_real_process_stdout_exits_cleanly_without_a_traceback`
holds the guarantee that matters — exit 0, no traceback on stderr — and would
fail if a future change reintroduced the noise.
