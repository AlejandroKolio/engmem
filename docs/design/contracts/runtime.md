# Contract: store resolution and failure reporting

Source: `src/engmem/runtime.py` and `src/engmem/settings.py`. The store resolves `--store PATH`, then `ENGMEM_HOME`, then
the choice saved by `engmem store set`, then `~/Developer/engmem` (`ENGMEM-SPEC.md` §3). This
file records what that order leaves open: what counts as a setting, what shape the answer takes,
where the saved choice lives and what happens when it cannot be read, and why a failure is
printed twice. It also records the second one-line setting, the mode (US-08).

## The saved choice: one file, one line (US-05, owner decision 2026-10-07)

Before US-05 engmem had no config file at all, and `install --store X` was used once and
forgotten: the installed templates, every later shell command and the MCP entry each resolved
their own store, and CLI and MCP could work with different memories without saying so. The
owner relaxed "no config files" to exactly one exception: a file holding one line, the absolute
store path, and nothing else. A second setting would need its own decision; this file is not a
place to grow one. US-08 took that decision for the mode, as a second file of its own (below), so
the rule now reads "one-line setting files: the store path and the mode".

**Where.** `$XDG_CONFIG_HOME/engmem/store`, default `~/.config/engmem/store`, the same XDG
family as the cache (`contracts/cache.md`). `XDG_CONFIG_HOME` is honoured on every platform when
it is an absolute path; a relative one is ignored, as the XDG spec requires, because it would
make the choice depend on each command's working directory. On Windows without it, the file is
`%APPDATA%\engmem\store` (`~/AppData/Roaming` when `APPDATA` is unset) — where Windows keeps
roaming per-user settings and where `_claude_desktop_config_path` already looks, so a Windows
user finds it next to the client config it has to agree with; `~/.config` is not a place a
Windows user ever looks. The default store itself stays the same on every platform (below):
moving it would point existing installs at an empty directory, and the setting file is new, so
there is nothing to move.

**Read lazily, in fourth place.** `locate_store` reads the file only when neither `--store` nor
`ENGMEM_HOME` names a store. An override is a one-off: it never writes the file, and a broken
file does not stop a command that does not use it, so a user can still work with
`--store` while repairing the saved choice. `engmem store show` reads it either way and names a
saved choice the override is hiding.

**What the line may hold.** One line, with or without its line ending (LF or CRLF) and with a
UTF-8 byte-order mark tolerated, because Notepad writes one. The path is never trimmed, for the
same reason as the blank rule below. A leading `~` is expanded, and the result must be absolute:
a relative path has no fixed base (each command has its own cwd), and resolving it against the
file's own directory would be a rule nobody guesses. `engmem store set` always writes the
absolute path, so only a hand edit can produce one.

**An unusable file is an error, never "no choice".** Empty or blank, more than one line, a NUL
byte, invalid UTF-8, a relative path, a directory or an unreadable file at that path, and a
symlink whose target is gone all raise `StoreSettingError` with the file and the reason. A
command stops with exit 2 (`cli.main` turns the error into `fail` in one place; `engmem mcp`
writes it to stderr only). Falling back to `~/Developer/engmem` would search and write a
different memory while reporting nothing wrong — exactly the failure US-05 removes (AC-05.3).
Only a file that does not exist (and is not a dangling link) means "no saved choice", and then
the behaviour is the one from before US-05, byte for byte.

**A saved store that does not exist** is not this module's error: `resolve_store` stays pure and
the commands name `store/sessions` as missing, as they do for a mistyped `--store`, now with a
pointer to `engmem store show` to see which setting chose the path. Neither the CLI nor the MCP
write tools look anywhere else.

**No `store unset`** (owner decision, 2026-10-08): deleting the file is the way back to the
default, and `store show` prints its path. **Install never saves** either (`contracts/install.md`).

**Writing it.** `save_store` stages a sibling temp file and replaces atomically (`staging`), so
a crash leaves the old choice or the new one, never half a path. A symlinked setting file is
written through, like the user files install edits (`contracts/install.md`). A path with a line
break, or one that cannot be encoded as UTF-8, is refused rather than written as something the
reader would reject.

## The saved mode: a second one-line file (US-08, owner decision 2026-10-08)

`$XDG_CONFIG_HOME/engmem/mode`, next to the store file and found by the same rules
(`settings.setting_file`, Windows `%APPDATA%` included), holds one word: `daily` or `research`.
`engmem mode set daily|research` writes it the way `store set` writes the store, atomically and
through a symlink; `engmem mode show` prints `mode: <mode>` on its first line, the line the
start template reads, then where it came from. No file means `daily`, the default the owner
chose: research mode is opt-in, so ordinary work never pays for the experiment's ritual.

**A second file, not a second line.** The store file's reader refuses anything but one line
holding an absolute path, and that strictness is what makes a broken store choice a named
error; a second line there would mean loosening the one check US-05 depends on. Two files keep
each reader exact and let one be repaired or deleted without touching the other.

**Tolerant of how a person types it, strict about what it says.** Surrounding whitespace, case,
a line ending and a UTF-8 BOM are accepted (`Research\r\n` reads as `research`), because none of
them can change which of two words was meant. Empty, more than one line, invalid UTF-8, a
directory or an unreadable file, and any other word raise `ModeSettingError` with the file and
the cause. `cli.main` turns it into exit 2 on both streams, like `StoreSettingError`. Reading a
broken file as `daily` would silently drop a user who chose research out of the experiment.

**Read only where the mode decides something.** `engmem mode show`, the MCP server's start
prompt and `engmem_create_draft` read it, each time they run, so a change applies to the next
session even in a server process that has been running since before the change (AC-08.4).
Search, roles, telemetry and backfill never read it: a broken mode file is not a reason a search
fails. Templates without a shell read the file by hand, with the same default.

**No environment override.** The store has `ENGMEM_HOME` because a one-off store is a real need;
a per-shell mode would let the shell, which runs the template, and the MCP server, launched by
the client with an environment of its own, record different modes for one session, the
divergence US-05 removed for the store. One file is the one answer both read.

**`settings.py` and not `runtime.py`.** The mode is read inside `engmem mcp`, whose stdout is the
protocol channel. `runtime` holds `fail`, which writes to stdout, and
`tests/test_stdout_is_protocol_only.py` keeps every module the server imports free of stdout
prints. The setting-file mechanics (location, read, atomic write) moved to `settings.py`, which
both `runtime` and `mcp_server` import; the store-specific rules stayed in `runtime`.

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
claude-desktop --store ./notes` writes the path into `claude_desktop_config.json` (and `--agent
codex` into `config.toml`), and the client launches the server from a working directory of its
own — the reason `_stdio_mcp_entry` already spends `sys.executable` rather than a bare `engmem`. Every
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
directory to buy a platform nicety `ENGMEM_HOME` and `engmem store set` already provide.
