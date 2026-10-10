# Walkthrough: hand a task from Codex to Claude Code and back

This walkthrough has Codex save a decision, Claude Code pick the task up from the same store,
save a decision of its own, and a new Codex session find that one by its id. It runs on a demo
store of its own. The command writes nothing outside the demo directory: your store, your
`~/.codex`, your `~/.claude`, your `~/.claude.json` and your saved engmem settings are not
changed by it. Claude Code itself, once you start it in step 2, records that you trusted `storefront/`
and keeps the session's transcript under your own `~/.claude.json` and `~/.claude/projects/`,
as it does for any folder (from its docs; not checked by running it).

It checks one pair of clients, Codex and Claude Code. Nothing here promises a duration.

## Before you start

- Codex is installed and signed in, and connected with `engmem install --agent codex`. The
  walkthrough uses the skills that command put in `~/.agents/skills`.
- Claude Code is installed and signed in, and connected with `engmem install --agent claude`.
  The walkthrough uses the `/engmem` commands that command put in `~/.claude/commands`.
- `git` is on `PATH`. The demo store and both workspaces are git repositories.
- Daily mode is enough (`engmem mode show`).

## Build the demo

```
engmem walkthrough handoff --dir ~/engmem-handoff
```

The directory must be new, empty, or one this walkthrough built before. The command refuses a
directory `engmem walkthrough codex` built, any other directory, and any path it would write
through that is a symbolic link or leads outside the directory. It creates:

| Path | What it is |
|---|---|
| `store/` | The demo store. Both clients use it. |
| `codex-home/` | The Codex config for the demo, the same one `walkthrough codex` writes. |
| `orders/` | Codex's workspace: `demo_orders/orders.py`, the order service. |
| `storefront/` | Claude Code's workspace: `demo_storefront/submit.py`, which sends orders. |
| `storefront/.mcp.json` | The engmem MCP server for Claude Code, with `--store` = the demo store. |
| `storefront/.claude/settings.json` | `env.ENGMEM_HOME` = the demo store, for Claude Code's shell commands. |

Each workspace gets one commit of its files, so a save can record the commit it was checked
at. The commit runs none of your git hooks and is not signed. Re-running the command writes only
missing files and never commits again.

Then it runs `engmem doctor --agent codex` and `engmem doctor --agent claude` against the demo
store, and stops with `not ready` when either reports an error, or a Codex warning about the
sandbox, the MCP entry or the MCP command. Fix what the line says and run the same command again.

## The steps

The command prints each step with the exact text to type.

1. **Codex saves the decision.** `cd` into `orders/` and start `codex` with `CODEX_HOME` and
   `ENGMEM_HOME` as printed. The first start asks you to sign in and to trust the folder. Type
   `$engmem record why OrderBook.place_order keys orders by request_id`, paste the printed
   decision, then `$engmem-save-quick` and answer `ok`.
2. **Claude Code continues in the storefront.** In another terminal, `cd` into `storefront/`
   and start Claude Code as printed:
   `ENGMEM_HOME=<demo store> claude --strict-mcp-config --mcp-config <storefront>/.mcp.json`.
   The first start asks you to trust the folder. Type the printed `/engmem add retrying the
   submission: ...` task. Before it plans, Claude Code should name the Codex record's id, its
   primer and its Source. The record's `snapshot:` line says `orders <commit>: unknown, no
   checkout of it here`: the order service's repository is not in this workspace, so engmem
   keeps the reference and says the current code was not checked. Then paste the printed
   decision, type `/engmem.save.quick`, answer `ok`, and note the id it names.
3. **Codex finds that record by its id.** Type `/new` in Codex, or start it again as in step 1.
   Type the printed `$engmem check OrderBook.place_order against the storefront record <id>:
   search engmem for <id> first`, with the id from step 2. Then paste the printed decision and
   save with `$engmem-save-quick`.
4. **Confirm the handoff.** `engmem walkthrough handoff --verify --dir ~/engmem-handoff`.

Each pasted decision ends with "Link this record to the ... repository only". That is how
`--verify` tells the clients apart (below).

## What `--verify` checks

First, **same store**: the four settings that pick a store, `--store` and `ENGMEM_HOME` in
`codex-home/config.toml`, `--store` in `storefront/.mcp.json` and `ENGMEM_HOME` in
`storefront/.claude/settings.json`, must all name the demo store. If one names another store,
names none, or cannot be parsed, the line is `mismatch: same store:`. It names each client's store
and the file that sets it, and the last line is `NOT COMPLETE: configuration mismatch`. The
handoff is not judged then. A record one client cannot see, when the two use different stores,
says nothing about whether the knowledge was handed over.

Then two checks, and `PASS` only when both hold:

- **codex to claude**: a published record linked to `orders` has a `Decision:` naming
  `request_id`, with a stated `Reason:`, `Source:` and a primer, and a search of a session linked
  to `storefront` showed it as a find: a search that showed only weak candidates does not
  count. Weakness is recorded per search, not per result, so a search that showed one reliable
  find and the Codex record as a weak candidate does count. The `ok:` line says "by a search that
  did not show only weak candidates" for that reason.
- **claude to codex**: a published record linked to `storefront` has a stated `Decision:`,
  `Reason:`, `Source:` and a primer, and a search whose query names its id, in a session linked to
  `orders`, showed that same record as a find. The id has to be in the query whole: a longer id
  that starts with it does not count, and neither does a search that names one record and shows
  another.

A `note: unavailable source:` line prints the Codex record as a search from `storefront/` shows
it: its Source, its anchor, and the snapshot saying the current code was not checked.

Anything missing is a `missing:` line with the step to repeat, and the last line is
`NOT COMPLETE`, exit 1.

## How it tells the clients apart

engmem records no client name, and this demo adds none. Each client works in its own checkout,
and the save templates link a record to the checkout it was saved in. A record linked to `orders`
is Codex's, and one linked to `storefront` is Claude Code's. A search belongs to the session its
`--session` names, so a session counts only once it has saved. That is why each step ends with a
save. A record linked to both is decided by the commit anchors it recorded; with anchors in both
or in neither, it is nobody's, and the `missing:` line says so.

`--verify` cannot see which program ran a session. If you start Claude Code in `orders/`, its
record looks like Codex's. Start each client where its step says.

## Why Claude Code is started this way

The `/engmem` commands run `engmem` in the shell, which takes the store from `ENGMEM_HOME`. If you
added an `engmem` MCP server to Claude Code yourself, it names your own store, and the MCP tools
would write there while the shell wrote to the demo. `--strict-mcp-config --mcp-config` makes
Claude Code use only the demo's `.mcp.json`. The `env` in `.claude/settings.json` covers a start
without `ENGMEM_HOME`.

`CLAUDE_CONFIG_DIR` would separate everything, but it also hides the `/engmem` commands and the
trigger rule in `~/.claude`, so it is not used.

## What was not checked by running Claude Code or Codex

These come from Claude Code's documentation, never from running it: the `.mcp.json` format, that
a project server outranks a user server with the same name, that `--strict-mcp-config` ignores
every other MCP configuration, that the `env` in project settings reaches the Bash tool, and the
trust prompt on first start, and that Claude Code keeps that trust and its transcripts in
`~/.claude.json` and `~/.claude/projects/`. The docs do not say which wins when `ENGMEM_HOME` is set both in the
shell and in `env`. Here both name the demo store. The Codex side relies on what
`docs/walkthrough-codex.md` lists under the same heading. Your first real run confirms them.

## Cleaning up

Delete the demo directory. The walkthrough command writes nothing outside it. Two things the
sessions leave elsewhere:

- Claude Code's own records of the demo session: the folder trust and project state for
  `storefront/` in `~/.claude.json`, and the transcript under `~/.claude/projects/`. Remove them
  with Claude Code's own tools if you do not want them. Codex keeps its state in the demo's
  `CODEX_HOME`, so it goes with the directory.
- The search cache under `~/.cache/engmem`, shared with your store, as in `walkthrough codex`.
