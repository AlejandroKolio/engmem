# Contract: the section cache

Source: `src/engmem/cache.py`. A best-effort, per-document store of the tokenised section
index `scoring.py` builds, so a repeated search does not re-tokenise a document whose body has
not changed.
Nothing in it is authoritative: deleting the whole directory costs one slower search.

## Where it lives

`~/.cache/engmem`, honouring `XDG_CACHE_HOME`, outside the store on purpose: a store is
git-tracked, so a derived file inside it is noise in every diff, and in a shared store it
conflicts on every pull.

`cache_root()` has no store component, so two stores on one machine (`--store` /
`ENGMEM_HOME`) share one directory and each prunes the other's entries. Answers stay correct
because an entry is served only for the exact body it was built from ("Keyed by content"): two
stores that reach one entry — the same file through a symlink, or one store path whose
directory was swapped for another store's with names, sizes and mtimes kept — get one store's
sections only when they hold that store's text. A pruned entry is a plain miss the caller
recomputes. What is lost is warmth on each switch. Known gap: keying the root per store would
fix it and has not been done.

A draft has an entry once it is a candidate for the `unsaved drafts also match` line
(`contracts/scoring.md`, "Drafts that also match"). Before that line only published documents
were indexed. A draft is edited often; each edit misses on the content key and rewrites its
entry, the same as an edited published document.

## Keyed by content

An entry carries `body_sha256`, the sha256 of the `doc.body` its payload was built from
(`content_key`), and `load` serves it only when that equals the hash of the body the current
search parsed. The payload is a pure function of `doc.body` and of the code that splits and
tokenises it, and the code is covered by the format version below, so a matching hash is exactly
the condition under which the cached payload equals the one the search would compute. There is
no invalidation step to forget, and no timestamp is consulted.

**The hash is of the text the payload is built from.** `scoring._entries_for_doc` hashes
`doc.body`, the very string it hands to `split_sections`; it never re-reads the file. One read
in `spine.parse_document` yields both the text and, through it, the key, so there is no window
between hashing and parsing. Hashing the file as it is at search time instead would file an
edited file's key over the old body's sections, and that entry would then be served for the new
text; `test_an_edit_during_a_search_does_not_poison_the_section_cache` pins it.

**The body, not the file's bytes.** The payload depends on the body only, so a front-matter
edit needs no re-tokenising, and the body is already in memory. A hash of the raw bytes would
need a second read, or a change to how `parse_document` reads (`read_text` translates line
endings), for no gain in correctness. `staging.version_of` is not reused: it is a client-facing
version of the raw bytes, truncated to 16 hex digits so a model copies it reliably. Nothing
copies this key, and the full digest costs the same.

**Why not `(mtime_ns, size)`.** Formats up to 4 keyed an entry by the file's `(mtime_ns,
size)`. A size-preserving edit under a kept mtime was served stale until the file changed
again: a restore from backup, an archive extracted twice, `touch -r`, a store directory
swapped for another at one path, or simply an edit inside one filesystem tick (NTFS advances
about every 15ms). That was documented as an accepted residual; US-21 (owner decision D1,
2026-10-11) closed it. No `stat` pre-check was kept in front of the hash: a matching
`(mtime_ns, size)` proves nothing, so it could never decide a hit, and a mismatch would only
skip a hash that costs under 2ms for 800 documents.

The entry's filename is the sha256 of the resolved document path, so a symlink and its target
share one entry. `resolve()` collapses symlinks and relative segments and nothing else, so a
hard link or a case-variant name on a case-insensitive filesystem gets its own entry: harmless
duplication. The filename decides where an entry lives, never whether it is served.

### What it costs

The search already reads every document on every call (`load_store`), so the hash adds no
I/O; its cost is CPU. Measured 2026-10-11 on an Apple-silicon Mac, Python 3.13, with stores of
synthetic five-section documents of about 5.4KB each, through the CLI (`engmem search
"WidgetCache eviction"`, a fresh process per run; cold = cache directory removed before the
run, warm = the run right after it; two interleaved rounds of 11 runs per build, the mean of the
two medians):

| documents | store size | before (identity key) warm / cold | after (content key) warm / cold |
|---|---|---|---|
| 10 | 54KB | 154 / 167 ms | 155 / 168 ms |
| 100 | 541KB | 192 / 314 ms | 194 / 316 ms |
| 800 | 4.3MB | 497 / 1461 ms | 500 / 1464 ms |

In process, `content_key` over every body measures 0.02 / 0.22 / 1.7 ms for 10 / 100 / 800
documents. The difference in the table is within run-to-run noise. Rerun the comparison if the
key ever covers more than the body.

Reading every document also means parsing every front matter on every call, and at scale that
parse, not the cache, was the cost: on a 20,112-document store (52MB) with a warm cache, one
`engmem search "retry window"` took 18.1–20.6 s, half of it in PyYAML's pure-Python loader.
`spine.load_yaml` therefore parses through libyaml (`yaml.CSafeLoader`) when PyYAML was built
with it, and falls back to `yaml.SafeLoader` otherwise; `spine.compose_yaml` gives the write
paths (`backfill._drop_declared_keys`, `mcp_server._patch_front_matter_line`) the node tree
through the same loader, so a write never refuses a document the read accepted. Measured
2026-10-20 on an Apple-silicon Mac, Python 3.13, three runs each: 18.1–20.6 s before, 10.2 s
after. The draft pass (`matching_drafts`) then loaded every published document's cache entry a
second time; it now reuses the entries the ranked search already holds (`SearchOutcome.entries`)
and loads only the draft candidates', so each document's entry is loaded once per search: on the
same store, interleaved runs, 11.1–12.5 s of CPU before, 8.1–8.4 s after.

The two loaders agree on the 20,112 documents and on dates, booleans, numbers, duplicate keys,
tags and the error classes, but not everywhere. libyaml accepts a tab after `key:` and inside a
plain value (`status:<tab>active`), which the pure loader rejects with `ScannerError`, and
rejects an astral character written as a surrogate pair escape in double quotes, which the pure
loader turns into lone surrogates. So on a PyYAML built without libyaml such a document is a
load error: whether a store is valid can depend on the build. Error wording differs too
(libyaml prints no snippet); nothing reads it.

## Kept between searches

A process that searches more than once — the MCP server answers every call of a session from
one — keeps what it built, and serves it again only where a cold load would build the same:

- `spine.load_store(sessions_dir, kept)` reuses a parsed `Doc` while its file reads byte for
  byte as before and its `(mtime_ns, size)` is unchanged. The file is still listed, stat'ed and
  read through `confined` on every call: a document that appeared, went, became unreadable or
  turned into a link is reported exactly as a cold load reports it, and a failed parse is never
  kept, so it fails again. The content hash catches an edit that kept size and mtime; the
  identity is in the key because a document stating no `date:` takes it from the mtime, so a
  `touch` alone changes the parse. Only the MCP server passes `kept` (`mcp_server._KEPT_DOCS`);
  the CLI loads once per process and keeps nothing.
- `scoring._entries_for_doc` keeps each document's section entries under the disk cache's own
  rule, the hash of the body, plus the document's id, which every entry carries.
- `scoring._doc_field_index` keeps the token sets of the four spine fields, keyed by their
  values.
- `cache.prune_orphans` prunes once per cache root and set of store paths.

The first three live in `cache.KeptByPath`: one value per path, served only under the key it was
built for, built again otherwise, and forgotten as soon as its path leaves the store, so a
long-lived server holds no document the store no longer has. It is keyed by the path's text, not
the `Path`: a Windows path compares case-insensitively, and a document renamed `Foo.md` to
`foo.md` keeps its bytes and stat, so an equal `Path` would serve the old path, and the old stem
as id and title. Nothing is dropped after a write tool: a write changes the bytes, and a changed
file is never served from memory. Reuse relies on nothing mutating a `Doc`, a `Section` or a
section entry after it is built; nothing does, and anything that starts to must copy first. It
also relies on one call at a time, as `mcp_server.serve`'s loop is; a threaded server would need
a lock around each of them.

Keeping documents is opt-in: only the MCP server passes `kept`, and the CLI parses every document
on every run. The other three are module state and act in every process, the CLI's too, which is
correct because their keys are the very inputs the value is built from; a one-shot CLI simply
never asks twice. Tests that mean two processes say so (`new_process` in `tests/conftest.py`).

Skipping a repeated prune has two residuals, accepted as housekeeping loss for the life of the
process: a temp file another, killed process leaked is not swept, and an `unlink` that failed
(a Windows sharing violation) is not retried. Both are retried as soon as the store's set of
documents changes, and by every new process. A prune that could not list the cache is not counted
as done.

Each search also counts document frequencies for the query's tokens only, the only ones
`_bm25_entry_score` reads, instead of for every token of the store.

Measured 2026-10-21 on an Apple-silicon Mac, Python 3.11, the 20,112-document store, six
searches in one process (`mcp_server._run_search_for_tool`), two runs: the first 6.5–9.7 s, every
later one 1.2–1.6 s (8.1–8.4 s of CPU per search before). The price is memory held between calls:
peak RSS was 0.84 GB for one search before and is 0.99 GB now, and the server keeps that between
calls instead of returning to its idle size. About half of what is left is reading
the 20,112 files through `confined`, three `open` calls each. Skipping that read when `stat` is
unchanged would make a warm search cheaper and would serve a stale document wherever two writes
land in one timestamp tick, so the read stays.

## `identity_for` is not the cache key

`identity_for`'s `(mtime_ns, size)` lives on in `Doc.source_identity` for the write-side
guards — `apply_backfill`, `mark_superseded`, `versions.retain` — which compare it against a
fresh `stat` to refuse a write over a file that changed since it was read. Those guards keep
the residual this cache no longer has: a size-preserving edit inside one filesystem tick of the
read is invisible to them (`contracts/backfill.md`, `contracts/mcp-server.md`). The cache does
not consult the identity at all, and an unstat-able file no longer goes uncached: its body was
read, so its key exists.

## Format version: what retires an entry

`CACHE_FORMAT_VERSION` must change whenever the same document would produce a different
payload: a new payload shape, but also a change to section boundaries (3) or to the
tokenizer (4, Unicode tokenization, `contracts/scoring.md` "Tokenisation"), or to the key
itself (5, the body's hash in place of `(mtime_ns, size)`). The content key cannot catch a
change to the code, because the body has not changed. Only the version can tell an entry the
old code built from one the current code would build. Bump 5 is the one a test cannot tell
from its absence: a format-4 entry has no `body_sha256`, so it would miss anyway. It is bumped
so the rule stays one rule, and so no reader has to work that out. Without the bump, a store
upgraded to Unicode tokenization kept serving its pre-upgrade entries, in which a Cyrillic
section had no tokens at all, and its documents stayed unfindable until each one was edited.
With it, every old entry is a silent miss, the first search after the upgrade rebuilds it,
and no document needs re-saving. This is the whole migration path.

The constant is `"5/unicode-<unicodedata.unidata_version>"`, not a bare number. NFKC,
`casefold` and the letter/mark classes the tokenizer splits on all come from the running
Python's Unicode database, and `cache_root()` is shared by every install on the machine. Two
Pythons with different Unicode versions (3.11 ships 14.0, 3.13 ships 15.1) can tokenize the
same text differently, and neither may serve the other's tokens as its own. The cost is a
rebuild each time the active Python changes. The comparison stays a plain `!=`, so a mismatch
remains a silent miss, as the table below requires.

## Degrading, and how loudly

`load` never raises: every damaged shape is a miss, and the caller recomputes. What differs is
whether the miss is announced, and a warning must be worth one line of a human's attention
**per document, per search**:

| shape | outcome |
|---|---|
| entry absent | silent miss |
| `format_version` mismatch | silent miss — bumping the constant must retire old entries quietly, not report a store full of corruption |
| `body_sha256` absent, wrong type, or not this body's hash | silent miss — see below |
| unreadable, not UTF-8, not JSON, not a JSON object, or a `payload` absent or null | warned miss |

The `body_sha256` row is a deliberate tie-break. A missing or non-string hash is damage,
exactly as a missing `payload` is, but the branch also carries the ordinary once-per-edit
mismatch, and hash damage is only partly detectable: `body_sha256: 5` can be recognised, a
well-formed hash of other text is indistinguishable from a stale entry. Warning on the
detectable slice alone would be arbitrary within one field, so the whole branch stays silent.
Either way the miss is rebuilt from the document, so damage costs latency, never a result.

A payload that loads here but does not have the shape `scoring` expects is decided a layer up,
as a warned miss (`contracts/scoring.md`, "A shape mismatch is a miss, never a crash").

Warnings go to stderr only. The agent reads stdout (`ENGMEM-SPEC.md` §4), so a
slow-but-correct search must never look like a failed one, and under `engmem mcp` (§5) stdout
carries protocol frames only, where one stray line kills the client session — `_warn` runs on
that path too. `tests/search/test_cache.py` asserts `captured.out == ""` on every degraded path, the
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
