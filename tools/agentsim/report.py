"""The markdown report: a table per scenario, strong and weak sides, the save cost, the raw numbers."""

from __future__ import annotations

from collections import defaultdict

from agentsim.trace import tokens

PRIMARY = "shell-claude"
TIE_BAND = 0.05
ENGMEM_RETRIEVAL_ACTIONS = ("engmem search", "engmem read", "engmem_search", "engmem_search_by_role", "engmem_read")


def _flag(value: bool | None) -> str:
    if value is None:
        return "–"
    return "yes" if value else "no"


def _correct(run: dict | None) -> str:
    return _flag(run["outcome"]["correct"]) if run else "–"


def _cost(run: dict | None, key: str) -> str:
    return str(run["cost"][key]) if run else "–"


def _label(run: dict) -> str:
    """The scenario a run belongs to: its class, and its policy variant when it has one."""
    variant = run.get("variant") or ""
    return f"{run['scenario_class']}" + (f" ({variant})" if variant else "")


def _by_scenario(runs: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        grouped[_label(run)].append(run)
    return dict(sorted(grouped.items(), key=lambda item: (item[1][0]["scenario_class"], item[0])))


def _pick(runs: list[dict], profile: str) -> dict | None:
    return next((r for r in runs if r["profile"] == profile), None)


def _pct(a: int, b: int) -> str:
    if b == 0:
        return "n/a (baseline is 0)"
    return f"{(a - b) / b * 100:+.0f} %"


def _to_answer(run: dict) -> tuple[int, int]:
    return run["cost"]["to_answer_tokens"], run["cost"]["to_answer_calls"]


CHAIN_NOTE = (
    "grep's cost here includes {reads} whole-file reads of superseded chain links ({tokens} tokens); "
    "engmem's search shows a redirect line for a superseded document instead."
)
SCALE_CLASS = 7
SCALE_NOTE = (
    " Those reads, not the store's size, explain the token difference against grep in this class, so it "
    "is not a scaling result."
)


def chain_reads(run: dict | None) -> tuple[int, int]:
    """grep's whole-file reads of superseded chain links in `run`: count and tokens."""
    if run is None:
        return 0, 0
    steps = [s for s in run.get("steps", []) if s.get("detail", {}).get("superseded_chain")]
    return len(steps), sum(s["tokens"] for s in steps)


def _chain_note(grep: dict | None) -> str:
    """Names grep's chain reads when it made more than one; in class 7 also says it is not scaling."""
    reads, chain_tokens = chain_reads(grep)
    if reads < 2:
        return ""
    note = CHAIN_NOTE.format(reads=reads, tokens=chain_tokens)
    return note + (SCALE_NOTE if grep["scenario_class"] == SCALE_CLASS else "")


def _observed(engmem: dict | None, grep: dict | None, none: dict | None) -> str:
    """Who was correct, and the cheapest of those to the answer."""
    labelled = [(label, run) for label, run in (("engmem", engmem), ("grep", grep), ("no history", none)) if run]
    correct = [(label, run) for label, run in labelled if run["outcome"]["correct"]]
    if not correct:
        return "nobody correct"
    ranked = sorted(correct, key=lambda item: _to_answer(item[1]))
    cheapest = ranked[0][0]
    first, second = _to_answer(ranked[0][1]), _to_answer(ranked[-1 if len(ranked) == 1 else 1][1])
    if len(ranked) > 1 and _tie(first[0], second[0]) and first[1] >= second[1]:
        cheapest = f"{ranked[0][0]} and {ranked[1][0]} about even"
    return f"correct: {', '.join(label for label, _ in correct)}; cheapest to the answer: {cheapest}"


def _class_table(runs: list[dict]) -> list[str]:
    lines = [
        "| profile | correct | reached | pitfall avoided | stale avoided | none found said | citation valid "
        "| Reuse Log row | to-answer tok | to-answer calls | of which template tok | save tok | save calls | wall s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        o, c = r["outcome"], r["cost"]
        lines.append(
            f"| {r['profile']} | {_flag(o['correct'])} | {_flag(o['reached'])} | {_flag(o['pitfall_avoided'])} "
            f"| {_flag(o['stale_avoided'])} | {_flag(o['none_found_said'])} | {_flag(o['citation_valid'])} "
            f"| {_flag(o['reuse_row_correct'])} | {c['to_answer_tokens']} | {c['to_answer_calls']} "
            f"| {c['to_answer_tokens'] - c['retrieval_tokens']} | {c['save_tokens']} | {c['save_calls']} "
            f"| {r['wall_seconds']:.2f} |"
        )
    return lines


def _scaling(runs: list[dict]) -> list[str]:
    """Search cost on the base store (class 1) beside the scale store (class 7), per profile."""
    lines = [
        "| profile | store docs | search calls | search seconds | search bytes | files grep matched | correct |",
        "|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        if run["scenario_class"] not in (1, 7) or run["condition"] == "none":
            continue
        search = [s for s in run["steps"] if s["phase"] == "search"]
        matched = [s["detail"]["files_matched"] for s in search if "files_matched" in s.get("detail", {})]
        lines.append(
            f"| {run['profile']} | {run['store_docs']} | {len(search)} | {sum(s['seconds'] for s in search):.3f} "
            f"| {sum(s['bytes'] for s in search)} | {', '.join(map(str, matched)) or '–'} "
            f"| {_flag(run['outcome']['correct'])} |"
        )
    return lines


def _save_table(runs: list[dict]) -> list[str]:
    lines = [
        "| scenario | profile | saved | save tok | of which template tok | save calls | refusals | warnings | note |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        if r["condition"] != "engmem":
            continue
        o, c = r["outcome"], r["cost"]
        lines.append(
            f"| {_label(r)} | {r['profile']} | {_flag(o['saved'])} | {c['save_tokens']} "
            f"| {tokens(c['save_template_bytes'])} | {c['save_calls']} | {o['save_refusals']} "
            f"| {o['save_warnings']} | {r['save_note'] or ''} |"
        )
    return lines


def _tie(a: int, b: int) -> bool:
    return abs(a - b) <= TIE_BAND * max(a, b)


def sides(runs: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Strong sides, weak sides and ties of engmem on the to-answer cost, computed from the runs only."""
    strong: list[str] = []
    weak: list[str] = []
    ties: list[str] = []
    for label, group in _by_scenario(runs).items():
        engmem, grep, none = _pick(group, PRIMARY), _pick(group, "grep"), _pick(group, "no-history")
        if engmem is None:
            continue
        name = f"Class {label} ({group[0]['class_name']})"
        e_ok = engmem["outcome"]["correct"]
        e_tok, e_calls = _to_answer(engmem)
        if grep is not None:
            g_ok = grep["outcome"]["correct"]
            g_tok, g_calls = _to_answer(grep)
            if e_ok and not g_ok:
                strong.append(f"{name}: {PRIMARY} is correct, grep is not.")
            if g_ok and not e_ok:
                weak.append(f"{name}: grep is correct, {PRIMARY} is not.")
            if not e_ok and not g_ok:
                weak.append(f"{name}: {PRIMARY} fails like grep; neither reaches the answer.")
            if e_ok and g_ok:
                comparison = f"{e_tok} vs {g_tok} tokens ({_pct(e_tok, g_tok)}), {e_calls} vs {g_calls} calls"
                if _tie(e_tok, g_tok):
                    ties.append(f"{name}: correct like grep with about the same tokens to the answer: {comparison}.")
                elif e_tok < g_tok:
                    note = _chain_note(grep)
                    strong.append(
                        f"{name}: correct like grep with fewer tokens to the answer: {comparison}."
                        + (f" {note[0].upper()}{note[1:]}" if note else "")
                    )
                else:
                    weak.append(f"{name}: correct like grep with more tokens to the answer: {comparison}.")
                if e_calls > g_calls:
                    weak.append(f"{name}: more calls to the answer than grep: {e_calls} vs {g_calls}.")
        if none is not None:
            n_ok = none["outcome"]["correct"]
            if e_ok and not n_ok:
                strong.append(f"{name}: correct where no history is not.")
            if n_ok and e_ok:
                weak.append(
                    f"{name}: no history reaches the same outcome at zero cost; {PRIMARY} spends "
                    f"{e_tok} tokens and {e_calls} calls to the answer."
                )
            if not e_ok and not n_ok:
                weak.append(f"{name}: {PRIMARY} fails like no history.")
        for other in group:
            if other["condition"] != "engmem" or other["profile"] == PRIMARY:
                continue
            if e_ok and not other["outcome"]["correct"]:
                weak.append(f"{name}: profile {other['profile']} misses what {PRIMARY} finds.")
        if engmem["outcome"]["saved"] is not None:
            weak.append(
                f"{name}: saving costs {PRIMARY} another {engmem['cost']['save_tokens']} tokens and "
                f"{engmem['cost']['save_calls']} calls, which grep and no history never pay."
            )
    return strong, weak, ties


def _ledger_rows(runs: list[dict]) -> list[str]:
    lines = [
        "| scenario | profile | harness-measured engmem bytes | cost.jsonl bytes |",
        "|---|---|---|---|",
    ]
    for r in runs:
        if r["engmem_observed_bytes"] is None:
            continue
        measured = sum(
            s["bytes"] for s in r["steps"]
            if s["action"] in ENGMEM_RETRIEVAL_ACTIONS and s["phase"] in ("search", "load")
        )
        lines.append(f"| {_label(r)} | {r['profile']} | {measured} | {r['engmem_observed_bytes']} |")
    return lines


def _template_failures(runs: list[dict]) -> list[str]:
    failures = []
    for r in runs:
        for check, passed in r["template_checks"].items():
            if not passed:
                failures.append(f"- scenario {_label(r)}, {r['profile']}: {check}")
    return sorted(set(failures))


HOW_TO_READ = (
    "How to read it: scripted agents follow the declared policies of contracts/e2e.md, never tuned per "
    "scenario. `to-answer` is everything that entered the context before the agent can answer: the start "
    "template the host injected (command copy, or MCP prompt plus tool definitions) and the output of the "
    "start, search and read calls. `save` is the save template plus the save calls' output, counted apart: "
    "grep and no history have no save. The strong and weak sides compare to-answer cost only. engmem CLI "
    "wall times include a Python interpreter start per call; grep and file reads run in-process, so their "
    "times are a lower bound. grep's search output is cut to its top "
)


def render(results: dict) -> str:
    meta, runs, tasks = results["meta"], results["runs"], results["tasks"]
    by_task = {t["id"]: t for t in tasks}
    strong, weak, ties = sides(runs)
    lines = [
        "# engmem end-to-end harness: Layer 1 report",
        "",
        f"Generated {meta['generated_at']} on {meta['platform']}, Python {meta['python']}. "
        f"Base store {meta['base_store_docs']} documents (mean {meta['mean_doc_bytes']} bytes), scale store "
        f"{meta['scale_store_docs']} documents. Harness time {meta['harness_seconds']} s."
        + (" Smoke plan: a reduced set of profiles per class." if meta.get("smoke") else ""),
        "",
        f"Tokens are an approximation: {meta['token_method']}. Real counts come only from Layer 2.",
        "",
        HOW_TO_READ + f"{meta.get('grep_top_lines', 20)} lines (`head`), so its search bytes do not grow with "
        "the store; the store-size table shows how many files it matched before the cut.",
        "",
        "## Summary",
        "",
        f"engmem columns are the {PRIMARY} profile.",
        "",
        "| scenario | declared winner | observed | engmem | grep | no history | engmem to-answer tok / calls "
        "| grep to-answer tok / calls | engmem save tok / calls |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for label, group in _by_scenario(runs).items():
        task = by_task[group[0]["task"]]
        engmem, grep, none = _pick(group, PRIMARY), _pick(group, "grep"), _pick(group, "no-history")
        lines.append(
            f"| {label}. {task['class_name']} | {task['expected_winner']} | {_observed(engmem, grep, none)} "
            f"| {_correct(engmem)} | {_correct(grep)} | {_correct(none)} "
            f"| {_cost(engmem, 'to_answer_tokens')} / {_cost(engmem, 'to_answer_calls')} "
            f"| {_cost(grep, 'to_answer_tokens')} / {_cost(grep, 'to_answer_calls')} "
            f"| {_cost(engmem, 'save_tokens')} / {_cost(engmem, 'save_calls')} |"
        )
    lines += ["", "## Strong sides", ""] + [f"- {s}" for s in strong or ["None measured."]]
    lines += ["", "## Weak sides", ""] + [f"- {w}" for w in weak or ["None measured."]]
    lines += ["", f"## Ties (within {TIE_BAND:.0%} of grep's tokens)", ""] + [f"- {t}" for t in ties or ["None."]]
    for label, group in _by_scenario(runs).items():
        task = by_task[group[0]["task"]]
        lines += [
            "",
            f"## Class {label}: {task['class_name']}",
            "",
            f"Task: \"{task['description']}\" (checkout `{task['repo']}`, store of {group[0]['store_docs']} "
            f"documents). Declared winner: {task['expected_winner']}. Without history: {task['without_history']}.",
            "",
        ]
        lines += _class_table(group)
        note = _chain_note(_pick(group, "grep"))
        if note:
            lines += ["", f"Note: {note}"]
        saver = next((r["saver"] for r in group if r.get("saver")), None)
        if saver:
            lines += [
                "",
                f"Saver: {saver['profile']} saved `{saver['doc_id']}` (saved: {_flag(saver['saved'])}, "
                f"{saver['calls']} calls, {saver['total_tokens']} tokens, refusals {saver['refusals']}); "
                "its cost is not in the rows above. Without engmem nothing is saved, so grep and no history "
                "have nothing to find.",
            ]
    lines += [
        "", "## Store size: class 1 on the base store beside class 7 on the larger store", "",
        "Search cost against store size only; the outcome and token gaps of class 7 have their own causes, "
        "named in its section.", "",
    ] + _scaling(runs)
    lines += ["", "## Save cost", ""] + _save_table(runs)
    failures = _template_failures(runs)
    lines += ["", "## Template checks", ""]
    lines += failures or ["Every rendered copy and prompt named the steps its profile ran."]
    lines += ["", "## engmem's own ledger", "",
              "What the harness counted from engmem's search and read output beside what `cost.jsonl` "
              "recorded for the same session. The CLI rows include stderr lines and the closing `note:` "
              "lines; a run without a draft has no session and no row.", ""]
    lines += _ledger_rows(runs)
    lines += ["", "## Raw numbers", "", "Every step of every run, with its bytes and seconds, is in `results.json`.", ""]
    return "\n".join(lines)
