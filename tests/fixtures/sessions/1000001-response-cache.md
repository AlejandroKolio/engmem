---
id: 1000001-response-cache
title: Response Cache API
date: 2026-06-10
task_date: 2026-06-10
status: active
superseded_by:
backfilled: false
tags: [platform, caching]
entities: [ResponseCacheController, ResponseCache, ETAG, TTL]
related: []
covers_files: [ResponseCacheController.java]
verified_at_commit: abc1234
capture_minutes: 12
---

## Pre-reg

Add response caching in front of the read endpoints.

## Decision Log

Reused the ETAG and TTL headers the upstream service already sends instead of adding a
separate cache-metadata table; rejected the separate table because the response already
carries the freshness data and an extra lookup would add latency for no real benefit.

## Landmines

ResponseCache lookups must be memoized per-request — a repeated call inside the same
request re-runs the upstream fetch and doubles latency.

## Cold-start primer

ResponseCacheController serves cached responses for read endpoints using the ETAG
(entity tag) and TTL (time to live) values already present on the upstream response.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
