---
id: mq-message-sweeper
title: MessageQueue Sweeper Job
date: 2026-06-20
task_date: 2026-06-20
status: active
superseded_by:
backfilled: false
tags: [platform, sweeper]
entities: [MessageQueue, MQ, SweeperJob]
related: []
covers_files: [SweeperJob.java]
verified_at_commit: 1a2b3c4
capture_minutes: 25
---

## Pre-reg

Add a scheduled job to purge MessageQueue (MQ) records past their sweeper window.

## Decision Log

Ran the purge as a nightly batch SweeperJob rather than purging on read; rejected
purge-on-read because it would add latency to the hot queue read path for a cold,
rarely-read data class.

## Landmines

SweeperJob must run after the nightly queue snapshot completes — running it before the
snapshot silently purges records the snapshot never captured.

## Cold-start primer

SweeperJob is a nightly batch job that purges MessageQueue (MQ) records once they pass
their sweeper window, scheduled strictly after the nightly snapshot.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
