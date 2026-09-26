# engmem

[![CI](https://github.com/AlejandroKolio/engmem/actions/workflows/ci.yml/badge.svg)](https://github.com/AlejandroKolio/engmem/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Personal engineering memory for AI-assisted development, built around two things most
memory tools do not do.

**A story is the unit, and the store lives outside every repository.** One engineering
story spans several repos and several sessions — a front end, a back end, a third service,
across days. Per-repo memory files cannot hold it, and a pile of context-free facts cannot
reconstruct it.

**It measures whether remembering actually changed anything.** Every session pre-registers
its intent *before* searching, and closes with a Reuse Log row quoting, verbatim, the prior
document that changed a decision. `tools/verify_citations.py` checks those quotes against
the documents they cite. The point is not to accumulate documents; it is to find out
whether accumulated documents pay for themselves.

Underneath: markdown files in a local git-tracked store, deterministic search, two runtime
dependencies, no database, no server, no network calls.

**[Anatomy of engmem](https://alejandrokolio.github.io/engmem/engmem-anatomy.html)** walks one task end to end — the command
run at each step, the real output it returns, and the call path through the code. It is a
standalone HTML page: open it from a checkout, since GitHub serves `.html` as source.

## The loop

Everything else in this README is plumbing that exists so this loop can run.

**1. Pre-register.** `/engmem <task>` writes a two-line naive plan *before* it searches
anything — obtained from a sub-agent that knows only the task, so the baseline is not
contaminated by what the store already told you. It never asks you for it and never waits
on it.

**2. Search, on the record.** `engmem search` is a bounded channel, not a better grep: its
output is capped at 4 KB and every call writes a row to `telemetry.jsonl` — the query, what
surfaced, hit or miss, bytes of context spent. An agent grepping around freely may well
find the same document; what it cannot do is leave a measurable trace, or promise the
context it pulls in has any ceiling at all.

**3. Work.**

**4. Record what was actually reused.** `/engmem.save` closes the session with a Reuse Log:
one row per prior document that changed a decision, each carrying a verbatim quote from it.
A row without a quote is invalid. If nothing was reused, the section says exactly
`Prior docs used: none.` — the most useful answer the experiment can get, and the easiest
to quietly avoid writing.

**5. Judge.** `tools/gate1_report.py` collects every Reuse Log row into one table and marks
each citation *distant* or *adjacent* by the rule pre-registered in `ENGMEM-SPEC.md` §11.
Whether it genuinely changed a decision is a human column, filled in by hand.

## What this is not

- **Not a vector database.** No embeddings, no index file, no second source of truth.
- **Not another memory bank.** Those are per-repository and per-agent; a story here spans
  repositories and outlives the session that wrote it.
- **Not a search engine competing with grep.** The engine is the sensor: bounded output so
  the cost is known, logged calls so the retrieval can be audited afterwards.

## Install

From a source checkout:

```bash
uv tool install --editable .   # or: pipx install .
```

Then wire it into your AI coding agent:

```bash
engmem install --agent claude            # templates -> ~/.claude/commands/ (Claude Code)
engmem install --agent claude --local    # templates -> ./.claude/commands/ (this repo only)
engmem install --agent copilot-ide       # templates -> ./.github/prompts/ (Copilot's IDE plugin)
engmem install --agent copilot-cli       # skills -> ~/.copilot/skills/ (Copilot CLI)
engmem install --agent claude-desktop    # MCP server entry (Claude Desktop)
```

Pick `copilot-ide` if you use the Copilot chat panel in VS Code / Visual Studio /
JetBrains; pick `copilot-cli` if you use the standalone `copilot` CLI, which reads
skills instead of prompt files. Pick `claude-desktop` if you use the desktop
app, which cannot run shell commands — it reaches engmem over MCP instead, and the
same commands arrive as prompts. `copilot-ide` and `--local` must be run from a repo
root (a directory containing `.git`) — they refuse to run anywhere else.

`install` is idempotent — re-running it upgrades templates in place and never duplicates
the trigger rule or touches existing store content.

To remove the wiring again, mirror it with the same `--agent`:

```bash
engmem uninstall --agent claude          # or copilot-ide / copilot-cli, plus --local
```

`uninstall` deletes the templates it installed and the trigger rule it appended, reports
the counts, and **never touches the store** — those are your documents, and they stay
readable as plain markdown without the tool. It prints the store path so you can remove
it yourself if you want to. Running it twice is safe.

The store location resolves in this order: `--store PATH` → `$ENGMEM_HOME` →
`~/Developer/engmem`. It holds `sessions/*.md` (the documents) and `telemetry.jsonl`
(append-only search log). No config files — which also means `install --store PATH` is not
remembered: the installed templates resolve `$ENGMEM_HOME`, else the default, every time
they run. Install says so when the two differ; set `ENGMEM_HOME` to the store you want the
agent to use. `--agent claude-desktop` is the exception, since its MCP entry records the
path itself.

## Usage

The daily loop runs through three agent slash commands installed above:

- `/engmem <task>` — start a task: records your intended approach (pre-reg) **before**
  searching, creates a draft session document, searches the store for relevant prior
  work, and reports what it found before proposing any plan.
- `/engmem.save` — finish a task: drafts the full document metadata from the diff and
  conversation for one-round approval, fills in decisions, rejected alternatives,
  lessons learned, and a Reuse Log of which prior documents actually influenced the work.
- `/engmem.save.quick` — save without the review round: no YAML check, no supersede question, and no quote review with the user (the MCP path still refuses a Reuse Log row without a quote); a single `ok` produces a minimal but valid document.

Direct search from the shell:

```bash
engmem search "SomeController"
engmem search "XYZ" --store /path/to/store
```

Output is a pasteable top-3 (id, path, score, why-matched, cold-start primer excerpt,
related docs, matched section locators) capped at 4 KB, with a footer scoreboard:
`docs: N | drafts: N | last doc: Nd ago`. No match prints `prior context: none found`
(exit 0). Ambiguous short queries (e.g. `MQ` matching both MessageQueue and
MetricsQuery) return one representative per cluster, marked `ambiguous`.

Ask for one section rather than whole documents:

```bash
engmem search "cache eviction" --role decisions   # what was decided, and what was rejected
engmem roles                                      # the 19 role names it understands
```

Word matching cannot answer "what did we reject and why" — those words appear nowhere in
the answer. Role addressing does, by resolving section headings across their spelling
variants, and it returns the section instead of the document: a few hundred bytes rather
than tens of kilobytes.

Two more commands:

```bash
engmem backfill --all      # propose front matter for documents written before engmem
engmem telemetry           # searches run, hit rate, context spent, split by channel
```

`backfill` never writes without showing the proposal first, leaves the document body
byte-identical, and is idempotent.

## What a document looks like

Plain markdown with a YAML header — readable, and greppable, without this tool:

```markdown
---
id: 1000001-widget-cache
title: Widget cache eviction
date: 2026-08-24
status: active
tags: [platform, caching]
entities: [WidgetCache, CacheWarmer]
related: [ttl-revalidation-v2]
covers_files: [WidgetCache.java]
---

## Pre-reg
## 1. Executive Summary
...
## 8. Decision Log
## 13. Future LLM Context (cold-start primer)
## 16. Reuse Log
## Search Trace
```

`/engmem.save` writes nineteen sections; `/engmem.save.quick` writes five (the list is in `ENGMEM-SPEC.md` §4). Every claim
carries `[Verified]`, `[Assumed]`, or `[Open]`, so the document says what it does not
know instead of staying silent about it.

No field is required. A document with missing or partial front matter still loads, and
what is missing is named on stdout — engmem never hides a document from you because its
metadata was incomplete.

## Development

```bash
uv sync --group dev
uv run pytest -q
```

Python ≥3.11. Runtime dependencies: `pyyaml` and `markdown-it-py`, and nothing else
(hard constraint — see `ENGMEM-SPEC.md` §2). Search correctness is defined by the golden fixture tests in
`tests/test_scoring.py` / `tests/test_cli.py`; at any conflict between the scoring
formula and a golden test, the test wins.

## What engmem touches on your machine

**No network.** No telemetry upload, no model calls, no fetching at runtime. Search,
ranking, and storage are entirely local. Two runtime dependencies, `pyyaml` and
`markdown-it-py`; neither opens a socket.

**Your store is private.** engmem reads and writes markdown under a directory you choose.
That store commonly holds notes about proprietary code — it lives outside this repository
and should stay that way. There is no encryption at rest; protect it the way you protect
the source it describes.

**Writes stay inside `sessions/`.** The MCP write tools resolve every path first and
compare after, so `..` segments, absolute paths, symlinks pointing out of the store, and
ids containing separators or NUL bytes are refused. Writes stage to a temporary file,
re-validate, then `os.replace`, so a crash mid-write cannot leave half a document.

**The cache is JSON, never pickle.** Token counts live under `~/.cache/engmem/`, which any
process running as you can write to. Unpickling from there would execute whatever it
contained; JSON cannot. A corrupted entry degrades to a cache miss.

**Installing touches known paths only.** `engmem install` writes templates into the agent
directory you name and, for `claude-desktop`, adds one key to that app's config. `engmem
uninstall` removes exactly what it added and never touches the store. The two files engmem
edits but does not own — your instructions file and that config, which holds every other
MCP server you configured — are replaced atomically, through any symlink rather than over
it, so a failed write leaves them exactly as they were.

One thing to know: `engmem search` prints matched document text to stdout. In a shared
terminal or a logged CI job, that text goes wherever the output goes.

## Contributing

The engineering principles this codebase is held to are `ENGMEM-SPEC.md` §10. The short
version: tests first and never weakened, standard library over dependencies, errors loud
on stdout, and examples in tests and docs invented rather than taken from real work.

## License

[MIT](LICENSE) © Alexander Shakhov
