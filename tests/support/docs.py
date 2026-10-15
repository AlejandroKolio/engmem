from __future__ import annotations

from pathlib import Path

from support.paths import TESTS_DIR

FIXTURES = TESTS_DIR / "fixtures" / "sessions"


def fixture_docs():
    from engmem.spine import load_store

    return load_store(FIXTURES).docs


def replaced(text: str, old: str, new: str, count: int = -1) -> str:
    """`str.replace` that fails when `old` is gone, so a variant cannot quietly equal its source."""
    assert old in text, f"variant source changed: {old!r} not found"
    return text.replace(old, new, count)


def write_file(dir_path: Path, name: str, text: str) -> Path:
    r"""Writes `text` verbatim: `write_text` translates every `\n` to `\r\n` on Windows, so a
    caller pinning a document's line endings was handed a document it never asked for."""
    path = dir_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


# the two documents the MCP write tools move between; identical copies in two test
# modules would drift apart silently, and the transition between them is the thing under test
DRAFT_CONTENT = """---
id: 20260101-widget-cache
title: WidgetCache rollout
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
mode: daily
---

## Pre-reg

Naive baseline: read the code and guess at the rollout shape.
pre-reg source: self (no sub-agent available)
"""

ACTIVE_CONTENT = """---
id: 20260101-widget-cache
title: WidgetCache rollout
date: 2026-01-01
task_date: 2026-01-01
status: active
superseded_by:
backfilled: false
tags: [cache]
entities: [WidgetCache, CacheWarmer]
related: []
covers_files: [WidgetCache.java]
verified_at_commit: abc1234
capture_minutes: 14
mode: daily
---

## Pre-reg

Naive baseline: read the code and guess at the rollout shape.
pre-reg source: self (no sub-agent available)

## Decision Log

Chose CacheWarmer over a lazy cache fill because cold start latency was too high.

## Landmines

CacheWarmer must run before WidgetCache accepts traffic or the first request hangs.

## Cold-start primer

WidgetCache serves cached widgets behind CacheWarmer, which pre-fills on boot.

## Reuse Log

Prior docs used: none.

## Search Trace

shell
"""
