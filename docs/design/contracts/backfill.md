# Contract: `engmem backfill`

Source: `ENGMEM-SPEC.md` §4 and data-model.md's "No field is a load gate". Implementation:
`src/engmem/backfill.py` (`propose_backfill` / `apply_backfill`), the write path shared with
the MCP tools in `src/engmem/staging.py`, and `cli.py`'s `_cmd_backfill`. A document written
before engmem existed loads with a partial spine; this command derives the missing front
matter from the document's own prose, shows it, and writes it only once a human agrees.

## Invariants

- Only front matter is written. The body is preserved byte-for-byte, and a staged file whose
  body differs from the original is refused before it is committed.
- A field the document already states (`spine.stated`) is never proposed, so a value a human
  or an earlier run set is never overwritten. New keys are appended to the author's own block,
  never re-dumped over it.
- `propose_backfill` computes; `apply_backfill` writes; nothing writes until `_cmd_backfill`
  has shown the proposal. The evidence string under each proposed value is the whole basis on
  which the human says yes, so it has to be true of the document: "no `- Repos:` preamble line
  with a value found" may not be printed over a line that carries one.
- `backfilled: true` is always proposed. It marks a document engmem did not author:
  `gate1_audit` keeps such documents out of the ritual population (`contracts/gate1.md`,
  "What "a session document" is"), and `ENGMEM-SPEC.md` §11 ("Amendment, recorded 2026-09-12")
  counts a document that is both backfilled and Pre-reg-less once, under this flag.
- `backfill` depends on `spine`, never the reverse. The preamble rules live in `spine.py`
  (`preamble`, `preamble_label_value`) because `spine`'s own `date` derivation obeys them.

## The CLI gate: propose, confirm, apply

`--id` names one document; `--all` selects every document `load_store` returned with
`spine_complete` false. Re-running on a document already completed is a no-op:
`spine_complete` is read from a fresh `load_store`, not from an earlier proposal. Every
proposal is printed (`output.render_backfill_proposal`) before any question is asked.
`--dry-run` stops there and `--yes` skips the prompt: they are the two halves of one
confirmation, preview then write, so a run with no terminal can still be gated on a preview
a human has read.

Not answering is a decline; an interrupt is not. End-of-input (`--all` behind a pipe, a cron
job with no terminal) is an unanswered `[y/N]`: it prints `cancelled` and exits as typing `n`
does. Ctrl-C exits 130 and is named on both streams, because exit 0 would let
`engmem backfill --all && deploy.sh` run `deploy.sh` on an interrupt. Neither leaves a
traceback, which reads as a crash partway through a write.

Exit codes: 2 when `sessions/` is missing or cannot be examined, when `sessions/` does not
resolve inside the store (`cli._sessions_is_contained`, checked before anything is read), when
`--id` names nothing, and whenever a target could not be read or written — under `--dry-run`
and after a decline too, because the preview is what a `--dry-run && --yes` script gates on.
Otherwise 0, including "nothing to backfill", which is printed rather than left silent so an
empty result is not mistaken for a crash. A target that vanishes or fails to read between
`load_store` and its own proposal is one document's failure, not the batch's.

Known gap: a `sessions/` that exists but cannot be listed is reported by `_load_sessions` on
both streams; under `--all` the run then continues over zero documents and exits 0 saying
every document has a complete spine. Stray `.md` files outside `sessions/` are not reported
here; `backfill` writes only where `load_store` found a document (`contracts/mcp-server.md`,
"Write tools: security contract").

## What is derived, and from what

| field | source |
|---|---|
| `id` | filename stem (`doc.id`, as `spine.parse_document` derives it) |
| `title` | first `# H1`, `Knowledge Base — ` prefix stripped (`doc.title`) |
| `date` | a `- Date:` / `- Updated:` preamble line carrying the date on that same line, else file mtime (`doc.date`) |
| `task_date` | = `date` |
| `status` | `superseded` when a `- Status:` preamble line contains the past participle `superseded`, else `active` — every other observed spelling ("Delivered", "In progress", no line at all) is a live document |
| `superseded_by` | the sibling `.md` link on that `- Status:` line, only when the line passes the word test and the effective status is `superseded` |
| `backfilled` | always `true` |
| `tags` | repo names from a `- Repos:` preamble line. Facet labels ("Classes", "Endpoints") are not tags: nearly every document in this genre has them, so a tag built from one distinguishes nothing |
| `repos` | the same names. `tags` is a scored spine field, so a repo name has to be there to be findable; `repos` is the declared answer to "which code is this about" and is never ranked on |
| `entities` | the `Search Keywords` section — every `Section` whose `canonical == "keywords"` (`sections.sections_for_role`), since an oversized section is split across its `###` subsections and a subsection whose own heading names a different role is not read. Proposed only when at least one term was derived |
| `related` | links to sibling `.md` files in the body, as CommonMark sees them (`markdown_it`): a link inside a code fence, an inline code span or escaped brackets is text being shown; a link inside a block quote is another document's edge, the policy `sections.py` applies to a quoted heading; a reference-style `[label][ref]` link is an edge |

Fields are proposed in this order, and only when not already stated in the document's own
front matter (`spine.stated`).

### A preamble line's value is on the line, and a blank label carries none

The `- Status:` and `- Repos:` patterns match the label only, every gap in them is
`[^\S\r\n]` and never `\s`, everything after the colon on that same line is the value, and
`preamble_label_value` walks the label lines in order until one carries a value that is not
blank. Both halves were got wrong once, each with a write behind it:

- `\s` spans the line break, so an unfilled `- Status:` read the next preamble line as its
  value: over `- Notes: superseded by [old](old-flow.md)` it derived `superseded` and aimed
  `superseded_by` at the document this one had replaced.
- A label with only whitespace after its colon must count as blank, not as a value of `" "`;
  a trailing space after an unfilled label is the ordinary way it is typed. Otherwise
  `- Status: ` shadowed a real `- Status: Superseded by [v2](v2.md)` below it, and on
  `- Repos:` the shadow was permanent: `tags: []` was written, the document went
  `spine_complete`, and two real repo names were never offered again (see "Why `tags` does not
  follow the same rule"). The evidence string at the prompt then said no such line was found
  while two sat in the document.

Blank means empty, whitespace-only, or nothing but emphasis markers. When no label line
carries a value the evidence says that ("no `- Status:` preamble line with a value found"),
not that no line exists.

Rejected: the ASCII `[ \t]` as the gap. The rule is "not across the line break", and `[ \t]`
also drops the non-breaking space and the en, thin, narrow and ideographic spaces, all of
which sit on the label's own line; `- Date:\xa02026-05-04` is what a Confluence/Notion/Google
Docs export writes, imported documents are the only corpus these derivations run on, and a
dropped line falls through to the same default as no line at all, so it never announces
itself. `str.strip()` strips Unicode whitespace too, so a value of one non-breaking space still
reads as blank. `\r` is excluded beside `\n` for honesty only: `preamble()` has already
normalised every line break to `\n` with `str.splitlines()`.

Known gap: a value written on the line below its label (`- Repos:` over an indented
`widget-cache`) does not contribute; only its first continuation line was ever read.

### `date` obeys the line rule, but not the value walk

`spine._derive_date` feeds `Doc.date` for every document in the store — recency in the
ranking (`scoring._date_ordinal`), the `last doc: Nd ago` footer, and the `date:`/`task_date:`
this command writes, where a wrong reading becomes the document's stated answer. Its pattern
had the same `\s` reach, and there the adopted value was someone else's date: an unfilled
`- Date:` took the first date-shaped token of whatever came next, including the `2024-01-15`
at the front of a sibling filename, and because the earliest match in the window wins it
shadowed a real `- Date:` further down.

It does not use `preamble_label_value`, because that walk stops at the first non-blank value,
which is wrong for a value that still has to parse: `- Date: TBD` above
`- Updated: 2024-01-15` would stop at `TBD` and drop the date to mtime. The date stays inside
the pattern, so the regex itself walks past label lines that carry none. Rejected with the
walk: "find a date anywhere in the value", which would read `- Date: see
2024-01-15-old-flow.md`, a pointer at another document, as this document's date.

## The successor of a superseded document

A `superseded` document with no `superseded_by` is a dead end: `output.py` prints a bare
`(superseded)` and `scoring.py` has nothing to redirect the reader to. The line that says the
document is superseded almost always names its successor, so that link, read by the sibling
rule `related` uses, becomes `superseded_by`. Only a link counts: an id guessed from prose
would render as `(not in store)` forever.

Two gates, both required. The word: `supersedes` and `superseding` name the document this one
*replaced*, and a stem test turned `- Status: Active — supersedes [v1](widget-cache-v1.md)`
into `status: superseded` plus a `superseded_by` aimed backwards — the live document hidden
and its reader sent to the dead one, silently. The link still reaches `related`; only `status`
and `superseded_by` are decided by the word. The effective status: `status` is never proposed
over a stated one, so without this gate the author's `status: active` stood while a
replaced-by claim was appended beside it. `superseded_by` is therefore proposed only when the
line passes the word test *and* the effective status — the derived one when none is stated,
the author's own when one is — is `superseded`. A stated `status: superseded` still takes its
successor from the line, the only place one is ever named, but unlocks nothing on its own:
over `- Status: Replaced by [v2](v2.md)` nothing is proposed.

Known gaps, left: "this superseded the old flow" on an active document's status line reads as
`superseded`, and no word test settles English; a line asserting both directions
(`Supersedes [v1](v1.md); superseded by [v3](v3.md)`) gets the right `status` but a
`superseded_by` decided by link order, since nothing on the line marks which link belongs to
which word; the bare stem (`- Status: Supersede by [v2](v2.md)`) reads as `active`, because
matching `supersede` also matches the two spellings above.

## The entity-extraction rule, and what it gets wrong on purpose

A `Search Keywords` section is hand-written prose grouped by facet (`**Classes:** WidgetCache,
CacheWarmer`), so the rule looks for identifier shape rather than parsing a list; the facet
label is scaffolding and is dropped. A candidate qualifies when it is backticked in the source
(the author's own "this is a literal identifier", accepted whatever its shape), or contains a
digit or one of `/ _ . -`, or has an uppercase letter after its first character (CamelCase, an
acronym, a Title Case phrase). A term over 4 words or 60 characters is prose.
`_ENTITY_DENYLIST` (`n/a`, `tbd`, `todo`, …) is applied before all three tests, so it also
overrides the backtick: several of its members pass the shape test on their own punctuation,
and a backticked `` `TBD` `` is still a placeholder.

Terms split on `,` `;` `·` `|`, but not inside a matched pair of parentheses: a parenthetical
is an aside about the term before it, and splitting through one turned `OrderAPI (v1, v2)`
into two digit-bearing fragments and stripped the backticks off
`` `WidgetCache` (thread-safe, LRU) ``, losing the author's explicit signal. Only a pair that
closes protects anything, so neither a stray `)` nor an unclosed `(` can swallow the rest of
the line. Rejected: "give up on the whole line" at the first unbalanced parenthesis, which
brought the bug back for the balanced part of any line ending in an unterminated aside.

Known false positive: a generic phrase title-cased for emphasis ("Read Path" as a facet body).
Known false negatives, deliberate: a lowercase term with no digit or punctuation (`sweeper`,
`cache warmup`) — accepting every lowercase noun phrase would flood `entities` with the
section's connective prose; a "one term per bullet" layout (`- **WidgetCache** — the
request-scoped cache class`), whose leading term reads as a facet label — telling "this bold
span names the row" from "this bold span groups the row" is judgement, not parsing, and is
left for a later revision; and a fully parenthesised list
(`**Classes:** (WidgetCache, CacheWarmer)`), which is one term that the trailing-parenthetical
strip then erases whole.

## The document with no derivable entities

When no section resolves to the `keywords` role, or none of its terms passes the test,
`entities` is left unset and a note tells the author to add or fix the section and re-run.
Guessing entities from body prose is the judgement the CLI does not make (`ENGMEM-SPEC.md`
§2, "The CLI is thin and dumb"; §10, "The agent writes facts; a human judges value").

Unset, not `entities: []`. An empty list is indistinguishable from a human's own "checked,
found none", and `spine.stated` counts it as answered: the document would go
`spine_complete`, `--all` would never offer it again, `--id` would answer "spine already
complete", and the note's advice could not be followed. The exit the note names is the author
writing `entities: []` themselves — the answer `backfill` will not give on their behalf.
Until then the document stays in `degraded_fields`, is listed by every `--all` run, and is
counted in the search footer's `N partial spine`; the `entities is empty` warning `load_store`
prints is on emptiness, not statedness, and is paid either way.

Known gap: documents written `entities: []` before this rule are `spine_complete` and are
never offered again. The `entities is empty` warnings name exactly that set, plus every
document whose author wrote `[]` deliberately — indistinguishable, which is the reason for the
rule — so the remedy is manual: delete the `entities:` line and re-run. Treating `[]` on a
`backfilled: true` document as unset would overrule the deliberate author, so it is not done.

### Why `tags` does not follow the same rule

`tags` is proposed even when the derived list is empty: a missing `- Repos:` line is the common
terminal case (not every session is about a repository), no note asks for one, and leaving it
unset would strand nearly every backfilled document in `degraded` with nothing actionable to
say.

That covers the missing-line case only. A `- Repos:` line that is present and yields no tag
writes `tags: []`, the document goes `spine_complete`, and fixing the line later changes
nothing — known and left; the evidence string distinguishes the two cases ("no `- Repos:`
preamble line with a value found" vs "`- Repos:` preamble line found, but no entry parsed as a
repo name"). Because that dead end is permanent, `` ` ``, `*` and `_` — CommonMark's code span
and both emphasis delimiters, the complete set, not the start of a punctuation strip — are
stripped from each entry before the slug test: a repo name contains none of them, and an
emphasised `- **Repos:** **widget-cache, platform-core**` otherwise wrote the `[]` that closes
the door. `_` cannot be left out because a mixed line (`*widget-cache*, _platform-core_`) then
loses one entry while the proposal looks right.

Known gap: stripping is shape-blind, so `- Repos: TBD` and `- Repos: *TBD*` both yield
`['tbd']`. The entity denylist would fix it; the alternative (`tags: []`, then
`spine_complete`) is not obviously better, so it waits for the next change to this derivation.

## What counts as a sibling, for `related`

`related` holds ids of documents in `sessions/`, so only a link naming a file in the document's
own directory contributes. The destination goes through `urlsplit`: a scheme or authority
means it is not a path (`https://…`, and `mailto:a@b.md`, which ends in `.md`); the path is
percent-decoded after the fragment was cut, never before, because a `#` that survived encoding
belongs to the filename; it is normalised so `./sibling.md` and `a/../sibling.md` count; and
anything still carrying `/`, or any `\` (a separator on the platform the link was written
for), points out of the directory. Without this
`[the spec](../../docs/design/contracts/backfill.md)` contributed the id `backfill`, rendered
forever as `backfill (not in store)` or colliding with a real document of that name.

`spine.validate_doc_id` is split here. Its shape test (`DOC_ID_RE`) is not applied: a document
whose filename does not match the canonical shape still loads (`load_store` warns), this
command exists for exactly those documents, and validating would silently drop real edges
between them; an id naming nothing is already rendered `(not in store)`. Its safety tests do
apply: a stem that is empty, starts with `.`, or carries a NUL is not an id, and without them a
typo'd `[see](..md)` wrote `related: ['.']`, which no author would recognise as their own.

## The proposal has to still describe the document

`propose_backfill` derives every value from `doc.body`, built during `load_store`, and
`_cmd_backfill` puts a human's prompt between that and the write — a prompt whose note asks
the author to go and edit the document. `apply_backfill` re-reads the body at write time, so
the edit survives and the byte-for-byte check passes; what goes stale, silently, is the derived
values. So the proposal carries the file's `(mtime_ns, size)` (`cache.identity_for`, the
section cache's staleness key; `contracts/cache.md`, "Identity, not invalidation") and a
write against anything else is refused with a `BackfillWriteError` telling the caller to
re-run.

Three orderings hold it up. The stamp is taken by `spine.parse_document` immediately before the
read it describes, not by `propose_backfill`, which runs later — for `--all`, a whole directory
scan later — and would stamp the already-edited file, certifying the staleness. The comparison
is made after `apply_backfill`'s own re-read, so one stat covers both reads; taken before it,
an edit landing in between produced a write of stale values that had just passed the check
(`mcp_server`'s `mark_superseded` orders it the same way). And `None` is not a match:
`None != None` is False, so a proposal with no stamp against a file that cannot be `stat`ed
passed on no evidence; "unverifiable" is refused like "changed".

The window closed is a human's; the residual is a size-preserving edit inside one filesystem
timestamp tick, which `contracts/cache.md` bounds for the same key.

`apply_backfill` also refuses a proposal whose `doc_id` or `path` is not the document it was
handed. Rejected: pairing the two positionally with a `zip`, which a dropped proposal silently
misaligned; the CLI loop carries each `doc` beside its own proposal, and the check is what
stops a foreign `id` landing in a document's front matter, which `load_store` would then
report as a duplicate id.

## Writing into front matter the author already started

New keys are appended to the author's own block, never re-dumped over it: a re-dump reformats
and reorders their lines and drops their comments. Appending has two collisions.

**A key the author declared and left empty.** `spine.stated` reads `tags:`, `tags: null` and
`tags: ~` as unset, so the field is proposed, and appending it leaves the key in the document
twice — PyYAML takes the last, so engmem still reads the right value, but the file is invalid
to a strict parser and a human editing the first occurrence sees no effect. So the author's
own empty line for each field being written is removed first (`_drop_declared_keys`).

Which lines those are is asked of the parser (`yaml.compose`), not of a line regex, because
both halves of "declares this key, with no value" are things only the parser knows. Column 0
is not a declaration test: PyYAML accepts an unindented continuation inside a flow collection,
so `status: null` on its own line can belong to a `navigation_miss: {` above it, and removing
it destroys the author's data while leaving a file that still parses. And emptiness has too
many spellings (`null`, `Null`, `~`, `!!null`, a lone comment, nothing at all) for one missed
spelling not to reintroduce the duplicate. A removable declaration is therefore a top-level
key in the set being written whose value node is tagged null and ends on the key's own line —
the last term because a whole line is removed per declaration. A quoted `"tags":` is the same
key, because PyYAML reads it as one.

A flow-style root (`{title: T, tags: null}`) is left alone, since every pair shares one line
and removing it would take the author's other keys; the block-style append then fails the
staged parse and the write is refused with a `BackfillWriteError`, the file byte-identical.
Two shapes keep the duplicate: a null written below its key (`tags:` over an indented `null`)
and the explicit-key form (`? tags` / `: null`). Removing those means removing a span, and a
span swallows whatever sits between its lines — a comment, for one — to buy the rarest
spellings in the format; PyYAML's last-wins keeps engmem reading correctly, as it did before
the rule. Because only an empty declaration is ever removed, no value the author wrote can be
lost, including on a document that already declares the key twice.

**A mapping with no collection in it.** `yaml.dump(..., default_flow_style=None)` renders an
all-scalar mapping in flow style (`{status: active, backfilled: true}`), and a flow mapping
appended into block front matter is a syntax error, so every document missing only scalar
fields failed to write, with an error naming a line the author never typed. `_NoAliasDumper`
forces block style for mappings; sequences keep the heuristic, so `tags: [widget-cache]`
renders as it always did.

### Line endings

The lines added, and the two `---` delimiters, take the document's own line ending
(`staging.newline_of`): the body is preserved byte-for-byte regardless, but emitting LF into a
CRLF document left the file with both — a whole-file diff under `core.autocrlf` or a
`.gitattributes` `eol`, on exactly the legacy documents this command exists for. The front
matter's own lines answer first; a document with no front matter is having one created above
its body, so the body answers. A document whose front matter and body disagree keeps both;
normalising either is not this command's call. `staging.read_document` reads bytes, never
`read_text`, which translates line endings invisibly to a caller rebuilding the file; the BOM
it splits off is re-prefixed at write time.

## Atomic, body-preserving writes

The staging is `engmem/staging.py` — `stage` / `commit` / `discard` — shared with
`mcp_server`'s write tools (`contracts/mcp-server.md`, "Write tools: security contract")
rather than written twice. It had been written twice, and the copies had diverged: the MCP
side wrote with `write_text`, without an `fsync`, and without restoring the replaced file's
mode. Sharing fixed all three at once.

`apply_backfill` stages the new content to a uuid-named sibling `.tmp` and, before
`os.replace` commits it, confirms three things against the staged file: the body is
byte-identical to the original; the file re-parses (`spine.parse_document`); and its front
matter actually states every field about to be reported as written. The third keeps the
returned message from becoming a lie: the first two pass over a staged document that appended
nothing, while `_drop_declared_keys` has by then removed the author's own empty declarations
for exactly those fields. No input reaches it today; it is there so a future change to the
append or the drop cannot turn a silent no-op into "backfilled 8 field(s)" with the author's
lines gone. On any failure, Ctrl-C included (`BaseException`, not `Exception`), the staged
file is discarded and the document is untouched.

The staged file is `fsync`ed before the replace: the rename is atomic only over content that
reached the disk. The directory entry is deliberately not synced — losing the rename gives
back the untouched document, a safe outcome, while losing the content would not be.

### What the replaced file inherits

`os.replace` puts a new file where the document was, so everything the old file carried is
put back deliberately. The temp file is created at `0600` and the document's own mode is
restored at commit time, immediately before the replace. Both halves matter: left to the
umask, a `0600` private note came back `0644`, a grant that does not appear in the diff shown
before writing; and narrowing only at the end would leave the staged file — the whole body,
read back, split and parsed before the commit — open in that window. A document that did not
exist before keeps `0600`: there is no mode to restore, and a session document is the user's
own notes. Ownership is restored best-effort (only root can give a file away), so
`sudo engmem backfill` does not leave a root-owned `0600` document its author cannot read.

Known gap: a document deleted between stage and commit is recreated at `0600`; the staleness
check runs before staging and does not cover it.

A symlinked document is refused, not written — `contracts/mcp-server.md`'s point 3 for the MCP
write tools — and `sessions/` itself being a link out of the store is refused by
`cli._sessions_is_contained`, its point 2, because a git checkout carries a symlinked
directory as readily as a file and following one aims every write of `--all --yes` outside the
store. `os.replace` over the link turned it into a regular file and left the original to
diverge; writing through it is worse in both directions. Out of the directory, it would make
`apply_backfill` the only path in this codebase that writes outside a store that is
relocatable with `--store`/`ENGMEM_HOME`, and `load_store` accepts any `*.md` link in
`sessions/` whatever it points at. Inside the directory, the link shares an inode with the
real document, so the alias's `id` lands in it and `load_store` reports a duplicate id across
the pair.

Not closed by this: a hard link. `os.replace` leaves the second name holding the pre-backfill
content. Keeping it would mean writing in place, forfeiting the atomic replace and the
guarantee that a crash never leaves a half-written document; it cannot be detected as cheaply
as a symlink, and unlike one it aims the write nowhere new.
