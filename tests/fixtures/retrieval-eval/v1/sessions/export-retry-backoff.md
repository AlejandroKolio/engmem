---
id: export-retry-backoff
title: Retry backoff for the nightly export
date: 2026-05-11
task_date: 2026-05-11
status: active
superseded_by:
backfilled: false
tags: [platform]
entities: [ExportRetryScheduler]
related: []
covers_files: [ExportRetryScheduler.java]
verified_at_commit: 1b2c3d4
capture_minutes: 20
---

## Decision Log

A failed export batch is retried with exponential backoff plus full jitter, capped at five
attempts; rejected a fixed interval because every worker retried in the same second. The
jitter comes from JitterSource so the tests can pin it.

## Cold-start primer

ExportRetryScheduler owns the retry budget; JitterSource supplies the random spread.
