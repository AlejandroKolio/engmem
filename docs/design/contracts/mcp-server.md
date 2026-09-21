# Contract: `engmem mcp` stdio server

Source: `src/engmem/mcp_server.py`. Author-approved addition beyond `ENGMEM-SPEC.md` §11
(see §5 `engmem mcp`): Claude Desktop cannot run shell commands, so the CLI alone is
unreachable from it.

## Transport and the stdout-purity rule

Newline-delimited JSON-RPC 2.0 on stdio. `serve()` is the only writer of `stdout`, through
`_write`, whose `json.dumps` keeps a multi-line tool result on one line. `stdout` carries the
protocol and nothing else; diagnostics go to `sys.stderr`. This is the opposite of the CLI,
where `runtime.fail` writes to both streams — a dual-stream write on this path corrupts the
frame stream.

Both directions are UTF-8. Outbound needs nothing: `json.dumps` defaults to
`ensure_ascii=True`, so every frame is ASCII whatever encoding `stdout` has. Inbound is pinned
by `_use_utf8_transport` (`utf-8`/`surrogateescape`) because `sys.stdin`'s error handler
depends on the host locale, and under `strict` one undecodable byte raised out of the read
loop and ended the session for every request still coming. Under `surrogateescape` the byte
becomes a surrogate the envelope check below refuses by name, identically on every host.

## Protocol-level fault vs. tool-level failure

A malformed request — missing or wrong-typed required field, unknown method or tool, wrong
`jsonrpc` — is a JSON-RPC error (`_ProtocolError`, codes per the 2.0 spec). A request that
conforms to its schema but fails at runtime — a missing store, a write tool's refusal — is a
tool result with `isError: true` (`_ToolError`). Conflating them either shows a normal "store
not found" as a protocol fault the client cannot render, or coerces a malformed request (a
non-string `session_id`) into something that looks like it worked.

One runtime fault is deliberately a `-32603` and not a tool result: an `OSError` out of
`staging.stage` or `staging.commit` (full disk, revoked permission). The document's state is
known — `os.replace` is the atomic last statement of `commit`, so the target is untouched —
but `isError` is for what the model can restate and retry, and a host fault would invite a
retry that cannot succeed. The boundary is `stage`/`commit` only: an `OSError` re-parsing the
staged file is a tool result. Either way the staged file is discarded; `discard` is
best-effort, so what actually keeps a leftover out of the store is its `.tmp` suffix —
`load_store` and `stray_documents` select on `.md`.

## Envelope checks before any handler runs

- **An unpaired surrogate anywhere in the message** (`_has_unpaired_surrogate`) is `-32600`
  with a null `id`. `\ud800` is valid JSON syntax and `json.loads` accepts it, but the `str`
  cannot be encoded as UTF-8, and each consumer downstream fails differently: `content.encode`
  in a write tool raises into a `-32603`, `json.dumps` echoes the escape into a frame a strict
  client rejects. One check on the whole decoded message covers `id`, `method`, every argument
  and every key, so a field added later cannot miss it. The `id` is null because the `id` may
  be the offending string. The walk is iterative: the decoder accepted this nesting depth, and
  a recursive walk could raise `RecursionError` where it did not. A valid surrogate *pair* is
  one astral code point and passes.
- **A non-conforming `id`** is `-32600` with a null `id`. Booleans are excluded by name:
  `isinstance(True, int)` holds, so a plain `(str, int, float)` check would echo `id: true`
  back.

An id-less message whose method is not in `_NOTIFICATION_METHODS` is unanswerable, so its
handler is skipped rather than doing work whose result is discarded. `_dispatch`'s `except
Exception` is the last-resort guard: one handler bug reports `-32603` instead of killing the
loop for every request still coming.

## `session_id`: optional to the schema, asked for in every wording

`session_id` is the only field joining a retrieval to the session document that later cites
it (`contracts/gate1.md`); a row without one is invisible to the Gate 1 analysis. It stays out
of `inputSchema["required"]` for both search tools because a search before any draft exists
is legitimate, and requiring it would turn that case into a protocol error instead of an
unattributed row. Absent, blank and whitespace-only all log `null` (`cli._session_id`'s rule,
mirrored so both channels write the same key); a non-string is `-32602`, not a stringified
value.

The wording carries the requirement instead, because at call time the schema is what the
model reads while the template's instruction has scrolled out of the transcript:
`_SESSION_ID_SCHEMA["description"]` is imperative and names the consequence of omitting it
(the rejected wording opened with "Optional:" and produced a store with no attributed row);
`TOOL_DESCRIPTION` and `ROLE_TOOL_DESCRIPTION` each ask once; `_handle_create_draft`'s success
text names the id and what to do with it, since nothing else carries the id from the call
that produced it to the call that needs it.

Recorded reversal. Two measurements a week apart (2026-09-11 and 2026-09-18: 0 of 33, then
2 of 44 rows attributed) showed the three wordings were not enough, so a search without an id
now ends with `telemetry.UNATTRIBUTED_MCP_NOTE`, appended by `_run_search_for_tool` and
`_run_role_search_for_tool` after the telemetry write, never `isError`. Blank ids earn the
note too — they log `null`. The earlier decision never to append to a search result
protected `context_bytes`, which measures exactly the rendered text; the note is added after
`context_bytes` is computed, and `tests/test_mcp_server.py` / `tests/test_cli.py` pin that a
search with and without an id logs the same value.

## Write tools: security contract

Every write target MUST resolve inside the resolved store's `sessions/`. Order matters:

1. `doc_id` is validated by `spine.validate_doc_id` before it touches a `Path` — that check
   alone rejects `..`, an absolute path, a NUL byte and every reserved device name.
2. `_resolve_sessions_dir` resolves both the store root and `sessions/` and rejects a
   mismatch, which catches `sessions/` itself being a symlink out of the store; a string
   comparison would not.
3. `_resolve_write_target` refuses to write through an existing target that is itself a
   symlink, checked before `Path.resolve()` would follow it and compare the wrong location.

Points 2 and 3 are `engmem backfill`'s rules too (`cli._sessions_is_contained`,
`apply_backfill`; `contracts/backfill.md`, "What the replaced file inherits"). Point 1 is
MCP-only: these tools take a `doc_id` from a caller, `backfill` writes only where `load_store`
already found a file.

Every write is staged through `engmem/staging.py` (shared with `backfill`) to a hidden
sibling `.<target>.<hex>.tmp`, parsed with the same `spine.parse_document` `load_store` uses,
and `os.replace`d over the target only after every handler-specific check passes. The uuid is
what lets a leftover from a crashed run never block or collide with a later write to the same
document: `stage` opens with `O_EXCL`, so a fixed name would fail every retry until
hand-deleted. On any failure the staged file is discarded. `engmem_create_draft` is the only
path that creates a document, so it alone sets a new document's mode: it keeps the staged
`0600`, and `engmem_complete_draft` restores whatever it finds, so an MCP-authored document
stays `0600` for life.

The three tools' own rules:
- `engmem_create_draft` never overwrites: fails if the target exists.
- `engmem_complete_draft` requires the existing file to be `status: draft` and the new
  content to be `status: active` with a matching `id` — it can never touch an active or
  superseded document.
- `engmem_mark_superseded` requires the existing file to be `status: active` and patches only
  the `status`/`superseded_by` lines (`_patch_front_matter_line`), leaving the rest
  byte-for-byte; retyping the whole document to flip two fields risks silent drift in
  everything else.

### `engmem_complete_draft`: Reuse Log rows — refuse, then warn

The Reuse Log is the one section the Gate 1 count reads mechanically (`ENGMEM-SPEC.md` §11,
`contracts/gate1.md`). Its rows are checked in two tiers, split by what decides the verdict.

1. **Refuse — defects visible in the row itself.** `_reuse_log_rejections` runs on the staged
   `doc` after the two transition checks and before `commit`. It walks every
   `canonical == "reuse"` section with the count's own parser (`gate1.reuse_log_rows`) and
   reports, per row, each defect the count would exclude it for without looking at any other
   document: no quoted span in `taken` (`gate1.quotes_in`, the same `QUOTE_RE`), a missing or
   empty classification cell, or one outside `gate1.VALID_CLASSIFICATIONS`
   (`gate1.classification_of`). One `_ToolError` carries one line per defect with the row
   quoted verbatim, so a row with both defects is fixed in one turn, not two. The three
   helpers are public in `gate1` for this reason only: the refusal must agree with the count
   to the character, and a second regex here would be the drift `contracts/gate1.md`
   describes, reintroduced between the gate and the write path. This tier walks every `reuse`
   section while `gate1.evaluate` reads the first, so the rows it refuses are a superset of
   the rows the count verdicts. It never calls `gate1.evaluate`, so it cannot fail for a
   reason outside the content.

   Known gap: a `reuse` section with neither rows nor the `Prior docs used: none.` line
   passes, and nothing downstream catches it — `gate1.evaluate` produces no row, no
   none-report and no conflict for it, and `gate1_audit` counts the section as present.
   Recorded so it is not mistaken for handled.

2. **Warn — defects that depend on the rest of the store.** `_citation_notes` runs after the
   commit and appends zero or more lines to the success text; the result stays
   `isError: false` and the write is never undone. At most one of two checks fires:

   - **No Reuse Log section** (`split_sections(doc.body)` on the committed `doc`, no re-read):
     a present-tense warning that the document is already in
     `gate1_audit.active_missing_reuse`, then return. Present tense because the `status` on
     disk already reads `active`, and a note describing a future condition invites deferring
     the fix. Returning there also keeps a `gate1.evaluate` failure from adding a `citation
     check did not run` note about a check that had nothing to check.
   - **Store-dependent citation problems.** `gate1.evaluate(store)` — the same verdicts
     `tools/verify_citations.py` and `tools/gate1_report.py` use — filtered to rows whose
     `citing.id` is the document just completed. Reported: `cited_missing` (with the fix:
     `prior-doc` wants a document id, not a filename) and `quote_not_found`, each carrying
     `row.source` so a document with several rows names the right one. `no_quote` is
     impossible here by construction: tier 1 refused it with the same parser and regex.
     `cited_superseded` and `verdicts.conflicts` are NOT reported: both are a judgement call
     for the reviewing human, and the store-wide tools surface them at the next run.
     `gate1.evaluate` can raise (`_repos` and `_line_number` call `read_text` unguarded on
     every document, not only this one); that is caught and degraded to `note: citation check
     did not run (<exc>)`, because a read-only diagnostic must not turn a successful commit
     into a `-32603` the caller cannot undo by retrying — the retry trips "not 'draft'".

Why the line falls where it does — a recorded reversal. Before the split every Reuse Log
defect was a warning, on the argument that a defect in content the author chose to publish is
a finding for the human, not a structural fault of the transition. Two measurements a week
apart (2026-09-11 and 2026-09-18) showed the cost: rows kept arriving without a quote or with
a misspelt classification, the warning was read, the document stayed active, and the count
did not move. A defect fixable from the row alone, in the same turn, is a typo the tool can
name, and naming it after the commit names it to a session that has moved on. The
store-dependent defects keep the old argument: whether a cited id resolves depends on
documents the author may not have open (the cited document may be about to land), and
refusing over them would block a save on state the tool cannot prove wrong. The "note, don't
fail" shape is the one `_run_search_for_tool`'s `telemetry not recorded` note already uses;
here the callee's own failure is caught explicitly because `gate1.evaluate` has no
error-return shape.

### `engmem_mark_superseded`: two reads of one document

This is the only write tool that rebuilds a file from what is on disk, and it needs the
document twice: the validating `parse_document` (which strips the BOM and translates line
endings) to confirm `status: active`, and `staging.read_document` for the raw bytes the patch
is spliced into. The validating parse runs first and stats before it reads, so its
`Doc.source_identity` covers the raw re-read as well; it is compared against a fresh
`identity_for(target)` after that read, and a writer landing anywhere in the span is refused.
Without this the failure is silent: the replacing document still carries `status: active`, so
the staged re-parse passes and the newer version is overwritten by the stale copy. It is
`apply_backfill`'s rule (`contracts/backfill.md`, "The proposal has to still describe the
document") applied to a short window — a short window is not a closed one.

The raw re-read's own failures are properties of the document, refused as `isError` saying
nothing was written: unreadable (`OSError`), or no longer UTF-8 because a writer replaced the
bytes between the reads (`UnicodeDecodeError`, a `ValueError`). Neither may escape as a
`-32603` with codec text in it.

Not closed by this: `engmem_create_draft`'s `exists()`-then-`os.replace` window. Closing it
needs an exclusive-create commit path in `staging.py`; until then "never overwrites" is a
check, not a guarantee against a concurrent creator.

### `_patch_front_matter_line`: what it refuses

The patcher composes the front matter with PyYAML rather than matching a regex
(`contracts/backfill.md`, "Writing into front matter the author already started": a column-0
`key:` is not a declaration test). It runs once per patched key, so the second call composes
what the first rewrote, and it refuses whenever the key is not a line it can own:

- a root flow mapping (`{status: active}`), where no key has a line;
- more than one `key:` line;
- a present key that is indented, or whose value spans lines;
- front matter that no longer composes — rewriting `status: &st active` drops the anchor and
  leaves a `*st` elsewhere undefined, so the second compose raises. A property of the
  document, refused as an `isError` naming the anchor, never a `-32603` with parser text in
  it.

Every refusal happens before anything is staged: the document stays byte-identical and no
`.tmp` is created.

## Broken pipe handling

A `BrokenPipeError` on `stdout` means the client is gone: reported on stderr, exit 0, a clean
teardown. Catching it is not enough — the failed flush leaves the frame in the buffer, the
interpreter flushes `sys.stdout` once more at shutdown, that flush hits the same dead pipe,
and CPython turns a returned 0 into exit status 120. `_mute_broken_stdout()` points the
stream's descriptor at `os.devnull` first so the shutdown flush has somewhere harmless to
land. Best effort by design: a stream with no descriptor is left alone, and a failed redirect
must not replace a clean teardown with a crash. The redirect outlives `serve()`, which is safe
only because the reader of that descriptor is already gone.

Recorded reversal: this redirect was once removed on a measurement showing no difference with
and without it. The measurement ran under `PYTHONUNBUFFERED=1`, where nothing is left to flush
and the path is never entered; CI, block-buffered, exited 120. Anything measuring this path
must spawn the child without `PYTHONUNBUFFERED`, which
`test_a_block_buffered_stdout_leaves_nothing_for_the_interpreter_shutdown_flush` does;
`test_broken_pipe_on_the_real_process_stdout_exits_cleanly_without_a_traceback` holds the
guarantee under whatever environment the suite inherits.
