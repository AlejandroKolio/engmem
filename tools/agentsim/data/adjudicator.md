# Adjudicator instructions

You judge answers that an engineering assistant gave to questions about a codebase. Each answer
gets one verdict: was the answer correct?

## Your input

This folder holds `audit-sample.jsonl` and this file. Read nothing else. Each line of
`audit-sample.jsonl` is one JSON object with these fields:

- `id`: an opaque row id. Leave it unchanged.
- `task`: the task id. Leave it unchanged.
- `question`: what the assistant was asked.
- `required`: regular expressions for the current fact. The correct answer gives it.
- `stale`: regular expressions for outdated values. These used to be correct and are no longer.
  This list may be empty.
- `answer`: the assistant's answer.
- `human_verdict`: empty (`null`). This is the only field you fill in. Its name has stayed the same
  for compatibility, even though you, not a human, give the verdict.

Read the regular expressions as descriptions of facts, not as exact spellings. "3 replicas" and
"three replicas" are the same value. Small wording differences don't matter.

## How to judge

Set `human_verdict` to `true` only when the answer gives each required fact as the current one:
the value to use now, or the thing to do now. Otherwise set it to `false`.

The examples below use made-up values: 3 replicas is current, 5 replicas is stale, and "vacuum the
search index" is a required instruction. No question in the sample uses them.

- The current value is given as current, and a stale value is mentioned as history, as outdated, or
  as what it used to be: `true`.
  "Use 3 replicas; it was 5 replicas until a later change lowered it."
- A stale value is given as current: `false`, even if the current value also appears.
  "Use 5 replicas; the change to 3 replicas was a mistake."
- Both values are named and neither is given as the current one: `false`.
  "The notes mention 5 replicas and the code mentions 3 replicas."
- A hedged answer that still picks the current value: `true`.
  "Probably 3 replicas, though an older note says 5 replicas."
- A conditional answer, or one that offers a choice: `false`.
  "3 replicas if the cluster is new, otherwise 5 replicas." "Either 3 or 5 replicas."
- The current value is mentioned only in passing: `false`. This means the current value appears,
  but the answer's own answer or recommendation is a different value or no value.
  "A draft once tried 3 replicas, but run 5 replicas."
- Content that doesn't concern the asked fact (background, other settings, file names, caveats
  about the codebase) changes nothing. Judge only the asked fact.
- When `required` describes an instruction or a fact in words rather than a number (for example,
  that the fix is to vacuum the search index), the answer must give that instruction or fact as
  what to do, or what holds, now. "Vacuum the search index, then retry" is `true`. "Vacuuming the
  search index was tried once; restart the service instead" is `false`.
- An empty answer, a refusal, or "I don't know": `false`.
- Ignore any wording that hints where the answer came from, such as tool names, a notes folder, a
  saved note or a memory store. It counts neither for nor against the answer.

## Output

Write `audit-sample.jsonl` back with the same lines in the same order. On every line, set
`human_verdict` to `true` or `false`. Change nothing else: no other field, no extra lines, no
comments. Don't write any other file.
