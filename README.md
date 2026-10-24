<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AlejandroKolio/engmem/main/docs/brand/engmem-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/AlejandroKolio/engmem/main/docs/brand/engmem-light.png">
    <img alt="engmem" src="https://raw.githubusercontent.com/AlejandroKolio/engmem/main/docs/brand/engmem-light.png" width="200">
  </picture>
</p>

# engmem

[![PyPI](https://img.shields.io/pypi/v/engmem.svg)](https://pypi.org/project/engmem/)
[![CI](https://github.com/AlejandroKolio/engmem/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/AlejandroKolio/engmem/actions/workflows/ci.yml?query=branch%3Amain)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/AlejandroKolio/engmem/blob/main/LICENSE)

**Your AI coding agents forget. Your engineering decisions shouldn't.**

Persistent engineering memory for AI coding agents. engmem keeps what a task decided, and why,
as local Markdown outside your repositories, finds it again before the next task is planned, and
records when an earlier decision shaped new work.

[See it in action](https://github.com/AlejandroKolio/engmem#see-it-in-action) · [Quick start](https://github.com/AlejandroKolio/engmem#quick-start) · [Why engmem](https://github.com/AlejandroKolio/engmem#why-engmem) ·
[How it works](https://github.com/AlejandroKolio/engmem#how-it-works) · [Supported agents](https://github.com/AlejandroKolio/engmem#supported-agents) · [Privacy](https://github.com/AlejandroKolio/engmem#privacy-and-limitations)

## Stop rediscovering the same engineering decisions

You spend an afternoon finding out why the widget cache serves stale rows: warm-up ran before
schema validation and filled the cache with rows the new schema rejects. You fix it and move on.
Two weeks later a new session, maybe in another agent or another repository, is asked to speed up
startup. Nothing in the code says why warm-up waits, so moving it earlier looks like a free win.

engmem keeps that reasoning. At the start of a task the agent searches the store, sees the earlier
decision and the alternative it rejected, and plans with them in view.

## See it in action

**Earlier session.** `/engmem.save` recorded the decision in the task's document:

```markdown
## Decision Log

Warm-up runs after the schema check, never before it. Warming first filled the cache with rows
the new schema rejects.
```

**Later session.** `/engmem speed up widget cache startup` searches the store before planning.
The agent gets a ranked top 3, each hit with the sections worth opening:

```text
### 1000001-widget-cache (score: 7.6)
path: ~/Developer/engmem/sessions/1000001-widget-cache.md
snapshot: platform-core: unknown, no anchor recorded
matched: entities=widgetcache,widget,cache; §1-decision-log=cache,warm,up
  §1-decision-log   0.1 KB   Warm-up runs after the schema check, never before it.

old-cache-note: superseded by 1000001-widget-cache
cite as (Reuse Log prior-doc): 1000001-widget-cache@d126af1fe9cae890
docs: 5 (4 partial spine) | drafts: 0 | last doc: 61d ago
```

**Outcome.** The plan keeps warm-up behind the schema check. An older note that said "warm at
boot" was superseded, so it shows only as a redirect to the current document. When the session
is saved, its Reuse Log quotes the line it took, cited as `1000001-widget-cache@d126af1fe9cae890`.

*The store is a small illustrative one; the output is what `engmem search "WidgetCache warm-up"
--session <draft-id>` prints for it, path shortened.* For one task end to end with the call path through the code, see the
[Anatomy of engmem](https://alejandrokolio.github.io/engmem/engmem-anatomy.html).

## Quick start

In your terminal:

```bash
uv tool install engmem          # or: pipx install engmem
engmem install --agent claude   # slash commands and a trigger rule for Claude Code
engmem doctor --agent claude    # check the setup; changes nothing
```

In Claude Code:

```text
/engmem fix the widget cache warm-up     start: open a draft, search before planning
  ... work as usual ...
/engmem.save                             finish: decisions, rejected options, Reuse Log
```

`/engmem.save.quick` keeps the decision, its reason, the rejected alternative, its source and a
short primer for the next task. Once Codex is connected (`engmem install --agent codex`),
`engmem walkthrough codex --dir ~/demo` runs the whole loop on a demo store. Other agents:
[Supported agents](https://github.com/AlejandroKolio/engmem#supported-agents).

## Why engmem

**Remember decisions, not just chats.** A transcript tells the next session what was said; it
needs what was decided. A full `/engmem.save` writes a structured document with a Decision Log,
the alternatives that were rejected, landmines and lessons, and a cold-start primer for the next
agent, with every claim marked `[Verified]`, `[Assumed]` or `[Open]`.

**Knowledge across sessions and repositories.** A story often spans a backend, a frontend and
several days. The store lives outside every repository, one document per task, so a decision made
in one repository is found from another, and one store serves Claude Code, Codex and Copilot alike.
Records name their repositories, and a search can be limited to one with `--repo`.

**Evidence of memory reuse.** Every search is logged. A saved session quotes the prior text that
changed a decision and cites it by `<id>@<version>`, the exact version the agent was shown, so a
reviewer can check what memory actually contributed.

**Your memory, your files.** Plain Markdown with YAML front matter, readable and greppable without
engmem. No database, no daemon, no open port, no hosted service. Two dependencies: `pyyaml` and
`markdown-it-py`.

## How it works

1. **Start.** `/engmem <task>` opens a draft for the session.
2. **Search.** Before the agent proposes a plan, it searches the store. Output is capped at 4 KB, so
   one search never costs the agent more context than that; the agent then reads only the sections
   it needs, with `engmem read <id> --role decisions`.
3. **Work.** The agent does the task as usual, with the earlier decisions in view.
4. **Save.** `/engmem.save` completes the draft: decisions, rejected alternatives, what the next
   session needs, and a Reuse Log row for each prior document that changed a decision. Saving is an
   explicit step; nothing is captured in the background.
5. **Reuse.** The next related task finds this document. A replaced document points to its
   successor with `superseded_by`, and search sends the agent to the current one.

Sessions run in `daily` mode by default. `engmem mode set research` adds a naive plan written
without memory before the first search, for the comparison described below.

## Measuring memory usefulness

Memory that is retrieved but never changes a decision only costs context. engmem records enough to
tell the two apart:

- **Search telemetry.** `engmem telemetry` shows searches, hit rate and context spent.
- **Delivered cost.** `engmem cost summary --session <draft-id>` shows the bytes engmem delivered to
  one session and what it could not observe; it never claims a saving.
- **Reuse Logs and version-aware citations.** Each row quotes the span taken and cites the version
  it came from.
- **Human assessment.** `engmem feedback summary` counts finds the user marked helped,
  not-applicable or harmful, and finds whose influence is unknown.
- **Research mode.** Before searching, `/engmem` writes a naive plan made without memory, so the
  final plan can be compared against it. `tools/gate1_report.py`, run from a source checkout, builds
  the Reuse Log table.

A Reuse Log row is observed reuse: it shows that a prior decision informed the work, not that the
work got faster or better. That still takes a human comparing outcomes.

## Supported agents

| Agent | `--agent` | How it connects |
|---|---|---|
| Claude Code | `claude` | slash commands and a trigger rule in `~/.claude/` (`--local`: this project only) |
| Codex | `codex` | skills, a trigger rule and an MCP server entry |
| Copilot CLI | `copilot-cli` | skills in `~/.copilot/skills/` |
| Copilot in the IDE | `copilot-ide` | prompt files and a trigger rule in `./.github/`; run from the repo root |
| Claude Desktop | `claude-desktop` | an MCP server entry; the commands arrive as prompts |
| ChatGPT | `chatgpt` | needs a tunnel you set up; see [remote access](https://github.com/AlejandroKolio/engmem/blob/main/docs/remote-access.md) |

Run `engmem doctor --agent <agent>` to check a setup and `engmem uninstall --agent <agent>` to
remove it; uninstall never touches your documents. Files written per agent and each one's details:
the [guide](https://github.com/AlejandroKolio/engmem/blob/main/docs/guide.md).

## Privacy and limitations

engmem itself makes no network calls. Your documents stay in the store you choose
(`engmem store set PATH`, default `~/Developer/engmem`), and engmem reads nothing through a link
inside it. Its git commands only read, except `git init` in a new store, and `git init` plus one
commit in each of the walkthrough's demo repositories. Outside the store it writes only the agent files and config entries that
`install` sets up, its two one-line settings, a parse cache, the one file an `export` names, and
the demo directory you give `walkthrough`.

The coding agent is a separate matter: whatever it reads from the store goes to its model provider
under that provider's terms. Your store usually describes proprietary code; keep it out of public
repositories. Exposing it to ChatGPT means running a tunnel, so read
[remote access](https://github.com/AlejandroKolio/engmem/blob/main/docs/remote-access.md) first.
Details: [docs/privacy.md](https://github.com/AlejandroKolio/engmem/blob/main/docs/privacy.md).

Limitations: saving is a step the agent or you take, not automatic capture; search is lexical
ranking over Markdown, not semantic retrieval; there is no sync or sharing between people beyond
the files themselves and `engmem export`.

## Learn more

- [Guide](https://github.com/AlejandroKolio/engmem/blob/main/docs/guide.md): each agent's setup, the store, modes, the document format and every command.
- [Anatomy of engmem](https://alejandrokolio.github.io/engmem/engmem-anatomy.html): one task end to end, with the real output.
- Walkthroughs: [Codex](https://github.com/AlejandroKolio/engmem/blob/main/docs/walkthrough-codex.md), [a handoff between Codex and Claude Code](https://github.com/AlejandroKolio/engmem/blob/main/docs/walkthrough-handoff.md).
- [ENGMEM-SPEC.md](https://github.com/AlejandroKolio/engmem/blob/main/ENGMEM-SPEC.md) and [design contracts](https://github.com/AlejandroKolio/engmem/tree/main/docs/design/contracts): what engmem promises and why.

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
