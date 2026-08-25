---
id: maze-render-new
title: MazeRenderer batched drawing
date: 2026-07-01
task_date: 2026-07-01
status: active
superseded_by:
backfilled: false
tags: [rendering]
entities: [MazeRenderer, VertexBatch]
related:
  - maze-render-old
covers_files: [MazeRenderer.java]
verified_at_commit: 2c3b4a5
capture_minutes: 20
---

## Pre-reg

Replace per-cell drawing in MazeRenderer with a single batched call.

## Decision Log

Collected every cell into one VertexBatch instead of tuning the per-cell path; rejected
tuning because the cost was structural — one call per tile — not a constant factor.

## Landmines

VertexBatch reuses its buffer between frames, so a renderer that keeps a reference to the
returned slice sees it overwritten on the next frame rather than failing loudly.

## Cold-start primer

MazeRenderer now submits one VertexBatch per frame, replacing the per-cell drawing whose
frame time grew with the grid (see maze-render-old).

## Reuse Log

| prior-doc | taken | impact | classification |
|---|---|---|---|
| maze-render-old | Confirmed the cost was structural — "one call for every tile, so frame time grew linearly with the grid" | Justified batching instead of tuning the old path | reuse |

## Search Trace

shell
