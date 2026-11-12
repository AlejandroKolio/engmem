"""Turn overhead (each call re-reads the context so far) and the break-even of a save (contracts/e2e.md)."""

from __future__ import annotations

import math

from agentsim.trace import TEMPLATE, Step, tokens

CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


def turn_overhead(steps: list[Step], phases: tuple[str, ...]) -> dict:
    """Each call of `phases` re-reads the context before it, one closing turn reads it all; `cached`
    prices what a turn already read at `CACHE_READ_FACTOR`, the new part at `CACHE_WRITE_FACTOR`."""
    context = 0
    read_before = 0
    plain = 0
    cached = 0.0
    turns = 0
    for step in steps:
        if step.phase not in phases:
            continue
        if step.kind != TEMPLATE:
            plain, cached, read_before = _turn(context, read_before, plain, cached)
            turns += 1
        context += tokens(step.bytes)
    plain, cached, _ = _turn(context, read_before, plain, cached)
    return {"turns": turns + 1, "plain": plain, "cached": round(cached)}


def _turn(context: int, read_before: int, plain: int, cached: float) -> tuple[int, float, int]:
    new = context - read_before
    return plain + context, cached + CACHE_READ_FACTOR * read_before + CACHE_WRITE_FACTOR * new, context


def break_even(save_tokens: float, saving_per_reuse: float) -> int | None:
    """How many later reuses repay one save: None when a reuse saves nothing."""
    if saving_per_reuse <= 0:
        return None
    return max(1, math.ceil(save_tokens / saving_per_reuse))
