# Contract: the section cache

Source: `src/engmem/cache.py`. A best-effort, per-document store of the tokenised section
index `scoring.py` builds, so a repeated search does not re-tokenise an unchanged document.
Nothing in it is authoritative: deleting the whole directory costs one slower search.

## Where it lives

`~/.cache/engmem`, honouring `XDG_CACHE_HOME`, outside the store on purpose: a store is
git-tracked, so a derived file inside it is noise in every diff, and in a shared store it
conflicts on every pull.

`cache_root()` has no store component, so two stores on one machine (`--store` /
`ENGMEM_HOME`) share one directory and each prunes the other's entries. Answers stay correct
for two reasons: entries are addressed by the sha256 of the resolved document path, so two
stores never read each other's entry unless they hold the same file, and a pruned entry is a
plain miss the caller recomputes. What is lost is warmth on each switch. Known gap: keying the
root per store would fix it and has not been done.

## Identity, not invalidation

An entry is keyed by `(mtime_ns, size)` of the document it was built from (`identity_for`). A
changed file has a different key, so a stale entry is never matched — there is no invalidation
step to forget to call. `identity_for` returns `None` when the file cannot be `stat()`'d, and
`load` and `store` both treat that as "no cache": a document with no staleness key could never
have its entry invalidated, so it must not get one.

**The key must be taken by the read it describes.** `scoring._entries_for_doc` passes
`doc.source_identity`, stamped by `spine.parse_document` immediately before it read the body
the payload is built from, not a fresh `stat()` at search time: stat'ing later files the old
body's sections under the edited file's identity, and that entry never invalidates. See
`contracts/backfill.md`, "The proposal has to still describe the document", for the same rule
on the write side; `test_an_edit_during_a_search_does_not_poison_the_section_cache` pins it
here.

The entry's filename is the sha256 of the resolved document path, so a symlink and its target
share one entry, kept honest by the identity check. `resolve()` collapses symlinks and relative
segments and nothing else, so a hard link or a case-variant name on a case-insensitive
filesystem gets its own entry: harmless duplication, each correctly keyed. The filename alone
proves nothing; the stored key must match before a hit is trusted.

### The residual

The key is not a hash. `store` keeps one entry per resolved path and replaces it, so a false
hit needs the key this path's entry currently holds, different content of the same size, and
no search in between. A copy does not do it (`cp -p` carries the source's mtime, which the
cache never held for that path); a reused timestamp does — a reproducible-build epoch, an
archive extracted twice, `touch -r` against the file's own earlier reference — and so does one
filesystem tick: `st_mtime_ns` is nanoseconds of field width, not of resolution (NTFS advances
about every 15ms), so a size-preserving edit landing in the same tick as the `stat` an entry
was keyed by is invisible to the key, and that entry is served until the file changes again.
`contracts/backfill.md` bounds the same residual for the same key to a human's confirmation
prompt, thousands of ticks wide; here the window is the one tick containing
`parse_document`'s `stat`, which an author editing by hand cannot aim at and a program that
writes a document and searches in the same breath can. The tests that rely on two identities
differing (`test_an_edit_during_a_search_does_not_poison_the_section_cache`,
`test_cache.py::test_editing_the_document_invalidates_the_cache_entry`) change the size and
assert the difference before relying on it, because a same-length edit passed on POSIX only
through the tens of microseconds between two writes there, and failed on Windows.

Rejected, so far: a stronger key. `st_ctime_ns` is POSIX-only (on Windows Python has reported
the creation time in that field). `sha256(doc.body)`, over bytes `parse_document` already
holds, would close the tick residual, but `identity_for` is shared with the write-side guards
(`apply_backfill`, `mark_superseded`), which compare a plain `stat` against the stamp. Each
holds its document's raw text from `staging.read_document`, which declines `read_text`'s
newline translation deliberately (`contracts/backfill.md`, "Line endings"), so a hash over
that text would disagree with `doc.body` on every CRLF document and refuse it as changed. The
change moves the guards from "the same file" to "the same bytes", couples them to `spine`'s
normalisation, and retires every stored key behind a `CACHE_FORMAT_VERSION` bump.

## Degrading, and how loudly

`load` never raises: every damaged shape is a miss, and the caller recomputes. What differs is
whether the miss is announced, and a warning must be worth one line of a human's attention
**per document, per search**:

| shape | outcome |
|---|---|
| entry absent | silent miss |
| `format_version` mismatch | silent miss — bumping the constant must retire old entries quietly, not report a store full of corruption |
| `key` absent, wrong type, or not matching | silent miss — see below |
| unreadable, not UTF-8, not JSON, not a JSON object, or a `payload` absent or null | warned miss |

The `key` row is a deliberate tie-break. A missing or scalar `key` is damage, exactly as a
missing `payload` is, but the branch also carries the ordinary once-per-edit mismatch, and key
damage is only partly detectable: `key: 5` can be recognised, `key: [1, 2, 3]` is
indistinguishable from a stale file. Warning on the detectable slice alone would be arbitrary
within one field, so the whole branch stays silent.

A payload that loads here but does not have the shape `scoring` expects is decided a layer up,
as a warned miss (`contracts/scoring.md`, "A shape mismatch is a miss, never a crash").

Warnings go to stderr only. The agent reads stdout (`ENGMEM-SPEC.md` §4), so a
slow-but-correct search must never look like a failed one, and under `engmem mcp` (§5) stdout
carries protocol frames only, where one stray line kills the client session — `_warn` runs on
that path too. `tests/test_cache.py` asserts `captured.out == ""` on every degraded path, the
silent ones included.

## Writing

`store` writes a uuid-named `.tmp` sibling and renames it, so a concurrent reader never sees a
half-written entry. A uuid, not `os.getpid()`: the pid separates processes but not threads, and
two threads sharing the name would interleave into one entry. The name is not hidden like
`staging.py`'s, which needs that to stay out of a document scan; this temp file is meant to be
visible to `prune_orphans`.

A failed write only warns — a search that already has its answer must not fail over its cache.
That means a *write* failure: a payload `json.dumps` refuses is a caller's bug and propagates,
because swallowing it would make a permanently uncacheable document indistinguishable from a
full disk. It stages nothing on the way out, since `json.dumps` is the argument to `write_text`
and raises before the temp file exists. The `BaseException` arm beside the `OSError` one
catches an asynchronous interruption (Ctrl-C) landing between `write_text` and `replace`,
where the temp file would otherwise survive forever.

No `fsync`, where `staging.commit` does: `staging` is the only writer of a file with no other
copy, so a lost write there is data loss, while a lost cache write is a rebuild. A torn entry is
not a hazard either — `json.dumps` cannot emit a valid-JSON prefix, so every truncation is a
parse failure and therefore a warned miss.

### Leaked temp files

A signal that kills the process between `write_text` and `replace` — `SIGTERM`, `SIGKILL`,
power loss — raises nothing to catch, and `engmem mcp` is a long-lived process that receives
exactly that signal on ordinary shutdown. A uuid is never reused, so no later `store` reclaims
the leaked file either. So `prune_orphans` globs `*.json.*.tmp` and removes what it finds on
name alone: a temp file does not address a document, so it cannot be compared against the
current store the way an entry is, and there is no age or ownership check. Without this,
interrupted writes would grow the cache without bound.

It also globs `*.json.tmp*`, the pid-named form `{digest}.json.tmp{pid}` an older `engmem`
wrote, which is otherwise uncollectable forever. The two schemes never produce the same
filename: a legacy name ends in decimal digits, not `.tmp`, so it never matches `*.json.*.tmp`;
a uuid name's hex segment after `.json.` cannot start with `tmp`, so it never matches
`*.json.tmp*`. That is a fact about these two schemes' names, not about the patterns —
`a.json.tmp.x.tmp` matches both.

Accepted, with no code guard: without an age check pruning cannot tell a leaked temp file from
one another `store` is still writing — this version's, or a pinned older install still on the
pid scheme, since `cache_root()` has no version. The writer's `replace` then raises
`FileNotFoundError`, an `OSError` its own arm turns into one warning and one recomputed entry,
not a crash. Rejected: an mtime threshold, which puts a clock dependency and a tunable into a
module that has neither, still leaves a window, and prevents only the loss already accepted
where two stores share a root. The residual cost is a misleading `cannot write cache entry ...
continuing without it` line naming a race, not a wrong answer.

`prune_orphans` deletes entries for documents no longer in the store it was given, comparing
filenames only — it never reads an entry. A deletion it cannot perform is ignored: pruning is
housekeeping, and failing it must not fail the search that triggered it. Pruning takes the
whole store while indexing takes whatever subset it was called with (`contracts/scoring.md`,
"Section index caching").
