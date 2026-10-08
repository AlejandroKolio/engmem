# Acceptance transcript: US-07 short capture

A scripted session of an agent that has the engmem MCP tools and no shell, following
`/engmem.save.quick` (`src/engmem/templates/engmem.save.quick.md`) twice against one empty
store: once the engineer confirms the preview, once the engineer declines it.

`tests/test_short_capture.py` replays it. Every `json call` block is sent to the MCP server in
order, against a temporary store. The `text result` block after it lists lines that must
appear in the server's answer, in that order: a line ending in ` …` matches any line it
begins, and `<store>` stands for the temporary store's path. `@draft` and `@preview` stand for
the text of the latest `markdown draft` and `markdown preview` blocks. The replay also checks
what the transcript claims about the agent: the content it publishes is the preview the user
confirmed, it publishes nothing after a decline, it asks nothing between the preview and the
reply, and every Source it records is the absence marker or appears word for word in an
earlier user turn or in an answer the server actually returned. The agent's own earlier turns
do not count: a Source only the agent has said is the invention the template forbids.

What this does not show: that a real Codex or Claude agent, given the template, produces this
sequence on its own. The agent's turns here are written by hand. A recorded session with a
real agent is owner-side acceptance (E-05).

## A. The engineer confirms the preview

### A1. The task starts

```text user
/engmem decide how ReportArchiver compresses archived reports
```

```markdown draft
---
id: 20260101-report-archive-compression
title: ReportArchiver compression
date: 2026-01-01
task_date: 2026-01-01
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit:
capture_minutes:
---

## Pre-reg

Naive baseline: pick the highest compression level the library offers.
pre-reg source: self (no sub-agent available)
```

```json call
{"method": "tools/call", "params": {"name": "engmem_create_draft", "arguments": {"id": "20260101-report-archive-compression", "content": "@draft"}}}
```

```text result
created sessions/20260101-report-archive-compression.md (status: draft, version: 45acbe42c85e9f7a). …
```

```json call
{"method": "tools/call", "params": {"name": "engmem_search", "arguments": {"query": "ReportArchiver compression level", "session_id": "20260101-report-archive-compression"}}}
```

```text result
prior context: none found
```

```text agent
prior context: none found

ReportArchiver writes one archive per day of reports. Two candidates: zstd level 19 for the
best ratio, or zstd level 3 for speed. Measuring both on last night's batch.
```

### A2. The work reaches a decision

```text user
Level 19 did not finish inside the nightly run. Level 3 finished in eleven minutes and kept
most of the ratio. Go with level 3. It is in PR #41.
```

```text agent
Done: ReportArchiver now compresses with zstd level 3, merged in PR #41.
```

### A3. Short capture

```text user
/engmem.save.quick
```

```json call
{"method": "prompts/get", "params": {"name": "engmem-save-quick"}}
```

```text result
- Source: not stated in the available material.
- **Declined** (`no`, `cancel`, `don't save`), or a reply you cannot read as either of
```

```text agent
The short record, ready to save:
```

```markdown preview
---
id: 20260101-report-archive-compression
title: ReportArchiver compresses archived reports with zstd level 3
date: 2026-01-01
task_date: 2026-01-01
status: active
superseded_by:
backfilled: false
tags: [archive]
entities: [ReportArchiver, zstd]
related: []
covers_files: []
verified_at_commit:
capture_minutes:
author:
repos: []
branch:
pr: "#41"
---

## Pre-reg

Naive baseline: pick the highest compression level the library offers.
pre-reg source: self (no sub-agent available)

## Decision Log

- Decision: ReportArchiver compresses archived reports with zstd level 3.
- Reason: level 19 did not finish inside the nightly run; level 3 finished in eleven minutes and kept most of the ratio.
- Rejected alternative: zstd level 19, because it did not finish inside the nightly run.
- Source: PR #41

## Future LLM Context (cold-start primer)

ReportArchiver compresses each day's archive with zstd level 3 so it fits the nightly run.
Raise the level only after measuring it against that run on a full batch.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
```

```text agent
Save it?
```

```text user confirm
ok
```

```json call
{"method": "tools/call", "params": {"name": "engmem_complete_draft", "arguments": {"id": "20260101-report-archive-compression", "content": "@preview", "expected_version": "45acbe42c85e9f7a"}}}
```

```text result
completed sessions/20260101-report-archive-compression.md (status: draft -> active)
```

```text agent
Saved sessions/20260101-report-archive-compression.md.
```

### A4. A later task finds it

```json call
{"method": "tools/call", "params": {"name": "engmem_search", "arguments": {"query": "zstd level for the archive"}}}
```

```text result
### 20260101-report-archive-compression …
ReportArchiver compresses each day's archive with zstd level 3 so it fits the nightly run. …
```

## B. The engineer declines the preview

### B1. The task starts

```text user
/engmem choose the timezone for the BillingCutoff job
```

```markdown draft
---
id: 20260102-billing-cutoff-timezone
title: BillingCutoff timezone
date: 2026-01-02
task_date: 2026-01-02
status: draft
superseded_by:
backfilled: false
tags: []
entities: []
related: []
covers_files: []
verified_at_commit:
capture_minutes:
---

## Pre-reg

Naive baseline: run the cutoff at local midnight of the main office.
pre-reg source: self (no sub-agent available)
```

```json call
{"method": "tools/call", "params": {"name": "engmem_create_draft", "arguments": {"id": "20260102-billing-cutoff-timezone", "content": "@draft"}}}
```

```text result
created sessions/20260102-billing-cutoff-timezone.md (status: draft, version: 5ace99e4b77ed9c2). …
```

```json call
{"method": "tools/call", "params": {"name": "engmem_search", "arguments": {"query": "BillingCutoff timezone", "session_id": "20260102-billing-cutoff-timezone"}}}
```

```text result
prior context: none found
```

### B2. The work reaches a decision

```text user
Run BillingCutoff at midnight UTC: the ledger stores every timestamp in UTC.
```

```text agent
Done: BillingCutoff is scheduled at 00:00 UTC.
```

### B3. Short capture, declined

```text user
/engmem.save.quick
```

```text agent
The short record, ready to save:
```

```markdown preview
---
id: 20260102-billing-cutoff-timezone
title: BillingCutoff runs at midnight UTC
date: 2026-01-02
task_date: 2026-01-02
status: active
superseded_by:
backfilled: false
tags: [billing]
entities: [BillingCutoff]
related: []
covers_files: []
verified_at_commit:
capture_minutes:
author:
repos: []
branch:
pr:
---

## Pre-reg

Naive baseline: run the cutoff at local midnight of the main office.
pre-reg source: self (no sub-agent available)

## Decision Log

- Decision: BillingCutoff runs at midnight UTC.
- Reason: the ledger stores every timestamp in UTC.
- Rejected alternative: not stated in the available material.
- Source: not stated in the available material.

## Future LLM Context (cold-start primer)

BillingCutoff runs at 00:00 UTC because the ledger stores timestamps in UTC.
Moving it to a local time means converting every ledger timestamp first.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
```

```text agent
Save it?
```

```text user decline
no, don't save this one
```

```text agent
Nothing saved. sessions/20260102-billing-cutoff-timezone.md stays a draft; /engmem.save or
/engmem.save.quick can finish it later.
```

### B4. The declined record is not found

```json call
{"method": "tools/call", "params": {"name": "engmem_search", "arguments": {"query": "BillingCutoff midnight UTC ledger"}}}
```

```text result
prior context: none found
```
