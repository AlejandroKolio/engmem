# Contract: the section cache

Source: `src/engmem/cache.py`. A best-effort, per-document store of the tokenised section
index `scoring.py` builds, so a repeated search does not re-tokenise an unchanged document.

## Where it lives, and why not in the store

`~/.cache/engmem`, honouring `XDG_CACHE_HOME` — outside the store on purpose. A store is
git-tracked, so a derived file inside it is noise in every diff, and in a shared one it
conflicts on every pull. Nothing in the cache is authoritative: deleting the whole directory
costs one slower search.

## Identity, not invalidation

An entry is keyed by `(mtime_ns, size)` of the document it was built from
(`identity_for`). A changed file has a different key, so a stale entry is simply never
matched — there is no invalidation step to forget to call.

The key is not a hash, so it is not absolute, and the residual is wider than the one
`backfill.md` argues for the same key: that argument rests on a bounded window — one command,
with a human's prompt in it — while a cache entry lives until the file changes or is pruned.
`store` keeps one entry per resolved path and replaces it, so a false hit needs a key equal to
the one this path's entry currently holds, different content of the same size, and no search
in between (one would have stored the new key). A copy does not do it — `cp -p` carries the
source's mtime, which the cache has never held for that path, and correctly misses. A
timestamp **reused** does: a reproducible-build epoch, an archive extracted twice with the
same recorded stamps, `touch -r` against the file's own earlier reference.

One reuse needs no reuser. `st_mtime_ns` is nanoseconds of field *width*, not of resolution:
NTFS takes the value from a system clock that advances about every 15ms, and older filesystems
are coarser still. Two writes inside one tick are stamped identically, so a size-preserving
edit landing in the same tick as the `stat` an entry was keyed by is invisible to the key — and
that entry, being a legitimate hit, is what every later search scores and renders until the
file changes again. `backfill.md` names the same residual for the same key and can bound it:
the window it has to survive is a human's confirmation prompt, thousands of ticks wide. The
window here is the one tick containing `parse_document`'s `stat`, which is what keeps it narrow
— an author editing by hand cannot aim at it, while a program that writes a document and
searches in the same breath can. That is not hypothetical: the test that pins the rule below
(`test_an_edit_during_a_search_does_not_poison_the_section_cache`) wrote `oldword`/`newword` —
the same length — and so it passed on POSIX only because the two writes are some tens of
microseconds apart there, and failed on Windows, where they are not. Its edit now changes the
size, and it asserts the two identities differ before it relies on them differing;
`test_cache.py::test_editing_the_document_invalidates_the_cache_entry` was already written that
way.

A stronger key exists — `st_ctime_ns` alongside the same `stat()` (POSIX only; through 3.13,
Python reports the creation time in that field on Windows), or `sha256(doc.body)`, over bytes
`spine.parse_document` has already read. Neither has been adopted, and the choice is not
local: `identity_for` is shared with `apply_backfill`'s staleness guard, and the cache reads
its key from `doc.source_identity`, stamped in `parse_document`. A body hash stamped there
would close the tick residual above, and the cost is not the hashing — nor a re-read: the
other two readers of that stamp (`apply_backfill`, `mark_superseded`) are write-side guards
that compare it against a plain `stat`, and both already hold the document's text when they
compare, from the `read_document` each runs before its check. What they lack is
`parse_document`'s normalisation of that text — `read_text`'s universal-newline translation,
the front-matter split, `body.strip("\n")` — which each would have to reproduce exactly for
its hash to be comparable against a stamp taken there. `read_document` declines that
translation deliberately (backfill.md, "Line endings"), so a guard hashing the text it holds
would disagree with `doc.body` by every line ending in a CRLF document and refuse it as
changed. That is a tighter coupling to `spine` than the `stat` those guards compare today,
and it moves what they mean from "the same file" to "the same bytes". Every stored key stops
being comparable as well, so the change carries a `CACHE_FORMAT_VERSION` bump that retires
every entry.

`identity_for` returns `None` when the file cannot be `stat()`'d. Both `load` and `store`
treat that as "no cache": a document with no staleness key could never have its entry
invalidated, so it must not get one.

**The key must be taken by the read it describes.** `scoring._entries_for_doc` passes
`doc.source_identity`, stamped by `spine.parse_document` immediately before it read the body
the payload is built from — *not* a fresh `stat()` at search time. Stat'ing later files the
old body's sections under the edited file's identity, and that entry then never invalidates:
the search keeps returning the pre-edit document until the file changes again. See
`contracts/backfill.md`, "The proposal has to still describe the document", for the same
rule on the write side.

The entry's filename is the sha256 of the *resolved* document path, so a symlink and its
target share one entry — correctly, since the identity check keeps it honest. `resolve()`
collapses symlinks and relative segments and nothing else, so a hard link or a case-variant
name on a case-insensitive filesystem still gets its own entry: harmless duplication, since
each is correctly keyed. The stored key must match before a hit is trusted; the filename
alone proves nothing.

## Degrading, and how loudly

`load` never raises. Every damaged shape is a miss, and the caller recomputes. What differs
is whether the miss is announced, and the rule is that a warning must be worth one line of a
human's attention **per document, per search**:

| shape | outcome |
|---|---|
| entry absent | silent miss |
| `format_version` mismatch | silent miss — bumping the constant must retire old entries quietly, not report a store full of corruption |
| `key` absent, wrong type, or not matching | silent miss — see below |
| unreadable, not UTF-8, not JSON, not a JSON object, or a `payload` absent or null | warned miss |

The `key` row is a deliberate tie-break rather than an obvious call. A missing or
scalar `key` is damage, exactly as a missing `payload` is, and `scoring` re-stores
after every miss so any warning would fire once and self-heal. But that branch also carries
the ordinary, correct, once-per-edit mismatch, and key damage is only *partly* detectable:
`key: 5` can be recognised, while `key: [1, 2, 3]` is indistinguishable from a stale file.
Warning on the detectable slice alone would be arbitrary within one field, so the whole
branch stays silent.

One more miss on a cache entry is decided a layer up and does not appear above: a payload
that loads here but does not have the shape `scoring` expects. `scoring._entries_from_cache`
treats that as a warned miss — see `contracts/scoring.md`.

Warnings go to stderr only, for two reasons. The agent reads stdout (`ENGMEM-SPEC.md` §4),
so a slow-but-correct search must never look like a failed one. And under `engmem mcp`
(§5) stdout carries protocol frames only, where a single stray line corrupts a frame and
kills the client session — `_warn` runs on that path too. `tests/test_cache.py` asserts
`captured.out == ""` on every degraded path, including the silent ones.

## Writing

`store` writes a uuid-named `.tmp` sibling and renames it, so a concurrent reader never sees a
half-written entry. A failed write only warns — a search that already has its answer must
not fail over its cache.

"A failed write only warns" means a *write* failure. A payload `json.dumps` refuses is a
caller's bug, not a filesystem fault, and it propagates — swallowing it would make a
permanently uncacheable document indistinguishable from a full disk. It stages nothing on the
way out either: `json.dumps` is the argument to `write_text`, so it raises before the temp
file exists. The `BaseException` arm beside the `OSError` one is for the other case — an
asynchronous interruption landing between `write_text` and `replace`, where a uuid-named temp
file would otherwise survive forever.

A uuid is never reused, so no later write of the *same* document ever reclaims a leaked temp
file either — unlike the pid-keyed name it replaced (`{digest}.json.tmp{pid}`), where a later
store of that same document from that same process would have overwritten one. That contrast is
not vacuous: nothing in `src/engmem` writes concurrently, but the same process can still leak a
temp file while it keeps running, through the `OSError` `_discard` swallows, and a later
re-store of that document from that same process would then have overwritten it under the pid
scheme. Neither name is self-limiting across *successive runs*, though: a run interrupted
mid-write leaks the one file it was writing, and no later run's `store` reclaims it — a
different pid names the next run's temp file under the old scheme, a different uuid under the
new. `prune_orphans` (below) is what closes that gap, for both names — see the pre-upgrade note
there.

It does not `fsync`, where `staging.commit` does. That is a property of the writer, not of
"cache versus document": `staging` is the only writer of a file with no other copy, so a lost
write is data loss, while a cache entry's source of truth is still on disk and a lost write is
a rebuild. A torn entry is not a hazard either — `json.dumps` cannot emit a valid-JSON prefix,
so every truncation is a parse failure and therefore a warned miss.

The temp file is removed if the write fails, but that only runs the `except` arms above,
and a signal that kills the process between `write_text` and `replace` — `SIGTERM`,
`SIGKILL`, power loss — raises nothing there to catch. `engmem mcp` is a long-lived process
that receives exactly that signal on ordinary shutdown, not on some exotic path. So
`prune_orphans` also globs `*.json.*.tmp` and removes what it finds, on name alone: a temp
file does not address a document, so it cannot be compared against the current store the way
an entry is, and there is no age or ownership check — presence under this name is the whole
criterion. Without this, a leaked temp file would never be collected again and a run of
interrupted writes would grow the cache without bound.

Before the uuid name, `store` wrote `{digest}.json.tmp{pid}` (see above); that pattern is still
possible on a machine that ran an older `engmem` and never got a clean pruning pass since. Left
alone it would be uncollectable forever, for the same reason a leaked uuid-named file was
before this fix — so `prune_orphans` globs `*.json.tmp*` too and removes what it finds, by the
same name-alone rule. The two naming schemes never produce the same filename: a legacy name
ends in the pid's decimal digits, not `.tmp`, so it never matches `*.json.*.tmp`; a uuid name
never matches `*.json.tmp*` either, because its hex segment — the uuid after `.json.`, not the
sha256 digest before it — can never start with the letters "tmp", since hex digits are
`0-9a-f`. That is a fact about these two schemes' filenames, not about the patterns in general:
a deliberately crafted name like `a.json.tmp.x.tmp` matches both.

A match on the legacy glob is not categorically residue, though. `cache_root()` has no version
and no store component, and the pid-named scheme is what a pinned older install, a second
checkout, or another venv can still be running concurrently. During that transition window such
a process can own an in-flight `{digest}.json.tmp{pid}` file, and this glob unlinks it
mid-write — the same benign race accepted below for `*.json.*.tmp`: the writer's `replace` then
raises `FileNotFoundError`, its own `except OSError` arm turns that into one warning and one
recomputed entry, not a crash.

That same lack of an age check means `prune_orphans` cannot distinguish a leaked temp file
from one another `store` call is still writing, between `write_text` and `replace`. If pruning
lands in that window, the writer's `replace` then raises `FileNotFoundError` — an `OSError`,
so its own `except OSError` arm turns it into one warning and one recomputed entry, not a
crash. This is accepted, with no code guard: an mtime threshold would put a clock dependency
and a tunable into a module that has neither, and would still leave a window, and the loss it
would prevent is the same one already accepted below, where two stores share a cache root and
prune each other's entries. The residual cost is a misleading warning line — `"cannot write
cache entry ... continuing without it"` naming a race, not the write fault it usually means —
not a wrong answer: the entry is simply recomputed on the next miss.

`prune_orphans` deletes entries for documents no longer in **the store it was given**,
comparing filenames only — it never reads an entry. A deletion it cannot perform is ignored:
pruning is housekeeping, and failing it must not fail the search that triggered it. The same
applies to a temp file it cannot delete.

`cache_root()` has no store component, so two stores on one machine (`--store` /
`ENGMEM_HOME`) share one cache directory and each prunes the other's entries.

Answers stay correct, for two reasons and not one. Entries are addressed by the sha256 of the
resolved document path, so two stores can never read each other's entry unless they hold the
same file — without that, a colliding hit would be served and non-authoritativeness would not
save it. And a pruned entry is a plain miss the caller recomputes, because nothing here is
authoritative.

What is lost is warmth, on each switch: the run after a switch rebuilds what the other run
dropped. Ten searches against one store then one against the other pays that once for ten
hits, not once per search — but it does leave the first store cold from then on. Keying the
root per store would fix it and has not been done.

This used to happen inside a single store too: `role_coverage` narrows to non-draft,
non-superseded documents before building the index, and pruning ran on that narrowed list, so
every `engmem roles` dropped the entry of every draft and superseded document. Pruning is now
given the whole store while indexing takes whatever subset it was called with — see
`contracts/scoring.md`.
