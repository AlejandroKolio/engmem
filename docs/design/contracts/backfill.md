# Contract: `engmem backfill`

Source: `ENGMEM-SPEC.md` §4, data-model.md's "No field is a load gate". Implementation:
`src/engmem/backfill.py` (`propose_backfill` / `apply_backfill`); CLI surface (the
confirmation prompt, `--id`/`--all`/`--dry-run`/`--yes`) is `cli.py`'s `_cmd_backfill`.

Nothing in `backfill.py` writes without a proposal being shown first — that gate lives in
`_cmd_backfill`, not in this module. `propose_backfill` only computes what would be written;
`apply_backfill` writes it, once the caller has decided to, atomically.

Neither way of not answering the prompt writes anything, but they exit differently, because
they mean different things. End-of-input (`--all` behind a pipe, a cron job with no terminal)
is an unanswered `[y/N]`, which is a "no": it prints `cancelled` and exits 0, exactly as
typing `n` does. An interrupt is not a decline — exit 0 would let
`engmem backfill --all && deploy.sh` run `deploy.sh` because the user pressed Ctrl-C — so it
is named on both streams and exits 130, the shell's own code for a run stopped by SIGINT.
Neither leaves a traceback, which reads as a crash partway through a write, the one outcome
this command's staging exists to make impossible.

## What is derived, and from what

| field | source |
|---|---|
| `id` | filename stem (`doc.id`, already how `spine.parse_document` derives it) |
| `title` | first `# H1`, `Knowledge Base — ` prefix stripped (`doc.title`, ditto) |
| `date` | a `- Date:` / `- Updated:` preamble line, else file mtime (`doc.date`, ditto) |
| `task_date` | = `date` |
| `status` | a `- Status:` preamble line — `superseded` if it says so, `active` otherwise (matches every observed spelling: "Delivered", "In progress", and the common case of no line at all). "Says so" is the past participle `superseded`, not the stem: `supersedes` and `superseding` state the *opposite* relationship, and reading them as this one wrote `status: superseded` onto the live document and pointed `superseded_by` at the document it had itself replaced |
| `superseded_by` | the sibling `.md` link on the `- Status:` line, when that line says the document is superseded. Proposed only then, and only when the line actually names a document |
| `backfilled` | always `true` — that is what the field is for |
| `tags` | repo names from a `- Repos:` preamble line. Facet labels ("Classes", "Endpoints") are deliberately *not* tags: nearly every document in this genre has them, so a weight-1 tag built from one distinguishes nothing and only dilutes |
| `repos` | the same repo names, on their own. Not a redundant copy: `tags` is a scored spine field, so a repo name has to be there to be findable, while `repos` is the declared answer to "which code is this about" and is never ranked on |
| `entities` | the document's `Search Keywords` section — every `Section` resolving to `canonical == "keywords"`, since an oversized section is split across its `###` subsections and the terms live in all of them (`sections.sections_for_role`) — except a subsection whose own heading names a *different* role (`### Status`, `### Flow`), which leaves the parent's role for its own and is not read. See the extraction rule below. Proposed only when at least one term was derived |
| `related` | markdown links to sibling `.md` files found in the body, as CommonMark sees them (`markdown_it`). A link quoted inside a code fence, an inline code span, or behind escaped brackets is text being shown, not an edge; a link inside a block quote is another document's edge, matching the call `sections.py` makes for a quoted heading; a reference-style `[label][ref]` link *is* an edge |

Fields are proposed in the order above. Each is proposed only if it is not already
present in the document's own front matter (`spine.stated`) — a field a human, or an
earlier `backfill` run, already set is never proposed for overwriting.

## The successor of a superseded document

A `superseded` document with no `superseded_by` is a dead end: `output.py` prints a bare
`(superseded)` and `scoring.py` has nothing to redirect the reader to. The line that says
the document is superseded almost always names the one to read instead — "Superseded by
[Widget Cache v2](widget-cache-v2.md)" — so that link, read by the same sibling rule
`related` uses, becomes `superseded_by`.

Only a link counts. `- Status: superseded, replaced by the new cache` names no document, and
an id guessed from that prose would render as `(not in store)` forever.

The direction has to be read from the word, because both directions are written on the same
line: `- Status: Active — supersedes [Widget Cache](widget-cache-v1.md)` names the document
this one *replaced*. Under a stem test it became `status: superseded` plus a `superseded_by`
aimed backwards, so the live document was hidden and its reader redirected to the dead one —
the worst outcome available here, and silent. The link is still an edge, so it reaches
`related` either way; it is only `superseded_by` and `status` that the word decides — and
only on a line that asserts *one* direction.

Three residuals are known and left:

  - genuine English ambiguity: "this superseded the old flow" on an active document's
    status line reads as `superseded`, and no word test settles it;
  - a line asserting *both* directions — `- Status: Supersedes [v1](v1.md); superseded by
    [v3](v3.md)` — gets the right `status`, but `superseded_by` is then decided by link
    order, not by the word, so the first spelling above aims it at `v1`, the document this
    one replaced. The word appears twice with opposite subjects; nothing on the line marks
    which link belongs to which, so this is a limit of the evidence, not of the test. The
    document is at least no longer hidden, which is what the stem test got wrong;
  - the bare stem: `- Status: Supersede by [v2](v2.md)` reads as `active`. Accepting it
    would mean matching `supersede`, which also prefixes `supersedes` and `superseding` —
    the exact false positive above. The past participle is the only spelling that names
    this document as the replaced one unambiguously.

## The entity-extraction rule, and what it gets wrong on purpose

A `Search Keywords` section is hand-written prose, not a list to parse mechanically:
bulleted, grouped by facet (`**Classes:** WidgetCache, CacheWarmer`), separated by `,` `;`
`·` or `|`, sometimes with backtick-quoted terms, sometimes with a plain (non-bold)
`Label:` instead of a bold one.

Per candidate term (after label-stripping and delimiter-splitting), a term qualifies as an
entity if:
  - it is wrapped in backticks in the source (`` `WidgetCache` ``) — the author's own
    explicit "this is a literal identifier" signal, always accepted regardless of shape; or
  - it contains a digit, or one of `/ _ . -` (endpoint paths, file names, hyphenated
    identifiers), and is not on the small denylist of tokens that also match this shape but
    are never entities (`n/a`, `tbd`, `todo`, …); or
  - it has an uppercase letter anywhere after its first character — CamelCase
    (`CacheWarmer`), an ALLCAPS acronym (`TTL`), or a multi-word Title Case phrase
    (`Response Cache`).

A term longer than 4 words or 60 characters is never accepted — that is prose, not a
keyword.

**Known false positives**: a generic capitalized phrase the author happened to
title-case for emphasis, not because it names a system entity (e.g. "Read Path" as a
facet body, not a label), and any deliberately backtick-quoted non-identifier the author
quoted for a different reason (rare in practice — backticks are a strong, deliberate
signal in this genre of document).

**Known false negatives**: a lowercase, single- or two-word term with no digit or
punctuation (`sweeper`, `cache warmup`) — the rule requires *some* identifier-shaped
signal, and plain English prose describing a concept has none. This is deliberate: the
alternative (accepting every lowercase noun phrase) would flood `entities` with the
section's connective prose and defeat its purpose. A "one term per bullet, with an
em-dash description" layout (`- **WidgetCache** — the request-scoped cache class`) is
also not handled: the leading term is read as a facet *label*, not a candidate, so it is
dropped rather than promoted to `entities`. Recognising that shape needs a different rule
(distinguishing "this bold span names the row" from "this bold span groups the row") that
starts to look like judgement rather than parsing an already-structured list, and is left
for a future revision rather than guessed at here.

## The document with no derivable entities

Every other field above is still proposed — `entities` is the only field this section
feeds. Rather than guess entities from free-form body prose (which is exactly the
"close to judgement" line `ENGMEM-SPEC.md` §2.4 draws around the CLI), `entities` is
**left unset** and a note is attached telling the human so, in both cases:

  - there is no `Search Keywords` section at all; or
  - there is one, but no term in it passes the shape test above.

Unset, not `entities: []`. An empty list is indistinguishable from a human's own
"checked, found none", and `spine.stated` counts it as answered: writing it would make
the document `spine_complete`, so `backfill --all` would never offer it again and
`backfill --id` would answer "spine already complete". The note tells the author to add
the section and re-run — writing `[]` would make that advice impossible to follow, and
the document's entities would stay empty permanently.

The author's way to close it out is to write `entities: []` themselves — that *is* the
answer "checked, there are none", and it is a judgement `backfill` will not make on their
behalf. Both notes say so, so the to-do never becomes a nag with no stated exit.

The cost, until they do, is that the document stays degraded (`entities` in
`degraded_fields`): every `backfill --all` run lists it with its note and writes nothing,
and the scoreboard footer every `engmem search` prints keeps counting it in
`N partial spine`. Both end the moment the author writes `entities: []`. The third cost
does not: `load_store` warns `entities is empty` on stderr whenever the list is empty
(`spine.py`, `if not doc.entities`), which is emptiness, not statedness — that warning is
paid the same either way, and is not something this rule adds or the exit removes. The
steady state is intended — "degraded" is the truthful description of a document whose
entities were never determined — but it is paid on the search surface, not only by
whoever runs `backfill`.

### Documents the earlier behaviour already stranded

Before this rule, `entities: []` was written. Those documents are `spine_complete`, so
neither `--all` (which selects on `not spine_complete`) nor `--id` (which answers "spine
already complete") will ever offer them again. The fix stops new ones being created; it
repairs none. To find them, read the `entities is empty` warnings any store load prints on
stderr — that check is on emptiness, so it names exactly this set (plus any document whose
author wrote `entities: []` deliberately, which is indistinguishable and is the whole
reason this rule exists). The manual remedy is to delete the `entities:` line and re-run
`backfill`. Recognising `entities: []` on a `backfilled: true`
document as unset would automate that, but it would also overrule a human who wrote `[]`
deliberately, so it is left as a knowing gap rather than decided here.

### Why `tags` does not follow the same rule

`tags` is proposed even when the derived list is empty. A missing `- Repos:` line is the
common, terminal case for this genre of document (not every session is about a repository)
and no note asks the author to go add one, so leaving `tags` unset would strand nearly
every backfilled document in `degraded` with nothing actionable to say about it.

That argument covers the missing-line case only. A `- Repos:` line that *is* present and
yields no tag (the parser rejected every entry) has exactly H1's shape: `tags: []` is
written, the document goes `spine_complete`, and fixing the line later changes nothing.
That dead end is known and knowingly left — the evidence string distinguishes the two
cases ("no `- Repos:` preamble line found" vs "`- Repos:` preamble line found, but no
entry parsed as a repo name") so a proposal at least does not read as the wrong one.

Because that dead end is permanent, an entry is read through the author's emphasis rather
than around it: `` ` ``, `*` and `_` — CommonMark's code span and *both* of its emphasis
delimiters — are stripped from each entry before the slug test, so
`- **Repos:** **widget-cache, platform-core**` and `- Repos: _widget-cache_` contribute
their names. A repo name contains none of the three, so nothing is lost by stripping them,
while leaving them in made an emphasised `- Repos:` line contribute nothing and then wrote
the `tags: []` that closes the door. The set is bounded to those three deliberately: it is
the complete list of characters CommonMark can wrap an entry in, not the start of a general
punctuation strip. A mixed line (`*widget-cache*, _platform-core_`) is the reason `_` cannot
be left out — one entry survives, so the proposal *looks* right while the other is gone.

Stripping is shape-blind, so a placeholder that happens to be slug-shaped still becomes a
tag: `- Repos: TBD` yields `['tbd']`, as it did before emphasis was stripped, and now so
does `- Repos: *TBD*`. Reusing the entity denylist here would fix it; the alternative that
happens today (`tags: []`, then `spine_complete`) is not obviously better, so it is left
for the next time this derivation is touched.

## What counts as a sibling, for `related`

`related` holds ids of documents in `sessions/`, so only a link naming a file in the
document's own directory can contribute one. The destination is read with `urlsplit`, then:

  - anything carrying a scheme or authority is not a path — `https://…`, and also
    `mailto:a@b.md`, which ends in `.md` and would otherwise contribute `mailto:a@b`;
  - the remaining path is percent-decoded (after the fragment was cut, never before: an
    anchor is written literally, so a `#` that survived encoding belongs to the filename)
    and normalised, so `./sibling.md` and `a/../sibling.md` are recognised as siblings;
  - anything still carrying `/` after that — or any `\`, which is not a separator here but
    is one on the platform the link was written for — points out of the directory and is
    dropped.

Without this, `[the spec](../../docs/design/contracts/backfill.md)` contributed the id
`backfill`: an edge to a document that does not exist, rendered forever as
`backfill (not in store)`, or worse, colliding with a real document of that name.

`spine.validate_doc_id` bundles two independent tests, and only one of them is declined
here. Its *shape* test (`DOC_ID_RE`) is not applied: a document whose filename does not match
the canonical id shape still loads (`load_store` warns, it does not reject), and this command
exists for exactly those legacy documents — validating here would silently drop real edges
between them. An id naming nothing in the store is already reported by `output.py` as
`(not in store)`, which is the honest answer.

Its *safety* tests have nothing to do with legacy id shapes and do still apply: a stem that
is empty, starts with `.`, or carries a NUL is not a document id. Without them `.md`, `..md`
and `...md` contributed the ids `.md`, `.` and `..` — a typo'd `[see](..md)` wrote
`related: ['.']`, which every later render shows as `. (not in store)` and which no author
would recognise as their own typo.

## Writing into front matter the author already started

New fields are appended to the document's own front matter, not re-dumped over it: a
re-dump would reformat and reorder the author's lines and drop their comments, and this
command exists to touch as little of a hand-written document as possible.

Appending has two collisions.

**A key the author declared and left empty.** `spine.stated` reads `tags:`, `tags: null`,
`tags: ~` (and every other spelling) as unset, so the field is proposed — and appending it
would leave the key in the document twice. PyYAML takes the last, so engmem itself would
still read the right value, but the file is invalid to a strict parser and a human editing
the first occurrence would see no effect. So the author's own line for each field being
written is removed first (`_drop_declared_keys`).

Which lines those are is asked of the parser (`yaml.compose`), not of a line regex, because
both halves of "declares this key, with no value" are things only the parser knows. Column 0
is not a declaration test: PyYAML accepts an unindented continuation inside a flow
collection, so `status: null` on its own line can belong to the value of a
`navigation_miss: {` above it, and removing it would destroy the author's data while leaving
a file that still parses. And emptiness has too many spellings to enumerate — `null`,
`Null`, `~`, `!!null`, a lone comment, nothing at all — where one missed spelling silently
reintroduces the duplicate.

A removable declaration is therefore a *top-level* key, in `names`, whose value node is
tagged null and *ends on the key's own line*. The last term is because a whole line is
removed per declaration, which needs one declaration per line — and for the same reason a
flow-style root (`{title: T, tags: null}`) is left entirely alone, since every pair there
shares one line and removing it would take the author's other keys with it. Nothing is then
written for such a document at all: the block-style addition makes the front matter
unparseable, the staged parse fails, and the write is refused with a `BackfillWriteError`
leaving the file byte-identical.

Two shapes are therefore left carrying the duplicate this section exists to prevent: a null
written below its key (`tags:` over an indented `null`) and the explicit-key form (`? tags`
/ `: null`). Removing those means removing a *span* rather than a line, and a span swallows
whatever sits between the two lines — a comment the author wrote there, for one — to buy the
rarest spellings in the format. The append lands beside them and PyYAML's last-wins keeps
engmem reading correctly, exactly as it did before this rule existed.

**A mapping with no collection in it.** `yaml.dump(..., default_flow_style=None)` renders a
mapping whose values are all scalars in flow style — `{status: active, backfilled: true}` —
and a flow mapping appended into a block front matter is a syntax error. Every document
missing only scalar spine fields therefore failed to write at all, with a `BackfillWriteError`
naming a line the author never typed. `_NoAliasDumper` forces block style for mappings;
sequences keep the heuristic, so `tags: [widget-cache]` renders as it always did.

A quoted spelling (`"tags":`) counts as the same key, because PyYAML reads it as one.

Because only an empty declaration is ever removed, no value the author wrote can be lost —
including on a document that already declares the key twice, where the valued line stays
and the append lands beside it. Reading is unaffected either way (PyYAML takes the last),
and nothing is destroyed. Where a *line-removable* declaration cannot be removed safely it
is left and the duplicate stands, rather than anything being guessed at; a flow-style root
is the one shape where that is not possible and the write is refused instead.

### Line endings

The lines added, and the two `---` delimiters, take the document's own line ending. The
body is preserved byte-for-byte regardless, but emitting LF into a CRLF document left the
file with both — a whole-file diff under `core.autocrlf` or a `.gitattributes` `eol`, on
exactly the legacy documents this command exists for.

The front matter's own lines answer first (that is the block being extended); a document
with no front matter is having one created above its body, so the body answers. A document
whose front matter and body already disagree keeps both as they are — normalising either
is not this command's call.

## Splitting a keywords line into terms

Terms are separated by `,` `;` `·` or `|`, but not inside parentheses: a parenthetical is
an aside about the term before it. Splitting through one produced fragments that are not
terms — `OrderAPI (v1, v2)` became `OrderAPI (v1` and `v2)`, both of which pass the shape
test on their digits — and did it to `` `WidgetCache` (thread-safe, LRU) `` too, where the
fragment no longer matches the backtick test and the author's own explicit "this is an
identifier" signal was lost entirely.

Only a pair that actually closes protects anything: the matched spans are marked in one
pass, and a separator splits unless it falls inside one. So neither a stray `)` nor an
unclosed `(` can swallow the rest of the line, and an unclosed one later on does not cost
the balanced parenthetical before it — a line-level "give up on the whole line" rule brought
the bug straight back for the balanced part of any line ending in an unterminated aside.

One consequence worth naming with the other false negatives: a fully parenthesised list
(`**Classes:** (WidgetCache, CacheWarmer)`) is one term, which the trailing-parenthetical
rule then erases whole, so it contributes nothing at all.

### The proposal has to still describe the document

`propose_backfill` derives every value from the document as it was when it read it, and
`_cmd_backfill` prints a confirmation prompt between that and the write. An author is free
to edit while the prompt waits — and the note this command prints for a document with no
`Search Keywords` section asks them to do exactly that.

Nothing else would notice. `apply_backfill` re-reads the body at write time, so the edit
survives and the byte-for-byte check still passes; what goes stale is the derived values,
silently. So the proposal carries the file's `(mtime_ns, size)` (`cache.identity_for`, the
same staleness key the section cache uses), and a write against anything else is refused
with a `BackfillWriteError` telling the caller to re-run.

The comparison is made *after* that re-read, not before it, so one stat covers both reads —
the `load_store` read every proposed value came from, and `apply_backfill`'s own. Taken
before the read, it certified the file up to the moment it ran and left the read that
followed uncovered: an edit landing in between produced a write of stale values that had
just passed a staleness check. That window is microseconds where the prompt's is a human's,
but it is closed by ordering rather than by argument, and `mcp_server`'s
`mark_superseded` — which took the rule from here — already orders it this way. A document
deleted in that window now fails in the read rather than in the check, as an `OSError` the
CLI reports per document; nothing is written either way.

An identity that cannot be taken at all is not a match. `None != None` is False, so a
proposal carrying no stamp, applied to a file that cannot be `stat`ed, passed the check on
no evidence — "unverifiable" is refused like "changed", since the whole point is that the
values are only known to describe a file whose identity was proven.

The stamp is taken by `spine.parse_document`, immediately before the read it describes — not by
`propose_backfill`, which runs later. Every proposed value comes from `doc.body`, and `doc`
was built during `load_store`; for `--all` that is one whole directory scan earlier. A stamp
taken at proposal time would already be describing the edited file, and would certify the
staleness rather than catch it.

The window this closes is a human's: for `--id`, the parse of every document sorted after the
target; for either flag, however long the confirmation prompt waits. A `(mtime_ns, size)` key
is enough for that — the residual needs a size-preserving edit inside one filesystem timestamp
tick.

`apply_backfill` also checks the proposal is for the document it was handed. The pair is
only ever assembled together in the CLI's proposal loop — it used to be aligned positionally
by a `zip`, which a dropped proposal would silently misalign, which is why that loop now
carries each `doc` alongside its own proposal. Writing one document's values into another
would put a foreign `id` in its front matter, and `load_store` would then report a duplicate
id across the two.

### What the replaced file inherits

`os.replace` puts a *new* file where the document was, so everything the old file carried
has to be put back deliberately.

The temp file is created at `0600`, and the document's own mode is read and restored at
commit time, immediately before the replace.

Both halves matter. Left to the umask, a `0600` private note came back `0644` — a silent
grant, and one that does not appear in the diff engmem shows before writing. But narrowing
only at the end would be too late for the staged file itself: it holds the whole body, and
the read-back, the split and a full parse all happen before the commit. Creating it at `0600`
covers that window; the restore covers the other direction, since without it a shared `0644`
document would come back `0600`.

One window this leaves: if the document is *deleted* between the stage and the commit, there
is no mode to read and the replace recreates it at `0600`. The staleness check runs before
staging and does not cover it. Restoring a mode nobody can read is not better than that.

**Ownership** is restored on a best-effort basis (only root can give a file away, and only
root can end up owning someone else's document). After `sudo engmem backfill`, a root-owned
`0600` document would otherwise be unreadable to the author who wrote it — the mode fix is
what turns that from a nuisance into a lockout.

A **symlinked** document is refused, not written — the same rule `mcp-server.md` states as
point 3 for the MCP write tools, and for stronger reasons here. `sessions/` itself being a
link out of the store is refused too, by `cli._sessions_is_contained`, mirroring that
contract's point 2: a git checkout carries a symlinked directory as readily as a symlinked
file, and following one aims every write of `--all --yes` at a directory the store does not
own.

`os.replace` over the link silently turned it into a regular file, leaving the original
untouched and the two copies diverging from the next edit onward. Writing *through* it is
worse in both directions. A link out of the directory would make `apply_backfill` the only
path in this codebase that writes outside the store, and the store is relocatable with
`--store`/`ENGMEM_HOME`: `load_store` accepts a link named `*.md` in `sessions/` whatever it
points at, so a link committed to a shared store would aim `backfill --all --yes` at any file
on the machine. A link *inside* the directory is no better — the two names share an inode, so
the alias's `id` lands in the real document too, and `load_store` then reports a duplicate id
across the pair, the real document having lost its own identity.

A **hard link** is not preserved: `os.replace` leaves the second name holding the
pre-backfill content, to diverge from the next edit onward. Keeping it would mean writing in
place, which forfeits the atomic replace and the guarantee that a crash can never leave a
half-written document. That trade is not worth it, so the limitation stands rather than being
fixed — it cannot be detected as cheaply as a symlink, and unlike one it does not aim the
write anywhere new.

## Where the write itself lives

The staging is `engmem/staging.py` — `stage` / `commit` / `discard` — shared with
`mcp_server`'s write tools rather than written twice. It had been written twice, and the two
copies had already diverged: the MCP side wrote with `write_text` (which translates line
endings, the very thing `staging.read_document` reads bytes to avoid), without an `fsync`, and
without restoring the mode of the file it replaced. Sharing fixed all three at once.

One rule the shared version had to decide: a document that did not exist before keeps the
staged `0600` rather than the umask default. There is no previous mode to restore, and a
session document is the user's own notes.

## Atomic, body-preserving writes

The staged file is `fsync`ed before the replace: the rename is atomic, but only over
content that reached the disk, and without the sync a crash just after it can leave the new
name pointing at unwritten blocks. The directory entry is deliberately *not* synced — losing
the rename gives back the untouched document, which is a safe outcome, while losing the
content would not be.

`apply_backfill` stages the new content to a sibling temp file, re-splits it, and
confirms the body is byte-identical to the original, that the staged file re-parses, and
that its front matter actually *states* every field being reported as written, before
`os.replace` commits it over the original — matching `mcp_server.py`'s write-tool
staging pattern, for the same reason: a crash between staging and committing must never
leave a half-written document, and a bug in this function must never be the thing that
corrupts a human's own hand-written document. Re-running `backfill` on a document it has
already completed is a no-op (`doc.spine_complete` is checked again from the file on
disk, not from a proposal computed a moment earlier).

The last of those three checks is what keeps the returned message from becoming a lie. The
body check and the parse both pass over a staged document that appended *nothing* — it is a
valid document, and its body is untouched — while `_drop_declared_keys` has by then removed
the author's own empty declarations for exactly those fields. So "backfilled 8 field(s)" is
asserted against the staged file before it is committed, the same way `mark_superseded`
re-reads its own patch instead of trusting that it landed. No input reaches it today; it is
there so that a future change to the append or the drop cannot turn a silent no-op into a
reported success, with the author's own lines gone.
