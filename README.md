# engmem

[![CI](https://github.com/AlejandroKolio/engmem/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/AlejandroKolio/engmem/actions/workflows/ci.yml?query=branch%3Amain)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Personal engineering memory for AI-assisted development, built around two things most
memory tools do not do.

**A story is the unit, and the store lives outside every repository.** One engineering
story spans several repos and several sessions — a front end, a back end, a third service,
across days. Per-repo memory files cannot hold it, and a pile of context-free facts cannot
reconstruct it.

**It measures whether remembering actually changed anything.** In research mode every
session pre-registers its intent *before* searching, and closes with a Reuse Log row quoting,
verbatim, the prior document that changed a decision. `tools/verify_citations.py` checks those quotes against
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
on it. This step runs in research mode only; in daily mode, the default, it is skipped
(see "Daily or research mode" below).

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
engmem install --agent codex             # skills -> ~/.agents/skills/, rule -> ~/.codex/AGENTS.md, MCP entry -> ~/.codex/config.toml
engmem install --agent chatgpt           # prints the Secure MCP Tunnel setup (ChatGPT)
```

Pick `copilot-ide` if you use the Copilot chat panel in VS Code / Visual Studio /
JetBrains; pick `copilot-cli` if you use the standalone `copilot` CLI, which reads
skills instead of prompt files. Pick `claude-desktop` if you use the desktop
app, which cannot run shell commands — it reaches engmem over MCP instead, and the
same commands arrive as prompts. `copilot-ide` and `--local` must be run from a repo
root (a directory containing `.git`) — they refuse to run anywhere else.

`codex` wires up both channels at once, because Codex (CLI, IDE extension and desktop app)
runs shell commands and MCP servers alike: the skills are invoked as `$engmem`,
`$engmem-save` and `$engmem-save-quick`, and the MCP server is there for the Codex surfaces
without a shell. Codex's default `workspace-write` sandbox keeps the CLI from writing to a
store outside the repository, and the skills reach for the shell first; add the store to
`[sandbox_workspace_write] writable_roots` in `~/.codex/config.toml`, or approve each write
when Codex asks. `$CODEX_HOME` is
honoured in place of `~/.codex`.

`chatgpt` writes nothing outside the store. ChatGPT runs no local MCP server — it only
calls servers it can reach — so `install` prints the
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
commands that run `engmem mcp` on your machine and let ChatGPT's developer mode reach it.
Whether the tunnel is available depends on your OpenAI workspace. engmem itself still opens
no port and makes no network call; `tunnel-client` does. Without a tunnel, a public bridge
(`supergateway` plus `ngrok` or `cloudflared`) works on any paid plan, but ChatGPT connects it
without authentication; run the server behind it as `engmem mcp --read-only`, so a leaked URL
can read the store but never write to it.

`install` is idempotent — re-running it upgrades templates in place and never duplicates
the trigger rule or touches existing store content.

To remove the wiring again, mirror it with the same `--agent`:

```bash
engmem uninstall --agent claude          # or any other --agent, plus --local if install used it
```

`uninstall` deletes the templates it installed and the trigger rule it appended, reports
the counts, and **never touches the store** — those are your documents, and they stay
readable as plain markdown without the tool. It prints the store path so you can remove
it yourself if you want to. Running it twice is safe. The two setting files below survive
it too, and a later install reuses them; delete `~/.config/engmem/` (`%APPDATA%\engmem\` on
Windows) for a full removal.

The store location resolves in this order: `--store PATH` → `$ENGMEM_HOME` → the store saved
with `engmem store set PATH` → `~/Developer/engmem`. It holds `sessions/*.md` (the documents)
and `telemetry.jsonl` (append-only search log). engmem has two one-line setting files, both in
`~/.config/engmem/` (`$XDG_CONFIG_HOME/engmem/`; `%APPDATA%\engmem\` on Windows): `store`, the
store path, written only by `engmem store set`, and `mode` (below). `install --store PATH`
does not save it — `--store` always means "this command only" — and install says so when the
store it just used is not the one later commands will find:

```bash
engmem store set ~/notes/engmem   # every command and installed template without --store uses it
engmem store show                 # the effective store, where it came from, and any MCP entry
                                  # (Claude Desktop, Codex) still pointing at a different store
```

**Daily or research mode.** By default engmem runs in daily mode: `/engmem` writes no Pre-reg
and launches no baseline sub-agent, and the session is kept outside the Gate 1 experiment.
Research mode runs the full experimental protocol: a baseline before the first search, or a
recorded `baseline_unavailable: <reason>` when none can be had.

```bash
engmem mode set research   # sessions started from now on run the Gate 1 protocol
engmem mode show           # mode: daily|research, and where it came from
```

Each session records its mode in its draft (`mode: daily` or `mode: research`) and keeps it;
switching applies to the next session. The `engmem` MCP prompt opens with the mode it read,
as its first line: `engmem mode for this session: research (saved choice in …)`.
`tools/gate1_report.py` lists daily sessions as outside the experiment; it keeps two kinds of
research session apart as well: one that recorded `baseline_unavailable` (an incomplete
observation) and one with neither a Pre-reg nor `baseline_unavailable` (research without a
baseline). Documents written before modes existed are counted as before.

A mode file that cannot be read never passes silently. `engmem mode show` exits 2 and names the
file. The session itself goes on in daily mode, the one choice that cannot put a wrong
observation into the experiment: `/engmem` tells you about the error, the MCP prompt's first
line says `daily (the saved mode cannot be read: …)`, and `engmem_create_draft` accepts only
daily drafts until `engmem mode set daily|research` rewrites the file.

Upgrading from an engmem without modes: re-run `engmem install --agent <agent>` for every agent
you installed. Templates and skills copied out by an older install write drafts without `mode`,
and the MCP `engmem_create_draft` refuses those (its message says so).

**Modes in Claude Desktop.** Desktop runs no shell, so set the mode once from a terminal:

```bash
engmem mode set research   # or: engmem mode set daily
engmem mode show
```

After upgrading engmem, quit Claude Desktop completely and reopen it: the MCP server loads
engmem's code when Desktop launches it, and keeps that code until it exits. The mode file
itself is read again on every call, so a later `mode set` needs no restart. To check, start
the `engmem` prompt from the chat box: the attachment (`+`) menu lists the connected MCP
servers, and under engmem its three prompts (menu names vary between Desktop versions). The
prompt's text is added to your message, and its first line names the mode:
`engmem mode for this session: …`. Desktop cannot
start a sub-agent, so in research mode the model writes the naive baseline itself before its
first search, and the Pre-reg ends with `pre-reg source: self (no sub-agent available)`. Desktop
does not see variables exported in your shell profile: if you set `XDG_CONFIG_HOME` only in the
terminal, `mode set` writes a file the Desktop server never reads. Leave it unset, or make it
visible to apps launched outside the terminal too.

A periodic check, from the engmem checkout (`tools/` is not in the package). The Reuse Log table
and the §11 figures always cover the whole store; `--since` narrows only the telemetry figures
of the audit-coverage block printed below them:

```bash
uv run python tools/gate1_report.py --store ~/Developer/engmem --since 2026-10-01   # telemetry since that date
uv run python tools/verify_citations.py --store ~/Developer/engmem   # exit 1 on a quote that does not match
engmem telemetry                                                     # searches, hit rate, context spent
```

When an agent finds nothing and you cannot tell why, `engmem doctor --agent codex` (or any other
`--agent`) checks that client's setup without changing it. It prints one line per check, marked
`ok`, `warning`, `error` or `unverified`. It covers the store and where it came from, whether
`sessions/` can be read and written, the `engmem` command and the MCP entry's Python, the
installed templates and their versions, the trigger rule, and whether the CLI and the MCP entry
use the same store. What the client's own sandbox allows is never marked `ok`, since doctor runs
outside it: it is `unverified`, or a `warning` when Codex's sandbox settings would block writes
to the store. The exit code is 1 when any line is an `error`.

If the saved file is unreadable or the store it names is missing, commands stop and name the
path; they never fall back to another store. `--agent claude-desktop` and `--agent codex`
record the store in their MCP entry at install time, so after a `store set` re-run the install
for those agents (`store show` prints the exact command); ChatGPT's `tunnel-client` profile
records it too, and `store show` does not inspect that profile. No documents are ever moved
for you.

## Usage

The daily loop runs through three agent slash commands installed above:

- `/engmem <task>` — start a task: in research mode, records a naive baseline (pre-reg)
  **before** searching; then creates a draft session document, searches the store for
  relevant prior work, and reports what it found before proposing any plan.
- `/engmem.save` — finish a task: drafts the full document metadata from the diff and
  conversation for one-round approval, fills in decisions, rejected alternatives,
  lessons learned, and a Reuse Log of which prior documents actually influenced the work.
- `/engmem.save.quick` — save the useful core of a decision: one preview with the decision, its reason, the rejected alternative, the source and a short context for the next task. Anything the session did not contain is marked as absent, never invented. A single `ok` publishes one valid, searchable document; declining publishes nothing and leaves the draft out of search. No YAML check, no supersede question, no quote review with the user (the MCP path still refuses a Reuse Log row without a quote).

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

Matching is exact, word for word, in any language that separates words with spaces or
punctuation: `ОТМЕНА, заказа!` finds "Отмена заказа", `CacheMémoire` finds itself and
`mémoire`. Case and Unicode spelling variants are folded; nothing else is. `cafe` does not
find `café`, `заказ` does not find `заказа`, and Chinese, Japanese or Thai text without
spaces is one long word. Upgrading re-tokenises cached documents on the next search; no
document needs re-saving.

Ask for one section rather than whole documents:

```bash
engmem search "cache eviction" --role decisions   # what was decided, and what was rejected
engmem roles                                      # the 19 role names it understands
```

Word matching cannot answer "what did we reject and why" — those words appear nowhere in
the answer. Role addressing does, by resolving section headings across their spelling
variants, and it returns the section instead of the document: a few hundred bytes rather
than tens of kilobytes.

Limit a search to one repository:

```bash
engmem search "Settings" --repo platform-core   # only records whose repos names platform-core
engmem search "Settings" --unscoped             # only records linked to no repository
```

The link is the `repos` front-matter field; a repository's name in the title or the tags does
not count. Names match regardless of case. A scoped result starts with a `scope:` line, and
each hit shows all of its `repos:`, so a story shared by two repositories appears once with
both. Without either flag the whole store is searched, as before. The MCP search tools take
the same scope as `repo` and `unscoped`. It narrows a search; it does not restrict access.

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
mode: research
---

## Pre-reg                  (research mode only)
## 1. Executive Summary
...
## 8. Decision Log
## 13. Future LLM Context (cold-start primer)
## 16. Reuse Log
## Search Trace
```

`/engmem.save` writes nineteen sections and `/engmem.save.quick` five (the list is in
`ENGMEM-SPEC.md` §4); a daily session has no Pre-reg, so eighteen and four. Every claim
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
ids containing separators or NUL bytes are refused. Writes stage to a temporary file and
re-validate before the document appears, so a crash mid-write cannot leave half a document.
A new draft is published with a hard link, which refuses an id that already exists (where
hard links are unavailable, a lock does the same), so of two calls creating one id exactly
one succeeds. Two engmem calls rewriting one document take turns (`os.replace` under a
lock), and finishing a draft can name the version it read, so a stale copy is refused
instead of replacing newer text.

**Two one-line setting files.** `~/.config/engmem/store` and
`~/.config/engmem/mode` (`$XDG_CONFIG_HOME/engmem/`; `%APPDATA%\engmem\` on Windows) are
written only by `engmem store set` and `engmem mode set`, each holding one line. Neither
`install` nor any other command writes them. Deleting a file returns that setting to its
default.

**`engmem doctor` changes nothing.** It reads configs and templates, runs `engmem --version`
and `<python> -m engmem.cli --version` with the MCP entry's Python, and writes one probe file into `sessions/`,
removed straight away, even when interrupted.

**The cache is JSON, never pickle.** Token counts live under `~/.cache/engmem/`, which any
process running as you can write to. Unpickling from there would execute whatever it
contained; JSON cannot. A corrupted entry degrades to a cache miss.

**Installing touches known paths only.** `engmem install` writes templates into the agent
directory you name and, for `claude-desktop` and `codex`, adds one server entry to that
app's config. `engmem uninstall` removes exactly what it added and never touches the store.
The files engmem edits but does not own — your instructions file and those configs, which hold every other
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
