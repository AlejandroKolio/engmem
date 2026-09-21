# Contract: the Gate 1 verdict core

Source: `src/engmem/gate1.py` and `src/engmem/gate1_audit.py`. Consumed by
`tools/verify_citations.py` (exit-code gate), `tools/gate1_report.py` (the review table) and
`mcp_server._reuse_log_rejections` (the write path). This is the module that decides the
pre-registered §11 endpoint, so "why is this row excluded" needs a written answer here, and
§11 forbids reading a rule into the count after the fact, so every change to a counting rule
is dated against the data it met.

## Two tools, one verdict, three public helpers

`QUOTE_RE`, `NONE_LINE` and the row parser were once byte-identical copies in both tools, so
widening the quote regex in one made the two disagree about which rows exist. Both now read
typed verdicts from `gate1.evaluate()`; neither imports the other, because the alternative
makes one tool's stdout prose the other's input format.

Their exit codes differ on purpose. `verify_citations.py` exits 1 on exactly the
citation-integrity axis and never grows to cover classification, staleness or dogfooding;
`gate1_report.py` exits 0 unconditionally, because a report that can fail is a report nobody
runs before the Gate 1 review. Every later addition to the report (the audit block, `--since`)
inherits that rule rather than overriding it.

`reuse_log_rows`, `quotes_in` and `classification_of` are public, not underscored, for one
consumer: `engmem_complete_draft` refuses a row the count would exclude for a defect visible in
the row itself, and that refusal must agree with the count to the character. A second regex in
`mcp_server.py` would be the tool-to-tool drift above, reintroduced between the gate and the
write path. The refusal walks every `reuse` section while `evaluate()` reads the first, so the
rows it refuses are a superset of the rows the count verdicts. The store-dependent axes
(`cited_missing`, `quote_not_found`, distance, staleness) are never decided at write time —
`contracts/mcp-server.md`, "`engmem_complete_draft`: Reuse Log rows — refuse, then warn".

## The five axes, and why they are not one enum

A single `status` enum collapses states that are reachable two different ways or not at all: a
row can be `verified` and `harmful` at once, and a `no_quote` row can never be `reuse` whatever
its classification cell says. `RowVerdict` keeps five independent fields:

- **A — citation integrity** (`integrity`): `no_quote` -> `cited_missing` -> `quote_not_found`
  -> `verified`, in that order because it is the only order in which each check has the data it
  needs.
- **B — classification** (`classification`): the author-declared `reuse` / `anti-reuse` /
  `harmful`, plus `missing` (blank cell, or a table with fewer than four columns) and
  `unrecognized` (present but misspelled). The two derived values are separate counters so a typo
  is fixable rather than folded into one "invalid" bucket.
- **C — distance** (`distance`): `adjacent` / `distant` / `undecidable`, computed only once axis A
  reaches `verified` — a `quote_not_found` row has nothing to be distant *from*, and the
  pre-existing bug was exactly `"cited doc not in store"` printed in the distance column.
- **D — staleness** (`staleness`): `cited_active` / `cited_superseded`, computed whenever the
  cited document resolves, independent of A and C. It never gates anything by itself (see
  "Supersession is a flag, not a filter").
- **E — human verdict**: the `changed a decision?` cell, the only axis this module never writes
  for an eligible row (see "Who fills the last column").

The four mechanical axes are `enum.StrEnum` types (`Integrity`, `Classification`, `Distance`,
`Staleness`) because a mistyped value could only ever fail in the permissive direction:
`exclusion_reason` returns `None` for "eligible", so `"cited_misssing"` matched no branch and
counted toward the primary endpoint. `Integrity("cited_misssing")` raises at construction.
Member values are byte-identical to the old strings, so the printed table and every summary
counter are unchanged. Two properties hold that up and are tested separately: a member renders
as its bare value (what an f-string needs), and a member is a `str` subclass (what
`gate1_report.py`'s `" | ".join(...)` needs — `str.join` never calls `Enum.__str__`, so the join
test says nothing about rendering). The rendering guard is kept although the current renderer
joins rather than formats, because it becomes load-bearing the day that line is rewritten.

`exclusion_reason` consults two lookup tables, `INTEGRITY_EXCLUSIONS` and
`CLASSIFICATION_EXCLUSIONS`, rather than a nine-branch `if` chain, so exhaustiveness tests can
walk each enum member by member and a member added without a reason fails the suite instead of
reading as eligible. `unrecognized` stays an explicit branch because its message quotes
`classification_raw`, the human's actual spelling, which no static table can hold.
`VALID_CLASSIFICATIONS` is a frozenset of `Classification` members, not strings, so a cell that
literally spells `missing` or `unrecognized` still reads as `unrecognized`.

## Citation integrity — the join

`verify_citations.py` always implemented every mechanical check; the defect was that
`gate1_report.py` never called any of it, so a quoteless row printed `"(no quote)"` and still
counted toward `distant`. `evaluate()` is the join: every row the report prints has been through
the same chain that decides the gate's exit code, and `is_valid()` / `is_primary_candidate()`
are the single place "is this row real" and "does it count" are decided.

That has to be true of the call graph, not only of the two functions existing.
`gate1_report.py`'s summary takes its printed `distant` figure from
`gate1.is_primary_candidate` directly rather than re-deriving "valid and distance == distant"
inline; the two were equivalent by construction, which is exactly why nothing forced them to
stay so.

## Classification is read

The Reuse Log's fourth column (`ENGMEM-SPEC.md` §4; `data-model.md`, "Reuse Log Entry") was
written by every save template and read by nothing, so a `harmful` row counted like a `reuse`
row. `exclusion_reason()` now treats `harmful`, `anti-reuse`, `missing` and `unrecognized` as
terminal exclusions, each under its own name in the verdict cell.

`anti-reuse` (`templates/engmem.save.md`, "Reuse Log rules") marks a row that genuinely
influenced the work — the prior document was opened, quoted, and the work deliberately went the
other way. It is still excluded because the §11 endpoint is conservative by construction: the
store is credited when it supplied the answer the work used, not a foil the work correctly
rejected. The two terminal classifications are not weighted against each other.

## Supersession is a flag, not a filter

`gate1_report.py` had no concept of a cited document the store itself marks replaced, so a
verbatim quote from one counted as clean distant reuse — the most credible-looking way to
inflate the endpoint, because the quote is genuine. It stays a visible flag rather than an
exclusion because `templates/engmem.save.md` makes a superseded citation `harmful` only when the
human says so at review time; auto-excluding it here would decide that question before it is
asked. A `verified` + `reuse` + `distant` row carrying `Staleness.CITED_SUPERSEDED` reaches
`is_primary_candidate() == True` and its verdict cell stays open.

Because the endpoint figure is quoted on its own, the summary also says how many of the counted
`distant` rows are stale, computed over the same `distant` list the endpoint comes from and not
over every row — a whole-table count would qualify the table, not the number that decides Gate 1.
The first tests for this could not tell the two scopes apart (the stale row was the only row in
both fixtures); the test that can adds a stale row excluded on another axis.

## Dogfooding identification

`ENGMEM-SPEC.md` §11 excludes stories about this repository and nothing implemented it. The
mechanism: the **citing** document's `repos` front-matter field names a member of
`DOGFOODING_REPOS` (`engmem`, this package's name in `pyproject.toml`), case-insensitively.

Rejected:

- **`covers_files`** names individual files and decays as the repository grows — every document
  written before a new file existed would need a retroactive edit to stay excluded. `repos` is a
  repository-level fact.
- **A deny-list keyed by document id** is a second source of truth a human must remember to
  update, disconnected from the document. `repos` is written in the same save-ritual step as the
  rest of the front matter and reviewed in the same YAML round.

`repos` was also already the field the adjacency check reads (§11's "shares a repository"),
so dogfooding adds no new parse.

Known gap: this trusts the human to write `engmem` into `repos`. A document whose `repos` is
empty or wrong is not excluded — the same trust boundary every other front-matter-derived fact
rests on.

An unreadable `repos` on the citing document reads as "not dogfooding": `_row_verdict` uses
`repos.get(citing.id) or set()`, which conflates `None` with an empty set. This cannot inflate
the endpoint, because the same lookup feeds `_distance()`, which returns `Distance.UNDECIDABLE`
whenever either side is `None` — the row is excluded from the count either way, under axis C's
name instead of dogfooding's.

## Front matter is parsed once

`_repos()` reads `repos:` without adding it to the `Doc` model (a loader change touching every
`Doc` consumer). It is the one place this module reads front matter outside
`spine.parse_document`, and that independence produced the same bug twice: a parse the tool
could not complete reading as "shares nothing" instead of "unknown", which scores `distant` —
the direction §11 says a parse failure must never work in.

Recorded reversal, the boundary search. The first `_repos()` found the closing delimiter with
`text.find("\n---", 3)`, while `spine.split_front_matter` requires a whole line that strips to
`---`. A YAML flow sequence continuing onto a line that begins `---` (`tags: [platform,` /
`---not-a-delimiter]`) is parsed correctly by spine and truncated by the substring search.
`load_store` already drops any document whose YAML fails outright, so every document reaching
`_repos()` has front matter spine parsed successfully; the two parsers had no business
disagreeing. `_repos()` now calls `spine.split_front_matter` for the boundary and
`yaml.safe_load` on the same text `spine.parse_document` parses.

Recorded reversal, list versus scalar. `{str(v) for v in value} if isinstance(value, list) else
set()` read a bare scalar (`repos: engmem`, the ordinary hand-written form) as "no
repositories", defeating both adjacency and dogfooding for the same row. `_repos()` now calls
`spine._coerce_list_field`, the rule every other list field already follows: a list reads as
before, a bare string degrades to a one-element set, `None`/absent reads as `set()`, and a
mapping or number raises `ValueError`, which `_repos()` turns into `None`. That mapping case
(`repos: {a: b}`, on a document that still loads because `repos` is not a spine field) is what
makes `Distance.UNDECIDABLE` reachable at all now that the boundary bug is closed.

## Draft and superseded citing documents

`load_store` returns every document regardless of `status`; search excludes `draft` and
`superseded` documents from its results (`scoring.py`, `_NEVER_PRIMARY`). Until the count did
the same, its population differed from the retrieval layer's, and a draft's Reuse Log is a
work-in-progress claim that has not been through the save ritual's review step.

`gate1_report.py`'s counted population excludes a row whenever the **citing** document's own
status is in `KNOWN_STATUSES_EXCLUDED_FROM_THE_COUNT` (`excluded_by_status()`). The row is still
printed, with the verdict cell filled as `"excluded: citing document status is <status>"`.

`verify_citations.py`'s population deliberately does not adopt this: its question is "is this
citation mechanically real", independent of lifecycle stage, and catching a bad quote while the
document is still a draft is strictly better than waiting for promotion to surface it.

Not closed by this: the **cited** document's status. Axis D already covers `superseded`; a
citation of a `draft` document is unaddressed.

## `Prior docs used: none.` conflicting with real rows

The Reuse Log is either exactly the sentence `Prior docs used: none.` or a table of rows
(`ENGMEM-SPEC.md` §6, `templates/engmem.save.md`). Both tools once skipped the whole section on a
substring match, so a half-edit that left the sentence next to real rows (start from the "none"
template, reuse something, forget the sentence) silently discarded every row and read as
"honestly reported no reuse".

`evaluate()` distinguishes three shapes: only the sentence (`none_reports`), only rows, or both
(`conflicts`). A conflicted document's rows go through the full verdict pipeline on their own
merits, and the conflict is reported loudly: `verify_citations.py` prints it and counts it in its
own footer figure, `gate1_report.py` prints a dedicated conflicts block and keeps the document out
of "documents reporting no reuse". Neither tool's exit code moves on a conflict alone — a bad
quote inside a conflicted section still fails `verify_citations.py`; a conflict without one does
not.

## Who fills the last column

`exclusion_reason(row)` returns `None` for exactly the rows the tool must leave open: axis A
`verified`, axis B `reuse`, not dogfooding, citing document not `draft`/`superseded` —
**regardless of axis C**. Distance is mechanically computed but human-overridable (§11: "the
human may overrule it per row, in writing"), so an `adjacent` or `undecidable` row that is
otherwise legitimate reuse stays open exactly like a `distant` one. Only population membership,
decided once and the same way for every row, gets a tool-written reason.

`is_valid(row)` is `exclusion_reason(row) is None`, and because axis E is unset for exactly these
rows, "N valid" and "rows awaiting human verdict" are one count, not two peers — an earlier
summary printed them as siblings and double-counted. `is_primary_candidate` is `is_valid AND
distance == Distance.DISTANT`, the strict subset §11 counts; the summary reads "N valid, of which
M distant", never two independent totals.

Precedence inside `exclusion_reason`: integrity, then the citing document's status, then
dogfooding, then classification — the `INTEGRITY_EXCLUSIONS` lookup, the two predicates, then
the `CLASSIFICATION_EXCLUSIONS` lookup — and the first applicable reason is the one printed, so a
draft citing document with a `harmful` dogfooding row prints only the status reason. Integrity
comes first because a row that never reached `verified` has no citation to classify or
attribute. The order is contract, pinned by a row that fails two axes at once. It is cosmetic,
not a masked count: the `classification` and `distance` columns still show the row's other facts,
and every summary counter is computed per axis across all rows, independent of this precedence.

The exclusion breakdown is not a partition. `dogfooding` and `excluded_by_status` are
independent per-row facts and can both hold for one row, so the per-axis figures may overlap and
do not sum to the rows excluded. The summary prints the true total, `len(rows) - len(valid)`,
before the breakdown, and the breakdown line says its figures may overlap.

## Deliberate limitation: current-text verification only

`verify_citations.py` checks a quote against the cited document's **current** body, so an edit
that removes the quoted passage makes a previously verified citation fail on the next run.
Verifying against history would need old revisions readable outside git or a git diff, both cut
by `ENGMEM-SPEC.md` §9. This is not stale-detection: a superseded document's current text is what
axis D already covers.

## The audit coverage block

`src/engmem/gate1_audit.py`, rendered by `tools/gate1_report.py` after the per-row table and its
summary. Telemetry knows `session_id` on every row; Gate 1 reads final documents; nothing joined
the two, so nobody could say how many sessions started the ritual, searched, and recorded either
a Reuse Log or `Prior docs used: none.` This is completeness control of the experimental sample,
not a schema gate: `gate1_audit.evaluate()` never raises, never changes `gate1_report.py`'s exit
0, and a document missing a section is a finding it reports, never an error.

### A second reader, not a wider `_Row`

`session_id` is written on every telemetry row by both channels (`telemetry.py`'s `log_search` /
`log_role_search`; the MCP side's blank-to-`None` rule is in `contracts/mcp-server.md`,
"`session_id`: optional to the schema, asked for in every wording"). But `_Row`, the type
`summarize()` totals, carries only `channel`, `result`, `context_bytes` and
`context_tokens_estimate`; `_read_row` never touches `session_id`, so `summarize()` structurally
cannot answer a per-session question. A reader defect, not a data gap.

`telemetry.SessionRow` / `telemetry.read_session_rows()` live in `telemetry.py`, next to `_Row`,
because a second parse of the same file format in a different module is the duplication that
diverges. Widening `_Row` was rejected: `summarize()`'s output is pinned byte-for-byte through
`engmem telemetry`, and a field only the audit needs has no business on the type the ordinary
reading surface totals. The two readers share `_load_json_object`, the decode-and-shape check,
so a decode rule cannot drift between them.

`read_session_rows` mirrors `summarize()`'s tolerance on purpose: a missing file is zero rows, a
file that cannot be read or decoded raises (`OSError`/`UnicodeDecodeError`), one malformed line is
skipped without poisoning its neighbours. Where it does not mirror: a row with no usable
`session_id` (absent, `null`, blank, non-string) is simply not returned, with no `unreadable`
counter of its own — that counter already exists under `engmem telemetry`'s name.

### A separate module, and exactly one figure from `verdicts`

`gate1.py` answers *is this row valid*; the audit answers *is the sample this analysis draws from
complete*, independent of whether any citation in it is any good. Different questions over an
overlapping input, and `gate1.py` already carries five axes in one dataclass.

`gate1_audit.evaluate()` takes `verdicts: gate1.Verdicts` as a parameter for one figure only,
`none_report_count`, which reuses `verdicts.none_reports` because that list already tells a clean
none-report from one conflicted by real rows; a second substring check would regress the bug
above and make this block disagree with the number printed a few lines earlier in the same
output.

Recorded reversal. An earlier version also derived "documents with a Reuse Log section" and
"active documents missing one" from `verdicts`, which answers "did `gate1.py` recognise a row or a
none-sentence here", not "is the section present": a `## Reuse Log` holding only the template's
header row and separator produced neither, so the document read as both not-present and missing.
`reuse_log_count` and `active_missing_reuse` are computed the way `active_missing_trace` always
was — `_has_role(doc, "reuse")`, section presence — and `verdicts` feeds `none_report_count`
alone. Everything else (status, section presence by role, the telemetry join) the audit computes
from `load_store`/`split_sections` directly, not through the per-row `RowVerdict` machinery that
also reads every document's `repos` from disk.

### What "a session document" is

Any file `sessions/*.md` that `load_store` parses into a `Doc`, of any status and however
degraded its spine. A file that fails to parse (duplicate id, invalid YAML, an `OSError`) is not a
`Doc`; it is already on `load_store`'s `errors` list, which neither module reads, and this block
does not count it a second time under another name.

A session document that never ran the ritual is still a `Doc` but not part of the ritual
population. Two things put it outside, and a document matching both is counted once, under the
backfilled figure: `backfilled: true` (`ENGMEM-SPEC.md` §4, "docs written after the fact"), and a
missing `## Pre-reg` section.

### The ritual figures

**(a) The ritual-start denominator is draft + active, not active alone.** An abandoned session
leaves exactly a draft, the population this block exists to notice. `AuditReport` carries
`draft_count`, `active_count` and `superseded_count` as three fields never summed inside the
module, so no wrong partial sum can leak; `gate1_report.py` prints `"N started (draft: D +
active: A) -> A completed"`. `superseded_count` is printed and named as counted in neither
figure, with the reason on the line: the pair answers "is the ritual currently incomplete", not
"did it ever finish", and hiding the count would look like silent loss.

**(d) A backfilled document never ran the ritual.** The three statuses were originally counted
over every `Doc`, backfilled ones included, which inflated both sides of the ritual figure and,
those documents being `active` far more often than not, pushed the completion ratio toward 1,
then listed the same ids under every "active documents missing ..." line as guaranteed noise.
`AuditReport.backfilled_count` is its own field, from `Doc.backfilled`, and the status triple and
the `active_missing_*` lists are computed over a population that excludes it. The ritual line
names the excluded count in the same sentence as `superseded_count`. This exclusion predates (e)
below, which narrowed the population again.

**(e) A document with no Pre-reg section never ran the ritual either.** Found in the live store
on 2026-09-11: seven documents written by an engmem whose save template had no `## Pre-reg` and
no `## Search Trace`, carrying no `backfilled: true` because the flag postdates them, so (d) did
not catch them and both sides of the ritual figure were inflated by seven — the countable-story
figure read 14 where the honest number was 3. The author stamped all seven that day; the audit
then stopped relying on the stamp, because the flag is a claim somebody has to remember to make
while the missing section is the evidence itself, readable by the same `_has_role` every other
figure uses. `ritual_docs` is `[d for d in docs.values() if not d.backfilled and _has_role(d,
"prereg")]`; the status triple and the `active_missing_*` lists follow from it. Recorded as the
§11 amendment of 2026-09-12.

Precedence is stated, not left to fall out: `backfilled_count` is computed first and unchanged,
and `no_prereg_docs` carries `not d.backfilled`, so a document matching both is counted once. A
document on both lines would read as two documents missing from the sample.

`AuditReport.no_prereg_docs` holds the ids, of any status; the ritual sentence names the count,
and the id line — the one a reader can act on, which is what finding the seven by hand lacked —
carries the precedence rule in its label: *"(any status; a backfilled document is counted on the
backfilled figure instead)"*. Without the parenthesis the line read `0` on the live store on
2026-09-12 and looked like "no such documents exist", when all seven were on the backfilled line
above.

Retired with it: `active_missing_prereg`. Once a Pre-reg section admits a document to the ritual
population, "active documents missing a Pre-reg section" is empty by construction, and a `0`
directly under a line reporting seven Pre-reg-less documents is a contradiction on the face of
the output. `no_prereg_docs` answers the question better, across every status, with ids.

Not closed by this: the primary endpoint's population. `gate1.py` is untouched and a Reuse Log
row from a document the audit excludes still counts as before. §11's "Left open, and named rather
than resolved" keeps that gap open deliberately — writing the exclusion into the endpoint with the
store's composition already known would be choosing a rule against visible data — and the
2026-09-12 amendment says so in the place that governs it.

### The telemetry figures

**(b) Search coverage is two named numbers, never one.** `telemetry_rows_with_session` and
`telemetry_distinct_sessions` are separate fields computed from the same `session_rows` two ways
(`len(session_rows)` versus the `dict` grouping by id), printed on one line by name, so "N
sessions" is never read where the number is rows. Searches-per-session is its own signal.

**(c) The cross-check: a Search Trace claiming a search ran, with no telemetry row to show for
it.** `## Search Trace` records `shell` / `paste` / `miss` (`ENGMEM-SPEC.md` §4;
`templates/engmem.start.md`, "5. Record the Search Trace"); `shell`/`paste` mean a search happened,
`miss` means it never did. A document whose trace is in `TRACE_RAN` with zero telemetry rows
carrying its id as `session_id` (within the window) is one whose retrieval provenance cannot be
reconstructed: the search never ran, or ran without `--session`. `TRACE_RAN` excludes `miss`,
which has nothing to reconstruct.

Recorded reversal, the population. An earlier draft narrowed this figure to `ritual_docs` on the
reasoning that a document outside the ritual made no claim on the ritual's terms. That confuses
two kinds of figure. The `active_missing_*` lists fire on an *absence* — a section the ritual asked
for and did not get — so a document the ritual never asked anything of is guaranteed noise there.
The cross-check fires on a *presence*: a trace reading `shell` is an affirmative claim the
document makes about itself, and a claim is owed evidence whoever made it. Narrowing printed `0`
where the honest answer was 1 and bought nothing for (e), whose seven documents have no Search
Trace at all. `unreconstructable_docs` stays over `docs.values()`, with neither exclusion.

Recorded reversal, the reader. `_trace_value` was once loosened from an exact whole-line match to
a word-by-word scan, on a non-blocking suggestion that a hand-edited `- shell` or `shell (via
MCP)` read as `None`. The scan then read a provenance claim out of prose in both directions: a
genuine `miss` document whose section explained *why* in a sentence containing "shell" returned
`shell`, and a genuine `shell` document with an earlier line mentioning `miss` dropped out of the
cross-check. Both are regressions on inputs the exact match read correctly, so it is reverted: a
line is exactly `shell`/`paste`/`miss` once whitespace and `.`/`:` are stripped, or it names
nothing. A finding marked non-blocking is a suggestion to weigh, and acting on one needs the same
failure-mode tests as any other behaviour change.

### Telemetry unreadable or undecodable does not fail the run

`telemetry.read_session_rows` raises `OSError`/`UnicodeDecodeError` by design, mirroring
`summarize()` (above): a file that exists and cannot be read is not silently zero, the
phantom-empty failure `cli.py`'s `_cmd_telemetry` handler also exists to prevent.
`telemetry.jsonl` is append-only and written by two concurrent processes (`cli.py` and
`mcp_server.py`), so a torn append or a stray byte is a realistic failure.

`gate1_audit.evaluate()` catches exactly that pair around its one `read_session_rows` call and
sets `AuditReport.telemetry_error`; every telemetry-derived field
(`telemetry_rows_with_session`, `telemetry_distinct_sessions`, `orphan_session_ids`,
`orphan_row_count`, `unreconstructable_docs`) stays at its empty default rather than being
computed against an empty read. The renderer prints `"UNMEASURED -- telemetry.jsonl unreadable
(<reason>), not zero"` in place of each, plus a standalone `UNREADABLE` line — the block's own
distinction between a stated gap and a false zero, applied to its own instrument.

The catch lives in `gate1_audit.py`, not `tools/gate1_report.py`, because the per-row table and
summary never open `telemetry.jsonl` and were never at risk. `main()` additionally prints the
table and summary before computing the audit block, so a different future failure inside
`gate1_audit.evaluate()` cannot take the §11 figure down with it — depth on top of the catch, not
a substitute.

### Two caveats stated in the output, not fixed

1. **`--session` is deliberately unvalidated** (`ENGMEM-SPEC.md` §5, search step 6: "nothing
   else is validated"). The orphan-row figure, "telemetry rows whose `session_id` matches no
   document **in this store**", therefore also counts typos and cross-store contamination, not
   only genuine gaps; the qualifier is printed verbatim in the block and is load-bearing.
2. **`telemetry.jsonl` is append-only with no rotation.** Pre-Gate-1 and testing rows are
   indistinguishable from experimental rows by anything this reader can see. `--since` (default
   unset, all-time, identical to every run before the flag existed) narrows every
   telemetry-derived figure including the cross-check, and the window in force is always the
   first line printed, so a quoted figure is never separated from its scope. An unparseable
   `--since` does not fail the run: it falls back to all-time and says so in the window line.
