"""The pre-registered decision rules and the accounting they and the Layer 1 report rest on."""

from __future__ import annotations

import random

import pytest

from agentsim import accounting, decision
from agentsim.profiles import LOAD, SAVE, SEARCH, START
from agentsim.trace import TEMPLATE, TOOL, Step, tokens

pytestmark = pytest.mark.e2e


def _records(family: str, arm: str, correct: int, total: int, usd: float, instances: int = 10) -> list[dict]:
    rows = []
    for i in range(total):
        rows.append({"family": family, "instance": f"{family}-{i % instances}", "arm": arm,
                     "correct": i < correct, "usd": usd})
    return rows


def _one(records, primary="E", reference="G+") -> decision.Family:
    (family,) = decision.analyse(records, primary, reference)
    return family


def test_a_large_correctness_gain_is_engmem_better():
    f = _one(_records("4", "E", 28, 30, 1.0) + _records("4", "G+", 12, 30, 1.0))
    assert f.diff > 50 and f.p_holm < 0.05 and f.verdict == decision.ENGMEM_BETTER


def test_equal_correctness_and_a_clearly_lower_cost_is_engmem_better():
    f = _one(_records("1", "E", 30, 30, 0.5) + _records("1", "G+", 30, 30, 1.0))
    assert f.diff == 0 and f.ratio == 0.5 and f.verdict == decision.ENGMEM_BETTER


def test_equal_correctness_and_equal_cost_is_a_tie():
    assert _one(_records("2", "E", 30, 30, 1.0) + _records("2", "G+", 30, 30, 1.0)).verdict == decision.TIE


def test_the_mirror_is_grep_better():
    assert _one(_records("13", "E", 10, 30, 1.0) + _records("13", "G+", 29, 30, 1.0)).verdict == decision.GREP_BETTER
    assert _one(_records("13", "E", 30, 30, 2.0) + _records("13", "G+", 30, 30, 1.0)).verdict == decision.GREP_BETTER


def test_a_tight_ratio_inside_the_tie_band_is_a_tie_and_a_wide_one_is_inconclusive():
    assert _one(_records("3", "E", 30, 30, 0.85) + _records("3", "G+", 30, 30, 1.0)).verdict == decision.TIE
    spread = _records("3", "E", 30, 30, 1.0)
    for i, row in enumerate(spread):
        row["usd"] = 0.3 if int(row["instance"].rsplit("-", 1)[1]) % 2 else 1.7
    f = _one(spread + _records("3", "G+", 30, 30, 1.0))
    assert f.ratio_low < 0.8 < 1.25 < f.ratio_high and f.verdict == decision.INCONCLUSIVE


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((15.0, 1.0, 20.0, 0.04, 1.0, 0.9, 1.1), decision.ENGMEM_BETTER),
        ((14.9, 1.0, 20.0, 0.001, 1.0, 0.9, 1.1), decision.INCONCLUSIVE),
        ((15.0, 1.0, 20.0, 0.05, 1.0, 0.9, 1.1), decision.INCONCLUSIVE),
        ((0.0, -4.9, 4.9, 1.0, 0.80, 0.7, 0.949), decision.ENGMEM_BETTER),
        ((0.0, -5.0, 4.9, 1.0, 0.80, 0.7, 0.949), decision.INCONCLUSIVE),
        ((0.0, -4.9, 4.9, 1.0, 0.80, 0.7, 0.95), decision.INCONCLUSIVE),
        ((0.0, -4.9, 4.9, 1.0, 0.81, 0.7, 0.949), decision.INCONCLUSIVE),
        ((0.0, -10.0, 10.0, 1.0, 1.0, 0.8, 1.25), decision.TIE),
        ((0.0, -10.1, 10.0, 1.0, 1.0, 0.8, 1.25), decision.INCONCLUSIVE),
        ((0.0, -4.9, 4.9, 1.0, 1.25, 1.06, 1.4), decision.GREP_BETTER),
    ],
    ids=["gain-at-15", "gain-below-15", "p-at-alpha", "cheaper", "non-inferior-bound", "ratio-upper-bound", "ratio-above-0.80",
         "tie-edges", "tie-outside", "dearer"],
)
def test_each_rule_holds_at_its_recorded_bound(args, expected):
    assert decision.verdict(*args) == expected


def test_holm_scales_the_smallest_p_by_the_family_count_and_stays_monotone():
    assert decision.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def test_the_fixed_readings():
    better = decision.Family("stale", 30, 30, 40, 20, 60, 0.001, 0.001, 1.0, 0.9, 1.1, decision.ENGMEM_BETTER)
    tie = decision.Family("retrieval", 30, 30, 0, -5, 5, 1.0, 1.0, 1.0, 0.9, 1.1, decision.TIE)
    plus_over_g = decision.Family("retrieval", 30, 30, 30, 10, 50, 0.001, 0.001, 1.0, 0.9, 1.1, decision.ENGMEM_BETTER)
    assert decision.readings([better], []) == ["staleness-aware memory"]
    assert decision.readings([tie], [plus_over_g]) == [
        "the value is the discipline of notes, not search", "no family better: say so in the README",
    ]


def test_a_general_claim_needs_the_same_direction_in_a_second_agent():
    better = decision.Family("4", 1, 1, 0, 0, 0, 1, 1, 1, 1, 1, decision.ENGMEM_BETTER)
    tie = decision.Family("4", 1, 1, 0, 0, 0, 1, 1, 1, 1, 1, decision.TIE)
    assert decision.general_claims({"claude": [better]}) == {}
    assert decision.general_claims({"claude": [better], "codex": [better]}) == {"4": decision.ENGMEM_BETTER}
    assert decision.general_claims({"claude": [better], "codex": [tie]}) == {}


def test_each_call_re_reads_the_context_before_it_and_a_closing_turn_reads_it_all():
    steps = [
        Step(START, TEMPLATE, "template", 0.0, "t" * 35),
        Step(SEARCH, TOOL, "search", 0.0, "s" * 70),
        Step(LOAD, TOOL, "read", 0.0, "r" * 35),
        Step(SAVE, TOOL, "save", 0.0, "x" * 35),
    ]
    result = accounting.turn_overhead(steps, (START, SEARCH, LOAD))
    t, s, r = tokens(35), tokens(70), tokens(35)
    assert result["turns"] == 3
    assert result["plain"] == t + (t + s) + (t + s + r)
    cached = 1.25 * t + (0.1 * t + 1.25 * s) + (0.1 * (t + s) + 1.25 * r)
    assert result["cached"] == round(cached)


def test_a_save_repays_itself_only_when_a_reuse_saves_tokens():
    assert accounting.break_even(1000, 300) == 4
    assert accounting.break_even(100, 300) == 1
    assert accounting.break_even(1000, 0) is None and accounting.break_even(1000, -5) is None


def _per_instance(family: str, arm: str, outcomes: list[bool], repeats: int, usd: float = 1.0) -> list[dict]:
    return [
        {"family": family, "instance": f"{family}-{i}", "arm": arm, "correct": ok, "usd": usd}
        for i, ok in enumerate(outcomes) for _ in range(repeats)
    ]


def test_duplicating_repeats_leaves_the_p_value_and_the_verdict_unchanged():
    e = [True] * 7 + [False] * 3
    plus = [True] * 4 + [False] * 6
    once = decision.analyse(_per_instance("stale", "E", e, 1) + _per_instance("stale", "G+", plus, 1))
    thrice = decision.analyse(_per_instance("stale", "E", e, 3) + _per_instance("stale", "G+", plus, 3))
    assert [(f.diff, f.p, f.p_holm, f.verdict) for f in once] == [(f.diff, f.p, f.p_holm, f.verdict) for f in thrice]
    assert once[0].runs_primary == 10 and thrice[0].runs_primary == 30


def test_the_sign_flip_test_is_exact_on_few_instances():
    rng = random.Random(1)
    assert decision.sign_flip_p([1, 1, 1, 1, 1], rng) == pytest.approx(2 / 32)
    assert decision.sign_flip_p([0, 0, 0], rng) == 1.0
    assert decision.sign_flip_p([1, -1], rng) == 1.0


def test_the_cost_ratio_uses_only_instances_priced_for_both_arms_while_correctness_uses_every_run():
    rows = [
        {"family": "stale", "instance": "a", "arm": "E", "correct": True, "usd": 1.0},
        {"family": "stale", "instance": "a", "arm": "G+", "correct": True, "usd": 2.0},
        {"family": "stale", "instance": "b", "arm": "E", "correct": True, "usd": 9.0},
        {"family": "stale", "instance": "b", "arm": "G+", "correct": False, "usd": None},
    ]
    (family,) = decision.analyse(rows)
    assert family.diff == 50.0 and family.ratio == 0.5
