---
id: maze-render-old
title: MazeRenderer per-cell drawing (superseded)
date: 2026-05-01
task_date: 2026-05-01
status: superseded
superseded_by: maze-render-new
backfilled: false
tags: [rendering]
entities: [MazeRenderer]
related: []
covers_files: [MazeRenderer.java]
verified_at_commit: 9f8e7d6
capture_minutes: 15
---

## Pre-reg

Draw each maze cell as its own shape so the renderer stays simple to reason about.

## Decision Log

Kept one draw call per cell in MazeRenderer; this was superseded once the grid grew past
a few hundred cells — see maze-render-new.

## Landmines

Per-cell drawing issued one call for every tile, so frame time grew linearly with the
grid — this is exactly why the batching rewrite happened.

## Cold-start primer

Superseded: MazeRenderer no longer draws cell by cell. See maze-render-new.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
