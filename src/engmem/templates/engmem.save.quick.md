---
description: Save an engmem session without the review round — one confirmation, nothing to verify
---

# /engmem.save.quick — save without reviewing

For when there's no attention left to verify anything. Close out the session started by
`/engmem` with a **minimal but valid** document, asking the user nothing except one final
confirmation.

## Rules

- **Nothing to verify.** The full `/engmem.save` asks the user to check the generated
  YAML, check that every Reuse Log row carries a real quote, and answer "does this
  overturn a prior document?" — that last one needs them to recall the whole store.
  This path removes all three demands rather than making them faster.
- **One confirmation, no questions.** No field-by-field prompting, no YAML review round,
  no supersede question.
- This path degrades document *completeness*, never document *validity* — the result must
  still parse cleanly (valid YAML front matter, every required field present).
- **Use it as the exception, not the default.** The cost is real: no cold-start primer
  means this document shows no preview in future search results; an unanswered supersede
  question means a stale document keeps surfacing as current; an unexamined Reuse Log
  usually means the reuse evidence the experiment needs is simply missing.

## Steps

1. From the code diff, extract exactly 3 bullets — decisions made and/or pitfalls hit.
   Put decision bullets in `## Decision Log` and pitfall bullets in `## Lessons Learned`
   (either section may hold more of the 3 than the other; neither is left out of the
   document even if it gets no bullets — write `None captured (quick save).` in that
   case).
2. Write `## Reuse Log` as exactly `Prior docs used: none.` unless prior docs obviously
   influenced the work, in which case list them as rows (with quotes, same validity rule
   as `/engmem.save`: a quoted span in `taken`, classification exactly `reuse` /
   `anti-reuse` / `harmful`; `engmem_complete_draft` refuses a row that breaks either and
   returns it) — but do not interrogate the user about it.
3. Write `## Search Trace` with the `shell` / `paste` / `miss` value recorded by
   `/engmem`.
4. Carry `## Pre-reg` over unedited from the draft. Skip
   `## Future LLM Context (cold-start primer)` — quick
   save doesn't write it (ENGMEM-SPEC.md §6.3: quick save writes only Decision Log,
   Reuse Log, and Search Trace beyond what the draft already had; Lessons Learned rides
   along with Decision Log's bullets).

   Headings here carry the same names as `/engmem.save`'s but **no numbers**. The numbers
   there index a fixed set of seventeen; a six-section document numbered 8, 11 and 16 would
   read as one with gaps where sections were dropped, which is exactly what this path does
   not claim. Same names, same roles, no false implication of missing content.
5. Generate the complete front matter automatically (same fields as `/engmem.save`,
   including `capture_minutes` from the draft creation time and `verified_at_commit` from
   `HEAD` — leave `verified_at_commit` blank if you have no shell to get it), set
   `status: active` directly.
6. Show the user the finished document in one message and ask exactly one thing: `ok`?
   On `ok`, write it. On anything else, treat the reply as edits, apply them, and write —
   still no follow-up questions. Write directly if you have file access; otherwise call
   the `engmem_complete_draft` MCP tool with `id` and the finished document as `content`.
