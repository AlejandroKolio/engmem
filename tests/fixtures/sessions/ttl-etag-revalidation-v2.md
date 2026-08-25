---
id: ttl-etag-revalidation-v2
title: TTL ETAG Revalidation v2
date: 2026-06-15
task_date: 2026-06-15
status: active
superseded_by:
backfilled: false
tags: [platform]
entities: [ETAG, TTL, revalidation, CacheRevalidationController]
related:
  - 1000001-response-cache
covers_files: [CacheRevalidationController.java]
verified_at_commit: def5678
capture_minutes: 18
---

## Pre-reg

Rework revalidation so TTL and ETAG are carried through to v2 of the cache API.

## Decision Log

Kept revalidation inside CacheRevalidationController rather than extracting a separate
RevalidationResolver; rejected the extraction because the logic is only 12 lines and a
new class would add an indirection layer for no real reuse benefit yet.

## Landmines

TTL must be evaluated before ETAG — reversing the order silently drops the entity tag
from the response.

## Cold-start primer

CacheRevalidationController v2 resolves a response's TTL (time to live) and ETAG (entity
tag) together in one pass, reusing the freshness metadata from the response-cache work.

## Reuse Log

| prior-doc | taken | impact | classification |
|---|---|---|---|
| 1000001-response-cache | Reused the upstream ETAG/TTL freshness headers — "Reused the ETAG and TTL headers the upstream service already sends instead of adding a separate cache-metadata table" | Avoided adding a second lookup path for the same values | reuse |

## Search Trace

shell
