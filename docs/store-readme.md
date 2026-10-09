# About this engmem store

Copy this file as `README.md` into the root of your store (next to `sessions/`)
and fill in the three fields marked `<...>`. This is the data's passport: who's
responsible for it, what's in here, who's allowed to read it, and how long it lives.

---

## What this is

Personal engineering memory: one markdown document per closed task (decisions and
their reasons, rejected alternatives, landmines) in `sessions/`, plus what the engmem tool
(https://github.com/AlejandroKolio/engmem) records around them. It's populated by the tool at
the end of each task and read back by it at the start of the next one.

- `telemetry.jsonl` — one line per search: the query, what surfaced, bytes of context spent.
- `versions/<id>/` — an exact copy of each document version a search or `engmem read` showed,
  so a Reuse Log quote can later be checked against the text it was taken from.
  `versions/.gitattributes` stops git from rewriting the copies' line endings; keep it.
- `feedback.jsonl` — your answers to "did this find help, not apply, or do harm?", with the
  decision and source you named.
- `cost.jsonl` — the byte count of each response engmem delivered and any baseline figures
  your client reported; never the text itself.

When you remove a fact from the store, delete the document's `versions/<id>/` too, and any
`feedback.jsonl` line whose decision or source repeats it; `telemetry.jsonl` keeps the queries
that were typed. `cost.jsonl` holds no text from the documents.

## Owner

`<name / work login>` — the sole author and the sole human reader.

## Provenance of the content — important

**These documents were written by an AI agent and have not been reviewed by a
human** (the human approved the draft with a single "y", without proofreading).
Content may be inaccurate, incomplete, or out of date relative to the code. Use it
as a hint, not a source of truth — the code and its git history are the source of
truth.

## Access level

`<personal / internal private git / ...>`. Documents may mention internal class
names, systems, and business constraints — the store's access level must be no
lower than the level of the single most sensitive fact mentioned anywhere in it.
Document content is sent to an LLM on every search and every task start — that's
a property of the tool by design, so take it into account when deciding what gets
written here.

## Sweeper period

This store exists for the sake of an experiment: to test whether pulling past
decisions into new tasks pays off. The decision point is the Gate 1 review
(`<date: ~3–4 weeks from when population began>`). Depending on its outcome, the
store either keeps living on ordinary terms, or gets archived/deleted entirely.
Until Gate 1, delete nothing from it — the log and the documents are the
experiment's data. Only research sessions with a recorded baseline, and documents
written before modes existed, are counted; `mode: daily` sessions (the default) and
research sessions without a baseline are kept outside the count but are still
searched like any other document.
