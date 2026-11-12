"""The pre-registered decision rules of Layer 2 (contracts/e2e.md, "Decision rules"), applied to graded runs."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass

BETTER_POINTS = 15.0
NON_INFERIOR_POINTS = -5.0
TIE_POINTS = 10.0
CHEAPER_RATIO = 0.80
CHEAPER_UPPER = 0.95
TIE_RATIO = (0.80, 1.25)
ALPHA = 0.05
RESAMPLES = 2000
FLIPS = 20000
EXACT_FLIPS = 16
SEED = 20261111
STALE_FAMILIES = ("stale",)
ENGMEM_BETTER, GREP_BETTER, TIE, INCONCLUSIVE = "engmem better", "grep better", "tie", "inconclusive"


@dataclass(frozen=True)
class Family:
    """One family's comparison of `primary` against `reference`; points are percentage points."""

    family: str
    runs_primary: int
    runs_reference: int
    diff: float
    diff_low: float
    diff_high: float
    p: float
    p_holm: float
    ratio: float
    ratio_low: float
    ratio_high: float
    verdict: str


def _rate(rows: list[dict]) -> float:
    return sum(1 for r in rows if r["correct"]) / len(rows)


def _usd(rows: list[dict]) -> float | None:
    """Mean USD over the rows that carry one; None when none does."""
    priced = [r["usd"] for r in rows if isinstance(r.get("usd"), (int, float))]
    return sum(priced) / len(priced) if priced else None


@dataclass(frozen=True)
class _Pair:
    """One instance: its correct rate over every run, and its mean USD per arm over the priced runs."""

    diff: float
    usd_primary: float | None
    usd_reference: float | None


def _pairs(by_instance: dict[str, tuple[list[dict], list[dict]]]) -> list[_Pair]:
    return [
        _Pair(_rate(a) - _rate(b), _usd(a), _usd(b))
        for _, (a, b) in sorted(by_instance.items())
    ]


def sign_flip_p(diffs: list[float], rng: random.Random) -> float:
    """Two-sided paired sign-flip test on per-instance differences: exact up to `EXACT_FLIPS`
    instances, else `FLIPS` seeded random flips."""
    if not diffs or all(d == 0 for d in diffs):
        return 1.0
    observed = abs(sum(diffs)) - 1e-12
    if len(diffs) <= EXACT_FLIPS:
        hits = sum(
            1 for mask in range(2 ** len(diffs))
            if abs(sum(d if mask >> i & 1 else -d for i, d in enumerate(diffs))) >= observed
        )
        return hits / 2 ** len(diffs)
    hits = sum(
        1 for _ in range(FLIPS)
        if abs(sum(d if rng.random() < 0.5 else -d for d in diffs)) >= observed
    )
    return (hits + 1) / (FLIPS + 1)


def _ratio(pairs: list[_Pair]) -> float:
    """Over the instances priced for both arms; NaN when there is none."""
    priced = [p for p in pairs if p.usd_primary is not None and p.usd_reference is not None]
    if not priced:
        return math.nan
    reference = sum(p.usd_reference for p in priced)
    return sum(p.usd_primary for p in priced) / reference if reference else math.inf


def _bootstrap(pairs: list[_Pair], rng: random.Random) -> tuple[list[float], list[float]]:
    """Resamples instances with replacement; every statistic is a mean over instances."""
    diffs, ratios = [], []
    for _ in range(RESAMPLES):
        sample = [rng.choice(pairs) for _ in pairs]
        diffs.append(100 * sum(p.diff for p in sample) / len(sample))
        ratio = _ratio(sample)
        if not math.isnan(ratio):
            ratios.append(ratio)
    return diffs, ratios


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def holm(p_values: list[float]) -> list[float]:
    """Holm's step-down adjustment, in the input order."""
    order = sorted(range(len(p_values)), key=lambda i: p_values[i])
    adjusted = [0.0] * len(p_values)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(p_values) - rank) * p_values[i]))
        adjusted[i] = running
    return adjusted


def verdict(diff: float, diff_low: float, diff_high: float, p_holm: float,
            ratio: float, ratio_low: float, ratio_high: float) -> str:
    """The rules as recorded: better on correctness or non-inferior and cheaper; a tie inside both
    bands; grep better as the mirror; anything else is inconclusive."""
    if (diff >= BETTER_POINTS and p_holm < ALPHA) or (
        diff_low > NON_INFERIOR_POINTS and ratio <= CHEAPER_RATIO and ratio_high < CHEAPER_UPPER
    ):
        return ENGMEM_BETTER
    if (diff <= -BETTER_POINTS and p_holm < ALPHA) or (
        diff_high < -NON_INFERIOR_POINTS and ratio >= 1 / CHEAPER_RATIO and ratio_low > 1 / CHEAPER_UPPER
    ):
        return GREP_BETTER
    if -TIE_POINTS <= diff_low and diff_high <= TIE_POINTS and TIE_RATIO[0] <= ratio_low and ratio_high <= TIE_RATIO[1]:
        return TIE
    return INCONCLUSIVE


def analyse(records: list[dict], primary: str = "E", reference: str = "G+") -> list[Family]:
    """Per family, at instance level: mean correct-rate difference (bootstrap 95 % CI, sign-flip p,
    Holm-adjusted), USD-to-answer ratio (CI) and verdict. Records hold family, instance, arm, correct, usd."""
    grouped: dict[str, dict[str, tuple[list[dict], list[dict]]]] = defaultdict(lambda: defaultdict(lambda: ([], [])))
    for record in records:
        if record["arm"] == primary:
            grouped[record["family"]][record["instance"]][0].append(record)
        elif record["arm"] == reference:
            grouped[record["family"]][record["instance"]][1].append(record)
    rng = random.Random(SEED)
    rows = []
    for family in sorted(grouped, key=_family_key):
        by_instance = {k: v for k, v in grouped[family].items() if v[0] and v[1]}
        if not by_instance:
            continue
        pairs = _pairs(by_instance)
        diffs, ratios = _bootstrap(pairs, rng)
        p = sign_flip_p([pair.diff for pair in pairs], rng)
        runs = (sum(len(a) for a, _ in by_instance.values()), sum(len(b) for _, b in by_instance.values()))
        rows.append((family, pairs, runs, diffs, ratios, p))
    adjusted = holm([row[5] for row in rows])
    result = []
    for (family, pairs, runs, diffs, ratios, p), p_holm in zip(rows, adjusted):
        diff = 100 * sum(pair.diff for pair in pairs) / len(pairs)
        ratio = _ratio(pairs)
        low, high = _percentile(diffs, 0.025), _percentile(diffs, 0.975)
        r_low, r_high = _percentile(ratios, 0.025), _percentile(ratios, 0.975)
        result.append(Family(
            family, runs[0], runs[1], diff, low, high, p, p_holm, ratio, r_low, r_high,
            verdict(diff, low, high, p_holm, ratio, r_low, r_high),
        ))
    return result


FAMILY_ORDER = ("retrieval", "stale", "section")


def _family_key(family: str) -> tuple[int, str]:
    if family in FAMILY_ORDER:
        return FAMILY_ORDER.index(family), family
    return (int(family), family) if family.isdigit() else (10**6, family)


def readings(e_vs_plus: list[Family], plus_vs_g: list[Family]) -> list[str]:
    """The fixed readings, worded as recorded, from the verdicts alone."""
    lines = []
    if any(f.family in STALE_FAMILIES and f.verdict == ENGMEM_BETTER for f in e_vs_plus):
        lines.append("staleness-aware memory")
    plus_better = {f.family for f in plus_vs_g if f.verdict == ENGMEM_BETTER}
    if any(f.verdict == TIE and f.family in plus_better for f in e_vs_plus):
        lines.append("the value is the discipline of notes, not search")
    if not any(f.verdict == ENGMEM_BETTER for f in e_vs_plus):
        lines.append("no family better: say so in the README")
    return lines


def general_claims(per_agent: dict[str, list[Family]]) -> dict[str, str]:
    """A family's direction counts as general only when every agent's verdict agrees and is not a tie
    or inconclusive; it needs at least two agents."""
    if len(per_agent) < 2:
        return {}
    verdicts: dict[str, set[str]] = defaultdict(set)
    seen: dict[str, int] = defaultdict(int)
    for families in per_agent.values():
        for f in families:
            verdicts[f.family].add(f.verdict)
            seen[f.family] += 1
    return {
        family: next(iter(found))
        for family, found in verdicts.items()
        if seen[family] == len(per_agent) and len(found) == 1 and next(iter(found)) in (ENGMEM_BETTER, GREP_BETTER)
    }


def render(e_vs_plus: list[Family], plus_vs_g: list[Family]) -> str:
    lines = [
        "| family | E runs | G+ runs | E − G+ pts (95 % CI) | Holm p | USD E/G+ (95 % CI) | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in e_vs_plus:
        lines.append(
            f"| {f.family} | {f.runs_primary} | {f.runs_reference} | {f.diff:+.1f} ({f.diff_low:+.1f}, {f.diff_high:+.1f}) "
            f"| {f.p_holm:.3f} | {f.ratio:.2f} ({f.ratio_low:.2f}, {f.ratio_high:.2f}) | {f.verdict} |"
        )
    lines += ["", "Readings: " + "; ".join(readings(e_vs_plus, plus_vs_g))]
    return "\n".join(lines) + "\n"
