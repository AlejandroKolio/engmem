---
id: metrics-query-refactor
title: MetricsQuery (MQ) Refactor
date: 2026-06-25
task_date: 2026-06-25
status: active
superseded_by:
backfilled: false
tags: [platform]
entities: [MetricsQuery, MQ, QueryService]
related: []
covers_files: [QueryService.java]
verified_at_commit: 5d6e7f8
capture_minutes: 30
---

## Pre-reg

Split MetricsQuery (MQ) handling out of QueryService's monolithic execute() method.

## Decision Log

Split QueryService.execute() into parse/plan/run steps within the same class rather than
three separate classes; rejected three classes because the steps share too much
request-scoped state to justify the extra wiring at this size.

## Landmines

The run step must not retry on timeout — a MetricsQuery retried after a run-step timeout
can double-count if the first attempt actually succeeded upstream.

## Cold-start primer

QueryService.execute() now runs MetricsQuery (MQ) through explicit parse → plan → run
steps in one class; the run step is deliberately non-retrying.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
