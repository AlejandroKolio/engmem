# Contract: `engmem mcp` stdio server

Source: `src/engmem/mcp_server.py`. Author-approved addition beyond `ENGMEM-SPEC.md` §9
(see §5 `engmem mcp`): Claude Desktop cannot run shell commands, so the CLI alone is
unreachable from it. Codex launches the same server from `config.toml`; ChatGPT reaches it
through OpenAI's `tunnel-client`, which spawns this stdio process and carries the frames
itself.

stdio is the only transport, and that is decided, not deferred (2026-10-04). ChatGPT calls only
servers it can reach, and the alternative to the tunnel was an HTTP transport here, exposed
through ngrok or similar: a listening socket in a tool whose contract is "no server, no
network calls" (`tests/test_no_network.py`), with the write tools open to whoever holds the
URL. The tunnel keeps both outside engmem.

## `--read-only`: a server someone else can reach

A ChatGPT plan without Secure MCP Tunnel can only reach the server through a public bridge
(`supergateway` plus `ngrok` or `cloudflared`), and ChatGPT's developer mode connects such a URL
without authentication. Whoever learns the URL then holds every tool. `--read-only` limits the
damage of that leak to reading: `tools/list` returns the two search tools only, and a call to a
write tool is the `-32602 unknown tool` every unlisted name gets, with `--read-only` named in the
message so a caller can tell the cause. It is a dispatch-table swap (`_READ_ONLY_METHODS`) whose
two handlers both allow `_SEARCH_TOOL_NAMES` rather than deny the write tools, so a tool added
later is unreachable here until it is named there.

What it does not stop: a search still appends a row to `telemetry.jsonl`, because the row is the
measurement engmem exists for, so a URL holder can grow that file; and the documents themselves
are readable. A read-only search writes nothing else (owner decision D2, 2026-10-09): it keeps no
copy under `versions/` and prints no `cite as` line, because a client behind a bridge cannot save
a Reuse Log. `_handle_read_only_tools_call` passes `retain_versions=False` down to
`search_report.compose` (`contracts/gate1.md`, "Versioned citations"). Prompts are still served, on purpose: `/engmem` is useful for its search step,
and the save prompts reach their write calls and get the `--read-only` refusal by name. A
bridge stays a test setup; the tunnel is the way to keep a store private.

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
`staging.stage`, `staging.commit` or `staging.commit_new` (full disk, revoked permission). The
document's state is known — `os.replace` is the atomic last statement of `commit`, and
`commit_new` publishes in one `os.link` or `os.replace`, so the target is untouched — but `isError` is for what the model can restate and retry, and a host fault would invite a
retry that cannot succeed. The one `OSError` that is a tool result is `commit_new`'s
`FileExistsError`: the id is taken, which the model can act on by choosing another. The
boundary is otherwise `stage`/`commit`/`commit_new` only: an `OSError` re-parsing the
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
now ends with `telemetry.UNATTRIBUTED_MCP_NOTE`, appended by `search_report.compose` after
the telemetry write (`contracts/output.md`, "One composed result for both channels"), never
`isError`. Blank ids earn the
note too — they log `null`. The earlier decision never to append to a search result
protected `context_bytes`, which measures exactly the rendered text; the note is added after
`context_bytes` is computed, and `tests/test_mcp_server.py` / `tests/test_cli.py` pin that a
search with and without an id logs the same value.

## `repo` / `unscoped`: the CLI's scope, refused rather than widened (US-09)

Both search tools take the same optional scope as `engmem search --repo` / `--unscoped` /
`--all-repos` (`_SCOPE_PROPERTIES`, read by `_search_scope`) and hand it to
`search_report.compose`, so the scope rules, the `scope:` line and the telemetry row are the
CLI's (`contracts/scoring.md`, `contracts/output.md`). The `--read-only` server lists and
honours them unchanged: it filters tools, not arguments.

- `null` and `unscoped: false` mean "not passed": the whole store, as before.
- A `repo` that is not a string, an `unscoped` that is not a boolean, a blank `repo`, and
  `repo` together with `unscoped: true` are each `-32602` naming the argument, and no search
  runs and no row is written. A blank `repo` is refused, not read as absent the way a blank
  `session_id` is: an absent id costs one row's attribution, while a blank scope read as
  absent would silently widen the search to every repository — the reader would take a
  whole-store result for the scoped one it asked for. The CLI refuses `--repo ""` the same way
  (exit 2).

### `repos` and `all_repos` (US-10)

- `repos` is a list of names, the MCP form of a repeated `--repo`; `repo` keeps US-09's
  single string unchanged, and both together are joined into one union. A separate list
  argument, not `repo` widened to "string or list": a `type` union or `anyOf` is the schema
  shape MCP clients handle least consistently, while a plain string and a plain array of
  strings are understood by all of them, and a call written for US-09 still reads the same.
- `repos` that is not a list of strings, an empty `repos`, or a blank name inside it is
  `-32602`, for the reason a blank `repo` is: read as absent or dropped, it would search more,
  or other, repositories than the caller listed. The schema says `"minItems": 1` so a client
  that validates never sends the empty list, and the server still refuses it for a client that
  does not. Repeated and case-variant names collapse in
  `Scope`, so `["a", "A"]` is the scope `repo: "a"` is.
- `all_repos: true` is `--all-repos`; `false` and `null` mean "not passed". It, `unscoped: true`
  and any repository name are three different scopes: two at once is `-32602` naming both.
  The message names only the arguments that carry a value: a `repo: null` next to `repos` is
  absent, so it is not quoted as one of the clashing scopes.
- Nothing about a scope outlives its call. `_search_scope` reads it from that call's arguments
  and the server keeps no other state, so a call without a scope argument searches the whole
  store whatever the call before it asked for (AC-10.4).

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
and published over the target only after every handler-specific check passes — by
`os.replace` for the two tools that rewrite a document, by `commit_new` for the one that
creates it (below). The uuid is
what lets a leftover from a crashed run never block or collide with a later write to the same
document: `stage` opens with `O_EXCL`, so a fixed name would fail every retry until
hand-deleted. On any failure the staged file is discarded. `engmem_create_draft` is the only
path that creates a document, so it alone sets a new document's mode: it keeps the staged
`0600`, and `engmem_complete_draft` restores whatever it finds, so an MCP-authored document
stays `0600` for life.

The three tools' own rules:
- `engmem_create_draft` never overwrites: fails if the target exists, including when another
  creator takes the id while the call is running.
- `engmem_complete_draft` requires the existing file to be `status: draft` and the new
  content to be `status: active` with a matching `id` — it can never touch an active or
  superseded document. Given `expected_version`, it also requires the draft on disk to be
  that version ("Rewriting a document: the version read", below).
- `engmem_mark_superseded` requires the existing file to be `status: active` and patches only
  the `status`/`superseded_by` lines (`_patch_front_matter_line`), leaving the rest
  byte-for-byte; retyping the whole document to flip two fields risks silent drift in
  everything else.

### The mode a draft records (US-08)

A session's mode is the condition the Gate 1 count reads (`contracts/gate1.md`, "Modes"), so the
one tool that creates a session checks it, and the one that finishes a session keeps it.

**`engmem_create_draft` takes the configured mode and no other** (`_mode_refusal`). The content's
front matter must state `mode`, and it must equal the mode saved now (`settings.effective_mode`,
read on every call, `daily` when nothing is saved). A missing `mode`, an unknown word, or the
other mode is refused, and the refusal names the configured mode and `engmem mode set` — that is
also how a client that never read the mode learns it, before anything is written or searched. A
missing `mode` is refused rather than accepted as legacy: a daily user's session without one
would otherwise enter the experiment as a legacy story. Its refusal also says that an installed
template or skill may predate modes and names `engmem install --agent <agent>`: a template copied
out before US-08 composes drafts without `mode`, and without the hint every draft it produces is
refused with no pointer to the cause. A `research` draft must also carry the
baseline before the first search: a `## Pre-reg` section, or `baseline_unavailable: <reason>` in
the front matter (AC-08.2, AC-08.5). A `daily` draft needs neither. The success text ends with
the recorded mode, after the version sentence, so the line a client parses for the version is
unchanged.

When the mode file cannot be read, a `daily` draft is still created and a `research` draft is
refused with the file's error. A daily record can never be a wrong observation in the
experiment, so the broken file blocks only the mode it could be hiding (AC-08.1: daily work is
never blocked).

**The shell path is unguarded.** A shell agent writes `sessions/<id>.md` itself, and nothing
checks its `mode` at write time. The count guards that path on its own side: the template's
placeholder `mode: <daily|research from step 0>`, copied as is, reads as an unrecognized mode,
and a `mode: research` document with neither a Pre-reg nor `baseline_unavailable` is excluded as
a missing baseline (`contracts/gate1.md`, "Modes"). A wrongly chosen but valid word — `daily`
written by a research user, or the reverse with a Pre-reg — is not detectable afterwards.

**`engmem_complete_draft` keeps the draft's condition** (`_refuse_a_changed_observation`). The
content's `mode` must equal the draft's — both absent for a draft written before modes existed —
and a draft's `baseline_unavailable` may not be dropped: a baseline cannot be supplied after the
search, and dropping the field would move an incomplete observation into the valid group. A
switch of the saved mode between the two calls does not matter here; the draft already
recorded its mode (AC-08.4). Adding `baseline_unavailable` at completion is allowed, since it only
moves the session out of the valid group.

**The `engmem` prompt names the mode on its first line** (`_mode_line`), read on every
`prompts/get`, because a client with no shell has no `engmem mode show` to run and the start
template's step 0 must be answerable before the Pre-reg it decides. Only the start prompt gets
the line; the save prompts copy the mode from the draft. An unreadable file reads there as
`daily` with the error quoted, which is what the template tells a shell agent to do. The mode
is not in `initialize`'s result: a server outlives many sessions, and a value read once at
launch would ignore a later `engmem mode set`.

### `engmem_complete_draft`: Reuse Log rows — refuse, then warn

The Reuse Log is the one section the Gate 1 count reads mechanically (`ENGMEM-SPEC.md` §11,
`contracts/gate1.md`). Its rows are checked in two tiers, split by what decides the verdict.

1. **Refuse — defects visible in the row itself.** `_reuse_log_rejections` runs on the staged
   `doc` after the two transition checks and before `commit`. It walks every
   `canonical == "reuse"` section with the count's own parser (`gate1.reuse_log_rows`) and
   reports, per row, each defect the count would exclude it for without looking at any other
   document: no quoted span in `taken` (`gate1.quotes_in`, the same `QUOTE_RE`), a missing or
   empty classification cell, or one outside `gate1.VALID_CLASSIFICATIONS`
   (`gate1.classification_of`), or a `prior-doc` cell `<id>@<version>` whose version is not 16
   lowercase hex digits (`versions.split_reference`, `versions.is_version` — the same reading
   `gate1.cited_reference` makes when the id does not resolve). One `_ToolError` carries one
   line per defect with the row quoted verbatim, so a row with both defects is fixed in one
   turn, not two. The one store read in this tier spares a cell that names an existing id
   exactly, the rule `gate1.cited_reference` applies, so an id spelled with `@` is not refused:
   `load_store` runs only when a row has such a malformed suffix. Whether a well-formed version
   was retained depends on the store, so that is tier 2; and a row with no version at all is a legacy row the count still reads, so it is
   never refused here. The three
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
     `version_unavailable` is reported the same way, with `row.version_problem` as its reason
     (the cited version has no intact retained copy, so the quote stays unverified).
     `cited_superseded` and `verdicts.conflicts` are NOT reported: both are a judgement call
     for the reviewing human, and the store-wide tools surface them at the next run.
     Rows `gate1.checked_against_current_text` names — no version, quotes compared with the
     current text — get one `note:` line, not a warning: they count exactly as before US-13,
     and the line only says the evidence can change under them and where the reference comes
     from (the search result's `cite as` line).
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
fail" shape is the one `search_report.compose` already uses for its `telemetry not recorded`
note; here the callee's own failure is caught explicitly because `gate1.evaluate` has no
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

`engmem_complete_draft` has the same window between confirming the file is a draft and
committing its replacement: another writer can activate the draft meanwhile, and the new
content still parses as `active`, so nothing else notices. It runs the same check,
`_refuse_if_changed_since_read`, just before the commit, and the staged file is discarded on
refusal. Between engmem writers both tools now hold the document's write lock across the
whole span (next section), so this check is what remains against a writer that does not
take it — a hand edit, a direct file write, `engmem backfill`.

### Rewriting a document: the version read

Before this, `engmem_complete_draft` checked `status: draft` and then `os.replace`d the
staged file. Two failures followed from that shape. Two clients completing one draft could
both pass the check and both be told they succeeded; the later rename silently replaced the
earlier document. And the check said nothing about *which* draft: a draft another agent had
changed since this client read it was still a draft, so the client's stale copy replaced the
newer text. `engmem_mark_superseded` had the first failure too — two supersedes of one
document, both seeing `status: active`, the later one dropping the earlier's successor.

**Version identity.** A document's version is `staging.version_of` of its bytes on disk: the
first 16 hex digits of their SHA-256, BOM and line endings included. It is derived, never
stored, so every existing document already has one and nothing is migrated. It changes with
any byte, including an edit that keeps `status: draft` and the file's size, which the
`(mtime_ns, size)` identity `_refuse_if_changed_since_read` uses cannot promise on a
filesystem with coarse timestamps. Sixty-four bits make an accidental match negligible; it
is not a security boundary, since any writer that can change the file can do worse than
forge a version. It is short so a model copies it reliably, and a mistyped one is a refusal,
never a wrong write.

**How the client learns and passes it.** `engmem_create_draft` names the version of the
bytes it wrote in its success text. In a shell-less runtime the draft the client knows *is*
the content it passed to that call (`templates/engmem.save.md`), so that version is the one
it read. `engmem_complete_draft` takes it as `expected_version`; under the lock, after the
status check, `_refuse_if_not_the_version_read` reads the file and refuses when its version
differs (surrounding whitespace ignored). The refusal says nothing was written, gives the
current version, tells the client to re-read, and carries the current draft's text — that
is how a client with no file access gets the newer version. No read tool was added for it:
the refusal is the one place the text is needed, and a read tool would be new surface for
`--read-only` to decide about. The status check runs first, so a document that is already
`active` or `superseded` is refused as not a draft whatever version is named.

`expected_version` is optional. Required, it would turn every call from a client or an
installed template that predates it into a `-32602`, and a draft written by hand has no
version the client was ever told. Omitted, the guarantee is weaker and stated in the tool's
description and schema: the lock still lets only one completion of a draft through, but a
draft changed while staying a draft is overwritten. Every template passes it. This is the
`session_id` shape (above): optional to the schema, asked for in every wording. A
non-string value is a `-32602`.

**Closing the check-to-replace window.** `staging.document_lock` makes a
`.<target>.write-lock` directory with `os.mkdir`, exclusive on every filesystem, and both
rewriting tools hold it from their existence check to their `os.replace`
(`_locked` wraps it). A second engmem writer waits for it, polling for up to
`_LOCK_WAIT_SECONDS` (2.0), then does its own checks against what the first one left: a
second completion finds `status: active` and is refused as not a draft, a second supersede
finds `status: superseded`. On Windows a `PermissionError` from the `mkdir` is a busy lock,
as for `commit_new`. The lock is released in a `finally`; a failed release is ignored, since
by then the write has happened. A lock still held after the wait is refused as an `isError`
that says nothing was written and names the lock to remove if no engmem process is running.
It is never broken automatically: deciding a lock is stale by its age races with a second
breaker, which can remove a lock a live writer has just taken, and a wrongly broken lock is
the silent replace this exists to prevent. A lock left by a killed process costs one
document a manual `rmdir`. The name does not end in `.md`, so no scan sees it.

A lock and not a rename-based compare-and-swap (move the document aside, check it, publish
the new one): between the move and the publish the document is absent from the store, and a
process killed there hides it. The lock's worst case is a refused write with the document
intact.

Limits, recorded rather than closed:

- The guarantee holds between writers that take the lock: `engmem_complete_draft` and
  `engmem_mark_superseded`. `engmem backfill` rewrites the same files with
  `staging.commit` after its own identity check and does not take it yet; it has the short
  window `apply_backfill` documents (`contracts/backfill.md`). Taking the lock there is the
  follow-up. Hand edits and the shell templates' direct writes never reach engmem; against
  them `_refuse_if_changed_since_read` narrows the window and does not close it.
- `engmem_create_draft` does not take the lock and needs none: its publish step cannot
  replace anything, and both rewriting tools refuse a document that does not exist.
- The lock relies on `mkdir` being atomic, which holds on local filesystems; a store on a
  network filesystem with weaker semantics is not a supported setup for concurrent writers.
- `engmem_mark_superseded` takes no version. It rebuilds the document from the bytes it
  reads under the lock, so there is no client copy to go stale; the only client input is
  the two ids.

### Creating a document: exclusive, not checked

`engmem_create_draft` used to check `exists()` and then `os.replace` the staged file over the
target. Two clients creating one id could both pass the check, both be told they succeeded,
and the later rename silently replaced the earlier writer's draft. The check still runs first,
so an id that is plainly taken is refused before anything is staged, but it is no longer what
the guarantee rests on. The publish step is `staging.commit_new`, which cannot replace
anything:

- **`os.link(staged, target)`** is the primary path. It fails with `FileExistsError` on any
  existing entry, a dangling symlink included, and it is atomic: the name appears already
  holding the whole staged, fsynced, parsed document, so no reader ever sees a partial one.
  The staged name is then discarded; if that fails, the leftover is a second name for the
  same bytes, and inert, because no scan loads a `.tmp`. The new document shares the staged file's inode,
  so it keeps the staged `0600`. POSIX filesystems, APFS and NTFS all support it, which
  covers the CI matrix.
- **A `mkdir` lock** is the fallback when `os.link` fails in a way that means the
  filesystem has no hard links (FAT, exFAT) or a sandbox forbids them: `EPERM`, `ENOTSUP`,
  `EOPNOTSUPP` or `ENOSYS`, and on Windows `ERROR_INVALID_FUNCTION` or `ERROR_NOT_SUPPORTED`
  (`_hard_links_unavailable`). Every other `OSError` from the link — `EIO`, `ENOSPC`,
  `EACCES` — propagates as the `-32603` above. Falling back on those would be a data-loss
  bug, not a retry: the lock excludes other lock holders only, so a creator that fell back
  on a transient error could check, find nothing, and replace a document another creator
  had just linked. `os.mkdir` is exclusive on every filesystem, so creating
  `.<target>.create-lock` serialises the check (`os.path.lexists`) and the `os.replace`
  between engmem creators; the lock is removed in a `finally`. On Windows a `PermissionError`
  from that `mkdir` is also a busy lock: NTFS answers access denied for a directory name still
  pending delete, which is a lock another creator has just released. A failed release is ignored,
  because by then the document is published and an error would report a create that happened
  as one that did not. A lock left by a killed process fails closed: later creates of that one
  id are refused with a message naming the lock to remove. Neither file name ends in `.md`,
  so `load_store` and `stray_documents` never see one.

A `FileExistsError` from either path becomes the same `isError` refusal the up-front check
gives, naming the id and saying to choose another; the staged file is discarded. It is not a
`-32603`: a taken id is the model's to act on.

An interrupted create never leaves a document behind. Before the publish step, all that
exists is the staged `.tmp`, which no scan loads; the publish step is a single link or rename;
and an exception or Ctrl-C anywhere in between discards the staged file and propagates, so no
success text is produced.

Limits, recorded rather than closed:

- The guarantee covers writers that go through `commit_new`, which today is only
  `engmem_create_draft`. The `/engmem` template tells a runtime that can write files directly
  to write `sessions/<id>.md` itself, and only if no file by that name exists yet; that is an
  instruction to the agent, a check at best. The write never reaches engmem, and nothing here
  can make it exclusive. A shell-capable create command (`engmem create-draft` or similar) would
  extend the guarantee to that path; it is a new CLI surface and not part of this change.
- The lock path is not exclusive against the link path. The case left open is two creators
  in one directory where one is refused hard links (a sandbox that forbids `link`, say) and
  the other is not — an agent in a sandbox and an unsandboxed host sharing one store. The
  refused creator checks, finds nothing, and its `os.replace` can land over the document the
  other creator linked a moment before; both are told they succeeded. This holds for as long
  as that setup does, not for one unlucky window. Closing it would mean taking the lock on
  the link path too, which costs every create a lock that a killed process leaves behind;
  not done while no supported setup mixes the two.
- `engmem_complete_draft` and `engmem_mark_superseded` still rewrite with `os.replace`;
  between engmem writers their read-to-commit window is closed by the document's write lock,
  against any other writer only narrowed ("Rewriting a document: the version read").

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
