# Contract: the Gate 1 verdict core

Source: `src/engmem/gate1.py`. Consumed by `tools/verify_citations.py` (exit-code gate) and
`tools/gate1_report.py` (human-readable table). This file exists because the module deciding
the pre-registered §11 endpoint is exactly where "why is this row excluded" most needs a
written answer, and because five requirements landed in the same review round, all inside the
same verdict, and none of them is safe to read in isolation from the other four.

## Why a third module, in `src/`, not a change to the two tools directly

`ENGMEM-SPEC.md` §9 is deliberately aggressive about new surface in `src/`. This module earns
the exception: it is the analysis layer for a pre-registered endpoint, not a product feature,
and it removes duplication that was actively dangerous — `QUOTE_RE`, `NONE_LINE`, and the row
parser were byte-identical in both tools before this round, which meant widening the quote
regex in one and not the other would make the two tools disagree about which rows exist.

The two tools stay separate rather than one importing the other, because their exit-code
contracts genuinely differ and both are test-pinned: `tests/test_verify_citations.py` asserts
`returncode != 0` in six places, `tests/test_gate1_report.py` asserts `returncode == 0`
unconditionally. A report that can fail is a report nobody runs before Gate 1 review — so
`gate1_report.py` exits 0 always, and `verify_citations.py`'s exit-1 condition is exactly the
citation-integrity axis (below) and does not grow to cover classification, staleness, or
dogfooding. Whoever is tempted to make the report a gate should read this paragraph first.

Whichever tool imported the other would also inherit its stdout format as an API —
`verify_citations`'s printing is prose for a human, not a data structure a second tool should
parse back apart. A shared library returning typed verdicts is the seam that avoids that.

## The five axes, and why they are not one enum

A single `status` enum collapses states that are reachable two different ways, or not at all
(a row can be `verified` and `classification: harmful` at once; a `no_quote` row can never be
`reuse` no matter what its classification cell says). `RowVerdict` keeps five independent
fields:

- **A — citation integrity** (`integrity`): `no_quote` -> `cited_missing` -> `quote_not_found`
  -> `verified`, evaluated in exactly that order because that is the only order in which each
  check has the data it needs — reused verbatim from `verify_citations.py`'s existing
  `continue` chain, not reimplemented.
- **B — classification** (`classification`): the author-declared `reuse` / `anti-reuse` /
  `harmful`, plus `missing` (the cell is blank, or the row is a 2- or 3-column table with no
  `classification` cell at all — `len(cells) < 4` no longer raises `IndexError` and no longer
  defaults to `reuse`) and `unrecognized` (present but misspelled). `missing` and
  `unrecognized` are separate counters, not merged into one "invalid" bucket, so a typo in the
  Reuse Log is fixable rather than invisible in the summary.
- **C — distance** (`distance`): `adjacent` / `distant` / `undecidable`. Computed only once
  axis A reaches `verified` — a `quote_not_found` row has nothing to be distant *from*, and
  printing `"cited doc not in store"` into a column labelled "distance" (the pre-existing bug)
  was a citation-integrity fact wearing axis C's label.
- **D — staleness** (`staleness`): `cited_active` / `cited_superseded`, computed whenever the
  cited document resolves in the store, independently of axis A or C. A citation can be both
  `verified`, `distant`, *and* `cited_superseded` at once — staleness never gates anything by
  itself (see "Supersession" below).
- **E — human verdict**: the `changed a decision?` cell. The only axis this module never
  writes for an eligible row (see "Who fills the last column").

## Citation integrity — the join (requirement 1)

`verify_citations.py` already implemented every mechanical check (no quote, cited doc absent,
quote absent from the cited body, with whitespace normalisation). The defect was that
`gate1_report.py` never called any of it — a quoteless row got the string `"(no quote)"` and
was still appended to the report and still counted toward `distant`. `evaluate()` is the join:
every row that reaches `tools/gate1_report.py` has already been through the same integrity
chain `tools/verify_citations.py` uses to decide its exit code, and `is_valid()` /
`is_primary_candidate()` are the single place both "is this row real" and "does it count"
are decided. `ENGMEM-SPEC.md:650-652`'s claim that a quoteless row is rejected "and so does
the count" is now true because there is exactly one count, computed from exactly one chain.

**ARCH-003.** "The single place ... does it count are decided" is a claim about the code, not
just the intent, so it must be true of the call graph, not only of the two functions existing.
`gate1_report.py`'s summary computes its printed `distant` figure by filtering `rows` through
`gate1.is_primary_candidate` directly (`distant = [r for r in rows if
gate1.is_primary_candidate(r)]`) rather than re-deriving "valid and distance == distant"
inline — the two were equivalent by construction even before this was fixed, which is exactly
why the drift was easy to miss: nothing forced them to stay equivalent as either definition
changed. `tests/test_gate1_report.py::
test_the_printed_distant_figure_equals_sum_of_is_primary_candidate` pins the wiring, not just
the arithmetic.

## Classification is read (requirement 2)

`cells[3]` (the Reuse Log's fourth column, `ENGMEM-SPEC.md` §4 / `data-model.md`'s Reuse Log
Entry table) was written by every save-template row and read by nothing. A `harmful` row
counted identically to a `reuse` row. It no longer does: `exclusion_reason()` treats
`harmful`, `anti-reuse`, `missing`, and `unrecognized` as terminal exclusions from the primary
count, each under its own name in the printed verdict cell.

`anti-reuse` (`templates/engmem.save.md`'s Reuse Log rules) marks a row that genuinely
influenced the work: the prior document was opened, quoted, and the work deliberately went
the other way because of it. That is still excluded from the primary count, and
deliberately so — the endpoint (`ENGMEM-SPEC.md` §11) is conservative by construction,
crediting the store only when it supplied the answer the work used, not when it supplied a
foil the work correctly rejected. `exclusion_reason()` does not distinguish the two
terminal classifications by weight; both print under their own name and neither passes
`is_primary_candidate()`.

## Supersession is a flag, not a filter (requirement 3)

`verify_citations.py` already detected a citation of a `superseded` document and correctly
did not fail the run on it. `gate1_report.py` had no concept of it at all, so a row quoting
verbatim from a document the store itself marks as replaced counted as a clean distant reuse
event — the reviewer who found this called it the most credible-looking way to inflate the
primary endpoint, because the quote is genuine.

Per `templates/engmem.save.md`'s own instruction to the save ritual, a superseded citation
becomes `harmful` **only when the human says so** at review time. Auto-excluding it here would
make that human judgment moot before it is ever asked, so staleness stays a *visible flag*
(`staleness == "cited_superseded"`) that a `verified` + `reuse` + `distant` row can still carry
straight through to `is_primary_candidate() == True`. The `changed a decision?` cell stays open
for exactly this row — the tool names the fact, the human makes the call.

**ARCH-002.** That backstop only works if a reviewer actually opens the per-row table. The
primary-endpoint figure in `gate1_report.py`'s summary is quoted on its own, so the summary
now also names how many of the counted `distant` rows are stale: `"of the N distant, M cite a
superseded document (flagged, not excluded -- see the staleness column)"`, computed over the
same `distant` list the endpoint figure comes from (see ARCH-003, immediately below) -- not
over every valid row, so it qualifies the number that actually decides Gate 1 rather than the
whole table.

**ARCH-007.** The scoping in the paragraph above -- `distant`, not `rows` -- is the entire
content of ARCH-002's requirement, and the first round of tests for it could not tell the two
apart: both fixtures happened to have the stale row be the only row, so a whole-table count and
a distant-scoped count agreed by coincidence. `tests/test_gate1_report.py::
test_a_stale_citation_excluded_from_the_count_does_not_inflate_the_stale_distant_figure` adds a
row that is `verified` and cites a `superseded` document but is excluded from the count on
another axis (`classification: harmful`), so `distant == 0` while a whole-table count of
staleness on that row would be `1`. Only the distant-scoped implementation passes it -- an
unscoping mutation (`distant` -> `rows` in the `sum(...)` above) is killed by this test alone
and by no other, which is the property this paragraph claims and the earlier tests did not
prove.

## Dogfooding identification (requirement 4)

`ENGMEM-SPEC.md:645-647`: "Stories about this repository are excluded from the count." Nothing
implemented it. **Chosen mechanism: the `repos` front-matter field contains `engmem`** (this
project's own package name, `pyproject.toml`), case-insensitively, on the *citing* document.

Why `repos` and not the alternatives considered:

- **`covers_files`** names individual files. It decays as the repository grows — a new file
  added to `engmem` carries no signal that it, too, needs marking, and every existing document
  written before that file existed would need a retroactive edit to stay excluded. `repos` is
  a repository-level fact and does not decay with file moves or additions.
- **An explicit deny-list keyed by document id** is a second source of truth a human must
  remember to update at save time, disconnected from the document it describes. `repos` is
  filled in the *same* step (`/engmem.save`'s front-matter draft) that already writes the rest
  of the document, so there is one write, not two to keep in sync.
- **`repos` was already being parsed.** `gate1_report.py`'s pre-existing `_repos()` reads this
  exact field for the adjacency check (`ENGMEM-SPEC.md` §11's "shares a repository" test).
  Reusing that output for dogfooding, rather than adding a new field or a new parse, keeps the
  parsing surface in one place — the thing requirement 5, below, is also about.
- **Auditability.** `repos` is a field the human already reviews and confirms during the save
  ritual's single YAML round (`ENGMEM-SPEC.md` §6, "more than one interactive round is a
  design failure"). A story wrongly marked (or wrongly *not* marked) dogfooding is visible and
  correctable at that point, not inferred by this tool from something less legible.

**Known limitation, recorded rather than hidden**: this relies on the human writing `engmem`
into `repos` for a story about this repository. A document whose `repos` list is empty or
wrong is not excluded. This is the same trust boundary every other front-matter-derived fact
in this store already rests on (`classification`, `status`), not a new one.

**An `_repos()` failure on the citing document reads as "not dogfooding", not as an error.**
`_row_verdict` computes `dogfooding` from `repos.get(citing.id) or set()`, which conflates
`None` (unreadable) with `set()` (readable, empty) -- an unreadable `repos` on the citing
document is silently treated as "does not name this repository". This cannot inflate the
primary endpoint: the identical lookup also feeds `_distance()`, which returns `"undecidable"`
whenever either side is `None` (see "Front matter is parsed once," below), so a row whose
dogfooding status could not be determined can also never be `"distant"` -- it is excluded from
the primary count either way, just under axis C's name rather than axis 4's.

## Front matter is parsed once (requirement 5, hardened by ARCH-001 / ARCH-004)

`_repos()` reads the `repos:` front-matter value without adding it to the `Doc` model (out of
scope — a loader change touching every `Doc` consumer). It is the one place in this module
that reads front matter independently of `spine.parse_document`, and two review rounds each
found a real bug in that independence — both had the same shape as requirement 5's original
defect: a parse the tool cannot complete reading as "shares nothing" instead of "unknown."

**The boundary search (ARCH-004, since fixed).** The first version of `_repos()` found the
front-matter closing delimiter with its own substring search, `text.find("\n---", 3)` — four
literal characters — while `spine.split_front_matter` requires a *whole line* that strips to
exactly `"---"`. A YAML flow sequence spanning lines legitimately continues a value onto a line
beginning with `"---"` followed by more characters (e.g. `tags: [platform,` /
`---not-a-delimiter]`) — `spine`'s line-based check correctly skips past it and finds the real
closing delimiter further down, while the substring search stopped there and handed
`yaml.safe_load` a truncated, syntactically broken fragment. This divergence was, at the time,
the *only* way `_distance()` could reach `"undecidable"` at all: `load_store` already drops any
document whose front-matter YAML fails outright (`spine.py:445-449`), so every document that
reaches `_repos()` has front matter `spine.parse_document` already parsed successfully — the
two parsers had no business disagreeing, and doing so was a defect of `_repos()`'s private
boundary rule, not evidence that "undecidable" needed two independent parsers to justify it.
`_repos()` now calls `spine.split_front_matter` directly for the boundary and `yaml.safe_load`
on the exact text `spine.parse_document` parses — there is no second boundary rule left to
diverge. `tests/test_gate1.py::test_the_flow_sequence_edge_case_no_longer_diverges_from_spine`
and its `test_gate1_report.py` counterpart pin the document that used to trigger this at its
correct, real `distant`/`adjacent` value — proving the false negative is gone, not merely
asserting the old (wrong) `"undecidable"` reading.

**The list-vs-scalar rule (ARCH-001, since fixed).** Closing the boundary bug removed one route
to a false `"distant"` but left another, load-bearing for both distance *and* dogfooding: the
original line `{str(v) for v in value} if isinstance(value, list) else set()` read a bare YAML
scalar (`repos: engmem`, not `repos: [engmem]`) as `set()` — "this document names no
repositories" — the same "unreadable reads as absent" shape, reachable the ordinary way a human
writes the field by hand, not a crafted edge case. `spine._coerce_list_field` already handles
this for every other list field (`tags`/`entities`/`related`/`covers_files`), with a warning
`gate1.py` cannot see because `repos` is not a spine field. `_repos()` now calls
`_coerce_list_field` directly: a `list` reads as before, a bare `str` degrades to a
one-element set exactly like every other list field, `None`/absent reads as `set()`, and
anything else (a mapping, a number — neither a list nor a string) raises `ValueError`, which
`_repos()` turns into `None`. This is also what makes `"undecidable"` genuinely reachable again
now that ARCH-004 closed its previous, accidental route:
`tests/test_gate1.py::test_unparseable_front_matter_is_undecidable_never_distant` now
constructs a `repos: {a: b}` mapping — the one shape `_coerce_list_field` itself cannot read,
on a document that still loads into the store fine (`repos` is not a spine field, so nothing
about loading the document depends on it).

Both fixes mattered for dogfooding (requirement 4, above) as much as for distance: a citing
document with `repos: engmem` (the scalar form) used to defeat the dogfooding exclusion for
the identical reason it defeated adjacency — `tests/test_gate1.py::
test_a_scalar_repos_value_makes_dogfooding_reachable` and
`test_a_scalar_repos_value_still_makes_a_shared_repo_adjacent` pin both, at both the unit and
the `gate1_report.py` table level, since the printed distant figure is what §11 counts.

## Draft and superseded citing documents (their own Reuse Log rows)

`load_store` returns every document regardless of `status`; search results exclude `draft` and
`superseded` documents (`ENGMEM-SPEC.md` §5.2, `data-model.md`'s state table). Before this
round neither tool filtered, so the report's counted population and the retrieval layer's
served population differed — a draft's Reuse Log is a work-in-progress claim about a document
that has not yet gone through the save ritual's review step.

**Decision**: `gate1_report.py`'s counted population excludes a row whenever the **citing**
document's own `status` is `draft` or `superseded` (`excluded_by_status()`), mirroring §5.2's
exclusion exactly. The row is still printed (visibility — nothing is silently dropped) with
`changed a decision?` filled in as `"excluded: citing document status is <status>"`.

`verify_citations.py`'s population is **unchanged** and deliberately does not adopt this
exclusion: its job is "is this citation mechanically real," independent of the citing
document's lifecycle stage. Catching a fabricated or mismatched quote while the document is
still a draft — before it goes active — is strictly better than waiting for promotion to
surface it; excluding drafts from the mechanical check would let a bad quote survive
undetected until the ritual's own review step, which the check exists to catch problems
*before*. `tests/test_verify_citations.py::test_a_drafts_own_citations_are_still_checked`
pins this.

Only the **cited** document's `status` is out of scope here (axis D, staleness, already covers
`superseded`; a citation of a `draft` document is not addressed by any of the five
requirements and is listed as out of scope in the implementation report, not fixed here).

## `Prior docs used: none.` conflicting with real rows

The Reuse Log section is supposed to be *either* exactly the sentence `Prior docs used: none.`
*or* a table of rows — never both (`ENGMEM-SPEC.md` §6, `templates/engmem.save.md`). Before
this round, both tools did `if NONE_LINE in reuse.body.casefold(): continue` — a substring
test that skipped the *entire* section whenever the sentence appeared anywhere in it. A
half-edit that left both the sentence and real rows in the same section (plausible: an author
starts from the "none" template, then reuses something and forgets to delete the sentence)
silently discarded every row in both tools, with no warning. The document then read as
"honestly reported no reuse" while carrying rows that were never checked or counted.

**Decision**: `evaluate()` now distinguishes three shapes per document's Reuse Log section —
only the sentence (`none_reports`, unchanged behaviour), only rows (processed normally), or
both (`conflicts`, a new, third outcome). A conflicted document's rows are **not** skipped —
they go through the full verdict pipeline like any other row, verified or not on their own
merits — and the conflict itself is reported loudly: `verify_citations.py` prints
`"<doc>: Reuse Log has both 'Prior docs used: none.' and N row(s) ..."` and counts it in its
own footer figure (not folded into "citing a superseded document"); `gate1_report.py` prints a
dedicated "Reuse Log conflicts" block after the table and excludes the document from
"documents reporting no reuse" (it did not honestly report none). Neither tool's exit code is
affected by a conflict alone — a bad quote inside a conflicted section still fails
`verify_citations.py`; a conflict with no bad quote does not.

## Who fills the last column

`exclusion_reason(row)` returns `None` for exactly the rows the tool must leave open: axis A
`verified`, axis B `reuse`, not dogfooding, and the citing document not `draft`/`superseded` —
**regardless of axis C**. Distance is mechanically computed but human-overridable
(`ENGMEM-SPEC.md` §11: "the human may overrule it per row, in writing"), so an `adjacent` or
`undecidable` row that is otherwise a legitimate reuse event stays open for the human exactly
like a `distant` one — the tool does not pre-empt that judgment by writing "excluded: adjacent"
into the cell. Only a row that is not `(verified AND reuse)`, or is dogfooding, or is a
draft/superseded document's own row, gets a tool-written reason: those are not per-row human
calls, they are population membership, decided once, the same way for every row.

`is_valid(row)` = `exclusion_reason(row) is None` — this is the "N valid" figure and, since
axis E is unset for exactly these rows, also "rows awaiting human verdict"; the two are the
same count, not two peers reported separately (an earlier draft of the summary line printed
them as siblings, which double-counts the same rows under two names). `is_primary_candidate`
= `is_valid AND distance == "distant"` is the strict subset `ENGMEM-SPEC.md` §11 actually
counts — the summary line reads "N valid ..., of which M distant," never both as independent
totals.

**ARCH-005 — precedence inside `exclusion_reason`.** The `if` chain checks integrity, then the
citing document's own status, then dogfooding, then classification, in that order, so the
FIRST applicable reason is the one printed — a draft citing document with a `harmful`-and-
dogfooding row prints only `"excluded: citing document status is draft"`. Integrity comes
first because a row that never reached `verified` cannot meaningfully be "reuse" or
"dogfooding" at all — there is no citation to classify or attribute yet. This is cosmetic, not
a masked count: the `classification` and `distance` columns still show the row's other facts
on the same line regardless of which reason won the verdict cell, and every summary counter
(citation integrity, classification, dogfooding, citing-document status) is computed per axis
across all rows, independently of `exclusion_reason`'s precedence.

**ARCH-006 — the exclusion breakdown is not a partition.** `dogfooding` and
`excluded_by_status` are independent per-row facts and can both be true of the same row (a
draft that also names `engmem` in `repos`), so `"N dogfooding (...), M citing document not
active (...)"` are not disjoint counts and do not sum to the number of rows actually excluded.
The summary prints `"rows excluded from the count: {len(rows) - len(valid)}"` — a true total,
the complement of "N valid" — immediately before the per-axis breakdown, and the breakdown's
own line says explicitly that its two figures may overlap and are not the total.
`tests/test_gate1_report.py::
test_a_row_excluded_on_two_axes_at_once_is_not_double_counted_in_the_total` constructs exactly
that row (draft + dogfooding) and pins the total at 1, not 2.

## Deliberate limitation, not a bug: current-text verification only

`verify_citations.py` checks a quote against the cited document's **current** body text. An
edit that later removes or rewords the quoted passage makes a previously-verified citation fail
on a subsequent run. This is correct v0.1 behaviour — verifying against history would require
either keeping old revisions readable outside git or diffing against git, both explicitly out
of scope (`ENGMEM-SPEC.md` §9: no FRESH/STALE checks via git diff) — and it is **not**
stale-detection: a superseded document's *current* text is exactly what axis D's staleness flag
already covers. This boundary was already reasoned about for supersession at
`verify_citations.py`'s comment above the staleness check; this is the same boundary, not a
new one, extended to cover an ordinary content edit as well as a formal supersession.

## The audit coverage block

`src/engmem/gate1_audit.py`, rendered by `tools/gate1_report.py` as a second block after the
per-row table and its summary. Telemetry knows `session_id` on every row; Gate 1 reads final
documents; nothing joined the two, so nobody could say how many sessions started the ritual,
searched, and honestly recorded either a Reuse Log or `Prior docs used: none.` This is
**completeness control of the experimental sample**, not a new mandatory schema gate --
`gate1_audit.evaluate()` never raises (ARCH-101: even `telemetry.jsonl` itself being unreadable
or undecodable degrades to an explicitly UNMEASURED figure, not an exception -- see "Telemetry
unreadable or undecodable does not fail the run," below), it never changes `gate1_report.py`'s
unconditional exit 0, and a document missing a section is a finding this block reports, never
an error anything raises.

### Why this needed a reader, not a schema change

`session_id` is already written on every telemetry row (`telemetry.py`'s `log_search` /
`log_role_search`; the MCP path normalises blank to `None` identically, `mcp_server.py`, with a
comment that both paths log the same key). But `_Row` -- the type `summarize()` totals -- carries
only `channel`, `result`, `context_bytes`, `context_tokens_estimate`; `_read_row` never touches
`session_id`. `summarize()` therefore structurally cannot answer a per-session question, no
matter how the data is queried. This is a **reader defect**, not a data gap: the fix is a second
reader over the same file, needing no schema change and no change to what is written.

### A second reader, not a wider `_Row`

`telemetry.SessionRow` / `telemetry.read_session_rows()` sit in `telemetry.py`, next to `_Row` /
`_read_row` / `summarize()`, not inside `gate1_audit.py`: they read the same file format those
three already own, and a second, independent parse of that format in a different module is
exactly the duplication-that-diverges risk this project's own `gate1.py` contract (above) was
written to name. Widening `_Row` itself was rejected -- `summarize()`'s callers (`engmem
telemetry`'s output) are pinned byte-for-byte, and a field only the audit needs has no business
riding on the type the ordinary telemetry reading surface totals. `_read_row` and
`read_session_rows` do share one thing: `_load_json_object`, the decode-and-shape-check that
used to live inline in `_read_row` alone. Extracting it is behaviour-preserving (verified: the
CLI's `engmem telemetry` output was diffed byte-for-byte on the fixture store before and after)
and removes the one place a decode rule could otherwise drift between the two readers.

`read_session_rows` mirrors `summarize()`'s own tolerance rules on purpose, not by
coincidence -- the two readers make the *same* missing/unreadable/malformed-row distinctions on
the *same* file, so an author who already understands one understands the other: a missing file
is zero rows; a file that cannot be read or decoded raises (`OSError`/`UnicodeDecodeError`); one
malformed line is silently skipped, never raised, never poisoning the rows around it. Where it
deliberately does not mirror `summarize()`: a row with no usable `session_id` (absent, `null`,
blank, non-string) is simply not returned, with no `unreadable` counter of its own -- that
counter already exists, under `engmem telemetry`'s own name, and duplicating it here would
answer a question this block does not ask.

### Where the audit computation lives, and why not `gate1.py`

`src/engmem/gate1_audit.py`, a new module, not a new set of functions inside `gate1.py`.
`gate1.py` answers *is this Reuse Log row valid* -- citation integrity, classification,
distance, staleness, per row. The audit answers *is the sample this analysis draws from
complete* -- did the session run the ritual at all, independent of whether any citation in it
is any good. These are different questions over an overlapping input (`load_store`,
`split_sections`, and -- for exactly one figure -- `gate1.Verdicts`), and `gate1.py` already
carries five independent axes in one dataclass; a sixth, orthogonal question bolted onto the
same module would be exactly the "mixed unrelated responsibilities" this project's own
engineering discipline forbids. The two tools that render Gate 1 output already import both
`gate1` and (now) `gate1_audit` side by side; nothing is hidden by the split.

`gate1_audit.evaluate()` takes `verdicts: gate1.Verdicts` as a parameter rather than calling
`gate1.evaluate()` a second time internally, for exactly **one** figure: `none_report_count`,
"sessions honestly reporting `Prior docs used: none.`," which reuses `verdicts.none_reports`
verbatim -- it already knows the difference between a clean none-report and one *conflicted* by
real rows sitting alongside it (see "`Prior docs used: none.` conflicting with real rows,"
above); re-deriving that with a second, simpler substring check would silently regress exactly
the bug that section of this file already fixed once, and would make this block's "documents
reporting no reuse" disagree with the number already printed a few lines above it in the same
tool's own output.

**ARCH-102, and why "exactly one figure" is load-bearing, not incidental.** An earlier version
of this module also derived "documents with a Reuse Log section" and "active documents missing
a Reuse Log section" from `verdicts` (`{r.citing.id for r in verdicts.rows} | none_reports |
conflicts`), which answers "did `gate1.py` recognise a row or a none-sentence here," not
"is the section PRESENT" -- a `## Reuse Log` holding only the template's own header row and
separator (`templates/engmem.save.md:162-163`) is present but produces neither, so that
document read as simultaneously *not* "with a Reuse Log section" and "missing" one, the false
sentence the header-only case letting a reviewer stop trusting the block. `reuse_log_count` and
`active_missing_reuse` are now computed the same way their sibling `active_missing_trace`
always was -- `_has_role(doc, "reuse")`, section presence, nothing to
do with `verdicts` -- and `verdicts` genuinely feeds only `none_report_count` again.
`tests/test_gate1_audit.py::test_a_header_only_reuse_log_counts_as_present_not_missing` pins the
header-only case at both ends: present in the count, absent from the missing list.

Everything else the audit needs (document status, section presence by role, the telemetry join)
`gate1_audit.py` computes for itself directly from `load_store`/`split_sections`, deliberately
*not* through `gate1.evaluate()`'s heavier per-row `RowVerdict` machinery (which also reads each
document's `repos` front matter from disk for dogfooding/distance, work this block has no use
for) -- reuse where the correctness already lives, direct computation everywhere else, rather
than paying for or duplicating a pipeline built to answer a different question.

### What "a session document" is

Precisely: any file `sessions/*.md` that `load_store` parses into a `Doc`, of any lifecycle
`status` and however degraded its spine -- regardless of whether it went on to carry a Reuse Log,
a Search Trace, or anything else. **Not every document in the store is one of these**: a file
that errors during parsing -- duplicate id, invalid YAML, an `OSError` reading the file itself --
is not a `Doc` at all; it is already surfaced through `load_store`'s own `errors` list (which
neither `gate1.py` nor `gate1_audit.py` reads or duplicates), and this block does not count it a
second time under a different name. `tests/test_gate1_audit.py::
test_a_document_that_fails_to_parse_is_not_counted_in_any_status_figure` pins this: a duplicate
id must not inflate the draft/active/superseded population.

**A session document that never ran the ritual is still a `Doc`, but is not part of the ritual
population.** Two things put it outside. `backfilled: true` (`ENGMEM-SPEC.md` §4: "docs written
after the fact") means the document was written after the work, never had a Pre-reg baseline, and
never ran a search through this ritual -- see "(d) A backfilled document never ran the ritual,"
below. A missing `## Pre-reg` section says the same thing about a document nobody stamped -- see
"(e) A document with no Pre-reg section never ran the ritual either," below. A document matching
both is counted once, under the backfilled figure.

### The three corrections to the naive counter list

**(a) The ritual-start denominator is draft + active, not active alone.** "Active session
documents" undercounts ritual starts, because an *abandoned* session leaves exactly a draft --
the population this block exists to notice. `AuditReport` carries `draft_count`, `active_count`,
and `superseded_count` as three independent fields (never summed inside `gate1_audit.py` itself,
so no wrong partial sum can leak out of the module); `gate1_report.py`'s own rendering computes
`started = draft_count + active_count` and prints `"N started (draft: D + active: A) -> A
completed"`. `superseded_count` is printed too, explicitly named as counted in *neither* figure,
with the reason stated in the line itself: this counter answers "is the ritual currently
incomplete," not "did it ever finish" -- a superseded document did complete the ritual once, and
hiding that count would look like silent data loss rather than a stated scope choice.
`tests/test_gate1_report.py::
test_the_audit_block_excludes_superseded_from_both_ritual_figures` uses a fixture carrying all
three statuses at once, specifically so a regression that folds `superseded` into `started`
cannot hide behind a fixture where the superseded count happens to be zero.

**(b) Telemetry search coverage is two named numbers, never one.** `telemetry_rows_with_session`
and `telemetry_distinct_sessions` are separate `AuditReport` fields, computed from the same
`session_rows` list two different ways (`len(session_rows)` versus `len(sessions_by_id)`, the
`dict` grouping by id) rather than one being derived by dividing the other -- both are printed
on the same line, by name, so a reader can never read "N sessions" when the number is actually
rows. Searches-per-session is its own signal (a session with many searches and one with none are
both invisible behind a bare total), which is exactly why both figures are kept, not collapsed.

**(c) The free cross-check: a Search Trace claiming a search ran, with no telemetry row to show
for it.** Every document's `## Search Trace` section records `shell` / `paste` / `miss`
(`ENGMEM-SPEC.md` §4, `templates/engmem.start.md` step 5) -- `shell`/`paste` both mean a search
actually happened; `miss` means it never did. A document whose trace is `shell` or `paste` but
for which zero telemetry rows carry its own id as `session_id` (within whatever window is in
force) is one whose retrieval provenance cannot be reconstructed -- the search either never ran,
or ran without `--session`. This is a set intersection over data every earlier round of this
work already collects: `TRACE_RAN = {"shell", "paste"}` deliberately excludes `miss`, which has
nothing to reconstruct and is not a gap -- `tests/test_gate1_audit.py::
test_a_miss_trace_with_no_telemetry_row_is_not_flagged` pins the exclusion; the mutation that
folds `miss` into the flagged set (`_trace_value(d) is not None` in place of `_trace_value(d) in
TRACE_RAN`) was proven to fail exactly that test.

**ARCH-106/ARCH-107 -- a P3 suggestion, tried, and reverted.** ARCH-106 (non-blocking) observed
that `_trace_value`'s exact whole-line match reads a hand-edited `- shell` or `shell (via MCP)`
as `None`, and proposed loosening it to scan the section word-by-word for the first token that
reduces to `shell`/`paste`/`miss`, wherever it fell on the line. That loosening shipped, and
ARCH-107 (P1) then reproduced the predictable failure of matching *anywhere in free text*: a
document whose Search Trace genuinely said `miss` also happened to mention "shell" in a
sentence explaining *why* it was a miss, and the word-by-word reader returned `shell` -- the
exact false claim `TRACE_RAN` exists to keep out. The same mechanism worked in reverse too: a
document that genuinely ran `shell`, with an earlier line merely mentioning `miss`, silently
dropped out of the cross-check. Both directions are real production-behaviour regressions on
inputs the previously approved exact-match version read correctly; the P3 that motivated the
loosening did not justify either one. `_trace_value` is reverted to the original exact
whole-line match (stripped of only a trailing `.`/`:`) -- a line is either EXACTLY
`shell`/`paste`/`miss`, or it names nothing, with no route from prose that merely mentions a
vocabulary word to a false match in either direction. The two ARCH-106 tests were deleted with
the loosening; five ARCH-107 tests now pin both failure directions plus the two-words-on-one-
line case, at both the `_trace_value` level (`tests/test_gate1_audit.py`) and end-to-end through
the printed provenance line (`tests/test_gate1_report.py`). Lesson recorded because it is worth
repeating: a finding marked non-blocking is a suggestion to weigh, not a task to execute, and
acting on one needs the same failure-mode tests as any other behaviour change.

**(d) A backfilled document never ran the ritual (ARCH-103).** `draft`/`active`/`superseded`
were originally counted over every `Doc` in `sessions/`, including `backfilled: true` documents
-- which never started the ritual, never ran a search, never had a Pre-reg baseline
(`ENGMEM-SPEC.md` §4). Counting them inflated both the started and completed sides of the ritual
figure and, being `active` far more often than not, pushed the completion ratio toward 1 --
making an incomplete sample look *more* complete than it is, then listing the same ids under
every "active documents missing ..." line as guaranteed noise. `AuditReport.backfilled_count` is
its own field, computed from `Doc.backfilled` (already parsed, `spine.py`) rather than silently
dropped or silently folded into either side; `draft`/`active`/`superseded` and the
`active_missing_*` lists are all computed over a population this exclusion removes the document
from -- `[d for d in docs.values() if not d.backfilled]` as of ARCH-103, narrowed again by (e)
below, which quotes the current predicate. `gate1_report.py`'s ritual line names the excluded
count in the same sentence as
`superseded_count`, on the same principle: a stated scope choice, not silent data loss.
`tests/test_gate1_audit.py::test_backfilled_documents_are_excluded_from_the_ritual_figures` and
`::test_backfilled_documents_are_excluded_from_the_missing_section_lists` pin both halves.

**(e) A document with no Pre-reg section never ran the ritual either.** Found in the live store
on 2026-09-11: seven documents written by an engmem version whose save template had no
`## Pre-reg` and no `## Search Trace` section at all. They carried no `backfilled: true` -- the
flag postdates them -- so (d) did not catch them, and they were counted as ritual documents. Both
sides of the ritual figure were inflated by seven, and the countable-story figure read 14 where
the honest number was 3. The correction had to be made by hand: the author stamped all seven
`backfilled: true` that day. Relying on that stamp is the part worth not repeating: the flag is a
claim somebody has to remember to make, while the missing
section is the evidence itself, sitting in the document, readable by the same `_has_role` every
other figure in this block already uses. `ritual_docs` is now `[d for d in docs.values() if not
d.backfilled and _has_role(d, "prereg")]`, and the draft/active/superseded triple and the
`active_missing_*` lists follow from it. The provenance cross-check does not -- see "What does
not move with them," below.

*Precedence, and why it is stated rather than left to fall out.* The two exclusions overlap on
exactly the seven documents above. `backfilled_count` is computed first and unchanged;
`no_prereg_docs` carries `not d.backfilled` so a document matching both is counted once, under
the backfilled figure. A document on both lines would read as two documents missing from the
sample, which is the same arithmetic error in the other direction.
`tests/test_gate1_audit.py::test_a_backfilled_document_with_no_prereg_section_is_counted_once_as_backfilled`
pins it; so does `tests/test_gate1_report.py::
test_a_backfilled_document_does_not_inflate_the_ritual_figures` end to end.

*Counted and named, never dropped.* This block exists to show what the sample is made of, so a
document leaving the ritual population has to leave visibly. `AuditReport.no_prereg_docs` holds
the ids, of any status; `gate1_report.py` names the count in the ritual sentence itself, in the
same clause shape `superseded_count` and `backfilled_count` already use, and prints the ids on
their own line where the retired Pre-reg line used to sit. The two lines do different jobs: the
ritual sentence accounts for the population it just reported, the id line is the one a reader can
act on -- which is precisely what was missing on 2026-09-11, when finding the seven documents was
manual work.

The id line's label carries the precedence rule in it (ARCH-004): *"session documents with no
Pre-reg section, excluded from the ritual population (any status; a backfilled document is
counted on the backfilled figure instead)."* Without that parenthesis the line reads `0` on the
live store today and looks like "no such documents exist," when what it means is that all seven
are on the backfilled line above. A figure whose zero has a second meaning has to say so on the
line, not in this file.

*What does not move with them: `unreconstructable_docs` (ARCH-001).* An earlier draft of this
round narrowed the cross-check to `ritual_docs` on the reasoning that a document outside the
ritual made no claim on the ritual's terms. That reasoning was wrong, and the difference is worth
stating because the two kinds of figure look alike from a distance. The `active_missing_*` lists
fire on an *absence* -- a section the ritual asked for and did not get -- so a document the ritual
never asked anything of produces guaranteed noise there, which is why (d) drops backfilled
documents from them. The cross-check fires on a *presence*: a `## Search Trace` reading `shell` or
`paste` is an affirmative claim the document makes about itself, and a claim is owed evidence
whoever made it and whenever. A backfilled document whose trace says `shell` with no telemetry row
naming it is exactly the gap this figure exists to print; narrowing the population printed `0`
where the honest answer was 1, and it bought nothing for the defect (e) fixes -- the seven live
documents have no Search Trace section at all and were never on that line.

So `unreconstructable_docs` stays over `docs.values()`, every document the store parses, with no
exclusion of either kind. Paragraph (c) above already states that population and stays correct as
written. The asymmetry with (d) is deliberate: presence-figures and absence-figures answer
different questions, and only the second one is scoped by who was asked to run the ritual.
`tests/test_gate1_audit.py::test_a_backfilled_document_with_a_shell_trace_is_still_on_the_cross_check`
and `::test_a_prereg_less_document_with_a_shell_trace_is_still_on_the_cross_check` pin both
exclusions against it, with
`tests/test_gate1_report.py::test_a_backfilled_document_with_a_shell_trace_still_appears_on_the_provenance_line`
pinning the printed line end to end; the mutation that scopes the population back to `ritual_docs`
was proven to fail all three.

*Retired with it: `active_missing_prereg`.* Once a Pre-reg section is what admits a document to
the ritual population, "active documents missing a Pre-reg section" is empty by construction --
the field could never again be non-empty, and the line could only ever print `0`. Keeping it
would have printed `0` directly underneath a line reporting seven documents with no Pre-reg
section, which is a contradiction on the face of the output rather than a harmless dead field.
The field and its line are removed; `no_prereg_docs` answers the same question and answers it
better, across every status instead of `active` alone, and with the ids attached.

*The boundary this does not cross.* This is the audit's population only. `gate1.py` is untouched,
and the primary endpoint's row count still includes rows from documents this block now excludes.
`ENGMEM-SPEC.md` §11's "Left open, and named rather than resolved" paragraph keeps that gap open
deliberately: writing the exclusion into the endpoint's population now, with the store's
composition already known, would be choosing a rule against visible data. The amendment recorded
in §11 on 2026-09-12 says the same thing in the place that governs it.

### Telemetry unreadable or undecodable does not fail the run (ARCH-101)

`telemetry.read_session_rows` deliberately raises `OSError`/`UnicodeDecodeError` -- it mirrors
`summarize()`'s own contract on purpose (see "A second reader, not a wider `_Row`," above): a
missing file is zero rows, but a file that exists and cannot be read or decoded is not silently
zero, the same phantom-empty failure `cli.py::_cmd_telemetry`'s own `(OSError,
UnicodeDecodeError)` handler exists to prevent. `telemetry.jsonl` is append-only and written by
two concurrent processes (`cli.py` and `mcp_server.py`), so a torn append or a stray byte is a
realistic single point of failure, not a hypothetical one.

`gate1_audit.evaluate()` catches exactly that pair around its one call to `read_session_rows`
and sets `AuditReport.telemetry_error` to the exception's message instead of letting it escape;
every telemetry-derived field (`telemetry_rows_with_session`, `telemetry_distinct_sessions`,
`orphan_session_ids`, `orphan_row_count`, `unreconstructable_docs`) stays at its empty/zero
default rather than being computed against an empty read. `gate1_report.py`'s renderer checks
`telemetry_error` before printing any of those lines and, when set, prints `"UNMEASURED --
telemetry.jsonl unreadable (<reason>), not zero"` in place of each one, plus a standalone
`"telemetry.jsonl: UNREADABLE (...)"` line -- the distinction this whole block exists to draw
(a stated gap, not a false zero) applied to its own instrument, not only to the sessions it
measures.

**Why the catch lives in `gate1_audit.py`, not in `tools/gate1_report.py`.** The per-row table
and its summary (`report()`/`gate1_report.py::_summary()`) never open `telemetry.jsonl` at all and were never at
risk; only the audit block was. `main()` additionally prints the table and summary *before*
computing the audit block at all (previously the reverse), so even a future, different failure
inside `gate1_audit.evaluate()` cannot take the table down with it -- defense in depth on top of
the catch, not a substitute for it. `tests/test_gate1_report.py::
test_invalid_utf8_in_telemetry_does_not_fail_the_run_and_the_table_survives` and
`::test_an_unreadable_telemetry_file_does_not_fail_the_run_and_the_table_survives` pin
`returncode == 0`, the per-row table, and the §11 endpoint figure surviving both failure modes.

### Two caveats stated in the output, not fixed

1. **`--session` is deliberately unvalidated** (`ENGMEM-SPEC.md` §5, search step 6: "nothing
   else is validated"). The orphan-row figure -- "telemetry rows whose `session_id` matches no
   document **in this store**" -- therefore also counts typos and cross-store contamination, not
   only genuine gaps; the bolded qualifier is load-bearing, not decoration, and is printed
   verbatim in the block (`tools/gate1_report.py::_audit_lines`, pinned by
   `tests/test_gate1_report.py::
   test_the_orphan_row_line_names_the_load_bearing_scope_qualifier`, which fails under a
   mutation that drops the qualifier from the printed string while leaving the count itself
   correct).
2. **`telemetry.jsonl` is append-only with no rotation.** Pre-Gate-1 and testing rows are
   indistinguishable from experimental rows by anything this reader can see. `--since` (default:
   unset, meaning all-time -- identical to every run before this block existed, so adding the
   flag changes nothing for a caller who never passes it) narrows every telemetry-derived figure
   in the block, including the cross-check above (`tests/test_gate1_audit.py::
   test_since_window_also_scopes_the_unreconstructable_cross_check`), and the window actually
   used is always the first line printed, in both the default and `--since` cases, so a quoted
   figure is never separated from the scope it was computed under. An unparseable `--since`
   value does not fail the run -- `gate1_report.py` exits 0 unconditionally, a decision this file
   already recorded once above and which this block's own new flag does not get to override -- it
   falls back to all-time and says so in the window line itself
   (`tests/test_gate1_report.py::test_an_unparseable_since_value_does_not_fail_the_run`).

