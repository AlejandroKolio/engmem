# Contract: store resolution and failure reporting

Source: `src/engmem/runtime.py`. The store resolves `--store PATH`, then `ENGMEM_HOME`, then
`~/Developer/engmem` (`ENGMEM-SPEC.md` §3). This file records what that order leaves open:
what counts as a setting, what shape the answer takes, and why a failure is printed twice.

## A blank setting is not a setting

An empty or whitespace-only `--store` or `ENGMEM_HOME` falls through to the next source.
`ENGMEM_HOME=` is the ordinary result of `export ENGMEM_HOME="$SOME_UNSET_VAR"`, and honouring
`ENGMEM_HOME="   "` once resolved to a directory literally named `"   "` under the shell's
working directory, which `engmem install` would `git init` and start writing documents into —
a wrong store that reports nothing wrong (§10, principle VIII).

The value is never trimmed: `--store "notes "` is a legal POSIX path, and silently retargeting
it would be the same bug in the other direction.

## The answer is absolute, and it is not resolved

Absolute, because the answer outlives the process that computed it: `install --agent
claude-desktop --store ./notes` writes the path into `claude_desktop_config.json`, and Claude
Desktop launches the server from a working directory of its own — the reason
`_claude_desktop_mcp_entry` already spends `sys.executable` rather than a bare `engmem`. Every
diagnostic that names the store gains the same way: `store not found: sessions` sends the
reader nowhere.

Not `Path.resolve()`, because the path is what the user reads back in every message, and a
symlinked store (`~/Developer/engmem` pointing into a synced folder or a dotfiles checkout) is
a supported setup. No security invariant rides on this: `cli._sessions_is_contained` and
`mcp_server._resolve_sessions_dir` re-resolve the store themselves, so a store handed to them
already resolved changes neither half of their comparison. `..` segments are left alone for
the same reason — `os.path.abspath` would collapse them lexically and, across a symlinked
parent, name a directory the kernel would not reach.

## Absolutising is hand-ported, not `Path.absolute()`

`resolve_store` and `default_store` go through `_absolute()`, which picks the cwd itself —
`os.path.abspath(path.drive)` when the path carries a drive, `os.getcwd()` otherwise — and
joins with `/`. `Path.absolute()` gained that drive branch only in Python 3.13 (bpo-89812);
before it, `resolve_store("C:notes")` with the process on another drive came back relative,
because a drive-letter component re-anchors a flat reparse
(`PureWindowsPath('D:/work', 'C:notes') == PureWindowsPath('C:notes')`). `pyproject.toml`
declares `requires-python = ">=3.11"` and CI runs `windows-latest` on 3.11, so that interpreter
ships; a drive-relative store written into the Desktop config is the wrong store that reports
nothing wrong.

## An unresolvable `~`/`~user` does not raise

`Path.expanduser()` raises `RuntimeError` rather than falling back when `~` or `~user` cannot
be resolved (no `HOME` and no passwd entry for the uid; `--store "~teammate/engmem"` for a
user this machine lacks). `_expand_home()`, which both `resolve_store` and `default_store`
use, catches it and returns the path unexpanded, so the literal `~nosuchuser/notes` (or
`default_store`'s bare `~`) becomes an ordinary relative component, is made absolute like any
other, and — not existing — is named by the `store not found` diagnostic
(`cli._read_sessions`) exactly as a mistyped `--store` would be. The alternative, an uncaught
traceback on stderr with stdout empty, is the swallowed error the next section exists to
prevent. `resolve_store` stays pure: whether the failure is fatal, one skipped item or an
interrupt is the command's choice, not this module's.

## A failure is printed to both streams

`fail` writes the same `error:` line to stderr and to stdout, because the consumer is an agent
that reads stdout and never stderr (§10, principle VIII; §5 applies the same rule to argparse's
own usage errors). stderr keeps the line where a human at a terminal expects it.

It returns `None` rather than exiting: the exit code belongs to the command, which knows
whether the failure is fatal (`return 2`), one document out of a batch (`_cmd_backfill` counts
it and carries on), or an interrupt (`return 130`). A `SystemExit` inside `fail` would take
that choice from every caller at once.

`engmem mcp` must never call it: there stdout is the JSON-RPC channel and one plain line
corrupts the frame in flight (`contracts/mcp-server.md`, "Transport and the stdout-purity
rule"). `tests/test_stdout_is_protocol_only.py` reads `mcp_server.py`'s syntax tree and fails
if the module so much as imports `fail`.

## `~/Developer/engmem` is the default on every platform

`Developer` is a macOS convention, and the default is deliberately not per-platform. The path
is quoted verbatim in `ENGMEM-SPEC.md` §3, `README.md`, `docs/design/data-model.md`, the
`--store` help text and the `engmem.start` template; a store is a git repository the user
already has, so moving the default would silently point an existing install at an empty
directory to buy a platform nicety `ENGMEM_HOME` already provides.
