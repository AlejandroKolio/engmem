---
id: cache-warmup-order
title: Cache warm-up order at deploy
date: 2026-05-25
task_date: 2026-05-25
status: active
superseded_by:
backfilled: false
tags: [platform]
entities: [WarmupPlanner]
related: []
covers_files: [WarmupPlanner.java]
verified_at_commit: 3d4e5f6
capture_minutes: 15
---

## Decision Log

On deploy the warm-up loads the hottest keys first, in the order the access log ranks them;
rejected loading everything up front because the instance took minutes to pass its health
check.

## Cold-start primer

WarmupPlanner reads the ranked key list and warms it in that order before traffic arrives.
