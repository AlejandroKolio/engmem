# Walkthrough: the first save-and-find cycle in Codex

This walkthrough has a real Codex session save one decision and a new session find it again,
before you load your own history. It runs on a demo store of its own. Your store, your
`~/.codex` and your saved engmem settings are not changed.

Nothing here promises a duration. The walkthrough proves the cycle works on your machine. It
does not measure how long that takes.

## Before you start

- Codex is installed and signed in, and you have connected engmem to it with
  `engmem install --agent codex`. The walkthrough uses the skills that command put in
  `~/.agents/skills` (`$engmem`, `$engmem-save-quick`). It does not install them.
- `git` is on `PATH`. The demo store and the demo workspace are both git repositories.
- The demo sessions record your saved mode (`engmem mode show`). In research mode `$engmem`
  also runs the Gate 1 baseline step. Daily mode is enough for the walkthrough.

## Build the demo

```
engmem walkthrough codex --dir ~/engmem-demo
```

The directory must be new, empty, or one this walkthrough built before. The command refuses a
directory `engmem walkthrough handoff` built, any other directory, and any path it would write
through that is a symbolic link or leads outside the directory, such as a Windows junction. It
creates:

| Path | What it is |
|---|---|
| `store/` | The demo store. Every engmem step of the walkthrough names it. |
| `codex-home/` | A Codex config for the demo only, used when `CODEX_HOME` points at it. |
| `workspace/` | A small git repository with `demo_orders/orders.py` and its acceptance check. |

The directory is resolved first, so on macOS `/tmp/demo` becomes `/private/tmp/demo`, and
every path in the demo config is that resolved one.

`codex-home/config.toml` starts with three settings. `sandbox_mode = "workspace-write"`.
`writable_roots` holds the demo store, so the shell `engmem` commands Codex runs can write
drafts, saves and telemetry there. `[shell_environment_policy] set` gives those commands
`ENGMEM_HOME` = the demo store. Then the walkthrough adds the same `[mcp_servers.engmem]`
block `engmem install --agent codex` writes, with `--store` = the demo store, and the same
trigger rule in `codex-home/AGENTS.md`.

Before printing any Codex step, the command runs step 0, `engmem doctor --agent codex`, against
the demo's `CODEX_HOME` and store, and prints its report. It stops with `not ready` when
doctor reports an error, or a warning about the sandbox, the MCP entry or the MCP command. Each
of those means Codex would write to another store or could not write at all. The line names
the cause and the fix. Fix it and run the same command again.

## The steps

The command prints each step with the exact text to type. In short:

1. **Start Codex on the demo.** In a new terminal, `cd` into `workspace/` and start
   `codex` with `CODEX_HOME` and `ENGMEM_HOME` set as printed. The first start with this
   `CODEX_HOME` asks you to sign in and to trust the folder. On Windows the printed line is
   PowerShell, and the two variables stay set in that window until you close it. The cmd.exe
   form is printed under it.
2. **Session 1 saves the decision.** Type `$engmem record why OrderBook.place_order keys
   orders by request_id`. Then paste the printed message. It states the decision ("repeating
   an operation with the same request_id does not create a second order"), the reason, the
   rejected alternative and the source. The short capture copies these and never makes them
   up. Then type `$engmem-save-quick` and answer the preview with `ok`.
3. **See the record.** `engmem search "retry order request_id" --store <demo store>`.
4. **Session 2 is a new session.** Type `/new`, or quit and start Codex again. Then type the
   printed task: `$engmem add retrying the operation: place_order_with_retry(...)`. Before it
   plans, the agent should name the record's id and its Source.
5. **The acceptance check.** In `workspace/`, run `python -m demo_orders.check`. It places two
   identical requests and expects one order. Then a first attempt that times out after the
   order was placed, and a retry, again expecting one order.
6. **Confirm the cycle.** `engmem walkthrough codex --verify --dir ~/engmem-demo`.

## What `--verify` checks

`--verify` prints one line per check and says `PASS` only when all three hold:

- **record**: a published document in the demo store has a `Decision:` line naming
  `request_id`, with a `Reason:` and a `Source:` that are not "not stated in the available
  material". A draft does not count. Only these labelled lines are read, which is what
  `$engmem-save-quick` writes. A full `$engmem-save` document is not recognised. If session 2
  saves a `request_id` record too, each one is a candidate.
- **recall**: a search attributed to another session showed one of those records as a find. A
  search without `--session`, the record's own session, or a search that showed it only as a
  weak candidate does not count. It does not check that the session came later, or that its id
  names a demo document.
- **acceptance check**: the check engmem ships exits 0. `--verify` never runs the workspace's
  `demo_orders/check.py`, because the agent can edit it, and says so in a `note:` line when that
  copy was changed. The check imports the workspace's `demo_orders`, which is code the agent
  edited, and runs it with the Python engmem runs on, in the directory you made for it.

Anything missing is a `missing:` line with what to repeat, and the last line is
`NOT COMPLETE`, exit 1. When Codex says the sandbox or MCP refused a write, the record or the
recall is missing, and the line points you back to step 0 for the reason.

`--verify` cannot see whether the agent *said* the id and the Source in session 2. That is in
the Codex transcript. Recording that transcript is the owner acceptance for this story (E-05).

## Running it again

Running the setup again changes nothing that is already there. The MCP block and the trigger
rule are not added twice, anything you added to `codex-home/` stays, and workspace files the
agent changed are kept. Only missing files are written again.

## What was not checked by running Codex

Three things the demo relies on were confirmed only from the strings in the Codex 0.160.0
binary, never by running Codex: the `shell_environment_policy.set` setting, Codex reading skills
from `~/.agents/skills` whatever `CODEX_HOME` is, and how `writable_roots` matches paths. Your
first real run of the walkthrough is what confirms them.

## Why the demo has its own `CODEX_HOME`

The engmem entry in your own `~/.codex/config.toml` launches the MCP server with `--store`
pointing at your store. If you ran the demo against that config with only `ENGMEM_HOME` set,
the shell commands would use the demo store but the MCP tools would still write to your store.
A separate `CODEX_HOME` keeps both on the demo.

That separation costs one sign-in. Codex keeps its login under `CODEX_HOME`, and the
walkthrough does not copy your credentials there.

Skills are the exception: Codex reads them from `~/.agents/skills` whatever `CODEX_HOME` is.
They name no store. The `$engmem` skill runs `engmem` commands, which take the demo store from
`ENGMEM_HOME`.

The search cache is not separated. Shell `engmem` commands under the sandbox cannot write
`~/.cache/engmem` and continue without it, with a warning on stderr. The demo's MCP server
shares the cache with your store, which can drop cached entries for your documents. They are
rebuilt on your next search (contracts/cache.md). No document is touched.

## Cleaning up

Delete the demo directory. The walkthrough command writes nothing outside it. The search
cache above is the one piece the demo sessions share with your store.
