"""Layer 1 on authored task instances: every arm, every size setting, one table (contracts/e2e.md)."""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

from agentsim import corpus as corpus_module
from agentsim import decision, harness, instances
from agentsim.live import family_of
from agentsim.accounting import break_even
from agentsim.padding import DEFAULT_DOC_SIZE, DOC_SIZES
from agentsim.profiles import ARMS

PROFILES = ("shell-claude", "grep", "grep-content", "grep-guided", "grep-notes", "no-history")
BUILT_IN = ("no-history", "multi-agent", "off-topic")
SMOKE_INSTANCES = ("c04-identifier", "c10-identifier", "c11-identifier", "c13-identifier")
SMOKE_PROFILES = ("shell-claude", "grep-content", "grep-guided", "grep-notes", "no-history")
SMOKE_BUILT_IN = ("multi-agent",)
RECORD, CONTENT = "grep-guided", "grep-content"
STALE_CLASSES = (4, 10, 11, 12)
SECTION_CLASSES = (3, 13)
SECTION_NOTE = ("Daily template reads primer, decisions and the section the primer points to or the task asks "
                "about; G+'s guidance names Lessons.")
CROSSOVER_CLASSES = (3, 4, 10)
TIE_BAND = 0.05


class InstancesRefused(RuntimeError):
    """The instance file cannot run: a format breach or an instance its sources cannot answer."""


def load(path: Path) -> tuple[instances.InstanceSet, dict]:
    facts = instances.facts_by_id(corpus_module.all_facts())
    try:
        return instances.load(path, facts), facts
    except instances.InstanceError as exc:
        raise InstancesRefused(str(exc)) from None


def check_against_stores(instance_set: instances.InstanceSet, facts: dict, root: Path) -> None:
    """At every size setting: each instance is answerable and no stale regex matches an unrelated
    document; any breach refuses the file before anything runs."""
    problems: list[str] = []
    for size in DOC_SIZES:
        generated = corpus_module.generate(root / size, doc_size=size)
        problems += [f"{size}: {p}" for p in instances.answerable_problems(instance_set, facts, generated)]
        problems += [f"{size}: instance {i!r}: {why}" for i, why in instances.stale_leaks(instance_set, facts, generated)]
    if problems:
        raise InstancesRefused("; ".join(problems))


def plan(instance_set: instances.InstanceSet, facts: dict, *, doc_size: str, smoke: bool) -> harness.Plan:
    tasks = instances.tasks_of(instance_set, facts)
    if smoke:
        tasks = [t for t in tasks if t.id in SMOKE_INSTANCES]
    return harness.Plan(
        profiles=SMOKE_PROFILES if smoke else PROFILES,
        tasks=SMOKE_BUILT_IN if smoke else BUILT_IN,
        instances=tuple(tasks),
        doc_size=doc_size,
    )


def run(out: Path, instances_path: Path, doc_sizes: tuple[str, ...] = (DEFAULT_DOC_SIZE,), *,
        smoke: bool = False, progress=None) -> dict:
    """One harness run per size setting over every instance in the file, none left out."""
    instance_set, facts = load(instances_path)
    check_against_stores(instance_set, facts, out / "check")
    started = time.perf_counter()
    by_size = {}
    for size in doc_sizes:
        size_plan = plan(instance_set, facts, doc_size=size, smoke=smoke)
        by_size[size] = harness.run(out / size, size_plan, progress=progress)
    return {
        "instances": str(instances_path),
        "provisional": instance_set.provisional,
        "author": instance_set.author,
        "smoke": smoke,
        "seconds": round(time.perf_counter() - started, 2),
        "sizes": by_size,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def table(runs: list[dict]) -> dict:
    """Per class and arm: runs, correct, stale avoided, and mean costs."""
    cells: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        cells[run["scenario_class"]][run["profile"]].append(run)
    summary: dict[int, dict[str, dict]] = {}
    for cls, by_arm in sorted(cells.items()):
        summary[cls] = {}
        for profile, group in by_arm.items():
            stale = [r["outcome"]["stale_avoided"] for r in group if r["outcome"]["stale_avoided"] is not None]
            opened = [r["outcome"].get("opened_answer") for r in group if r["outcome"].get("opened_answer") is not None]
            newest = [r["outcome"].get("correct_newest_rule") for r in group
                      if r["outcome"].get("correct_newest_rule") is not None]
            summary[cls][profile] = {
                "n": len(group),
                "correct": sum(1 for r in group if r["outcome"]["correct"]),
                "opened_n": len(opened),
                "opened": sum(1 for v in opened if v),
                "newest_n": len(newest),
                "newest": sum(1 for v in newest if v),
                "authored": any(r.get("wording") for r in group),
                "stale_n": len(stale),
                "stale_avoided": sum(1 for v in stale if v),
                "tokens": _mean([r["cost"]["to_answer_tokens"] for r in group]),
                "calls": _mean([r["cost"]["to_answer_calls"] for r in group]),
                "turn_tokens": _mean([r["cost"]["turn_tokens"] for r in group]),
                "turn_tokens_cached": _mean([r["cost"]["turn_tokens_cached"] for r in group]),
                "save_tokens": _mean([r["cost"]["save_tokens"] for r in group]),
                "save_turn_tokens_cached": _mean([r["cost"]["save_turn_tokens_cached"] for r in group]),
                "class_name": group[0]["class_name"],
            }
    return summary


LAYER1_ARMS = {"shell-claude": "E", "grep-guided": "G+", "grep-content": "G"}


def _family_rows(runs: list[dict]) -> list[str]:
    records = [
        {"family": family, "instance": r["task"], "arm": LAYER1_ARMS[r["profile"]],
         "correct": bool(r["outcome"]["correct"]), "usd": float(r["cost"]["turn_tokens_cached"])}
        for r in runs
        if r["profile"] in LAYER1_ARMS and r.get("wording") and (family := family_of(r["scenario_class"]))
    ]
    lines = [
        "| family | comparison | instances | diff pts (95 % CI) | sign-flip p | Holm p | cost ratio (95 % CI) |",
        "|---|---|---|---|---|---|---|",
    ]
    for primary, reference in (("E", "G+"), ("E", "G"), ("G+", "G")):
        for f in decision.analyse(records, primary, reference):
            lines.append(
                f"| {f.family} | {primary} − {reference} | {f.runs_primary} | {f.diff:+.0f} ({f.diff_low:+.0f}, "
                f"{f.diff_high:+.0f}) | {f.p:.3f} | {f.p_holm:.3f} | {f.ratio:.2f} ({f.ratio_low:.2f}, {f.ratio_high:.2f}) |"
            )
    return lines


def _by_wording(runs: list[dict]) -> dict[str, dict[str, str]]:
    """Correct k/n per wording and arm over the authored instances."""
    counts: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for run in runs:
        if run.get("wording"):
            cell = counts[run["wording"]][run["profile"]]
            cell[0] += 1 if run["outcome"]["correct"] else 0
            cell[1] += 1
    return {w: {p: f"{k}/{n}" for p, (k, n) in row.items()} for w, row in sorted(counts.items())}


def _rate(cell: dict | None) -> str:
    return f"{cell['correct']}/{cell['n']}" if cell else "–"


def _stale(cell: dict | None, profile: str) -> str:
    if profile == "no-history":
        return "n/a"
    if not cell:
        return "–"
    return f"{cell['stale_avoided']}/{cell['stale_n']}" if cell["stale_n"] else "0/0"


def _opened(cell: dict | None) -> str:
    return f"{cell['opened']}/{cell['opened_n']}" if cell and cell["opened_n"] else "–"


def _tokens(cell: dict | None, key: str = "tokens") -> str:
    return f"{cell[key]:.0f}" if cell else "–"


def _paired_cost(runs: list[dict], cls: int, reference: str) -> tuple[float, float, int] | None:
    """Mean cached turn tokens of E and the reference over the tasks both answered correctly."""
    by_task: dict[str, dict[str, dict]] = defaultdict(dict)
    for run in runs:
        if run["scenario_class"] == cls and run["profile"] in ("shell-claude", reference):
            by_task[run["task"]][run["profile"]] = run
    pairs = [
        (pair["shell-claude"]["cost"]["turn_tokens_cached"], pair[reference]["cost"]["turn_tokens_cached"])
        for pair in by_task.values()
        if len(pair) == 2 and all(r["outcome"]["correct"] for r in pair.values())
    ]
    if not pairs:
        return None
    return _mean([e for e, _ in pairs]), _mean([r for _, r in pairs]), len(pairs)


def _versus(summary: dict, runs: list[dict], reference: str) -> tuple[list[str], list[str], list[str]]:
    """E against one grep arm per class: correctness, then the pre-registered cost (mean over every run),
    with the tasks both answered correctly as a labelled secondary."""
    strong, weak, ties = [], [], []
    label = ARMS[reference]
    for cls, arms in summary.items():
        e, ref = arms.get("shell-claude"), arms.get(reference)
        if not e or not ref:
            continue
        name = f"Class {cls} ({e['class_name']})"
        e_rate, r_rate = e["correct"] / e["n"], ref["correct"] / ref["n"]
        if e_rate > r_rate:
            strong.append(f"{name}: E correct {_rate(e)}, {label} {_rate(ref)}.")
        elif r_rate > e_rate:
            weak.append(f"{name}: {label} correct {_rate(ref)}, E {_rate(e)}.")
        e_cost, r_cost = e["turn_tokens_cached"], ref["turn_tokens_cached"]
        paired = _paired_cost(runs, cls, reference)
        secondary = (
            f"; secondary, the {paired[2]} task(s) both answered correctly: {paired[0]:.0f} vs {paired[1]:.0f}"
            if paired else "; secondary: no task both answered correctly"
        )
        if r_cost == 0:
            if e_cost > 0:
                weak.append(f"{name}: E spends {e_cost:.0f} cached turn tokens per run where {label} spends none.")
            continue
        ratio = e_cost / r_cost
        cost = (f"cached turn tokens to the answer, mean over every run, {e_cost:.0f} vs {r_cost:.0f} "
                f"(E/{label} {ratio:.2f}){secondary}")
        if abs(ratio - 1) <= TIE_BAND:
            ties.append(f"{name}: {cost}.")
        elif ratio < 1:
            strong.append(f"{name}: cheaper, {cost}.")
        else:
            weak.append(f"{name}: dearer, {cost}.")
    return strong, weak, ties


def _break_even_rows(summary: dict) -> list[str]:
    lines = [
        "| class | E save, cached turn tok | E to answer | G+ to answer | reuses to repay vs G+ | G to answer | reuses to repay vs G |",
        "|---|---|---|---|---|---|---|",
    ]
    for cls, arms in summary.items():
        e = arms.get("shell-claude")
        if not e or not e["save_turn_tokens_cached"]:
            continue
        cells = []
        for ref in (RECORD, CONTENT):
            other = arms.get(ref)
            if other is None:
                cells += ["–", "–"]
                continue
            reuses = break_even(e["save_turn_tokens_cached"], other["turn_tokens_cached"] - e["turn_tokens_cached"])
            cells += [f"{other['turn_tokens_cached']:.0f}", "never" if reuses is None else str(reuses)]
        lines.append(f"| {cls} | {e['save_turn_tokens_cached']:.0f} | {e['turn_tokens_cached']:.0f} | " + " | ".join(cells) + " |")
    return lines


def render_size(size: str, results: dict) -> list[str]:
    summary = table(results["runs"])
    arms = [p for p in PROFILES if any(p in row for row in summary.values())]
    header = " | ".join(ARMS[p] for p in arms)
    lines = [
        f"## Size setting {size}",
        "",
        f"Base store {results['meta']['base_store_docs']} documents (mean {results['meta']['mean_doc_bytes']} bytes); "
        f"harness time {results['meta']['harness_seconds']} s.",
        "",
        "### Correct (answers judged by the instance regexes)",
        "",
        f"| class | {header} |",
        "|---|" + "---|" * len(arms),
    ]
    for cls, row in summary.items():
        mark = " †" if cls in SECTION_CLASSES else ""
        lines.append(f"| {cls}. {next(iter(row.values()))['class_name']}{mark} | " + " | ".join(_rate(row.get(p)) for p in arms) + " |")
    lines += ["", f"† {SECTION_NOTE} N's column shows how many keys are guessable without any notes."]
    lines += [
        "", "### Stale avoided", "",
        "Over runs that gave an answer (opened at least one document), in classes whose instances carry stale "
        "regexes; N gives no answer, so n/a.", "",
        f"| class | {header} |", "|---|" + "---|" * len(arms),
    ]
    for cls, row in summary.items():
        if cls in STALE_CLASSES:
            lines.append(f"| {cls} | " + " | ".join(_stale(row.get(p), p) for p in arms) + " |")
    lines += [
        "", "### Opened the document holding the answer", "",
        "Finding the document, apart from reading the section that holds the answer.", "",
        f"| class | {header} |", "|---|" + "---|" * len(arms),
    ]
    for cls, row in summary.items():
        if any(cell["opened_n"] for cell in row.values()):
            lines.append(f"| {cls} | " + " | ".join(_opened(row.get(p)) for p in arms) + " |")
    lines += [
        "", "### Sensitivity: E graded newest-first", "",
        "E is scored literally (the daily template does not say to prefer the newest note); this row grades "
        "the same E runs as G+ is graded, for comparison only.", "",
        "| class | E literal | E newest-first |", "|---|---|---|",
    ]
    for cls, row in summary.items():
        e = row.get("shell-claude")
        if e and e["newest_n"]:
            lines.append(f"| {cls} | {_rate(e)} | {e['newest']}/{e['newest_n']} |")
    lines += [
        "", "### Cost to the answer: tokens / calls / cached turn tokens (means)", "",
        f"| class | {header} |", "|---|" + "---|" * len(arms),
    ]
    for cls, row in summary.items():
        cells = [
            f"{c['tokens']:.0f} / {c['calls']:.1f} / {c['turn_tokens_cached']:.0f}" if (c := row.get(p)) else "–"
            for p in arms
        ]
        lines.append(f"| {cls} | " + " | ".join(cells) + " |")
    lines += ["", "### Correct by wording", "", f"| wording | {header} |", "|---|" + "---|" * len(arms)]
    for wording, row in _by_wording(results["runs"]).items():
        lines.append(f"| {wording} | " + " | ".join(row.get(p, "–") for p in arms) + " |")
    lines += ["", "### Families (Layer 1, descriptive)", "",
              "The pre-registered families analysed as Layer 2 will be, at instance level; cost is cached turn "
              "tokens to the answer. Layer 1 is deterministic, so these describe the scripted policies, not a test "
              "of real agents.", ""]
    lines += _family_rows(results["runs"])
    lines += ["", "### Save cost and break-even", ""] + _break_even_rows(summary)
    for reference in (RECORD, CONTENT):
        strong, weak, ties = _versus(summary, results["runs"], reference)
        label = ARMS[reference]
        lines += ["", f"### E against {label}" + (" (the comparison of record)" if reference == RECORD else ""), ""]
        lines += ["Strong sides:"] + [f"- {s}" for s in strong or ["None measured."]]
        lines += ["", "Weak sides:"] + [f"- {w}" for w in weak or ["None measured."]]
        lines += ["", f"Ties (within {TIE_BAND:.0%}):"] + [f"- {t}" for t in ties or ["None."]]
    return lines


def crossover(by_size: dict) -> list[str]:
    """E/G+ cost ratio per size for the role and stale classes, and where it crosses 1."""
    if len(by_size) < 2:
        return []
    lines = ["## Size crossover (role and stale classes)", "",
             "E/G+ cached turn tokens to the answer, means over every run of the class.", "",
             "| class | " + " | ".join(f"E/G+ at {s}" for s in by_size) + " | crossover |",
             "|---|" + "---|" * (len(by_size) + 1)]
    summaries = {size: table(results["runs"]) for size, results in by_size.items()}
    for cls in CROSSOVER_CLASSES:
        ratios = []
        for size, summary in summaries.items():
            e, ref = summary.get(cls, {}).get("shell-claude"), summary.get(cls, {}).get(RECORD)
            ratios.append(e["turn_tokens_cached"] / ref["turn_tokens_cached"] if e and ref and ref["turn_tokens_cached"] else None)
        crossing = "none in range"
        for (size_a, a), (size_b, b) in zip(zip(by_size, ratios), list(zip(by_size, ratios))[1:]):
            if a is not None and b is not None and (a - 1) * (b - 1) < 0:
                crossing = f"between {size_a} and {size_b}"
                break
        cells = [f"{r:.2f}" if r is not None else "–" for r in ratios]
        lines.append(f"| {cls} | " + " | ".join(cells) + f" | {crossing} |")
    return lines


def render(document: dict) -> str:
    banner = (
        "PROVISIONAL INSTANCES: written by the harness implementer, not blind. These numbers check the "
        "harness; they support no claim."
        if document["provisional"] else f"Instances by: {document['author']}"
    )
    lines = [
        "# engmem end-to-end harness: Layer 1 on authored instances",
        "",
        banner,
        "",
        "Layer 1 is scripted: every arm follows a declared policy (contracts/e2e.md), so these results "
        "describe those policies, not what real agents do. Layer 2 measures real agents.",
        "",
        f"Instances: `{Path(document['instances']).name}`. Arms: E = shell-claude (daily `/engmem`), blind G = "
        "`rg -c` then whole files, G = content-mode `rg -n -i -C1`, G+ = G with engmem's advice as an "
        "instruction, GN = G over notes an earlier agent wrote itself (class 8 only), N = no history. "
        "Tokens are ceil(bytes / 3.5). Cached turn tokens: each call re-reads the context so far, the part "
        "already read at 0.1, the new part at 1.25. Every instance in the file runs; none is chosen by its outcome.",
        "",
    ]
    for size, results in document["sizes"].items():
        lines += render_size(size, results) + [""]
    lines += crossover(document["sizes"])
    return "\n".join(lines) + "\n"
