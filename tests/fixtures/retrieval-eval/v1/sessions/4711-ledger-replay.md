---
id: 4711-ledger-replay
title: Ledger replay from a partial outage
date: 2026-05-18
task_date: 2026-05-18
status: active
superseded_by:
backfilled: false
tags: [payments]
entities: [LedgerReplayGuard, WAL]
related: []
covers_files: [LedgerReplayGuard.java]
verified_at_commit: 2c3d4e5
capture_minutes: 30
---

## Decision Log

Replaying the ledger is guarded by LedgerReplayGuard, which refuses an entry it has already
applied; rejected replaying blindly because a duplicate entry double-counts a balance.

## Cold-start primer

LedgerReplayGuard keeps the applied-entry watermark; a replay starts from it, never from zero.
