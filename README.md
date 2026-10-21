# engmem

[![PyPI](https://img.shields.io/pypi/v/engmem.svg)](https://pypi.org/project/engmem/)
[![CI](https://github.com/AlejandroKolio/engmem/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/AlejandroKolio/engmem/actions/workflows/ci.yml?query=branch%3Amain)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/AlejandroKolio/engmem/blob/main/LICENSE)

**Engineering memory for AI coding agents, kept as plain markdown you own.**

A coding agent starts each session without what earlier sessions decided. engmem keeps what was
decided on a task, and why, in a local store outside every repository, finds it again when the
next task starts, and records whether it changed anything. One document holds a story that spans
a backend, a frontend and three days of sessions.

- **Plain files.** One markdown document per task, readable and greppable without the tool.
- **Bounded search.** Each search prints at most 4 KB, so one search never costs the agent more
  than that.
- **Accountable.** Every search is logged, and a saved session quotes the prior document that
  changed a decision, checked against the exact version it was shown.
- **Local.** No database, no daemon, no open port, no network calls. Two dependencies: `pyyaml` and
  `markdown-it-py`.

## Install

```bash
uv tool install engmem          # or: pipx install engmem
engmem install --agent claude   # wire it into your agent
```

| Agent | `--agent` | What it sets up |
|---|---|---|
| Claude Code | `claude` | slash commands in `~/.claude/commands/` and a trigger rule in `~/.claude/CLAUDE.md` (`--local`: `./.claude/commands/`, `./CLAUDE.md`) |
| Claude Desktop | `claude-desktop` | an MCP server entry in Claude Desktop's `claude_desktop_config.json`; the commands arrive as prompts |
| Codex | `codex` | skills in `~/.agents/skills/`, a trigger rule in `~/.codex/AGENTS.md` and an MCP server entry in `~/.codex/config.toml` |
| Copilot CLI | `copilot-cli` | skills in `~/.copilot/skills/` |
| Copilot in the IDE | `copilot-ide` | prompt files in `./.github/prompts/` and a trigger rule in `./.github/copilot-instructions.md`; run from the repo root |
| ChatGPT | `chatgpt` | prints the tunnel setup; see [remote access](https://github.com/AlejandroKolio/engmem/blob/main/docs/remote-access.md) |

Run `engmem doctor --agent <agent>` to check the setup, and `engmem uninstall --agent <agent>` to
remove it. Uninstall never touches your documents.

## How it works

```text
/engmem fix the widget cache warm-up     start: search the store before planning
  ... work ...
/engmem.save                             finish: record decisions and what was reused
```

1. **Start.** `/engmem <task>` opens a draft for the session and searches the store before the
   agent proposes a plan.
2. **Search.** The agent gets a ranked top 3, each hit with the section worth opening and the
   sentence in it that matched:

   ```text
   ### 1000001-widget-cache (score: 7.2)
   path: /home/you/Developer/engmem/sessions/1000001-widget-cache.md
   matched: entities=widgetcache,widget,cache; §2-decision-log=cache,warm,up
     §2-decision-log   0.1 KB   Warm-up runs after the schema check, never before it.

   old-cache-note: superseded by 1000001-widget-cache
   cite as (Reuse Log prior-doc): 1000001-widget-cache@811c3af96cf357bb
   docs: 5 | drafts: 0 | last doc: 58d ago
   ```

   That came from `engmem search "WidgetCache warm-up"`. The older note that said otherwise was
   superseded, so it appears only as a redirect to the current document.

3. **Save.** `/engmem.save` writes the session up: decisions, rejected alternatives, what the
   next session needs to know, and a Reuse Log row quoting each prior document that changed a
   decision. `/engmem.save.quick` keeps just the decision and its reason.
4. **Measure.** In research mode `/engmem` first writes a naive plan, before it searches, so you
   can later see whether memory changed the outcome. `engmem telemetry` and `engmem feedback
   summary` add it up; `tools/gate1_report.py`, run from a source checkout, builds the Reuse Log
   table.

## Commands

| In your agent | |
|---|---|
| `/engmem <task>` | start a task: open a draft, search, report what was found |
| `/engmem.save` | finish it: the full document and its Reuse Log |
| `/engmem.save.quick` | finish it in one step: decision, reason, rejected alternative |

| In the shell | |
|---|---|
| `engmem search "<words>"` | ranked search; `--role decisions` for one section, `--repo NAME` for one repository |
| `engmem read <id>` | print a document, counted and citable |
| `engmem export <id>` | preview a copy for a colleague, with links to your other notes cut; `--out FILE --confirm <fingerprint>` writes it |
| `engmem store set PATH` | choose where the store lives (default `~/Developer/engmem`) |
| `engmem mode set research` | run the measured protocol (default: `daily`) |
| `engmem doctor --agent <agent>` | check an agent's setup without changing it |
| `engmem walkthrough codex --dir ~/demo` | try the whole loop on a demo store |
| `engmem telemetry` | searches, hit rate and context spent |

The [guide](https://github.com/AlejandroKolio/engmem/blob/main/docs/guide.md) covers each agent's setup, the store, modes and every command.

## A document

```markdown
---
id: 1000001-widget-cache
title: Widget cache eviction
date: 2026-08-24
task_date: 2026-08-24
status: active
tags: [platform, caching]
entities: [WidgetCache, CacheWarmer]
repos: [platform-core]
---

## 1. Executive Summary
## 8. Decision Log
## 13. Future LLM Context (cold-start primer)
## 16. Reuse Log
```

No field is required: a document with partial front matter still loads, and engmem names what is
missing. A document that is replaced points to its successor with `superseded_by`, and search
sends the agent to the current one.

## Privacy

engmem makes no network calls. Its git commands only read, except `git init` in a new store and
in the walkthrough's demo repositories. Your documents stay in the store you choose, and engmem
reads nothing through a link inside it. Outside the store it writes only the agent files and
config entries listed under Install, its two one-line settings, a parse cache, the one file an
`export` names, and the demo directory you give `walkthrough`. Your store usually describes
proprietary code; keep it out of public repositories. Details: [docs/privacy.md](https://github.com/AlejandroKolio/engmem/blob/main/docs/privacy.md).

## Learn more

- [Guide](https://github.com/AlejandroKolio/engmem/blob/main/docs/guide.md): each agent's setup, the store, modes and every command.
- [Anatomy of engmem](https://alejandrokolio.github.io/engmem/engmem-anatomy.html): one task end
  to end, with the real output and the call path through the code.
- Walkthroughs: [Codex](https://github.com/AlejandroKolio/engmem/blob/main/docs/walkthrough-codex.md), [a handoff between Codex and Claude
  Code](https://github.com/AlejandroKolio/engmem/blob/main/docs/walkthrough-handoff.md).
- [ENGMEM-SPEC.md](https://github.com/AlejandroKolio/engmem/blob/main/ENGMEM-SPEC.md) and [design contracts](https://github.com/AlejandroKolio/engmem/tree/main/docs/design/contracts): what engmem
  promises and why.

## Development

```bash
uv sync --group dev
uv run pytest -q
```

See [CONTRIBUTING.md](https://github.com/AlejandroKolio/engmem/blob/main/CONTRIBUTING.md). The principles the code is held to are in
`ENGMEM-SPEC.md` §10: tests first and never weakened, the standard library over dependencies,
errors stated loudly, and examples invented rather than taken from real work.

## License

[MIT](https://github.com/AlejandroKolio/engmem/blob/main/LICENSE) © Alexander Shakhov
